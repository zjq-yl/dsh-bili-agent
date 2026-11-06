import sys
import asyncio
import json
import errno
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bili_agent.platform import Platform, subtitle_url
from bili_agent.protocol import RpcError


class PlatformTests(unittest.IsolatedAsyncioTestCase):
    async def test_mention_context_checks_author_and_send_uses_root_parent(self):
        root = {'rpid': 100, 'oid': 170001, 'mid': 33, 'type': 1, 'root': 0, 'content': {'message': '公开上下文'}}
        reply = {'rpid': 101, 'oid': 170001, 'mid': 22, 'type': 1, 'root': 100, 'content': {'message': '@角色 你好'}}
        comments = AsyncMock(return_value={'root': root, 'replies': [reply]})
        send = AsyncMock(return_value={'rpid': 900})
        sdk = SimpleNamespace(comment=SimpleNamespace(Comment=Mock(return_value=SimpleNamespace(get_sub_comments=comments)),
            send_comment=send, CommentResourceType=SimpleNamespace(VIDEO=1)),
            video=SimpleNamespace(Video=Mock(return_value=SimpleNamespace(get_bvid=lambda: 'BV17x411w7KC', get_aid=lambda: 170001))))
        settings = SimpleNamespace(get_wbi_retry_times=Mock(return_value=3), set_wbi_retry_times=Mock())
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {
                'bilibili_api': sdk, 'bilibili_api.utils': SimpleNamespace(),
                'bilibili_api.utils.network': SimpleNamespace(request_settings=settings)}):
            platform = Platform(Path(directory))
            platform.credential = lambda: 'test'
            params = {'oid': '170001', 'root': '100', 'parent': '101', 'peer': '22'}
            context = await platform.mention_context(params)
            self.assertEqual(context['root_text'], '公开上下文')
            await platform.send_comment({'bvid': context['bvid'], 'text': '你好', 'root': '100', 'parent': '101'})
            self.assertEqual(send.call_args.kwargs['root'], 100)
            self.assertEqual(send.call_args.kwargs['parent'], 101)
            with self.assertRaisesRegex(RpcError, 'MENTION_CONTEXT_MISMATCH'):
                await platform.mention_context({**params, 'peer': '44'})
            with self.assertRaisesRegex(RpcError, 'MENTION_COMMENT_UNAVAILABLE'):
                await platform.mention_context({**params, 'parent': '102'})

    async def test_contact_lists_classification_pagination_and_invalid_payload(self):
        mutual = AsyncMock(return_value={'list': [{'mid': n, 'uname': f'好友{n}', 'attribute': 6} for n in range(100, 151)]})
        followers = AsyncMock(return_value={'list': [
            {'mid': 22, 'attribute': 2}, {'mid': 33, 'attribute': 6}, {'mid': 44, 'attribute': 128}], 'total': 1500})
        following = AsyncMock(return_value={'list': [{'mid': 55, 'attribute': 2}], 'total': 1})
        user = SimpleNamespace(get_self_friends=mutual, User=Mock(return_value=SimpleNamespace(
            get_followers=followers, get_followings=following)))
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {'bilibili_api': SimpleNamespace(user=user)}):
            platform = Platform(Path(directory))
            platform.credential = lambda: 'test'
            params = {'account_uid': '77'}
            first = await platform.contacts(params)
            self.assertEqual(len(first['items']), 50)
            self.assertTrue(first['has_more'])
            last = await platform.contacts({**params, 'page': 2})
            self.assertEqual([item['peer'] for item in last['items']], ['150'])
            self.assertFalse(last['has_more'])
            fans = await platform.contacts({**params, 'kind': 'followers', 'page': 20})
            self.assertEqual([item['peer'] for item in fans['items']], ['22'])
            self.assertTrue(fans['limited'])
            self.assertFalse(fans['has_more'])
            followers.assert_awaited_once_with(pn=20, ps=50)
            own = await platform.contacts({**params, 'kind': 'following'})
            self.assertEqual(own['items'][0]['peer'], '55')
            following.assert_awaited_once_with(pn=1, ps=50)
            for invalid in ({'kind': 'invalid'}, {'page': True}, {'page': 0}, {'account_uid': 'bad'}):
                with self.assertRaisesRegex(RpcError, 'INVALID_CONTACT_PAGE'):
                    await platform.contacts({**params, **invalid})
            mutual.return_value = {'list': [{'mid': 22, 'attribute': 2}]}
            with self.assertRaisesRegex(RpcError, 'INVALID_RELATION_RESPONSE'):
                await platform.contacts(params)

    async def test_relation_normalizes_both_directions_and_follow_uses_sdk_once(self):
        relation = AsyncMock()
        modify = AsyncMock(return_value={})
        target = SimpleNamespace(get_relation=relation, modify_relation=modify)
        user = SimpleNamespace(User=Mock(return_value=target), RelationType=SimpleNamespace(SUBSCRIBE=1))
        settings = SimpleNamespace(get_wbi_retry_times=Mock(return_value=3), set_wbi_retry_times=Mock())
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {
                'bilibili_api': SimpleNamespace(user=user), 'bilibili_api.utils': SimpleNamespace(),
                'bilibili_api.utils.network': SimpleNamespace(request_settings=settings)}):
            platform = Platform(Path(directory))
            platform.credential = lambda: 'test-credential'
            for own, other, expected in [(0, 0, False), (2, 0, False), (0, 2, False), (6, 6, True), (2, 2, True), (128, 6, False)]:
                relation.return_value = {'relation': {'attribute': own}, 'be_relation': {'attribute': other}}
                self.assertEqual((await platform.relation({'peer_uid': '22'}))['mutual'], expected)
            for invalid in ({}, {'relation': {'attribute': 6}}, {'relation': {'attribute': True}, 'be_relation': {'attribute': 6}}):
                relation.return_value = invalid
                with self.assertRaisesRegex(RpcError, 'INVALID_RELATION_RESPONSE'):
                    await platform.relation({'peer_uid': '22'})
            await platform.follow({'peer_uid': '22'})
            modify.assert_awaited_once_with(1)
            user.User.assert_called_with(22, credential='test-credential')
            self.assertEqual([call.args[0] for call in settings.set_wbi_retry_times.call_args_list], [1, 3])
            for peer in ('', '0', '-1', '22;anything', 22):
                with self.assertRaisesRegex(RpcError, 'INVALID_UID'):
                    await platform.follow({'peer_uid': peer})

    async def test_popular_feed_uses_sdk_and_normalizes_candidates(self):
        hot = SimpleNamespace(get_hot_videos=AsyncMock(return_value={'list': [
            {'bvid': 'BV17x411w7KC', 'title': '热门视频', 'duration': 100, 'owner': {'mid': 123, 'name': 'UP'}},
            {'bvid': 'invalid'}, None]}))
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {'bilibili_api': SimpleNamespace(hot=hot)}):
            platform = Platform(Path(directory))
            result = await platform.request('popular', {'page': 2, 'limit': 20})
            hot.get_hot_videos.assert_awaited_once_with(pn=2, ps=20)
            self.assertEqual(result['source'], 'bilibili_popular')
            self.assertEqual(len(result['candidates']), 1)
            self.assertEqual(result['candidates'][0]['author_uid'], '123')
            self.assertEqual(result['candidates'][0]['duration'], 100)
            for invalid in ({'page': True}, {'limit': 21}, {'page': 0}):
                with self.assertRaisesRegex(RpcError, 'INVALID_SEARCH_PAGE'):
                    await platform.popular(invalid)

    async def test_public_comment_uses_video_resource_and_returns_receipt(self):
        send = AsyncMock(return_value={'rpid': 123456})
        settings = SimpleNamespace(get_wbi_retry_times=Mock(return_value=3), set_wbi_retry_times=Mock())
        sdk = SimpleNamespace(comment=SimpleNamespace(send_comment=send, CommentResourceType=SimpleNamespace(VIDEO=1)),
            video=SimpleNamespace(Video=Mock(return_value=SimpleNamespace(get_aid=lambda: 170001))))
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {
                'bilibili_api': sdk, 'bilibili_api.utils': SimpleNamespace(),
                'bilibili_api.utils.network': SimpleNamespace(request_settings=settings)}):
            platform = Platform(Path(directory))
            credential = object()
            platform.credential = lambda: credential
            result = await platform.send_comment({'bvid': 'BV17x411w7KC', 'text': '有具体例子，便于理解。'})
            self.assertEqual(result, {'rpid': '123456'})
            self.assertEqual(send.call_args.args[1:], (170001, 1))
            self.assertIs(send.call_args.kwargs['credential'], credential)
            self.assertEqual([call.args[0] for call in settings.set_wbi_retry_times.call_args_list], [1, 3])

            for receipt in ({'rpid_str': '12345678901234567890', 'rpid': 123},
                            {'reply': {'rpid_str': '12345678901234567890'}}):
                send.return_value = receipt
                self.assertEqual(await platform.send_comment({'bvid': 'BV17x411w7KC', 'text': '测试正文'}),
                                 {'rpid': '12345678901234567890'})
            for receipt, error in [({'need_captcha': 1, 'rpid': 123}, 'PLATFORM_CHALLENGE'),
                                   ({'reply': 'invalid'}, 'COMMENT_RECEIPT_MISSING'),
                                   ({'rpid': 0}, 'COMMENT_RECEIPT_MISSING'),
                                   (None, 'COMMENT_RECEIPT_MISSING')]:
                send.return_value = receipt
                send.reset_mock()
                with self.assertRaisesRegex(RpcError, error):
                    await platform.send_comment({'bvid': 'BV17x411w7KC', 'text': '测试正文'})
                self.assertEqual(send.await_count, 1)  # Never replay a possibly accepted write.
                self.assertEqual(settings.set_wbi_retry_times.call_args.args, (3,))

    async def test_platform_risk_codes_are_reported_for_interactions(self):
        with tempfile.TemporaryDirectory() as directory:
            platform = Platform(Path(directory))
            for operation in ('send_comment', 'video_action'):
                for code in (-352, -412):
                    error = Exception('platform risk response')
                    error.code = code
                    handler = AsyncMock(side_effect=error)
                    setattr(platform, operation, handler)
                    platform.next_request = 0
                    with self.assertRaisesRegex(RpcError, '^PLATFORM_CHALLENGE$'):
                        await platform.request(operation, {})
                    handler.assert_awaited_once()

    async def test_slow_video_fetch_does_not_hold_private_message_lane(self):
        with tempfile.TemporaryDirectory() as directory:
            platform = Platform(Path(directory))
            entered, release = asyncio.Event(), asyncio.Event()
            async def slow(params):
                entered.set()
                await release.wait()
                return {}
            platform.fetch = slow
            platform.identity = AsyncMock(return_value={'uid': '77'})
            video = asyncio.create_task(platform.request('fetch', {}))
            try:
                await entered.wait()
                platform.next_request = 0  # Advance the shared rate gate, not the in-flight video.
                result = await asyncio.wait_for(platform.request('identity', {}), 1)
                self.assertEqual(result['uid'], '77')
                self.assertFalse(video.done())
            finally:
                release.set()
                await video

    async def test_video_card_uses_type_seven_and_platform_metadata(self):
        captured = {}
        class Api:
            def __init__(self, **kwargs):
                captured['api'] = kwargs
            def update_params(self, **kwargs):
                captured['params'] = kwargs
                return self
            def update_data(self, **kwargs):
                captured['data'] = kwargs
                return self
            @property
            async def result(self):
                return {'msg_key': '12345'}
        settings = SimpleNamespace(get_wbi_retry_times=Mock(return_value=3), set_wbi_retry_times=Mock())
        sdk = SimpleNamespace(session=SimpleNamespace(API={'operate': {'send_msg': {'url': 'test', 'method': 'POST'}}}))
        modules = {'bilibili_api': sdk, 'bilibili_api.utils': SimpleNamespace(),
                   'bilibili_api.utils.network': SimpleNamespace(Api=Api, request_settings=settings)}
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, modules):
            platform = Platform(Path(directory))
            platform.credential = lambda: SimpleNamespace(raise_for_no_sessdata=lambda: None, raise_for_no_bili_jct=lambda: None)
            platform.video_part = AsyncMock(return_value=(SimpleNamespace(get_aid=lambda: 170001),
                {'title': '真实标题', 'pic': 'https://i0.hdslb.com/test.jpg', 'owner': {'name': '作者'}}, {}))
            result = await platform.share_video({'account_uid': '77', 'peer_uid': '22', 'bvid': 'BV17x411w7KC'})
            self.assertEqual(result['msg_key'], '12345')
            self.assertEqual(captured['data']['msg[msg_type]'], 7)
            card = json.loads(captured['data']['msg[content]'])
            self.assertEqual((card['source'], card['title'], card['id']), (5, '真实标题', 170001))
            self.assertEqual(captured['params']['w_receiver_id'], 22)
            self.assertEqual([call.args[0] for call in settings.set_wbi_retry_times.call_args_list], [1, 3])

    async def test_dislike_without_app_token_never_calls_like_false_and_favorite_uses_folder(self):
        item = SimpleNamespace(like=AsyncMock(), set_favorite=AsyncMock(), get_aid=lambda: 1)
        settings = SimpleNamespace(get_wbi_retry_times=Mock(return_value=3), set_wbi_retry_times=Mock())
        modules = {'bilibili_api': SimpleNamespace(video=SimpleNamespace(Video=lambda **kwargs: item)),
                   'bilibili_api.utils': SimpleNamespace(),
                   'bilibili_api.utils.network': SimpleNamespace(Api=Mock(), request_settings=settings)}
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, modules):
            platform = Platform(Path(directory))
            platform.credential = lambda: object()
            with self.assertRaisesRegex(RpcError, 'DISLIKE_REQUIRES_APP_TOKEN'):
                await platform.video_action({'action': 'dislike', 'bvid': 'BV17x411w7KC'})
            item.like.assert_not_called()
            result = await platform.video_action({'action': 'favorite', 'bvid': 'BV17x411w7KC', 'folder_id': '123'})
            self.assertTrue(result['accepted'])
            item.set_favorite.assert_awaited_once_with(add_media_ids=[123])

    async def test_network_permission_failure_is_distinct_from_login_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            platform = Platform(Path(directory))
            platform.identity = AsyncMock(side_effect=PermissionError(errno.EACCES, 'restricted'))
            with self.assertRaisesRegex(RpcError, '^NETWORK_ACCESS_DENIED$'):
                await platform.request('identity', {})
            self.assertEqual(platform.last_status, 'NETWORK_ACCESS_DENIED')

    async def test_selects_requested_part_preserves_large_cid_and_prefers_manual_chinese(self):
        calls = []
        class Video:
            def __init__(self, **kwargs):
                calls.append(kwargs)

            async def get_info(self):
                return {"title": "测试分P", "pages": [{"page": 1, "cid": 1, "duration": 20},
                                                     {"page": 2, "cid": 9007199254740993, "duration": 30}]}

            async def get_subtitle(self, cid):
                calls.append(cid)
                return {"subtitles": [
                    {"lan": "ai-zh", "subtitle_url": "//aisubtitle.hdslb.com/ai.json"},
                    {"lan": "zh-CN", "subtitle_url": "//aisubtitle.hdslb.com/manual.json"}]}

        sdk = SimpleNamespace(Credential=lambda **kwargs: kwargs, video=SimpleNamespace(Video=Video))
        download = AsyncMock(return_value=[{"from": 0, "to": 10, "content": "内容"}])
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {"bilibili_api": sdk}), patch("bili_agent.platform.download_subtitles", download):
            platform = Platform(Path(directory))
            result = await platform.request("fetch", {"bvid": "BV17x411w7KC", "page": 2})
            self.assertEqual(result["cid"], "9007199254740993")
            self.assertEqual(result["page"], 2)
            self.assertFalse(result["subtitle_generated"])
            self.assertEqual(calls[1], 9007199254740993)
            download.assert_awaited_once_with("//aisubtitle.hdslb.com/manual.json")

    async def test_missing_subtitles_stops_without_inventing_content(self):
        sdk = SimpleNamespace(Credential=lambda **kwargs: kwargs, video=SimpleNamespace(Video=lambda **kwargs: SimpleNamespace(
            get_info=AsyncMock(return_value={"title": "test", "pages": [{"page": 1, "cid": 1, "duration": 20}]}),
            get_subtitle=AsyncMock(return_value={"subtitles": []}))))
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {"bilibili_api": sdk}), patch("bili_agent.platform.download_subtitles", AsyncMock()) as download:
            platform = Platform(Path(directory))
            with self.assertRaisesRegex(RpcError, "NO_SUBTITLE_OR_LOGIN_REQUIRED"):
                await platform.request("fetch", {"bvid": "BV17x411w7KC"})
            download.assert_not_called()

    async def test_search_only_video_candidates_and_strips_markup(self):
        search = SimpleNamespace(search_by_type=AsyncMock(return_value={"result": [
            {"bvid": "BV17x411w7KC", "title": '<em>学习</em>&amp;复习', "author": "作者"},
            {"bvid": "invalid", "title": "bad"}]}), SearchObjectType=SimpleNamespace(VIDEO="video"),
            OrderVideo=SimpleNamespace(TOTALRANK="totalrank"))
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {"bilibili_api": SimpleNamespace(search=search)}):
            result = await Platform(Path(directory)).request("search", {"keyword": "学习"})
            self.assertEqual(len(result["candidates"]), 1)
            self.assertEqual(result["candidates"][0]["title"], "学习&复习")
            self.assertEqual(search.search_by_type.call_args.kwargs["search_type"], "video")

    def test_subtitle_cdn_url_boundary(self):
        self.assertEqual(subtitle_url("//aisubtitle.hdslb.com/a.json"), "https://aisubtitle.hdslb.com/a.json")
        for url in ["http://aisubtitle.hdslb.com/a", "https://hdslb.com.evil.test/a", "file:///secret", "https://127.0.0.1/a", "https://user:pass@aisubtitle.hdslb.com/a"]:
            with self.assertRaises(RpcError):
                subtitle_url(url)

    async def test_private_send_uses_verified_uid_and_disables_sdk_retries(self):
        settings = SimpleNamespace(get_wbi_retry_times=Mock(return_value=3), set_wbi_retry_times=Mock())
        credential = SimpleNamespace(dedeuserid='stale')
        session = SimpleNamespace(send_msg=AsyncMock(return_value={'msg_key': '999'}), EventType=SimpleNamespace(TEXT=1))
        modules = {'bilibili_api': SimpleNamespace(session=session),
                   'bilibili_api.utils': SimpleNamespace(),
                   'bilibili_api.utils.network': SimpleNamespace(request_settings=settings)}
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, modules):
            platform = Platform(Path(directory))
            platform.credential = lambda: credential
            result = await platform.request('send_dm', {'account_uid': '77', 'peer_uid': '22', 'text': 'hello'})
            self.assertEqual(result['msg_key'], '999')
            self.assertEqual(credential.dedeuserid, '77')
            self.assertEqual(session.send_msg.call_args.kwargs['receiver_id'], 22)
            self.assertEqual([call.args[0] for call in settings.set_wbi_retry_times.call_args_list], [1, 3])

    async def test_audio_metadata_selects_requested_part_and_audio_only(self):
        item = SimpleNamespace(get_download_url=AsyncMock(return_value={'dash': {'audio': [
            {'id': 1, 'bandwidth': 100000, 'baseUrl': 'https://x.bilivideo.com/high'},
            {'id': 2, 'bandwidth': 64000, 'baseUrl': 'https://x.bilivideo.com/low'}]}}))
        with tempfile.TemporaryDirectory() as directory:
            platform = Platform(Path(directory))
            platform.video_part = AsyncMock(return_value=(item, {'title': 'test'}, {'cid': 9007199254740993, 'duration': 10}))
            result = await platform.request('audio', {'bvid': 'BV17x411w7KC', 'page': 2})
            self.assertEqual(result['cid'], '9007199254740993')
            self.assertEqual(result['page'], 2)
            self.assertEqual(result['track_id'], '2')
            item.get_download_url.assert_awaited_once_with(cid=9007199254740993)


if __name__ == "__main__":
    unittest.main()
