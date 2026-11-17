import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from bili_agent.automation import VideoAgent
from bili_agent.messaging import Messaging
from bili_agent.mentions import Mentions
from bili_agent.runner import Runner
from bili_agent.social import Social
from bili_agent.store import Store
from .test_agent import FakeModel, sample
from .test_messaging import FakePlatform

VIDEO_REPLY = '{"reply":"这条视频讲了怎么给自己设定小目标。"}'


class MentionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.platform, self.model = FakePlatform(), FakeModel()
        self.runner = Runner(self.store, self.model)
        self.dm = Messaging(self.store, self.platform, self.model, self.runner.memory, True, [])
        await self.dm.bind()
        self.social = Social(self.store, self.dm)
        self.dm.friends = self.social
        self.video = VideoAgent(self.store, self.runner, self.dm, self.social, self.platform, self.model, Path(self.temp.name))
        self.flow = self.video.interaction.mentions
        self.context = {'bvid': sample()['bvid'], 'text': '你好呀', 'root_text': '公开的讨论'}
        original = self.platform.request

        async def request(operation, params):
            if operation == 'mention_context':
                return self.context
            if operation == 'send_comment':
                self.platform.sent.append(params)
                return {'rpid': '999'}
            return await original(operation, params)

        self.platform.request = request
        self.flow.next_poll = time.time() + 300
        with self.store.db:
            self.store.db.execute("INSERT INTO mentions(account,id,peer,oid,root,parent,created) VALUES('77','1','22','170001','100','101',?)", (time.time(),))

    async def asyncTearDown(self):
        await self.video.interaction.close()
        await self.video.close()
        await self.dm.close()
        await self.runner.pause()
        self.store.close()
        self.temp.cleanup()

    def row(self):
        return dict(self.store.db.execute('SELECT * FROM mentions').fetchone())

    async def tick(self):
        with self.store.db:
            self.store.db.execute('UPDATE mentions SET next_check=0')
        await self.flow.tick()

    async def analyze_sample(self):
        key = self.runner.enqueue(sample())['id']
        await self.runner.analyze(self.store.db.execute('SELECT * FROM videos WHERE id=?', (key,)).fetchone())
        return key

    async def prepare_reply(self):
        """被 @ 后先等本条视频总结完成，再依据总结生成回复。"""
        key = await self.analyze_sample()
        await self.tick()
        self.assertEqual(self.row()['state'], 'waiting_video')
        self.assertEqual(self.row()['video_id'], key)
        with patch.object(self.model, 'generate', AsyncMock(return_value=VIDEO_REPLY)) as model:
            await self.tick()
            self.assertEqual(self.row()['state'], 'planned')
            payload = json.loads(model.call_args.args[2])
            self.assertEqual(set(payload), {'comment', 'video'})
            self.assertTrue(payload['video']['summary'])
        return key

    async def test_reply_is_grounded_in_summary_and_sent_once(self):
        await self.prepare_reply()
        await self.tick()
        self.assertEqual(self.row()['state'], 'sent')
        self.assertEqual(self.platform.sent[0]['root'], '100')
        self.assertEqual(self.platform.sent[0]['parent'], '101')
        self.assertIsNone(self.social.profile('22'))
        self.flow = Mentions(self.video.interaction)
        self.flow.next_poll = time.time() + 300
        await self.tick()
        self.assertEqual(len(self.platform.sent), 1)

    async def test_uncertain_send_and_restart_never_replay(self):
        await self.prepare_reply()
        original = self.platform.request

        async def timeout(operation, params):
            if operation == 'send_comment':
                self.platform.sent.append(params)
                raise TimeoutError()
            return await original(operation, params)

        self.platform.request = timeout
        await self.tick()
        self.assertEqual(self.row()['state'], 'uncertain')
        self.flow.retry('1')
        self.flow = Mentions(self.video.interaction)
        self.flow.next_poll = time.time() + 300
        await self.tick()
        self.assertEqual(len(self.platform.sent), 1)

    async def test_new_video_is_analyzed_before_any_reply(self):
        original = self.platform.request

        async def fetch(operation, params):
            return sample() if operation == 'fetch' else await original(operation, params)

        self.platform.request = fetch
        with patch.object(self.runner, 'start'):
            await self.tick()
        self.assertEqual(self.row()['state'], 'waiting_video')
        self.assertEqual(self.store.db.execute('SELECT priority FROM videos').fetchone()[0], 0)
        video_id = self.row()['video_id']
        await self.tick()
        self.assertEqual(self.row()['state'], 'waiting_video')
        self.assertEqual(self.platform.sent, [])
        await self.runner.analyze(self.store.db.execute('SELECT * FROM videos WHERE id=?', (video_id,)).fetchone())
        with patch.object(self.model, 'generate', AsyncMock(return_value=VIDEO_REPLY)) as model:
            await self.tick()
            self.assertTrue(json.loads(model.call_args.args[2])['video']['summary'])
        self.assertEqual(self.platform.sent, [])
        await self.tick()
        self.assertEqual(self.row()['state'], 'sent')

    async def test_nonfriend_or_changed_context_cannot_receive_reply(self):
        await self.prepare_reply()
        original = self.platform.request

        async def not_mutual(operation, params):
            if operation == 'relation':
                return {'mutual': False, 'blocked': False, 'following': True, 'followed_by': False}
            return await original(operation, params)

        self.platform.request = not_mutual
        await self.tick()
        self.assertEqual(self.row()['state'], 'ignored')
        self.platform.request = original
        self.flow.update('1', state='planned')
        self.context = {**self.context, 'text': '评论已经改了'}
        await self.tick()
        self.assertEqual(self.row()['error'], 'MENTION_CONTEXT_CHANGED')
        self.assertEqual(self.platform.sent, [])

    async def test_send_switch_and_shared_public_quota(self):
        await self.prepare_reply()
        self.dm.send_enabled = False
        await self.tick()
        self.assertEqual(self.row()['state'], 'planned')
        self.dm.send_enabled = True
        self.video.configure({'comment_daily_limit': 0})
        await self.tick()
        self.assertEqual(self.platform.sent, [])
        self.video.configure({'comment_daily_limit': 1})
        await self.tick()
        self.assertFalse(self.video.interaction.comments.can_send())

    async def test_poll_filters_old_notifications_and_deduplicates(self):
        since = time.time()
        with self.store.db:
            self.store.db.execute('INSERT INTO mention_sync(account,since) VALUES(?,?)', ('77', since))
        item = {'id': 2, 'at_time': since + 1, 'user': {'mid': 22},
                'item': {'type': 'reply', 'business_id': 1, 'subject_id': 170001, 'root_id': 0, 'source_id': 102}}
        data = {'items': [item, {**item, 'id': 3, 'at_time': since - 1}], 'cursor': {'is_end': True}}
        original = self.platform.request

        async def poll(operation, params):
            return data if operation == 'mentions' else await original(operation, params)

        self.platform.request = poll
        await self.flow.poll()
        await self.flow.poll()
        rows = list(self.store.db.execute('SELECT * FROM mentions'))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]['root'], '102')
        self.assertEqual(self.platform.sent, [])

    async def test_pagination_watermark_survives_restart_without_skipping_older_new_mentions(self):
        with self.store.db:
            self.store.db.execute("INSERT INTO mention_sync(account,since) VALUES('77',100)")

        def item(key, at):
            return {'id': key, 'at_time': at, 'user': {'mid': 22}, 'item': {
                'type': 'reply', 'business_id': 1, 'subject_id': 170001, 'source_id': key + 100, 'root_id': 0}}

        pages = [{'items': [item(2, 200)], 'cursor': {'id': 2, 'time': 200, 'is_end': False}},
                 {'items': [item(3, 150)], 'cursor': {'is_end': True}}]
        original = self.platform.request
        calls = []

        async def request(operation, params):
            if operation == 'mentions':
                calls.append(params)
                return pages.pop(0)
            return await original(operation, params)

        self.platform.request = request
        await self.flow.poll()
        self.assertEqual(self.store.db.execute('SELECT since FROM mention_sync').fetchone()[0], 100)
        self.flow = Mentions(self.video.interaction)
        await self.flow.poll()
        self.assertEqual(calls, [{}, {'id': 2, 'time': 200}])
        self.assertEqual(self.store.db.execute('SELECT since FROM mention_sync').fetchone()[0], 200)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM mentions').fetchone()[0], 3)
