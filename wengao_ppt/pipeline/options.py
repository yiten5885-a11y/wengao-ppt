"""wengao_ppt.pipeline.options —— JobOptions（C1）的加载、校验与默认值。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from wengao_ppt.contracts_rs import validate

DEFAULTS = {
    "schema": "rs.job_options.v1",
    "mode": "generate",
    "language": "auto",
    "research": {"enabled": True, "max_queries": 12, "locales": ["material", "en"]},
    "images": {"enabled": True, "allow_ai_generated": True,
               "per_scene_budget_s": 20, "deck_budget_s": 300},
    "transition": {"enabled": True, "family": "fade", "duration_ms": 500},
    "theme": None,
    "target_pages": None,
}


def normalize(raw: Optional[dict]) -> dict:
    """把任意用户输入合并成合法 JobOptions（深合并默认值）。"""
    opts = json.loads(json.dumps(DEFAULTS))
    for k, v in (raw or {}).items():
        if isinstance(v, dict) and isinstance(opts.get(k), dict):
            opts[k].update(v)
        else:
            opts[k] = v
    return validate(opts)


def load(path: Path) -> dict:
    return normalize(json.loads(Path(path).read_text(encoding="utf-8")))
