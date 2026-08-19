# -*- coding: utf-8 -*-
"""길드(서버)별 봇 설정 저장소 (SQLite).

Flask 웹 설정 페이지와 봇 프로세스가 같은 테이블(같은 playlists.db 파일)을
공유해서 읽고 쓴다. 두 프로세스가 동시에 접근하므로 WAL 모드를 켠다.
"""

import os
import sqlite3

DB_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "playlists.db")

DEFAULTS = {
    "music_channel_id": None,
    "controller_message_id": None,
    "default_volume": 0.30,
    "shuffle": False,
    "loop_mode": "off",
}

_COLUMNS = ["guild_id", "music_channel_id", "controller_message_id", "default_volume", "shuffle", "loop_mode", "updated_at"]


def _get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id INTEGER PRIMARY KEY,
            music_channel_id INTEGER,
            controller_message_id INTEGER,
            default_volume REAL NOT NULL DEFAULT 0.30,
            shuffle INTEGER NOT NULL DEFAULT 0,
            loop_mode TEXT NOT NULL DEFAULT 'off',
            updated_at TEXT
        )
        """
    )
    return conn


def _row_to_dict(row):
    if row is None:
        return None
    d = dict(zip(_COLUMNS, row))
    d["shuffle"] = bool(d["shuffle"])
    return d


def get_settings(guild_id: int) -> dict:
    with _get_conn() as conn:
        row = conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM guild_settings WHERE guild_id = ?", (guild_id,)
        ).fetchone()
    return _row_to_dict(row) or dict(DEFAULTS, guild_id=guild_id, updated_at=None)


def list_guild_ids():
    with _get_conn() as conn:
        return [r[0] for r in conn.execute("SELECT guild_id FROM guild_settings").fetchall()]


def upsert_settings(guild_id: int, **fields) -> None:
    current = get_settings(guild_id)
    current.update(fields)
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO guild_settings
                (guild_id, music_channel_id, controller_message_id, default_volume, shuffle, loop_mode, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(guild_id) DO UPDATE SET
                music_channel_id=excluded.music_channel_id,
                controller_message_id=excluded.controller_message_id,
                default_volume=excluded.default_volume,
                shuffle=excluded.shuffle,
                loop_mode=excluded.loop_mode,
                updated_at=excluded.updated_at
            """,
            (
                guild_id,
                current.get("music_channel_id"),
                current.get("controller_message_id"),
                float(current.get("default_volume", DEFAULTS["default_volume"])),
                1 if current.get("shuffle") else 0,
                current.get("loop_mode", DEFAULTS["loop_mode"]),
            ),
        )


# ---- discord.py(비동기) 쪽에서 쓰는 executor 래퍼 ----
async def async_get_settings(loop, guild_id: int) -> dict:
    return await loop.run_in_executor(None, get_settings, guild_id)


async def async_list_guild_ids(loop):
    return await loop.run_in_executor(None, list_guild_ids)


async def async_upsert_settings(loop, guild_id: int, **fields) -> None:
    await loop.run_in_executor(None, lambda: upsert_settings(guild_id, **fields))
