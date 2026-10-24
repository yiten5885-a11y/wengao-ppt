"""wengao_ppt.research.runner

主流程执行器：
- run_research(material_text, options, job_dir, emit) -> ResearchBundle
- 串联规划、检索、抓取、抽取、核对、视图组装与报告导出
- 全过程不抛出异常，失败时优雅降级并记录 omissions
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import uuid
from typing import Any, Callable

from wengao_ppt.research.compare import assemble_views
from wengao_ppt.research.extract import extract_facts_from_snapshot
from wengao_ppt.research.fetch import fetch_url_text, save_snapshot
from wengao_ppt.research.ground import verify_fact_grounding
from wengao_ppt.research.plan import plan_research_queries
from wengao_ppt.research.report import write_reports_and_queries
from wengao_ppt.research.search import ResilientSearchBackend, SearchBackend
from wengao_ppt.research.types import (
    DiscardedRecord,
    Fact,
    OmissionRecord,
    ResearchBundle,
)

logger = logging.getLogger(__name__)


def run_research(
    material_text: str,
    options: dict[str, Any],
    job_dir: str,
    emit: Callable[[dict[str, Any]], None] = lambda ev: None,
    search_backend: SearchBackend | None = None,
) -> ResearchBundle:
    """执行联网研究流水线。

    关闭或出错时均返回合法 ResearchBundle，绝不抛出阻断异常。
    """
    research_opts = options.get("research", {})
    enabled = research_opts.get("enabled", True)
    bundle_id = f"rb-{uuid.uuid4().hex[:12]}"
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    material_lang = options.get("language", "zh")
    if material_lang == "auto":
        material_lang = "zh"

    # 若未启用，直接返回合法空 bundle
    if not enabled:
        bundle = ResearchBundle(
            bundle_id=bundle_id,
            created_at=now_iso,
            material_language=material_lang,
            enabled=False,
            queries=[],
            facts=[],
            views=[],
            discarded=[],
            omissions=[],
            report_path="evidence/research/report.md",
        )
        os.makedirs(os.path.join(job_dir, "evidence", "research"), exist_ok=True)
        bundle_file = os.path.join(job_dir, "evidence", "research", "bundle.json")
        with open(bundle_file, "w", encoding="utf-8") as f:
            json.dump(bundle.to_dict(), f, ensure_ascii=False, indent=2)
        write_reports_and_queries(bundle, job_dir)
        return bundle

    # 1. 发送规划阶段事件
    try:
        emit({
            "stage": "research",
            "status": "running",
            "summary": "正在解析输入材料并规划行业中性检索词...",
        })
    except Exception:
        pass

    backend = search_backend or ResilientSearchBackend()
    max_queries = research_opts.get("max_queries", 6)

    # 规划检索词并提取用户自带事实
    queries, user_facts = plan_research_queries(material_text, options, max_queries=max_queries)

    # 保存用户材料快照至 evidence/research/snapshots/user_material.txt 供双向追溯
    user_snapshot_rel = "evidence/research/snapshots/user_material.txt"
    user_snapshot_abs = os.path.join(job_dir, user_snapshot_rel)
    os.makedirs(os.path.dirname(user_snapshot_abs), exist_ok=True)
    with open(user_snapshot_abs, "w", encoding="utf-8") as f:
        f.write(material_text)
    import hashlib
    u_sha = hashlib.sha256(material_text.encode("utf-8")).hexdigest()
    for uf in user_facts:
        uf.snapshot_path = user_snapshot_rel
        uf.snapshot_sha256 = u_sha

    # 2. 依次执行检索并抓取快照
    try:
        emit({
            "stage": "research",
            "status": "running",
            "summary": f"已规划 {len(queries)} 条检索词，正在联网检索权威数据源...",
        })
    except Exception:
        pass

    candidate_facts: list[Fact] = []
    discarded: list[DiscardedRecord] = []
    fact_counter = 1
    seen_urls: set[str] = set()

    for q_record in queries:
        if len(candidate_facts) >= 6:
            break
        items = backend.search(q_record.q, locale=q_record.locale, max_results=2, timeout_s=6)
        for item in items[:2]:
            if len(candidate_facts) >= 6:
                break
            if item.url in seen_urls:
                continue
            seen_urls.add(item.url)

            # 抓取页面正文
            text_content = fetch_url_text(item.url, timeout_s=8)
            if not text_content:
                discarded.append(DiscardedRecord(
                    code="RESEARCH.FETCH_FAILED",
                    detail=f"无法抓取页面或有效正文过短: {item.title}",
                    url=item.url,
                ))
                continue

            # 临时生成一个 fact_id 前缀
            temp_fid = f"f{fact_counter:03d}"
            snap_rel, snap_sha = save_snapshot(job_dir, temp_fid, text_content)

            # 结构化抽取
            extracted = extract_facts_from_snapshot(
                text_content,
                source_url=item.url,
                publisher_hint=item.title,
                start_index=fact_counter,
            )
            if not extracted:
                discarded.append(DiscardedRecord(
                    code="RESEARCH.SOURCE_MISSING",
                    detail=f"页面中未抽取到合规数值事实: {item.title}",
                    url=item.url,
                ))
            else:
                for ef in extracted:
                    ef.snapshot_path = snap_rel
                    ef.snapshot_sha256 = snap_sha
                    candidate_facts.append(ef)
                    fact_counter += 1

    # 3. 逐字核验与一致性过滤 (Grounding Check)
    try:
        emit({
            "stage": "compare",
            "status": "running",
            "summary": "正在执行逐字原文比对与可比性核验...",
        })
    except Exception:
        pass

    accepted_facts: list[Fact] = list(user_facts)
    for cf in candidate_facts:
        is_grounded, disc_record = verify_fact_grounding(cf, job_dir)
        if is_grounded:
            accepted_facts.append(cf)
        else:
            if disc_record:
                discarded.append(disc_record)

    # 4. 组装对比与趋势视图
    try:
        emit({
            "stage": "trend",
            "status": "running",
            "summary": "正在基于真实数据生成横向对比与纵向趋势视图...",
        })
    except Exception:
        pass

    views, omissions, conflicts = assemble_views(accepted_facts, options)

    # 5. 生成完整 ResearchBundle
    bundle = ResearchBundle(
        bundle_id=bundle_id,
        created_at=now_iso,
        material_language=material_lang,
        enabled=True,
        queries=queries,
        facts=accepted_facts,
        views=views,
        discarded=discarded,
        omissions=omissions,
        report_path="evidence/research/report.md",
    )

    # 6. 落盘 bundle.json、report.md、queries.json
    res_dir = os.path.join(job_dir, "evidence", "research")
    os.makedirs(res_dir, exist_ok=True)
    bundle_file = os.path.join(res_dir, "bundle.json")
    with open(bundle_file, "w", encoding="utf-8") as f:
        json.dump(bundle.to_dict(), f, ensure_ascii=False, indent=2)

    write_reports_and_queries(bundle, job_dir)

    try:
        emit({
            "stage": "trend",
            "status": "done",
            "summary": f"联网研究完成：已核对 {len(accepted_facts)} 项事实，生成 {len(views)} 个视图",
            "counts": {"facts": len(accepted_facts), "views": len(views)},
            "refs": ["evidence/research/bundle.json", "evidence/research/report.md"],
        })
    except Exception:
        pass

    return bundle
