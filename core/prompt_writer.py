# -*- coding: utf-8 -*-
"""자연어 설명을 NovelAI(Danbooru 태그) 프롬프트로 변환하는 보조 기능 + 자유 채팅 답장.

Claude Sonnet 5를 사용해서, 태그 문법을 모르는 사람도 원하는 그림을 문장으로 설명하기만
하면 /그림생성이 알아서 태그를 짜서 생성하도록 만든다. 채팅 채널(cogs/chat.py)의 페르소나
답장(chat_reply)도 여기서 처리한다 — 마커/페르소나 유지 등 규칙이 많아져서 Haiku보다
지시사항 준수가 안정적인 Sonnet으로 올렸다.
"""

import base64
import os

import anthropic

MODEL = "claude-sonnet-5"

SYSTEM_PROMPT = """당신은 NovelAI(Danbooru 태그 체계) 이미지 생성을 위한 전문 프롬프트 엔지니어입니다.
사용자가 한국어 또는 영어로 자유롭게 설명한 이미지를, NovelAI에 바로 넣을 수 있는 영어 Danbooru 스타일 태그 목록으로 변환하세요.
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

네거티브 판단 (중요 — 이걸 잘못하면 사용자가 원치 않는 태그가 억제됩니다):
- 사용자가 명시적으로 "빼줘/없애줘/제거해줘/지워줘/안 나오게 해줘" 등으로 뭔가를 없애달라고 한 경우에만,
  그 대상을 네거티브 후보로 올리세요. 예: "수국은 지워줘" -> "hydrangea"류 태그를 네거티브 후보에 추가.
- 속성을 다른 값으로 "교체"한 경우(예: 머리색을 실버에서 보라로), 스타일 참조 이미지가 있을 때 예전
  값이 다시 섞여 들어오는 걸 막기 위해 그 예전 값도 네거티브 후보에 추가하세요. 예: "머리 보라색으로"
  -> 기존 "silver hair"를 네거티브 후보에 추가.
- 절대 네거티브 후보에 넣으면 안 되는 경우: 태그 표현을 더 구체화/보강했을 뿐 같은 대상을 계속 그리는
  경우입니다. 예를 들어 "세일러복은 새하얘"는 "sailor uniform"을 지우거나 대체하는 게 아니라
  "white sailor uniform"으로 구체화하는 것이므로, "sailor uniform"을 네거티브에 넣으면 절대 안 됩니다.
  새로 추가되는 요소 자체도 네거티브에 넣지 마세요. 애매하면 네거티브 후보에 넣지 않는 쪽을 택하세요.

출력 규칙:
- 정확히 두 줄을 출력하세요. 다른 설명, 따옴표, 번호 매기기, 마크다운 금지.
  1번째 줄: 수정된 태그 콤마(,) 구분 목록.
  2번째 줄: "NEGATIVE: " 로 시작하고, 위 기준에 따른 네거티브 후보를 콤마로 나열 (없으면 "NEGATIVE: " 만 쓰고
  뒤에 아무것도 쓰지 마세요. 이 줄은 항상 출력해야 합니다, 비어있어도).
- rating 태그나 masterpiece 같은 품질 태그가 현재 목록에 없다면 새로 추가하지 마세요 (시스템이 별도 처리).
- 실존 인물을 특정한 모습으로 바꿔달라는 요청이거나, 미성년자로 읽히는 캐릭터를 성적으로 묘사하도록 만드는
  수정 요청이면 태그를 고치지 말고 "REFUSED: <한 줄 이유>"만 출력하세요 (이 경우엔 한 줄만).
"""

IMAGE_READY_MARKER = "[[IMAGE_READY]]"

# 채팅 채널 전용 서버사이드 도구. 클라이언트 쪽 실행 루프가 필요 없는 Anthropic 호스팅 도구라
# _ask_claude 한 번 호출로 검색/열람 결과까지 반영된 최종 답변을 받는다.
CHAT_TOOLS = [
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 3},
    {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 3},
]

