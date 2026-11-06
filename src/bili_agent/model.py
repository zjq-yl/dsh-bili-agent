"""通过 RPC 调用 DSH 宿主模型，并记录额度和调用结果。"""
import asyncio
from contextlib import asynccontextmanager

from .protocol import RpcError

RECOVERABLE_MODEL_ERRORS = {'HOST_CALL_FAILED', 'RPC_TIMEOUT', 'MODEL_BUSY', 'MODEL_DAILY_LIMIT', 'TimeoutError', 'ConnectionError'}


class Model:
    def __init__(self, peer, store):
        self.peer, self.store = peer, store
        self.condition = asyncio.Condition()
        self.active = False
        self.waiters = []
        self.sequence = 0
        self.reply_lock = asyncio.Lock()

    @asynccontextmanager
    async def turn(self, purpose: str):
        """Prioritize replies and requested selections between serialized model calls."""
        self.sequence += 1
        ticket = (0 if purpose in ('dm_reply', 'check') else 1 if purpose == 'requested_recommendations' else 2, self.sequence)
        async with self.condition:
            self.waiters.append(ticket)
            try:
                await self.condition.wait_for(lambda: not self.active and ticket == min(self.waiters))
                self.active = True
            finally:
                self.waiters.remove(ticket)
                self.condition.notify_all()
        try:
            yield
        finally:
            async with self.condition:
                self.active = False
                self.condition.notify_all()

    async def generate(self, purpose: str, system: str, text: str) -> str:
        """Serialize replies independently from background analysis (at most two calls)."""
        if len(system) + len(text) > 48000:
            raise RpcError(-32602, "MODEL_INPUT_TOO_LARGE")
        async with (self.reply_lock if purpose == 'dm_reply' else self.turn(purpose)):
            call_id = self.store.reserve_call(purpose)
            try:
                result = await self.peer.call("model.generate", {"system": system, "text": text, "purpose": purpose}, timeout=65)
                if not isinstance(result, dict) or not isinstance(result.get("text"), str) or not result["text"].strip():
                    raise RpcError(-32006, "INVALID_MODEL_RESULT")
                self.store.finish_call(call_id, "completed", result.get("usage"))
                return result["text"]
            except asyncio.CancelledError:
                self.store.finish_call(call_id, "cancelled", error="CANCELLED_USAGE_UNKNOWN")
                raise
            except Exception as exc:
                self.store.finish_call(call_id, "failed", error=type(exc).__name__)
                raise
