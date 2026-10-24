"""wengao_ppt.pipeline.decisions —— 冲突裁决（C4）读写。

规则：冲突裁决**不改 bundle**。用户的选择写进
`<job_dir>/evidence/research/conflict_decisions.json`：
    { "c01": "use_web | use_mine | open" }
W2 在规划/排版时读取；`open` 时页面采用网络值并标"待确认"。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

VALID = ("use_web", "use_mine", "open")


def decisions_path(job_dir: Path) -> Path:
    return Path(job_dir) / "evidence" / "research" / "conflict_decisions.json"


def load_decisions(job_dir: Path) -> dict:
    p = decisions_path(job_dir)
    if not p.is_file():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (ValueError, OSError):
        return {}


def record_decision(job_dir: Path, conflict_id: str, choice: str) -> dict:
    if choice not in VALID:
        raise ValueError(f"裁决取值 {choice!r} 不在 {VALID} 内")
    p = decisions_path(job_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    d = load_decisions(job_dir)
    d[conflict_id] = choice
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return d


def decision_for(decisions: dict, conflict_id: str) -> str:
    v = decisions.get(conflict_id, "open")
    return v if v in VALID else "open"


def open_conflicts(bundle: Optional[dict], decisions: dict) -> list:
    """返回尚未裁决的冲突 id 列表（界面"待确认卡"数据源）。"""
    if not bundle:
        return []
    out = []
    for v in bundle.get("views") or []:
        for c in v.get("conflicts") or []:
            cid = c.get("conflict_id")
            if cid and decision_for(decisions, cid) == "open":
                out.append(cid)
    return out
