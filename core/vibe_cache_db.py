# -*- coding: utf-8 -*-
"""Vibe Transfer 인코딩 결과 캐시 (SQLite, playlists.db 공유).

encode-vibe API는 호출 1회당 2 Anlas가 소모되고 결과가 비결정적이므로,
같은 이미지+모델+information_extracted 조합은 재인코딩하지 않고 캐시된 값을 재사용한다.
"""

import hashlib
import os
import sqlite3

DB_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "playlists.db")


def image_hash(image_bytes: bytes) -> str:
    return hashlib.sha256(image_bytes).hexdigest()


def _get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vibe_cache (
            image_hash TEXT NOT NULL,
            model TEXT NOT NULL,
            information_extracted REAL NOT NULL,
            encoded TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (image_hash, model, information_extracted)
        )
        """
    )
    return conn


def get_cached(image_hash_value: str, model: str, information_extracted: float):
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT encoded FROM vibe_cache WHERE image_hash = ? AND model = ? AND information_extracted = ?",
            (image_hash_value, model, information_extracted),
        ).fetchone()
    return row[0] if row else None


def save_cached(image_hash_value: str, model: str, information_extracted: float, encoded: str) -> None:
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO vibe_cache (image_hash, model, information_extracted, encoded)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(image_hash, model, information_extracted) DO UPDATE SET encoded=excluded.encoded
            """,
            (image_hash_value, model, information_extracted, encoded),
        )


async def async_get_cached(loop, image_hash_value: str, model: str, information_extracted: float):
    return await loop.run_in_executor(None, get_cached, image_hash_value, model, information_extracted)


async def async_save_cached(loop, image_hash_value: str, model: str, information_extracted: float, encoded: str) -> None:
    await loop.run_in_executor(None, lambda: save_cached(image_hash_value, model, information_extracted, encoded))
