"""好友档案与视频完成事件：明确兴趣匹配、持久去重、受控私信推荐。"""
import asyncio
import json
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from .messaging import uid
from .protocol import RpcError
from .connections import FriendConnections

LOCAL_TIME = timezone(timedelta(hours=8))
MAX_PROACTIVE_VIDEOS = 5
PREFERENCES = {'mode': 'learning', 'level': '', 'goal': '', 'max_seconds': 0,
               'daily_limit': MAX_PROACTIVE_VIDEOS, 'push_start': '09:00', 'push_end': '22:00',
               'paused_until': 0, 'blocked_keywords': [], 'blocked_authors': []}
FEEDBACK = {'like': '喜欢', 'too_basic': '太基础', 'too_hard': '太难', 'too_long': '太长',
            'not_interested': '不感兴趣', 'seen': '看过了', 'block_author': '不再推荐这个UP'}


def clock_minutes(value: str) -> int:
    """Parse a 24-hour local clock for recommendation delivery windows."""
    if not isinstance(value, str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', value):
        raise RpcError(-32602, 'INVALID_PUSH_TIME')
    hour, minute = map(int, value.split(':'))
    return hour * 60 + minute


class Social:
    """Keep account-scoped friend preferences and deliver matching video recommendations."""

    def __init__(self, store, messaging):
        self.db, self.messaging = store.db, messaging
        self.task = None
        self.wake = asyncio.Event()
        self.error = None
        self.model_driven = False
        self.proactive_enabled = True
        self.share_cards = True
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS friends(
                account TEXT NOT NULL, peer TEXT NOT NULL, name TEXT NOT NULL,
                category TEXT NOT NULL, interests TEXT NOT NULL, style TEXT NOT NULL,
                recommend INTEGER NOT NULL, auto_send INTEGER NOT NULL, created REAL NOT NULL,
                PRIMARY KEY(account,peer)
            );
            CREATE TABLE IF NOT EXISTS recommendations(
                account TEXT NOT NULL, peer TEXT NOT NULL, bvid TEXT NOT NULL,
                video_id INTEGER NOT NULL, reason TEXT NOT NULL, draft_id TEXT,
                created REAL NOT NULL, PRIMARY KEY(account,peer,bvid)
            );
            CREATE TABLE IF NOT EXISTS friend_preferences(
                account TEXT NOT NULL,peer TEXT NOT NULL,value TEXT NOT NULL,PRIMARY KEY(account,peer));
            CREATE TABLE IF NOT EXISTS recommendation_feedback(
                account TEXT NOT NULL,peer TEXT NOT NULL,bvid TEXT NOT NULL,kind TEXT NOT NULL,
                created REAL NOT NULL,PRIMARY KEY(account,peer,bvid,kind));
        """)
        if 'recommendation_bvid' not in {row[1] for row in self.db.execute('PRAGMA table_info(dm_outbox)')}:
            with self.db:
                self.db.execute('ALTER TABLE dm_outbox ADD COLUMN recommendation_bvid TEXT')
                # Recover structured video identities from legacy command keys, not chat prose.
                for row in self.db.execute("SELECT id,command_id FROM dm_outbox WHERE card_bvid IS NULL AND command_id IS NOT NULL").fetchall():
                    command = row['command_id']
                    proactive = re.fullmatch(r'rec-(BV[0-9A-Za-z]{10})-[0-9]+', command)
                    requested = re.fullmatch(r'req-[0-9]+-([0-9]+)-[0-9]+', command)
                    video = self.db.execute('SELECT bvid FROM videos WHERE id=?', (int(requested[1]),)).fetchone() if requested else None
                    bvid = proactive[1] if proactive else video[0] if video else None
                    if bvid:
                        self.db.execute('UPDATE dm_outbox SET recommendation_bvid=? WHERE id=?', (bvid, row['id']))
        self.db.execute('CREATE INDEX IF NOT EXISTS dm_recommendation_history ON dm_outbox(account,peer,recommendation_bvid)')
        self.connections = FriendConnections(self)

    def previous_recommendation(self, peer: str, bvid: str) -> sqlite3.Row | None:
        """Find the durable delivery or reservation shared by all recommendation routes."""
        return self.db.execute("""SELECT * FROM dm_outbox WHERE account=? AND peer=? AND recommendation_bvid=?
            AND state IN ('draft','sending','sent','uncertain')
            ORDER BY CASE WHEN state='draft' THEN 1 ELSE 0 END,created,rowid LIMIT 1""",
            (self.messaging.account, peer, bvid)).fetchone()

    def can_recommend(self, peer: str, video: dict, max_seconds: int | None = None, request_id: int | None = None) -> bool:
        """Select only suitable videos not already sent, reserved or awaiting receipt verification."""
        previous = self.previous_recommendation(peer, video.get('bvid', ''))
        own_request = previous and request_id is not None and (previous['command_id'] or '').startswith(f'req-{request_id}-')
        return self.matches(peer, video, max_seconds) and (previous is None or bool(own_request))

    def cancel_duplicate(self, draft: sqlite3.Row) -> bool:
        """Recheck legacy queued text under the shared send lock; never replay an uncertain send."""
        parent = self.db.execute('SELECT * FROM dm_outbox WHERE account=? AND id=?',
                                (self.messaging.account, draft['depends_on'])).fetchone() if draft['depends_on'] else draft
        if not parent or not parent['recommendation_bvid']:
            return False
        previous = self.previous_recommendation(draft['peer'], parent['recommendation_bvid'])
        if not previous or previous['id'] == parent['id']:
            return False
        with self.db:
            self.db.execute("UPDATE dm_outbox SET state='cancelled',error='DUPLICATE_RECOMMENDATION' WHERE state='draft' AND (id=? OR depends_on=?)",
                            (draft['id'], draft['id']))
        self.messaging.store.event('recommendation', '已拦截重复推荐', parent['recommendation_bvid'], 'DUPLICATE_RECOMMENDATION')
        return True

    def preferences(self, peer: str) -> dict:
        """Read recipient-owned preferences independently from delivery permissions."""
        row = self.db.execute('SELECT value FROM friend_preferences WHERE account=? AND peer=?',
                              (self.messaging.account, peer)).fetchone()
        prefs = {**PREFERENCES, **(json.loads(row[0]) if row else {})}
        prefs['daily_limit'] = min(MAX_PROACTIVE_VIDEOS, prefs['daily_limit'])
        return prefs

    def save_preferences(self, peer: str, value: dict) -> dict:
        """Update explicit interests/pace without granting any sending permission."""
        account, peer = self.messaging.require_account(), uid(peer)
        if not isinstance(value, dict) or set(value) - set(PREFERENCES):
            raise RpcError(-32602, 'INVALID_RECOMMENDATION_PREFERENCES')
        prefs = {**self.preferences(peer), **value}
        if (prefs['mode'] not in ('learning', 'leisure')
                or any(not isinstance(prefs[k], str) or len(prefs[k]) > n for k, n in [('level', 100), ('goal', 500)])
                or type(prefs['max_seconds']) is not int or not 0 <= prefs['max_seconds'] <= 21600
                or type(prefs['daily_limit']) is not int or not 0 <= prefs['daily_limit'] <= MAX_PROACTIVE_VIDEOS
                or type(prefs['paused_until']) not in (int, float) or not 0 <= prefs['paused_until'] <= 4102444800):
            raise RpcError(-32602, 'INVALID_RECOMMENDATION_PREFERENCES')
        clock_minutes(prefs['push_start']); clock_minutes(prefs['push_end'])
        for key in ('blocked_keywords', 'blocked_authors'):
            values = prefs[key]
            if not isinstance(values, list) or len(values) > 30 or any(not isinstance(s, str) or not 1 <= len(s.strip()) <= 100 for s in values):
                raise RpcError(-32602, 'INVALID_RECOMMENDATION_PREFERENCES')
            prefs[key] = list(dict.fromkeys(s.strip() for s in values))
        with self.db:
            self.db.execute('INSERT INTO friend_preferences VALUES(?,?,?) ON CONFLICT(account,peer) DO UPDATE SET value=excluded.value',
                            (account, peer, json.dumps(prefs, ensure_ascii=False)))
        self.wake.set()
        return prefs

    def feedback(self, peer: str, bvid: str, kind: str) -> dict:
        """Record explicit feedback only for a video actually delivered to this recipient."""
        peer = uid(peer)
        if not isinstance(kind, str) or kind not in FEEDBACK or not isinstance(bvid, str) or not re.fullmatch(r'BV[0-9A-Za-z]{10}', bvid):
            raise RpcError(-32602, 'INVALID_RECOMMENDATION_FEEDBACK')
        if not self.db.execute("SELECT 1 FROM dm_outbox WHERE account=? AND peer=? AND state='sent' AND instr(text,?)>0", (self.messaging.require_account(), peer, bvid)).fetchone():
            raise RpcError(-32602, 'RECOMMENDATION_NOT_DELIVERED')
        row = self.db.execute("SELECT result,document FROM videos WHERE bvid=? AND state='done' ORDER BY id DESC LIMIT 1", (bvid,)).fetchone()
        if not row:
            raise RpcError(-32602, 'VIDEO_NOT_ANALYZED')
        document = json.loads(row['document'])
        if kind == 'block_author' and not document.get('author_uid'):
            raise RpcError(-32602, 'VIDEO_AUTHOR_UNKNOWN')
        with self.db:
            opposite = {'like': 'not_interested', 'not_interested': 'like', 'too_basic': 'too_hard', 'too_hard': 'too_basic'}.get(kind)
            if opposite:
                self.db.execute('DELETE FROM recommendation_feedback WHERE account=? AND peer=? AND bvid=? AND kind=?', (self.messaging.account, peer, bvid, opposite))
            inserted = self.db.execute('INSERT OR IGNORE INTO recommendation_feedback VALUES(?,?,?,?,?)',
                (self.messaging.account, peer, bvid, kind, time.time())).rowcount
        if inserted:
            prefs = self.preferences(peer)
            if kind == 'too_long':
                self.save_preferences(peer, {'max_seconds': min(prefs['max_seconds'] or 21600, max(60, int(document['duration'] * .75)))})
            elif kind == 'block_author':
                self.save_preferences(peer, {'blocked_authors': list(dict.fromkeys([*prefs['blocked_authors'], document['author_uid']]))[-30:]})
            self.messaging.store.event('feedback', f'已记录视频反馈：{FEEDBACK[kind]}', bvid)
        return {'bvid': bvid, 'kind': kind, 'saved': True}

    def feedback_context(self, peer: str) -> list[dict]:
        """Provide bounded, recipient-isolated examples for future matching."""
        return [{'bvid': row['bvid'], 'feedback': FEEDBACK[row['kind']],
                 'title': json.loads(row['document'])['title']} for row in self.db.execute('''SELECT f.bvid,f.kind,v.document FROM recommendation_feedback f
            JOIN videos v ON v.id=(SELECT MIN(id) FROM videos WHERE bvid=f.bvid)
            WHERE f.account=? AND f.peer=? ORDER BY f.created DESC LIMIT 8''', (self.messaging.account, peer))]

    def recent_videos(self, peer: str) -> list[dict]:
        """Recover only this recipient's delivered video context for follow-up questions."""
        bvids = []
        for row in self.db.execute("SELECT text FROM dm_outbox WHERE account=? AND peer=? AND state='sent' ORDER BY created DESC LIMIT 8", (self.messaging.account, peer)):
            for bvid in re.findall(r'BV[0-9A-Za-z]{10}', row[0]):
                if bvid not in bvids:
                    bvids.append(bvid)
        results = []
        for bvid in bvids[:3]:
            row = self.db.execute("SELECT result FROM videos WHERE bvid=? AND state='done' ORDER BY id DESC LIMIT 1", (bvid,)).fetchone()
            if row:
                video = json.loads(row[0])
                results.append({k: video[k] for k in ('bvid', 'title', 'summary', 'facts', 'evidence') if k in video})
        return results

    def handle_message(self, peer: str, text: str) -> str | None:
        """Handle unambiguous preference commands and feedback without requiring a model."""
        phrase = text.strip().rstrip('。！!')
        settings = {'学习模式': {'mode': 'learning'}, '休闲模式': {'mode': 'leisure'},
                    '暂停推荐': {'paused_until': 4102444800}, '恢复推荐': {'paused_until': 0}}
        if phrase in ('今天先别发', '今天别推荐了'):
            tomorrow = datetime.now(LOCAL_TIME).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            settings[phrase] = {'paused_until': tomorrow.timestamp()}
        if phrase in settings:
            self.save_preferences(peer, settings[phrase])
            return '已更新你的推荐设置。主动推荐会按新设置处理；你明确提出的找视频请求仍单独处理，发送权限保持原设置。'
        match = re.fullmatch(r'(学习目标|我的水平)[：:]\s*(.{1,500})', phrase)
        if match:
            field = 'goal' if match[1] == '学习目标' else 'level'
            if field == 'level' and len(match[2]) > 100:
                return '水平描述请控制在100字以内，例如“我的水平：刚学会Python变量和循环”。'
            self.save_preferences(peer, {field: match[2]})
            return '已记住你的' + match[1] + '，之后推荐会作为参考。'
        commands = {'这个不错': 'like', '这个视频不错': 'like', '喜欢': 'like', '太基础': 'too_basic',
                    '太基础了': 'too_basic', '太难': 'too_hard', '太难了': 'too_hard', '太长': 'too_long',
                    '太长了': 'too_long', '不感兴趣': 'not_interested', '看过了': 'seen',
                    '不要这个UP': 'block_author', '不要这个up': 'block_author'}
        codes = re.findall(r'BV[0-9A-Za-z]{10}', phrase)
        command = re.sub(r'BV[0-9A-Za-z]{10}', '', phrase).strip(' ，,：:')
        if command not in commands:
            return None
        videos = self.recent_videos(peer)
        if not codes:
            if len(videos) != 1:
                return '请附上对应视频的BV号，例如“BVxxxxxxxxxx 太长”，这样不会把反馈记到别的视频上。'
            codes = [videos[0]['bvid']]
        if len(codes) != 1:
            return '请每次指定一个视频的BV号来反馈。'
        try:
            self.feedback(peer, codes[0], commands[command])
        except RpcError as exc:
            if str(exc) == 'VIDEO_AUTHOR_UNKNOWN':
                return '这个视频暂时没有已核实的UP主信息，未添加UP屏蔽。你可以在面板填写该UP的UID。'
            if str(exc) in ('RECOMMENDATION_NOT_DELIVERED', 'VIDEO_NOT_ANALYZED'):
                return '没有找到这条视频已向你发送并完成分析的记录，请核对BV号。'
            raise
        return f'已记录你对 {codes[0]} 的反馈：{FEEDBACK[commands[command]]}。之后会据此调整推荐。'

    def matches(self, peer: str, video: dict, max_seconds: int | None = None) -> bool:
        """Apply explicit exclusions before asking the model for subjective suitability."""
        prefs = self.preferences(peer)
        if video.get('bvid') and ('duration' not in video or 'author_uid' not in video):
            cached = self.db.execute('SELECT document FROM videos WHERE bvid=? ORDER BY id LIMIT 1', (video['bvid'],)).fetchone()
            if cached:
                video = {**json.loads(cached[0]), **video}
        haystack = ' '.join([video.get('title', ''), video.get('summary', '')]).casefold()
        if any(word.casefold() in haystack for word in prefs['blocked_keywords']):
            return False
        if video.get('author_uid') in prefs['blocked_authors'] or video.get('author') in prefs['blocked_authors']:
            return False
        maximum = prefs['max_seconds'] if max_seconds is None else max_seconds
        if maximum and video.get('duration', 0) > maximum:
            return False
        return not self.db.execute("SELECT 1 FROM recommendation_feedback WHERE account=? AND peer=? AND bvid=? AND kind IN ('not_interested','seen','block_author')", (self.messaging.account, peer, video.get('bvid'))).fetchone()

    def delivery_status(self, peer: str, now: datetime | None = None) -> dict:
        """Explain proactive quiet hours and daily caps using Beijing time, including overnight windows."""
        now = (now or datetime.now(LOCAL_TIME)).astimezone(LOCAL_TIME)
        prefs = self.preferences(peer)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        used = self.db.execute('''SELECT COUNT(*) FROM recommendations r JOIN dm_outbox o ON o.id=r.draft_id
            WHERE r.account=? AND r.peer=? AND o.attempted>=?''', (self.messaging.account, peer, midnight.timestamp())).fetchone()[0]
        start, end = clock_minutes(prefs['push_start']), clock_minutes(prefs['push_end'])
        minute = now.hour * 60 + now.minute
        within = start == end or (start <= minute < end if start < end else minute >= start or minute < end)
        reason, next_at = None, now.timestamp()
        if prefs['paused_until'] > next_at:
            reason, next_at = 'RECOMMENDATIONS_PAUSED', prefs['paused_until']
        elif not within:
            slot = midnight + timedelta(minutes=start)
            if slot <= now:
                slot += timedelta(days=1)
            reason, next_at = 'RECOMMENDATION_QUIET_HOURS', slot.timestamp()
        elif prefs['daily_limit'] == 0 or used >= prefs['daily_limit']:
            reason, next_at = 'RECOMMENDATION_DAILY_LIMIT', (midnight + timedelta(days=1)).timestamp()
        connection = self.connections.get(peer)
        if connection and (not connection['enabled'] or not connection['mutual'] or connection['state'] != 'ready'):
            reason = 'FRIEND_GREETING_PENDING' if connection['mutual'] and connection['enabled'] else 'FRIEND_NOT_MUTUAL'
        return {'used': used, 'limit': prefs['daily_limit'], 'reason': reason, 'next_at': next_at}

    def profile(self, peer):
        row = self.db.execute('SELECT * FROM friends WHERE account=? AND peer=?',
                              (self.messaging.account, peer)).fetchone()
        if not row:
            return None
        return {**dict(row), 'interests': json.loads(row['interests']),
                'recommend': bool(row['recommend']), 'auto_send': bool(row['auto_send']),
                'preferences': self.preferences(peer), 'delivery': self.delivery_status(peer),
                'connection': self.connections.get(peer)}

    def guard_delivery(self, draft: sqlite3.Row) -> None:
        """Enforce the proactive cap inside Messaging's send lock, including manual sends."""
        recommendation = self.db.execute('SELECT 1 FROM recommendations WHERE account=? AND draft_id=?',
            (self.messaging.account, draft['depends_on'] or draft['id'])).fetchone()
        if not recommendation:
            return  # Ordinary conversation and explicit recommendations have no daily cap.
        delivery = self.delivery_status(draft['peer'])
        if draft['depends_on']:
            # A paired card is the same recommendation, not a sixth video. Do not carry it into another day.
            parent = self.db.execute('SELECT attempted FROM dm_outbox WHERE id=?', (draft['depends_on'],)).fetchone()
            midnight = datetime.now(LOCAL_TIME).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            if not parent or not parent['attempted'] or parent['attempted'] < midnight:
                raise RpcError(-32040, 'RECOMMENDATION_CARD_EXPIRED')
            if delivery['reason'] == 'RECOMMENDATION_DAILY_LIMIT' and delivery['limit'] > 0:
                return
        if delivery['reason']:
            raise RpcError(-32040, delivery['reason'])

    def status(self):
        account = self.messaging.account
        peers = self.db.execute('SELECT peer FROM friends WHERE account=? ORDER BY created,peer', (account,)).fetchall()
        deliveries = self.db.execute("""SELECT r.peer,r.bvid,r.video_id,r.reason,r.draft_id,o.state,o.error
            FROM recommendations r LEFT JOIN dm_outbox o ON o.id=r.draft_id
            WHERE r.account=? AND r.draft_id IS NOT NULL ORDER BY r.created DESC LIMIT 20""", (account,))
        return {'friends': [self.profile(row[0]) for row in peers],
                'recommendations': [dict(row) for row in deliveries], 'error': self.error,
                'feedback': [dict(row) for row in self.db.execute('SELECT peer,bvid,kind,created FROM recommendation_feedback WHERE account=? ORDER BY created DESC LIMIT 30', (account,))],
                'state': 'RUNNING' if self.task and not self.task.done() else 'STOPPED',
                'matching': 'model_persona_and_friend' if self.model_driven else 'explicit_interest_keywords', 'format': 'short_reason_link'}

    @staticmethod
    def proactive_body(result: dict, reason: str) -> str:
        """Keep unsolicited recommendations to one reason and a final video link."""
        short_reason = reason.strip()[:68].rstrip('，。； ') + '。'
        link = f"https://www.bilibili.com/video/{result['bvid']}/?p={result.get('page', 1)}"
        return f"刚刷到《{result['title'][:35]}》，{short_reason}\n{link}"

    def recommend(self, peer: str, video_id: int, result: dict, reason: str, share: bool = False,
                  request_id: int | None = None) -> str:
        """Reserve one video per recipient across proactive pushes and all explicit requests."""
        account = self.messaging.require_account()
        previous = self.previous_recommendation(peer, result['bvid'])
        if previous:
            return previous['id']
        link = f"https://www.bilibili.com/video/{result['bvid']}/?p={result.get('page', 1)}"
        if request_id is None:
            # Proactive pushes are sent as each browsed video finishes, so keep the DM conversational.
            body = self.proactive_body(result, reason)
        else:
            body = f"找到一个适合你这次点播的视频：《{result['title'][:60]}》。{reason[:160]}\n视频总结：{result['summary'][:400]}\n{link}"
        if request_id is not None:
            draft_id = self.messaging.compose(peer, body, f"req-{request_id}-{video_id}-{peer}")['id']
            with self.db:
                self.db.execute('UPDATE dm_outbox SET recommendation_bvid=? WHERE id=?', (result['bvid'], draft_id))
            if share:
                self.messaging.compose_card(peer, result['bvid'], draft_id, f"req-card-{request_id}-{video_id}-{peer}")
            return draft_id
        existing = self.db.execute('SELECT draft_id FROM recommendations WHERE account=? AND peer=? AND bvid=?',
                                   (account, peer, result['bvid'])).fetchone()
        if existing and existing['draft_id']:
            draft_id = existing['draft_id']
        else:
            draft_id = self.messaging.compose(peer, body, f"rec-{result['bvid']}-{peer}")['id']
            with self.db:
                self.db.execute('INSERT INTO recommendations VALUES(?,?,?,?,?,?,?) ON CONFLICT(account,peer,bvid) DO UPDATE SET reason=excluded.reason,draft_id=excluded.draft_id',
                    (account, peer, result['bvid'], video_id, reason, draft_id, time.time()))
                self.db.execute('UPDATE dm_outbox SET recommendation_bvid=? WHERE id=?', (result['bvid'], draft_id))
        if share:
            self.messaging.compose_card(peer, result['bvid'], draft_id, f"card-{result['bvid']}-{peer}")
        if not existing or not existing['draft_id']:
            self.messaging.store.event('recommendation', '已生成简短推荐和视频链接', result['bvid'])
        self.wake.set()
        return draft_id

    def save(self, value, pending_connection: bool = False):
        account = self.messaging.require_account()
        if not isinstance(value, dict):
            raise RpcError(-32602, 'INVALID_FRIEND_PROFILE')
        peer = uid(value.get('peer_uid'))
        name, category, style = value.get('name'), value.get('category', '好友'), value.get('style', '')
        interests = value.get('interests')
        recommend, auto_send = value.get('recommend', True), value.get('auto_send', False)
        if (peer == account or not isinstance(name, str) or not 1 <= len(name.strip()) <= 50
                or not isinstance(category, str) or not 1 <= len(category.strip()) <= 30
                or not isinstance(style, str) or len(style) > 500
                or not isinstance(interests, list) or not (1 if recommend else 0) <= len(interests) <= 12
                or any(not isinstance(item, str) or not 1 <= len(item.strip()) <= 30 for item in interests)
                or type(recommend) is not bool or type(auto_send) is not bool):
            raise RpcError(-32602, 'INVALID_FRIEND_PROFILE')
        if auto_send and not pending_connection and not self.connections.get(peer) and (not self.messaging.send_enabled or peer not in self.messaging.allowed_peers):
            raise RpcError(-32040, 'CONFIGURE_DM_SEND_SCOPE_FIRST')
        if 'preferences' in value:
            self.save_preferences(peer, value['preferences'])
        with self.db:
            self.db.execute("""INSERT INTO friends VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(account,peer) DO UPDATE SET name=excluded.name,category=excluded.category,
                interests=excluded.interests,style=excluded.style,
                created=CASE WHEN friends.recommend=0 AND excluded.recommend=1 THEN excluded.created ELSE friends.created END,
                recommend=excluded.recommend,auto_send=excluded.auto_send""",
                (account, peer, name.strip(), category.strip(), json.dumps(list(dict.fromkeys(item.strip() for item in interests)), ensure_ascii=False),
                 style.strip(), recommend, auto_send, time.time()))
        self.wake.set()
        return self.profile(peer)

    def start(self):
        self.connections.start()
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())

    def discover(self):
        account = self.messaging.account
        if not account:
            return
        # Completed experiences are the durable event source, so restarts cannot lose events.
        candidates = self.db.execute("""SELECT v.id,v.bvid,v.result,e.valence,e.topic,f.peer
            FROM videos v JOIN experiences e ON e.video_id=v.id
            JOIN friends f ON f.account=? AND f.recommend=1 AND e.created>=f.created
            LEFT JOIN recommendations r ON r.account=f.account AND r.peer=f.peer AND r.bvid=v.bvid
            WHERE v.state='done' AND r.bvid IS NULL ORDER BY e.created,v.id LIMIT 20""", (account,)).fetchall()
        for row in candidates:
            if self.db.execute('SELECT 1 FROM recommendations WHERE account=? AND peer=? AND bvid=?',
                               (account, row['peer'], row['bvid'])).fetchone():
                continue
            friend, result = self.profile(row['peer']), json.loads(row['result'])
            haystack = ' '.join([row['topic'], result['title'], result['summary'], *(fact['text'] for fact in result['facts'])]).casefold()
            matches = [term for term in friend['interests'] if term.casefold() in haystack]
            reason, draft_id = '兴趣未匹配或没有正面感受', None
            if row['valence'] > 0 and matches and self.can_recommend(row['peer'], result):
                reason = '这段内容提到了你感兴趣的' + '、'.join(matches[:3]) + '，觉得你可能会喜欢。'
                body = f"刚刷到《{result['title'][:35]}》，{reason[:68]}\nhttps://www.bilibili.com/video/{row['bvid']}/"
                draft = self.messaging.compose(row['peer'], body, f"rec-{row['bvid']}-{row['peer']}")
                draft_id = draft['id']
                with self.db:
                    self.db.execute('UPDATE dm_outbox SET recommendation_bvid=? WHERE id=?', (row['bvid'], draft_id))
            with self.db:
                self.db.execute('INSERT OR IGNORE INTO recommendations VALUES(?,?,?,?,?,?,?)',
                                (account, row['peer'], row['bvid'], row['id'], reason, draft_id, time.time()))

    async def tick(self):
        if not self.model_driven:
            self.discover()
        if not self.messaging.send_enabled or not self.proactive_enabled:
            return
        if self.messaging.priority_pending():
            return
        if self.messaging.delivery_window()['reason']:
            return
        # Older queued cards belong to the former long-form format; leave only the text link.
        with self.db:
            self.db.execute("""UPDATE dm_outbox SET state='cancelled',error='PROACTIVE_LINK_ONLY'
                WHERE account=? AND state='draft' AND card_bvid IS NOT NULL
                AND depends_on IN (SELECT draft_id FROM recommendations WHERE account=?)""",
                (self.messaging.account, self.messaging.account))
        pending = self.db.execute("""SELECT o.id,o.peer,r.bvid,r.reason FROM recommendations r
            JOIN dm_outbox o ON o.id=r.draft_id JOIN friends f ON f.account=r.account AND f.peer=r.peer
            WHERE r.account=? AND f.recommend=1 AND f.auto_send=1 AND o.state='draft'
            ORDER BY r.created LIMIT 20""", (self.messaging.account,)).fetchall()
        for row in pending:
            if row['peer'] not in self.messaging.allowed_peers:
                continue
            if self.delivery_status(row['peer'])['reason']:
                continue
            video = self.db.execute("SELECT result FROM videos WHERE bvid=? AND state='done' LIMIT 1", (row['bvid'],)).fetchone()
            document = json.loads(video[0]) if video else None
            if document and not self.matches(row['peer'], document):
                continue
            if self.model_driven:
                decision = self.db.execute('SELECT value FROM video_decisions WHERE account=? AND bvid=?', (self.messaging.account, row['bvid'])).fetchone()
                if not decision or json.loads(decision[0]).get('rating') != '值得看':
                    continue
            if document:
                with self.db:
                    self.db.execute("UPDATE dm_outbox SET text=? WHERE id=? AND state='draft' AND text LIKE '推荐给你：%'",
                                    (self.proactive_body(document, row['reason']), row['id']))
            result = await self.messaging.publish(row['id'])
            if result['state'] == 'uncertain':
                raise RpcError(-32040, 'DM_SEND_UNCERTAIN')
            break  # One automatic recommendation per tick; normal DM quotas still apply.

    async def run(self):
        while True:
            self.wake.clear()
            try:
                await self.tick()
                self.error = None
            except asyncio.CancelledError:
                raise
            except RpcError as exc:
                self.error = str(exc)
            except Exception as exc:
                self.error = type(exc).__name__
            try:
                await asyncio.wait_for(self.wake.wait(), 10)
            except TimeoutError:
                pass

    async def close(self):
        await self.connections.close()
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
