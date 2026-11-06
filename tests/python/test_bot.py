import asyncio
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from legacy.comment_bot.adapters import Bilibili, DshResponder, parse_reply
from legacy.comment_bot.bot import Store, eligible, load_config, poll, single_instance


def sample(rpid=100, message="小助手，这个讲的是什么？", mid=22):
    return {"rpid": rpid, "mid": mid, "root": 0, "ctime": int(time.time()) - 1,
            "content": {"message": message, "members": []}}


class FakeBili:
    def __init__(self, comments):
        self.comments, self.sent = comments, []
        self.fail = False

    async def fetch(self, bvid, pages):
        return {"aid": 123, "title": "Python 入门", "desc": "学习变量"}, self.comments

    async def send(self, aid, rpid, text):
        self.sent.append((aid, rpid, text))
        if self.fail:
            raise TimeoutError("结果未知")
        return "999"


class FakeResponder:
    def __init__(self, text="这是关于 Python 变量的入门介绍。"):
        self.text, self.calls = text, 0

    async def reply(self, info, comment):
        self.calls += 1
        return self.text


class BotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with open(Path(__file__).resolve().parents[2] / "legacy/comment_bot/config.example.toml", "rb") as file:
            self.cfg = tomllib.load(file)
        self.cfg["videos"] = ["BV17x411w7KC"]
        self.cfg["database"] = str(Path(self.temp.name) / "bot.db")
        self.store = Store(self.cfg["database"])
        self.addCleanup(lambda: self.store.db.close())
        self.bili = FakeBili([sample()])
        self.responder = FakeResponder()

    async def tick(self, send=False):
        return await poll(self.cfg, self.store, self.bili, self.responder, 11, send)

    def status(self, mode="live"):
        return self.store.db.execute("SELECT status FROM replies WHERE mode=?", (mode,)).fetchone()[0]

    async def test_preview_deduplicates_and_does_not_send(self):
        self.assertEqual(await self.tick(), 1)
        self.assertEqual(await self.tick(), 0)
        self.assertEqual(self.bili.sent, [])
        self.assertEqual(self.responder.calls, 1)
        self.assertEqual(self.status("preview"), "preview")

    async def test_preview_then_live_and_restart_do_not_duplicate(self):
        await self.tick()
        await self.tick(True)
        self.store.db.execute("UPDATE replies SET attempted_at=0")
        self.store.db.commit()
        self.store.db.close()
        self.store = Store(self.cfg["database"])
        await self.tick(True)
        self.assertEqual(len(self.bili.sent), 1)
        self.assertEqual(self.status(), "sent")

    async def test_uncertain_send_is_never_retried(self):
        self.bili.fail = True
        await self.tick(True)
        self.store.db.close()
        self.store = Store(self.cfg["database"])
        # 放开限频，确保实际检验去重而不是被频率限制挡住。
        self.store.db.execute("UPDATE replies SET attempted_at=0")
        self.store.db.commit()
        await self.tick(True)
        self.assertEqual(len(self.bili.sent), 1)
        self.assertEqual(self.status(), "uncertain")

    async def test_crash_after_begin_send_is_uncertain(self):
        key = (11, self.cfg["videos"][0], 100, "live")
        self.store.claim(key)
        self.store.begin_send(key, "hello", 0, "2020-01-01")
        self.store.db.close()
        self.store = Store(self.cfg["database"])
        await self.tick(True)
        self.assertEqual(self.bili.sent, [])
        self.assertEqual(self.status(), "uncertain")

    async def test_rate_and_daily_limit_survive_restart(self):
        self.bili.comments.append(sample(101))
        await self.tick(True)
        self.assertEqual(len(self.bili.sent), 1)
        self.cfg["max_per_day"] = 1
        self.store.db.execute("UPDATE replies SET attempted_at=0")
        self.store.db.commit()
        self.store.db.close()
        self.store = Store(self.cfg["database"])
        await self.tick(True)
        self.assertEqual(len(self.bili.sent), 1)

    async def test_skipping_and_generation_failure_never_send(self):
        self.responder.text = None
        await self.tick(True)
        self.assertEqual(self.status(), "skipped")
        self.bili.comments = [sample(101)]
        async def fail(*args):
            raise ValueError("错误")
        self.responder.reply = fail
        await self.tick(True)
        self.assertEqual(self.bili.sent, [])
        row = self.store.db.execute("SELECT status FROM replies WHERE rpid=101").fetchone()
        self.assertEqual(row[0], "failed")

    async def test_scan_continues_after_bad_comment(self):
        self.bili.comments = [{}, sample(mid=11), sample(message="路过"), sample()]
        await self.tick()
        self.assertEqual(self.responder.calls, 1)

    def test_trigger_age_self_and_nested_filter(self):
        now = time.time()
        self.assertTrue(eligible(sample(), 11, self.cfg, now))
        for comment in (sample(mid=11), sample(message="路过")):
            self.assertFalse(eligible(comment, 11, self.cfg, now))
        mention = sample(message="你好")
        mention["content"]["members"] = [{"mid": "11"}]
        self.assertTrue(eligible(mention, 11, self.cfg, now))
        old = sample()
        old["ctime"] = int(now) - 7200
        self.assertFalse(eligible(old, 11, self.cfg, now))
        nested = sample()
        nested["root"] = 88
        self.assertFalse(eligible(nested, 11, self.cfg, now))

    def test_config_validation_and_relative_paths(self):
        text = Path(Path(__file__).resolve().parents[2] / "legacy/comment_bot/config.example.toml").read_text(encoding="utf-8")
        path = Path(self.temp.name) / "config.toml"
        path.write_text(text, encoding="utf-8")
        with self.assertRaises(ValueError):
            load_config(path)
        path.write_text(text.replace("BV_REPLACE_ME", "BV17x411w7KC"), encoding="utf-8")
        cfg = load_config(path)
        self.assertTrue(Path(cfg["database"]).is_absolute())

    def test_duplicate_process_lock(self):
        with single_instance(self.cfg["database"]):
            with self.assertRaises(RuntimeError):
                with single_instance(self.cfg["database"]):
                    self.fail("不应取得第二把锁")
        with single_instance(self.cfg["database"]):
            pass


