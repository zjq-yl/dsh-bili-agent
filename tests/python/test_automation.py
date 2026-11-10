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
from bili_agent.library import browse as browse_library, cover_url
from bili_agent.media import Media
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
        await self.auto.interaction.close()
        await self.runner.pause()
        await self.dm.close()
        self.store.close()
        self.temp.cleanup()

    def friend(self):
        return self.social.save({'peer_uid': '22', 'name': '好友', 'interests': ['Python'], 'auto_send': True,
                                 'preferences': {'push_start': '00:00', 'push_end': '00:00'}})

    async def test_library_queries_all_history_and_filters_ratings_with_safe_video_links(self):
        for i in range(28):
            video = {**sample(), 'bvid': 'BV' + f'{i:010}', 'title': f'科技视频 {i}', 'author': '测试UP',
                     'cover': 'https://i0.hdslb.com/bfs/archive/example.jpg', 'page': 2}
            key = self.runner.enqueue(video)['id']
            with self.store.db:
                self.store.db.execute("UPDATE videos SET state='done',result=? WHERE id=?", (json.dumps({'summary': f'摘要 {i}'}), key))
                self.store.db.execute('INSERT INTO video_decisions VALUES(?,?,?,?)', (self.dm.account, video['bvid'], key,
                    json.dumps({'rating': '值得看' if i % 2 else '没必要看', 'reason': '内容判断'})))
        first = browse_library(self.store.db, self.dm.account)
        self.assertEqual(first['total'], 28)
        self.assertEqual(len(first['items']), 12)
        self.assertEqual(first['counts']['值得看'], 14)
        last = browse_library(self.store.db, self.dm.account, offset=24)
        self.assertEqual(len(last['items']), 4)
        self.assertIn('?p=2', last['items'][0]['url'])
        self.assertIn('title', last['items'][0])
        self.assertNotIn('subtitles', last['items'][0])
        filtered = browse_library(self.store.db, self.dm.account, rating='值得看')
        self.assertTrue(all(item['assessment']['rating'] == '值得看' for item in filtered['items']))
        self.assertEqual(browse_library(self.store.db, self.dm.account, query='科技视频 27')['total'], 1)
        self.assertEqual(browse_library(self.store.db, 'other', rating='unrated')['total'], 28)
        for bad in ['javascript:alert(1)', 'https://hdslb.com.evil.test/a', 'https://i0.hdslb.com:80/a', 'https://user:secret@i0.hdslb.com/a']:
            self.assertEqual(cover_url(bad), '')

    async def test_proactive_cap_five_at_send_boundary_does_not_limit_chat_or_requested_videos(self):
        self.friend()
        row = await self.video()
        result = json.loads(row['result'])
        drafts = []
        for i in range(6):
            video = {**result, 'bvid': 'BV' + f'{i:010}'}
            drafts.append(self.social.recommend('22', row['id'], video, '匹配兴趣。'))
        with patch('bili_agent.messaging.MIN_SEND_INTERVAL', 0):
            for draft in drafts[:5]:
                await self.dm.publish(draft)
            with self.assertRaisesRegex(RpcError, 'RECOMMENDATION_DAILY_LIMIT'):
                await self.dm.publish(drafts[5])
            for i in range(25):
                chat = self.dm.compose('22', '普通聊天回复', f'chat-test-{i}')
                await self.dm.publish(chat['id'])
            requested = self.social.recommend('22', row['id'], result, '这是你点播的内容。', request_id=100)
            await self.dm.publish(requested)
        self.assertEqual(len(self.platform.sent), 31)
        self.assertEqual(self.social.delivery_status('22')['used'], 5)
        self.assertIsNone(self.dm.delivery_window()['limit'])
        restarted = Social(self.store, self.dm)
        await restarted.connections.tick()  # A restart requires fresh mutual confirmation.
        self.assertEqual(restarted.delivery_status('22')['reason'], 'RECOMMENDATION_DAILY_LIMIT')
        with self.assertRaisesRegex(RpcError, 'INVALID_RECOMMENDATION_PREFERENCES'):
            self.social.save_preferences('22', {'daily_limit': 6})

    async def test_paired_card_keeps_quiet_hours_and_cannot_cross_beijing_midnight(self):
        self.friend()
        self.social.save_preferences('22', {'daily_limit': 1})
        row = await self.video()
        parent = self.social.recommend('22', row['id'], json.loads(row['result']), '匹配兴趣。', share=True)
        await self.dm.publish(parent)
        card = self.store.db.execute('SELECT * FROM dm_outbox WHERE depends_on=?', (parent,)).fetchone()
        self.social.guard_delivery(card)  # Paired card does not count as a second video.
        self.social.save_preferences('22', {'push_start': '22:00', 'push_end': '07:00'})
        noon = datetime.now(BEIJING).replace(hour=12, minute=0)
        with patch('bili_agent.social.datetime', wraps=datetime) as clock:
            clock.now.return_value = noon
            with self.assertRaisesRegex(RpcError, 'RECOMMENDATION_QUIET_HOURS'):
                self.social.guard_delivery(card)
            with self.store.db:
                self.store.db.execute('UPDATE dm_outbox SET attempted=? WHERE id=?',
                    (noon.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() - 1, parent))
            with self.assertRaisesRegex(RpcError, 'RECOMMENDATION_CARD_EXPIRED'):
                self.social.guard_delivery(card)

    async def test_request_uses_history_before_network_and_ignores_proactive_pause_and_clock(self):
        self.friend()
        self.social.save_preferences('22', {'daily_limit': 0, 'paused_until': 4102444800})
        self.auto.configure({'proactive_enabled': False, 'actions_enabled': False, 'share_cards': False,
                             'scheduled': False, 'start_time': '01:00', 'end_time': '01:01'})
        video = await self.video()
        self.dm.ingest('22', [message()])
        self.auto.request('22', '101', {'keyword': '学习方法', 'count': 1})
        for _ in range(5):
            await self.auto.tick()
            await self.auto.interaction.tick()
        self.assertEqual(self.platform.searches, [])
        self.assertEqual(self.auto.status()['jobs'][0]['state'], 'complete')
        self.assertIn(video['bvid'], self.platform.sent[0]['text'])
        self.assertIn('视频总结', self.platform.sent[0]['text'])
        self.assertEqual(self.social.delivery_status('22')['used'], 0)

    async def test_insufficient_history_is_reused_then_searches_for_missing_results(self):
        self.auto.configure({'proactive_enabled': False, 'actions_enabled': False, 'share_cards': False})
        old = await self.video()
        self.dm.ingest('22', [message()])
        key = self.auto.request('22', '101', {'keyword': 'Python', 'count': 2})['id']
        for _ in range(20):
            await self.auto.tick()
            await self.auto.interaction.tick()
            await asyncio.sleep(.005)
            if self.auto.status()['jobs'][0]['state'] == 'delivering':
                break
        self.assertTrue(self.platform.searches)
        selected = json.loads(self.store.db.execute('SELECT selection FROM browse_jobs WHERE id=?', (key,)).fetchone()[0])
        self.assertEqual(len(selected), 2)
        self.assertIn(old['id'], [item['video_id'] for item in selected])

    async def test_fresh_explicit_requests_are_not_rejected_by_old_daily_or_ten_minute_cap(self):
        for i in range(4):
            source = str(500+i)
            self.dm.ingest('22', [message(source)])
            self.auto.request('22', source, {'keyword': 'DeepSeek', 'count': 1})
        self.assertEqual(len(self.auto.status()['jobs']), 4)

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
                await self.auto.interaction.tick()
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

    async def test_scheduled_popular_priority_stops_at_target_without_search(self):
        self.auto.configure({'scheduled': True, 'start_time': '00:00', 'end_time': '23:59',
                             'rounds': 1, 'per_round': 1})
        self.auto.schedule(datetime.now(BEIJING).replace(hour=12))
        original = self.platform.request
        async def request(operation, params):
            if operation == 'popular':
                return {'candidates': [{'bvid': 'BV0000000009', 'title': '热门知识', 'duration': 100}]}
            return await original(operation, params)
        self.platform.request = AsyncMock(side_effect=request)
        self.auto.configure({'discovery_source': 'search'})
        with patch.object(self.auto, 'job_allowed', return_value=True), patch.object(self.runner, 'start'):
            for _ in range(3):
                job = self.store.db.execute('SELECT * FROM browse_jobs').fetchone()
                await self.auto.browse(job)
        job = self.store.db.execute('SELECT * FROM browse_jobs').fetchone()
        self.assertEqual(job['state'], 'waiting')
        self.assertEqual(self.platform.searches, [])
        self.assertEqual(self.platform.request.call_args_list[0].args, ('popular', {'page': 1, 'limit': 20}))
        self.assertEqual(self.store.db.execute('SELECT bvid FROM videos').fetchone()[0], 'BV0000000009')
        self.auto.schedule(datetime.now(BEIJING).replace(hour=12))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM browse_jobs').fetchone()[0], 1)

    async def test_popular_skips_unusable_videos_then_searches_only_remaining_target(self):
        self.auto.configure({'scheduled': True, 'start_time': '00:00', 'end_time': '23:59',
                             'rounds': 1, 'per_round': 2})
        self.auto.schedule(datetime.now(BEIJING).replace(hour=12))
        original = self.platform.request
        async def request(operation, params):
            if operation == 'popular':
                return {'candidates': [
                    {'bvid': 'BV0000000008', 'title': '超长视频', 'duration': 601},
                    {'bvid': 'BV0000000009', 'title': '无字幕', 'duration': 100},
                    {'bvid': 'BV0000000010', 'title': '可用热门', 'duration': 100}]}
            if operation == 'fetch' and params['bvid'] == 'BV0000000009':
                raise RpcError(-32010, 'NO_SUBTITLE_OR_LOGIN_REQUIRED')
            return await original(operation, params)
        self.platform.request = AsyncMock(side_effect=request)
        with patch.object(self.auto, 'job_allowed', return_value=True), patch.object(self.runner, 'start'):
            for _ in range(12):
                job = self.store.db.execute('SELECT * FROM browse_jobs').fetchone()
                if job['state'] == 'waiting':
                    break
                await self.auto.browse(job)
        self.assertEqual(job['state'], 'waiting')
        self.assertEqual(len(self.platform.searches), 1)
        self.assertEqual(self.platform.searches[0]['page'], 1)
        selected = {r[0] for r in self.store.db.execute('SELECT bvid FROM videos')}
        self.assertEqual(selected, {'BV0000000010', 'BV0000000001'})
        fetched = [c.args[1]['bvid'] for c in self.platform.request.call_args_list if c.args[0] == 'fetch']
        self.assertNotIn('BV0000000008', fetched)
        self.assertEqual(self.store.db.execute('SELECT error FROM browse_items WHERE bvid=?', ('BV0000000009',)).fetchone()[0],
                         'NO_SUBTITLE_OR_LOGIN_REQUIRED')

    async def test_home_feed_skips_saved_videos_and_searches_for_remaining_target(self):
        self.auto.configure({'discovery_source': 'home_feed_first', 'scheduled': True,
                             'start_time': '00:00', 'end_time': '23:59', 'rounds': 1, 'per_round': 2})
        self.auto.schedule(datetime.now(BEIJING).replace(hour=12))
        saved = {**sample(), 'bvid': 'BV0000000009', 'cid': '9000000009'}
        self.runner.enqueue(saved)
        original = self.platform.request

        async def request(operation, params):
            if operation == 'home_feed':
                return {'candidates': [
                    {'bvid': 'BV0000000009', 'title': '已经分析', 'duration': 100},
                    {'bvid': 'BV0000000010', 'title': '新推荐', 'duration': 100}]}
            return await original(operation, params)

        self.platform.request = AsyncMock(side_effect=request)
        with patch.object(self.auto, 'job_allowed', return_value=True), patch.object(self.runner, 'start'):
            for _ in range(10):
                job = self.store.db.execute('SELECT * FROM browse_jobs').fetchone()
                if job['state'] == 'waiting':
                    break
                await self.auto.browse(job)
        selected = {row[0] for row in self.store.db.execute('SELECT bvid FROM browse_items WHERE video_id IS NOT NULL')}
        self.assertEqual(selected, {'BV0000000010', 'BV0000000001'})
        self.assertEqual(len(self.platform.searches), 1)
        self.assertEqual(self.platform.request.call_args_list[0].args, ('home_feed', {'limit': 20}))

    async def test_automatic_browsing_transcribes_missing_subtitles_once(self):
        self.auto.configure({'scheduled': True, 'start_time': '00:00', 'end_time': '23:59',
                             'rounds': 1, 'per_round': 1})
        self.auto.schedule(datetime.now(BEIJING).replace(hour=12))
        model_dir = self.directory / 'asr-model'
        model_dir.mkdir()
        (model_dir / 'model.bin').write_bytes(b'fixture')
        media = Media(self.store, self.platform, self.runner, self.directory, str(model_dir))
        media.transcribe = AsyncMock(return_value=sample()['subtitles'])
        self.auto.media = media
        original = self.platform.request
        metadata = {**sample(), 'bvid': 'BV0000000009', 'page': 1,
                    'url': 'https://x.bilivideo.com/audio.m4s', 'track_id': '1'}

        async def request(operation, params):
            if operation == 'popular':
                return {'candidates': [{'bvid': metadata['bvid'], 'title': metadata['title'], 'duration': 100}]}
            if operation == 'fetch':
                raise RpcError(-32010, 'NO_SUBTITLE_OR_LOGIN_REQUIRED')
            if operation == 'audio':
                return metadata
            return await original(operation, params)

        self.platform.request = AsyncMock(side_effect=request)
        with patch.object(self.auto, 'job_allowed', return_value=True), patch.object(self.runner, 'start'), \
                patch('bili_agent.media.importlib.util.find_spec', return_value=True), \
                patch('bili_agent.media.download_audio', AsyncMock(return_value={
                    'path': 'fixture', 'bytes': 100, 'sha256': 'fixture'})):
            for _ in range(3):
                job = self.store.db.execute('SELECT * FROM browse_jobs').fetchone()
                await self.auto.browse(job)
        video = self.store.db.execute('SELECT * FROM videos WHERE bvid=?', (metadata['bvid'],)).fetchone()
        self.assertEqual(json.loads(video['document'])['source'], 'audio_asr')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM videos').fetchone()[0], 1)
        self.assertEqual(self.store.db.execute('SELECT video_id FROM browse_items').fetchone()[0], video['id'])
        self.assertEqual(len([c for c in self.platform.request.call_args_list if c.args[0] == 'audio']), 1)
        self.assertEqual(media.status(self.store.db.execute('SELECT id FROM media_jobs').fetchone()[0])['state'], 'done')

    async def test_automatic_browsing_checks_audio_duration_before_download(self):
        self.auto.configure({'scheduled': True, 'start_time': '00:00', 'end_time': '23:59',
                             'rounds': 1, 'per_round': 1})
        self.auto.schedule(datetime.now(BEIJING).replace(hour=12))
        media = Media(self.store, self.platform, self.runner, self.directory)
        self.auto.media = media
        original = self.platform.request

        async def request(operation, params):
            if operation == 'popular':
                return {'candidates': [{'bvid': 'BV0000000009', 'title': '短视频', 'duration': 100}]}
            if operation == 'fetch':
                raise RpcError(-32010, 'NO_SUBTITLE_OR_LOGIN_REQUIRED')
            if operation == 'audio':
                return {**sample(), 'duration': 601}
            return await original(operation, params)

        self.platform.request = AsyncMock(side_effect=request)
        with patch.object(self.auto, 'job_allowed', return_value=True):
            for _ in range(2):
                job = self.store.db.execute('SELECT * FROM browse_jobs').fetchone()
                await self.auto.browse(job)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM media_jobs').fetchone()[0], 0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM videos').fetchone()[0], 0)
        self.assertEqual(self.store.db.execute('SELECT error FROM browse_items').fetchone()[0],
                         'VIDEO_EXCEEDS_AUTOMATION_DURATION')

    async def test_only_worthwhile_videos_are_proactively_recommended_and_replay_is_idempotent(self):
        self.friend()
        row = await self.video()
        await self.auto.review(row)
        self.auto.interaction.prepare_results()
        await self.auto.review(row)
        self.auto.interaction.prepare_results()
        self.assertEqual(self.auto.assessment(row['id'])['rating'], '值得看')
        self.assertEqual(len(self.social.status()['recommendations']), 1)
        self.assertEqual(len(self.dm.status()['outbox']), 1)
        self.assertEqual(self.platform.sent, [])
        self.assertEqual(self.platform.cards, [])
        # A different BV in the middle tier gets a summary but no recommendation.
        video = sample('2'); video['bvid'] = 'BV17x411w7KD'
        key = self.runner.enqueue(video)['id']
        await self.runner.analyze(self.store.db.execute('SELECT * FROM videos WHERE id=?', (key,)).fetchone())
        self.model.rating = '可以看'
        other = self.store.db.execute('SELECT v.*,e.created FROM videos v JOIN experiences e ON e.video_id=v.id WHERE v.id=?', (key,)).fetchone()
        await self.auto.review(other)
        self.auto.interaction.prepare_results()
        self.assertEqual(len(self.social.status()['recommendations']), 1)
        self.assertEqual(self.auto.assessment(key)['rating'], '可以看')

    async def test_proactive_link_only_and_uncertain_sends_are_not_repeated(self):
        self.friend()
        row = await self.video()
        await self.auto.review(row)
        self.auto.interaction.prepare_results()
        await self.auto.interaction.send_pending()
        self.assertEqual(self.platform.cards, [])
        with patch('bili_agent.messaging.MIN_SEND_INTERVAL', 0):
            await self.social.tick()
            await self.social.tick()
            await self.social.tick()
        self.assertEqual(len(self.platform.sent), 1)
        self.assertEqual(len(self.platform.cards), 0)
        self.assertIn('https://www.bilibili.com/video/', self.platform.sent[0]['text'])
        await self.auto.interaction.perform_action()
        self.assertEqual(len(self.platform.actions), 1)
        await self.auto.interaction.perform_action()
        self.assertEqual(len(self.platform.actions), 1)

    async def test_invented_peer_cannot_create_outbox_or_actions(self):
        self.friend()
        self.model.bad_peer = True
        row = await self.video()
        with self.assertRaisesRegex(RpcError, 'INVALID_VIDEO_DECISION'):
            await self.auto.review(row)
            self.auto.interaction.prepare_results()
        self.assertEqual(self.dm.status()['outbox'], [])
        self.assertEqual(self.auto.status()['actions'], [])

    async def test_review_corrects_excess_evidence_once_with_specific_feedback(self):
        row = await self.video()
        result = json.loads(row['result'])
        valid = {'rating': '可以看', 'like': False, 'dislike': False, 'favorite': False,
                 'reason': '内容有依据', 'evidence': [result['evidence'][0]['id']], 'recommendations': []}
        bad = {**valid, 'evidence': valid['evidence'] * 11}
        with patch.object(self.model, 'generate', AsyncMock(side_effect=[json.dumps(bad), json.dumps(valid)])) as model:
            await self.auto.review(row)
        self.assertEqual(model.await_count, 2)
        feedback = json.loads(model.call_args_list[1].args[2])['validation_feedback']
        self.assertEqual(feedback['field'], 'evidence')
        self.assertEqual(feedback['attempts'], 1)
        self.assertIn('1至10', model.call_args_list[0].args[1])
        self.assertEqual(self.auto.assessment(row['id'])['rating'], '可以看')

    async def test_failed_review_records_detail_and_retries_without_resummarizing(self):
        row = await self.video()
        result = json.loads(row['result'])
        valid = {'rating': '可以看', 'like': False, 'dislike': False, 'favorite': False,
                 'reason': '内容有依据', 'evidence': [result['evidence'][0]['id']], 'recommendations': []}
        with patch.object(self.model, 'generate', AsyncMock(return_value='not JSON')) as model:
            await self.auto.tick()
        self.assertEqual(model.await_count, 2)
        failed = self.auto.assessment(row['id'])
        self.assertEqual(failed['error_detail']['field'], 'json')
        self.assertEqual(failed['error_detail']['attempts'], 2)
        self.assertFalse(self.auto.status()['actions'])
        self.assertEqual(self.dm.status()['outbox'], [])
        # Retry remains eligible even if the old video predates the automation window.
        self.auto.created = time.time() + 10000
        self.auto.configure({'actions_enabled': False, 'proactive_enabled': False, 'request_enabled': False, 'comments_enabled': False})
        self.auto.retry_review(row['id'])
        self.auto.retry_review(row['id'])
        self.assertTrue(self.auto.assessment(row['id'])['retry_requested'])
        with patch.object(self.model, 'generate', AsyncMock(return_value=json.dumps(valid))) as model:
            await self.auto.tick()
        model.assert_awaited_once()
        self.assertEqual(model.call_args.args[0], 'video_decision')
        self.assertEqual(self.auto.assessment(row['id'])['rating'], '可以看')
        self.assertEqual(self.store.db.execute('SELECT result FROM videos WHERE id=?', (row['id'],)).fetchone()[0], row['result'])
        with self.assertRaisesRegex(RpcError, 'VIDEO_REVIEW_NOT_FAILED'):
            self.auto.retry_review(row['id'])

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
                await self.auto.interaction.tick()
                await asyncio.sleep(.005)
                if self.auto.status()['jobs'][0]['state'] == 'complete':
                    break
            await self.auto.interaction.send_pending()
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
        with patch.object(self.auto.interaction, 'send_pending', AsyncMock(side_effect=RpcError(-32040, 'DM_SEND_LIMIT'))):
            await self.auto.tick()
            await self.auto.interaction.tick()
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
        await self.auto.interaction.tick()
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
        self.auto.interaction.prepare_results()
        self.assertEqual(self.auto.status()['jobs'][0]['state'], 'delivering')
        await self.auto.interaction.send_pending()
        self.assertEqual(self.auto.status()['jobs'][0]['state'], 'complete')
        self.auto = Automation(self.store, self.runner, self.dm, self.social, self.platform, self.model, self.directory)
        await self.auto.tick()
        await self.auto.interaction.tick()
        self.assertEqual(len(self.platform.sent), 1)

    async def test_no_matching_video_still_returns_a_final_reply(self):
        self.auto.configure({'rounds': 1, 'per_round': 1, 'actions_enabled': False, 'proactive_enabled': False})
        self.model.rating = '没必要看'
        self.dm.ingest('22', [message()])
        self.auto.request('22', '101', {'keyword': 'Python', 'count': 1})
        for _ in range(20):
            await self.auto.tick()
            await self.auto.interaction.tick()
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
        self.auto.interaction.prepare_results()
        with self.store.db:
            self.store.db.execute('INSERT INTO browse_items VALUES(?,?,?,NULL)', (key, row['bvid'], row['id']))
        model = AsyncMock(return_value='{"recommendations":[]}')
        with patch.object(self.model, 'generate', model):
            for _ in range(3):
                job = self.store.db.execute('SELECT * FROM browse_jobs WHERE id=?', (key,)).fetchone()
                self.assertFalse(await self.auto.finish_request(job, early=True))
        self.assertEqual(model.await_count, 1)

    async def test_explicit_requests_do_not_resend_previously_recommended_video(self):
        self.friend()
        row = await self.video()
        await self.auto.review(row)
        self.auto.interaction.prepare_results()
        original = self.social.status()['recommendations'][0]['draft_id']
        with patch('bili_agent.messaging.MIN_SEND_INTERVAL', 0):
            await self.dm.publish(original)
            result = json.loads(row['result'])
            first = self.social.recommend('22', row['id'], result, '符合这次的需求', request_id=1)
            repeated = self.social.recommend('22', row['id'], result, '符合这次的需求', request_id=1)
            second = self.social.recommend('22', row['id'], result, '符合这次的需求', request_id=2)
            self.assertEqual(first, repeated)
            self.assertEqual(len({original, first, second}), 1)
            await self.dm.publish(first)
            await self.dm.publish(second)
        self.assertEqual(len(self.platform.sent), 1)

    async def test_history_search_excludes_videos_already_delivered_to_requester(self):
        self.friend()
        row = await self.video()
        await self.auto.review(row)
        draft = self.social.recommend('22', row['id'], json.loads(row['result']), '推荐理由', request_id=99)
        await self.dm.publish(draft)
        self.dm.ingest('22', [message()])
        key = self.auto.request('22', '101', {'keyword': 'Python', 'count': 1})['id']
        job = self.store.db.execute('SELECT * FROM browse_jobs WHERE id=?', (key,)).fetchone()
        self.assertFalse(await self.auto.prepare_history(job))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM browse_items WHERE job_id=?', (key,)).fetchone()[0], 0)

    async def test_uncertain_result_never_marks_request_complete_or_resends(self):
        self.dm.ingest('22', [message()])
        key = self.auto.request('22', '101', {'keyword': 'Python', 'count': 1})['id']
        await self.auto.finish_request(self.store.db.execute('SELECT * FROM browse_jobs WHERE id=?', (key,)).fetchone())
        self.auto.interaction.prepare_results()
        with self.store.db:
            self.store.db.execute("UPDATE dm_outbox SET state='uncertain',error='SEND_RECEIPT_MISSING'")
        self.auto.interaction.update_deliveries()
        await self.auto.interaction.send_pending()
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
        self.auto.interaction.start()
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
        self.auto.interaction.prepare_results()
        request = self.platform.request
        async def no_token(operation, params):
            if operation == 'video_action':
                raise RpcError(-32010, 'DISLIKE_REQUIRES_APP_TOKEN')
            return await request(operation, params)
        self.platform.request = no_token
        await self.auto.interaction.perform_action()
        self.assertEqual(self.auto.status()['actions'][0]['state'], 'local_only')
        with self.store.db:
            self.store.db.execute("UPDATE video_actions SET state='sending'")
        self.auto = Automation(self.store, self.runner, self.dm, self.social, self.platform, self.model, self.directory)
        await self.auto.interaction.perform_action()
        self.assertEqual(self.auto.status()['actions'][0]['state'], 'uncertain')
        self.assertEqual(self.platform.actions, [])
