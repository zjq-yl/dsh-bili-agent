import asyncio
import json
import time

from .content import SUMMARY_SYSTEM, chunks, parse_summary, summary_json, validate_video
from .protocol import RpcError
from .memory import Memory, TOPICS
from .model import RECOVERABLE_MODEL_ERRORS


class Runner:
    def __init__(self, store, model, platform=None):
        self.store, self.model = store, model
        self.task = None
        self.state = "PAUSED"
        self.error = None
        self.platform = platform
        self.discovery = None
        self.excluded = set()
        self.last_selection = None
        self.activity = None
        self.on_complete = None
        self.assessment = None
        self.wake = asyncio.Event()
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY, bvid TEXT NOT NULL, cid TEXT NOT NULL,
                document TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
                checkpoint INTEGER NOT NULL DEFAULT 0, notes TEXT NOT NULL DEFAULT '[]',
                result TEXT, error TEXT, UNIQUE(bvid,cid)
            );
            UPDATE videos SET state='queued' WHERE state IN ('analyzing','reflecting');
            CREATE TABLE IF NOT EXISTS video_rollups (
                video_id INTEGER NOT NULL, level INTEGER NOT NULL, group_index INTEGER NOT NULL,
                note TEXT NOT NULL, PRIMARY KEY(video_id,level,group_index)
            );
        """)
        if 'error_detail' not in {row[1] for row in self.store.db.execute('PRAGMA table_info(videos)')}:
            self.store.db.execute('ALTER TABLE videos ADD COLUMN error_detail TEXT')
        if 'priority' not in {row[1] for row in self.store.db.execute('PRAGMA table_info(videos)')}:
            self.store.db.execute('ALTER TABLE videos ADD COLUMN priority INTEGER NOT NULL DEFAULT 10')
        for name, definition in [('retry_at', 'REAL NOT NULL DEFAULT 0'), ('retry_count', 'INTEGER NOT NULL DEFAULT 0')]:
            if name not in {row[1] for row in self.store.db.execute('PRAGMA table_info(videos)')}:
                self.store.db.execute(f'ALTER TABLE videos ADD COLUMN {name} {definition}')
        with self.store.db:
            self.recovered_quota_tasks = self.store.db.execute(
                "UPDATE videos SET state='queued',error=NULL,error_detail=NULL WHERE state='failed' AND error='MODEL_DAILY_LIMIT'").rowcount
        self.memory = Memory(store)

    def enqueue(self, value, source="manual_transcript", priority: int = 10):
        video = validate_video(value)
        video["source"] = source
        if source in ("bilibili_subtitles", "audio_asr"):
            video["subtitle_language"] = value.get("subtitle_language")
            video["subtitle_generated"] = value.get("subtitle_generated")
        with self.store.db:
            self.store.db.execute("INSERT OR IGNORE INTO videos(bvid,cid,document) VALUES(?,?,?)",
                                  (video["bvid"], video["cid"], json.dumps(video, ensure_ascii=False)))
            row = self.store.db.execute("SELECT id,state FROM videos WHERE bvid=? AND cid=?",
                                        (video["bvid"], video["cid"])).fetchone()
            self.store.db.execute('UPDATE videos SET priority=MIN(priority,?) WHERE id=?', (priority, row['id']))
        self.wake.set()
        return dict(row)

    def status(self):
        rows = self.store.db.execute("SELECT id,bvid,cid,state,checkpoint,error,error_detail,document,retry_at FROM videos ORDER BY id DESC LIMIT 20")
        queue = []
        for row in rows:
            item = dict(row)
            item['total_segments'] = len(list(chunks(json.loads(item.pop('document')))))
            item['error_detail'] = json.loads(item['error_detail']) if item['error_detail'] else None
            item['assessment'] = self.assessment(item['id']) if self.assessment else None
            queue.append(item)
        return {"state": self.state, "error": self.error, "mode": "transcript_analysis", "activity": self.activity,
                "actual_playback": False, "queue": queue,
                "discovery": self.discovery, "last_selection": self.last_selection}

    def result(self, video_id):
        row = self.store.db.execute("SELECT result,state,error,error_detail FROM videos WHERE id=?", (video_id,)).fetchone()
        if not row:
            raise RpcError(-32602, "VIDEO_NOT_FOUND")
        return {"state": row["state"], "error": row["error"],
                "assessment": self.assessment(video_id) if self.assessment else None,
                "error_detail": json.loads(row['error_detail']) if row['error_detail'] else None,
                "result": json.loads(row["result"]) if row["result"] else None}

    def start(self, keyword=None):
        if keyword is not None:
            if not isinstance(keyword, str) or not 1 <= len(keyword.strip()) <= 100:
                raise RpcError(-32602, "INVALID_KEYWORD")
            if not self.platform:
                raise RpcError(-32010, "PLATFORM_NOT_CONFIGURED")
            if self.task and not self.task.done():
                raise RpcError(-32002, "PAUSE_BEFORE_CHANGING_TOPIC")
            self.discovery = {"keyword": keyword.strip(), "remaining_searches": 3, "next_at": 0}
            self.excluded.clear()
        if self.task is None or self.task.done():
            self.state, self.error = "IDLE", None
            self.task = asyncio.create_task(self.run())
            self.store.event('analysis', '分析循环已启动')
        return self.status()

    async def pause(self, state="PAUSED"):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.state = state
        self.activity = None
        self.store.event('analysis', '分析循环已暂停' if state == 'PAUSED' else '分析循环已停止')
        return self.status()

    async def run(self):
        while True:
            self.wake.clear()
            row = self.store.db.execute("SELECT * FROM videos WHERE state='queued' ORDER BY priority,id LIMIT 1").fetchone()
            if row is None:
                if self.discovery and self.discovery["remaining_searches"] > 0 and time.time() >= self.discovery["next_at"]:
                    try:
                        await self.discover()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self.state = "BLOCKED"
                        self.error = str(exc) if isinstance(exc, RpcError) else type(exc).__name__
                        return
                    continue
                self.state = "IDLE"
                # 没有外部事件也会定期检查；空闲不调用模型。
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=5)
                except TimeoutError:
                    pass
                continue
            try:
                await self.analyze(row)
            except asyncio.CancelledError:
                with self.store.db:
                    self.store.db.execute("UPDATE videos SET state='queued' WHERE id=? AND state!='done'", (row["id"],))
                raise
            except Exception as exc:
                error = str(exc) if isinstance(exc, RpcError) else type(exc).__name__
                with self.store.db:
                    detail = exc.data if isinstance(exc, RpcError) else None
                    self.store.db.execute("UPDATE videos SET state='failed',error=?,error_detail=?,retry_at=0 WHERE id=?",
                                          (error, json.dumps(detail), row["id"]))
                    if error in RECOVERABLE_MODEL_ERRORS:
                        self.store.db.execute('UPDATE videos SET retry_at=?,retry_count=retry_count+1 WHERE id=?',
                            (time.time() + min(900, 60 * 2 ** min(row['retry_count'], 4)), row['id']))
                # 故障时停住，避免模型配置错误导致连续消耗整队任务。
                self.state, self.error = "BLOCKED", error
                self.activity = None
                self.store.event('analysis', '视频分析失败，已保留进度', str(row['id']), error)
                return

    async def discover(self):
        self.state = "DISCOVER"
        # 单次主题会话最多3次搜索，间隔至少60秒；暂停/继续不重置次数。
        self.discovery["remaining_searches"] -= 1
        self.discovery["next_at"] = time.time() + 60
        found = await self.platform.request("search", {"keyword": self.discovery["keyword"]})
        seen = {row[0] for row in self.store.db.execute("SELECT DISTINCT bvid FROM videos")}
        profile = self.memory.profile()
        candidates = [item for item in found["candidates"] if item["bvid"] not in seen | self.excluded
                      and not any(word.casefold() in item['title'].casefold() for word in profile['avoided_keywords'])]
        if not candidates:
            self.last_selection = {"reason": "没有新的候选视频"}
            return
        interests = self.memory.profile()["interests"]
        def score(item):
            # ponytail: 标题关键词匹配是基础选片，不将标题当作已理解的内容；后续可接标签语义匹配。
            return (sum(interest["score"] - .5 for interest in interests if interest["topic"] in item["title"])
                    + sum(.2 for word in profile['preferred_keywords'] if word.casefold() in item['title'].casefold()))
        chosen = max(candidates, key=score)
        self.last_selection = {"bvid": chosen["bvid"], "title": chosen["title"],
                               "reason": "在本次搜索内按偏好关键词和积累兴趣加权，排除避开关键词与已入队视频"}
        try:
            video = await self.platform.request("fetch", {"bvid": chosen["bvid"], "page": 1})
        except RpcError as exc:
            if str(exc) in ("NO_SUBTITLE_OR_LOGIN_REQUIRED", "NO_SUBTITLE_URL", "VIDEO_UNAVAILABLE"):
                self.excluded.add(chosen["bvid"])
                self.last_selection["skipped"] = str(exc)
                return
            raise
        self.enqueue(video, source="bilibili_subtitles")

    def retry(self, video_id):
        with self.store.db:
            count = self.store.db.execute("UPDATE videos SET state='queued',error=NULL,error_detail=NULL,retry_at=0 WHERE id=? AND state='failed'",
                                          (video_id,)).rowcount
        if not count:
            raise RpcError(-32602, "VIDEO_NOT_FAILED")
        self.wake.set()
        return {"queued": True}

    async def summarize(self, purpose: str, payload: dict, allowed: set[int], stage: str,
                        reflect: bool = False) -> dict:
        """Validate grounded output; regenerate once with field-level feedback, within model quotas."""
        system = SUMMARY_SYSTEM
        if reflect:
            system += ('\n额外输出reflection对象：{"topic":"给定分类之一","impression":"主观感受",'
                       '"valence":-1到1的数值,"evidence":[字幕整数ID]}。'
                       'topic必须来自topics；impression为1至800字符，建议200以内；'
                       'valence为有限数值；evidence为1至10个allowed_evidence_ids中的整数。'
                       '人设仅影响感受，不改变事实总结或输出格式。\n' + self.memory.persona_system())
        payload = {**payload, 'allowed_evidence_ids': sorted(allowed)}
        for attempt in range(2):
            raw = await self.model.generate(purpose, system, json.dumps(payload, ensure_ascii=False))
            try:
                result = parse_summary(raw, allowed)
                if reflect:
                    result['reflection'] = self.memory.validate_reflection(summary_json(raw).get('reflection'), allowed)
                return result
            except RpcError as exc:
                if str(exc) not in ('INVALID_SUMMARY_EVIDENCE', 'INVALID_REFLECTION'):
                    raise
                detail = {**(exc.data or {'field': 'reflection', 'reason': 'invalid_reflection'}),
                          'stage': stage, 'attempts': attempt + 1}
                if attempt:
                    raise RpcError(exc.code, str(exc), detail) from None
                payload['validation_feedback'] = detail

    async def reduce_notes(self, video_id: int, title: str, notes: list[dict]) -> list[dict]:
        """Merge at most four notes per call and persist each merge for resumable long videos."""
        level = 0
        while len(notes) > 4:
            merged = []
            for start in range(0, len(notes), 4):
                group = notes[start:start + 4]
                if len(group) == 1:
                    merged.append(group[0])
                    continue
                key = (video_id, level, start // 4)
                saved = self.store.db.execute('SELECT note FROM video_rollups WHERE video_id=? AND level=? AND group_index=?', key).fetchone()
                if saved:
                    merged.append(json.loads(saved[0]))
                    continue
                allowed = {i for note in group for fact in note['facts'] for i in fact['evidence']}
                note = await self.summarize('notes_summary', {'title': title, 'notes': group}, allowed,
                                            f'rollup:{level + 1}:{start // 4 + 1}')
                with self.store.db:
                    self.store.db.execute('INSERT INTO video_rollups VALUES(?,?,?,?)', (*key, json.dumps(note, ensure_ascii=False)))
                merged.append(note)
            notes, level = merged, level + 1
        return notes

    async def analyze(self, row):
        video, notes = json.loads(row["document"]), json.loads(row["notes"])
        batches = list(chunks(video))
        self.state = "ANALYZE"
        with self.store.db:
            self.store.db.execute("UPDATE videos SET state='analyzing' WHERE id=?", (row["id"],))
        for index in range(row["checkpoint"], len(batches)):
            batch = batches[index]
            self.activity = {'video_id': row['id'], 'bvid': video['bvid'], 'title': video['title'],
                             'message': f'正在总结第 {index + 1} / {len(batches)} 段', 'started': time.time()}
            self.store.event('analysis', self.activity['message'], video['bvid'])
            note = await self.summarize("segment_summary", {
                "title": video["title"], "subtitles": batch,
            }, {part["id"] for part in batch}, f'segment:{index + 1}')
            notes.append(note)
            with self.store.db:
                self.store.db.execute("UPDATE videos SET checkpoint=?,notes=? WHERE id=?",
                                      (index + 1, json.dumps(notes, ensure_ascii=False), row["id"]))
        self.state = "REFLECT"
        self.activity = {'video_id': row['id'], 'bvid': video['bvid'], 'title': video['title'],
                         'message': '正在合并笔记并生成最终总结', 'started': time.time()}
        self.store.event('analysis', self.activity['message'], video['bvid'])
        with self.store.db:
            self.store.db.execute("UPDATE videos SET state='reflecting' WHERE id=?", (row["id"],))
        notes = await self.reduce_notes(row['id'], video['title'], notes)
        allowed = {i for note in notes for fact in note["facts"] for i in fact["evidence"]}
        profile = self.memory.profile()
        result = await self.summarize('video_summary', {'title': video['title'], 'notes': notes,
            'profile': {k: v for k, v in profile.items() if k != 'persona'}, 'topics': TOPICS}, allowed, 'final', reflect=True)
        reflection = result['reflection']
        result.update({"bvid": video["bvid"], "cid": video["cid"], "title": video["title"],
                       'duration': video['duration'], 'author': video.get('author', ''), 'author_uid': video.get('author_uid', ''),
                       "basis": "audio_transcript" if video["source"] == "audio_asr" else "subtitles_only", "source": video["source"], "actual_playback": False,
                       "page": video["page"], "subtitle_language": video.get("subtitle_language"),
                       "subtitle_generated": video.get("subtitle_generated"),
                       "reflection": reflection,
                       "limitations": (["机器音频转写可能有误", "未分析画面、音色或非语言声音"] if video["source"] == "audio_asr"
                                       else ["基于字幕，未分析音频", "字幕可能缺漏，未分析画面"]),
                       "evidence": [{**part, "url": f'https://www.bilibili.com/video/{video["bvid"]}/?p={video["page"]}&t={int(part["from"])}'}
                                    for part in video["subtitles"] if part["id"] in ({i for fact in result["facts"] for i in fact["evidence"]} | set(reflection["evidence"]))]})
        with self.store.db:
            self.store.db.execute("UPDATE videos SET state='done',error=NULL,error_detail=NULL,result=? WHERE id=?",
                                  (json.dumps(result, ensure_ascii=False), row["id"]))
            self.memory.record(row["id"], video["bvid"], reflection)
        if self.on_complete:
            self.on_complete()
        self.store.event('analysis', '视频总结与记忆已保存', video['bvid'])
        self.activity = None