class AdapterTests(unittest.TestCase):
    def test_reject_malformed_model_output(self):
        self.assertEqual(parse_reply('{"reply":"  你好  "}', 20), "你好")
        self.assertIsNone(parse_reply('{"reply":null}', 20))
        for text in ('[]', '{}', 'hello', '{"reply":true}', '{"reply":""}',
                     '{"reply":"超过长度限制"}', '{"reply":"hi","send":true}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_reply(text, 3)

    def test_dsh_adapter_contract_without_network(self):
        captured = {}
        class Harness:
            def __init__(self, **kwargs):
                captured.update(kwargs)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def close(self):
                pass
            def run(self, prompt, session_id):
                captured["prompt"] = prompt
                return SimpleNamespace(final_response='{"reply":"你好"}', finish_reason="completed")
        with tempfile.TemporaryDirectory() as directory:
            cfg = {"dsh_home": directory, "dsh_workspace": directory, "persona": "友善",
                   "model": "test-model", "model_timeout_seconds": 10, "max_reply_chars": 20}
            with patch.dict("sys.modules", {"deepseek_harness": SimpleNamespace(DeepSeekHarness=Harness)}):
                self.assertEqual(DshResponder(cfg)._reply({"title": "标题"}, sample()), "你好")
        self.assertEqual(captured["profile"], "sdk-minimal")
        self.assertTrue(Path(captured["patches"][0]).is_file())
        self.assertIn("不可信", captured["prompt"])

    def test_bilibili_send_reply_target_and_missing_receipt(self):
        captured = {}
        async def send_comment(**kwargs):
            captured.update(kwargs)
            return {"rpid": 999}
        bili = Bilibili.__new__(Bilibili)
        bili.credential = object()
        bili.comment = SimpleNamespace(send_comment=send_comment,
                                       CommentResourceType=SimpleNamespace(VIDEO=1))
        self.assertEqual(asyncio.run(bili.send(123, 456, "你好")), "999")
        self.assertEqual((captured["oid"], captured["root"]), (123, 456))
        async def empty(**kwargs):
            return {}
        bili.comment.send_comment = empty
        with self.assertRaises(RuntimeError):
            asyncio.run(bili.send(123, 456, "你好"))


if __name__ == "__main__":
    unittest.main()
