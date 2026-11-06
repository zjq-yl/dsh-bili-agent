"""Python 进程入口：组装业务服务、分发 RPC，并管理任务生命周期。"""
import argparse
import asyncio
import json
import os
from pathlib import Path

from . import __version__
from .media import Media
from .messaging import Messaging, uid
from .model import Model
from .platform import Platform
from .player import Player
from .protocol import Peer, RpcError
from .runner import Runner
from .store import Store
from .social import Social
from .automation import VideoAgent
from .library import browse as browse_library
from .content import validate_video


class Runtime:
    """Own the worker services and their shared data directory."""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir).resolve()
        self.peer = Peer(self.handle)
        self.ready = False
        self.store = None
        self.model = None
        self.runner = None
        self.platform = Platform(self.data_dir)
        self.player = Player(self.data_dir)
        self.media = None
        self.messaging = None
        self.social = None
        self.automation = None

    async def handle(self, method, params):
        if method == "worker.hello":
            return self.initialize(params)
        if not self.ready:
            raise RpcError(-32003, "NOT_READY")
        if method == "worker.health":
            return {"ready": True, "pid": os.getpid(), "version": __version__,
                    "capabilities": {"platform": self.platform.last_status, "player": self.player.status()},
                    "agent": self.runner.status(),
                    "messaging": self.messaging.status(),
                    "social": self.social.status(),
                    "automation": self.automation.status(),
                    "agents": {"video": {"name": "视频 Agent", "state": 'RUNNING' if self.automation.task and not self.automation.task.done() else 'STOPPED',
                        "analysis_state": self.runner.state, "activity": self.automation.activity,
                        "proactive_video_push": self.automation.config['proactive_enabled']}, "interaction": self.automation.interaction.status()},
                    "events": self.store.events(),
                    "recent_model_calls": self.store.call_status()}
        if method == "agent.status":
            return self.runner.status()
        if method == 'library.list':
            return browse_library(self.store.db, self.messaging.account, params.get('query', ''), params.get('rating', ''),
                                  params.get('offset', 0), params.get('limit', 12))
        if method == 'library.refresh':
            video_id = params.get('video_id')
            if type(video_id) is not int or video_id <= 0:
                raise RpcError(-32602, 'INVALID_VIDEO_ID')
            row = self.store.db.execute('SELECT document FROM videos WHERE id=?', (video_id,)).fetchone()
            if not row:
                raise RpcError(-32602, 'VIDEO_NOT_FOUND')
            document = json.loads(row['document'])
            metadata = await self.platform.request('metadata', {'bvid': document['bvid'], 'page': document['page']})
            if metadata['cid'] != document['cid']:
                raise RpcError(-32010, 'VIDEO_PART_CHANGED')
            clean = validate_video({**document, **metadata})
            document.update({key: clean[key] for key in ('title', 'cover', 'author', 'author_uid')})
            with self.store.db:
                self.store.db.execute('UPDATE videos SET document=? WHERE id=?', (json.dumps(document, ensure_ascii=False), video_id))
            return {'updated': True, 'has_cover': bool(clean['cover'])}
        if method == 'automation.status':
            return self.automation.status()
        if method == 'mentions.retry':
            return self.automation.interaction.mentions.retry(params.get('id'))
        if method == 'automation.retry_review':
            return self.automation.retry_review(params.get('video_id'))
        if method == 'automation.configure':
            result = self.automation.configure(params.get('config'))
            if not self.automation.config['dm_auto_start']:
                await self.messaging.close()
            return result
        if method == 'automation.folders':
            identity = await self.messaging.bind()
            return await self.platform.request('favorite_folders', {'account_uid': identity['uid']})
        if method == "player.command":
            return await self.player.command(params.get("action"), params)
        if method.startswith("media."):
            return await self.handle_media(method, params)
        if method.startswith("dm."):
            return await self.handle_messaging(method, params)
        if method == 'friends.status':
            return self.social.status()
        if method == 'friends.save':
            return await self.social.connections.save_profile(params.get('profile'))
        if method == 'friends.connect':
            return await self.social.connections.connect(params.get('profile'), params.get('greeting', ''))
        if method == 'friends.connection':
            return self.social.connections.control(params.get('peer_uid'), params.get('action'))
        if method == 'friends.directory':
            return await self.social.connections.directory(params.get('kind', 'mutual'), params.get('page', 1))
        if method == 'friends.lookup':
            return await self.social.connections.lookup(params.get('peer_uid'))
        if method == 'friends.contact':
            return await self.social.connections.contact(params.get('peer_uid'), params.get('action'), params.get('name', ''))
        if method == 'friends.message':
            return await self.social.connections.send_message(params.get('peer_uid'), params.get('text'), params.get('command_id'))
        if method == 'friends.preferences':
            if params.get('pause_today') is True:
                peer = uid(params.get('peer_uid'))
                self.social.handle_message(peer, '今天先别发')
                return self.social.preferences(peer)
            return self.social.save_preferences(params.get('peer_uid'), params.get('preferences'))
        if method == 'friends.feedback':
            return self.social.feedback(params.get('peer_uid'), params.get('bvid'), params.get('kind'))
        if method == "memory.profile":
            return self.runner.memory.profile()
        if method == "memory.configure":
            return self.runner.memory.configure(params.get("profile"))
        if method == "memory.recall":
            return self.runner.memory.recall(params.get("query"))
        if method == "platform.search":
            return await self.platform.request("search", params)
        if method == "platform.enqueue":
            video = await self.platform.request("fetch", params)
            result = self.runner.enqueue(video, source="bilibili_subtitles")
            return {**result, "bvid": video["bvid"], "cid": video["cid"], "page": video["page"],
                    "subtitle_language": video["subtitle_language"]}
        if method == "agent.enqueue":
            return self.runner.enqueue(params.get("video"))
        if method == "agent.start":
            self.automation.video_paused = False
            return self.runner.start(params.get("keyword"))
        if method in ("agent.pause", "agent.stop"):
            self.automation.video_paused = True
            return await self.runner.pause("PAUSED" if method == "agent.pause" else "STOPPED")
        if method in ("agent.result", "agent.retry", "agent.reanalyze"):
            video_id = params.get("video_id")
            if type(video_id) is not int or video_id <= 0:
                raise RpcError(-32602, "INVALID_VIDEO_ID")
            if method == 'agent.reanalyze':
                return self.runner.reanalyze(video_id)
            return self.runner.result(video_id) if method == "agent.result" else self.runner.retry(video_id)
        if method == "model.check":
            return {"text": await self.model.generate("check", "请简短回答。", "只输出：模型连接正常")}
        if method == "worker.shutdown":
            await self.automation.interaction.close()
            await self.automation.close()
            await self.runner.pause()
            await self.social.close()
            await self.media.close()
            await self.messaging.close()
            self.peer.stopping.set()
            return {"stopped": True}
        raise RpcError(-32601, "未知方法")

    def initialize(self, params):
        """Validate host settings and construct services once per worker."""
        if params.get("protocol_version") != 1:
            raise RpcError(-32001, "PROTOCOL_MISMATCH")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.store:
            settings = params.get('settings', {})
            if not isinstance(settings, dict) or not isinstance(settings.get('asr_model', ''), str):
                raise RpcError(-32602, 'INVALID_SETTINGS')
            peers = settings.get('dm_allowed_peers', [])
            if not isinstance(peers, list) or len(peers) > 100 or type(settings.get('dm_send_enabled', False)) is not bool:
                raise RpcError(-32602, 'INVALID_DM_SETTINGS')
            for value in peers:
                uid(value)
            self.store = Store(self.data_dir)
            self.model = Model(self.peer, self.store)
            self.runner = Runner(self.store, self.model, self.platform)
            self.media = Media(self.store, self.platform, self.runner, self.data_dir, settings.get('asr_model', ''))
            self.messaging = Messaging(self.store, self.platform, self.model, self.runner.memory,
                settings.get('dm_send_enabled', False), peers)
            self.social = Social(self.store, self.messaging)
            self.social.model_driven = True
            self.messaging.friends = self.social
            self.automation = VideoAgent(self.store, self.runner, self.messaging, self.social, self.platform, self.model, self.data_dir)
            self.social.proactive_enabled = self.automation.config['proactive_enabled']
            self.messaging.recommendations = self.automation
            self.runner.assessment = self.automation.assessment
            self.runner.on_complete = self.automation.wake.set
            self.automation.start()
            self.automation.interaction.start()
        self.ready = True
        return {"protocol_version": 1, "worker_version": __version__, "pid": os.getpid()}

    async def handle_media(self, method, params):
        if method == 'media.start':
            return self.media.start(params)
        if method == 'media.status':
            return self.media.status(params.get('job_id'))
        if method == 'media.cancel':
            await self.media.close()
            return {'cancelled': True}
        raise RpcError(-32601, "未知方法")

    async def handle_messaging(self, method, params):
        if method == 'dm.account':
            return await self.messaging.bind()
        if method == 'dm.status':
            return self.messaging.status()
        if method == 'dm.sync':
            return await self.messaging.sync(params.get('peer_uid'))
        if method == 'dm.history':
            return self.messaging.history(params.get('peer_uid'))
        if method == 'dm.reply':
            return await self.messaging.draft_reply(params.get('msg_key'))
        if method == 'dm.compose':
            return self.messaging.compose(params.get('peer_uid'), params.get('text'), params.get('command_id'))
        if method == 'dm.send':
            return await self.messaging.publish(params.get('draft_id'))
        if method == 'dm.summarize':
            return await self.messaging.summarize(params.get('peer_uid'))
        if method == 'dm.memory':
            return self.messaging.private_memory(params.get('peer_uid'))
        if method == 'dm.forget':
            return self.messaging.forget(params.get('peer_uid'))
        if method == 'dm.start':
            self.automation.dm_paused = False
            self.automation.start_attempted = True
            return await self.messaging.start(params.get('auto_send'))
        if method == 'dm.stop':
            self.automation.dm_paused = True
            await self.messaging.close()
            return self.messaging.status()
        raise RpcError(-32601, "未知方法")

    async def close(self) -> None:
        try:
            if self.automation:
                await self.automation.interaction.close()
                await self.automation.close()
            if self.runner:
                await self.runner.pause()
            if self.social:
                await self.social.close()
            if self.media:
                await self.media.close()
            if self.messaging:
                await self.messaging.close()
            await self.player.close()
        finally:
            if self.store:
                self.store.close()

    async def run(self) -> None:
        try:
            await self.peer.serve()
        finally:
            await self.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    args = parser.parse_args()
    asyncio.run(Runtime(args.data_dir).run())


if __name__ == "__main__":
    main()
