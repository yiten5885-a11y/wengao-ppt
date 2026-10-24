"""wengao_ppt.research.report

研究成果报告与查询清单导出：
- 生成 evidence/research/report.md 人类可读报告
- 导出 evidence/research/queries.json 实际发出的中性检索词清单供用户审计
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from wengao_ppt.research.types import ResearchBundle

logger = logging.getLogger(__name__)


def generate_research_report_markdown(bundle: ResearchBundle) -> str:
    lines = [
        "# 稳稿 PPT：联网研究与数据事实报告 (Research Report)",
        "",
        f"- **Bundle ID**: `{bundle.bundle_id}`",
        f"- **创建时间**: `{bundle.created_at}`",
        f"- **材料语言**: `{bundle.material_language}`",
        f"- **研究功能状态**: `{'已启用' if bundle.enabled else '已关闭'}`",
        "",
        "---",
        "",
        "## 1. 实际发出的外发检索词清单 (Queries)",
        "",
        "> [!NOTE] 隐私保护声明：根据公共契约，所有外发查询仅包含中性实体词与行业/财务指标词，严禁外发用户原文段落。",
        "",
        "| 序号 | 检索词 | 语言 | 检索后端 | 发送时间 |",
        "|---|---|---|---|---|",
    ]
    for idx, q in enumerate(bundle.queries, 1):
        lines.append(f"| {idx} | `{q.q}` | {q.locale} | {q.backend} | {q.sent_at} |")

    lines.extend([
        "",
        "## 2. 采纳的事实清单 (Accepted Facts)",
        "",
        "| Fact ID | 来源类型 | 主体 | 指标 | 数值与单位 | 周期 | 来源机构 | 等级 | 快照 SHA-256 |",
        "|---|---|---|---|---|---|---|---|---|",
    ])
    for f in bundle.facts:
        sha_short = f.snapshot_sha256[:12] if f.snapshot_sha256 else "—"
        lines.append(
            f"| `{f.fact_id}` | `{f.origin}` | {f.subject} | {f.metric} | **{f.value} {f.unit}** | {f.period} | {f.publisher or '—'} | {f.source_grade} | `{sha_short}` |"
        )

    lines.extend([
        "",
        "## 3. 对比与洞察视图 (Views)",
        "",
    ])
    if not bundle.views:
        lines.append("_未生成任何视图（数据不足或被省略）。_")
    else:
        for v in bundle.views:
            lines.append(f"### 视图 `{v.view_id}`: {v.title} ({v.kind})")
            lines.append(f"- **指标**: {v.metric} ({v.unit}) | **周期**: {v.period}")
            if v.highlight:
                lines.append(f"- **亮点发现 ({v.highlight.type})**: {v.highlight.text}")
                lines.append(f"  - 规则名: `{v.highlight.rule}`")
                lines.append(f"  - 计算细节: `{json.dumps(v.highlight.computed, ensure_ascii=False)}`")
            if v.conflicts:
                for c in v.conflicts:
                    lines.append(f"  - ⚠️ **数据冲突 `{c.conflict_id}`**: 用户材料事实 `{c.user_fact}` 与网络事实 `{c.web_fact}` 存在差异，已标'待确认'并列呈现。")
            if v.footnote:
                lines.append(f"- **页脚出处**: {v.footnote.text}")
            lines.append("")

    lines.extend([
        "## 4. 丢弃的未达标候选 (Discarded)",
        "",
    ])
    if not bundle.discarded:
        lines.append("_无丢弃条目。_")
    else:
        lines.append("| 错误码 | 详细原因 | 来源 URL |")
        lines.append("|---|---|---|")
        for d in bundle.discarded:
            lines.append(f"| `{d.code}` | {d.detail} | {d.url or '—'} |")

    lines.extend([
        "",
        "## 5. 降级省略项 (Omissions)",
        "",
    ])
    if not bundle.omissions:
        lines.append("_无省略项。_")
    else:
        lines.append("| 拟建视图/对比需求 | 错误码 / 理由 |")
        lines.append("|---|---|")
        for o in bundle.omissions:
            lines.append(f"| {o.want} | `{o.code}` |")

    lines.append("")
    return "\n".join(lines)


def write_reports_and_queries(bundle: ResearchBundle, job_dir: str):
    """将 report.md 与 queries.json 写入 job 证据目录。"""
    research_dir = os.path.join(job_dir, "evidence", "research")
    os.makedirs(research_dir, exist_ok=True)

    # 1. 写 report.md
    report_md = generate_research_report_markdown(bundle)
    report_path = os.path.join(job_dir, bundle.report_path)
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_md)

    # 2. 写 queries.json
    queries_path = os.path.join(research_dir, "queries.json")
    with open(queries_path, "w", encoding="utf-8") as f:
        queries_data = [q.to_dict() if hasattr(q, "to_dict") else q for q in bundle.queries]
        json.dump(queries_data, f, ensure_ascii=False, indent=2)
