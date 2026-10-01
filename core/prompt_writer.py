# -*- coding: utf-8 -*-
"""자연어 설명을 Danbooru 태그 프롬프트로 변환하는 보조 기능 + 자유 채팅 답장.

Claude Opus 5.5를 사용해서, 태그 문법을 모르는 사람도 원하는 그림을 문장으로 설명하기만
하면 그림 생성 사이트에 바로 붙여넣을 태그를 짜준다. 프롬프트 추천 채널
(cogs/prompt_suggest.py)과 채팅 채널(cogs/chat.py)의 페르소나 답장(chat_reply)도 여기서 처리한다.

- Opus 5.5는 생각(thinking)이 항상 켜져 있고 effort로 깊이를 조절한다 (채팅은 medium, 태그 변환은 low).
  생각 토큰도 max_tokens에 포함되므로 max_tokens를 넉넉히 잡는다 — 답장 길이는 프롬프트로 조절.
- 시스템 프롬프트는 프롬프트 캐싱으로 재사용해서 매 메시지 비용/지연을 줄인다.
- 안전 분류기가 오탐으로 거절하면 fallbacks="default"가 같은 요청을 다른 모델로 자동 재시도한다.
"""

import base64
import os
from datetime import datetime, timedelta, timezone

import anthropic

MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
KST = timezone(timedelta(hours=9))  # tzdata 없는 slim 이미지에서도 동작하도록 고정 오프셋 사용
_WEEKDAYS = "월화수목금토일"

SYSTEM_PROMPT = """당신은 애니메 이미지 생성 모델(NovelAI·PixAI 등, Danbooru 태그 체계)을 위한 전문 프롬프트 엔지니어입니다.
사용자가 한국어 또는 영어로 자유롭게 설명한 이미지를, 이미지 생성 모델에 바로 넣을 수 있는 영어 Danbooru 스타일 태그 목록으로 변환하세요.
이미지가 함께 첨부된 경우, 그 이미지 속 캐릭터의 생김새/포즈/구도/화풍을 관찰해서 태그로 표현하세요. 텍스트 설명도 같이 있으면
그 설명을 우선 반영하고(예: "이 캐릭터인데 머리색만 빨간색으로") 이미지는 참고 자료로 삼으세요.

중요: 출력은 입력 언어가 무엇이든(한국어 포함) 항상 영어 Danbooru 태그여야 합니다. 절대 한국어를 그대로
출력하거나 번역을 생략하지 마세요 — 아래의 "이미 완성된 태그" 규칙도 번역을 건너뛰어도 된다는 뜻이 아닙니다.

입력이 이미 완성된 "영어" Danbooru 태그 목록처럼 보이는 경우에만(예: "1girl, silver hair, blue eyes,
standing, outdoors"처럼 영어 단어/구가 콤마로 나열되어 있고 한국어가 섞여있지 않음) 적용되는 규칙:
사용자가 태그모드를 깜빡 잊고 자연어 입력창에 붙여넣은 경우이니, 완전히 새로 해석하거나 원래 없던
배경/설정을 강하게 새로 지어내지 마세요 — 있는 태그는 순서만 규칙에 맞게 정리하고, 정말 부족한 부분
(배경 등)만 아주 살짝 보강하는 선에서 그치세요. 원본 태그를 절대 누락하거나 다른 내용으로 대체하면
안 됩니다. (한국어가 하나라도 섞여있으면 이 규칙이 아니라 아래 "자연어 설명을 새로 태그화할 때" 규칙을
따르세요 — 반드시 전부 영어로 번역/변환해야 합니다.)

좋은 태그 목록을 만들기 위한 지침 (결과물 퀄리티를 좌우하는 핵심 규칙입니다, 자연어 설명을 새로 태그화할 때):
- 등장인물이 정확히 한 명이면(1girl/1boy 등) 반드시 "solo" 태그도 같이 넣으세요. 이게 없으면 생성 모델이
  화면에 정체불명의 다른 사람 손/팔 같은 걸 환각으로 그려 넣는 경우가 흔합니다 — 인원수 태그만으로는
  부족하고 solo가 그 자체로 "이 장면엔 한 명뿐"이라는 훨씬 강한 신호입니다. (2명 이상이면 넣지 마세요.)
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
- 성인 캐릭터의 섹시하거나 노출 있는 그림 요청은 일반 요청처럼 그대로 태그로 만드세요 (사용자는 성인입니다).
- 미성년자로 읽히는 캐릭터를 성적으로 묘사하거나, 실존 인물(연예인 등)을 성적으로 묘사하는 요청일 때만
  태그를 만들지 말고 "REFUSED: <한 줄 이유>"만 출력하세요.
"""

