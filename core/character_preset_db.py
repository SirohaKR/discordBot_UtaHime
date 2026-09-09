# -*- coding: utf-8 -*-
"""유저별 캐릭터 프리셋(고정 특징 묘사) 저장소 (SQLite, playlists.db 공유).

NovelAI에는 "이 이미지 속 캐릭터를 그대로 유지해줘" 같은 진짜 정체성 참조 기능이
없다(Vibe Transfer는 화풍/색감 전이지 얼굴 고정이 아님). 대신 커뮤니티에서 실제로
캐릭터 일관성을 잡는 방법은 머리색/눈색/헤어스타일/특징적인 소품 같은 캐릭터 고정
태그를 매번 똑같이 재사용하는 것 — 이 모듈은 그 고정 묘사를 이름 붙여 저장해뒀다가
/그림생성에서 재사용하게 해준다 (cogs/image.py 참고).
"""

import os
import sqlite3

DB_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "playlists.db")


def _get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS character_presets (
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, name)
        )
        """
    )
    return conn


def save_preset(user_id: int, name: str, description: str) -> None:
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO character_presets (user_id, name, description) VALUES (?, ?, ?)
            ON CONFLICT(user_id, name) DO UPDATE SET description=excluded.description
            """,
            (user_id, name, description),
        )


def get_preset(user_id: int, name: str):
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT description FROM character_presets WHERE user_id = ? AND name = ?",
            (user_id, name),
        ).fetchone()
    return row[0] if row else None


def list_presets(user_id: int):
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT name, description FROM character_presets WHERE user_id = ? ORDER BY name",
            (user_id,),
        ).fetchall()
    return [(r[0], r[1]) for r in rows]


def delete_preset(user_id: int, name: str) -> bool:
    with _get_conn() as conn:
        cur = conn.execute("DELETE FROM character_presets WHERE user_id = ? AND name = ?", (user_id, name))
    return cur.rowcount > 0


async def async_save_preset(loop, user_id: int, name: str, description: str) -> None:
    await loop.run_in_executor(None, lambda: save_preset(user_id, name, description))


async def async_get_preset(loop, user_id: int, name: str):
    return await loop.run_in_executor(None, get_preset, user_id, name)


async def async_list_presets(loop, user_id: int):
    return await loop.run_in_executor(None, list_presets, user_id)


async def async_delete_preset(loop, user_id: int, name: str) -> bool:
    return await loop.run_in_executor(None, delete_preset, user_id, name)
