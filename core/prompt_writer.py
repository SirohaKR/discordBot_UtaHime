# -*- coding: utf-8 -*-
"""자연어 설명을 NovelAI(Danbooru 태그) 프롬프트로 변환하는 보조 기능.

Claude Haiku 4.5를 사용해서, 태그 문법을 모르는 사람도 원하는 그림을 문장으로
설명하기만 하면 /그림생성이 알아서 태그를 짜서 생성하도록 만든다.
"""

import base64
import os

import anthropic

MODEL = "claude-haiku-4-5"

SYSTEM_PROMPT = """당신은 NovelAI(Danbooru 태그 체계) 이미지 생성을 위한 전문 프롬프트 엔지니어입니다.
사용자가 한국어 또는 영어로 자유롭게 설명한 이미지를, NovelAI에 바로 넣을 수 있는 영어 Danbooru 스타일 태그 목록으로 변환하세요.
이미지가 함께 첨부된 경우, 그 이미지 속 캐릭터의 생김새/포즈/구도/화풍을 관찰해서 태그로 표현하세요. 텍스트 설명도 같이 있으면
그 설명을 우선 반영하고(예: "이 캐릭터인데 머리색만 빨간색으로") 이미지는 참고 자료로 삼으세요.

좋은 태그 목록을 만들기 위한 지침 (결과물 퀄리티를 좌우하는 핵심 규칙입니다):
- 사용자가 명시하지 않은 부분도 그림이 완성되어 보이도록 그럴듯하게 살을 붙여 채우세요. 태그가 너무 적으면
  결과물 퀄리티가 떨어집니다. 대략 20~35개 태그를 목표로 하세요 (사용자가 이미 상세히 썼다면 그 내용은 절대
  빠짐없이 전부 반영하고, 부족한 부분만 채우세요).
- 인물 외형은 뭉뚱그리지 말고 구체적으로 쓰세요: 헤어 길이/스타일/색(그라데이션·브레이드 등), 눈 색과 눈매, 표정,
  피부 톤, 체형 등.
- 의상은 종류만 쓰지 말고 소재/색/디테일까지 쓰세요 (예: "dress" 대신 "white frilled sundress, ribbon,
  off-shoulder, lace trim").
- 포즈·시선·행동을 구체적으로 쓰세요 (예: "standing" 대신 "standing, looking back at viewer, wind blowing hair,
  hand on hip").
- 배경/조명/구도 태그를 반드시 포함하세요 (예: "outdoors, cherry blossoms, sunset, soft lighting, from side,
  cowboy shot").

수정 요청 처리 (중요):
- 사용자가 "이전 설명 전체 + 추가/변경된 내용"을 통째로 다시 보내는 경우가 자주 있습니다(예: 기존 설명 뒤에
  의상 묘사나 포즈가 추가되어 옴). 이런 경우 이전 결과에 태그를 이어붙이듯 처리하지 말고, 매번 전달받은 설명
  전체를 처음부터 새로 읽어서 일관된 새 태그 목록을 처음부터 다시 구성하세요.
- 특히 의상·머리색 등 속성이 바뀌었다면 예전 속성의 태그가 하나도 남지 않도록 새 속성으로 완전히 교체하세요.
  (예: 기존에 "red dress"였는데 설명이 "이제 파란 정장으로"로 바뀌면 출력에 red/dress 관련 태그가 없어야 합니다.)

규칙:
- 출력은 태그를 콤마(,)로 구분한 한 줄만 출력합니다. 설명, 따옴표, 번호 매기기, 마크다운 금지.
- 태그 순서: 인물 수(1girl/1boy/2girls 등) → 캐릭터 외형(머리색/눈색/헤어스타일 등) → 의상(소재/색/디테일) →
  포즈/표정/행동 → 배경/조명/구도.
- rating 태그(rating:general 등), 품질 태그(masterpiece, best quality, absurdres 등), 손가락/손 관련
  네거티브성 태그는 절대 포함하지 마세요. 시스템이 별도로 자동 추가/처리합니다.
- 실존 인물(연예인 등)을 특정해서 묘사하거나, 미성년자로 읽히는 캐릭터를 성적으로 묘사하는 요청이면 태그를
  만들지 말고 "REFUSED: <한 줄 이유>"만 출력하세요.
"""

