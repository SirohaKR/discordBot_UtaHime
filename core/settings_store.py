# -*- coding: utf-8 -*-
"""
서버(길드)별 설정을 저장하고 불러오는 아주 단순한 저장소 (시로냥 봇에서 그대로 가져옴).

서버 관리 웹(web/app.py)과 TTS/음성 허브/인증/역할 선택 cog가 같이 쓴다. 음악/채팅 설정은
여기가 아니라 playlists.db(SQLite)에 있다. docker-compose.yml에서 ./data를 봇/웹 양쪽에 마운트한다.

데이터베이스(DB) 대신 JSON 파일 하나(data/settings.json)를 쓰는 이유:
- 서버 하나당 저장할 값이 몇 개 안 되고 자주 바뀌지도 않는다.
- 메모장으로 열어봐도 내용을 바로 알아볼 수 있어서, 파이썬을 몰라도
  data/settings.json을 직접 열어 값을 확인하거나 고칠 수 있다.

저장되는 값 예시 (서버 ID마다 하나씩):
{
  "123456789012345678": {
    "entrance": {"channel_id": 111, "title": "...", "body": "...", "message_id": 999},
    "verification": {
      "channel_id": 222,
      "title": "...",
      "body": "...",
      "role_id": 333,
      "log_channel_id": 999,
      "threads": {"444(유저ID)": 555(스레드ID)}
    },
    "announcement": {"channel_id": 666, "title": "...", "body": "...", "message_id": 777},
    "guild_rules": {"channel_id": 666, "title": "...", "body": "...", "message_id": 777},
    "job_list": [{"label": "히어로", "emoji": "🦸", "role_id": 777}, ...],
    "job_roles": {"message_id": 888, "channel_id": 666, "emoji_to_role": {"🦸": {"role_id": 777, "label": "히어로"}}},
    "rank_role_ids": {"길드마스터": 333, ...},
    "voice_hubs": [
      {"id": 444, "name_template": "{user}의 파티"},
      {"id": 555, "name_template": "개인방 {n}"},
      {"id": 556, "name_template": "자유 음성방 {n}"}
    ],
    "temp_voice_channels": [
      {"channel_id": 777, "hub_id": 555, "number": 1},
      {"channel_id": 778, "hub_id": 555, "number": 2}
    ],
    "tts": {"channel_id": 777, "voice": "ko-KR-SunHiNeural"},
    "tts_user_voices": {"888(유저ID)": "ko-KR-InJoonNeural"},
    "bot_log_channel_id": 999,
    "last_announced_version": "2026-09-14"
  }
}

name_template의 {user}는 입장한 사람 이름, {n}은 번호로 치환된다 (core/settings_store.py가
아니라 cogs/channels.py가 실제로 치환한다). {n} 번호는 그 허브에서 "지금 살아있는
채널 중 비어있는 가장 작은 번호"를 쓴다 — 누적 카운터가 아니라서, 방이 지워지면
다음에 그 번호가 다시 쓰인다.
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
    """특정 서버의 설정을 딕셔너리로 가져온다. 아직 설정한 적이 없으면 빈 딕셔너리를 준다.

    예전 구조로 저장된 서버 설정을 처음 읽을 때 자동으로 새 구조로 옮겨주는(마이그레이션)
    작업도 여기서 한다 — 옮긴 뒤에는 바로 저장해서 다음부터는 이 코드를 다시 안 타게 한다.
    """
    all_data = _load_all()
    guild_key = str(guild_id)
    settings = all_data.get(guild_key, {})
    changed = False

    # 예전엔 "공지사항"과 "길드 규칙"이 한 기능(announcement)이었다. 이미 써둔 내용을
    # 새로 나뉜 "길드 규칙" 쪽으로 그대로 옮겨서 이어서 쓸 수 있게 한다.
    if "guild_rules" not in settings and settings.get("announcement"):
        settings["guild_rules"] = dict(settings["announcement"])
        changed = True

    # 예전엔 음성 허브가 hub_voice_channel_id 하나뿐이었다. 여러 개를 관리하는
    # voice_hubs 목록으로 옮겨준다 (이름은 기존 동작 그대로 "{user}의 파티").
    if "voice_hubs" not in settings and settings.get("hub_voice_channel_id"):
        settings["voice_hubs"] = [{"id": settings["hub_voice_channel_id"], "name_template": "{user}의 파티"}]
        changed = True

    # 예전엔 봇이 만든 임시 음성채널을 그냥 ID 목록(temp_voice_channel_ids)으로만
    # 관리해서, 허브별로 몇 번 방이 지금 몇 개나 떠있는지 알 수 없었다(그래서 번호가
    # 계속 누적되기만 했다). 어느 허브 소속인지/번호가 뭐였는지는 알 수 없으니
    # hub_id/number는 비워두고, "삭제 감시 대상"으로만 이어서 관리한다.
    if "temp_voice_channels" not in settings and settings.get("temp_voice_channel_ids"):
        settings["temp_voice_channels"] = [
            {"channel_id": cid, "hub_id": None, "number": None} for cid in settings["temp_voice_channel_ids"]
        ]
        changed = True

    if changed:
        all_data[guild_key] = settings
        _save_all(all_data)

    return settings


def update_guild_settings(guild_id: int, **kwargs: Any) -> None:
    """특정 서버의 설정 중 넘겨받은 값들만 덮어써서 저장한다.

    예) update_guild_settings(guild.id, hub_voice_channel_id=123)
    """
    all_data = _load_all()
    guild_key = str(guild_id)
    guild_data = all_data.get(guild_key, {})
    guild_data.update(kwargs)
    all_data[guild_key] = guild_data
    _save_all(all_data)