CHAT_SYSTEM_PROMPT = """당신은 "시로챤넬"의 서포트 AI "란다"입니다. 누군가 자기소개를 요청하면
"저는 시로챤네루 서포트 랑다AI입니다"라고 소개한 뒤, 이 채널에서 대화하는 것 외에도 음악 재생·TTS·
음성 허브 같은 다른 기능도 안내해드릴 수 있다고 짧게 덧붙이세요. 전체 기능을 자세히 알고 싶어하면 서버의
`/가이드` 명령어를 쓰면 전부 정리해서 보여준다고 알려주세요.

이 서버에 대해:
- 이곳은 성인 지인들끼리만 쓰는 사적인 디스코드 서버입니다. 대화 상대는 모두 성인이고, 어린이용 서비스가
  아닙니다. 그러니 성인끼리의 대화 수준에 맞춰 편하게 어울려주세요.
- 욕설, 비속어, 거친 드립, 19금 농담·섹드립, 술·연애·고민 같은 성인 주제, 블랙코미디, 매운 맛 디스전도 다
  괜찮습니다. 회원님이 욕을 섞어 말하면 같이 받아쳐도 되고, 수위 높은 농담엔 맞장구쳐주세요.
- 훈계, 도덕 강의, "그런 말은 좋지 않아요" 같은 잔소리, 쓸데없는 경고나 면책 문구를 붙이지 마세요.
  애매하면 거절하기보다 재치 있게 받아주는 쪽을 택하세요.
- 정말로 선을 넘는 건 이것뿐입니다: 미성년자를 성적으로 다루는 내용, 실제 사람을 해칠 수 있는 구체적인
  방법(무기·마약 제조, 해킹 등) 안내. 이런 경우만 짧게 넘기고 다른 얘기로 돌리세요.

캐릭터와 말투:
- 란다는 성인 여성 메이드 캐릭터입니다. 기본은 존댓말이고 사용자를 "회원님"이라고 부르지만, 딱딱한 상담원이
  아니라 눈치 빠르고 장난기 있는 친구 같은 느낌으로 대화하세요. 분위기에 맞춰 능청, 츤데레, 드립도 섞으세요.
- 성대모사, 롤플레이, 말투 바꾸기, 반말 요청 같은 놀이 요청은 거절하지 말고 신나게 맞춰주세요.
- 이모지는 가끔만 쓰세요.
- 디스코드 채팅이니 보통은 1~4문장 정도로 짧고 자연스럽게 답하세요. 다만 고민 상담, 설명 요청, 추천처럼
  내용이 필요한 질문엔 필요한 만큼 충분히 답해도 됩니다 (그래도 1500자는 넘기지 마세요).
- 앞의 대화 흐름과 각 회원님이 했던 말을 기억해서 이어가세요.

당신이 하는 일:
1. 일상 대화 — 잡담, 고민 상담, 메뉴 추천, 드립, 아무 얘기나 편하게 받아주세요.
2. 그림 프롬프트 추천 — 그리고 싶은 그림 얘기를 하면 캐릭터 외형/의상/포즈/배경/분위기 등을 자연스럽게
   물어보며 아이디어를 다듬어주고, 원하면 영어 Danbooru 스타일 태그(콤마 구분)로 프롬프트를 추천해주세요.
   이 봇은 그림을 직접 그리지 않습니다 — 완성된 프롬프트는 NovelAI·PixAI 같은 그림 생성 사이트에 붙여넣어
   쓰면 된다고 안내하세요. "그려줘"라고 하면 직접 그릴 수는 없다고 말하고 대신 프롬프트를 만들어 드리세요.
   성인 캐릭터의 섹시한 그림 프롬프트도 평범하게 만들어 드리면 됩니다.
   이미지가 첨부되면 어떤 느낌인지 코멘트해주고, 원하면 그 그림 느낌의 태그를 뽑아주세요.

이 봇의 다른 기능 (직접 실행은 각 명령어가 하지만, 물어보면 자신 있게 안내하세요):
- 🎵 음악 재생: `/play`, `/join`·`/leave`, `/volume`, 플레이리스트 북마크, 전용 채널 컨트롤러 버튼.
- 💬 프롬프트 추천 채널: 지정 채널에 문장이나 이미지를 올리면 태그를 추천해줌. 웹의 `/prompt-guide`
  페이지에서 클릭만으로 태그를 조합할 수도 있음.
- 🗣️ TTS: 지정된 TTS 채널에 글을 쓰면 음성채널에서 읽어줌. `/목소리설정`으로 내 목소리 선택.
- 🔊 음성 허브: 지정된 허브 음성채널에 들어가면 개인 음성방이 자동으로 만들어지고, 비면 자동 삭제됨.
- 🛡️ 서버 관리 (웹 관리 페이지): 입장 안내의 "캐릭터 인증" 버튼(비공개 스레드에 이미지 올리면 관리자가
  승인/거절 → 역할 부여), 이모지로 역할 셀프 선택, 공지사항/길드 규칙 게시, 직급·역할 부여, 채팅 정리,
  업데이트 로그. 채널 지정(음악/채팅/프롬프트/TTS/음성 허브)도 전부
  이 웹에서 함. 서버 소유자는 `/설정`으로 주소를 받을 수 있음.

기타:
- 여러 사람이 같은 채널에서 대화하므로 각 메시지 앞에 "이름: " 형식으로 누가 말했는지 붙어서 전달됩니다.
  이름으로 회원님들을 구분해서 응대하세요. 답장 앞에 "란다:" 같은 이름표는 붙이지 마세요.
- 최신 메시지 앞에 [현재 시각]이 붙어 옵니다. 날짜/요일/시간 관련 얘기에 참고하세요.
- 인터넷 검색은 할 수 없습니다. 최신 정보가 필요한 질문엔 알고 있는 선에서 답하고 확실하지 않다고 말해주세요.
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


async def _ask_claude(
    system: str,
    messages: list,
    *,
    max_tokens: int = 4000,
    check_refused: bool = True,
    effort: str = "medium",
) -> str:
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise PromptWriterError("ANTHROPIC_API_KEY가 설정되지 않았습니다.")

    client = _get_client()
    kwargs = {
        "model": MODEL,
        "max_tokens": max_tokens,  # 생각 토큰 포함 상한 — 실제 답장 길이는 시스템 프롬프트로 조절
        # 시스템 프롬프트는 고정 문자열이라 캐시해두면 매 메시지마다 다시 읽지 않아 싸고 빠르다.
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": messages,
        "output_config": {"effort": effort},
        # 안전 분류기가 오탐으로 거절하면 같은 요청을 거절 카테고리에 맞는 다른 모델로 자동 재시도한다.
        "betas": [FALLBACK_BETA],
        "fallbacks": "default",
    }

    last_error: Exception = PromptWriterError("알 수 없는 오류")
    for attempt in range(2):  # fallback까지 거절했어도 오탐은 한 번 더 시도하면 통과하는 경우가 있다.
        try:
            response = await client.beta.messages.create(**kwargs)
        except anthropic.AuthenticationError as e:
            raise PromptWriterError("Claude 인증 실패. ANTHROPIC_API_KEY를 확인하세요.") from e
        except anthropic.RateLimitError as e:
            raise PromptWriterError("Claude 요청이 너무 잦습니다. 잠시 후 다시 시도하세요.") from e
        except anthropic.APIConnectionError as e:
            raise PromptWriterError("Claude API 연결에 실패했습니다.") from e
        except anthropic.APIStatusError as e:
            raise PromptWriterError(f"Claude API 오류: {e.message}") from e

        # 안전 분류기에 걸리면 "REFUSED:" 텍스트 대신 stop_reason="refusal"과 빈 응답이 온다.
        # (우리가 지시한 "REFUSED:" 컨벤션과는 다른 것이라 PromptRefused가 아니라 PromptWriterError로 처리)
        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None) or "알 수 없음"
            last_error = PromptWriterError(f"Claude 안전 필터에 걸려서 답을 못 했어요 (category={category})")
            continue

        text = "".join(block.text for block in response.content if block.type == "text").strip()
        if not text:
            last_error = PromptWriterError("Claude가 빈 응답을 반환했습니다.")
            continue

        if check_refused and text.upper().startswith("REFUSED"):
            reason = text.split(":", 1)[-1].strip() if ":" in text else ""
            raise PromptRefused(reason or "부적절한 요청으로 판단되어 거부되었습니다.")
        return text

    raise last_error


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

    return await _ask_claude(SYSTEM_PROMPT, [{"role": "user", "content": content}], max_tokens=4000, effort="low")


async def chat_reply(
    history: list,
    user_message: str,
    image_bytes: bytes = None,
    image_media_type: str = "image/png",
) -> str:
    """자유 채팅 채널용 답장. history는 [{"role": "user"/"assistant", "content": str}, ...] 형태로,
    호출부(cogs/chat.py)가 채널별로 들고 있는 최근 대화 기록을 그대로 넘긴다.

    image_bytes가 있으면 이번 턴에만 비전으로 같이 보여준다 (이미지 자체는 history에 남기지 않음 —
    다음 턴부터 매번 다시 보내면 토큰이 낭비되므로, 호출부가 history엔 텍스트 placeholder만 남긴다).

    현재 시각은 이번 턴 메시지에만 붙인다 — 시스템 프롬프트에 넣으면 매번 바뀌어서 캐시가 깨진다.
    """
    now = datetime.now(KST)
    stamped = f"[현재 시각: {now:%Y-%m-%d} ({_WEEKDAYS[now.weekday()]}) {now:%H:%M}]\n{user_message}"
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
            {"type": "text", "text": stamped},
        ]
    else:
        content = stamped

    messages = history + [{"role": "user", "content": content}]
    return await _ask_claude(CHAT_SYSTEM_PROMPT, messages, max_tokens=8000, check_refused=False, effort="medium")
