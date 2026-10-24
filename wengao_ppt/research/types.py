"""wengao_ppt.research.types

数据契约模型定义，完全对齐公共契约 C4 (rs.research_bundle.v1)。
"""

from __future__ import annotations

import datetime
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Optional


@dataclass
class Fact:
    fact_id: str
    origin: Literal["USER_MATERIAL", "WEB_RESEARCH"]
    subject: str
    metric: str
    value: float
    unit: str
    period: str
    definition: str = ""
    publisher: str = ""
    source_grade: Literal["A", "B", "C"] = "B"
    url: str = ""
    retrieved_at: str = ""
    quote_span: str = ""
    snapshot_path: str = ""
    snapshot_sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SeriesPoint:
    x: str
    value: float
    fact_ref: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Series:
    label: str
    origin: Literal["USER_MATERIAL", "WEB_RESEARCH"]
    points: list[SeriesPoint] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "origin": self.origin,
            "points": [p.to_dict() if hasattr(p, "to_dict") else p for p in self.points],
        }


@dataclass
class Highlight:
    type: Literal["max_gap", "inflection", "rank_change", "streak"]
    text: str
    fact_refs: list[str] = field(default_factory=list)
    rule: str = ""
    computed: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Conflict:
    conflict_id: str
    user_fact: str
    web_fact: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Footnote:
    text: str
    fact_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class View:
    view_id: str
    kind: Literal["comparison", "trend", "insight"]
    title: str
    metric: str
    unit: str
    period: str
    series: list[Series] = field(default_factory=list)
    highlight: Optional[Highlight] = None
    conflicts: list[Conflict] = field(default_factory=list)
    footnote: Optional[Footnote] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "view_id": self.view_id,
            "kind": self.kind,
            "title": self.title,
            "metric": self.metric,
            "unit": self.unit,
            "period": self.period,
            "series": [s.to_dict() if hasattr(s, "to_dict") else s for s in self.series],
            "highlight": self.highlight.to_dict() if self.highlight else None,
            "conflicts": [c.to_dict() if hasattr(c, "to_dict") else c for c in self.conflicts],
            "footnote": self.footnote.to_dict() if self.footnote else None,
        }


@dataclass
class QueryRecord:
    q: str
    locale: str = "zh"
    backend: str = "searxng"
    sent_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DiscardedRecord:
    code: str
    detail: str
    url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OmissionRecord:
    want: str
    code: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ResearchBundle:
    schema: str = "rs.research_bundle.v1"
    bundle_id: str = ""
    created_at: str = ""
    material_language: str = "zh"
    enabled: bool = True
    queries: list[QueryRecord] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    views: list[View] = field(default_factory=list)
    discarded: list[DiscardedRecord] = field(default_factory=list)
    omissions: list[OmissionRecord] = field(default_factory=list)
    report_path: str = "evidence/research/report.md"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "bundle_id": self.bundle_id,
            "created_at": self.created_at or datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "material_language": self.material_language,
            "enabled": self.enabled,
            "queries": [q.to_dict() if hasattr(q, "to_dict") else q for q in self.queries],
            "facts": [f.to_dict() if hasattr(f, "to_dict") else f for f in self.facts],
            "views": [v.to_dict() if hasattr(v, "to_dict") else v for v in self.views],
            "discarded": [d.to_dict() if hasattr(d, "to_dict") else d for d in self.discarded],
            "omissions": [o.to_dict() if hasattr(o, "to_dict") else o for o in self.omissions],
            "report_path": self.report_path,
        }


@dataclass
class VerifyIssue:
    fact_id: Optional[str]
    code: str
    detail: str


@dataclass
class VerifyReport:
    schema: str = "rs.verify_report.v1"
    is_valid: bool = True
    checked_at: str = ""
    passed_facts_count: int = 0
    rejected_facts_count: int = 0
    issues: list[dict[str, Any]] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
