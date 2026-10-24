"""wengao_ppt.research.search.base

检索后端抽象接口。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class SearchResultItem:
    title: str
    url: str
    snippet: str
    engine: str = ""
    score: float = 0.0


class SearchBackend(ABC):
    @abstractmethod
    def search(
        self,
        query: str,
        locale: str = "zh",
        max_results: int = 5,
        timeout_s: int = 8,
    ) -> list[SearchResultItem]:
        """执行检索，返回结果条目列表。失败或无结果时返回空列表，不得抛出异常。"""
        pass
