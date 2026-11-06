"""双向逐行 JSON-RPC；stdin 读取与任务执行分离，支持取消和 EOF 清理。"""
import asyncio
import json
import sys
import threading

MAX_MESSAGE = 1024 * 1024


class RpcError(Exception):
    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code, self.data = code, data


class Peer:
    def __init__(self, handler):
        self.handler = handler
        self.pending = {}
        self.tasks = {}
        self.counter = 0
        self.stopping = asyncio.Event()
        self.closed = False

    def send(self, message):
        if self.closed:
            raise RpcError(-32000, "连接已关闭")
        line = json.dumps(message, ensure_ascii=False, allow_nan=False)
        if len(line.encode("utf-8")) > MAX_MESSAGE:
            raise RpcError(-32600, "消息过大")
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    def notify(self, method, params):
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    async def call(self, method, params, timeout=60):
        self.counter += 1
        request_id = f"py-{self.counter}"
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        except (TimeoutError, asyncio.CancelledError):
            if not self.closed:
                self.notify("rpc.cancel", {"id": request_id})
            raise
        finally:
            self.pending.pop(request_id, None)

    async def dispatch(self, message):
        rid = message.get("id")
        try:
            params = message.get("params", {})
            if not isinstance(params, dict):
                raise RpcError(-32602, "params 必须是对象")
            result = await self.handler(message["method"], params)
            if rid is not None:
                self.send({"jsonrpc": "2.0", "id": rid, "result": result})
        except asyncio.CancelledError:
            if rid is not None and not self.closed:
                self.send({"jsonrpc": "2.0", "id": rid,
                           "error": {"code": -32800, "message": "已取消"}})
        except Exception as exc:
            if rid is not None and not self.closed:
                error = {"code": getattr(exc, "code", -32603),
                         "message": str(exc) if isinstance(exc, RpcError) else type(exc).__name__}
                self.send({"jsonrpc": "2.0", "id": rid, "error": error})
        finally:
            if rid is not None:
                self.tasks.pop(rid, None)

    async def serve(self):
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue(maxsize=64)

        def read_input():
            while not self.closed:
                line = sys.stdin.buffer.readline(MAX_MESSAGE + 2)
                if self.closed:
                    return
                try:
                    asyncio.run_coroutine_threadsafe(queue.put(line), loop).result()
                except Exception:
                    return
                if not line or len(line) > MAX_MESSAGE + 1:
                    return

        reader = threading.Thread(target=read_input, name="rpc-stdin", daemon=True)
        reader.start()
        stop = asyncio.create_task(self.stopping.wait())
        read = None
        try:
            while not self.stopping.is_set():
                read = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait({read, stop}, return_when=asyncio.FIRST_COMPLETED)
                if stop in done:
                    read.cancel()
                    await asyncio.gather(read, return_exceptions=True)
                    break
                line = read.result()
                if not line or len(line) > MAX_MESSAGE + 1:
                    break
                try:
                    message = json.loads(line.decode("utf-8"))
                    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                        raise ValueError()
                    rid = message.get("id")
                    if rid is not None and (type(rid) not in (str, int)):
                        raise ValueError()
                except (ValueError, UnicodeError):
                    self.send({"jsonrpc": "2.0", "id": None,
                               "error": {"code": -32700, "message": "无效 JSON-RPC 消息"}})
                    continue
                if "method" not in message:
                    future = self.pending.get(rid)
                    if future and not future.done():
                        if "error" in message:
                            error = message["error"]
                            if not isinstance(error, dict):
                                error = {}
                            future.set_exception(RpcError(error.get("code", -32000), error.get("message", "宿主失败")))
                        elif "result" in message:
                            future.set_result(message["result"])
                    continue
                if message["method"] == "rpc.cancel":
                    params = message.get("params", {})
                    if isinstance(params, dict) and type(params.get("id")) in (str, int):
                        task = self.tasks.get(params.get("id"))
                        if task:
                            task.cancel()
                    continue
                if not isinstance(message["method"], str) or rid in self.tasks:
                    self.send({"jsonrpc": "2.0", "id": rid,
                               "error": {"code": -32600, "message": "无效或重复请求"}})
                    continue
                if len(self.tasks) >= 32:
                    self.send({"jsonrpc": "2.0", "id": rid,
                               "error": {"code": -32002, "message": "BUSY"}})
                    continue
                # 本项目的业务命令均要求 ID；通知仅使用 rpc.cancel。
                if rid is None:
                    continue
                self.tasks[rid] = asyncio.create_task(self.dispatch(message))
        finally:
            stop.cancel()
            if read and not read.done():
                read.cancel()
            for task in list(self.tasks.values()):
                task.cancel()
            await asyncio.gather(*list(self.tasks.values()), stop, return_exceptions=True)
            self.closed = True
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RpcError(-32000, "连接已关闭"))
            # 宿主收到 shutdown 响应后关闭 stdin；等待读取线程退出，避免
            # Windows 在 Python finalization 时仍有线程持有标准输入锁。
            await asyncio.to_thread(reader.join, 2)
