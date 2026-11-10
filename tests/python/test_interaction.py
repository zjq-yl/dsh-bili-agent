import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from bili_agent.automation import VideoAgent
from bili_agent.messaging import Messaging
from bili_agent.runner import Runner
from bili_agent.social import Social
from bili_agent.store import Store
from .test_agent import sample
from .test_automation import Platform, Model
from .test_messaging import message


class InteractionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.platform, self.model = Platform(), Model()
        self.runner = Runner(self.store, self.model)
        self.dm = Messaging(self.store, self.platform, self.model, self.runner.memory, True, ['22'])
        await self.dm.bind()
        self.social = Social(self.store, self.dm)
        self.social.model_driven = True
        self.dm.friends = self.social
        self.video = VideoAgent(self.store, self.runner, self.dm, self.social, self.platform, self.model, Path(self.temp.name))
        self.interaction = self.video.interaction
        self.video.configure({'scheduled': False})
        self.social.save({'peer_uid': '22', 'name': '好友', 'interests': ['Python'], 'auto_send': True})

    async def asyncTearDown(self):
        await self.interaction.close()
        await self.video.close()
        await self.dm.close()
        await self.runner.pause()
        self.store.close()
        self.temp.cleanup()

    async def analyzed(self):
        key = self.runner.enqueue(sample())['id']
        await self.runner.analyze(self.store.db.execute('SELECT * FROM videos WHERE id=?', (key,)).fetchone())
        row = self.store.db.execute('SELECT v.*,e.created FROM videos v JOIN experiences e ON e.video_id=v.id WHERE v.id=?', (key,)).fetchone()
        await self.video.review(row)
        return row

    async def test_video_agent_owns_proactive_push_and_chat_agent_does_not_send_it(self):
        row = await self.analyzed()
        self.assertEqual(self.video.assessment(row['id'])['rating'], '值得看')
        self.assertEqual(len(self.dm.status()['outbox']), 1)
        self.interaction.prepare_results()
        self.interaction.prepare_results()
        self.assertEqual(len(self.dm.status()['outbox']), 1)
        await self.interaction.send_pending()
        self.assertEqual(self.platform.sent, [])

    async def test_friend_request_handoff_does_not_also_create_a_proactive_push(self):
        self.dm.ingest('22', [message()])
        key = self.video.request('22', '101', {'keyword': 'Python', 'count': 1})['id']
        video_id = self.runner.enqueue(sample())['id']
        with self.store.db:
            self.store.db.execute('INSERT INTO browse_items VALUES(?,?,?,NULL)', (key, sample()['bvid'], video_id))
        await self.runner.analyze(self.store.db.execute('SELECT * FROM videos WHERE id=?', (video_id,)).fetchone())
        row = self.store.db.execute('SELECT v.*,e.created FROM videos v JOIN experiences e ON e.video_id=v.id WHERE v.id=?', (video_id,)).fetchone()
        await self.video.review(row)
        self.assertEqual(self.dm.status()['outbox'], [])
        job = self.store.db.execute('SELECT * FROM browse_jobs WHERE id=?', (key,)).fetchone()
        await self.video.finish_request(job)
        self.assertEqual(self.dm.status()['outbox'], [])
        self.interaction.prepare_results()
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM request_deliveries').fetchone()[0], 1)
        self.assertEqual(self.social.status()['recommendations'], [])

    async def test_public_comment_is_grounded_separate_from_dm_and_sent_only_once(self):
        row = await self.analyzed()
        self.interaction.prepare_results()
        evidence = json.loads(row['result'])['evidence'][0]['id']
        result = json.dumps({'comment': '这里的变量例子很清楚，也可以试试修改数值。', 'reason': '能围绕例子交流。', 'evidence': [evidence]})
        request = self.platform.request
        sent = []
        async def send(operation, params):
            if operation == 'send_comment':
                sent.append(params)
                return {'rpid': '123456'}
            return await request(operation, params)
        with patch.object(self.model, 'generate', AsyncMock(return_value=result)) as model, patch.object(self.platform, 'request', send):
            await self.interaction.comments.tick()
            await self.interaction.comments.tick()
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.interaction.comments.status()[0]['state'], 'sent')
        self.assertEqual(self.platform.sent, [])
        payload = json.loads(model.call_args.args[2])
        self.assertNotIn('recommendations', payload['rating'])
        self.assertNotIn('friends', payload)

    async def test_public_comment_uncertain_receipt_never_replays(self):
        row = await self.analyzed()
        self.interaction.prepare_results()
        evidence = json.loads(row['result'])['evidence'][0]['id']
        output = json.dumps({'comment': '这个例子适合动手验证。', 'reason': '补充学习建议。', 'evidence': [evidence]})
        request = self.platform.request
        calls = []
        async def send(operation, params):
            if operation == 'send_comment':
                calls.append(params)
                return {}
            return await request(operation, params)
        with patch.object(self.model, 'generate', AsyncMock(return_value=output)), patch.object(self.platform, 'request', send):
            await self.interaction.comments.tick()
            await self.interaction.comments.tick()
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.interaction.comments.status()[0]['state'], 'uncertain')

    async def test_model_can_skip_and_disabled_comments_do_not_call_model(self):
        await self.analyzed()
        self.interaction.prepare_results()
        self.video.configure({'comments_enabled': False})
        with patch.object(self.model, 'generate', AsyncMock()) as model:
            await self.interaction.comments.tick()
            model.assert_not_called()
        self.video.config['comments_enabled'] = True
        with patch.object(self.model, 'generate', AsyncMock(return_value='{"comment":null,"reason":"没有额外观点","evidence":[]}')):
            await self.interaction.comments.tick()
        self.assertEqual(self.interaction.comments.status()[0]['state'], 'skipped')

    async def test_comment_daily_cap_counts_uncertain_attempts_without_blocking_chat(self):
        await self.analyzed()
        self.interaction.prepare_results()
        import time
        with self.store.db:
            for i in range(3):
                self.store.db.execute("INSERT INTO public_comments(account,bvid,video_id,state,attempted) VALUES(?,?,1,'uncertain',?)",
                    (self.dm.account, f'BV{i:010}', time.time() - 70))
        with patch.object(self.model, 'generate', AsyncMock()) as model:
            await self.interaction.comments.tick()
            model.assert_not_called()
        self.assertIsNone(self.dm.delivery_window()['limit'])

    async def test_comment_rejects_invented_evidence_and_does_not_comment_old_library(self):
        row = await self.analyzed()
        self.interaction.comments.reset_window()
        self.interaction.prepare_results()
        self.assertEqual(self.interaction.comments.status(), [])
        with self.store.db:
            self.store.db.execute('UPDATE comment_policy SET since=0')
        self.interaction.prepare_results()
        with patch.object(self.model, 'generate', AsyncMock(return_value='{"comment":"例子很清晰","reason":"参与讨论","evidence":[999999]}')):
            await self.interaction.comments.tick()
        self.assertEqual(self.interaction.comments.status()[0]['state'], 'failed')
