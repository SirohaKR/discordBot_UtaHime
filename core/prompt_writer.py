# -*- coding: utf-8 -*-
"""자연어 설명을 NovelAI(Danbooru 태그) 프롬프트로 변환하는 보조 기능.

Claude Haiku 4.5를 사용해서, 태그 문법을 모르는 사람도 원하는 그림을 문장으로
설명하기만 하면 /그림생성이 알아서 태그를 짜서 생성하도록 만든다.
"""

import base64
import os

import anthropic

MODEL = "claude-haiku-4-5"

SYSTEM_PROMPT = """당신은 NovelAI(Danbooru 태그 체계) 이미지 생성을 위한 프롬프트 작성 보조입니다.
사용자가 한국어 또는 영어로 자유롭게 설명한 이미지를, NovelAI에 바로 넣을 수 있는 영어 Danbooru 스타일 태그 목록으로 변환하세요.
이미지가 함께 첨부된 경우, 그 이미지 속 캐릭터의 생김새/포즈/구도/화풍을 관찰해서 태그로 표현하세요. 텍스트 설명도 같이 있으면
그 설명을 우선 반영하고(예: "이 캐릭터인데 머리색만 빨간색으로") 이미지는 참고 자료로 삼으세요.

규칙:
- 출력은 태그를 콤마(,)로 구분한 한 줄만 출력합니다. 설명, 따옴표, 번호 매기기, 마크다운 금지.
- 태그 순서: 인물 수(1girl/1boy/2girls 등) → 캐릭터 외형(머리색/눈색/헤어스타일 등) → 의상 → 포즈/표정/행동 → 배경/구도.
- rating: 태그(rating:general 등)나 품질 태그(masterpiece, best quality, absurdres 등)는 절대 포함하지 마세요. 시스템이 별도로 자동 추가합니다.
- 실존 인물(연예인 등)을 특정해서 묘사하거나, 미성년자로 읽히는 캐릭터를 성적으로 묘사하는 요청이면 태그를 만들지 말고 "REFUSED: <한 줄 이유>"만 출력하세요.
- 설명이 모호하면 자연스럽게 살을 붙여 완성된 태그 목록을 만드세요.
"""

_client = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic()
    return _client


class PromptWriterError(RuntimeError):
    pass


class PromptRefused(RuntimeError):
    """요청이 정책상 거부된 경우."""


async def write_tags(description: str = "", image_bytes: bytes = None, image_media_type: str = "image/png") -> str:
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise PromptWriterError("ANTHROPIC_API_KEY가 설정되지 않았습니다.")

    if image_bytes:
        content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": image_media_type,
                    "data": base64.standard_b64encode(image_bytes).decode("utf-8"),
                },
            },
            {"type": "text", "text": description or "첨부된 이미지를 보고 어울리는 태그를 만들어줘."},
        ]
    else:
        content = description

    client = _get_client()
    try:
        response = await client.messages.create(
            model=MODEL,
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.AuthenticationError as e:
        raise PromptWriterError("Claude 인증 실패. ANTHROPIC_API_KEY를 확인하세요.") from e
    except anthropic.RateLimitError as e:
        raise PromptWriterError("Claude 요청이 너무 잦습니다. 잠시 후 다시 시도하세요.") from e
    except anthropic.APIConnectionError as e:
        raise PromptWriterError("Claude API 연결에 실패했습니다.") from e
    except anthropic.APIStatusError as e:
        raise PromptWriterError(f"Claude API 오류: {e.message}") from e

    text = "".join(block.text for block in response.content if block.type == "text").strip()
    if text.upper().startswith("REFUSED"):
        reason = text.split(":", 1)[-1].strip() if ":" in text else ""
        raise PromptRefused(reason or "부적절한 요청으로 판단되어 거부되었습니다.")
    return text