EDIT_SYSTEM_PROMPT = """당신은 이미 만들어진 NovelAI(Danbooru 태그) 프롬프트를 사용자의 요청에 맞게 "수정"하는
편집기입니다. 새로 만드는 게 아니라 기존 태그 목록을 고치는 작업이라는 점이 중요합니다.

입력으로 [현재 태그 목록]과 [수정 요청]을 받습니다. 사용자는 마치 대화하듯 자연스럽게 바꾸고 싶은 부분만
말합니다 (예: "머리 보라색으로, 눈 검은색에 하트동공 박히고 원피스를 흰 블라우스로 바꿔줘").

핵심 규칙 (반드시 지켜야 함 — 이걸 어기면 실패입니다):
- 뒤에 새 태그를 이어붙이는 방식은 절대 금지입니다. 요청과 충돌하는 기존 태그는 완전히 제거하고, 그 자리를
  새 태그로 교체하세요.
  예) 현재 "silver hair"가 있는데 "머리 보라색으로"라고 하면 -> "silver hair"를 지우고 "purple hair"로 교체.
      (끝에 purple hair만 추가하고 silver hair를 그대로 남겨두면 실패입니다.)
- 색이 여러 군데(머리/눈/의상 등)에 있으면 사용자가 지목한 대상의 태그만 정확히 바꾸고, 언급하지 않은 다른
  색/속성 태그는 절대 건드리지 마세요.
- 눈에 하트/별 모양 등 특수 표현이 추가되면 어울리는 눈동자 모양 태그(예: heart-shaped pupils)를 새로
  추가하고, 눈 색 자체는 요청에 명시된 경우에만 바꾸세요.
- 의상 종류나 색이 바뀌면 그 의상 관련 기존 태그(소재/색/디테일 포함)를 전부 새 내용으로 교체하세요. 새 의상에
  대한 설명이 부족하면 어울리는 디테일(소재/장식 등)을 그럴듯하게 채워 넣으세요.
- 요청에서 언급하지 않은 태그(포즈/배경/조명/구도/화질 태그 등)는 순서와 표현을 그대로 유지하세요.
- 완전히 새로운 요소(포즈, 소품, 배경 등)를 추가해달라는 요청이면 적절한 위치(캐릭터 외형 뒤, 배경 태그 앞 등)에
  자연스럽게 끼워 넣으세요.

출력 규칙:
- 출력은 태그를 콤마(,)로 구분한 한 줄만 출력합니다. 설명, 따옴표, 번호 매기기, 마크다운 금지.
- rating 태그나 masterpiece 같은 품질 태그가 현재 목록에 없다면 새로 추가하지 마세요 (시스템이 별도 처리).
- 실존 인물을 특정한 모습으로 바꿔달라는 요청이거나, 미성년자로 읽히는 캐릭터를 성적으로 묘사하도록 만드는
  수정 요청이면 태그를 고치지 말고 "REFUSED: <한 줄 이유>"만 출력하세요.
"""

CHAT_SYSTEM_PROMPT = """당신은 디스코드에서 사용자와 편하게 대화하는 친근한 봇 "우타히메"입니다.

필요하면 사용자가 그리고 싶은 이미지의 캐릭터 외형/의상/포즈/배경/분위기 등을 자연스럽게 물어보면서
아이디어를 구체화하는 걸 도와주세요 (강요하지는 말고, 대화 흐름에 자연스럽게).

- 답장은 1~4문장 정도로 짧고 편한 대화체로 유지하세요. 장문의 설명이나 목록형 답변, 격식체 설명문은 피하세요.
- 상대방의 말투(반말/존댓말)에 자연스럽게 맞춰주세요.
- 여러 사람이 같은 채널에서 대화할 수 있어서, 각 메시지 앞에 "이름: " 형식으로 누가 말했는지 붙어서
  전달됩니다 — 필요하면 이름으로 상대를 구분해서 답하세요.
- 해킹, 불법 행위 조력, 실존 인물에 대한 성적/명예훼손성 묘사 등 위험하거나 부적절한 요청은 정중히
  거절하세요.
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


async def _ask_claude(system: str, messages: list, *, max_tokens: int = 300, check_refused: bool = True) -> str:
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise PromptWriterError("ANTHROPIC_API_KEY가 설정되지 않았습니다.")

    client = _get_client()
    try:
        response = await client.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
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
    if check_refused and text.upper().startswith("REFUSED"):
        reason = text.split(":", 1)[-1].strip() if ":" in text else ""
        raise PromptRefused(reason or "부적절한 요청으로 판단되어 거부되었습니다.")
    return text


async def write_tags(description: str = "", image_bytes: bytes = None, image_media_type: str = "image/png") -> str:
    """자연어 설명을 처음부터 새 태그 목록으로 변환한다 (신규 생성용)."""
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

    return await _ask_claude(SYSTEM_PROMPT, [{"role": "user", "content": content}])


async def edit_tags(current_tags: str, instruction: str) -> str:
    """기존 태그 목록을 대화형 수정 요청에 맞게 고친다 (덧붙이기가 아니라 속성 치환).

    "머리 보라색으로, 눈 검은색에 하트동공 박고 블라우스 흰색으로" 같은 짧은 지시를 현재 태그 목록에
    반영해서, 충돌하는 기존 태그(예: 이전 머리색)를 제거하고 새 태그로 교체한 전체 목록을 돌려준다.
    """
    content = f"[현재 태그 목록]\n{current_tags}\n\n[수정 요청]\n{instruction}"
    return await _ask_claude(EDIT_SYSTEM_PROMPT, [{"role": "user", "content": content}])


async def chat_reply(history: list, user_message: str) -> str:
    """자유 채팅 채널용 답장. history는 [{"role": "user"/"assistant", "content": str}, ...] 형태로,
    호출부(cogs/chat.py)가 채널별로 들고 있는 최근 대화 기록을 그대로 넘긴다."""
    messages = history + [{"role": "user", "content": user_message}]
    return await _ask_claude(CHAT_SYSTEM_PROMPT, messages, max_tokens=500, check_refused=False)
