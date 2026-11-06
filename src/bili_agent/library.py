"""Read the persistent video library without exposing transcripts or making platform calls."""
import heapq
import json
import re
import sqlite3
from collections.abc import Iterator
from urllib.parse import urlsplit

from .protocol import RpcError

RATINGS = ('值得看', '可以看', '没必要看')


def cover_url(value: object) -> str:
    """Allow public Bilibili CDN covers only; missing or invalid artwork stays empty."""
    if not isinstance(value, str) or len(value) > 2000:
        return ''
    value = 'https:' + value if value.startswith('//') else value
    try:
        url = urlsplit(value)
        if (url.scheme == 'http' and (url.hostname or '').endswith('.hdslb.com')
                and not url.username and not url.password and url.port in (None, 80)):
            return url._replace(scheme='https', netloc=url.hostname).geturl()
        return value if (url.scheme == 'https' and (url.hostname or '').endswith('.hdslb.com')
                         and not url.username and not url.password and url.port in (None, 443)) else ''
    except ValueError:
        return ''


def records(db: sqlite3.Connection, account: str | None) -> Iterator[dict]:
    """Yield metadata and stored summaries, leaving large subtitle documents in SQLite."""
    for row in db.execute('''SELECT v.id,v.bvid,v.state,v.checkpoint,v.error,
        json_extract(v.document,'$.title') AS title,json_extract(v.document,'$.page') AS page,
        json_extract(v.document,'$.author') AS author,json_extract(v.document,'$.author_uid') AS author_uid,
        json_extract(v.document,'$.duration') AS duration,json_extract(v.document,'$.cover') AS cover,
        json_extract(v.result,'$.summary') AS summary,d.value AS assessment
        FROM videos v LEFT JOIN video_decisions d ON d.bvid=v.bvid AND d.account=? ORDER BY v.id DESC''', (account,)):
        item = dict(row)
        item['assessment'] = json.loads(item['assessment']) if item['assessment'] else None
        item['cover'] = cover_url(item['cover'])
        item['url'] = f"https://www.bilibili.com/video/{item['bvid']}/?p={item['page'] or 1}"
        yield item


def browse(db: sqlite3.Connection, account: str | None, query: str = '', rating: str = '', offset: int = 0, limit: int = 12) -> dict:
    """Search all stored videos and paginate by model rating, including unfinished work."""
    if (not isinstance(query, str) or len(query) > 100 or rating not in ('', 'unrated', *RATINGS)
            or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50):
        raise RpcError(-32602, 'INVALID_LIBRARY_QUERY')
    counts = {key: 0 for key in ('all', 'unrated', *RATINGS)}
    items, total = [], 0
    for item in records(db, account):
        haystack = ' '.join(str(item.get(k) or '') for k in ('bvid', 'title', 'author', 'summary')).casefold()
        if query.strip().casefold() not in haystack:
            continue
        category = (item['assessment'] or {}).get('rating')
        category = category if category in RATINGS else 'unrated'
        counts['all'] += 1
        counts[category] += 1
        if rating and category != rating:
            continue
        if offset <= total < offset + limit:
            item['summary'] = (item['summary'] or '')[:220]
            items.append(item)
        total += 1
    return {'items': items, 'total': total, 'counts': counts, 'offset': offset, 'limit': limit}


def history_candidates(db: sqlite3.Connection, account: str, query: str, limit: int = 20) -> list[dict]:
    """Recall likely matches from all completed videos; the model makes the final selection."""
    words = re.findall(r'[a-z0-9_+.-]+|[\u4e00-\u9fff]+', query.casefold())
    terms = set(words)
    for word in words:
        if re.fullmatch(r'[\u4e00-\u9fff]{3,}', word):
            terms.update(word[i:i+2] for i in range(len(word)-1))
    def score(item):
        title = item['title'].casefold()
        summary = (item['summary'] or '').casefold()
        return (sum(4 for word in terms if word in title) + sum(1 for word in terms if word in summary), item['id'])
    candidates = (item for item in records(db, account) if item['state'] == 'done')
    # Bounded model input; search the full local history, not just the latest status page.
    return heapq.nlargest(limit, candidates, key=score)
