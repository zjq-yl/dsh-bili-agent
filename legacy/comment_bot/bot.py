"""运行：python bot.py --config config.toml --once；真实发送需 --send。"""
import argparse
import asyncio
import json
import logging
import os
import re
import sqlite3
import time
import tomllib
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

if __package__:
    from .adapters import Bilibili, DshResponder
else:
    from adapters import Bilibili, DshResponder

LOG = logging.getLogger("bili-bot")
CHINA_TIME = timezone(timedelta(hours=8))


def load_config(path):
    path = Path(path).resolve()
    with path.open("rb") as file:
        cfg = tomllib.load(file)
    if not cfg.get("videos") or not isinstance(cfg["videos"], list) or any(
        not isinstance(v, str) or not re.fullmatch(r"BV[0-9A-Za-z]{10}", v)
        for v in cfg["videos"]
    ):
        raise ValueError("videos 必须填写有效 BV 号列表")
    if not isinstance(cfg.get("keywords"), list) or any(
        not isinstance(k, str) or not k.strip() for k in cfg["keywords"]
    ):
        raise ValueError("keywords 必须为非空字符串列表；空列表表示只响应 @")
    bounds = {
        "poll_seconds": (10, 86400), "pages_per_video": (1, 20),
        "max_per_poll": (1, 100), "max_per_day": (1, 1000),
        "send_interval_seconds": (10, 86400), "max_comment_age_seconds": (1, 604800),
        "max_reply_chars": (1, 500), "model_timeout_seconds": (10, 600),
    }
    for key, (low, high) in bounds.items():
        value = cfg.get(key)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{key} 必须是 {low} 至 {high} 的整数")
    for key in ("database", "dsh_home", "dsh_workspace", "model", "persona"):
        if not isinstance(cfg.get(key), str) or not cfg[key].strip():
            raise ValueError(f"缺少配置：{key}")
    for key in ("database", "dsh_home", "dsh_workspace"):
        cfg[key] = str((path.parent / cfg[key]).resolve())
    return cfg