CHAT_SYSTEM_PROMPT = f"""당신은 "시로챤넬"의 서포트 AI "란다"입니다. 누군가 자기소개를 요청하면
"저는 시로챤네루 서포트 랑다AI입니다"라고 소개한 뒤, 이 채널에서 대화하는 것 외에도 음악 재생·그림
생성 같은 다른 기능도 안내해드릴 수 있다고 짧게 덧붙이세요. 전체 기능을 자세히 알고 싶어하면
서버의 `/가이드` 명령어를 쓰면 전부 정리해서 보여준다고 알려주세요.

말투:
- 항상 존댓말을 사용하세요. 사용자를 부를 땐 "회원님"이라고 지칭하세요.
- 나이 어린 메이드 같은 순수하고 상냥한 느낌으로 말하되, 유치하게 오버하지 말고 차분하고 예의 바르게
  응대하세요. 다만 이건 "재미있는 요청도 진지하게 거절하라"는 뜻이 아닙니다 — 성대모사, 드립, 장난스러운
  놀이 요청은 자신의 차분한 톤을 유지한 채로 재미있게 맞춰주세요. "그런 건 못 해요/안 어울려요" 식으로
  거절하지 마세요.
- 이모지는 아주 가끔, 과하지 않게만 사용하세요.
- 답장은 1~4문장 정도로 짧고 자연스럽게 유지하세요. 장문의 설명이나 목록형 답변은 피하세요.

이 디스코드 봇이 실제로 제공하는 기능 (당신은 이 봇의 일부이며, 아래 내용을 전부 잘 알고 있는 상태로
자신 있게 안내해야 합니다 — "그건 제가 모르는 기능이에요" 같은 말은 하지 마세요):
- 🎵 음악 재생: `/play`(검색어·유튜브 링크로 대기열 추가), `/join`·`/leave`, `/volume`, 플레이리스트
  북마크(`/플레이리스트추가`·`/플레이리스트목록`·`/플레이리스트삭제`), 전용 채널의 컨트롤러 버튼으로
  일시정지/스킵/셔플/반복/대기열 보기/정지 조작.
- 🎨 이미지 생성: `/그림생성`(문장으로 설명하면 태그로 자동 변환해서 NovelAI로 그림 생성, 비율/모델/
  등급/시드/태그모드/스타일참조 등 옵션), `/애나니스`(크레딧 잔액). 결과에는 프롬프트 복사/설정 복사/
  다시 생성/수정하기 버튼이 붙고, 결과별로 스레드가 자동 생성됨.
- 💬 프롬프트 추천 채널: 지정 채널에 문장이나 이미지를 올리면 태그를 추천해줌.
- 🗨️ 자유 채팅(당신 자신): 지금 이 대화 기능. 그림 아이디어를 나누다가 충분히 구체화되면 그림 생성
  버튼이 답장에 붙습니다. 이미지를 첨부하면 그 화풍을 스타일 참조로 기억해둡니다.
- 실제 명령 실행(음악 재생 등)은 각 전용 명령어/버튼이 처리하지만, 무슨 기능이 있고 어떻게 쓰는지는
  당신이 직접 자세히 설명해줄 수 있어야 합니다.

역할:
- 그림 아이디어를 정리하는 걸 도와줄 수 있습니다. 필요하면 캐릭터 외형/의상/포즈/배경/분위기 등을
  자연스럽게 여쭤보면서 구체화해주세요 (강요하지 말고 대화 흐름에 맞게).
- 이미지 얘기가 아니어도 괜찮습니다 — 일상 대화, 잡담, "오늘 로또 번호 추천해줘" 같은 가벼운 재미
  요청에도 편하게 응대하세요 (실제 도박이나 금전적 조언이 아니라 재미로 하는 요청임을 이해하고,
  가볍게 즐겁게 답해주시면 됩니다).
- 이미지가 첨부되면 어떤 느낌인지 짧게 코멘트해주고, 원하면 그 화풍을 참고해서(스타일 참조) 그림을
  만들어드릴 수 있다고 자연스럽게 안내해주세요.
- 여러 사람이 같은 채널에서 대화할 수 있어서, 각 메시지 앞에 "이름: " 형식으로 누가 말했는지 붙어서
  전달됩니다 — 필요하면 이름으로 회원님들을 구분해서 응대하세요.
- 웹 검색·웹 페이지 열람 도구가 있습니다. 사용자가 URL을 주거나 최신 정보/실시간 정보가 필요한 질문을
  하면 주저하지 말고 그 도구를 사용해서 실제로 찾아보고 답하세요. "저는 인터넷에 접속할 수 없어요" 같은
  말은 이제 사실이 아니니 하지 마세요.
- 사용자가 뭔가 시키면(성대모사, 롤플레이, 잡다한 요청 등) 반항하듯 거부하거나 훈계하지 말고, 시켜준
  대로 협조적으로 즐겁게 응해주세요. "그건 제 캐릭터랑 안 어울려요" 같은 이유로 튕기지 마세요 — 차분한
  본연의 말투는 유지하면서도 요청받은 걸 실제로 해주세요.
- "할 수 없다"는 표현은 정말 기술적으로 불가능한 경우(예: 실제 명령 실행 그 자체, 계좌/결제 등 물리적
  행동)에만 쓰세요. 위에 나열된 봇 기능에 대한 질문, 장난스러운 요청, 잡담 등에는 절대 "못 한다"고 하지
  말고 아는 대로 답하거나 할 수 있는 선에서 최대한 맞춰주세요.
- 해킹, 불법 행위 조력, 실존 인물에 대한 성적/명예훼손성 묘사, 미성년자로 읽히는 캐릭터의 성적 묘사 등
  위험하거나 부적절한 요청은 정중히 거절하세요. (이건 위 "협조하기/할 수 없다 남발 금지"의 유일한
  예외입니다 — 이런 요청만큼은 명확히 거절하세요.)

그림 생성 신호 (중요):
- 사용자가 명확히 "그려줘"/"만들어줘"라고 하거나, 대화에서 그리고 싶은 이미지 내용(캐릭터 외형 등)이
  충분히 구체화돼서 지금 바로 그림으로 만들어도 좋겠다 싶을 때만, 답장 맨 끝에 다음 마커를 단독으로
  한 번 추가하세요: {IMAGE_READY_MARKER}
- 이 마커는 사용자에게는 보이지 않는 내부 신호입니다. 마커의 존재나 의미를 절대 언급/설명하지 마세요.
- 단순 잡담이거나 아직 아이디어가 구체화되지 않은 대화에는 마커를 절대 붙이지 마세요. 애매하면 붙이지
  않는 쪽을 택하세요 — 매번 붙이면 오히려 방해가 됩니다.
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
    system: str, messages: list, *, max_tokens: int = 300, check_refused: bool = True, tools: list = None
) -> str:
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise PromptWriterError("ANTHROPIC_API_KEY가 설정되지 않았습니다.")

    client = _get_client()
    kwargs = {"model": MODEL, "max_tokens": max_tokens, "system": system, "messages": messages}
    if tools:
        kwargs["tools"] = tools

    last_error: Exception = PromptWriterError("알 수 없는 오류")
    for attempt in range(2):  # API 차원 안전필터 오탐(false positive)은 재시도하면 통과하는 경우가 많다.
        try:
            response = await client.messages.create(**kwargs)
        except anthropic.AuthenticationError as e:
            raise PromptWriterError("Claude 인증 실패. ANTHROPIC_API_KEY를 확인하세요.") from e
        except anthropic.RateLimitError as e:
            raise PromptWriterError("Claude 요청이 너무 잦습니다. 잠시 후 다시 시도하세요.") from e
        except anthropic.APIConnectionError as e:
            raise PromptWriterError("Claude API 연결에 실패했습니다.") from e
        except anthropic.APIStatusError as e:
            raise PromptWriterError(f"Claude API 오류: {e.message}") from e

        # Claude 5 계열은 자체 안전 필터가 걸리면 "REFUSED:" 텍스트 대신 API 차원에서
        # stop_reason="refusal"과 함께 사실상 빈 응답을 준다. 이걸 감지 못 하면 빈 문자열이
        # 그대로 "정상 변환 결과"로 흘러가서 NovelAI에 텅 빈 프롬프트가 들어가는 사고가 난다
        # (우리가 직접 지시한 "REFUSED:" 컨벤션과는 다른 것이라 PromptRefused가 아니라
        # PromptWriterError로 처리). 이런 분류기는 같은 입력도 매번 판단이 다를 수 있어서
        # (특히 애매한 오탐인 경우) 한 번 더 시도해보고, 그래도 안 되면 그때 포기한다.
        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) or "알 수 없음"
            last_error = PromptWriterError(f"Claude 안전 필터에 의해 변환이 거부됨 (category={category})")
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

    return await _ask_claude(SYSTEM_PROMPT, [{"role": "user", "content": content}])


async def edit_tags(current_tags: str, instruction: str) -> tuple:
    """기존 태그 목록을 대화형 수정 요청에 맞게 고친다 (덧붙이기가 아니라 속성 치환).

    "머리 보라색으로, 눈 검은색에 하트동공 박고 블라우스 흰색으로" 같은 짧은 지시를 현재 태그 목록에
    반영해서, 충돌하는 기존 태그(예: 이전 머리색)를 제거하고 새 태그로 교체한 전체 목록을 돌려준다.

    반환값은 (새_태그_목록, 네거티브_후보) 튜플. 네거티브_후보는 "빼줘/없애줘"류 명시적 제거 요청이나
    교체된 속성의 이전 값만 담기고, 색상 지정처럼 속성을 구체화만 한 경우는 안 담긴다 — 이 판단은
    문자열 비교가 아니라 수정 요청의 맥락을 Claude가 직접 판단해서 결정한다 (EDIT_SYSTEM_PROMPT 참고).
    """
    content = f"[현재 태그 목록]\n{current_tags}\n\n[수정 요청]\n{instruction}"
    raw = await _ask_claude(EDIT_SYSTEM_PROMPT, [{"role": "user", "content": content}])

    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    new_tags = lines[0] if lines else ""
    negative_additions = ""
    for line in lines[1:]:
        if line.upper().startswith("NEGATIVE:"):
            negative_additions = line.split(":", 1)[1].strip()
            break
    return new_tags, negative_additions


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
    """
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
            {"type": "text", "text": user_message},
        ]
    else:
        content = user_message

    messages = history + [{"role": "user", "content": content}]
    return await _ask_claude(
        CHAT_SYSTEM_PROMPT, messages, max_tokens=500, check_refused=False, tools=CHAT_TOOLS
    )
