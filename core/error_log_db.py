# -*- coding: utf-8 -*-
"""에러 로그 저장소 (SQLite).

디스코드 채널로 알림을 보낸 주요 에러를 함께 기록해, 웹 설정 페이지의
"사용 로그" 화면에서 최근 에러 이력을 확인할 수 있게 한다.
playlists.db 파일을 공유한다.
"""

import os
import sqlite3

DB_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "playlists.db")

MAX_ROWS_PER_GUILD = 200


def _get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS error_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            message TEXT NOT NULL,
            occurred_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_error_log_guild ON error_log(guild_id, id)")
    return conn


def _add(guild_id, message: str, occurred_at: str) -> None:
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO error_log (guild_id, message, occurred_at) VALUES (?, ?, ?)",
            (guild_id, message, occurred_at),
        )
        if guild_id is not None:
            conn.execute(
                """
                DELETE FROM error_log
                WHERE guild_id = ? AND id NOT IN (
                    SELECT id FROM error_log WHERE guild_id = ? ORDER BY id DESC LIMIT ?
                )
                """,
                (guild_id, guild_id, MAX_ROWS_PER_GUILD),
            )


def _list_recent(guild_id: int, limit: int = 20):
    with _get_conn() as conn:
        return conn.execute(
            "SELECT message, occurred_at FROM error_log WHERE guild_id = ? ORDER BY id DESC LIMIT ?",
            (guild_id, limit),
        ).fetchall()


# ---- 동기 API (Flask 등 동기 컨텍스트용) ----
list_recent_errors_sync = _list_recent


# ---- discord.py(비동기) 쪽에서 쓰는 executor 래퍼 ----
async def log_error(loop, guild_id, message: str, occurred_at: str) -> None:
    await loop.run_in_executor(None, _add, guild_id, message, occurred_at)
