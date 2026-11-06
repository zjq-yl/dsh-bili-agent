"""插件独立数据目录：进程锁、调用记账；不记录完整模型输入与凭据。"""
import json
import os
import sqlite3
import time
from pathlib import Path

from .protocol import RpcError


class Store:
    def __init__(self, data_dir):
        path = Path(data_dir)
        self.lock = (path / "worker.lock").open("a+b")
        if self.lock.tell() == 0:
            self.lock.write(b"0")
            self.lock.flush()
        self.lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise RpcError(-32004, "DATA_DIR_IN_USE") from None
        try:
            self.db = sqlite3.connect(path / "agent.sqlite3")
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS model_calls (
                    id INTEGER PRIMARY KEY, started REAL NOT NULL, purpose TEXT NOT NULL,
                    state TEXT NOT NULL, usage TEXT, error TEXT
                );
                UPDATE model_calls SET state='uncertain', error='WORKER_RESTARTED'
                    WHERE state='running';
                CREATE TABLE IF NOT EXISTS activity_events (
                    id INTEGER PRIMARY KEY, created REAL NOT NULL, category TEXT NOT NULL,
                    message TEXT NOT NULL, reference TEXT, error TEXT
                );
            """)
        except BaseException:
            self.lock.close()
            raise

    def event(self, category: str, message: str, reference: str = '', error: str | None = None) -> None:
        """Persist bounded operational events, never prompts, private messages or credentials."""
        with self.db:
            self.db.execute('INSERT INTO activity_events(created,category,message,reference,error) VALUES(?,?,?,?,?)',
                            (time.time(), category, message, str(reference), error))
            self.db.execute('DELETE FROM activity_events WHERE id <= (SELECT MAX(id)-500 FROM activity_events)')

    def events(self, category: str | None = None) -> list[dict]:
        """Return the latest operational events for the authenticated local panel."""
        if category is not None:
            return [dict(row) for row in self.db.execute('SELECT * FROM activity_events WHERE category=? ORDER BY id DESC LIMIT 100', (category,))]
        return [dict(row) for row in self.db.execute('SELECT * FROM activity_events ORDER BY id DESC LIMIT 100')]

    def reserve_call(self, purpose):
        # Keep usage accounting without imposing a local daily request limit.
        with self.db:
            call_id = self.db.execute("INSERT INTO model_calls(started,purpose,state) VALUES(?,?,'running')",
                                      (time.time(), purpose)).lastrowid
        self.event('model', '开始模型调用', purpose)
        return call_id

    def finish_call(self, call_id, state, usage=None, error=None):
        with self.db:
            self.db.execute("UPDATE model_calls SET state=?,usage=?,error=? WHERE id=?",
                            (state, json.dumps(usage) if usage is not None else None, error, call_id))
        self.event('model', '模型调用完成' if state == 'completed' else '模型调用未完成', str(call_id), error)

    def call_status(self):
        return [dict(row) for row in self.db.execute(
            "SELECT id,started,purpose,state,usage,error FROM model_calls ORDER BY id DESC LIMIT 10")]

    def close(self):
        self.db.close()
        self.lock.close()
