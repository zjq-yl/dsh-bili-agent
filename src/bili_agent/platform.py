"""B 站 API 边界：字幕、音频地址、账号和私信；依赖按需加载。"""
import asyncio
import errno
import html
import json
import re
import time
from urllib.parse import urlsplit

from .content import validate_video
from .protocol import RpcError


def read_credentials(data_dir):
    path = data_dir / "credentials.json"
    if not path.exists():
        return {}
    if path.stat().st_size > 16384:
        raise RpcError(-32010, "INVALID_CREDENTIAL_FILE")
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError):
        raise RpcError(-32010, "INVALID_CREDENTIAL_FILE") from None
    if not isinstance(values, dict) or any(not isinstance(v, str) for v in values.values()):
        raise RpcError(-32010, "INVALID_CREDENTIAL_FILE")
    return values


def subtitle_url(value):
    if not isinstance(value, str):
        raise RpcError(-32010, "INVALID_SUBTITLE_URL")
    url = "https:" + value if value.startswith("//") else value
    parts = urlsplit(url)
    host = parts.hostname or ""
    if (parts.scheme != "https" or not (host == "hdslb.com" or host.endswith(".hdslb.com"))
            or parts.username or parts.password or parts.port not in (None, 443)):
        raise RpcError(-32010, "INVALID_SUBTITLE_URL")
    return url


async def download_subtitles(url):
    import aiohttp
    url = subtitle_url(url)
    # CDN请求不带账号Cookie，不接受重定向到其他域。
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        async with session.get(url, allow_redirects=False) as response:
            if response.status != 200:
                raise RpcError(-32010, "SUBTITLE_DOWNLOAD_FAILED")
            raw = bytearray()
            async for chunk in response.content.iter_chunked(32768):
                raw.extend(chunk)
                if len(raw) > 2 * 1024 * 1024:
                    raise RpcError(-32010, "SUBTITLE_FILE_TOO_LARGE")
            document = json.loads(raw)
            if not isinstance(document, dict) or not isinstance(document.get("body"), list):
                raise RpcError(-32010, "INVALID_SUBTITLE_FILE")
            return document["body"]


