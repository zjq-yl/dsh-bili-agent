"""Persist explicitly requested follows, mutual confirmation and one greeting per friend."""
import asyncio
import time

from .messaging import uid, MAX_MESSAGE_LENGTH
from .protocol import RpcError


class FriendConnections:
    """Advance only panel-authorized recipients; never discover new follow targets."""

    def __init__(self, social):
        self.social, self.dm, self.db = social, social.messaging, social.db
        self.task = None
        self.wake = asyncio.Event()
        self.lock = asyncio.Lock()
        self.db.executescript('''CREATE TABLE IF NOT EXISTS friend_connections(
            account TEXT NOT NULL, peer TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
            enabled INTEGER NOT NULL DEFAULT 1, follow_attempted INTEGER NOT NULL DEFAULT 0,
            following INTEGER NOT NULL DEFAULT 0, mutual INTEGER NOT NULL DEFAULT 0,
            greeting TEXT NOT NULL, greeting_id TEXT, checked REAL NOT NULL DEFAULT 0,
            next_check REAL NOT NULL DEFAULT 0, error TEXT, created REAL NOT NULL,
            PRIMARY KEY(account,peer));''')
        with self.db:
            self.db.execute('UPDATE friend_connections SET mutual=0,next_check=0 WHERE enabled=1')

    def get(self, peer: str) -> dict | None:
        row = self.db.execute('SELECT * FROM friend_connections WHERE account=? AND peer=?',
                              (self.dm.account, peer)).fetchone()
        return dict(row) if row else None

    def allowed_peers(self) -> set[str]:
        """Grant only explicit, enabled, mutually confirmed recipients in the current account."""
        return {row[0] for row in self.db.execute("""SELECT peer FROM friend_connections
            WHERE account=? AND enabled=1 AND mutual=1 AND state IN ('greeting','ready')""", (self.dm.account,))}

    def update(self, peer: str, **values) -> None:
        # Column names are internal constants, never passed from RPC input.
        with self.db:
            self.db.execute('UPDATE friend_connections SET ' + ','.join(f'{key}=?' for key in values)
                            + ' WHERE account=? AND peer=?', (*values.values(), self.dm.account, peer))

    async def connect(self, profile: dict, greeting: str = '') -> dict:
        """Save the profile and a durable follow request; network work proceeds in the background."""
        if not isinstance(profile, dict) or not isinstance(greeting, str) or len(greeting) > MAX_MESSAGE_LENGTH:
            raise RpcError(-32602, 'INVALID_FRIEND_PROFILE')
        peer = uid(profile.get('peer_uid'))
        await self.dm.bind()
        previous = self.get(peer)
        if not previous and self.db.execute('SELECT COUNT(*) FROM friend_connections WHERE account=?',
                                           (self.dm.account,)).fetchone()[0] >= 100:
            raise RpcError(-32602, 'FRIEND_CONNECTION_LIMIT')
        greeting = greeting.strip() or f"你好呀，我是{self.dm.memory.profile()['name']}。很高兴认识你！以后看到适合你的内容会分享给你，也可以直接告诉我你想看什么。"
        # A connected friend has explicit per-account authorization; legacy save remains local-only.
        self.social.save(profile, pending_connection=True)
        with self.db:
            self.db.execute('''INSERT OR IGNORE INTO friend_connections(account,peer,greeting,created)
                VALUES(?,?,?,?)''', (self.dm.account, peer, greeting, time.time()))
        if not previous:
            self.dm.store.event('friend', '已接收关注请求，互关确认后发送一次招呼', peer)
        self.wake.set()
        return self.social.profile(peer)

    def control(self, peer: str, action: str) -> dict:
        """Check, retry a failed follow, or stop the workflow without unfollowing remotely."""
        peer = uid(peer)
        row = self.get(peer)
        if not row or action not in ('check', 'retry', 'pause', 'resume'):
            raise RpcError(-32602, 'INVALID_FRIEND_CONNECTION_ACTION')
        if action in ('pause', 'resume'):
            self.update(peer, enabled=int(action == 'resume'), mutual=0, next_check=0)
        elif action == 'retry':
            if self.lock.locked():
                raise RpcError(-32002, 'FRIEND_CONNECTION_BUSY')
            # Re-read the relation before retrying any write. Never recreate the greeting.
            self.update(peer, follow_attempted=0, next_check=0, error=None)
        else:
            self.update(peer, next_check=0)
        self.dm.store.event('friend', {'check': '已请求检查互关状态', 'retry': '已请求重试关注',
                                      'pause': '已暂停好友流程与该对象发送', 'resume': '已恢复好友关系检查'}[action], peer)
        self.wake.set()
        return self.social.profile(peer)

    def guard(self, draft) -> None:
        """Enforce greeting-before-sharing inside the common private-message send lock."""
        row = self.get(draft['peer'])
        if not row:
            return
        if not row['enabled'] or not row['mutual']:
            raise RpcError(-32040, 'FRIEND_NOT_MUTUAL')
        if row['state'] == 'ready':
            return
        if row['state'] == 'greeting' and row['greeting_id'] == draft['id']:
            return
        raise RpcError(-32040, 'FRIEND_GREETING_PENDING')

    def state(self, peer: str, state: str, error: str | None = None) -> None:
        old = self.get(peer)
        self.update(peer, state=state, error=error)
        if old['state'] != state or old['error'] != error:
            labels = {'following': '正在关注 B 站账号', 'waiting_mutual': '已关注，等待对方回关',
                      'greeting': '已确认互关，招呼等待发送', 'ready': '招呼已确认发送，可以按偏好分享视频',
                      'blocked': '好友流程等待处理', 'uncertain': '招呼发送结果待确认，不重复发送'}
            self.dm.store.event('friend', labels.get(state, state), peer, error)

    async def tick(self) -> None:
        """Advance one due recipient, isolating platform waits from video and chat loops."""
        if not self.dm.account or self.lock.locked():
            return
        async with self.lock:
            row = self.db.execute('''SELECT * FROM friend_connections WHERE account=? AND enabled=1
                AND next_check<=? ORDER BY next_check,created LIMIT 1''', (self.dm.account, time.time())).fetchone()
            if not row:
                return
            peer = row['peer']
            self.update(peer, next_check=time.time() + 60)
            try:
                await self.advance(dict(row))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.state(peer, 'blocked', self.dm.safe_error(exc))
                self.update(peer, next_check=time.time() + 300)

    async def advance(self, row: dict) -> None:
        peer = row['peer']
        await self.dm.bind()  # Guard against changed credentials before any platform write.
        relation = await self.dm.platform.request('relation', {'peer_uid': peer})
        self.update(peer, following=int(relation['following']), mutual=int(relation['mutual']), checked=time.time())
        if relation['following']:
            # A later manual unfollow must not be undone by this workflow.
            self.update(peer, follow_attempted=1)
        if not self.get(peer)['enabled']:
            return
        if relation['blocked']:
            self.state(peer, 'blocked', 'FRIEND_RELATION_BLOCKED')
            self.update(peer, next_check=time.time() + 300)
            return
        if not relation['following']:
            if row['follow_attempted']:
                self.state(peer, 'blocked', 'FOLLOW_NOT_CONFIRMED')
                self.update(peer, next_check=time.time() + 300)
                return
            self.update(peer, follow_attempted=1)
            self.state(peer, 'following')
            await self.dm.platform.request('follow', {'peer_uid': peer})
            relation = await self.dm.platform.request('relation', {'peer_uid': peer})
            self.update(peer, following=int(relation['following']), mutual=int(relation['mutual']), checked=time.time())
            if not relation['following']:
                self.state(peer, 'blocked', 'FOLLOW_NOT_CONFIRMED')
                return
        if not self.get(peer)['enabled']:
            return
        if not relation['mutual']:
            self.state(peer, 'waiting_mutual')
            return
        draft = self.db.execute('SELECT * FROM dm_outbox WHERE account=? AND id=?',
                                (self.dm.account, row['greeting_id'])).fetchone() if row['greeting_id'] else None
        if not draft:
            draft = self.dm.compose(peer, row['greeting'], f'friend-hello-{peer}')
            self.update(peer, greeting_id=draft['id'])
        if draft['state'] == 'sent':
            self.state(peer, 'ready')
            self.update(peer, next_check=time.time() + 300)
            return
        if draft['state'] in ('uncertain', 'sending', 'cancelled'):
            self.state(peer, 'uncertain', draft['error'] or 'DM_SEND_UNCERTAIN')
            self.update(peer, next_check=time.time() + 300)
            return
        self.state(peer, 'greeting')
        if not self.dm.send_enabled:
            self.state(peer, 'greeting', 'DM_SEND_DISABLED')
            return
        result = await self.dm.publish_when_ready(draft['id'])
        if result['state'] == 'sent':
            self.state(peer, 'ready')
            self.social.wake.set()
        elif result['state'] == 'uncertain':
            self.state(peer, 'uncertain', result.get('error') or 'DM_SEND_UNCERTAIN')
        else:
            self.state(peer, 'greeting', 'DM_SEND_LIMIT')
            self.update(peer, next_check=time.time() + 10)

    def start(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())

    async def run(self) -> None:
        while True:
            self.wake.clear()
            await self.tick()
            try:
                await asyncio.wait_for(self.wake.wait(), 5)
            except TimeoutError:
                pass

    async def close(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
