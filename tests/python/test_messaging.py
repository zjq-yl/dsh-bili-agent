import asyncio
import json
import tempfile
import time
import unittest
from unittest.mock import patch

from bili_agent.memory import Memory
from bili_agent.messaging import Messaging
from bili_agent.protocol import RpcError
from bili_agent.runner import Runner
from bili_agent.store import Store


def message(key='101', peer='22', text='我喜欢编程教程', age=10):
    return {'msg_key': key, 'sender_uid': peer, 'receiver_id': '77', 'receiver_type': 1,
            'msg_type': 1, 'timestamp': time.time() - age, 'content': json.dumps({'content': text})}


class FakePlatform:
    def __init__(self):
        self.uid, self.sent, self.inbox = '77', [], []
        self.fail_send = False

    async def request(self, operation, params):
        if operation == 'identity':
            return {'uid': self.uid, 'name': 'test', 'can_send_dm': True}
        if operation == 'sessions':
            return {'session_list': [{'talker_id': 22, 'session_type': 1}]}
        if operation == 'messages':
            return {'messages': self.inbox}
        if operation == 'send_dm':
            self.sent.append(params)
            if self.fail_send:
                raise TimeoutError('unknown remote result')
            return {'msg_key': '9007199254740993'}
        raise AssertionError(operation)


class FakeModel:
    def __init__(self):
        self.calls = []

    async def generate(self, purpose, system, text):
        value = json.loads(text)
        self.calls.append((purpose, value))
        if purpose == 'dm_reply':
            return json.dumps({'reply': '我记下了，你更喜欢哪种教程？'})
        return json.dumps({'summary': '对方分享了自己的兴趣。', 'facts': [
            {'text': '对方表示喜欢编程教程。', 'evidence': [value['messages'][0]['msg_key']]}]})


class MessagingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.memory = Memory(self.store)
        self.platform, self.model = FakePlatform(), FakeModel()
        self.dm = Messaging(self.store, self.platform, self.model, self.memory, True, ['22', '33'])
        await self.dm.bind()

    async def asyncTearDown(self):
        await self.dm.close()
        self.store.close()
        self.temp.cleanup()

    async def test_sync_deduplicates_filters_non_text_and_wrong_peer(self):
        self.platform.inbox = [message(), message(), {**message('102'), 'msg_type': 2}, message('103', peer='33')]
        result = await self.dm.sync('22')
        self.assertEqual(result['new_messages'], 1)
        self.assertEqual((await self.dm.sync('22'))['new_messages'], 0)
        self.assertEqual(len(self.dm.history('22')), 1)

    async def test_reply_is_durable_draft_then_explicit_idempotent_send(self):
        self.dm.ingest('22', [message()])
        draft = await self.dm.draft_reply('101')
        self.assertEqual(draft['state'], 'draft')
        self.assertEqual(self.platform.sent, [])
        self.assertEqual((await self.dm.draft_reply('101'))['id'], draft['id'])
        self.assertEqual(len(self.model.calls), 1)
        result = await self.dm.publish(draft['id'])
        self.assertEqual(result['state'], 'sent')
        self.assertEqual(result['receipt'], '9007199254740993')
        await self.dm.publish(draft['id'])
        self.assertEqual(len(self.platform.sent), 1)
        self.assertEqual(self.platform.sent[0]['peer_uid'], '22')
        self.assertEqual(self.dm.history('22')[-1]['direction'], 'outgoing')

    async def test_custom_persona_is_used_for_dm_without_granting_send_permission(self):
        self.memory.configure({'name': '阿树', 'style': '简短', 'persona': '喜欢历史，说话温和。'})
        self.dm.send_enabled = False
        generate = self.model.generate
        systems = []
        async def capture(purpose, system, text):
            systems.append(system)
            return await generate(purpose, system, text)
        self.model.generate = capture
        self.dm.ingest('22', [message()])
        draft = await self.dm.draft_reply('101')
        self.assertIn('喜欢历史，说话温和。', systems[0])
        self.assertIn('固定执行边界', systems[0])
        with self.assertRaisesRegex(RpcError, 'DM_SEND_DISABLED'):
            await self.dm.publish(draft['id'])
        self.assertEqual(self.platform.sent, [])

    async def test_unknown_send_is_not_replayed_after_restart(self):
        self.platform.fail_send = True
        draft = self.dm.compose('22', 'test', 'test-command-1')
        self.assertEqual((await self.dm.publish(draft['id']))['state'], 'uncertain')
        self.store.close()
        self.store = Store(self.temp.name)
        self.dm = Messaging(self.store, self.platform, self.model, Memory(self.store), True, ['22'])
        result = await self.dm.publish(draft['id'])
        self.assertEqual(result['state'], 'uncertain')
        self.assertEqual(len(self.platform.sent), 1)

    async def test_account_and_recipient_guards_and_command_conflict(self):
        self.dm.compose('22', 'hello', 'test-command-2')
        with self.assertRaisesRegex(RpcError, 'IDEMPOTENCY_KEY_CONFLICT'):
            self.dm.compose('33', 'hello', 'test-command-2')
        draft = self.dm.compose('44', 'hello', 'test-command-3')
        with self.assertRaisesRegex(RpcError, 'RECIPIENT_NOT_ENABLED'):
            await self.dm.publish(draft['id'])
        self.platform.uid = '88'
        with self.assertRaisesRegex(RpcError, 'DATA_DIR_ACCOUNT_MISMATCH'):
            await self.dm.sync()
        self.assertEqual(self.platform.sent, [])

    async def test_memory_and_reply_context_are_isolated_and_persisted(self):
        self.dm.ingest('22', [message(text='只有甲知道的事情')])
        summary = await self.dm.summarize('22')
        self.assertEqual(summary['facts'][0]['evidence'], ['101'])
        await self.dm.summarize('22')
        self.assertEqual(len(self.model.calls), 1)
        self.dm.ingest('33', [message('201', peer='33', text='你好')])
        await self.dm.draft_reply('201')
        context = self.model.calls[-1][1]
        self.assertEqual(context['private_memories'], [])
        self.assertNotIn('只有甲', json.dumps(context, ensure_ascii=False))
        self.store.close()
        self.store = Store(self.temp.name)
        self.dm = Messaging(self.store, self.platform, self.model, Memory(self.store))
        self.assertEqual(self.dm.private_memory('22'), [summary])
        self.assertEqual(self.dm.private_memory('33'), [])

    async def test_forget_clears_context_and_cannot_be_restored_by_resync(self):
        original = message()
        self.dm.ingest('22', [original])
        await self.dm.summarize('22')
        await self.dm.draft_reply('101')
        self.dm.forget('22')
        self.assertEqual(self.dm.history('22'), [])
        self.assertEqual(self.dm.private_memory('22'), [])
        self.assertEqual(self.dm.ingest('22', [original]), 0)
        self.assertEqual(self.dm.history('22'), [])
        self.assertEqual(self.dm.status()['outbox'][0]['state'], 'cancelled')
        self.assertEqual(self.dm.status()['outbox'][0]['text'], '')

    async def test_forget_during_model_call_discards_stale_output(self):
        self.dm.ingest('22', [message()])
        entered, release = asyncio.Event(), asyncio.Event()
        async def generate(*args):
            entered.set()
            await release.wait()
            return '{"reply":"stale private text"}'
        self.model.generate = generate
        pending = asyncio.create_task(self.dm.draft_reply('101'))
        await entered.wait()
        self.dm.forget('22')
        release.set()
        with self.assertRaisesRegex(RpcError, 'CONTEXT_CHANGED'):
            await pending
        self.assertEqual(self.dm.status()['outbox'], [])

    async def test_background_preview_ignores_old_messages_and_never_sends(self):
        self.platform.inbox = [message('100', age=100), message('101', age=1)]
        self.dm.task = asyncio.create_task(self.dm.watch(time.time() - 2))
        async with asyncio.timeout(3):
            while not self.dm.status()['outbox']:
                await asyncio.sleep(.005)
        await self.dm.close()
        self.assertEqual(self.dm.status()['outbox'][0]['source_key'], '101')
        self.assertEqual(self.platform.sent, [])

    async def test_video_share_card_is_received_once_and_supplies_bvid_to_reply_context(self):
        Runner(self.store, self.model)  # The host initializes the video store before messaging.
        card = {**message(), 'msg_type': 7, 'content': json.dumps({
            'title': 'Python 入门', 'jump_url': 'https://www.bilibili.com/video/BV17x411w7KC/'})}
        self.platform.inbox = [card, card]
        self.assertEqual((await self.dm.sync('22'))['new_messages'], 1)
        self.assertEqual(self.dm.history('22')[0]['text'], '[分享视频] Python 入门 BV17x411w7KC')
        await self.dm.draft_reply('101')
        context = self.model.calls[-1][1]
        self.assertIn('BV17x411w7KC', context['reply_to']['text'])
        self.assertEqual(context['video_summaries'], [])
        self.assertEqual(self.platform.sent, [])

    async def test_malformed_cards_and_non_video_media_are_ignored(self):
        entries = [{**message(str(200 + i)), 'msg_type': kind, 'content': content} for i, (kind, content) in enumerate([
            (7, 'not-json'), (8, '{"title":{}}'), (7, '{"jump_url":"file:///secret"}'),
            (2, '{"content":"image"}'), (7, '[]'),
        ])]
        self.assertEqual(self.dm.ingest('22', entries), 0)
        self.assertEqual(self.dm.history('22'), [])

    async def test_auto_reply_continues_after_two_rounds_and_deduplicates(self):
        with patch('bili_agent.messaging.POLL_INTERVAL', .005), patch('bili_agent.messaging.MIN_SEND_INTERVAL', 0):
            await self.dm.start(auto_send=True)
            for index in range(3):
                self.platform.inbox.append(message(str(101 + index), age=0))
                async with asyncio.timeout(2):
                    while len(self.platform.sent) < index + 1:
                        await asyncio.sleep(.005)
            await asyncio.sleep(.03)
            self.assertEqual(len(self.platform.sent), 3)
            self.assertEqual(len(self.model.calls), 3)
            self.assertTrue(self.dm.status()['auto_send'])
            await self.dm.close()

    async def test_progress_and_private_logs_cover_check_generation_and_receipt(self):
        entered, release = asyncio.Event(), asyncio.Event()
        generate = self.model.generate
        async def delayed(purpose, system, text):
            if purpose == 'dm_reply':
                entered.set()
                await release.wait()
            return await generate(purpose, system, text)
        self.model.generate = delayed
        with patch('bili_agent.messaging.POLL_INTERVAL', .05):
            await self.dm.start(auto_send=True)
            self.platform.inbox = [message(text='PRIVATE_MESSAGE_MUST_NOT_APPEAR_IN_LOGS', age=0)]
            await asyncio.wait_for(entered.wait(), 2)
            during = self.dm.status()
            self.assertTrue(any(item['stage'] == 'reply' for item in during['activities']))
            self.assertIsNotNone(during['next_poll_at'])  # Reception continues during model generation.
            self.assertFalse(during['incoming'][0]['before_listener'])
            self.assertNotIn('text', during['incoming'][0])
            release.set()
            async with asyncio.timeout(2):
                while not self.platform.sent or self.dm.next_poll_at is None:
                    await asyncio.sleep(.005)
            status = self.dm.status()
            self.assertGreater(status['next_poll_at'], status['last_sync'])
            self.assertEqual(status['incoming'][0]['reply_state'], 'sent')
            log = json.dumps(status['logs'], ensure_ascii=False)
            for text in ('检查完成', '收到来自', '回复草稿已就绪', '私信已确认发送'):
                self.assertIn(text, log)
            self.assertNotIn('PRIVATE_MESSAGE_MUST_NOT_APPEAR_IN_LOGS', log)
            await self.dm.close()
            self.assertIsNone(self.dm.status()['next_poll_at'])
            self.assertEqual(self.dm.status()['activities'], [])

    async def test_blocked_listener_has_no_countdown_and_logs_only_safe_error(self):
        async def broken(*args):
            raise ValueError('SECRET_PROVIDER_TOKEN_MUST_NOT_BE_LOGGED')
        self.model.generate = broken
        await self.dm.start(auto_send=True)
        self.platform.inbox = [message(age=0)]
        async with asyncio.timeout(2):
            while self.dm.state != 'BLOCKED':
                await asyncio.sleep(.005)
        status = self.dm.status()
        self.assertEqual(status['error'], 'ValueError')
        self.assertIsNone(status['next_poll_at'])
        self.assertEqual(status['activities'], [])
        log = json.dumps(status['logs'], ensure_ascii=False)
        self.assertIn('监听已暂停', log)
        self.assertNotIn('SECRET_PROVIDER_TOKEN', log)

    async def test_filtered_dm_logs_remain_visible_among_other_activity(self):
        await self.dm.sync('22')
        for index in range(105):
            self.store.event('analysis', 'other activity', str(index))
        status = self.dm.status()
        self.assertTrue(status['logs'])
        self.assertTrue(all(event['category'] == 'dm' for event in status['logs']))
        self.assertTrue(any('检查完成' in event['message'] for event in status['logs']))

    async def test_auto_reply_coalesces_burst_without_replying_to_older_message(self):
        with patch('bili_agent.messaging.POLL_INTERVAL', .005), patch('bili_agent.messaging.MIN_SEND_INTERVAL', 0):
            await self.dm.start(auto_send=True)
            self.platform.inbox = [message('100', age=100), message('101', age=0), message('102', age=0)]
            async with asyncio.timeout(2):
                while not self.platform.sent:
                    await asyncio.sleep(.005)
            await asyncio.sleep(.03)
            self.assertEqual(len(self.platform.sent), 1)
            self.assertEqual(self.model.calls[0][1]['reply_to']['msg_key'], '102')
            states = dict(self.store.db.execute('SELECT msg_key,state FROM dm_messages WHERE direction="incoming"'))
            self.assertEqual(states, {'100': 'new', '101': 'coalesced', '102': 'drafted'})
            await self.dm.close()

    async def test_running_preview_switches_to_auto_without_losing_pending_reply(self):
        with patch('bili_agent.messaging.POLL_INTERVAL', .005):
            await self.dm.start(auto_send=False)
            task = self.dm.task
            self.platform.inbox = [message(age=0)]
            async with asyncio.timeout(2):
                while not self.dm.status()['outbox']:
                    await asyncio.sleep(.005)
            self.assertEqual(self.platform.sent, [])
            await self.dm.start()
            self.assertTrue(self.dm.status()['auto_send'])
            self.assertIs(self.dm.task, task)
            async with asyncio.timeout(2):
                while not self.platform.sent:
                    await asyncio.sleep(.005)
            self.assertEqual(len(self.model.calls), 1)
            await self.dm.start(auto_send=False)
            self.assertFalse(self.dm.auto_send)

    async def test_transient_platform_failure_retries_instead_of_stopping_listener(self):
        original = self.platform.request
        failed = False
        async def flaky(operation, params):
            nonlocal failed
            if operation == 'messages' and not failed:
                failed = True
                raise RpcError(-32010, 'PLATFORM_REQUEST_FAILED')
            return await original(operation, params)
        self.platform.request = flaky
        with patch('bili_agent.messaging.POLL_INTERVAL', .005):
            await self.dm.start()
            self.platform.inbox = [message(age=0)]
            async with asyncio.timeout(2):
                while not self.platform.sent:
                    await asyncio.sleep(.005)
            self.assertTrue(failed)
            self.assertEqual(self.dm.state, 'RUNNING')

    async def test_successive_late_messages_trigger_without_restarting_listener(self):
        self.dm.ingest('22', [message('100', age=120)])
        with patch('bili_agent.messaging.POLL_INTERVAL', 3600), patch('bili_agent.messaging.MIN_SEND_INTERVAL', 0):
            await self.dm.start()
            for index in range(3):
                self.dm.ingest('22', [message(str(101 + index), age=90 - index)])
                async with asyncio.timeout(2):
                    while len(self.platform.sent) <= index:
                        await asyncio.sleep(.005)
            self.assertEqual(len(self.model.calls), 3)
            self.assertEqual(self.dm.state, 'RUNNING')
            self.assertFalse(self.dm.status()['incoming'][0]['before_listener'])

    async def test_restart_recovers_unanswered_turn_after_previous_reply(self):
        self.dm.ingest('22', [message('100', age=120), message('101', age=90)])
        with self.store.db:
            self.store.db.execute("UPDATE dm_messages SET state='drafted' WHERE msg_key='100'")
        await self.dm.start()
        async with asyncio.timeout(2):
            while not self.platform.sent:
                await asyncio.sleep(.005)
        self.assertEqual(self.model.calls[0][1]['reply_to']['msg_key'], '101')

    async def test_invalid_reply_does_not_stop_later_conversation(self):
        original = self.model.generate
        async def broken(purpose, system, text):
            if json.loads(text).get('reply_to', {}).get('msg_key') == '101':
                return 'not json'
            return await original(purpose, system, text)
        self.model.generate = broken
        with patch('bili_agent.messaging.POLL_INTERVAL', .005), patch('bili_agent.messaging.MIN_SEND_INTERVAL', 0):
            await self.dm.start()
            self.platform.inbox = [message('101', age=0)]
            async with asyncio.timeout(2):
                while not self.platform.sent:
                    await asyncio.sleep(.005)
            self.platform.inbox.append(message('102', age=0))
            async with asyncio.timeout(2):
                while len(self.platform.sent) < 2:
                    await asyncio.sleep(.005)
            self.assertEqual(self.dm.state, 'RUNNING')

    async def test_uncertain_send_keeps_listening_without_resending_it(self):
        with patch('bili_agent.messaging.POLL_INTERVAL', .005), patch('bili_agent.messaging.MIN_SEND_INTERVAL', 0):
            self.platform.fail_send = True
            await self.dm.start()
            self.platform.inbox = [message('101', age=0)]
            async with asyncio.timeout(2):
                while not self.platform.sent:
                    await asyncio.sleep(.005)
            self.platform.fail_send = False
            self.platform.inbox.append(message('102', age=0))
            async with asyncio.timeout(2):
                while len(self.platform.sent) < 2:
                    await asyncio.sleep(.005)
            self.assertEqual(self.dm.state, 'RUNNING')
            self.assertEqual(self.store.db.execute("SELECT state FROM dm_outbox WHERE source_key='101'").fetchone()[0], 'uncertain')

    async def test_memory_summary_does_not_block_new_inbox_checks(self):
        entered = asyncio.Event()
        async def slow_memory(peer):
            entered.set()
            await asyncio.Event().wait()
        self.dm.summarize = slow_memory
        with patch('bili_agent.messaging.POLL_INTERVAL', .005):
            await self.dm.start()
            self.platform.inbox = [message(str(100 + i), age=0) for i in range(6)]
            await asyncio.wait_for(entered.wait(), 2)
            previous = self.dm.last_sync
            async with asyncio.timeout(2):
                while self.dm.last_sync == previous:
                    await asyncio.sleep(.005)
            self.assertEqual(self.dm.state, 'RUNNING')

    async def test_message_event_replies_without_waiting_for_next_poll_and_deduplicates(self):
        with patch('bili_agent.messaging.POLL_INTERVAL', 3600):
            await self.dm.start()
            async with asyncio.timeout(2):
                while self.dm.last_sync is None:
                    await asyncio.sleep(.005)
            previous = self.dm.last_sync
            incoming = message(age=0)
            self.dm.ingest('22', [incoming, incoming])
            async with asyncio.timeout(2):
                while not self.platform.sent:
                    await asyncio.sleep(.005)
            self.dm.ingest('22', [incoming])
            await asyncio.sleep(.02)
            self.assertEqual(len(self.platform.sent), 1)
            self.assertEqual(len(self.model.calls), 1)
            self.assertEqual(self.dm.last_sync, previous)

    async def test_new_messages_are_received_and_persisted_while_reply_model_is_blocked(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.model.generate
        async def slow(*args):
            entered.set()
            await release.wait()
            return await original(*args)
        self.model.generate = slow
        with patch('bili_agent.messaging.POLL_INTERVAL', .005):
            await self.dm.start()
            self.platform.inbox = [message('101', age=0)]
            await asyncio.wait_for(entered.wait(), 2)
            self.platform.inbox.append(message('102', age=0))
            async with asyncio.timeout(2):
                while len(self.dm.history('22')) < 2:
                    await asyncio.sleep(.005)
            row = self.store.db.execute("SELECT accepted,state FROM dm_messages WHERE msg_key='102'").fetchone()
            self.assertEqual(tuple(row), (1, 'new'))
            self.assertEqual(self.platform.sent, [])
            release.set()

    async def test_auto_reply_waits_when_another_sender_owns_the_send_lock(self):
        original = self.dm.publish
        attempts = 0
        async def busy_once(draft_id):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RpcError(-32002, 'DM_SEND_BUSY')
            return await original(draft_id)
        self.dm.publish = busy_once
        with patch('bili_agent.messaging.POLL_INTERVAL', .005):
            await self.dm.start(auto_send=True)
            self.platform.inbox = [message(age=0)]
            async with asyncio.timeout(2):
                while not self.platform.sent:
                    await asyncio.sleep(.005)
            self.assertEqual(self.dm.state, 'RUNNING')
            self.assertEqual(len(self.model.calls), 1)
            await self.dm.close()

    async def test_cancel_after_send_start_is_uncertain_and_cannot_replay(self):
        draft = self.dm.compose('22', 'test', 'test-command-4')
        original = self.platform.request
        entered = asyncio.Event()
        async def pending(operation, params):
            if operation == 'send_dm':
                entered.set()
                await asyncio.Event().wait()
            return await original(operation, params)
        self.platform.request = pending
        task = asyncio.create_task(self.dm.publish(draft['id']))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.dm.status()['outbox'][0]['state'], 'uncertain')
        self.platform.request = original
        self.assertEqual((await self.dm.publish(draft['id']))['state'], 'uncertain')
        self.assertEqual(self.platform.sent, [])

    async def test_missing_receipt_stays_uncertain_and_disabled_send_never_calls_platform(self):
        draft = self.dm.compose('22', 'test', 'test-command-5')
        self.dm.send_enabled = False
        with self.assertRaisesRegex(RpcError, 'DM_SEND_DISABLED'):
            await self.dm.publish(draft['id'])
        self.assertEqual(self.platform.sent, [])
        self.dm.send_enabled = True
        original = self.platform.request
        async def no_receipt(operation, params):
            result = await original(operation, params)
            return {} if operation == 'send_dm' else result
        self.platform.request = no_receipt
        self.assertEqual((await self.dm.publish(draft['id']))['state'], 'uncertain')
        self.assertEqual(len(self.platform.sent), 1)

    async def test_model_outage_preserves_accepted_message_across_listener_restart(self):
        original = self.model.generate
        async def unavailable(*args):
            raise RpcError(-32006, 'HOST_CALL_FAILED')
        self.model.generate = unavailable
        with patch('bili_agent.messaging.POLL_INTERVAL', .01), patch('bili_agent.messaging.MIN_SEND_INTERVAL', 0):
            await self.dm.start(auto_send=True)
            self.platform.inbox = [message(age=0)]
            async with asyncio.timeout(2):
                while not self.platform.sent:
                    await asyncio.sleep(.005)
            await self.dm.close()
            self.assertIn('暂时不可用', self.platform.sent[0]['text'])
            row = self.store.db.execute("SELECT accepted,state FROM dm_messages WHERE msg_key='101'").fetchone()
            self.assertEqual(tuple(row), (1, 'retry'))
            self.model.generate = original
            self.dm = Messaging(self.store, self.platform, self.model, self.memory, True, ['22', '33'])
            await self.dm.start(auto_send=True)
            async with asyncio.timeout(2):
                while len(self.platform.sent) < 2:
                    await asyncio.sleep(.005)
            await self.dm.close()
        self.assertEqual(len(self.platform.sent), 2)
        self.assertIn('我记下了', self.platform.sent[1]['text'])


if __name__ == '__main__':
    unittest.main()
