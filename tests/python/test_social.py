import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

from bili_agent.memory import Memory
from bili_agent.messaging import Messaging
from bili_agent.protocol import RpcError
from bili_agent.runner import Runner
from bili_agent.social import Social, LOCAL_TIME
from bili_agent.store import Store
from .test_agent import FakeModel as VideoModel, sample
from .test_messaging import FakePlatform, FakeModel, message


class SocialTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.platform, self.model = FakePlatform(), FakeModel()
        self.dm = Messaging(self.store, self.platform, self.model, Memory(self.store), True, ['22', '33'])
        await self.dm.bind()
        self.social = Social(self.store, self.dm)
        self.dm.friends = self.social
        self.runner = Runner(self.store, VideoModel())
        self.runner.on_complete = self.social.wake.set

    async def asyncTearDown(self):
        await self.runner.pause()
        await self.social.close()
        await self.dm.close()
        self.store.close()
        self.temp.cleanup()

    def friend(self, peer='22', **changes):
        return self.social.save({'peer_uid': peer, 'name': '好友', 'category': '学习伙伴',
                                 'interests': ['知识'], 'style': '简洁直接',
                                 'preferences': {'push_start': '00:00', 'push_end': '00:00'}, **changes})

    async def test_recommendations_share_recipient_history_and_survive_restart(self):
        video_id = await self.video()
        result = json.loads(self.store.db.execute('SELECT result FROM videos WHERE id=?', (video_id,)).fetchone()[0])
        first = self.social.recommend('22', video_id, result, '推荐理由', request_id=1)
        second = self.social.recommend('22', video_id, result, '另一理由', request_id=2)
        self.assertEqual(first, second)
        self.assertFalse(self.social.can_recommend('22', result))
        self.assertTrue(self.social.can_recommend('33', result))
        with self.store.db:
            self.store.db.execute("UPDATE dm_outbox SET state='uncertain' WHERE id=?", (first,))
        restarted = Social(self.store, self.dm)
        self.assertEqual(restarted.recommend('22', video_id, result, '主动推荐', share=True), first)
        self.assertFalse(restarted.can_recommend('22', result))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM dm_outbox').fetchone()[0], 1)
        account = self.dm.account
        self.dm.account = '999'
        self.assertTrue(restarted.can_recommend('22', result))
        self.dm.account = account

    async def test_legacy_duplicates_are_backfilled_and_blocked_before_send(self):
        video_id = await self.video()
        result = json.loads(self.store.db.execute('SELECT result FROM videos WHERE id=?', (video_id,)).fetchone()[0])
        first = self.dm.compose('22', '已发的推荐', f'req-1-{video_id}-22')['id']
        duplicate = self.dm.compose('22', '另一请求的重复推荐', f'req-2-{video_id}-22')['id']
        card = self.dm.compose_card('22', result['bvid'], duplicate, f'req-card-2-{video_id}-22')['id']
        with self.store.db:
            self.store.db.execute("UPDATE dm_outbox SET state='sent' WHERE id=?", (first,))
            self.store.db.execute('DROP INDEX dm_recommendation_history')
            self.store.db.execute('ALTER TABLE dm_outbox DROP COLUMN recommendation_bvid')
        self.social = Social(self.store, self.dm)
        self.dm.friends = self.social
        self.assertFalse(self.social.can_recommend('22', result))
        cancelled = await self.dm.publish(duplicate)
        self.assertEqual((cancelled['state'], cancelled['error']), ('cancelled', 'DUPLICATE_RECOMMENDATION'))
        self.assertEqual(self.store.db.execute('SELECT state FROM dm_outbox WHERE id=?', (card,)).fetchone()[0], 'cancelled')
        self.assertEqual(self.platform.sent, [])


    async def video(self, cid='1'):
        key = self.runner.enqueue(sample(cid))['id']
        await self.runner.analyze(self.store.db.execute('SELECT * FROM videos WHERE id=?', (key,)).fetchone())
        return key

    async def test_video_completion_triggers_durable_matching_draft_and_no_automatic_send(self):
        self.friend()
        self.friend('33', interests=['烹饪'])
        self.social.start()
        await self.video()
        async with asyncio.timeout(2):
            while not self.social.status()['recommendations']:
                await asyncio.sleep(.005)
        await self.social.close()
        result = self.social.status()['recommendations']
        self.assertEqual([row['peer'] for row in result], ['22'])
        draft = self.dm.status()['outbox'][0]
        self.assertIn('知识', draft['text'])
        self.assertIn('内容摘记', draft['text'])
        self.assertIn('https://www.bilibili.com/video/', draft['text'])
        self.assertLessEqual(len(draft['text']), 800)
        self.assertEqual(self.platform.sent, [])
        self.social = Social(self.store, self.dm)
        await self.video('2')
        await self.social.tick()
        self.assertEqual(len(self.dm.status()['outbox']), 1)

    async def test_automatic_delivery_reuses_existing_outbox_and_does_not_repeat(self):
        self.friend(auto_send=True)
        await self.video()
        await self.social.tick()
        self.assertEqual(len(self.platform.sent), 1)
        self.assertEqual(self.social.status()['recommendations'][0]['state'], 'sent')
        self.social = Social(self.store, self.dm)
        await self.social.tick()
        self.assertEqual(len(self.platform.sent), 1)

    async def test_uncertain_delivery_is_not_automatically_replayed(self):
        self.friend(auto_send=True)
        self.platform.fail_send = True
        await self.video()
        with self.assertRaisesRegex(RpcError, 'DM_SEND_UNCERTAIN'):
            await self.social.tick()
        await self.social.tick()
        self.assertEqual(len(self.platform.sent), 1)
        self.assertEqual(self.social.status()['recommendations'][0]['state'], 'uncertain')

    async def test_old_videos_negative_impressions_and_disabled_recommendations_do_not_send(self):
        await self.video()
        self.friend(auto_send=True)
        await self.social.tick()
        self.assertEqual(self.dm.status()['outbox'], [])
        key = await self.video('2')
        with self.store.db:
            self.store.db.execute('UPDATE experiences SET valence=-1 WHERE video_id=?', (key,))
        await self.social.tick()
        self.assertEqual(self.dm.status()['outbox'], [])
        self.friend(recommend=False, auto_send=False)
        await self.video('3')
        await self.social.tick()
        self.assertEqual(self.platform.sent, [])

    async def test_friend_preferences_are_account_scoped_and_only_given_to_their_own_replies(self):
        self.friend(style='甲喜欢直接说重点')
        self.friend('33', style='乙喜欢展开讨论')
        self.dm.ingest('22', [message()])
        await self.dm.draft_reply('101')
        context = self.model.calls[-1][1]
        self.assertEqual(context['friend']['style'], '甲喜欢直接说重点')
        self.assertNotIn('乙喜欢', str(context))
        self.dm.account = '88'
        self.assertEqual(self.social.status()['friends'], [])
        self.dm.account = '77'
        self.dm.send_enabled = False
        with self.assertRaisesRegex(RpcError, 'CONFIGURE_DM_SEND_SCOPE_FIRST'):
            self.friend(auto_send=True)

    async def test_enabling_recommendations_does_not_send_videos_completed_while_disabled(self):
        self.friend(recommend=False)
        await self.video()
        self.friend(recommend=True)
        await self.social.tick()
        self.assertEqual(self.dm.status()['outbox'], [])

    async def test_explicit_feedback_is_scoped_idempotent_and_affects_following_matches(self):
        self.friend(auto_send=True)
        key = await self.video()
        await self.social.tick()
        video = self.runner.result(key)['result']
        self.social.feedback('22', video['bvid'], 'too_long')
        first = self.social.preferences('22')['max_seconds']
        self.social.feedback('22', video['bvid'], 'too_long')
        self.assertEqual(self.social.preferences('22')['max_seconds'], first)
        self.assertLess(first, 180)
        self.assertFalse(self.social.matches('22', video))
        self.assertTrue(self.social.matches('33', video))
        with self.assertRaisesRegex(RpcError, 'RECOMMENDATION_NOT_DELIVERED'):
            self.social.feedback('33', video['bvid'], 'seen')
        self.assertEqual(self.social.feedback_context('33'), [])
        self.social = Social(self.store, self.dm)
        self.assertEqual(self.social.preferences('22')['max_seconds'], first)

    async def test_quiet_hours_and_daily_video_cap_do_not_count_explicit_requests(self):
        self.friend(auto_send=True, preferences={'push_start': '22:00', 'push_end': '07:00', 'daily_limit': 1})
        noon = datetime(2026, 11, 5, 12, tzinfo=LOCAL_TIME)
        self.assertEqual(self.social.delivery_status('22', noon)['reason'], 'RECOMMENDATION_QUIET_HOURS')
        self.assertIsNone(self.social.delivery_status('22', noon.replace(hour=23))['reason'])
        self.assertIsNone(self.social.delivery_status('22', noon.replace(hour=6))['reason'])
        self.social.save_preferences('22', {'push_start': '00:00', 'push_end': '00:00'})
        key = await self.video()
        await self.social.tick()
        self.assertEqual(self.social.delivery_status('22')['reason'], 'RECOMMENDATION_DAILY_LIMIT')
        result = self.runner.result(key)['result']
        explicit = self.social.recommend('22', key, result, '这是你明确提出的需求', request_id=10)
        with patch('bili_agent.messaging.MIN_SEND_INTERVAL', 0):
            await self.dm.publish(explicit)
        self.assertEqual(self.social.delivery_status('22')['used'], 1)

    async def test_simple_commands_and_followups_do_not_leak_between_peers(self):
        self.friend(auto_send=True)
        await self.video()
        await self.social.tick()
        self.dm.ingest('22', [message('101', text='学习目标：学会整理表格')])
        reply = await self.dm.draft_reply('101')
        self.assertIn('记住', reply['text'])
        self.assertEqual(self.model.calls, [])
        self.assertEqual(self.social.preferences('22')['goal'], '学会整理表格')
        self.dm.ingest('22', [message('102', text='这个视频哪里有用？')])
        await self.dm.draft_reply('102')
        self.assertTrue(self.model.calls[-1][1]['video_summaries'])
        self.dm.ingest('33', [message('103', peer='33', text='哪里有用？')])
        await self.dm.draft_reply('103')
        self.assertEqual(self.model.calls[-1][1]['video_summaries'], [])
        self.assertEqual(self.model.calls[-1][1]['preferences']['goal'], '')
        self.assertIsNone(self.social.handle_message('22', '别人说今天先别发'))
        self.social.handle_message('22', '今天先别发')
        self.assertEqual(self.social.delivery_status('22')['reason'], 'RECOMMENDATIONS_PAUSED')
        self.social.handle_message('22', '恢复推荐')
        self.assertIsNone(self.social.delivery_status('22')['reason'])

    async def test_recommendation_contains_verified_timestamp_and_respects_message_length(self):
        self.friend()
        key = await self.video()
        result = self.runner.result(key)['result']
        result.update(title='题' * 300, summary='摘' * 1500)
        draft_id = self.social.recommend('22', key, result, '理' * 180)
        body = self.store.db.execute('SELECT text FROM dm_outbox WHERE id=?', (draft_id,)).fetchone()[0]
        self.assertLessEqual(len(body), 800)
        self.assertIn('内容依据', body)
        self.assertIn('&t=', body)

    async def test_block_author_uses_verified_uid(self):
        self.friend(auto_send=True)
        video = {**sample(), 'author': '测试UP', 'author_uid': '888'}
        key = self.runner.enqueue(video)['id']
        await self.runner.analyze(self.store.db.execute('SELECT * FROM videos WHERE id=?', (key,)).fetchone())
        await self.social.tick()
        self.social.feedback('22', video['bvid'], 'block_author')
        self.assertEqual(self.social.preferences('22')['blocked_authors'], ['888'])
        self.assertFalse(self.social.matches('22', {**video, 'bvid': 'BV0000000001'}))
        self.assertTrue(self.social.matches('33', video))


if __name__ == '__main__':
    unittest.main()
