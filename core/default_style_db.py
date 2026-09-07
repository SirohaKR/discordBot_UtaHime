# -*- coding: utf-8 -*-
"""서버별 기본 스타일 참조(Vibe Transfer) 이미지 저장소 (SQLite, playlists.db 공유).

/그림생성에서 스타일참조 이미지를 따로 첨부하지 않으면, 여기 등록된 이미지를 자동으로
스타일 참조로 사용한다. 이미지 자체를 BLOB으로 저장해서 인코딩은 매번(모델별로) 다시
하되 vibe_cache_db 캐시를 그대로 재사용할 수 있게 한다.
"""

import os
import sqlite3

DB_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "playlists.db")


def _get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS default_style (
            guild_id INTEGER PRIMARY KEY,
            image_bytes BLOB NOT NULL,
            strength REAL NOT NULL,
            information_extracted REAL NOT NULL,
            set_by TEXT,
            set_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    return conn


def get_default_style(guild_id: int):
    """(image_bytes, strength, information_extracted) 튜플 또는 None."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT image_bytes, strength, information_extracted FROM default_style WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
    return (row[0], row[1], row[2]) if row else None


def set_default_style(
    guild_id: int, image_bytes: bytes, strength: float, information_extracted: float, set_by: str
) -> None:
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO default_style (guild_id, image_bytes, strength, information_extracted, set_by)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                image_bytes=excluded.image_bytes,
                strength=excluded.strength,
                information_extracted=excluded.information_extracted,
                set_by=excluded.set_by,
                set_at=datetime('now')
            """,
            (guild_id, image_bytes, strength, information_extracted, set_by),
        )


def clear_default_style(guild_id: int) -> None:
    with _get_conn() as conn:
        conn.execute("DELETE FROM default_style WHERE guild_id = ?", (guild_id,))


async def async_get_default_style(loop, guild_id: int):
    return await loop.run_in_executor(None, get_default_style, guild_id)


async def async_set_default_style(
    loop, guild_id: int, image_bytes: bytes, strength: float, information_extracted: float, set_by: str
) -> None:
    await loop.run_in_executor(
        None, lambda: set_default_style(guild_id, image_bytes, strength, information_extracted, set_by)
    )


async def async_clear_default_style(loop, guild_id: int) -> None:
    await loop.run_in_executor(None, lambda: clear_default_style(guild_id))
