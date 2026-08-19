# -*- coding: utf-8 -*-
"""재생 대기열 관리자.

asyncio.Queue의 private 속성(`_queue`)을 직접 건드리는 대신,
셔플/반복을 명시적으로 지원하는 전용 자료구조를 쓴다.

핵심 아이디어(셔플이 "매끄럽게" 느껴지도록):
- 셔플 ON 상태에서 곡을 추가해도 대기열 전체를 다시 섞지 않는다.
  이미 정해진 다음 곡들의 순서는 그대로 두고, 새 곡만 무작위 위치에 끼워 넣는다.
  → 곡 몇 개를 잇달아 추가해도 "다음 곡" 예측이 매번 뒤집히지 않는다.
- 셔플을 켜는 순간에만 O(n) 셔플을 1회 수행하고, 끄면 원래 추가 순서로 즉시 복귀한다.
- 몇백 곡 규모에서 add/pop/toggle 모두 체감 지연 없이 동작한다(O(n)이어도 n이 작아 수 ms 이내).
"""

import random
from collections import deque


class SongQueue:
    def __init__(self):
        self._order = deque()    # 셔플 꺼짐 상태에서의 재생 순서(=추가 순서), 항상 최신 상태 유지
        self._shuffled = deque()  # 셔플 켜짐 상태에서 실제로 소비되는 순서
        self.shuffle = False

    def __len__(self):
        return len(self._order)

    def is_empty(self) -> bool:
        return not self._order

    def add(self, song) -> None:
        self._order.append(song)
        if self.shuffle:
            pos = random.randint(0, len(self._shuffled))
            self._shuffled.insert(pos, song)

    def add_many(self, songs) -> None:
        for song in songs:
            self.add(song)

    def pop_next(self):
        """다음 곡을 꺼내서 반환. 없으면 None."""
        if not self._order:
            return None
        if self.shuffle and self._shuffled:
            song = self._shuffled.popleft()
            self._order.remove(song)
        else:
            song = self._order.popleft()
        return song

    def peek_next(self):
        """다음 곡을 꺼내지 않고 미리 확인(프리페치용). 없으면 None."""
        if not self._order:
            return None
        if self.shuffle and self._shuffled:
            return self._shuffled[0]
        return self._order[0]

    def push_front(self, song) -> None:
        """한 곡 반복: 바로 다음 차례로 재생되도록 맨 앞에 삽입."""
        self._order.appendleft(song)
        if self.shuffle:
            self._shuffled.appendleft(song)

    def push_back(self, song) -> None:
        """전체 반복: 대기열 맨 뒤로 재삽입(셔플 중이면 무작위 위치)."""
        self.add(song)

    def set_shuffle(self, enabled: bool) -> None:
        self.shuffle = enabled
        if enabled:
            items = list(self._order)
            random.shuffle(items)
            self._shuffled = deque(items)
        else:
            self._shuffled.clear()

    def clear(self) -> None:
        self._order.clear()
        self._shuffled.clear()

    def to_list(self):
        """대기열 보기용: 실제로 재생될 순서 그대로 반환."""
        return list(self._shuffled) if self.shuffle else list(self._order)
