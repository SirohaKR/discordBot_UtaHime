# -*- coding: utf-8 -*-
"""
디스코드 REST API를 직접 호출하는 아주 얇은 래퍼.

웹 설정 페이지(web/app.py)는 실행 중인 봇(main.py)과 완전히 다른 프로세스라서, 봇이
디스코드와 맺고 있는 실시간 연결(게이트웨이)을 함께 쓸 수 없다. 대신 디스코드가 제공하는
REST API를 봇 토큰으로 직접 호출해서 "채널/역할/멤버 목록 가져오기", "메시지 보내기",
"채널 만들기" 같은 일회성 작업을 처리한다.

이렇게 REST로 직접 호출하면 main.py(봇)가 꺼져 있어도 설정 페이지 자체는 동작한다.
다만 이모지를 눌렀을 때 실제로 역할이 부여되거나, 음성채널이 자동 생성되는 것은
main.py가 켜져 있어야 일어난다 (그건 실시간 감시가 필요한 일이라서).
"""
from __future__ import annotations

import json
import os
import time
from urllib.parse import quote

import requests

API_BASE = "https://discord.com/api/v10"

# 디스코드 스노우플레이크(메시지 ID 등)는 "만들어진 시각"을 그대로 품고 있는 숫자다.
# 2015-01-01(디스코드 자체 기준 시각)부터 몇 밀리초 지났는지를 앞쪽 비트에 담아뒀다.
# "채널 정리" 기능에서 "이 메시지가 14일보다 오래됐는지"를 판단할 때 쓴다 — 오래된
# 메시지는 한꺼번에(bulk) 못 지우고 하나씩 지워야 한다는 디스코드 자체 제한 때문.
DISCORD_EPOCH_MS = 1420070400000


def snowflake_from_timestamp_ms(timestamp_ms: int) -> int:
    return (timestamp_ms - DISCORD_EPOCH_MS) << 22


def _headers() -> dict:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN이 설정되지 않았습니다. .env 파일을 확인하세요.")
    return {"Authorization": f"Bot {token}"}


def get_bot_guilds() -> list[dict]:
    """봇이 들어가 있는 서버 목록."""
    r = requests.get(f"{API_BASE}/users/@me/guilds", headers=_headers(), timeout=10)
    r.raise_for_status()
    return r.json()


def get_channels(guild_id: int) -> list[dict]:
    """서버의 모든 채널(텍스트/음성/카테고리) 목록."""
    r = requests.get(f"{API_BASE}/guilds/{guild_id}/channels", headers=_headers(), timeout=10)
    r.raise_for_status()
    return r.json()


def get_roles(guild_id: int) -> list[dict]:
    """서버의 모든 역할 목록."""
    r = requests.get(f"{API_BASE}/guilds/{guild_id}/roles", headers=_headers(), timeout=10)
    r.raise_for_status()
    return r.json()


def get_members(guild_id: int, limit: int = 1000) -> list[dict]:
    """서버 멤버 목록. (디스코드 개발자 포털에서 SERVER MEMBERS INTENT를 켜야 값이 나온다)"""
    r = requests.get(
        f"{API_BASE}/guilds/{guild_id}/members", headers=_headers(), params={"limit": limit}, timeout=10
    )
    r.raise_for_status()
    return r.json()


def get_member(guild_id: int, user_id: int) -> dict | None:
    """특정 유저가 이 서버의 멤버인지 확인한다. 멤버가 아니면 None(404)을 돌려준다.

    "내 목소리 설정" 오픈 페이지에서, 디스코드로 로그인한 사람이 실제로 이 길드
    멤버인지 확인할 때 쓴다 (아무 디스코드 계정이나 로그인은 할 수 있지만, 길드원이
    아니면 목소리를 못 바꾸게 막기 위함).
    """
    r = requests.get(f"{API_BASE}/guilds/{guild_id}/members/{user_id}", headers=_headers(), timeout=10)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def create_voice_channel(guild_id: int, name: str, parent_id: int | None = None) -> dict:
    """음성채널을 새로 만든다. type=2가 음성채널을 뜻하는 디스코드 API 코드다."""
    payload: dict = {"name": name, "type": 2}
    if parent_id:
        payload["parent_id"] = parent_id
    r = requests.post(f"{API_BASE}/guilds/{guild_id}/channels", headers=_headers(), json=payload, timeout=10)
    r.raise_for_status()
    return r.json()


def send_message(channel_id: int, embed: dict, components: list | None = None) -> dict:
    """채널에 임베드 메시지를 보낸다. 성공하면 생성된 메시지 정보(id 포함)를 돌려준다.

    components는 버튼 등을 붙일 때 쓰는 디스코드 API 원본 형식(Action Row 배열)이다.
    예) [{"type": 1, "components": [{"type": 2, "style": 1, "label": "...", "custom_id": "..."}]}]
    """
    payload: dict = {"embeds": [embed]}
    if components:
        payload["components"] = components
    r = requests.post(f"{API_BASE}/channels/{channel_id}/messages", headers=_headers(), json=payload, timeout=10)
    r.raise_for_status()
    return r.json()


