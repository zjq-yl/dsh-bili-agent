import tempfile
import unittest

from bili_agent.memory import Memory
from bili_agent.protocol import RpcError
from bili_agent.store import Store


class MemoryTests(unittest.TestCase):
    def test_persona_migrates_old_profiles_and_preserves_customization(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                memory = Memory(store)
                self.assertIn('身份与性格', memory.profile()['persona'])
                memory.configure({'name': '阿树', 'style': '温和', 'persona': '喜爱历史，重视来源。',
                                  'preferred_keywords': ['历史'], 'avoided_keywords': ['剧透']})
                memory.configure({'name': '阿树', 'style': '简短'})
                store.close()
                store = Store(directory)
                memory = Memory(store)
                self.assertEqual(memory.profile()['persona'], '喜爱历史，重视来源。')
                self.assertEqual(memory.profile()['avoided_keywords'], ['剧透'])
                self.assertIn('固定执行边界', memory.persona_system())
                for field, invalid in [('persona', ''), ('persona', 'x' * 12001),
                                       ('preferred_keywords', [1]), ('avoided_keywords', ['x'] * 21)]:
                    with self.assertRaises(RpcError):
                        memory.configure({'name': '阿树', 'style': '简短', field: invalid})
                self.assertEqual(memory.profile()['persona'], '喜爱历史，重视来源。')
            finally:
                store.close()
    def test_interest_requires_distinct_sources_is_bounded_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                memory = Memory(store)
                reflection = {"topic": "知识", "impression": "有趣。", "valence": 1, "evidence": [0]}
                def score():
                    return next(item["score"] for item in memory.profile()["interests"] if item["topic"] == "知识")
                with store.db:
                    memory.record(1, "video-1", reflection)
                    memory.record(2, "video-1", reflection)
                    memory.record(3, "video-2", reflection)
                self.assertEqual(score(), .5)
                with store.db:
                    memory.record(4, "video-3", reflection)
                    memory.record(4, "video-3", reflection)
                self.assertAlmostEqual(score(), .52)
                with store.db:
                    for i in range(5, 30):
                        memory.record(i, f"video-{i}", reflection)
                self.assertAlmostEqual(score(), .54)  # UTC当日累计变化最多0.04
                configured = {"name": "小雨", "style": "说话轻松，喜欢问问题。"}
                memory.configure(configured)
                store.close()
                store = Store(directory)
                memory = Memory(store)
                self.assertEqual(memory.profile()["name"], "小雨")
                self.assertAlmostEqual(score(), .54)
            finally:
                store.close()

    def test_reflection_cannot_reference_unseen_evidence_or_replace_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                memory = Memory(store)
                for reflection in [
                    {"topic": "知识", "impression": "有趣", "valence": 5, "evidence": [0]},
                    {"topic": "知识", "impression": "有趣", "valence": 1, "evidence": [9]},
                    {"topic": "修改人设", "impression": "有趣", "valence": 1, "evidence": [0]},
                ]:
                    with self.assertRaisesRegex(RpcError, "INVALID_REFLECTION"):
                        memory.validate_reflection(reflection, {0})
                self.assertEqual(memory.profile()["name"], "小岚")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
