# -*- coding: utf-8 -*-
"""현재 재생 상태 저장소 (SQLite).

웹 설정 페이지가 봇 프로세스의 현재 재생 상태를 폴링해서 보여주기 위한 용도.
플레이리스트 북마크와 동일한 playlists.db 파일을 공유한다.
"""

import os
import sqlite3

DB_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "playlists.db")


def _get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS now_playing (
            guild_id INTEGER PRIMARY KEY,
            title TEXT,
            url TEXT,
            thumbnail TEXT,
            requester TEXT,
            duration TEXT,
            queue_length INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """
    )
    return conn


def _set(guild_id: int, title, url, thumbnail, requester, duration, queue_length: int, updated_at: str) -> None:
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO now_playing (guild_id, title, url, thumbnail, requester, duration, queue_length, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                title=excluded.title,
                url=excluded.url,
                thumbnail=excluded.thumbnail,
                requester=excluded.requester,
                duration=excluded.duration,
                queue_length=excluded.queue_length,
                updated_at=excluded.updated_at
            """,
            (guild_id, title, url, thumbnail, requester, duration, queue_length, updated_at),
        )


def _clear(guild_id: int, updated_at: str) -> None:
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO now_playing (guild_id, title, url, thumbnail, requester, duration, queue_length, updated_at)
            VALUES (?, NULL, NULL, NULL, NULL, NULL, 0, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                title=NULL, url=NULL, thumbnail=NULL, requester=NULL, duration=NULL,
                queue_length=0, updated_at=excluded.updated_at
            """,
            (guild_id, updated_at),
        )


def _get(guild_id: int):
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT title, url, thumbnail, requester, duration, queue_length, updated_at "
            "FROM now_playing WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
    if not row:
        return None
    keys = ["title", "url", "thumbnail", "requester", "duration", "queue_length", "updated_at"]
    return dict(zip(keys, row))


# ---- 동기 API (Flask 등 동기 컨텍스트용) ----
get_now_playing_sync = _get


# ---- discord.py(비동기) 쪽에서 쓰는 executor 래퍼 ----
async def set_now_playing(loop, guild_id: int, *, title, url, thumbnail, requester, duration, queue_length, updated_at) -> None:
    await loop.run_in_executor(None, _set, guild_id, title, url, thumbnail, requester, duration, queue_length, updated_at)


async def clear_now_playing(loop, guild_id: int, updated_at: str) -> None:
    await loop.run_in_executor(None, _clear, guild_id, updated_at)
