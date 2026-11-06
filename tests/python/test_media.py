import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bili_agent.media import Media, audio_url, download_audio
from bili_agent.protocol import RpcError
from bili_agent.runner import Runner
from bili_agent.store import Store
from .test_agent import FakeModel, sample


class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.runner = Runner(self.store, FakeModel())
        self.platform = SimpleNamespace(request=AsyncMock())
        self.media = Media(self.store, self.platform, self.runner, self.temp.name)

    async def asyncTearDown(self):
        await self.media.close()
        await self.runner.pause()
        self.store.close()
        self.temp.cleanup()

    async def test_subtitle_first_does_not_download_audio(self):
        self.platform.request.return_value = sample()
        with patch('bili_agent.media.download_audio', AsyncMock()) as download:
            job = self.media.start({'bvid': sample()['bvid']})
            await self.media.task
            result = self.media.status(job['job_id'])
            self.assertEqual(result['state'], 'done')
            self.assertIn('summary_task', result['result'])
            download.assert_not_called()

    async def test_audio_asr_enters_same_summary_pipeline_with_correct_provenance(self):
        metadata = {**sample(), 'page': 1, 'url': 'https://x.bilivideo.com/audio.m4s', 'track_id': '1'}
        self.platform.request.side_effect = [RpcError(-1, 'NO_SUBTITLE_OR_LOGIN_REQUIRED'), metadata]
        model_dir = Path(self.temp.name) / 'asr-model'
        model_dir.mkdir()
        (model_dir / 'model.bin').write_bytes(b'test model marker')
        self.media.model_path = str(model_dir)
        self.media.transcribe = AsyncMock(return_value=sample()['subtitles'])
        with patch('bili_agent.media.importlib.util.find_spec', return_value=True), patch('bili_agent.media.download_audio',
                AsyncMock(return_value={'path': 'fixture', 'bytes': 100, 'sha256': 'fixture'})):
            job = self.media.start({'bvid': sample()['bvid'], 'transcribe': True})
            await self.media.task
        result = self.media.status(job['job_id'])
        self.assertEqual(result['state'], 'done')
        video_id = result['result']['summary_task']['id']
        self.runner.start()
        async with asyncio.timeout(3):
            while self.runner.result(video_id)['state'] != 'done':
                await asyncio.sleep(.005)
        summary = self.runner.result(video_id)['result']
        self.assertEqual(summary['basis'], 'audio_transcript')
        self.assertEqual(summary['source'], 'audio_asr')

    async def test_cancel_stops_background_job_and_restart_preserves_status(self):
        started = asyncio.Event()
        async def pending(*args):
            started.set()
            await asyncio.Event().wait()
        self.platform.request.side_effect = pending
        job = self.media.start({'bvid': sample()['bvid']})
        await started.wait()
        await self.media.close()
        self.assertEqual(self.media.status(job['job_id'])['state'], 'cancelled')
        another = Media(self.store, self.platform, self.runner, self.temp.name)
        self.assertEqual(another.status(job['job_id'])['state'], 'cancelled')

    def test_audio_url_and_asr_preflight(self):
        self.assertEqual(audio_url('//x.bilivideo.com/a'), 'https://x.bilivideo.com/a')
        for url in ['file:///a', 'https://127.0.0.1/a', 'https://bilivideo.com.evil.test/a', 'https://user:pass@x.bilivideo.com/a']:
            with self.assertRaises(RpcError):
                audio_url(url)
        with self.assertRaisesRegex(RpcError, 'LOCAL_ASR_MODEL_REQUIRED'):
            self.media.start({'bvid': sample()['bvid'], 'transcribe': True})

    async def test_download_rejects_redirects_and_removes_oversized_partial_file(self):
        class Response:
            status, content_length = 200, None
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            @property
            def content(self):
                return self
            async def iter_chunked(self, size):
                yield b'0000ftyp' + b'x' * 32
        response = Response()
        class Session:
            def __init__(self, **kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def get(self, url, **kwargs):
                assert kwargs['allow_redirects'] is False
                assert 'Cookie' not in kwargs['headers']
                return response
        module = SimpleNamespace(ClientSession=Session, ClientTimeout=lambda **kwargs: None)
        metadata = {'url': 'https://x.bilivideo.com/a', 'bvid': sample()['bvid']}
        path = Path(self.temp.name) / 'audio.m4a'
        with patch.dict(sys.modules, {'aiohttp': module}), patch('bili_agent.media.MAX_AUDIO_BYTES', 16):
            with self.assertRaisesRegex(RpcError, 'AUDIO_TOO_LARGE'):
                await download_audio(metadata, path)
            self.assertFalse(path.exists())
            self.assertFalse(path.with_suffix('.part').exists())
            response.status = 302
            with self.assertRaisesRegex(RpcError, 'AUDIO_DOWNLOAD_FAILED'):
                await download_audio(metadata, path)

    async def test_cancel_transcription_kills_and_reaps_subprocess(self):
        entered, stopped = asyncio.Event(), asyncio.Event()
        class Process:
            returncode = None
            async def wait(self):
                entered.set()
                await stopped.wait()
                return self.returncode
            def kill(self):
                self.returncode = -9
                stopped.set()
        process = Process()
        with patch('bili_agent.media.asyncio.create_subprocess_exec', AsyncMock(return_value=process)):
            task = asyncio.create_task(self.media.transcribe(Path(self.temp.name) / 'audio.m4a', 'test'))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(process.returncode, -9)


if __name__ == '__main__':
    unittest.main()