def send_message_with_file(
    channel_id: int, embed: dict, filename: str, file_bytes: bytes, components: list | None = None
) -> dict:
    """이미지를 URL이 아니라 파일 그대로 첨부해서 임베드 메시지를 보낸다.

    embed["image"]["url"]을 "attachment://<filename>"으로 넣어두면, 디스코드가 같이
    보낸 첨부파일과 그 이름으로 매칭해서 임베드 안에 그 이미지를 띄워준다. 어딘가에
    미리 업로드해서 공개 URL을 만들어둘 필요가 없다.
    """
    payload_json: dict = {"embeds": [embed]}
    if components:
        payload_json["components"] = components
    files = {"files[0]": (filename, file_bytes)}
    data = {"payload_json": json.dumps(payload_json)}
    r = requests.post(
        f"{API_BASE}/channels/{channel_id}/messages", headers=_headers(), data=data, files=files, timeout=20
    )
    r.raise_for_status()
    return r.json()


def add_reaction(channel_id: int, message_id: int, emoji: str) -> None:
    """메시지에 봇이 직접 이모지를 눌러(반응을 남겨) 사람들이 누를 자리를 만들어둔다."""
    encoded = quote(emoji)
    r = requests.put(
        f"{API_BASE}/channels/{channel_id}/messages/{message_id}/reactions/{encoded}/@me",
        headers=_headers(),
        timeout=10,
    )
    r.raise_for_status()


def add_role(guild_id: int, user_id: int, role_id: int) -> None:
    r = requests.put(
        f"{API_BASE}/guilds/{guild_id}/members/{user_id}/roles/{role_id}", headers=_headers(), timeout=10
    )
    r.raise_for_status()


def remove_role(guild_id: int, user_id: int, role_id: int) -> None:
    r = requests.delete(
        f"{API_BASE}/guilds/{guild_id}/members/{user_id}/roles/{role_id}", headers=_headers(), timeout=10
    )
    r.raise_for_status()


def _request_with_rate_limit_retry(method: str, url: str, **kwargs) -> requests.Response:
    """디스코드가 "너무 빨리 요청한다"며 429를 돌려주면, 응답에 적힌 대기 시간만큼
    쉬었다가 다시 시도한다. 메시지를 하나씩 지울 때(채널 정리 기능)처럼 요청을 짧은
    시간에 많이 보내야 하는 곳에서 쓴다 — 임의로 간격을 정해서 쉬는 대신, 디스코드가
    알려주는 실제 값을 그대로 따른다.
    """
    while True:
        r = requests.request(method, url, headers=_headers(), timeout=10, **kwargs)
        if r.status_code == 429:
            retry_after = r.json().get("retry_after", 1)
            time.sleep(retry_after)
            continue
        return r


def get_channel_messages(channel_id: int, limit: int = 100, before: int | None = None) -> list[dict]:
    """채널의 메시지 목록을 최신순으로 가져온다. limit은 디스코드 API 한도인 최대 100.
    before(메시지 ID)를 넘기면 그 메시지보다 이전 것들을 가져온다 — 100개씩 계속
    페이지를 넘기며 오래된 메시지까지 훑을 때 쓴다."""
    params = {"limit": limit}
    if before:
        params["before"] = before
    r = _request_with_rate_limit_retry("GET", f"{API_BASE}/channels/{channel_id}/messages", params=params)
    r.raise_for_status()
    return r.json()


def bulk_delete_messages(channel_id: int, message_ids: list[str]) -> None:
    """2~100개의 메시지를 한 번에 지운다. 디스코드 자체 제한으로 14일보다 오래된
    메시지가 섞여 있으면 통째로 실패하니, 부르기 전에 반드시 14일 이내 것만 걸러야
    한다 (web/app.py의 _cleanup_channel_messages가 그 필터링을 담당)."""
    r = _request_with_rate_limit_retry(
        "POST", f"{API_BASE}/channels/{channel_id}/messages/bulk-delete", json={"messages": message_ids}
    )
    r.raise_for_status()


def delete_message(channel_id: int, message_id: int) -> None:
    """메시지를 하나씩 지운다. 14일보다 오래돼서 bulk_delete_messages를 못 쓸 때 대신 쓴다."""
    r = _request_with_rate_limit_retry("DELETE", f"{API_BASE}/channels/{channel_id}/messages/{message_id}")
    if r.status_code == 404:
        return  # 이미 지워졌으면(예: 유저가 직접 지움) 그냥 넘어간다.
    r.raise_for_status()