class Platform:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.lock = asyncio.Lock()
        self.dm_lock = asyncio.Lock()
        self.rate_lock = asyncio.Lock()
        self.next_request = 0
        self.last_status = "unverified"

    async def request(self, operation, params):
        # Private messaging must not wait for a slow video fetch. All starts still share a rate gate.
        lane = self.dm_lock if operation in ('identity', 'sessions', 'messages', 'send_dm', 'share_video', 'video_action', 'send_comment') else self.lock
        async with lane:
            async with self.rate_lock:
                delay = self.next_request - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                self.next_request = time.monotonic() + 3
            try:
                async with asyncio.timeout(60):
                    handlers = {"search": self.search, "fetch": self.fetch, "audio": self.audio, 'metadata': self.metadata,
                                "identity": self.identity, "sessions": self.sessions, "messages": self.messages,
                                "send_dm": self.send_dm, "share_video": self.share_video, 'send_comment': self.send_comment,
                                "video_action": self.video_action, "favorite_folders": self.favorite_folders}
                    if operation not in handlers:
                        raise RpcError(-32601, "UNKNOWN_PLATFORM_OPERATION")
                    result = await handlers[operation](params)
                self.last_status = "available"
                return result
            except ImportError:
                self.last_status = "dependencies_missing"
                raise RpcError(-32010, "INSTALL_PLUGIN_REQUIREMENTS") from None
            except RpcError as exc:
                self.last_status = str(exc)
                raise
            except TimeoutError:
                self.last_status = "timeout"
                raise RpcError(-32010, "PLATFORM_TIMEOUT") from None
            except Exception as exc:
                code = getattr(exc, "code", None)
                if isinstance(exc, OSError) and exc.errno in (errno.EACCES, errno.EPERM):
                    self.last_status = "NETWORK_ACCESS_DENIED"
                else:
                    self.last_status = {-101: "LOGIN_REQUIRED", -412: "PLATFORM_CHALLENGE", -404: "VIDEO_UNAVAILABLE"}.get(code, "PLATFORM_REQUEST_FAILED")
                raise RpcError(-32010, self.last_status) from None

    def credential(self):
        from bilibili_api import Credential
        values = read_credentials(self.data_dir)
        return Credential(**{key: values[key] for key in ("sessdata", "buvid3", "bili_jct", "dedeuserid") if key in values})

    async def identity(self, params):
        from bilibili_api import user
        info = await user.get_self_info(credential=self.credential())
        return {"uid": str(info["mid"]), "name": str(info.get("name", ""))[:100],
                "can_send_dm": bool(read_credentials(self.data_dir).get('bili_jct'))}

    async def sessions(self, params):
        from bilibili_api import session
        return await session.get_sessions(credential=self.credential(), session_type=1)

    async def messages(self, params):
        from bilibili_api import session
        return await session.fetch_session_msgs(talker_id=int(params["peer_uid"]), credential=self.credential(),
                                                session_type=1, begin_seqno=0)

    async def send_dm(self, params):
        from bilibili_api import session
        from bilibili_api.utils.network import request_settings
        credential = self.credential()
        # 使用刚经identity验证的实际UID，避免过期的DedeUserID错误指定发送者。
        credential.dedeuserid = params["account_uid"]
        previous = request_settings.get_wbi_retry_times()
        request_settings.set_wbi_retry_times(1)
        try:
            return await session.send_msg(credential=credential, receiver_id=int(params["peer_uid"]),
                                          msg_type=session.EventType.TEXT, content=params["text"])
        finally:
            request_settings.set_wbi_retry_times(previous)

    async def send_comment(self, params: dict) -> dict:
        """Send one top-level public comment; unknown receipts must never be replayed."""
        from bilibili_api import comment, video
        from bilibili_api.utils.network import request_settings
        text, bvid = params.get('text'), params.get('bvid')
        if (not isinstance(text, str) or not 1 <= len(text.strip()) <= 300
                or not isinstance(bvid, str) or not re.fullmatch(r'BV[0-9A-Za-z]{10}', bvid)):
            raise RpcError(-32602, 'INVALID_PUBLIC_COMMENT')
        credential = self.credential()
        previous = request_settings.get_wbi_retry_times()
        request_settings.set_wbi_retry_times(1)
        try:
            response = await comment.send_comment(text.strip(), video.Video(bvid=bvid).get_aid(),
                comment.CommentResourceType.VIDEO, credential=credential)
            return {'rpid': str(response.get('rpid') or (response.get('reply') or {}).get('rpid') or '')}
        finally:
            request_settings.set_wbi_retry_times(previous)

    async def video_part(self, params):
        bvid, page = params.get("bvid"), params.get("page", 1)
        if (not isinstance(bvid, str) or not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid)
                or type(page) is not int or not 1 <= page <= 1000):
            raise RpcError(-32602, "INVALID_VIDEO_PART")
        from bilibili_api import video
        item = video.Video(bvid=bvid, credential=self.credential())
        info = await item.get_info()
        part = next((p for p in info.get("pages", []) if p.get("page") == page), None)
        if not part:
            raise RpcError(-32602, "VIDEO_PART_NOT_FOUND")
        return item, info, part

    async def share_video(self, params: dict) -> dict:
        """Send a real type-7 video card; the outbox owns scope, quotas and uncertain receipts."""
        from bilibili_api import session
        from bilibili_api.utils.network import Api, request_settings
        item, info, _ = await self.video_part({'bvid': params['bvid']})
        credential = self.credential()
        credential.raise_for_no_sessdata()
        credential.raise_for_no_bili_jct()
        card = {'source': 5, 'id': item.get_aid(), 'bvid': params['bvid'],
                'title': str(info['title'])[:300], 'thumb': info.get('pic', ''),
                'author': info.get('owner', {}).get('name', '')}
        sender, receiver = int(params['account_uid']), int(params['peer_uid'])
        data = {'msg[sender_uid]': sender, 'msg[receiver_id]': receiver, 'msg[receiver_type]': 1,
                'msg[msg_type]': 7, 'msg[msg_status]': 0, 'msg[content]': json.dumps(card, ensure_ascii=False),
                'msg[timestamp]': int(time.time()), 'msg[new_face_version]': 0, 'mobi_app': 'web', 'build': 0}
        previous = request_settings.get_wbi_retry_times()
        request_settings.set_wbi_retry_times(1)
        try:
            return await Api(**session.API['operate']['send_msg'], credential=credential).update_params(
                w_sender_uid=sender, w_receiver_id=receiver).update_data(**data).result
        finally:
            request_settings.set_wbi_retry_times(previous)

    async def metadata(self, params: dict) -> dict:
        """Refresh public display metadata without downloading media or invoking a model."""
        _, info, part = await self.video_part(params)
        return {'cid': str(part['cid']), 'title': str(info.get('title', ''))[:300],
                'cover': info.get('pic', ''), 'author': str((info.get('owner') or {}).get('name', ''))[:100],
                'author_uid': str((info.get('owner') or {}).get('mid') or '')}

    async def favorite_folders(self, params: dict) -> dict:
        """List the verified account's folders without creating or changing a folder."""
        from bilibili_api import favorite_list
        data = await favorite_list.get_video_favorite_list(int(params['account_uid']), credential=self.credential())
        return {'folders': [{'id': str(row['id']), 'title': str(row['title'])[:100]} for row in data.get('list', [])]}

    async def video_action(self, params: dict) -> dict:
        """Apply one explicit like/favorite/dislike action; never interpret dislike as unlike."""
        from bilibili_api import video
        from bilibili_api.utils.network import Api, request_settings
        bvid = params.get('bvid')
        if not isinstance(bvid, str) or not re.fullmatch(r'BV[0-9A-Za-z]{10}', bvid):
            raise RpcError(-32602, 'INVALID_BVID')
        credential = self.credential()
        item = video.Video(bvid=bvid, credential=credential)
        action = params.get('action')
        # Resolve missing configuration before issuing a write request.
        folder = params.get('folder_id')
        if action == 'favorite' and not folder:
            folders = (await self.favorite_folders(params))['folders']
            defaults = [row for row in folders if row['title'] == '默认收藏夹']
            if not defaults:
                raise RpcError(-32010, 'FAVORITE_FOLDER_REQUIRED')
            folder = defaults[0]['id']
        access_key = read_credentials(self.data_dir).get('access_key')
        if action == 'dislike' and not access_key:
            raise RpcError(-32010, 'DISLIKE_REQUIRES_APP_TOKEN')
        previous = request_settings.get_wbi_retry_times()
        request_settings.set_wbi_retry_times(1)
        try:
            if action == 'like':
                await item.like(True)
            elif action == 'favorite':
                await item.set_favorite(add_media_ids=[int(folder)])
            elif action == 'dislike':
                await Api(url='https://app.biliapi.net/x/v2/view/dislike', method='POST').update_data(
                    access_key=access_key, aid=item.get_aid(), dislike=0).result
            else:
                raise RpcError(-32602, 'INVALID_VIDEO_ACTION')
            return {'accepted': True, 'action': action, 'bvid': bvid}
        finally:
            request_settings.set_wbi_retry_times(previous)

    async def audio(self, params):
        item, info, part = await self.video_part(params)
        data = await item.get_download_url(cid=int(part["cid"]))
        tracks = (data.get("dash") or {}).get("audio") or []
        tracks = [t for t in tracks if isinstance(t, dict) and (t.get("baseUrl") or t.get("base_url"))]
        if not tracks:
            raise RpcError(-32010, "NO_AUDIO_TRACK")
        # 语音转写无需高码率；不下载混合视频流，不越过会员或地区限制。
        track = min(tracks, key=lambda t: int(t.get("bandwidth", 0)))
        return {"bvid": params["bvid"], "cid": str(part["cid"]), "page": params.get("page", 1),
                "title": str(info.get("title", ""))[:300], "duration": part["duration"],
                'cover': info.get('pic', ''), 'author': str((info.get('owner') or {}).get('name', ''))[:100],
                'author_uid': str((info.get('owner') or {}).get('mid') or ''),
                "url": track.get("baseUrl") or track.get("base_url"), "track_id": str(track.get("id", ""))}

    async def search(self, params):
        keyword = params.get("keyword")
        if not isinstance(keyword, str) or not 1 <= len(keyword.strip()) <= 100:
            raise RpcError(-32602, "INVALID_KEYWORD")
        from bilibili_api import search
        page, limit = params.get('page', 1), params.get('limit', 10)
        if type(page) is not int or not 1 <= page <= 50 or type(limit) is not int or not 1 <= limit <= 20:
            raise RpcError(-32602, 'INVALID_SEARCH_PAGE')
        data = await search.search_by_type(keyword=keyword.strip(), search_type=search.SearchObjectType.VIDEO,
                                          order_type=search.OrderVideo.TOTALRANK, page=page)
        items = []
        for row in data.get("result", []):
            if not isinstance(row, dict) or not re.fullmatch(r"BV[0-9A-Za-z]{10}", str(row.get("bvid", ""))):
                continue
            items.append({"bvid": row["bvid"], "title": html.unescape(re.sub(r"<[^>]*>", "", str(row.get("title", ""))))[:300],
                          "author": str(row.get("author", ""))[:100], 'author_uid': str(row.get('mid') or ''),
                          "duration_label": str(row.get("duration", ""))[:30]})
            if len(items) == limit:
                break
        return {"keyword": keyword.strip(), "candidates": items, "source": "bilibili_search", "page": page}

    async def fetch(self, params):
        bvid, page = params.get("bvid"), params.get("page", 1)
        language = params.get("language", "zh-CN")
        if not isinstance(bvid, str) or not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid):
            raise RpcError(-32602, "INVALID_BVID")
        if type(page) is not int or not 1 <= page <= 1000 or not isinstance(language, str) or len(language) > 30:
            raise RpcError(-32602, "INVALID_PAGE_OR_LANGUAGE")
        from bilibili_api import video
        item = video.Video(bvid=bvid, credential=self.credential())
        info = await item.get_info()
        pages = info.get("pages", [])
        part = next((p for p in pages if p.get("page") == page), None)
        if not part:
            raise RpcError(-32602, "VIDEO_PART_NOT_FOUND")
        cid = str(part["cid"])
        metadata = await item.get_subtitle(cid=int(cid))
        tracks = metadata.get("subtitles", []) if isinstance(metadata, dict) else []
        if not tracks:
            raise RpcError(-32010, "NO_SUBTITLE_OR_LOGIN_REQUIRED")
        priorities = [language, "zh-CN", "zh-Hans", "ai-zh", "zh-TW", "zh-Hant"]
        tracks = [track for track in tracks if isinstance(track, dict) and track.get("subtitle_url")]
        if not tracks:
            raise RpcError(-32010, "NO_SUBTITLE_URL")
        selected = min(tracks, key=lambda track: priorities.index(track.get("lan")) if track.get("lan") in priorities else len(priorities))
        subtitles = await download_subtitles(selected["subtitle_url"])
        result = validate_video({"bvid": bvid, "cid": cid, "title": str(info.get("title", ""))[:300],
                                 "duration": part["duration"], "subtitles": subtitles, "page": page,
                                 'author': str((info.get('owner') or {}).get('name', ''))[:100],
                                 'author_uid': str((info.get('owner') or {}).get('mid') or ''), 'cover': info.get('pic', '')})
        result.update({"page": page, "subtitle_language": selected.get("lan"),
                       "subtitle_generated": str(selected.get("lan", "")).startswith("ai-"), "source": "bilibili_subtitles"})
        return result
