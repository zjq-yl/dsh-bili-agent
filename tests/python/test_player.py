import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bili_agent.player import Player, OBSERVE_SCRIPT, normalize_observation
from bili_agent.protocol import RpcError


def observation():
    return {"current_time": 5, "duration": 100, "paused": True, "ended": False,
            "seeking": False, "ready_state": 4, "playback_rate": 1, "width": 640, "height": 360,
            "played": [[0, 5]]}


class FakeVideo:
    def __init__(self):
        self.first = self
        self.value = observation()
        self.screenshot = AsyncMock()

    async def evaluate(self, script, arg=None):
        if script == OBSERVE_SCRIPT:
            return self.value.copy()
        if 'v.currentTime = seconds' in script:
            self.value["current_time"] = arg
        elif 'await v.play()' in script:
            self.value["paused"] = False
        elif 'v.pause()' in script:
            self.value["paused"] = True


class PlayerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.player = Player(self.temp.name)
        self.video = FakeVideo()
        self.player.page = SimpleNamespace(is_closed=lambda: False,
            url="https://www.bilibili.com/video/BV17x411w7KC/?p=2", locator=lambda _: self.video)
        self.player.target = {"bvid": "BV17x411w7KC", "page": 2}
        self.context = self.player.context = SimpleNamespace(close=AsyncMock())
        self.driver = self.player.playwright = SimpleNamespace(stop=AsyncMock())

    async def asyncTearDown(self):
        await self.player.close()
        self.temp.cleanup()

    async def test_seek_does_not_count_as_playback_and_frames_are_not_recognition(self):
        result = await self.player.command("seek", {"seconds": 99})
        self.assertEqual(result["current_time"], 99)
        self.assertEqual(result["played_seconds"], 5)
        self.assertFalse(result["content_understood"])
        playing = await self.player.command("play", {})
        self.assertEqual(playing["state"], "playing")
        frame = await self.player.command("frame", {})
        self.assertEqual(frame["at_seconds"], 99)
        self.assertFalse(frame["recognized"])
        self.assertTrue(self.video.value["paused"])
        self.assertTrue(Path(frame["path"]).is_relative_to(Path(self.temp.name)))
        self.video.screenshot.assert_awaited_once()

    async def test_target_change_and_invalid_time_are_rejected(self):
        for seconds in [-1, 101, float("nan"), "1"]:
            with self.assertRaisesRegex(RpcError, "INVALID_SEEK_TIME"):
                await self.player.command("seek", {"seconds": seconds})
        self.player.page.url = "https://www.bilibili.com/video/BV17x411w7KC/?p=1"
        with self.assertRaisesRegex(RpcError, "PLAYER_TARGET_CHANGED"):
            await self.player.command("frame", {})
        self.video.screenshot.assert_not_called()

    async def test_cancel_releases_context_and_driver(self):
        entered = asyncio.Event()
        async def wait_forever(*args):
            entered.set()
            await asyncio.Event().wait()
        self.video.evaluate = wait_forever
        task = asyncio.create_task(self.player.command("play", {}))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.context.close.assert_awaited_once()
        self.driver.stop.assert_awaited_once()
        self.assertIsNone(self.player.page)

    async def test_missing_dependency_and_missing_browser_are_distinct(self):
        with patch.dict(sys.modules, {"playwright": None}):
            with self.assertRaisesRegex(RpcError, "INSTALL_PLAYWRIGHT"):
                await self.player.command("open", {"bvid": "BV17x411w7KC"})
        driver = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=AsyncMock(
            side_effect=RuntimeError("Executable doesn't exist"))), stop=AsyncMock())
        module = SimpleNamespace(async_playwright=lambda: SimpleNamespace(start=AsyncMock(return_value=driver)))
        with patch.dict(sys.modules, {"playwright": SimpleNamespace(), "playwright.async_api": module}):
            with self.assertRaisesRegex(RpcError, "INSTALL_CHROMIUM"):
                await self.player.command("open", {"bvid": "BV17x411w7KC"})
        driver.stop.assert_awaited_once()

    def test_played_ranges_are_merged_not_double_counted(self):
        value = observation()
        value["played"] = [[7, 9], [0, 5], [4, 8]]
        self.assertEqual(normalize_observation(value)["played_seconds"], 9)
        value["played"] = [[0, 101]]
        with self.assertRaisesRegex(RpcError, "INVALID_PLAYED_RANGES"):
            normalize_observation(value)


if __name__ == "__main__":
    unittest.main()
