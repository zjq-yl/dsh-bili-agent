"""后台字幕与音频任务：受限下载、可选本地转写、分析入队。"""
import asyncio
import hashlib
import importlib.util
import json
import math
import re
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .content import validate_video
from .protocol import RpcError

MAX_AUDIO_BYTES = 80 * 1024 * 1024
MAX_CACHE_BYTES = 500 * 1024 * 1024


def audio_url(value: str) -> str:
    """Accept HTTPS URLs from supported Bilibili audio CDN domains only."""
    if not isinstance(value, str):
        raise RpcError(-32030, "INVALID_AUDIO_URL")
    url = 'https:' + value if value.startswith('//') else value
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ''
        valid = (parsed.scheme == 'https' and parsed.port in (None, 443) and not parsed.username and not parsed.password
                 and any(host == d or host.endswith('.' + d) for d in ('bilivideo.com', 'bilivideo.cn')))
    except ValueError:
        valid = False
    if not valid:
        raise RpcError(-32030, "INVALID_AUDIO_URL")
    return url


async def download_audio(metadata: dict, path: Path) -> dict:
    """Download one audio track atomically, enforcing size and redirect limits."""
    import aiohttp
    url = audio_url(metadata["url"])
    temporary = path.with_suffix('.part')
    total, digest = 0, hashlib.sha256()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as session:
            async with session.get(url, allow_redirects=False, headers={
                'Referer': f'https://www.bilibili.com/video/{metadata["bvid"]}/',
                'User-Agent': 'Mozilla/5.0',
            }) as response:
                if response.status != 200:
                    raise RpcError(-32030, "AUDIO_DOWNLOAD_FAILED")
                if response.content_length and response.content_length > MAX_AUDIO_BYTES:
                    raise RpcError(-32030, "AUDIO_TOO_LARGE")
                with temporary.open('wb') as output:
                    async for chunk in response.content.iter_chunked(65536):
                        total += len(chunk)
                        if total > MAX_AUDIO_BYTES:
                            raise RpcError(-32030, "AUDIO_TOO_LARGE")
                        digest.update(chunk)
                        output.write(chunk)
        if total == 0:
            raise RpcError(-32030, "EMPTY_AUDIO")
        with temporary.open('rb') as file:
            if b'ftyp' not in file.read(64):
                raise RpcError(-32030, "INVALID_DASH_AUDIO")
        temporary.replace(path)
        return {"path": str(path), "bytes": total, "sha256": digest.hexdigest()}
    finally:
        temporary.unlink(missing_ok=True)


