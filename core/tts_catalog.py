# -*- coding: utf-8 -*-
"""
타입캐스트 전체 목소리 카탈로그(2026년 기준 약 600개)를 가져오고 검색하는 코드.

core/tts_voices.py의 TTS_VOICES는 자주 쓸만한 것만 손으로 골라둔 "추천" 목록이고,
여기는 타입캐스트 API가 제공하는 전체 목소리를 그대로 가져와서 이름/성별/나이/
용도로 검색할 수 있게 해준다. "/목소리설정" 명령어의 자동완성과, 관리자 설정
페이지("🗣️ TTS")의 목소리 검색에서 함께 쓴다.

디스코드 슬래시 명령어의 선택지는 최대 25개까지만 보여줄 수 있어서, 600개를
전부 고정 목록으로 넣을 수는 없다. 그래서 사용자가 글자를 입력할 때마다 그
글자로 이 카탈로그를 검색해서 맞는 것만 최대 25개 보여주는 자동완성 방식을 쓴다.

한 번 받아온 목록은 과정(프로세스) 안에 계속 캐싱해둔다 — 타입캐스트가 목소리를
새로 추가하는 일이 잦지 않아서, 매번 API를 부르는 것보다 낫다. 새 목소리가
추가된 걸 반영하려면 봇/웹 페이지를 재시작하면 된다.

미리듣기는 타입캐스트가 목소리마다 미리 준비해둔 무료 샘플 음원(preview_url)을
그대로 쓴다 — 실제 합성 API(유료, 크레딧 소모)를 매번 부르지 않아도 된다.
"""
from __future__ import annotations

import os

import aiohttp

_TYPECAST_VOICES_URL = "https://api.typecast.ai/v3/voices"

_GENDER_KO = {"male": "남성", "female": "여성"}
_AGE_KO = {
    "child": "아동",
    "teenager": "청소년",
    "young_adult": "청년",
    "middle_age": "중년",
    "elder": "노년",
}

_typecast_catalog_cache: list[dict] | None = None


def _looks_non_korean(names: dict) -> bool:
    """영어 이름이 "성 이름"처럼 두 단어 이상이면(예: "Miu Kobayashi") 대개 일본어권
    등 한국어가 아닌 시장을 겨냥한 목소리다 — 한국어 이름란도 그냥 그 발음을 한글로
    옮겨 적은 것뿐이라("고바야시 미우"), 실제로 한국어를 잘 읽는다는 뜻이 아니다.

    타입캐스트 API 응답에 언어를 나타내는 필드가 따로 없어서 쓰는 근사치다 —
    완벽하진 않아서(그리스 신화 이름 같은 한 단어짜리 서양 이름 등은 못 거름),
    "확실히 아닌 것"만 우선 걸러내는 용도로 쓴다.
    """
    eng_name = names.get("eng", "")
    return len(eng_name.split()) >= 2


async def list_typecast_voices() -> list[dict]:
    """타입캐스트 목소리 목록을 {id, name, label, search_text, preview_url} 형태로 돌려준다.
    한국어 시장 대상이 아닌 게 뚜렷한 목소리(_looks_non_korean 참고)는 미리 걸러둔다.

    TYPECAST_API_KEY가 없으면 빈 목록을 돌려준다 (Polly처럼 유료 기능이라, 키가
    없는 서버에서는 그냥 안 보이는 게 맞다).
    """
    global _typecast_catalog_cache
    if _typecast_catalog_cache is not None:
        return _typecast_catalog_cache

    api_key = os.getenv("TYPECAST_API_KEY", "")
    if not api_key:
        return []

    async with aiohttp.ClientSession() as session:
        async with session.get(_TYPECAST_VOICES_URL, headers={"X-API-KEY": api_key}) as resp:
            resp.raise_for_status()
            raw = await resp.json()

    catalog = []
    for v in raw:
        names = v.get("voice_name", {})
        if _looks_non_korean(names):
            continue

        kor_name = names.get("kor") or names.get("eng") or v["voice_id"]
        eng_name = names.get("eng", "")
        gender = _GENDER_KO.get(v.get("gender"), "")
        age = _AGE_KO.get(v.get("age"), "")
        use_cases = ", ".join(v.get("use_cases", []))
        descriptor = " · ".join(part for part in (gender, age) if part)
        label = f"{kor_name} ({descriptor})" if descriptor else kor_name

        catalog.append(
            {
                "id": f"typecast_id:{v['voice_id']}",
                "name": kor_name,
                "label": label,
                "gender": gender,
                "age": age,
                "use_cases": use_cases,
                "preview_url": v.get("preview_url"),
                "search_text": f"{kor_name} {eng_name} {gender} {age} {use_cases}".lower(),
            }
        )

    _typecast_catalog_cache = catalog
    return catalog


async def search_typecast_voices(query: str, limit: int = 1000) -> list[dict]:
    """이름/성별/나이/용도로 부분 일치 검색. 검색어가 비어있으면 전체 목록(앞에서부터
    limit개)을 돌려준다 — 뭘 검색해야 할지 모를 때 그냥 목록을 훑어볼 수 있게."""
    catalog = await list_typecast_voices()
    q = query.strip().lower()
    if not q:
        return catalog[:limit]
    return [v for v in catalog if q in v["search_text"]][:limit]


async def find_typecast_voice(voice_id: str) -> dict | None:
    """id("typecast_id:...")로 카탈로그에서 딱 하나 찾는다. 이미 골라놓은 목소리를
    다시 화면에 보여줄 때(검색을 다시 안 해도 되게) 쓴다."""
    for v in await list_typecast_voices():
        if v["id"] == voice_id:
            return v
    return None
