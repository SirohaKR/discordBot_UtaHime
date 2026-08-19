# -*- coding: utf-8 -*-
"""재생 기록 저장소 (SQLite).

곡이 재생을 시작할 때마다 한 줄씩 남겨 웹 설정 페이지의 "사용 로그" 화면에서
최근 재생 이력을 보여주기 위한 용도. playlists.db 파일을 공유한다.
"""

import os
import sqlite3

DB_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "playlists.db")

MAX_ROWS_PER_GUILD = 500


def _get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS playback_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            url TEXT,
            requester TEXT,
            played_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_playback_log_guild ON playback_log(guild_id, id)")
    return conn


def _add(guild_id: int, title: str, url: str, requester: str, played_at: str) -> None:
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO playback_log (guild_id, title, url, requester, played_at) VALUES (?, ?, ?, ?, ?)",
            (guild_id, title, url, requester, played_at),
        )
        # 길드당 최근 MAX_ROWS_PER_GUILD건만 남기고 오래된 기록은 정리한다.
        conn.execute(
            """
            DELETE FROM playback_log
            WHERE guild_id = ? AND id NOT IN (
                SELECT id FROM playback_log WHERE guild_id = ? ORDER BY id DESC LIMIT ?
            )
            """,
            (guild_id, guild_id, MAX_ROWS_PER_GUILD),
        )


def _list_recent(guild_id: int, limit: int = 50):
    with _get_conn() as conn:
        return conn.execute(
            "SELECT title, url, requester, played_at FROM playback_log WHERE guild_id = ? ORDER BY id DESC LIMIT ?",
            (guild_id, limit),
        ).fetchall()


# ---- 동기 API (Flask 등 동기 컨텍스트용) ----
list_recent_playback_sync = _list_recent


# ---- discord.py(비동기) 쪽에서 쓰는 executor 래퍼 ----
async def log_playback(loop, guild_id: int, title: str, url: str, requester: str, played_at: str) -> None:
    await loop.run_in_executor(None, _add, guild_id, title, url, requester, played_at)
