"""wengao_ppt.research.search.mock

离线测试检索适配器。
"""

from __future__ import annotations

from wengao_ppt.research.search.base import SearchBackend, SearchResultItem


class MockSearchBackend(SearchBackend):
    def __init__(self, predefined_results: dict[str, list[SearchResultItem]] | None = None):
        self.predefined_results = predefined_results or {}
        self.recorded_queries: list[dict] = []

    def set_results(self, query_substr: str, items: list[SearchResultItem]):
        self.predefined_results[query_substr] = items

    def search(
        self,
        query: str,
        locale: str = "zh",
        max_results: int = 5,
        timeout_s: int = 8,
    ) -> list[SearchResultItem]:
        self.recorded_queries.append({"query": query, "locale": locale})
        for k, items in self.predefined_results.items():
            if k in query or query in k:
                return items[:max_results]
        return []
