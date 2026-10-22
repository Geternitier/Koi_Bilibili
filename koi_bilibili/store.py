"""插件自己的 SQLite。只记元数据、动作摘要和去重，不记 cookie，不记别人的评论正文。"""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def close(self) -> None:
        self._db.close()

    def _migrate(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bvid TEXT NOT NULL,
                action TEXT NOT NULL,
                rpid TEXT,
                at TEXT NOT NULL,
                ok INTEGER NOT NULL,
                error_code INTEGER,
                args_hash TEXT
            );
            CREATE TABLE IF NOT EXISTS msgfeed_seen (
                item_id TEXT PRIMARY KEY,
                at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS quota (
                bucket TEXT NOT NULL,
                action TEXT NOT NULL,
                bvid TEXT NOT NULL,
                n INTEGER NOT NULL,
                PRIMARY KEY (bucket, action, bvid)
            );
            CREATE TABLE IF NOT EXISTS approvals (
                id TEXT PRIMARY KEY,
                args_hash TEXT NOT NULL,
                state TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS inbound (
                message_id TEXT PRIMARY KEY,
                conversation TEXT NOT NULL,
                at TEXT NOT NULL
            );
            """
        )
        self._db.commit()

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return default if row is None else str(row["value"])

    def set(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._db.commit()

    def set_owner(self, mid: int, uname: str, source: str, at: str) -> None:
        self.set("owner_mid", str(mid))
        self.set("owner_uname", uname)
        self.set("owner_source", source)
        self.set("owner_bound_at", at)

    def owner_mid(self) -> int | None:
        raw = self.get("owner_mid")
        return int(raw) if raw else None

    def owner_uname(self) -> str:
        return self.get("owner_uname") or ""

    def set_account(self, mid: int, uname: str, online: bool, blocked: bool) -> None:
        self.set("account_mid", str(mid))
        self.set("account_uname", uname)
        self.set("account_online", "1" if online else "0")
        self.set("account_blocked", "1" if blocked else "0")

    def account_mid(self) -> int | None:
        raw = self.get("account_mid")
        return int(raw) if raw else None

    def set_grant(self, payload: str) -> None:
        self.set("autonomy_grant", payload)

    def grant(self) -> str | None:
        return self.get("autonomy_grant")

    def clear_grant(self) -> None:
        self._db.execute("DELETE FROM meta WHERE key = ?", ("autonomy_grant",))
        self._db.commit()

    def next_fresh_idx(self) -> int:
        current = int(self.get("fresh_idx") or "0") + 1
        self.set("fresh_idx", str(current))
        return current

    def watch_progress(self, bvid: str) -> int:
        return int(self.get(f"progress:{bvid}") or "0")

    def set_watch_progress(self, bvid: str, seconds: int) -> None:
        self.set(f"progress:{bvid}", str(max(0, seconds)))

    def watched_today(self, day: str) -> int:
        return int(self.get(f"watch:{day}") or "0")

    def add_watched(self, day: str, seconds: int) -> None:
        total = self.watched_today(day) + max(0, seconds)
        self.set(f"watch:{day}", str(total))

    def add_quota(self, bucket: str, action: str, bvid: str, n: int = 1) -> None:
        self._db.execute(
            """
            INSERT INTO quota(bucket, action, bvid, n) VALUES(?, ?, ?, ?)
            ON CONFLICT(bucket, action, bvid) DO UPDATE SET n = n + excluded.n
            """,
            (bucket, action, bvid, n),
        )
        self._db.commit()

    def quota_total(self, bucket: str, action: str) -> int:
        row = self._db.execute(
            "SELECT COALESCE(SUM(n), 0) AS n FROM quota WHERE bucket = ? AND action = ?",
            (bucket, action),
        ).fetchone()
        return int(row["n"])

    def quota_of(self, bucket: str, action: str, bvid: str) -> int:
        row = self._db.execute(
            "SELECT n FROM quota WHERE bucket = ? AND action = ? AND bvid = ?",
            (bucket, action, bvid),
        ).fetchone()
        return 0 if row is None else int(row["n"])

    def record_action(
        self,
        *,
        bvid: str,
        action: str,
        at: str,
        ok: bool,
        rpid: str = "",
        error_code: int | None = None,
        args_hash: str = "",
    ) -> None:
        self._db.execute(
            """
            INSERT INTO actions(bvid, action, rpid, at, ok, error_code, args_hash)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (bvid, action, rpid, at, 1 if ok else 0, error_code, args_hash),
        )
        self._db.commit()

    def last_action_at(self, action: str) -> datetime | None:
        row = self._db.execute(
            "SELECT at FROM actions WHERE action = ? AND ok = 1 ORDER BY id DESC LIMIT 1",
            (action,),
        ).fetchone()
        if row is None:
            return None
        return datetime.fromisoformat(row["at"])

    def recent_comment_hashes(self, limit: int = 20) -> list[str]:
        rows = self._db.execute(
            """
            SELECT args_hash FROM actions
            WHERE action = 'comment' AND ok = 1 AND args_hash != ''
            ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [str(row["args_hash"]) for row in rows]

    def owns_comment(self, rpid: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM actions WHERE action = 'comment' AND ok = 1 AND rpid = ?",
            (rpid,),
        ).fetchone()
        return row is not None

    def save_approval(self, approval_id: str, args_hash: str) -> None:
        self._db.execute(
            """
            INSERT INTO approvals(id, args_hash, state) VALUES(?, ?, 'pending')
            ON CONFLICT(id) DO UPDATE SET args_hash = excluded.args_hash
            """,
            (approval_id, args_hash),
        )
        self._db.commit()

    def permit(self, approval_id: str) -> None:
        self._db.execute("UPDATE approvals SET state = 'ok' WHERE id = ?", (approval_id,))
        self._db.commit()

    def take_permit(self, args_hash: str) -> bool:
        row = self._db.execute(
            "SELECT id FROM approvals WHERE args_hash = ? AND state = 'ok' ORDER BY id LIMIT 1",
            (args_hash,),
        ).fetchone()
        if row is None:
            return False
        self._db.execute("UPDATE approvals SET state = 'used' WHERE id = ?", (row["id"],))
        self._db.commit()
        return True

    def seen(self, item_id: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM msgfeed_seen WHERE item_id = ?", (item_id,)
        ).fetchone()
        return row is not None

    def mark_seen(self, item_id: str, at: str) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO msgfeed_seen(item_id, at) VALUES(?, ?)",
            (item_id, at),
        )
        self._db.commit()
        self._trim_seen()

    def _trim_seen(self) -> None:
        self._db.execute(
            """
            DELETE FROM msgfeed_seen WHERE item_id NOT IN (
                SELECT item_id FROM msgfeed_seen ORDER BY at DESC LIMIT 500
            )
            """
        )
        self._db.commit()

    def remember_inbound(self, message_id: str, conversation: str, at: datetime) -> None:
        self._db.execute(
            """
            INSERT INTO inbound(message_id, conversation, at) VALUES(?, ?, ?)
            ON CONFLICT(message_id) DO UPDATE SET at = excluded.at
            """,
            (message_id, conversation, at.isoformat()),
        )
        self._db.commit()

    def inbound_at(self, message_id: str) -> datetime | None:
        row = self._db.execute(
            "SELECT at FROM inbound WHERE message_id = ?", (message_id,)
        ).fetchone()
        if row is None:
            return None
        return datetime.fromisoformat(row["at"])

    def cursor(self, kind: str) -> str | None:
        return self.get(f"cursor:{kind}")

    def set_cursor(self, kind: str, value: str) -> None:
        self.set(f"cursor:{kind}", value)


def within(moment: datetime | None, now: datetime, seconds: float) -> bool:
    if moment is None:
        return False
    return now - moment < timedelta(seconds=seconds)
