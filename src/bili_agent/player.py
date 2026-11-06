"""有限的播放器命令与媒体证据；模型不能注入 JS、任意 URL 或输出路径。"""
import asyncio
import importlib.util
import math
import os
import re
import sys
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .platform import read_credentials
from .protocol import RpcError

VIDEO_SELECTOR = '#bilibili-player video, .bpx-player-video-wrap video, video'
OBSERVE_SCRIPT = """v => ({
    current_time: v.currentTime, duration: Number.isFinite(v.duration) ? v.duration : null,
    paused: v.paused, ended: v.ended, seeking: v.seeking, ready_state: v.readyState,
    playback_rate: v.playbackRate, width: v.videoWidth, height: v.videoHeight,
    played: Array.from({length: v.played.length}, (_, i) => [v.played.start(i), v.played.end(i)])
})"""


def normalize_observation(value):
    if not isinstance(value, dict):
        raise RpcError(-32020, "INVALID_PLAYER_OBSERVATION")
    current, duration = value.get("current_time"), value.get("duration")
    if type(current) not in (int, float) or not math.isfinite(current) or current < 0:
        raise RpcError(-32020, "INVALID_PLAYER_OBSERVATION")
    if duration is not None and (type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0):
        raise RpcError(-32020, "INVALID_PLAYER_OBSERVATION")
    ranges = value.get("played")
    if not isinstance(ranges, list) or len(ranges) > 1000:
        raise RpcError(-32020, "INVALID_PLAYED_RANGES")
    merged = []
    for pair in ranges:
        if (not isinstance(pair, list) or len(pair) != 2
                or any(type(x) not in (int, float) or not math.isfinite(x) for x in pair)
                or not 0 <= pair[0] <= pair[1] or (duration is not None and pair[1] > duration + .1)):
            raise RpcError(-32020, "INVALID_PLAYED_RANGES")
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return {**value, "played": merged, "played_seconds": sum(end - start for start, end in merged),
            "evidence_basis": "browser_media_element", "content_understood": False}


