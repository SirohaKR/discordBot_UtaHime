# -*- coding: utf-8 -*-
"""플레이리스트 북마크 저장소 (SQLite).

유튜브 플레이리스트 URL을 서버(guild)별로 이름 붙여 저장해두고
`!플레이리스트재생 <이름>`으로 바로 불러와 재생하기 위한 용도.

sqlite3는 동기 API라서 이벤트 루프를 막지 않도록 항상 executor를 통해 호출한다.
"""

import os
import sqlite3

DB_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "playlists.db")


def _get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS playlists (
            guild_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            added_by TEXT,
            added_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, name)
        )
        """
    )
    return conn


def _add(guild_id: int, name: str, url: str, added_by: str, added_at: str) -> None:
    with _get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO playlists (guild_id, name, url, added_by, added_at) VALUES (?, ?, ?, ?, ?)",
            (guild_id, name, url, added_by, added_at),
        )


def _get(guild_id: int, name: str):
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT url FROM playlists WHERE guild_id = ? AND name = ?", (guild_id, name)
        ).fetchone()
        return row[0] if row else None


def _list(guild_id: int):
    with _get_conn() as conn:
        return conn.execute(
            "SELECT name, url, added_by FROM playlists WHERE guild_id = ? ORDER BY name COLLATE NOCASE",
            (guild_id,),
        ).fetchall()


def _delete(guild_id: int, name: str) -> bool:
    with _get_conn() as conn:
        cur = conn.execute("DELETE FROM playlists WHERE guild_id = ? AND name = ?", (guild_id, name))
        return cur.rowcount > 0


# ---- 동기 API (Flask 등 동기 컨텍스트용) ----
add_playlist_sync = _add
get_playlist_sync = _get
list_playlists_sync = _list
delete_playlist_sync = _delete


# ---- discord.py(비동기) 쪽에서 쓰는 executor 래퍼 ----
async def add_playlist(loop, guild_id: int, name: str, url: str, added_by: str, added_at: str) -> None:
    await loop.run_in_executor(None, _add, guild_id, name, url, added_by, added_at)


async def get_playlist(loop, guild_id: int, name: str):
    return await loop.run_in_executor(None, _get, guild_id, name)


async def list_playlists(loop, guild_id: int):
    return await loop.run_in_executor(None, _list, guild_id)


async def delete_playlist(loop, guild_id: int, name: str) -> bool:
    return await loop.run_in_executor(None, _delete, guild_id, name)
