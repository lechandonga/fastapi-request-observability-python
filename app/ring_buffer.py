"""有上界的近期请求明细环形缓冲（线程安全）。

容量在构造时固定；超过容量后最老记录被自动淘汰，
长期运行下内存占用恒定，不无限增长。
"""

from __future__ import annotations

from collections import deque
from threading import Lock
from typing import Generic, TypeVar

T = TypeVar("T")


class RingBuffer(Generic[T]):
    def __init__(self, capacity: int) -> None:
        if not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("capacity 必须为正整数")
        self._capacity = capacity
        self._deque: deque[T] = deque(maxlen=capacity)
        self._lock = Lock()

    def add(self, item: T) -> None:
        with self._lock:
            self._deque.append(item)

    def snapshot(self) -> list[T]:
        """返回按插入顺序排列的副本（旧 -> 新）。"""
        with self._lock:
            return list(self._deque)

    def clear(self) -> None:
        with self._lock:
            self._deque.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._deque)

    @property
    def capacity(self) -> int:
        return self._capacity