class Player:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.playwright = None
        self.context = None
        self.page = None
        self.target = None
        self.lock = asyncio.Lock()
        self.last_status = "not_started"

    def status(self):
        return {"state": self.last_status, "dependency_installed": importlib.util.find_spec("playwright") is not None,
                "opened": self.page is not None, "target": self.target, "vision_connected": False, "asr_connected": False}

    async def command(self, action, params):
        if action not in ("open", "observe", "play", "pause", "seek", "frame", "close", "status"):
            raise RpcError(-32602, "INVALID_PLAYER_ACTION")
        if action == "status":
            return self.status()
        if self.lock.locked():
            raise RpcError(-32002, "PLAYER_BUSY")
        async with self.lock:
            try:
                async with asyncio.timeout(45):
                    if action == "close":
                        await self.close()
                        return self.status()
                    if action == "open":
                        return await self.open(params)
                    self.ensure_target()
                    video = self.page.locator(VIDEO_SELECTOR).first
                    if action == "play":
                        # muted可避免常规自动播放限制，不绕过登录、付费或平台验证。
                        await video.evaluate("async v => { v.muted = true; await v.play(); }")
                    elif action == "pause":
                        await video.evaluate("v => v.pause()")
                    elif action == "seek":
                        seconds = params.get("seconds")
                        observed = await self.observe()
                        if (type(seconds) not in (int, float) or not math.isfinite(seconds)
                                or observed["duration"] is None or not 0 <= seconds <= observed["duration"]):
                            raise RpcError(-32602, "INVALID_SEEK_TIME")
                        await video.evaluate("(v, seconds) => { v.currentTime = seconds; }", seconds)
                    elif action == "frame":
                        await video.evaluate("v => v.pause()")
                        observed = await self.observe()
                        if observed.get("ready_state", 0) < 2 or not observed.get("width") or observed.get("seeking"):
                            raise RpcError(-32020, "FRAME_NOT_READY")
                        cache = self.data_dir / "frames"
                        cache.mkdir(parents=True, exist_ok=True)
                        if len(list(cache.glob("*.png"))) >= 100:
                            raise RpcError(-32020, "FRAME_CACHE_FULL")
                        path = cache / f"{uuid.uuid4().hex}.png"
                        await video.screenshot(path=str(path), timeout=10000)
                        self.ensure_target()
                        return {"path": str(path), "target": self.target, "at_seconds": observed["current_time"],
                                "recognized": False, "basis": "paused_video_element_screenshot"}
                    return await self.observe()
            except asyncio.CancelledError:
                # 取消打开/播放等操作后关闭独占上下文，避免留下不可控播放。
                await self.close()
                raise
            except ImportError:
                self.last_status = "dependencies_missing"
                raise RpcError(-32020, "INSTALL_PLAYWRIGHT") from None
            except RpcError:
                if action == "open":
                    await self.close()
                raise
            except TimeoutError:
                await self.close()
                self.last_status = "timeout"
                raise RpcError(-32020, "PLAYER_TIMEOUT") from None
            except Exception as exc:
                await self.close()
                self.last_status = "browser_missing" if "Executable doesn't exist" in str(exc) else "failed"
                raise RpcError(-32020, "INSTALL_CHROMIUM" if self.last_status == "browser_missing" else "PLAYER_OPERATION_FAILED") from None

    def ensure_target(self):
        if self.page is None or self.page.is_closed() or not self.target:
            raise RpcError(-32020, "PLAYER_NOT_OPEN")
        url = urlsplit(self.page.url)
        page = parse_qs(url.query).get("p", ["1"])[0]
        if (url.hostname != "www.bilibili.com" or url.path.rstrip("/") != f'/video/{self.target["bvid"]}'
                or page != str(self.target["page"])):
            raise RpcError(-32020, "PLAYER_TARGET_CHANGED")

    async def open(self, params):
        bvid, page, visible = params.get("bvid"), params.get("page", 1), params.get("visible", False)
        if (not isinstance(bvid, str) or not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid)
                or type(page) is not int or not 1 <= page <= 1000 or type(visible) is not bool):
            raise RpcError(-32602, "INVALID_PLAYER_TARGET")
        await self.close()
        from playwright.async_api import async_playwright
        local_browsers = Path(sys.prefix) / "playwright-browsers"
        if local_browsers.is_dir():
            os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(local_browsers))
        self.playwright = await async_playwright().start()
        self.context = await self.playwright.chromium.launch_persistent_context(
            str(self.data_dir / "browser-profile"), headless=not visible,
            viewport={"width": 1280, "height": 800}, accept_downloads=False,
        )
        credentials = read_credentials(self.data_dir)
        names = {"sessdata": "SESSDATA", "buvid3": "buvid3", "bili_jct": "bili_jct", "dedeuserid": "DedeUserID"}
        cookies = [{"name": names[key], "value": value, "domain": ".bilibili.com", "path": "/", "secure": True}
                   for key, value in credentials.items() if key in names and value]
        if cookies:
            await self.context.add_cookies(cookies)
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        self.page.set_default_timeout(10000)
        self.target = {"bvid": bvid, "page": page}
        await self.page.goto(f"https://www.bilibili.com/video/{bvid}/?p={page}", wait_until="domcontentloaded", timeout=30000)
        video = self.page.locator(VIDEO_SELECTOR).first
        await video.wait_for(state="attached")
        await video.evaluate("v => { v.pause(); v.muted = true; }")
        self.last_status = "opened"
        return await self.observe()

    async def observe(self):
        self.ensure_target()
        observed = normalize_observation(await self.page.locator(VIDEO_SELECTOR).first.evaluate(OBSERVE_SCRIPT))
        self.last_status = "ended" if observed["ended"] else "paused" if observed["paused"] else "playing"
        return {**observed, "target": self.target, "state": self.last_status}

    async def close(self):
        context, playwright = self.context, self.playwright
        self.context = self.playwright = self.page = self.target = None
        try:
            if context:
                await asyncio.wait_for(context.close(), 2)
        finally:
            if playwright:
                await asyncio.wait_for(playwright.stop(), 2)
            self.last_status = "closed"