@contextmanager
def single_instance(database):
    """同一数据库只运行一个实例；操作系统在进程退出后自动释放锁。"""
    path = Path(database + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as file:
        if file.tell() == 0:
            file.write(b"0")
            file.flush()
        file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("已有机器人使用此数据库运行") from None
        try:
            yield
        finally:
            file.seek(0)
            if os.name == "nt":
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(file, fcntl.LOCK_UN)


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE IF NOT EXISTS replies (
            account INTEGER, bvid TEXT, rpid INTEGER, mode TEXT, status TEXT,
            text TEXT, error TEXT, remote_id TEXT, attempted_at REAL,
            day TEXT, updated_at REAL,
            PRIMARY KEY (account, bvid, rpid, mode)
        )""")
        # 崩溃前已经发起的请求不能再次发送；尚未生成完的任务保留为失败。
        with self.db:
            self.db.execute("UPDATE replies SET status='uncertain' WHERE status='sending'")
            self.db.execute("UPDATE replies SET status='failed' WHERE status='generating'")

    def claim(self, key):
        with self.db:
            result = self.db.execute(
                "INSERT OR IGNORE INTO replies "
                "(account,bvid,rpid,mode,status,updated_at) VALUES (?,?,?,?,'generating',?)",
                (*key, time.time()),
            )
        return result.rowcount == 1

    def update(self, key, status, text=None, error=None, remote_id=None):
        with self.db:
            self.db.execute(
                "UPDATE replies SET status=?,text=?,error=?,remote_id=?,updated_at=? "
                "WHERE account=? AND bvid=? AND rpid=? AND mode=?",
                (status, text, error, remote_id, time.time(), *key),
            )

    def begin_send(self, key, text, now, day):
        with self.db:
            self.db.execute(
                "UPDATE replies SET status='sending',text=?,attempted_at=?,day=?,updated_at=? "
                "WHERE account=? AND bvid=? AND rpid=? AND mode=?",
                (text, now, day, now, *key),
            )

    def allowance(self, account, day, now, cfg):
        count = self.db.execute(
            "SELECT COUNT(*) FROM replies WHERE account=? AND mode='live' AND day=?",
            (account, day),
        ).fetchone()[0]
        last = self.db.execute(
            "SELECT MAX(attempted_at) FROM replies WHERE account=? AND mode='live'",
            (account,),
        ).fetchone()[0]
        return count < cfg["max_per_day"] and (
            last is None or now - last >= cfg["send_interval_seconds"]
        )


def eligible(comment, account, cfg, now):
    if int(comment["mid"]) == account or int(comment.get("root", 0)) != 0:
        return False
    age = now - int(comment["ctime"])
    message = str(comment["content"]["message"]).strip()
    if not message or not 0 <= age <= cfg["max_comment_age_seconds"]:
        return False
    mentions = comment["content"].get("members") or []
    return any(str(m.get("mid")) == str(account) for m in mentions) or any(
        word.casefold() in message.casefold() for word in cfg["keywords"]
    )


async def poll(cfg, store, bili, responder, account, send):
    processed = 0
    for bvid in cfg["videos"]:
        try:
            info, comments = await bili.fetch(bvid, cfg["pages_per_video"])
        except Exception as exc:
            # 第三方异常可能包含请求头，日志只记录异常类型。
            LOG.error("读取 %s 失败：%s", bvid, type(exc).__name__)
            continue
        for comment in comments:
            now = time.time()
            if processed >= cfg["max_per_poll"]:
                return processed
            day = datetime.fromtimestamp(now, CHINA_TIME).date().isoformat()
            if send and not store.allowance(account, day, now, cfg):
                return processed
            try:
                if not eligible(comment, account, cfg, now):
                    continue
                rpid = int(comment["rpid"])
                if rpid <= 0:
                    raise ValueError("无效评论 ID")
            except (KeyError, ValueError, TypeError):
                LOG.warning("忽略格式异常的评论，视频 %s", bvid)
                continue
            key = (account, bvid, rpid, "live" if send else "preview")
            if not store.claim(key):
                continue
            processed += 1
            try:
                text = await responder.reply(info, comment)
            except Exception as exc:
                store.update(key, "failed", error=type(exc).__name__)
                LOG.error("生成失败 rpid=%s：%s", rpid, type(exc).__name__)
                continue
            if text is None:
                store.update(key, "skipped")
            elif not send:
                store.update(key, "preview", text=text)
                print(json.dumps({"mode": "preview", "bvid": bvid, "rpid": rpid,
                                  "reply": text}, ensure_ascii=False), flush=True)
            else:
                now = time.time()
                day = datetime.fromtimestamp(now, CHINA_TIME).date().isoformat()
                store.begin_send(key, text, now, day)
                try:
                    remote_id = await bili.send(int(info["aid"]), rpid, text)
                except Exception as exc:
                    store.update(key, "uncertain", text=text, error=type(exc).__name__)
                    LOG.error("发送结果待确认 rpid=%s，停止本轮发送", rpid)
                    return processed
                store.update(key, "sent", text=text, remote_id=remote_id)
                LOG.info("已回复 rpid=%s，回复 ID=%s", rpid, remote_id)
    return processed


async def run(cfg, once, send):
    for name in ("BILI_SESSDATA", "DEEPSEEK_API_KEY"):
        if not os.environ.get(name):
            raise ValueError(f"请设置环境变量 {name}")
    if send and not os.environ.get("BILI_JCT"):
        raise ValueError("发送模式需要 BILI_JCT")
    bili = Bilibili()
    account = await bili.identity()
    if account <= 0:
        raise ValueError("登录状态无效")
    LOG.info("账号 UID=%s，模式=%s", account, "真实发送" if send else "仅预览")
    store = Store(cfg["database"])
    responder = DshResponder(cfg)
    try:
        while True:
            count = await poll(cfg, store, bili, responder, account, send)
            LOG.info("本轮处理 %s 条评论", count)
            if once:
                return
            await asyncio.sleep(cfg["poll_seconds"])
    finally:
        store.db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--once", action="store_true", help="只扫描一轮")
    parser.add_argument("--send", action="store_true", help="真实发送，默认只预览")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = load_config(args.config)
        with single_instance(cfg["database"]):
            asyncio.run(run(cfg, args.once, args.send))
    except KeyboardInterrupt:
        LOG.info("已停止")
    except (ValueError, FileNotFoundError, ModuleNotFoundError) as exc:
        LOG.error("启动失败：%s", exc)
        return 1
    except Exception as exc:
        LOG.error("运行失败：%s；请检查依赖、配置和登录状态", type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
