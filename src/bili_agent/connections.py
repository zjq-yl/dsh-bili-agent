"""Persist explicitly requested follows, mutual confirmation and one greeting per friend."""
import asyncio
import time

from .messaging import uid, MAX_MESSAGE_LENGTH
from .protocol import RpcError


class FriendConnections:
    """Advance only selected recipients; directory reads never initiate follows."""

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
        if 'greet_required' not in {row[1] for row in self.db.execute('PRAGMA table_info(friend_connections)')}:
            with self.db:
                self.db.execute('ALTER TABLE friend_connections ADD COLUMN greet_required INTEGER NOT NULL DEFAULT 1')
        with self.db:
            self.db.execute('UPDATE friend_connections SET mutual=0,next_check=0 WHERE enabled=1')

    def get(self, peer: str) -> dict | None:
        row = self.db.execute('SELECT * FROM friend_connections WHERE account=? AND peer=?',
                              (self.dm.account, peer)).fetchone()
        return dict(row) if row else None

    async def directory(self, kind: str = 'mutual', page: int = 1) -> dict:
        """List real platform contacts without granting permission or starting follow requests."""
        await self.dm.bind()
        result = await self.dm.platform.request('contacts', {'kind': kind, 'page': page, 'account_uid': self.dm.account})
        return {**result, 'checked': time.time(), 'items': [{**item, 'connection': self.get(item['peer']),
                'has_profile': self.social.profile(item['peer']) is not None} for item in result['items']]}

    async def lookup(self, peer: str) -> dict:
        """Check one UID directly; absence from a paginated list never proves absence of friendship."""
        peer = uid(peer)
        await self.dm.bind()
        if peer == self.dm.account:
            raise RpcError(-32602, 'INVALID_DM_CONTENT_OR_RECIPIENT')
        relation = await self.dm.platform.request('relation', {'peer_uid': peer})
        return {'peer': peer, **relation, 'kind': 'blocked' if relation['blocked'] else 'mutual' if relation['mutual']
                else 'following' if relation['following'] else 'followers' if relation['followed_by'] else 'stranger',
                'connection': self.get(peer), 'checked': time.time()}

    def register(self, peer: str, name: str = '', greet: bool = True) -> None:
        """Create a selected recipient only, without inventing interests or enabling recommendations."""
        if not self.social.profile(peer):
            self.social.save({'peer_uid': peer, 'name': name or peer, 'interests': [],
                              'recommend': False, 'auto_send': False}, pending_connection=True)
        greeting = f"你好呀，我是{self.dm.memory.profile()['name']}。很高兴认识你！"
        with self.db:
            self.db.execute('''INSERT OR IGNORE INTO friend_connections(account,peer,greeting,greet_required,created)
                VALUES(?,?,?,?,?)''', (self.dm.account, peer, greeting, int(greet), time.time()))

    async def contact(self, peer: str, action: str, name: str = '') -> dict:
        """Explicitly follow back or remember a decision not to follow; listing alone does neither."""
        if action not in ('follow_back', 'decline') or not isinstance(name, str) or len(name) > 50:
            raise RpcError(-32602, 'INVALID_FRIEND_CONNECTION_ACTION')
        found = await self.lookup(peer)
        peer = found['peer']
        if found['blocked'] or not found['followed_by']:
            raise RpcError(-32040, 'CONTACT_NOT_FOLLOWER')
        if found['mutual']:
            return found  # Already a friend: do not create another greeting.
        self.register(peer, name)
        self.update(peer, enabled=int(action == 'follow_back'), state='pending' if action == 'follow_back' else 'declined',
                    following=int(found['following']), mutual=0, checked=time.time(), next_check=0, error=None,
                    follow_attempted=0)
        self.dm.store.event('friend', '已选择回关，确认互关后打招呼' if action == 'follow_back' else '已选择不回关，不自动添加或私信', peer)
        self.wake.set()
        return self.social.profile(peer)

    async def prepare_send(self, draft) -> bool:
        """Resolve real friendship before every send, queueing the selected message behind an add if needed."""
        peer = draft['peer']
        row = self.get(peer)
        if row and not row['enabled']:
            raise RpcError(-32040, 'FRIEND_CONNECTION_PAUSED')
        # A recent negative check may be reused while the dedicated workflow waits for a follow-back.
        if row and row['state'] not in ('ready', 'greeting') and time.time() - row['checked'] < 60:
            if row['state'] == 'uncertain':
                raise RpcError(-32040, 'DM_SEND_UNCERTAIN')
            self.defer(draft['id'], 'FRIEND_ADD_REQUIRED')
            return False
        found = await self.lookup(peer)
        row = self.get(peer)  # A pause may arrive while the platform lookup is in flight.
        if row and not row['enabled']:
            raise RpcError(-32040, 'FRIEND_CONNECTION_PAUSED')
        if found['blocked']:
            if row:
                self.update(peer, mutual=0)
            raise RpcError(-32040, 'FRIEND_RELATION_BLOCKED')
        if not row:
            self.register(peer, greet=not found['mutual'])
            row = self.get(peer)
        self.update(peer, following=int(found['following']), mutual=int(found['mutual']), checked=time.time())
        if found['following']:
            self.update(peer, follow_attempted=1)
        if not found['mutual']:
            self.state(peer, 'waiting_mutual' if found['following'] else 'pending')
            self.defer(draft['id'], 'FRIEND_ADD_REQUIRED')
            self.wake.set()
            return False
        if not row['greet_required']:
            self.state(peer, 'ready')
        elif row['state'] != 'ready' and row['greeting_id'] != draft['id']:
            self.defer(draft['id'], 'FRIEND_GREETING_PENDING')
            self.update(peer, next_check=0)
            self.wake.set()
            return False
        self.guard(draft)
        return True

    def defer(self, draft_id: str, error: str) -> None:
        with self.db:
            self.db.execute("UPDATE dm_outbox SET error=? WHERE id=? AND account=? AND state='draft'", (error, draft_id, self.dm.account))

    async def send_message(self, peer: str, content: str, command_id: str) -> dict:
        """Send a user-selected message through the same friendship gate as agent recommendations."""
        await self.dm.bind()
        draft = self.dm.compose(peer, content, command_id)
        return await self.dm.publish(draft['id'])

    async def save_profile(self, profile: dict) -> dict:
        """Allow recommendation preferences for an existing platform friend without refollowing."""
        if not isinstance(profile, dict):
            raise RpcError(-32602, 'INVALID_FRIEND_PROFILE')
        peer = uid(profile.get('peer_uid'))
        await self.dm.bind()
        if (profile.get('auto_send') is True
                and peer not in self.dm.allowed_peers and not self.get(peer)):
            found = await self.lookup(peer)
            if not found['mutual']:
                raise RpcError(-32040, 'FRIEND_NOT_MUTUAL')
            self.social.save(profile, pending_connection=True)
            self.register(found['peer'], greet=False)
            self.update(found['peer'], following=1, mutual=1, state='ready', checked=time.time(), follow_attempted=1)
            return self.social.profile(found['peer'])
        return self.social.save(profile)

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
            if state == 'ready' and not old['greet_required']:
                labels['ready'] = '已确认互关，可以发送私信'
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
        if not row['greet_required']:
            self.state(peer, 'ready')
            self.update(peer, next_check=time.time() + 300)
            await self.send_waiting(peer)
            return
        draft = self.db.execute('SELECT * FROM dm_outbox WHERE account=? AND id=?',
                                (self.dm.account, row['greeting_id'])).fetchone() if row['greeting_id'] else None
        if not draft:
            draft = self.dm.compose(peer, row['greeting'], f'friend-hello-{peer}')
            self.update(peer, greeting_id=draft['id'])
        if draft['state'] == 'sent':
            self.state(peer, 'ready')
            self.update(peer, next_check=time.time() + 300)
            await self.send_waiting(peer)
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
            self.update(peer, next_check=time.time() + 10)
        elif result['state'] == 'uncertain':
            self.state(peer, 'uncertain', result.get('error') or 'DM_SEND_UNCERTAIN')
        else:
            self.state(peer, 'greeting', 'DM_SEND_LIMIT')
            self.update(peer, next_check=time.time() + 10)

    async def send_waiting(self, peer: str) -> None:
        if not self.dm.send_enabled:
            return
        draft = self.db.execute("""SELECT id FROM dm_outbox WHERE account=? AND peer=? AND state='draft'
            AND error IN ('FRIEND_ADD_REQUIRED','FRIEND_GREETING_PENDING') ORDER BY created LIMIT 1""", (self.dm.account, peer)).fetchone()
        if draft:
            try:
                await self.dm.publish_when_ready(draft['id'])
            except RpcError as exc:
                # Delivery windows/caps must not turn a healthy friendship into a failed add.
                self.dm.store.event('friend', '待发内容尚未满足发送条件', peer, self.dm.safe_error(exc))
                self.update(peer, next_check=time.time() + 60)
            else:
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
