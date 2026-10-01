# -*- coding: utf-8 -*-
"""
채팅에서 자주 쓰는 "초성체"/자모만 있는 글자를 TTS가 자연스럽게 읽을 수 있는
글자로 바꿔주는 전처리기. core/tts_engine.py의 synthesize()가 실제 합성 전에
이 모듈의 normalize_for_tts()를 거쳐서 텍스트를 정리한다.

두 가지 방식을 함께 쓴다:

1) 뜻이 정해진 줄임말("ㄹㅇ" -> "리얼", "ㅅㅂ" -> "씨발" 등)은 뜻을 유추할 수
   없어서(자음만으로는 어떤 모음이 들어갈지 알 수 없다) 사전(_ABBREVIATIONS)으로
   직접 관리한다. 자주 쓰이는 것 위주로만 넣었고, 여기 없는 조합은 2번으로
   처리된다.

2) 사전에 없는 낱개 자음/모음("ㅋㅋㅋ", "ㅠㅠ", "ㅗㅜㅑ" 등)은 한글 음절
   조합 공식을 그대로 이용해서 실제로 읽을 수 있는 음절로 바꾼다.
   - 모음만 있으면 소리가 없는 초성 'ㅇ'을 붙인다 (ㅠ -> 유, ㅏ -> 아).
   - 자음만 있으면 중성 'ㅡ'를 붙인다 (ㅋ -> 크, ㅎ -> 흐).
   이 방식은 어떤 모음/자음 조합이 와도 다 커버되기 때문에, 사전을 아무리
   늘려도 못 잡는 케이스들을 자동으로 받아준다.
"""
from __future__ import annotations

# 뜻이 있는 초성 줄임말. 긴 것부터 매칭해야 "ㄴㅇㄱ"이 "ㄴ"+"ㅇㄱ"처럼 잘못
# 쪼개지지 않는다 (normalize_for_tts가 길이 4→1 순서로 시도함).
_ABBREVIATIONS: dict[str, str] = {
    "ㅂㄷㅂㄷ": "부들부들",
    "ㄴㅇㄱ": "노잼",
    "ㄱㅇㄷ": "고생",
    "ㅍㅌㅊ": "평타",
    "ㄱㄱ": "고고",
    "ㄴㄴ": "노노",
    "ㅇㅋ": "오케이",
    "ㄱㅅ": "감사",
    "ㅊㅋ": "축하",
    "ㅊㅅ": "축하",
    "ㅎㅇ": "하이",
    "ㅂㅂ": "바이바이",
    "ㄹㅇ": "리얼",
    "ㅅㅂ": "씨발",
    "ㅆㅂ": "씨발",
    "ㅈㄴ": "존나",
    "ㅇㅈ": "인정",
    "ㅇㄷ": "어디",
    "ㄱㅊ": "괜찮",
    "ㅁㄹ": "몰라",
    "ㅊㅊ": "추천",
    "ㅇㅇ": "응",
    "ㄷㄷ": "덜덜",
    "ㅅㄱ": "수고",
    "ㄲㅈ": "꺼져",
    "ㅈㅅ": "죄송",
    "ㄷㅊ": "닥쳐",
    "ㄳ": "감사",
}
_MAX_ABBREVIATION_LEN = max(len(key) for key in _ABBREVIATIONS)

# 한글 음절 = (초성 * 21 + 중성) * 28 + 종성, 0xAC00부터 시작 (유니코드 공식).
_INITIALS = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"  # 19개
_MEDIALS = "ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ"  # 21개
_SILENT_INITIAL = _INITIALS.index("ㅇ")
_NEUTRAL_MEDIAL = _MEDIALS.index("ㅡ")


def _compose_single_jamo(ch: str) -> str | None:
    """낱개 자음/모음 하나를 실제로 읽을 수 있는 음절로 바꾼다. 해당 없으면 None."""
    if ch in _MEDIALS:
        medial_index = _MEDIALS.index(ch)
        return chr(0xAC00 + (_SILENT_INITIAL * 21 + medial_index) * 28)
    if ch in _INITIALS:
        initial_index = _INITIALS.index(ch)
        return chr(0xAC00 + (initial_index * 21 + _NEUTRAL_MEDIAL) * 28)
    return None


def normalize_for_tts(text: str) -> str:
    """초성체/자모만 있는 글자를 TTS가 읽기 좋은 형태로 바꾼 새 문자열을 돌려준다."""
    result: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        for length in range(_MAX_ABBREVIATION_LEN, 0, -1):
            chunk = text[i : i + length]
            if chunk in _ABBREVIATIONS:
                result.append(_ABBREVIATIONS[chunk])
                i += length
                break
        else:
            composed = _compose_single_jamo(text[i])
            result.append(composed if composed is not None else text[i])
            i += 1
    return "".join(result)