class Media:
    """Run one durable media job at a time and enqueue available transcripts."""

    def __init__(self, store, platform, runner, data_dir, model_path=''):
        self.store, self.platform, self.runner = store, platform, runner
        self.directory = Path(data_dir) / 'audio'
        self.model_path = model_path
        self.task = None
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS media_jobs (
                id TEXT PRIMARY KEY, state TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}', error TEXT
            );
            UPDATE media_jobs SET state='interrupted',error='WORKER_RESTARTED'
            WHERE state IN ('queued','fetching','downloading','transcribing');
        """)

    def start(self, params, *, metadata=None, priority=10):
        """Start a media job; trusted callers may reuse fetched metadata and set queue priority."""
        if self.task and not self.task.done():
            raise RpcError(-32002, 'MEDIA_BUSY')
        if (not isinstance(params.get('bvid'), str) or not re.fullmatch(r'BV[0-9A-Za-z]{10}', params['bvid'])
                or type(params.get('page', 1)) is not int or not 1 <= params.get('page', 1) <= 1000
                or params.get('mode', 'auto') not in ('auto', 'audio', 'both')
                or type(params.get('transcribe', False)) is not bool):
            raise RpcError(-32602, 'INVALID_MEDIA_REQUEST')
        if params.get('transcribe'):
            if not self.model_path or not (Path(self.model_path) / 'model.bin').is_file():
                raise RpcError(-32030, 'LOCAL_ASR_MODEL_REQUIRED')
            if importlib.util.find_spec('faster_whisper') is None:
                raise RpcError(-32030, 'INSTALL_AUDIO_REQUIREMENTS')
        job_id = uuid.uuid4().hex
        with self.store.db:
            self.store.db.execute("INSERT INTO media_jobs(id,state) VALUES(?,'queued')", (job_id,))
        self.task = asyncio.create_task(self.run(job_id, params.copy(), metadata, priority))
        return {"job_id": job_id, "state": "queued"}

    def status(self, job_id):
        if not isinstance(job_id, str):
            raise RpcError(-32602, 'INVALID_JOB_ID')
        row = self.store.db.execute('SELECT * FROM media_jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            raise RpcError(-32602, 'MEDIA_JOB_NOT_FOUND')
        return {"job_id": job_id, "state": row['state'], "result": json.loads(row['result']), "error": row['error']}

    def update(self, job_id, state, result, error=None):
        with self.store.db:
            self.store.db.execute('UPDATE media_jobs SET state=?,result=?,error=? WHERE id=?',
                                  (state, json.dumps(result, ensure_ascii=False), error, job_id))

    async def run(self, job_id, params, metadata=None, priority=10):
        result = {}
        try:
            self.update(job_id, 'fetching', result)
            mode = params.get('mode', 'auto')
            transcript = None
            if mode != 'audio':
                try:
                    transcript = await self.platform.request('fetch', params)
                    result['summary_task'] = self.runner.enqueue(transcript, source='bilibili_subtitles', priority=priority)
                except RpcError as exc:
                    if str(exc) not in ('NO_SUBTITLE_OR_LOGIN_REQUIRED', 'NO_SUBTITLE_URL'):
                        raise
                    result['subtitle_error'] = str(exc)
            if mode == 'auto' and transcript:
                self.update(job_id, 'done', result)
                return
            if mode == 'auto' and not params.get('transcribe'):
                raise RpcError(-32030, 'NO_SUBTITLE_ASR_DISABLED')
            metadata = metadata if metadata is not None else await self.platform.request('audio', params)
            duration = metadata.get('duration')
            if (type(duration) not in (int, float) or not math.isfinite(duration) or not 0 < duration <= 21600
                    or (params.get('transcribe') and duration > 3600)):
                raise RpcError(-32030, 'AUDIO_DURATION_LIMIT')
            self.directory.mkdir(parents=True, exist_ok=True)
            if sum(p.stat().st_size for p in self.directory.glob('*') if p.is_file()) + MAX_AUDIO_BYTES > MAX_CACHE_BYTES:
                raise RpcError(-32030, 'AUDIO_CACHE_FULL')
            self.update(job_id, 'downloading', result)
            path = self.directory / f'{job_id}.m4a'
            result['audio'] = {**await download_audio(metadata, path),
                               **{k: metadata[k] for k in ('bvid', 'cid', 'page', 'duration', 'track_id')}}
            self.update(job_id, 'downloaded', result)
            if params.get('transcribe'):
                self.update(job_id, 'transcribing', result)
                rows = await self.transcribe(path, job_id)
                value = validate_video({**metadata, 'subtitles': rows})
                value['subtitle_language'] = 'asr'
                value['subtitle_generated'] = True
                result['transcript_segments'] = len(value['subtitles'])
                result['transcript'] = {'path': str(self.directory / f'{job_id}.json'), 'source': 'audio_asr'}
                if transcript is None:
                    result['summary_task'] = self.runner.enqueue(value, source='audio_asr', priority=priority)
                else:
                    result['summary_basis'] = 'bilibili_subtitles'
                    result['asr_note'] = '已保存音频转写，已有字幕总结不自动覆盖'
            self.update(job_id, 'done', result)
        except asyncio.CancelledError:
            self.update(job_id, 'cancelled', result)
            raise
        except Exception as exc:
            self.update(job_id, 'failed', result, str(exc) if isinstance(exc, RpcError) else type(exc).__name__)

    async def transcribe(self, path, job_id):
        output = self.directory / f'{job_id}.json'
        process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'bili_agent.transcribe',
            '--audio', str(path), '--model', self.model_path, '--output', str(output),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(process.wait(), 900)
            if process.returncode != 0 or not output.is_file():
                raise RpcError(-32030, 'ASR_FAILED')
            if output.stat().st_size > 2 * 1024 * 1024:
                raise RpcError(-32030, 'ASR_RESULT_TOO_LARGE')
            return json.loads(output.read_text(encoding='utf-8'))
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def close(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
