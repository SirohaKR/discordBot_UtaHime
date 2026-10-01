# -*- coding: utf-8 -*-
"""
TTS / 음성 허브(자동 음성방) 설정 저장소. 시로냥 봇에서 그대로 가져온 구조다.

음악/채팅 쪽 설정은 playlists.db(SQLite)에 있지만, 이 두 기능은 시로냥과 같은 형식의
JSON 파일 하나(data/settings.json)를 쓴다 — 서버 하나당 값이 몇 개 안 되고, 메모장으로
열어서 바로 확인/수정할 수 있어서. (docker-compose.yml에서 ./data 폴더를 볼륨으로 마운트함)

저장되는 값 예시 (서버 ID마다 하나씩):
{
  "123456789012345678": {
    "voice_hubs": [
      {"id": 444, "name_template": "{user}의 방", "user_limit": 0},
      {"id": 555, "name_template": "개인방 {n}", "user_limit": 1}
    ],
    "temp_voice_channels": [
      {"channel_id": 777, "hub_id": 555, "number": 1}
    ],
    "tts": {"channel_id": 777, "voice": "ko-KR-SunHiNeural"},
    "tts_user_voices": {"888(유저ID)": "ko-KR-InJoonNeural"}
  }
}

name_template의 {user}는 입장한 사람 이름, {n}은 번호로 cogs/channels.py가 치환한다.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SETTINGS_PATH = DATA_DIR / "settings.json"


def _load_all() -> dict:
    if not SETTINGS_PATH.exists():
        return {}
    with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_all(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_guild_settings(guild_id: int) -> dict:
    """특정 서버의 설정을 딕셔너리로 가져온다. 아직 설정한 적이 없으면 빈 딕셔너리를 준다."""
    return _load_all().get(str(guild_id), {})


def update_guild_settings(guild_id: int, **kwargs: Any) -> None:
    """특정 서버의 설정 중 넘겨받은 값들만 덮어써서 저장한다.

    예) update_guild_settings(guild.id, tts={"channel_id": 123, "voice": "ko-KR-SunHiNeural"})
    """
    all_data = _load_all()
    guild_key = str(guild_id)
    guild_data = all_data.get(guild_key, {})
    guild_data.update(kwargs)
    all_data[guild_key] = guild_data
    _save_all(all_data)
