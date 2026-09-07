# -*- coding: utf-8 -*-
"""자유 채팅 채널 설정 저장소 (SQLite, playlists.db 공유)."""

import os
import sqlite3

DB_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "playlists.db")


def _get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_channel_settings (
            guild_id INTEGER PRIMARY KEY,
            channel_id INTEGER
        )
        """
    )
    return conn


def get_channel(guild_id: int):
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT channel_id FROM chat_channel_settings WHERE guild_id = ?", (guild_id,)
        ).fetchone()
    return row[0] if row else None


def set_channel(guild_id: int, channel_id: int) -> None:
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO chat_channel_settings (guild_id, channel_id) VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id
            """,
            (guild_id, channel_id),
        )


def clear_channel(guild_id: int) -> None:
    with _get_conn() as conn:
        conn.execute("DELETE FROM chat_channel_settings WHERE guild_id = ?", (guild_id,))


async def async_get_channel(loop, guild_id: int):
    return await loop.run_in_executor(None, get_channel, guild_id)


async def async_set_channel(loop, guild_id: int, channel_id: int) -> None:
    await loop.run_in_executor(None, lambda: set_channel(guild_id, channel_id))


async def async_clear_channel(loop, guild_id: int) -> None:
    await loop.run_in_executor(None, lambda: clear_channel(guild_id))
