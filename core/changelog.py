# -*- coding: utf-8 -*-
"""
CHANGELOG.md에서 제일 최근 업데이트 항목 하나를 읽어오는 코드.

main.py가 봇을 켤 때마다 이걸 읽어서, 서버마다 지정해둔 "업데이트 로그 채널"에
아직 안 알려준 버전이면 올려준다 (core/settings_store.py의 last_announced_version과
비교). 코드를 고칠 때 CHANGELOG.md 맨 위에 새 "## 날짜" 항목을 추가해두기만 하면,
다음에 봇이 재시작될 때 자동으로 안내가 나간다.
"""
from __future__ import annotations

from pathlib import Path

CHANGELOG_PATH = Path(__file__).resolve().parent.parent / "CHANGELOG.md"


def get_latest_entry() -> tuple[str, str] | None:
    """제일 위에 있는 "## ..." 항목 하나를 (버전 표시, 내용) 형태로 돌려준다.

    파일이 없거나 항목이 하나도 없으면 None.
    """
    if not CHANGELOG_PATH.exists():
        return None

    lines = CHANGELOG_PATH.read_text(encoding="utf-8").splitlines()
    version = None
    body_lines: list[str] = []

    for line in lines:
        if line.startswith("## "):
            if version is None:
                version = line[3:].strip()
                continue
            break  # 두 번째 "## " 항목을 만나면 첫 항목은 다 읽은 것.
        if version is not None:
            body_lines.append(line)

    if version is None:
        return None

    body = "\n".join(body_lines).strip()
    return version, body
