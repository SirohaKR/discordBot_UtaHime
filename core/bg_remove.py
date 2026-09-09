# -*- coding: utf-8 -*-
"""이미지 배경 제거 (스탠딩 그림용 투명 배경 만들기).

`rembg`(오픈소스, CPU만으로 동작)를 쓴다. 세그멘테이션 모델이라 이미지 생성(diffusion)보다
훨씬 가벼워서 GPU 없는 저사양 환경(NAS 등)에서도 초 단위로 처리된다. 애니메이션 캐릭터
컷아웃 전용으로 튜닝된 "isnet-anime" 모델을 써서 일반 배경 제거보다 결과가 더 깔끔하다.

세션(모델)은 첫 호출 때 한 번만 로드해서 재사용한다 — 매 요청마다 새로 만들면 느리다.
모델 파일은 처음 쓸 때 자동으로 다운로드되어 로컬에 캐싱된다(기본 ~/.u2net).
"""

_session = None


def _get_session():
    global _session
    if _session is None:
        from rembg import new_session

        _session = new_session("isnet-anime")
    return _session


def remove_background_bytes(image_bytes: bytes) -> bytes:
    """PNG/JPEG 등 이미지 바이트를 받아 배경을 제거한 PNG 바이트를 돌려준다.

    CPU 연산이라 시간이 좀 걸릴 수 있으니(저사양 환경 기준 수 초~수십 초), 호출부에서
    반드시 asyncio.to_thread 등으로 이벤트 루프를 막지 않게 실행해야 한다.
    """
    from rembg import remove

    return remove(image_bytes, session=_get_session())
