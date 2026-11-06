import asyncio
import json
import tempfile
import unittest
from unittest.mock import AsyncMock

from bili_agent.content import validate_video, parse_summary, parse_analysis
from bili_agent.protocol import RpcError
from bili_agent.runner import Runner
from bili_agent.store import Store


def sample(cid="1"):
    return {"bvid": "BV17x411w7KC", "cid": cid, "title": "离线合成字幕：学习方法", "duration": 180,
            "subtitles": [{"from": 0, "to": 20, "content": "先设定一个小目标。"},
                          {"from": 130, "to": 150, "content": "复习时主动回忆刚学的内容。"}]}


class FakeModel:
    def __init__(self):
        self.inputs = []
        self.block_at = None
        self.entered = asyncio.Event()
        self.invalid = False

    async def generate(self, purpose, system, text):
        value = json.loads(text)
        self.inputs.append((purpose, value))
        if len(self.inputs) == self.block_at:
            self.entered.set()
            await asyncio.Event().wait()
        ids = [part["id"] for part in value["subtitles"]] if "subtitles" in value else value.get('allowed_evidence_ids', [0])[:1]
        return json.dumps({"summary": "作者分享学习方法。", "facts": [
            {"text": "作者建议设定小目标。", "evidence": [99999] if self.invalid else ids}],
            "reflection": {"topic": "知识", "impression": "我想试试这种学习方法。", "valence": 1, "evidence": ids},
            "analysis": {"question": "怎样学习？", "conclusion": "从小目标开始。", "reasoning": "作者通过目标和回忆连接学习步骤。",
                         "caveats": "未提供对照实验。", "takeaways": "可先设定一个小目标。", "evidence": ids}})


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.model = FakeModel()
        self.runner = Runner(self.store, self.model)

    async def asyncTearDown(self):
        await self.runner.pause()
        self.store.close()
        self.temp.cleanup()

    async def wait_state(self, target):
        async with asyncio.timeout(3):
            while self.runner.state != target:
                await asyncio.sleep(.005)

    async def test_queue_advances_without_external_events_and_persists_results(self):
        first = self.runner.enqueue(sample())
        second = self.runner.enqueue(sample("2"))
        self.assertEqual(first, self.runner.enqueue(sample()))
        self.assertEqual(self.model.inputs, [])  # 默认暂停，入队不消耗模型调用
        self.runner.start()
        async with asyncio.timeout(3):
            while self.runner.result(second["id"])["state"] != "done":
                await asyncio.sleep(.005)
        self.assertEqual(len(self.model.inputs), 6)
        self.assertEqual(self.model.inputs[0][1]["subtitles"][0]["id"], 0)
        self.assertEqual(len(self.model.inputs[0][1]["subtitles"]), 1)
        result = self.runner.result(first["id"])["result"]
        self.assertFalse(result["actual_playback"])
        self.assertEqual(result["basis"], "subtitles_only")
        self.assertEqual(result["evidence"][0]["id"], 0)
        await self.runner.pause("STOPPED")
        self.assertEqual(self.runner.status()["state"], "STOPPED")

    async def test_deep_summary_requires_valid_evidence_and_reanalysis_keeps_last_result_until_success(self):
        key = self.runner.enqueue(sample())['id']
        await self.runner.analyze(self.store.db.execute('SELECT * FROM videos WHERE id=?', (key,)).fetchone())
        previous = self.runner.result(key)['result']
        self.assertIn('reasoning', previous['analysis'])
        with self.assertRaisesRegex(RpcError, 'INVALID_SUMMARY_EVIDENCE'):
            parse_analysis({**previous['analysis'], 'evidence': [99999]}, {0, 1})
        self.runner.reanalyze(key)
        self.assertEqual(self.runner.result(key)['result'], previous)
        self.assertEqual(self.store.db.execute('SELECT checkpoint FROM videos WHERE id=?', (key,)).fetchone()[0], 0)
        await self.runner.analyze(self.store.db.execute('SELECT * FROM videos WHERE id=?', (key,)).fetchone())
        self.assertEqual(self.runner.result(key)['state'], 'done')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM experiences').fetchone()[0], 1)

    async def test_requested_video_preempts_scheduled_work_between_segments(self):
        scheduled = self.runner.enqueue(sample())['id']
        original = self.model.generate
        async def receive_request(purpose, system, text):
            if purpose == 'segment_summary' and not self.store.db.execute("SELECT 1 FROM videos WHERE priority=0").fetchone():
                self.runner.enqueue({**sample('2'), 'title': '好友点播'}, priority=0)
            return await original(purpose, system, text)
        self.model.generate = receive_request
        await self.runner.analyze(self.store.db.execute('SELECT * FROM videos WHERE id=?', (scheduled,)).fetchone())
        row = self.store.db.execute('SELECT state,checkpoint FROM videos WHERE id=?', (scheduled,)).fetchone()
        self.assertEqual((row['state'], row['checkpoint']), ('queued', 1))
        self.assertEqual(self.store.db.execute("SELECT priority FROM videos WHERE state='queued' ORDER BY priority,id LIMIT 1").fetchone()[0], 0)

    async def test_pause_restart_resume_from_last_completed_segment(self):
        video_id = self.runner.enqueue(sample())["id"]
        self.model.block_at = 2
        self.runner.start()
        await asyncio.wait_for(self.model.entered.wait(), 3)
        await self.runner.pause()
        self.assertEqual(self.runner.status()["queue"][0]["checkpoint"], 1)
        self.store.close()
        self.store = Store(self.temp.name)
        self.model = FakeModel()
        self.runner = Runner(self.store, self.model)
        self.assertEqual(self.runner.state, "PAUSED")
        self.runner.start()
        async with asyncio.timeout(3):
            while self.runner.result(video_id)["state"] != "done":
                await asyncio.sleep(.005)
        self.assertEqual(len(self.model.inputs), 2)
        self.assertEqual(self.model.inputs[0][1]["subtitles"][0]["id"], 1)

    async def test_invalid_evidence_blocks_and_requires_explicit_retry(self):
        self.model.invalid = True
        video_id = self.runner.enqueue(sample())["id"]
        self.runner.start()
        await self.wait_state("BLOCKED")
        self.assertEqual(self.runner.result(video_id)["error"], "INVALID_SUMMARY_EVIDENCE")
        self.assertIsNone(self.runner.result(video_id)["result"])
        self.model.invalid = False
        self.runner.retry(video_id)
        self.runner.start()
        async with asyncio.timeout(3):
            while self.runner.result(video_id)["state"] != "done":
                await asyncio.sleep(.005)

    async def test_idle_makes_no_model_calls(self):
        self.runner.start()
        await asyncio.sleep(.02)
        self.assertEqual(self.runner.state, "IDLE")
        self.assertEqual(self.model.inputs, [])

    async def test_invalid_summary_is_repaired_once_with_feedback_and_original_ids(self):
        generate = self.model.generate
        systems = []
        async def repairable(purpose, system, text):
            systems.append(system)
            result = json.loads(await generate(purpose, system, text))
            if purpose == 'segment_summary' and len(self.model.inputs) == 1:
                result['facts'] *= 7
            return json.dumps(result)
        self.model.generate = repairable
        self.runner.memory.configure({'name': '测试角色', 'style': '简短', 'persona': '喜欢编程，冷幽默。'})
        key = self.runner.enqueue(sample())['id']
        await self.runner.analyze(self.store.db.execute('SELECT * FROM videos WHERE id=?', (key,)).fetchone())
        feedback = self.model.inputs[1][1]['validation_feedback']
        self.assertEqual((feedback['field'], feedback['reason'], feedback['actual']), ('facts', 'count', 7))
        self.assertEqual(self.model.inputs[1][1]['allowed_evidence_ids'], [0])
        self.assertEqual(len(self.model.inputs), 4)
        self.assertEqual(self.runner.result(key)['state'], 'done')
        self.assertIn('喜欢编程，冷幽默。', systems[-1])
        self.assertNotIn('喜欢编程，冷幽默。', systems[0])

    async def test_persistent_failure_records_specific_diagnostic_without_storing_model_output(self):
        self.model.invalid = True
        key = self.runner.enqueue(sample())['id']
        self.runner.start()
        await self.wait_state('BLOCKED')
        detail = self.runner.result(key)['error_detail']
        self.assertEqual(detail, {'field': 'facts[0].evidence', 'reason': 'unknown_ids', 'stage': 'segment:1', 'attempts': 2})
        self.assertEqual(len(self.model.inputs), 2)
        self.assertEqual(self.runner.status()['queue'][0]['total_segments'], 2)
        self.assertEqual(json.loads(self.store.db.execute('SELECT notes FROM videos WHERE id=?', (key,)).fetchone()[0]), [])

    async def test_hierarchical_merges_are_bounded_and_reused_after_final_failure(self):
        video = sample()
        video['duration'] = 2400
        video['subtitles'] = [{'from': i * 130, 'to': i * 130 + 5, 'content': f'第{i}个方法'} for i in range(17)]
        key = self.runner.enqueue(video)['id']
        generate = self.model.generate
        async def broken_final(purpose, system, text):
            result = json.loads(await generate(purpose, system, text))
            if purpose == 'video_summary':
                result['summary'] = 'x' * 1501
            return json.dumps(result)
        self.model.generate = broken_final
        self.runner.start()
        await self.wait_state('BLOCKED')
        self.assertEqual(self.runner.result(key)['error_detail']['stage'], 'final')
        self.assertTrue(all(len(value['notes']) <= 4 for _, value in self.model.inputs if 'notes' in value))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM video_rollups').fetchone()[0], 5)
        await self.runner.pause()
        self.store.close()
        self.store = Store(self.temp.name)
        self.model = FakeModel()
        self.runner = Runner(self.store, self.model)
        self.runner.retry(key)
        await self.runner.analyze(self.store.db.execute('SELECT * FROM videos WHERE id=?', (key,)).fetchone())
        self.assertEqual([p for p, _ in self.model.inputs], ['video_summary'])
        self.assertEqual(self.runner.result(key)['state'], 'done')

    def test_json_wrappers_and_diagnostics_never_accept_unseen_evidence(self):
        value = {'summary': '摘要', 'facts': [{'text': '观点', 'evidence': [5]}]}
        self.assertEqual(parse_summary('```json\n' + json.dumps(value) + '\n```', {5}), value)
        for raw, reason in [('[]', 'object_required'), ('not-json', 'invalid_json'),
                            (json.dumps(value), 'unknown_ids')]:
            with self.assertRaises(RpcError) as raised:
                parse_summary(raw, {0})
            self.assertEqual(raised.exception.data['reason'], reason)
        value['facts'][0]['evidence'] = ['5']
        with self.assertRaises(RpcError) as raised:
            parse_summary(json.dumps(value), {5})
        self.assertEqual(raised.exception.data['reason'], 'integer_ids_required')

    async def test_discovery_applies_explicit_preferred_and_avoided_keywords(self):
        self.runner.memory.configure({'name': '小岚', 'style': '简洁', 'preferred_keywords': ['Python'], 'avoided_keywords': ['广告']})
        video = sample()
        class Platform:
            request = AsyncMock(side_effect=[{'candidates': [
                {'bvid': 'BV17x411w7KD', 'title': 'Python 广告'},
                {'bvid': 'BV17x411w7KE', 'title': '日常'},
                {'bvid': video['bvid'], 'title': 'Python 入门'},
            ]}, video])
        self.runner.platform = Platform()
        self.runner.discovery = {'keyword': '教程', 'remaining_searches': 3, 'next_at': 0}
        await self.runner.discover()
        self.assertEqual(self.runner.last_selection['bvid'], video['bvid'])

    async def test_final_summary_failure_reuses_saved_notes_after_restart(self):
        original_generate = self.model.generate

        async def oversized_final(purpose, system, text):
            result = json.loads(await original_generate(purpose, system, text))
            if purpose == "video_summary":
                result["summary"] = "长" * 1501
            return json.dumps(result)

        self.model.generate = oversized_final
        video_id = self.runner.enqueue(sample())["id"]
        self.runner.start()
        await self.wait_state("BLOCKED")
        self.assertEqual(self.runner.result(video_id)["error"], "INVALID_SUMMARY_EVIDENCE")
        saved = self.store.db.execute("SELECT checkpoint,notes FROM videos WHERE id=?", (video_id,)).fetchone()
        self.assertEqual(saved["checkpoint"], 2)
        self.assertEqual(len(json.loads(saved["notes"])), 2)

        await self.runner.pause()
        self.store.close()
        self.store = Store(self.temp.name)
        self.model = FakeModel()
        self.runner = Runner(self.store, self.model)
        self.runner.retry(video_id)
        self.runner.start()
        async with asyncio.timeout(3):
            while self.runner.result(video_id)["state"] != "done":
                await asyncio.sleep(.005)
        self.assertEqual([purpose for purpose, _ in self.model.inputs], ["video_summary"])
        self.assertEqual(self.runner.result(video_id)["result"]["facts"][0]["evidence"], [0])

    def test_summary_hard_limits_accept_boundary_and_reject_overflow(self):
        fact = {"text": "事" * 500, "evidence": list(range(10))}
        valid = {"summary": "摘" * 1500, "facts": [fact.copy() for _ in range(6)]}
        self.assertEqual(parse_summary(json.dumps(valid), set(range(10))), valid)
        cases = [
            {**valid, "summary": "摘" * 1501},
            {**valid, "facts": [fact] * 7},
            {**valid, "facts": [{**fact, "text": "事" * 501}]},
            {**valid, "facts": [{**fact, "evidence": list(range(11))}]},
        ]
        for value in cases:
            with self.subTest(value=value), self.assertRaisesRegex(RpcError, "INVALID_SUMMARY_EVIDENCE"):
                parse_summary(json.dumps(value), set(range(11)))

    async def test_autonomous_discovery_avoids_seen_videos_and_keeps_session_budget(self):
        self.runner.enqueue(sample())
        new_video = sample("2")
        new_video["bvid"] = "BV17x411w7KD"
        class Platform:
            request = AsyncMock(side_effect=[
                {"candidates": [{"bvid": "BV17x411w7KC", "title": "已看"},
                                {"bvid": "BV17x411w7KD", "title": "知识学习"}]}, new_video])
        self.runner.platform = Platform()
        self.runner.start("学习")
        async with asyncio.timeout(3):
            while len(self.runner.status()["queue"]) != 2 or any(v["state"] != "done" for v in self.runner.status()["queue"]):
                await asyncio.sleep(.005)
        self.assertEqual(self.runner.last_selection["bvid"], "BV17x411w7KD")
        self.assertEqual(self.runner.discovery["remaining_searches"], 2)
        await self.runner.pause()
        self.runner.start()
        self.assertEqual(self.runner.discovery["remaining_searches"], 2)
        self.assertEqual(Platform.request.await_count, 2)

    def test_call_accounting_survives_restart_without_imposing_daily_limit(self):
        first = self.store.reserve_call("test")
        self.store.finish_call(first, "failed")
        self.store.reserve_call("test")
        self.store.close()
        self.store = Store(self.temp.name)
        self.assertEqual(self.store.call_status()[0]["state"], "uncertain")
        for _ in range(125):
            call_id = self.store.reserve_call('test')
            self.store.finish_call(call_id, 'completed')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM model_calls').fetchone()[0], 127)
        self.runner = Runner(self.store, self.model)

    def test_removed_quota_recovers_only_quota_blocked_videos_with_progress_intact(self):
        key = self.runner.enqueue(sample())['id']
        other = self.runner.enqueue(sample('2'))['id']
        with self.store.db:
            self.store.db.execute("UPDATE videos SET state='failed',checkpoint=1,error='MODEL_DAILY_LIMIT' WHERE id=?", (key,))
            self.store.db.execute("UPDATE videos SET state='failed',error='INVALID_SUMMARY_EVIDENCE' WHERE id=?", (other,))
        self.runner = Runner(self.store, self.model)
        self.assertEqual(self.runner.result(key)['state'], 'queued')
        self.assertEqual(self.store.db.execute('SELECT checkpoint FROM videos WHERE id=?', (key,)).fetchone()[0], 1)
        self.assertEqual(self.runner.result(other)['state'], 'failed')
        self.assertEqual(self.runner.recovered_quota_tasks, 1)

    def test_subtitle_input_validation_and_evidence_ids(self):
        for field, value in [("cid", 1), ("bvid", "../file"), ("duration", float("nan")), ("subtitles", [])]:
            video = sample()
            video[field] = value
            with self.assertRaises(RpcError):
                validate_video(video)
        with self.assertRaisesRegex(RpcError, "INVALID_SUMMARY_EVIDENCE"):
            parse_summary('{"summary":"test", "facts":[{"text":"x","evidence":[1]}]}', {0})


if __name__ == "__main__":
    unittest.main()
