import asyncio
import json
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, AsyncMock

from bili_agent.automation import Automation, BEIJING
from bili_agent.messaging import Messaging
from bili_agent.protocol import RpcError
from bili_agent.runner import Runner
from bili_agent.social import Social
from bili_agent.store import Store
from .test_agent import FakeModel as VideoModel, sample
from .test_messaging import FakePlatform, message


class Platform(FakePlatform):
    def __init__(self):
        super().__init__()
        self.actions, self.cards, self.searches = [], [], []

    async def request(self, operation, params):
        if operation == 'search':
            self.searches.append(params)
            return {'candidates': [{'bvid': 'BV' + f'{i:010}', 'title': 'Python 入门'}
                                   for i in range((params['page'] - 1) * 5 + 1, params['page'] * 5 + 1)]}
        if operation == 'fetch':
            video = sample()
            video.update(bvid=params['bvid'], cid=params['bvid'][2:], title='Python 入门')
            return video
        if operation == 'share_video':
            self.cards.append(params)
            return {'msg_key': str(5000 + len(self.cards))}
        if operation == 'video_action':
            self.actions.append(params)
            return {'accepted': True}
        return await super().request(operation, params)


class Model(VideoModel):
    def __init__(self):
        super().__init__()
        self.rating = '值得看'
        self.bad_peer = False

    async def generate(self, purpose, system, text):
        value = json.loads(text)
        if purpose == 'video_decision':
            return json.dumps({'rating': self.rating, 'like': self.rating == '值得看', 'dislike': self.rating == '没必要看',
                'favorite': False, 'reason': '讲解有例子，适合入门。', 'evidence': [value['video']['evidence'][0]['id']],
                'recommendations': [{'peer_uid': '999' if self.bad_peer else f['peer'], 'reason': '符合你学习编程的兴趣。', 'share': True}
                                    for f in value['friends'] if self.rating == '值得看']})
        if purpose == 'requested_recommendations':
            return json.dumps({'recommendations': [{'video_id': row['video_id'], 'reason': '有具体的编程学习方法。', 'share': True}
                                                  for row in value['videos'][:value['wanted']]]})
        if purpose == 'dm_reply':
            return json.dumps({'reply': '我帮你找找。', 'recommend_request': {'keyword': 'Python', 'count': 2}})
        return await super().generate(purpose, system, text)


class AutomationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = Store(self.directory)
        self.platform, self.model = Platform(), Model()
        self.runner = Runner(self.store, self.model)
        self.dm = Messaging(self.store, self.platform, self.model, self.runner.memory, True, ['22'])
        await self.dm.bind()
        self.social = Social(self.store, self.dm)
        self.social.model_driven = True
        self.dm.friends = self.social
        self.auto = Automation(self.store, self.runner, self.dm, self.social, self.platform, self.model, self.directory)
        self.dm.recommendations = self.auto
        self.auto.configure({'scheduled': False})

    async def asyncTearDown(self):
        await self.auto.close()
        await self.runner.pause()
        await self.dm.close()
        self.store.close()
        self.temp.cleanup()

    def friend(self):
        return self.social.save({'peer_uid': '22', 'name': '好友', 'interests': ['Python'], 'auto_send': True,
                                 'preferences': {'push_start': '00:00', 'push_end': '00:00'}})

    async def test_request_overrides_are_temporary_and_reuse_saved_analysis(self):
        self.friend()
        self.social.save_preferences('22', {'mode': 'learning', 'goal': '学习编程', 'paused_until': 4102444800})
        video = await self.video()
        self.dm.ingest('22', [message()])
        key = self.auto.request('22', '101', {'keyword': '有趣的知识', 'count': 1, 'mode': 'leisure', 'goal': '放松一下'})['id']
        self.assertEqual(self.social.preferences('22')['mode'], 'learning')
        with self.store.db:
            self.store.db.execute('UPDATE browse_jobs SET candidates=? WHERE id=?', (json.dumps([{'bvid': video['bvid'], 'title': '知识'}]), key))
        job = self.store.db.execute('SELECT * FROM browse_jobs WHERE id=?', (key,)).fetchone()
        self.assertEqual(json.loads(job['requirements'])['mode'], 'leisure')
        self.assertTrue(self.auto.job_allowed(job))
        original = self.platform.request
        with patch.object(self.platform, 'request', AsyncMock(wraps=original)) as platform:
            await self.auto.browse(job)
        self.assertFalse(any(call.args[0] == 'fetch' for call in platform.await_args_list))
        self.assertEqual(self.store.db.execute('SELECT priority FROM videos WHERE id=?', (video['id'],)).fetchone()[0], 0)

    async def test_requested_video_recovers_from_transient_model_failure_without_losing_request(self):
        self.auto.configure({'actions_enabled': False, 'proactive_enabled': False, 'share_cards': False})
        self.dm.ingest('22', [message()])
        job = self.auto.request('22', '101', {'keyword': '学习', 'count': 1})['id']
        key = self.runner.enqueue(sample(), priority=0)['id']
        with self.store.db:
            self.store.db.execute('INSERT INTO browse_items VALUES(?,?,?,NULL)', (job, sample()['bvid'], key))
            self.store.db.execute("UPDATE browse_jobs SET state='waiting',round_index=rounds WHERE id=?", (job,))
        original = self.model.generate
        self.model.generate = AsyncMock(side_effect=RpcError(-32006, 'HOST_CALL_FAILED'))
        self.runner.start()
        async with asyncio.timeout(2):
            while self.runner.state != 'BLOCKED':
                await asyncio.sleep(.005)
        self.assertGreater(self.store.db.execute('SELECT retry_at FROM videos WHERE id=?', (key,)).fetchone()[0], time.time())
        self.model.generate = original
        with self.store.db:
            self.store.db.execute('UPDATE videos SET retry_at=? WHERE id=?', (time.time() - 1, key))
        with patch('bili_agent.messaging.MIN_SEND_INTERVAL', 0):
            for _ in range(30):
                await self.auto.tick()
                await asyncio.sleep(.005)
                if self.auto.status()['jobs'][0]['state'] == 'complete':
                    break
        self.assertEqual(self.auto.status()['jobs'][0]['state'], 'complete')
        self.assertTrue(any('https://www.bilibili.com/video/' in item['text'] for item in self.platform.sent))

    async def video(self, cid='1'):
        key = self.runner.enqueue(sample(cid))['id']
        await self.runner.analyze(self.store.db.execute('SELECT * FROM videos WHERE id=?', (key,)).fetchone())
        return self.store.db.execute('SELECT v.*,e.created FROM videos v JOIN experiences e ON e.video_id=v.id WHERE v.id=?', (key,)).fetchone()

    def test_beijing_schedule_is_three_rounds_and_restart_does_not_duplicate(self):
        self.auto.configure({'scheduled': True})
        self.auto.schedule(datetime(2026, 11, 6, 17, 59, tzinfo=BEIJING))
        self.assertEqual(len(self.auto.status()['jobs']), 0)
        for hour, expected in [(18, 1), (19, 2), (20, 3), (21, 3)]:
            self.auto.schedule(datetime(2026, 11, 6, hour, tzinfo=BEIJING))
            self.assertEqual(len(self.auto.status()['jobs']), expected)
        self.assertEqual(self.auto.status()['slots'], ['18:00', '19:00', '20:00'])
        self.auto = Automation(self.store, self.runner, self.dm, self.social, self.platform, self.model, self.directory)
        self.auto.schedule(datetime(2026, 11, 6, 20, 30, tzinfo=BEIJING))
        self.assertEqual(len(self.auto.status()['jobs']), 3)
        self.assertTrue(all(job['wanted'] == 5 for job in self.auto.status()['jobs']))
        with self.assertRaises(RpcError):
            self.auto.configure({'start_time': '21:00', 'end_time': '18:00'})

    async def test_only_worthwhile_videos_are_proactively_recommended_and_replay_is_idempotent(self):
        self.friend()
        row = await self.video()
        await self.auto.review(row)
        await self.auto.review(row)
        self.assertEqual(self.auto.assessment(row['id'])['rating'], '值得看')
        self.assertEqual(len(self.social.status()['recommendations']), 1)
        self.assertEqual(len(self.dm.status()['outbox']), 2)
        self.assertEqual(self.platform.sent, [])
        self.assertEqual(self.platform.cards, [])
        # A different BV in the middle tier gets a summary but no recommendation.
        video = sample('2'); video['bvid'] = 'BV17x411w7KD'
        key = self.runner.enqueue(video)['id']
        await self.runner.analyze(self.store.db.execute('SELECT * FROM videos WHERE id=?', (key,)).fetchone())
        self.model.rating = '可以看'
        other = self.store.db.execute('SELECT v.*,e.created FROM videos v JOIN experiences e ON e.video_id=v.id WHERE v.id=?', (key,)).fetchone()
        await self.auto.review(other)
        self.assertEqual(len(self.social.status()['recommendations']), 1)
        self.assertEqual(self.auto.assessment(key)['rating'], '可以看')

    async def test_reason_precedes_card_and_uncertain_sends_are_not_repeated(self):
        self.friend()
        row = await self.video()
        await self.auto.review(row)
        await self.auto.send_pending()
        self.assertEqual(self.platform.cards, [])
        with patch('bili_agent.messaging.MIN_SEND_INTERVAL', 0):
            await self.social.tick()
            await self.auto.send_pending()
            await self.auto.send_pending()
        self.assertEqual(len(self.platform.sent), 1)
        self.assertEqual(len(self.platform.cards), 1)
        self.assertIn('https://www.bilibili.com/video/', self.platform.sent[0]['text'])
        await self.auto.perform_action()
        self.assertEqual(len(self.platform.actions), 1)
        await self.auto.perform_action()
        self.assertEqual(len(self.platform.actions), 1)

    async def test_invented_peer_cannot_create_outbox_or_actions(self):
        self.friend()
        self.model.bad_peer = True
        row = await self.video()
        with self.assertRaisesRegex(RpcError, 'INVALID_VIDEO_DECISION'):
            await self.auto.review(row)
        self.assertEqual(self.dm.status()['outbox'], [])
        self.assertEqual(self.auto.status()['actions'], [])

    async def test_dm_request_delivers_enough_results_before_exhausting_search_rounds(self):
        self.auto.configure({'proactive_enabled': False, 'actions_enabled': False})
        self.dm.ingest('22', [message(text='推荐几个 Python 视频')])
        await self.dm.draft_reply('101')
        jobs = self.auto.status()['jobs']
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['wanted'], 2)
        self.auto.request('22', '101', {'keyword': 'Python', 'count': 2})
        self.assertEqual(len(self.auto.status()['jobs']), 1)
        with patch('bili_agent.messaging.MIN_SEND_INTERVAL', 0):
            for _ in range(100):
                await self.auto.tick()
                await asyncio.sleep(.005)
                if self.auto.status()['jobs'][0]['state'] == 'complete':
                    break
            await self.auto.send_pending()
        job = self.auto.status()['jobs'][0]
        self.assertEqual(job['state'], 'complete')
        self.assertLess(job['round_index'], 3)
        self.assertLess(len(job['videos']), 15)
        self.assertEqual([call['page'] for call in self.platform.searches], [1])
        self.assertTrue(self.platform.sent)
        self.assertIn('https://www.bilibili.com/video/', self.platform.sent[0]['text'])
        self.assertEqual(len(job['deliveries']), 2)

    async def test_send_cooldown_does_not_block_review_or_result_creation(self):
        row = await self.video()
        with patch.object(self.auto, 'send_pending', AsyncMock(side_effect=RpcError(-32040, 'DM_SEND_LIMIT'))):
            await self.auto.tick()
        self.assertEqual(self.auto.assessment(row['id'])['rating'], '值得看')
        self.assertEqual(self.auto.status()['delivery_error'], 'DM_SEND_LIMIT')

    async def test_legacy_model_quota_is_removed_and_does_not_block_search(self):
        with self.store.db:
            self.store.db.execute('UPDATE automation_settings SET value=?', (json.dumps({**self.auto.config, 'model_daily_limit': 1}),))
        self.auto = Automation(self.store, self.runner, self.dm, self.social, self.platform, self.model, self.directory)
        self.store.reserve_call('test')
        self.dm.ingest('22', [message()])
        key = self.auto.request('22', '101', {'keyword': 'Python', 'count': 2})['id']
        await self.auto.tick()
        job = self.auto.status()['jobs'][0]
        self.assertEqual(job['id'], key)
        self.assertEqual(job['state'], 'browsing')
        self.assertIsNone(job['error'])
        self.assertNotIn('model_daily_limit', self.auto.config)
        self.assertIsNone(self.auto.model_budget()['limit'])
        self.assertEqual(len(self.platform.searches), 1)

    async def test_request_is_complete_only_after_receipt_and_restart_does_not_resend(self):
        self.dm.ingest('22', [message()])
        key = self.auto.request('22', '101', {'keyword': 'Python', 'count': 1})['id']
        job = self.store.db.execute('SELECT * FROM browse_jobs WHERE id=?', (key,)).fetchone()
        await self.auto.finish_request(job)
        self.assertEqual(self.auto.status()['jobs'][0]['state'], 'delivering')
        await self.auto.send_pending()
        self.assertEqual(self.auto.status()['jobs'][0]['state'], 'complete')
        self.auto = Automation(self.store, self.runner, self.dm, self.social, self.platform, self.model, self.directory)
        await self.auto.tick()
        self.assertEqual(len(self.platform.sent), 1)

    async def test_no_matching_video_still_returns_a_final_reply(self):
        self.auto.configure({'rounds': 1, 'per_round': 1, 'actions_enabled': False, 'proactive_enabled': False})
        self.model.rating = '没必要看'
        self.dm.ingest('22', [message()])
        self.auto.request('22', '101', {'keyword': 'Python', 'count': 1})
        for _ in range(20):
            await self.auto.tick()
            await asyncio.sleep(.005)
            if self.auto.status()['jobs'][0]['state'] == 'complete':
                break
        self.assertEqual(self.auto.status()['jobs'][0]['state'], 'complete')
        self.assertIn('没有找到', self.platform.sent[0]['text'])

    async def test_accepted_request_cannot_silently_drop_acknowledgement(self):
        self.dm.ingest('22', [message()])
        with patch.object(self.model, 'generate', AsyncMock(return_value=json.dumps({
                'reply': None, 'recommend_request': {'keyword': 'Python', 'count': 1}}))):
            draft = await self.dm.draft_reply('101')
        self.assertEqual(draft['state'], 'draft')
        self.assertIn('收到', draft['text'])

    async def test_early_selection_is_not_repeated_without_new_analyzed_videos(self):
        self.dm.ingest('22', [message()])
        key = self.auto.request('22', '101', {'keyword': 'Python', 'count': 1})['id']
        row = await self.video()
        await self.auto.review(row)
        with self.store.db:
            self.store.db.execute('INSERT INTO browse_items VALUES(?,?,?,NULL)', (key, row['bvid'], row['id']))
        model = AsyncMock(return_value='{"recommendations":[]}')
        with patch.object(self.model, 'generate', model):
            for _ in range(3):
                job = self.store.db.execute('SELECT * FROM browse_jobs WHERE id=?', (key,)).fetchone()
                self.assertFalse(await self.auto.finish_request(job, early=True))
        self.assertEqual(model.await_count, 1)

    async def test_explicit_requests_get_separate_replies_for_a_previously_recommended_video(self):
        self.friend()
        row = await self.video()
        await self.auto.review(row)
        original = self.social.status()['recommendations'][0]['draft_id']
        with patch('bili_agent.messaging.MIN_SEND_INTERVAL', 0):
            await self.dm.publish(original)
            result = json.loads(row['result'])
            first = self.social.recommend('22', row['id'], result, '符合这次的需求', request_id=1)
            repeated = self.social.recommend('22', row['id'], result, '符合这次的需求', request_id=1)
            second = self.social.recommend('22', row['id'], result, '符合这次的需求', request_id=2)
            self.assertEqual(first, repeated)
            self.assertEqual(len({original, first, second}), 3)
            await self.dm.publish(first)
            await self.dm.publish(second)
        self.assertEqual(len(self.platform.sent), 3)

    async def test_uncertain_result_never_marks_request_complete_or_resends(self):
        self.dm.ingest('22', [message()])
        key = self.auto.request('22', '101', {'keyword': 'Python', 'count': 1})['id']
        await self.auto.finish_request(self.store.db.execute('SELECT * FROM browse_jobs WHERE id=?', (key,)).fetchone())
        with self.store.db:
            self.store.db.execute("UPDATE dm_outbox SET state='uncertain',error='SEND_RECEIPT_MISSING'")
        self.auto.update_deliveries()
        await self.auto.send_pending()
        job = self.auto.status()['jobs'][0]
        self.assertEqual(job['state'], 'delivering')
        self.assertEqual(job['error'], 'DM_SEND_UNCERTAIN')
        self.assertEqual(self.platform.sent, [])

    async def test_forgetting_request_prevents_late_recommendation_delivery(self):
        self.dm.ingest('22', [message()])
        key = self.auto.request('22', '101', {'keyword': 'Python', 'count': 2})['id']
        job = self.store.db.execute('SELECT * FROM browse_jobs WHERE id=?', (key,)).fetchone()
        self.dm.forget('22')
        self.assertFalse(self.auto.job_allowed(job))
        await self.auto.browse(job)
        self.assertEqual(self.platform.searches, [])

    async def test_default_boot_listens_but_manual_stop_is_respected(self):
        (self.directory / 'credentials.json').write_text('{}')
        self.auto.start()
        async with asyncio.timeout(2):
            while self.dm.state != 'RUNNING':
                await asyncio.sleep(.005)
        self.assertTrue(self.dm.auto_send)
        self.auto.dm_paused = True
        await self.dm.close()
        self.auto.wake.set()
        await asyncio.sleep(.02)
        self.assertEqual(self.dm.state, 'STOPPED')

    async def test_action_failure_stays_uncertain_after_restart_and_local_dislike_is_honest(self):
        row = await self.video()
        self.model.rating = '没必要看'
        await self.auto.review(row)
        request = self.platform.request
        async def no_token(operation, params):
            if operation == 'video_action':
                raise RpcError(-32010, 'DISLIKE_REQUIRES_APP_TOKEN')
            return await request(operation, params)
        self.platform.request = no_token
        await self.auto.perform_action()
        self.assertEqual(self.auto.status()['actions'][0]['state'], 'local_only')
        with self.store.db:
            self.store.db.execute("UPDATE video_actions SET state='sending'")
        self.auto = Automation(self.store, self.runner, self.dm, self.social, self.platform, self.model, self.directory)
        await self.auto.perform_action()
        self.assertEqual(self.auto.status()['actions'][0]['state'], 'uncertain')
        self.assertEqual(self.platform.actions, [])
