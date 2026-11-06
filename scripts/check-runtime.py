"""验证基础依赖，以及按需启用的 ASR 或浏览器；不访问 B 站、不调用模型。"""
import argparse
import asyncio
import importlib.metadata
import importlib.util
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bili_agent.player import OBSERVE_SCRIPT, normalize_observation


async def browser_probe():
    from playwright.async_api import async_playwright
    local = Path(sys.prefix) / "playwright-browsers"
    if local.is_dir():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(local))
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content('<canvas width="320" height="180"></canvas><video muted width="320" height="180"></video>')
            # 浏览器内生成一段WebM，验证真实媒体解码、播放区间和截图，完全离线。
            await page.evaluate("""async () => {
                const canvas = document.querySelector('canvas'), ctx = canvas.getContext('2d');
                const stream = canvas.captureStream(15), chunks = [];
                const recorder = new MediaRecorder(stream, {mimeType: 'video/webm;codecs=vp8'});
                recorder.ondataavailable = e => chunks.push(e.data);
                const stopped = new Promise(resolve => recorder.onstop = resolve);
                recorder.start();
                const timer = setInterval(() => { ctx.fillStyle = `hsl(${Date.now()%360},80%,50%)`; ctx.fillRect(0,0,320,180); }, 50);
                await new Promise(resolve => setTimeout(resolve, 1200));
                recorder.stop(); await stopped; clearInterval(timer); stream.getTracks().forEach(t => t.stop());
                const video = document.querySelector('video');
                video.src = URL.createObjectURL(new Blob(chunks, {type:'video/webm'}));
                video.muted = true; await video.play();
            }""")
            await page.wait_for_function("document.querySelector('video').currentTime > 0.3", timeout=10000)
            video = page.locator('video')
            await video.evaluate('v => v.pause()')
            observed = normalize_observation(await video.evaluate(OBSERVE_SCRIPT))
            assert observed["played_seconds"] > 0 and observed["width"] == 320
            png = await video.screenshot()
            assert png.startswith(b'\x89PNG\r\n\x1a\n')
            return {"state": "passed", "source": "generated_local_webm", "played_seconds": observed["played_seconds"],
                    "frame_bytes": len(png), "bilibili_verified": False}
        finally:
            await browser.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--browser', action='store_true')
    parser.add_argument('--audio', action='store_true')
    args = parser.parse_args()
    report = {"python": sys.executable, "dependencies": {}}
    failed = False
    packages = [("bilibili_api", "bilibili-api-python"), ("aiohttp", "aiohttp")]
    if args.audio:
        packages.append(("faster_whisper", "faster-whisper"))
    if args.browser:
        packages.append(("playwright", "playwright"))
    for module, package in packages:
        if importlib.util.find_spec(module) is None:
            report["dependencies"][module] = "missing"
            failed = True
        else:
            report["dependencies"][module] = importlib.metadata.version(package)
    if args.browser and report["dependencies"]["playwright"] != "missing":
        try:
            report["browser"] = asyncio.run(browser_probe())
        except Exception as exc:
            report["browser"] = {"state": "failed", "error_type": type(exc).__name__,
                                  "next_step": "Install Chromium with this Python, then retry; check process launch permission."}
            failed = True
    elif args.browser:
        report["browser"] = {"state": "not_run", "reason": "playwright_missing"}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
