import asyncio
import tempfile
import unittest

from bili_agent.memory import Memory
from bili_agent.messaging import Messaging
from bili_agent.protocol import RpcError
from bili_agent.social import Social
from bili_agent.store import Store
from .test_messaging import FakeModel, FakePlatform


class RelationsPlatform(FakePlatform):
    def __init__(self):
        super().__init__()
        self.following = self.mutual = self.blocked = False
        self.follow_count = 0
        self.fail_follow = False
        self.operations = []

    async def request(self, operation, params):
        self.operations.append(operation)
        if operation == 'relation':
            return {'following': self.following, 'followed_by': self.mutual,
                    'mutual': self.following and self.mutual and not self.blocked, 'blocked': self.blocked}
        if operation == 'follow':
            self.follow_count += 1
            if self.fail_follow:
                raise RpcError(-32010, 'PLATFORM_TIMEOUT')
            self.following = True
            return {'requested': True}
        return await super().request(operation, params)


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.platform = RelationsPlatform()
        self.dm = Messaging(self.store, self.platform, FakeModel(), Memory(self.store), True, ['33'])
        self.social = Social(self.store, self.dm)
        self.dm.friends = self.social
        self.flow = self.social.connections

    async def asyncTearDown(self):
        await self.social.close()
        await self.dm.close()
        self.store.close()
        self.temp.cleanup()

    async def connect(self, **changes):
        return await self.flow.connect({'peer_uid': '22', 'name': '测试好友', 'interests': ['编程'],
                                       'recommend': True, 'auto_send': True, **changes}, '你好，我是测试角色。')

    async def check(self):
        self.flow.control('22', 'check')
        await self.flow.tick()

    async def test_panel_follow_waits_for_mutual_and_greets_once_across_restart(self):
        await self.connect()
        self.assertNotIn('22', self.dm.allowed_peers)
        await self.flow.tick()
        self.assertEqual(self.platform.follow_count, 1)
        self.assertEqual(self.flow.get('22')['state'], 'waiting_mutual')
        self.assertEqual(self.platform.sent, [])
        self.assertNotIn('22', self.dm.allowed_peers)
        self.assertEqual(self.platform.operations[1:5], ['identity', 'relation', 'follow', 'relation'])
        self.platform.mutual = True
        await self.check()
        self.assertEqual(self.flow.get('22')['state'], 'ready')
        self.assertIn('22', self.dm.allowed_peers)
        self.assertEqual(len(self.platform.sent), 1)
        self.assertEqual(self.platform.sent[0]['text'], '你好，我是测试角色。')
        await self.connect()
        await self.check()
        self.social = Social(self.store, self.dm)
        self.dm.friends = self.social
        self.flow = self.social.connections
        self.assertNotIn('22', self.dm.allowed_peers, 'restart requires fresh relationship confirmation')
        await self.flow.tick()
        self.assertEqual(self.flow.get('22')['state'], 'ready')
        self.assertEqual((self.platform.follow_count, len(self.platform.sent)), (1, 1))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM dm_outbox').fetchone()[0], 1)
        self.dm.account = '999'
        self.assertNotIn('22', self.dm.allowed_peers)
        self.assertIsNone(self.flow.get('22'))

    async def test_sharing_cannot_pass_greeting_or_pause_even_with_host_scope(self):
        self.dm.configured_peers.add('22')
        await self.connect()
        draft = self.dm.compose('22', '分享内容', 'test-sharing-22')
        with self.assertRaisesRegex(RpcError, 'FRIEND_NOT_MUTUAL'):
            await self.dm.publish(draft['id'])
        self.platform.following = self.platform.mutual = True
        self.dm.send_enabled = False
        await self.flow.tick()
        self.assertEqual(self.flow.get('22')['state'], 'greeting')
        self.assertEqual(self.flow.get('22')['error'], 'DM_SEND_DISABLED')
        self.dm.send_enabled = True
        with self.assertRaisesRegex(RpcError, 'FRIEND_GREETING_PENDING'):
            await self.dm.publish(draft['id'])
        await self.check()
        self.assertEqual(len(self.platform.sent), 1)
        self.flow.control('22', 'pause')
        with self.assertRaisesRegex(RpcError, 'FRIEND_NOT_MUTUAL'):
            await self.dm.publish(draft['id'])
        self.assertEqual(self.platform.follow_count, 0, 'already followed users do not get another follow')
        self.flow.control('22', 'resume')
        self.assertEqual(self.flow.get('22')['mutual'], 0, 'resume requires a fresh mutual check')
        self.platform.mutual = False
        await self.flow.tick()
        self.assertEqual(self.flow.get('22')['state'], 'waiting_mutual')
        self.assertEqual(self.social.delivery_status('22')['reason'], 'FRIEND_NOT_MUTUAL')
        self.platform.following = False
        await self.check()
        self.assertEqual(self.platform.follow_count, 0, 'never undo a manual unfollow')
        self.assertEqual(self.flow.get('22')['error'], 'FOLLOW_NOT_CONFIRMED')

    async def test_uncertain_greeting_is_never_replayed_or_replaced(self):
        self.platform.following = self.platform.mutual = True
        self.platform.fail_send = True
        await self.connect()
        await self.flow.tick()
        self.assertEqual(self.flow.get('22')['state'], 'uncertain')
        self.social = Social(self.store, self.dm)
        self.dm.friends = self.social
        self.flow = self.social.connections
        await self.connect()
        await self.flow.tick()
        self.assertEqual(self.flow.get('22')['state'], 'uncertain')
        self.assertEqual(len(self.platform.sent), 1)
        self.assertNotIn('22', self.dm.allowed_peers)

    async def test_follow_timeout_checks_before_retry_and_can_recover_remote_success(self):
        self.platform.fail_follow = True
        await self.connect()
        await self.flow.tick()
        self.assertEqual(self.flow.get('22')['error'], 'PLATFORM_TIMEOUT')
        await self.check()
        self.assertEqual(self.platform.follow_count, 1)
        self.assertEqual(self.flow.get('22')['error'], 'FOLLOW_NOT_CONFIRMED')
        self.flow.control('22', 'retry')
        self.platform.fail_follow = False
        await self.flow.tick()
        self.assertEqual(self.platform.follow_count, 2)
        self.assertEqual(self.flow.get('22')['state'], 'waiting_mutual')
        self.platform.mutual = True
        await self.check()
        self.assertEqual(self.flow.get('22')['state'], 'ready')

    async def test_invalid_or_self_profile_and_account_change_never_follow(self):
        for changes in ({'peer_uid': '77'}, {'peer_uid': 'bad'}, {'interests': []}, {'auto_send': 'yes'}):
            with self.assertRaises(RpcError):
                await self.connect(**changes)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM friend_connections').fetchone()[0], 0)
        await self.connect()
        self.platform.uid = '88'
        await self.flow.tick()
        self.assertEqual(self.flow.get('22')['error'], 'DATA_DIR_ACCOUNT_MISMATCH')
        self.assertEqual((self.platform.follow_count, len(self.platform.sent)), (0, 0))

    async def test_relation_block_and_inflight_pause_stop_greeting(self):
        await self.connect()
        self.platform.blocked = True
        await self.flow.tick()
        self.assertEqual(self.flow.get('22')['error'], 'FRIEND_RELATION_BLOCKED')
        self.assertEqual(self.platform.follow_count, 0)
        self.platform.blocked = False
        original = self.platform.request
        async def pause_during_follow(operation, params):
            result = await original(operation, params)
            if operation == 'follow':
                self.flow.control('22', 'pause')
                self.platform.mutual = True
            return result
        self.platform.request = pause_during_follow
        await self.check()
        self.assertEqual(self.platform.sent, [])
        self.assertNotIn('22', self.dm.allowed_peers)

    async def test_background_progress_and_shutdown(self):
        await self.connect()
        self.platform.mutual = True
        self.flow.start()
        async with asyncio.timeout(2):
            while self.flow.get('22')['state'] != 'ready':
                await asyncio.sleep(.005)
        await self.flow.close()
        self.assertTrue(self.flow.task.done())
        self.assertEqual(len(self.platform.sent), 1)
