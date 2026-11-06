"""Interaction Agent: private chat, recommendation delivery and public interaction."""
import asyncio
import json
import time
import sqlite3

from .protocol import RpcError
from .comments import Comments
from .mentions import Mentions


class InteractionAgent:
    """Consume durable video results independently of video discovery and analysis."""

    def __init__(self, video):
        self.video = video
        self.db, self.store = video.db, video.store
        self.dm, self.social, self.runner = video.dm, video.social, video.runner
        self.platform, self.model = video.platform, video.model
        self.wake = asyncio.Event()
        self.task, self.error, self.activity = None, None, None
        self.comments = Comments(self)
        self.mentions = Mentions(self)

    @property
    def config(self) -> dict:
        return self.video.config

    def job_allowed(self, job: sqlite3.Row) -> bool:
        return self.video.job_allowed(job)

    def assessment(self, video_id: int) -> dict | None:
        return self.video.assessment(video_id)

    def step(self, message: str, reference: str = '') -> None:
        self.activity = {'message': message, 'reference': str(reference), 'started': time.time()}
        self.store.event('interaction', message, str(reference))

    def status(self) -> dict:
        return {'name': '聊天 Agent', 'state': 'RUNNING' if self.task and not self.task.done() else 'STOPPED',
                'error': self.error, 'activity': self.activity, 'chat': self.dm.state,
                'proactive_video_push': False, 'comments': self.comments.status(), 'mentions': self.mentions.status(),
                'comments_enabled': self.config['comments_enabled'], 'comment_daily_limit': self.config['comment_daily_limit']}

    def prepare_results(self) -> None:
        """Replay idempotent handoffs; a restart never loses a completed video result."""
        for row in self.db.execute("""SELECT v.*,e.created,d.value FROM video_decisions d JOIN videos v ON v.id=d.video_id
            JOIN experiences e ON e.video_id=v.id WHERE d.account=? AND v.state='done' ORDER BY v.id""", (self.dm.account,)).fetchall():
            decision, result = json.loads(row['value']), json.loads(row['result'])
            self.accept_video(row, decision, result)
        for job in self.db.execute("SELECT * FROM browse_jobs WHERE account=? AND state='result_ready'", (self.dm.account,)).fetchall():
            self.accept_request(job)

    def accept_video(self, row: sqlite3.Row, decision: dict, result: dict) -> None:
        """Consume video evidence for public interaction, never for unsolicited video pushes."""
        self.comments.queue(row, decision)
        with self.db:
            for action in ('like', 'dislike', 'favorite'):
                if decision[action] and self.config['actions_enabled']:
                    self.db.execute('INSERT OR IGNORE INTO video_actions(account,bvid,action,reason) VALUES(?,?,?,?)',
                                     (self.dm.account, row['bvid'], action, decision['reason']))

    def accept_request(self, job: sqlite3.Row) -> bool:
        """Turn a durable Video Agent result into the requesting friend's reply."""
        rows = self.db.execute('''SELECT v.id,v.result FROM browse_items i JOIN videos v ON v.id=i.video_id
            WHERE i.job_id=? AND v.state='done' ORDER BY v.id''', (job['id'],)).fetchall()
        videos = {row['id']: json.loads(row['result']) for row in rows
                  if (self.assessment(row['id']) or {}).get('rating') in ('值得看', '可以看')
                  and self.social.matches(job['peer'], json.loads(row['result']), json.loads(job['requirements']).get('max_seconds'))}
        selection = json.loads(job['selection'] or '[]')
        if not self.job_allowed(job):
            return False  # Scope or privacy may have changed while waiting for the model.
        selection = [choice for choice in selection if choice['video_id'] in videos
                     and self.social.can_recommend(job['peer'], videos[choice['video_id']],
                         json.loads(job['requirements']).get('max_seconds'), request_id=job['id'])]
        if not selection:
            failed = self.db.execute('''SELECT 1 FROM browse_items i LEFT JOIN videos v ON v.id=i.video_id
                WHERE i.job_id=? AND (i.error IS NOT NULL OR v.state='failed') LIMIT 1''', (job['id'],)).fetchone()
            message = ('这次没有取得足够可用的分析结果，部分视频的字幕获取或分析失败，暂时不能给出可靠推荐。可以在面板查看原因并重试。'
                       if failed else '这次没有找到已完成分析、适合且尚未给你推荐过的新视频。之前推荐或等待发送确认的内容已排除，你可以换一个题材，我再找找。')
            draft = self.dm.compose(job['peer'], message, f"request-empty-{job['id']}")
            with self.db:
                self.db.execute('INSERT OR IGNORE INTO request_deliveries(job_id,draft_id) VALUES(?,?)', (job['id'], draft['id']))
        for choice in selection:
            if choice['video_id'] not in videos:
                continue  # Preferences can change after selection was saved.
            draft_id = self.social.recommend(job['peer'], choice['video_id'], videos[choice['video_id']], choice['reason'],
                                             choice['share'] and self.config['share_cards'], request_id=job['id'])
            with self.db:
                self.db.execute('INSERT OR IGNORE INTO request_deliveries(job_id,draft_id) VALUES(?,?)', (job['id'], draft_id))
        with self.db:
            self.db.execute("UPDATE browse_jobs SET state='delivering',error=NULL WHERE id=?", (job['id'],))
        self.store.event('request', f'推荐结果已生成，等待发送，共 {len(selection)} 个视频', str(job['id']))
        return True

    def notify_waiting(self, job, error: str) -> None:
        """Send one durable progress reply per request/error/day, without claiming completion."""
        if job['kind'] != 'request' or not self.job_allowed(job):
            return
        message = '找视频的请求暂时遇到了处理问题，尚未完成推荐。进度已经保留，可以在控制面板查看具体原因。'
        key = f"request-wait-{job['id']}-{int(time.time() // 86400)}-{error[:30]}"
        draft = self.dm.compose(job['peer'], message, key)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO request_deliveries(job_id,draft_id,role) VALUES(?,?,'progress')", (job['id'], draft['id']))
            self.db.execute('UPDATE browse_jobs SET error=? WHERE id=?', (error, job['id']))

    def update_deliveries(self) -> None:
        """A generated result is complete only after every result text has a receipt."""
        for job in self.db.execute("SELECT id FROM browse_jobs WHERE account=? AND state='delivering'", (self.dm.account,)).fetchall():
            deliveries = list(self.db.execute("SELECT o.state,o.error FROM request_deliveries d JOIN dm_outbox o ON o.id=d.draft_id WHERE d.job_id=? AND d.role='result'", (job['id'],)))
            states = [row['state'] for row in deliveries]
            if states and all(row['state'] == 'sent' or (row['state'] == 'cancelled' and row['error'] == 'DUPLICATE_RECOMMENDATION') for row in deliveries):
                with self.db:
                    self.db.execute("UPDATE browse_jobs SET state='complete',error=NULL WHERE id=?", (job['id'],))
                self.store.event('request', '推荐结果已处理，重复视频已跳过' if 'cancelled' in states else '推荐回复已全部确认发送', str(job['id']))
            elif any(state in ('uncertain', 'cancelled') for state in states):
                with self.db:
                    self.db.execute("UPDATE browse_jobs SET error='DM_SEND_UNCERTAIN' WHERE id=?", (job['id'],))

    async def send_pending(self) -> None:
        if not self.dm.send_enabled:
            return
        rows = self.db.execute('''SELECT o.* FROM dm_outbox o WHERE o.account=? AND o.state='draft'
            AND EXISTS(SELECT 1 FROM request_deliveries d WHERE d.draft_id=o.id OR d.draft_id=o.depends_on)
            ORDER BY EXISTS(SELECT 1 FROM request_deliveries d WHERE d.draft_id=o.id OR d.draft_id=o.depends_on) DESC,o.created LIMIT 20''', (self.dm.account,)).fetchall()
        for row in rows:
            if row['peer'] not in self.dm.allowed_peers:
                continue
            job = self.db.execute('''SELECT j.* FROM browse_jobs j JOIN request_deliveries d ON d.job_id=j.id
                WHERE d.draft_id=? OR d.draft_id=?''', (row['id'], row['depends_on'])).fetchone()
            if not job or not self.job_allowed(job):
                continue
            if row['card_bvid'] and not self.config['share_cards']:
                continue
            if row['depends_on']:
                parent = self.db.execute('SELECT state FROM dm_outbox WHERE id=?', (row['depends_on'],)).fetchone()
                if not parent or parent['state'] != 'sent':
                    if not parent or parent['state'] in ('uncertain', 'cancelled'):
                        with self.db:
                            self.db.execute("UPDATE dm_outbox SET state='cancelled',error='CARD_PARENT_NOT_CONFIRMED' WHERE id=?", (row['id'],))
                    continue
            try:
                await self.dm.publish(row['id'])
            except RpcError as exc:
                if str(exc) == 'RECOMMENDATION_CARD_EXPIRED':
                    with self.db:
                        self.db.execute("UPDATE dm_outbox SET state='cancelled',error=? WHERE id=?", (str(exc), row['id']))
                    continue
                raise
            self.update_deliveries()
            break

    async def perform_action(self) -> None:
        if not self.config['actions_enabled']:
            return
        row = self.db.execute("SELECT * FROM video_actions WHERE account=? AND state='planned' ORDER BY rowid LIMIT 1", (self.dm.account,)).fetchone()
        if not row:
            return
        now = time.time()
        attempts = self.db.execute('SELECT COUNT(*),MAX(attempted) FROM video_actions WHERE account=? AND attempted>=?',
                                   (self.dm.account, int(now // 86400) * 86400)).fetchone()
        if attempts[0] >= 30 or (attempts[1] and now - attempts[1] < 60):
            return
        await self.dm.bind()  # Verify the current login still belongs to this data directory.
        key = (self.dm.account, row['bvid'], row['action'])
        with self.db:
            self.db.execute("UPDATE video_actions SET state='sending',attempted=? WHERE account=? AND bvid=? AND action=?", (now, *key))
        try:
            result = await self.platform.request('video_action', {'account_uid': self.dm.account, 'bvid': row['bvid'],
                'action': row['action'], 'folder_id': self.config['favorite_folder']})
            if result.get('accepted') is not True:
                raise RpcError(-32040, 'ACTION_RECEIPT_MISSING')
            state, error = 'sent', None
        except asyncio.CancelledError:
            with self.db:
                self.db.execute("UPDATE video_actions SET state='uncertain',error='CANCELLED_AFTER_ACTION_START' WHERE account=? AND bvid=? AND action=?", key)
            raise
        except Exception as exc:
            error = str(exc) if isinstance(exc, RpcError) else type(exc).__name__
            state = 'local_only' if error == 'DISLIKE_REQUIRES_APP_TOKEN' else 'blocked' if error == 'FAVORITE_FOLDER_REQUIRED' else 'uncertain'
        with self.db:
            self.db.execute('UPDATE video_actions SET state=?,error=? WHERE account=? AND bvid=? AND action=?', (state, error, *key))

    async def tick(self) -> None:
        if not self.dm.account:
            return
        self.prepare_results()
        self.update_deliveries()
        self.error = None
        for action in (self.send_pending, self.perform_action, self.mentions.tick, self.comments.tick):
            try:
                await action()
            except RpcError as exc:
                self.error = self.dm.safe_error(exc)
                self.video.delivery_error = self.error
        self.update_deliveries()

    def start(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())

    async def run(self) -> None:
        while True:
            self.wake.clear()
            try:
                if (self.video.data_dir / 'credentials.json').is_file():
                    if self.config['dm_auto_start'] and not self.video.dm_paused and not self.video.start_attempted:
                        await self.dm.start()
                        self.video.start_attempted = True
                    elif not self.dm.account:
                        await self.dm.bind()
                    await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error = self.dm.safe_error(exc)
                self.store.event('interaction', '互动任务等待处理，闲聊接收独立运行', error=self.error)
            finally:
                self.activity = None
            try:
                await asyncio.wait_for(self.wake.wait(), 15 if self.error else 5)
            except TimeoutError:
                pass

    async def close(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
