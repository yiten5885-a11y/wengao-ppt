"""最小运行器：唯一入口、任务锁、阶段日志、候选/正式分离、超时、取消、幂等、失败保护。

- status.json 由 runner 唯一写入者原子更新；模型不得写 status/passed/waiver/verdict。
- run_id / attempt_id 唯一；同一请求幂等，重试产生新 attempt_id。
- 候选产物先写独立位置，完整验证后才成为当前版本；候选失败不覆盖上一有效版本。
- 本阶段必需检查未实现 → GATE.CHECK_NOT_IMPLEMENTED，正式 READY 必须被拦截。
"""
from __future__ import annotations

import json
import hashlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

from . import __version__
from .core import save_json_atomic, load_json, sha256_file
from .extractors import extract_any
from . import checks as checks_mod
from . import source_fidelity as source_fidelity_mod
from . import theme as theme_mod
from . import render as render_mod
from . import glyphcheck as glyph_mod
from . import fontenv as fontenv_mod
from . import incident as incident_mod

CST = timezone(timedelta(hours=8))
CONTRACT_VERSION = "contracts/1.1.0"
SUPPORT_PROFILE = "P1_TEXT_LIMITED"
P2_SUPPORT_PROFILE = "P2_TABLE_LIMITED"
P2_CHART_SUPPORT_PROFILE = "P2_CHART_LIMITED"
INTERNAL_CANDIDATE_LABEL = "DEVELOPMENT_CANDIDATE"

# P1 到期必需检查。未实现/未运行的项必须记 NOT_EVALUABLE 并阻断正式 READY。
P1_REQUIRED_CHECKS = [
    "content_fidelity.required_text",
    "content_fidelity.occurrence",
    "content_fidelity.sign",
    "native_editability.visible_native",
    "render.real_render",
]

# P2 到期必需检查（在 P1 基础上增加规则表关系与原生编辑实证）。
P2_REQUIRED_CHECKS = P1_REQUIRED_CHECKS + [
    "table.native_present",
    "table.relation_binding",
    "parser.object_agreement",
]

# P2 图表子范围必需检查：原生图表存在、系列—类别—数值绑定、工作簿与缓存一致。
# 双解析一致性（R09）同样适用：图表藏在组合里时两个视角必须能对上。
CHART_REQUIRED_CHECKS = [
    "chart.native_present",
    "chart.relation_binding",
    "chart.source_consistency",
    "parser.object_agreement",
]

# 任何 profile 都到期的检查：几何必须来自**实际候选 PPTX**，不能来自布局记录（审查 A03）。
ALWAYS_REQUIRED_CHECKS = ["object.actual_geometry", "content_fidelity.hidden_state",
                          "object.group_structure", "parser.census_consistency",
                          # 事故后新增：字形是否真的画出来（不是文本层有字就算）、
                          # 主题改色后文字与实际底色的对比度是否达标。
                          # 这两项不许从必需集合里消失，缺项=NOW_IMPLEMENTED=阻断。
                          "render.glyph_quality", "theme.contrast",
                          # HG01–HG12 排版硬门禁：每个候选都必须过实际页图/文本/层级检查；
                          # 原生录像或任何静态证据缺失时仍以 NOT_EVALUABLE 阻断出口。
                          "layout.hard_gate",
                          # HG13：按具体子场景语义选择最佳页、全稿去重并核验真实嵌入照片。
                          "visual.semantic_scene_coverage"]

LAYOUT_GATE_POLICY_FILE = "layout_hard_gate_policy.json"
LAYOUT_GATE_POLICY_BUNDLE_SHA256 = (
    "0009870f902fe7c4035ab5b74c8dd3f3b08b63e33c9fbb786e7de07ea613a809")
LAYOUT_GATE_POLICY_DOCUMENT_SHA256 = (
    "3a1e4498bf2b5ec49bce8e6c1042a7625454de561607c47c548096c840645d96")

# 当前实际可执行的检查全集。W01：预期必需集合 = profile 侧 ∪（内容侧 ∩ 本集合）。
# 内容侧推导出但仍未实现的项不混入必需集合，而是登记为 scope_gaps 并如实暴露，
# 既不允许"带图表的输入降成纯文字范围"，也不允许用未实现项假装已核验。
IMPLEMENTED_CHECKS = {
    "content_fidelity.required_text", "content_fidelity.occurrence",
    "content_fidelity.sign", "native_editability.visible_native",
    "render.real_render", "object.actual_geometry",
    "content_fidelity.hidden_state", "object.group_structure",
    "content_fidelity.notes", "object.media_binding", "parser.census_consistency",
    "table.native_present", "table.relation_binding",
    "parser.object_agreement",
    "chart.native_present", "chart.relation_binding", "chart.source_consistency",
    "field.break_fidelity",
    # R5 材料生成：由**原材料冻结台账**到候选的来源侧覆盖核对。
    # 与上面"以候选自身为基线"的检查不同：这一项的期望集合在模型调用**之前**
    # 就已冻结，候选不得决定"应该有什么"。
    "source.fact_coverage",
    "render.glyph_quality",
    "theme.contrast",
    "layout.hard_gate",
    "visual.semantic_scene_coverage",
}

# 内容侧推导：原始对象台账里出现什么，就必须到期什么（W01）。
CONTENT_DERIVED_REQUIRED = {
    "has_tables": ["table.native_present", "table.relation_binding"],
    "has_charts": ["chart.native_present", "chart.relation_binding",
                   "chart.source_consistency"],
    "has_notes": ["content_fidelity.notes"],
    "has_media": ["object.media_binding"],
}


def profile_required_checks(profile):
    if profile == P2_SUPPORT_PROFILE:
        return list(P2_REQUIRED_CHECKS)
    if profile == P2_CHART_SUPPORT_PROFILE:
        return list(P1_REQUIRED_CHECKS) + list(CHART_REQUIRED_CHECKS)
    return list(P1_REQUIRED_CHECKS)


def required_checks_for(profile):
    """保留旧签名：只给 profile 侧集合，供历史调用点与 precheck 使用。"""
    return profile_required_checks(profile)


#: 材料任务身份在**建任务时**冻结在此文件；它不依赖台账是否还在、
#: 是否被换过。缺失台账不再等于"退回普通 PPTX 自基线路径"。
MATERIAL_INTENT_FILE = "material_intent.json"
MATERIAL_LEDGER_FILE = "material_fact_ledger.json"
MATERIAL_PLAN_FILE = "material_content_plan.json"


def material_intent_path(job_dir):
    return Path(job_dir) / MATERIAL_INTENT_FILE


def load_material_intent(job_dir):
    """读取冻结的材料任务身份；不存在返回 None（普通 PPTX 任务）。

    文件存在但损坏时返回 {"material": True, "corrupt": True}——**仍然算材料任务**，
    绝不因"读不出来"就把它降级成可自基线放行的普通任务。
    """
    p = material_intent_path(job_dir)
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            return {"material": True, "corrupt": True}
        d.setdefault("material", True)
        return d
    except Exception:  # noqa: BLE001
        return {"material": True, "corrupt": True}


def load_material_source(job_dir, material_intent=None):
    """返回 (ledger, plan, status, reason)。

    身份约定（关键）：
    - **材料任务**（存在 material_intent）→ 无论台账在不在，
      `source.fact_coverage` 都是必需检查；台账缺失/损坏/未冻结/身份不符时
      状态为 NOT_EVALUABLE 且带明确原因，正常接纳/导出必须被拒绝。
    - **非材料任务**（无 material_intent）→ 保持原普通 PPTX 美化路径，
      status="NOT_MATERIAL"。
    """
    if material_intent is None:
        material_intent = load_material_intent(job_dir)
    if not material_intent:
        return None, None, "NOT_MATERIAL", "非材料任务：按普通 PPTX 路径处理"
    if material_intent.get("corrupt"):
        return None, None, "MATERIAL_INTENT_CORRUPT", "材料任务身份文件损坏，无法核对来源"

    ledger_p = Path(job_dir) / MATERIAL_LEDGER_FILE
    if not ledger_p.is_file():
        return None, None, "MATERIAL_LEDGER_MISSING", "材料任务缺少冻结台账，来源核对不可执行"
    try:
        ledger = json.loads(ledger_p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return None, None, "MATERIAL_LEDGER_CORRUPT", f"冻结台账损坏（{type(e).__name__}）"
    if not ledger.get("frozen_before_model_call"):
        return None, None, "MATERIAL_LEDGER_NOT_FROZEN", \
            "台账未声明冻结于模型调用之前，不得作为期望集合"

    want = material_intent.get("ledger_sha256")
    if want and sha256_file(ledger_p) != want:
        return None, None, "MATERIAL_LEDGER_IDENTITY_MISMATCH", \
            "台账内容与建任务时冻结的身份不一致（疑似被候选侧重写）"

    plan = None
    plan_p = Path(job_dir) / MATERIAL_PLAN_FILE
    if not plan_p.is_file():
        return None, None, "MATERIAL_PLAN_MISSING", "材料任务缺少内容计划，来源核对不可执行"
    try:
        plan = json.loads(plan_p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return None, None, "MATERIAL_PLAN_CORRUPT", f"内容计划损坏（{type(e).__name__}）"
    wp = material_intent.get("plan_sha256")
    if wp and sha256_file(plan_p) != wp:
        return None, None, "MATERIAL_PLAN_IDENTITY_MISMATCH", \
            "内容计划与建任务时冻结的身份不一致"

    return ledger, plan, "OK", "来源核对可执行"


def _glyph_check_status(glyph) -> str:
    """`render.glyph_quality` 这一项的 checks_map 状态。

    判定依据是**可信预期**（`glyph["applicability"]`），不是输出侧分母：
      * 预期无法建立 → NOT_EVALUABLE（配合 GLYPH.EXPECTATION_UNAVAILABLE 阻断）；
      * 输出侧判定缺字 → FAILED；
      * 预期明确为 0（真的没有原生中文）→ EXECUTED，子检查不适用但**其他检查不跳过**；
      * 预期 > 0 且输出侧确实评估通过（OK）→ EXECUTED；
      * 其余（预期 > 0 但证据不足等）→ NOT_EVALUABLE，不得记成通过。
    """
    g = glyph or {}
    applicability = g.get("applicability")
    verdict = g.get("verdict")
    if applicability == "EXPECTATION_UNAVAILABLE":
        return "NOT_EVALUABLE"
    if verdict == "MISSING":
        return "FAILED"
    if applicability in ("NOT_APPLICABLE_EXPECTED_ZERO", "APPLICABLE") \
            and verdict in ("OK", "NOT_APPLICABLE"):
        return "EXECUTED"
    return "NOT_EVALUABLE"


def expected_required_checks(profile, census=None, in_doc=None, rs_options=None) -> dict:
    """W01：预期必需检查集合 = profile 侧 ∪ 内容侧（原始对象台账）∪ 恒成立项。

    返回值带三块信息：
      - required：本次实际到期的集合（不含未实现项）
      - scope_gaps：内容侧到期但当前未实现的项（如实暴露，不冒充已核验）
      - derived_from：每一项由什么推导出来，便于复核"谁把必需项删掉了"

    rs_options：RS 管线（W4）传入的 JobOptions；传入时把 RS 条件必检项
    （配图账本/转场一致性/研究证据/版式复核）并入 required——**只加不减**。
    不传（旧美化/材料路径）时行为与本函数历史版本完全一致。
    """
    derived = {}
    for name in ALWAYS_REQUIRED_CHECKS:
        derived.setdefault(name, []).append("always")
    for name in profile_required_checks(profile):
        derived.setdefault(name, []).append(f"profile:{profile}")

    content_flags = set()
    k = ((census or {}).get("kind_counts") or {})
    if k.get("charts"):
        content_flags.add("has_charts")
    # T1：原始输入里有字段或软换行事件，来源→候选的事件级保真就必须到期。
    cc_counts = (((census or {}).get("content_census") or {}).get("counts") or {})
    if cc_counts.get("fields") or cc_counts.get("soft_breaks"):
        derived.setdefault("field.break_fidelity", []).append(
            "content:has_field_or_break_events")
    if in_doc is not None:
        raw_tables = (in_doc.get("tables", []) if isinstance(in_doc, dict)
                      else getattr(in_doc, "tables", []))
        raw_charts = (in_doc.get("charts", []) if isinstance(in_doc, dict)
                      else getattr(in_doc, "charts", []))
        pages = (in_doc.get("pages", []) if isinstance(in_doc, dict)
                 else getattr(in_doc, "pages", []))
        objs = (in_doc.get("objects", []) if isinstance(in_doc, dict)
                else getattr(in_doc, "objects", []))
        if raw_tables:
            content_flags.add("has_tables")
        if raw_charts:
            content_flags.add("has_charts")
        if any((p.get("notes") or "").strip() for p in pages):
            content_flags.add("has_notes")
        if any("PICTURE" in str(o.get("type", "")).upper() for o in objs):
            content_flags.add("has_media")

    scope_gaps = []
    for flag in sorted(content_flags):
        for name in CONTENT_DERIVED_REQUIRED.get(flag, []):
            if name in IMPLEMENTED_CHECKS:
                derived.setdefault(name, []).append(f"content:{flag}")
            else:
                scope_gaps.append({"check": name, "derived_from": f"content:{flag}",
                                   "detail": "输入台账推导出该项到期，但当前未实现；"
                                             "登记为缺口，不冒充已核验。"})
    exp = {"required": sorted(derived),
           "derived_from": {k2: sorted(set(v)) for k2, v in derived.items()},
           "scope_gaps": scope_gaps,
           "material_source": None,
           "material_plan": None,
           "rs_checks": {}}
    if rs_options:
        # RS 条件必检项并入（只加不减；wengao_ppt.gate_rs 拥有注册表）
        try:
            from wengao_ppt import gate_rs
            exp = gate_rs.extend_expected(exp, rs_options)
        except Exception:  # noqa: BLE001 —— 注册表不可用时不凭空加项
            pass
    return exp


def now_iso():
    return datetime.now(CST).isoformat()


class GateError(Exception):
    def __init__(self, code, detail):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def verify_input_package(input_path):
    """真实可读性核对：必须是可打开的 OOXML 包且含演示文稿主部件。

    D10-A（U2 实测发现的缺陷）：此前只按扩展名判断，损坏文件/伪装的 .pptx
    能通过预检、建出 QUEUED 任务，要到运行或验收才失败 —— 界面在预检这一步
    把"根本不是 PPTX 的输入"说成了"待处理任务"。现在在唯一的建任务入口核对。
    """
    p = Path(input_path)
    if not p.is_file():
        raise GateError("INPUT.NOT_FOUND", f"原稿不存在：{p}")
    try:
        with zipfile.ZipFile(p) as z:
            names = set(z.namelist())
    except zipfile.BadZipFile as e:
        raise GateError("INPUT.NOT_A_PPTX_PACKAGE",
                        f"不是合法的 OOXML 压缩包（PPTX），无法作为原稿处理：{e}")
    if "[Content_Types].xml" not in names:
        raise GateError("INPUT.NOT_A_PPTX_PACKAGE",
                        "包内缺少 [Content_Types].xml，不是合法 PPTX。")
    if not any(n.startswith("ppt/presentation.xml") for n in names):
        raise GateError("INPUT.NOT_A_PPTX_PACKAGE",
                        "包内缺少 ppt/presentation.xml，不是 PowerPoint 演示文稿。")


class Runner:
    def __init__(self, project_root):
        self.root = Path(project_root)
        self.jobs = self.root / "jobs"
        self.jobs.mkdir(parents=True, exist_ok=True)

    # ---------------- 任务与状态 ----------------
    def _job_dir(self, job_id):
        return self.jobs / job_id

    # ---------------- 契约身份 ----------------
    def contracts_dir(self):
        """定位契约目录：数据根优先，其次产品包自身（安装后契约随代码走）。"""
        for cand in (Path(self.root) / "contracts",
                     Path(__file__).resolve().parents[1] / "contracts"):
            if cand.is_dir():
                return cand
        return Path(self.root) / "contracts"

    def vendor_dir(self):
        """定位 vendor 目录：数据根优先，其次产品包自身（随代码分发）。"""
        for cand in (Path(self.root) / "vendor" / "luobo-ppt",
                     Path(__file__).resolve().parents[1] / "vendor" / "luobo-ppt"):
            if cand.is_dir():
                return cand
        return Path(self.root) / "vendor" / "luobo-ppt"

    def contracts_identity(self) -> dict:
        cdir = self.contracts_dir()
        out = {}
        for n in ("error_codes_v1.json", "schemas_v1.json", "support_matrix_v1.json",
                  LAYOUT_GATE_POLICY_FILE):
            # This is a shipped acceptance policy, not mutable job data. Resolve it
            # from the program package even when a data root contains legacy contracts.
            fp = ((Path(__file__).resolve().parents[1] / "contracts" / n)
                  if n == LAYOUT_GATE_POLICY_FILE else cdir / n)
            if fp.is_file():
                out[n] = sha256_file(fp)
        return out

    def attach_native_playback_evidence(self, job_id, video_bytes, review_record,
                                        expected_candidate_sha256):
        """Attach one native playback recording to the exact current candidate.

        The uploader cannot set a pass flag. It supplies a video and a detailed
        reviewer record; validation later parses the media, checks its hash and
        duration, runs a full local decode, and independently checks the record.
        """
        with self._lock(job_id, "native_playback_evidence"):
            d = self._job_dir(job_id)
            job = load_json(d / "job.json")
            if not isinstance(job, dict):
                raise GateError("JOB.NOT_FOUND", "任务不存在，无法附加原生实播证据。")
            candidate = d / "candidate" / (Path(job["input_path"]).stem + ".themed.pptx")
            if not candidate.is_file():
                raise GateError("GATE.ARTIFACT_MISSING", "当前候选 PPTX 不存在。")
            actual_candidate_sha = sha256_file(candidate)
            if (not isinstance(expected_candidate_sha256, str)
                    or actual_candidate_sha != expected_candidate_sha256):
                raise GateError("EVIDENCE.STALE",
                                "页面显示的候选已变化；请刷新后再附加复核证据。")
            if not isinstance(video_bytes, (bytes, bytearray)) or not video_bytes:
                raise GateError("EVIDENCE.NATIVE_VIDEO_MISSING", "录像文件为空。")
            if not isinstance(review_record, dict) or \
                    review_record.get("schema") != "WG-NATIVE-REVIEW/1":
                raise GateError("EVIDENCE.NATIVE_REVIEW_SCHEMA",
                                "复核记录必须使用 WG-NATIVE-REVIEW/1 格式。")
            video_sha = hashlib.sha256(video_bytes).hexdigest()
            if (review_record.get("pptx_sha256") != actual_candidate_sha
                    or review_record.get("video_sha256") != video_sha):
                raise GateError("EVIDENCE.NATIVE_REVIEW_IDENTITY",
                                "复核 JSON 中的 PPTX/录像哈希与本次上传不一致。")
            if review_record.get("reopened") is not True or \
                    review_record.get("reopened_pptx_sha256") != actual_candidate_sha:
                raise GateError("EVIDENCE.NATIVE_REOPEN_INVALID",
                                "复核记录未确认关闭后从磁盘重开同一候选或重开哈希不符。")

            # 门禁前置校验：拒绝合成的空 review 与伪造 MP4 头进入证据库
            program_root = Path(__file__).resolve().parents[1]
            gate_path = program_root / "scripts" / "layout_gate.py"
            if gate_path.is_file():
                import importlib.util
                spec = importlib.util.spec_from_file_location("wengao_layout_gate_runtime", gate_path)
                if spec and spec.loader:
                    gate_mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(gate_mod)
                    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
                        tf.write(bytes(video_bytes))
                        tf_path = Path(tf.name)
                    try:
                        mp4_info = gate_mod._inspect_mp4(tf_path)
                        if not mp4_info.get("ok"):
                            raise GateError("EVIDENCE.NATIVE_VIDEO_INVALID",
                                            f"实播录像 MP4 校验失败：{mp4_info.get('error')}")
                        if not 30.0 <= mp4_info.get("duration_seconds", 0) <= 60.0:
                            raise GateError("EVIDENCE.NATIVE_VIDEO_DURATION",
                                            f"实播录像时长必须在 30–60 秒区间内，实测为 {mp4_info.get('duration_seconds')} 秒")
                    finally:
                        tf_path.unlink(missing_ok=True)
                    from pptx import Presentation
                    prs = Presentation(str(candidate))
                    all_pages = list(range(1, len(prs.slides) + 1))
                    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", encoding="utf-8", delete=False) as rtf:
                        json.dump(review_record, rtf)
                        rtf_path = Path(rtf.name)
                    try:
                        rev_dict = {"record_path": str(rtf_path), "record_sha256": gate_mod.sha256(rtf_path)}
                        ok_rev, issues = gate_mod._validate_native_review_record(
                            rev_dict, actual_candidate_sha, video_sha, all_pages)
                        if not ok_rev:
                            err_msg = issues[0].get("note") if issues else "复核记录不合规"
                            raise GateError("EVIDENCE.NATIVE_REVIEW_INVALID", f"复核记录校验未通过：{err_msg}")
                    finally:
                        rtf_path.unlink(missing_ok=True)

            evidence_dir = d / "layout_gate_evidence" / actual_candidate_sha
            evidence_dir.mkdir(parents=True, exist_ok=True)
            video_path = evidence_dir / f"playback-{video_sha}.mp4"
            if not video_path.exists():
                tmp_video = evidence_dir / f".playback-{video_sha}.tmp"
                tmp_video.write_bytes(bytes(video_bytes))
                os.replace(tmp_video, video_path)
            review_json_path = evidence_dir / f"review-{video_sha}.json"
            if not review_json_path.exists():
                save_json_atomic(review_record, review_json_path)
            review_sha = sha256_file(review_json_path)
            pages = review_record.get("pages")
            pages = ([x.get("page") for x in pages if isinstance(x, dict)]
                     if isinstance(pages, list) else None)
            reopened_sha = review_record.get("reopened_pptx_sha256")
            payload = {
                "schema": "WG-NATIVE-PLAYBACK-EVIDENCE/1",
                "pptx_sha256": actual_candidate_sha,
                "video_path": str(video_path),
                "video_sha256": video_sha,
                "pages": pages,
                "reopen": {"reopened": review_record.get("reopened") is True,
                           "reopened_pptx_sha256": reopened_sha},
                "independent_review": {"record_path": str(review_json_path),
                                       "record_sha256": review_sha},
                "attempt_id": job.get("attempt_id"),
                "attached_at": now_iso(),
            }
            manifest_path = evidence_dir / "native_playback.json"
            save_json_atomic(payload, manifest_path)
            return {"attached": True, "job_id": job_id,
                    "candidate_pptx_sha256": actual_candidate_sha,
                    "video_sha256": video_sha,
                    "review_record_sha256": review_sha,
                    "manifest_path": str(manifest_path.relative_to(d)),
                    "manifest_sha256": sha256_file(manifest_path),
                    "note": "仅收录证据；门禁必须重新运行且全项通过才会允许导出。"}

    def _run_layout_hard_gate(self, job_id, job, pptx, pdf, exp, cancel_check=None):
        """Run the frozen HG01–HG12 gate against this exact candidate and render.

        Gate inputs and reports live under the job data root, never in the shipped
        program directory.  Each invocation gets a fresh directory so old PNG/PDF
        evidence cannot silently bind to a changed candidate.
        """
        d = self._job_dir(job_id)
        pptx = Path(pptx)
        pdf = Path(pdf) if pdf else None
        candidate_sha = sha256_file(pptx) if pptx.is_file() else None
        stamp = f"{time.time_ns()}"
        run_dir = d / "layout_gate_runs" / f"{(candidate_sha or 'missing')[:16]}-{stamp}"
        run_dir.mkdir(parents=True, exist_ok=False)
        report_path = run_dir / "layout_gate.json"
        program_root = Path(__file__).resolve().parents[1]
        policy_path = program_root / "contracts" / LAYOUT_GATE_POLICY_FILE
        gate_path = program_root / "scripts" / "layout_gate.py"
        report = {
            "schema": "WG-WORKBENCH-LAYOUT-GATE/1",
            "verdict": "UNVERIFIED", "findings": [],
            "unverified": [], "pages_checked": [], "counts": {},
        }
        source = Path(job.get("input_path") or "")
        binding = {
            "job_id": job_id,
            "run_id": job.get("run_id"),
            "attempt_id": job.get("attempt_id"),
            "candidate_pptx_sha256": candidate_sha,
            "candidate_path": str(pptx),
            "source_sha256": sha256_file(source) if source.is_file() else None,
            "source_path": str(source),
            "pdf_sha256": sha256_file(pdf) if pdf and pdf.is_file() else None,
            "pdf_path": str(pdf) if pdf else None,
            "policy_bundle_sha256": None,
            "policy_document_sha256": LAYOUT_GATE_POLICY_DOCUMENT_SHA256,
            "layout_gate_source_sha256": sha256_file(gate_path) if gate_path.is_file() else None,
            "static_only": True,
        }
        status = "NOT_EVALUABLE"
        details = []
        try:
            if not gate_path.is_file():
                raise RuntimeError("布局门禁实现文件缺失")
            if not policy_path.is_file():
                raise RuntimeError("布局硬门禁政策文件缺失")
            policy_bytes = policy_path.read_bytes()
            policy_sha = hashlib.sha256(policy_bytes).hexdigest()
            binding["policy_bundle_sha256"] = policy_sha
            if policy_sha != LAYOUT_GATE_POLICY_BUNDLE_SHA256:
                raise RuntimeError("布局硬门禁政策文件哈希与冻结身份不符")
            policy = json.loads(policy_bytes.decode("utf-8"))
            if (policy.get("policy_version") != "WG-LAYOUT-20261021.1"
                    or policy.get("document_sha256") != LAYOUT_GATE_POLICY_DOCUMENT_SHA256):
                raise RuntimeError("布局硬门禁政策版本/源文档身份不符")
            if not pdf or not pdf.is_file():
                raise RuntimeError("本轮真实 PDF 渲染缺失")
            if cancel_check and cancel_check():
                raise RuntimeError("验收收到取消请求，未继续布局门禁")

            # Gate coordinates are native slide numbers. LibreOffice omits hidden
            # slides from PDF; until a hidden-slide render mapping is independently
            # implemented, fail closed rather than shift all subsequent pages.
            from pptx import Presentation
            prs = Presentation(str(pptx))
            hidden = [i + 1 for i, slide in enumerate(prs.slides)
                      if slide._element.get("show") in ("0", "false", "off")]
            pdf_pages, pdf_error = render_mod.pdf_page_count(pdf)
            binding["pptx_page_count"] = len(prs.slides)
            binding["hidden_pages"] = hidden
            binding["pdf_page_count"] = pdf_pages
            if hidden or pdf_pages != len(prs.slides):
                raise RuntimeError(
                    "页码与真实 PDF 无法一一绑定："
                    f"PPTX={len(prs.slides)}，PDF={pdf_pages}，隐藏页={hidden}，"
                    f"PDF解析={pdf_error}")

            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "wengao_layout_gate_runtime", gate_path)
            if spec is None or spec.loader is None:
                raise RuntimeError("无法载入布局门禁实现")
            gate_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(gate_mod)

            full_png_dir = run_dir / "png-full-1920"
            png_full = gate_mod.render_pngs(pdf, full_png_dir, "full")
            no_text_dir = run_dir / "notext"
            no_text_dir.mkdir(parents=True, exist_ok=True)
            no_text_pptx = no_text_dir / f"{pptx.stem}-notext.pptx"
            gate_mod.strip_text_pptx(pptx, no_text_pptx)
            if cancel_check and cancel_check():
                raise RuntimeError("验收收到取消请求，无字版渲染前停止")
            no_text_render_dir = no_text_dir / "render"
            no_text_render = render_mod.render_real(
                no_text_pptx, no_text_render_dir,
                cancel_check=cancel_check,
                pid_file=str(self._render_pid_file(job_id)), data_root=self.root)
            binding["notext_render"] = {
                "code": no_text_render.get("code"),
                "pdf_sha256": no_text_render.get("pdf_sha256"),
                "renderer": no_text_render.get("renderer"),
                "font_environment": no_text_render.get("font_environment"),
            }
            if no_text_render.get("code") != "RENDER.OK" or not no_text_render.get("pdf"):
                raise RuntimeError("无字版真实渲染未成功：" +
                                   str(no_text_render.get("code") or "unknown"))
            png_notext = gate_mod.render_pngs(
                Path(no_text_render["pdf"]), no_text_dir / "png-1920", "notext")

            baseline = source if source.is_file() and source.suffix.lower() == ".pptx" else None
            source_fidelity_verified = (
                exp.get("material_status") == "OK"
                and exp.get("source_fact_coverage_status") == "EXECUTED")
            native_path = (d / "layout_gate_evidence" / candidate_sha
                           / "native_playback.json")
            native_playback = None
            if native_path.is_file():
                native_playback = load_json(native_path)
                if native_playback is None:
                    native_playback = {}
                binding["native_playback_manifest"] = str(native_path.relative_to(d))
                binding["native_playback_manifest_sha256"] = sha256_file(native_path)
            binding["static_only"] = native_playback is None
            report = gate_mod.check(
                pptx, pdf, pages=None, baseline=baseline,
                expect_pptx_sha=candidate_sha, policy=policy,
                png_full=png_full, png_notext=png_notext, outdir=None,
                static_only=(native_playback is None),
                native_playback=native_playback,
                source_fidelity_verified=source_fidelity_verified)
            status = ("FAILED" if report.get("verdict") == "FAIL"
                      else "EXECUTED" if report.get("verdict") == "PASS"
                      and not report.get("findings") and not report.get("unverified")
                      else "NOT_EVALUABLE")
        except Exception as exc:  # noqa: BLE001 — checker failures are evidence gaps, never passes
            details.append({"code": "LAYOUT.HARD_GATE_EXECUTION_ERROR", "severity": "REVIEW",
                            "type": "LAYOUT_GATE_EXECUTION_UNVERIFIED",
                            "error_type": type(exc).__name__,
                            "detail": str(exc)[:500]})
            report = {
                "schema": "WG-WORKBENCH-LAYOUT-GATE/1",
                "verdict": "UNVERIFIED", "findings": [],
                "unverified": [{"gate": "HG10",
                                "type": "LAYOUT_GATE_EXECUTION_UNVERIFIED",
                                "detail": str(exc)[:500]}],
                "pages_checked": [], "counts": {},
            }
        report["workbench_binding"] = binding
        save_json_atomic(report, report_path)
        report_sha = sha256_file(report_path)
        summary = {
            "status": status,
            "verdict": report.get("verdict", "UNVERIFIED"),
            "report_path": str(report_path.relative_to(d)),
            "report_sha256": report_sha,
            "candidate_pptx_sha256": candidate_sha,
            "pdf_sha256": binding.get("pdf_sha256"),
            "policy_bundle_sha256": binding.get("policy_bundle_sha256"),
            "policy_document_sha256": binding.get("policy_document_sha256"),
            "layout_gate_source_sha256": binding.get("layout_gate_source_sha256"),
            "pages_checked": report.get("pages_checked", []),
            "page_count": binding.get("pptx_page_count"),
            "finding_count": len(report.get("findings") or []),
            "unverified_count": len(report.get("unverified") or []),
            "static_only": binding.get("static_only", True),
        }
        if status == "FAILED":
            details.append({"code": "LAYOUT.HARD_GATE_FAILED", "severity": "BLOCKING",
                            "gate_verdict": report.get("verdict"),
                            "finding_count": summary["finding_count"],
                            "report_path": summary["report_path"],
                            "report_sha256": report_sha})
        elif status != "EXECUTED":
            details.append({"code": "LAYOUT.HARD_GATE_UNVERIFIED", "severity": "REVIEW",
                            "gate_verdict": report.get("verdict", "UNVERIFIED"),
                            "unverified_count": summary["unverified_count"],
                            "report_path": summary["report_path"],
                            "report_sha256": report_sha,
                            "note": "缺证据/检查异常不能当通过；下载与正式放行保持阻断。"})
        return status, summary, details

    def _layout_gate_evidence_problems(self, d, validation):
        """Re-bind the separate hard-gate report before export/release decisions."""
        problems = []
        gate = validation.get("layout_hard_gate")
        if not isinstance(gate, dict):
            return ["验证记录缺少 layout.hard_gate 身份"]
        rel = gate.get("report_path")
        if not isinstance(rel, str) or not rel:
            return ["layout.hard_gate 缺少门禁报告路径"]
        report_path = (Path(d) / rel).resolve()
        try:
            report_path.relative_to(Path(d).resolve())
        except ValueError:
            return ["layout.hard_gate 报告路径越出当前任务目录"]
        if not report_path.is_file():
            return ["layout.hard_gate 报告文件缺失"]
        actual_report_sha = sha256_file(report_path)
        if actual_report_sha != gate.get("report_sha256"):
            problems.append("layout.hard_gate 报告 SHA-256 与验证记录不一致")
            return problems
        report = load_json(report_path)
        if not isinstance(report, dict):
            return ["layout.hard_gate 报告无法解析"]
        if gate.get("status") != "EXECUTED" or report.get("verdict") != "PASS":
            problems.append("layout.hard_gate 未 PASS，或未完整执行")
        if report.get("findings") or report.get("unverified"):
            problems.append("layout.hard_gate 报告仍有 FAIL/UNVERIFIED 项")
        identity = report.get("identity") or {}
        binding = report.get("workbench_binding") or {}
        if (identity.get("pptx_sha256") != validation.get("candidate_pptx_sha256")
                or binding.get("candidate_pptx_sha256")
                != validation.get("candidate_pptx_sha256")):
            problems.append("layout.hard_gate 未绑定当前候选 PPTX")
        expected_pdf = (validation.get("render") or {}).get("pdf_sha256")
        if identity.get("pdf_sha256") != expected_pdf or binding.get("pdf_sha256") != expected_pdf:
            problems.append("layout.hard_gate 未绑定当前验证 PDF")
        if identity.get("policy_sha256") != LAYOUT_GATE_POLICY_DOCUMENT_SHA256:
            problems.append("layout.hard_gate 政策身份与当前冻结政策不符")
        if binding.get("policy_bundle_sha256") != LAYOUT_GATE_POLICY_BUNDLE_SHA256:
            problems.append("layout.hard_gate 政策文件字节与当前冻结身份不符")
        if (not isinstance(report.get("pages_checked"), list)
                or not report.get("pages_checked")
                or len(report.get("pages_checked")) != binding.get("pptx_page_count")):
            problems.append("layout.hard_gate 未覆盖当前 PPTX 的全部页面")
        return problems

    def create_job(self, input_path, mode, theme=theme_mod.DEFAULT_THEME_ID, edit_scope=None,
                   support_profile=SUPPORT_PROFILE, target_environment=None,
                   material_intent=None, preserve_design=False):
        input_path = Path(input_path)
        # 建任务即核对真实可读性：损坏/伪装输入不得先建成"待处理任务"再在运行期失败。
        verify_input_package(input_path)
        ihash = sha256_file(input_path)
        # A1（D10-B）：主题身份进入任务身份。未注册主题在此明确拒绝（正式错误码），
        # 不允许拖到 run 阶段才 KeyError；参数只从可信注册表解析。
        try:
            ident = theme_mod.resolve_theme(theme)
        except theme_mod.ThemeError as e:
            raise GateError(e.code, e.detail)
        # 幂等：同一输入 + 同一模式 + 同一契约版本 + 同一 profile + **同一主题身份**
        # （theme_id + theme_version + 有效配置摘要）→ 同一 job_id。
        # 换主题 = 新身份 = 新任务目录，不会覆盖另一主题的候选/报告/导出。
        # 材料任务与普通美化任务**身份不同**：同一份 PPTX 走两条路径时不得互相复用任务目录，
        # 否则材料身份的冻结会被普通调用污染（或反之）。
        key = "|".join([ihash, mode, CONTRACT_VERSION, support_profile,
                        ident["theme_id"], ident["theme_version"], ident["config_digest"],
                        "material" if material_intent is not None else "plain",
                        # 保留原设计 ≠ 主题应用：两者必须是不同任务身份，
                        # 否则"原稿预览"会被当成"已套主题"的候选复用。
                        "preserve" if preserve_design else "theme"])
        job_id = "job-" + uuid.uuid5(uuid.NAMESPACE_URL, key).hex[:12]
        d = self._job_dir(job_id)
        if d.exists() and (d / "job.json").exists():
            job = load_json(d / "job.json")
            job["previous_attempt_id"] = job.get("attempt_id")
            job["attempt_id"] = uuid.uuid4().hex[:8]
            job["retried_at"] = now_iso()
            # 重新冻结主题身份（同一 job_id 意味着摘要相同，此处只做幂等写回）
            job["theme"] = ident["theme_id"]
            job["theme_name"] = ident["name"]
            job["theme_version"] = ident["theme_version"]
            job["theme_registry_version"] = ident["registry_version"]
            job["theme_config_digest"] = ident["config_digest"]
            if material_intent is not None:
                job["material_intent"] = material_intent
                save_json_atomic(material_intent, material_intent_path(d))
            elif load_material_intent(d) is not None:
                # 已冻结的材料身份不得被后续普通调用悄悄抹掉
                job.setdefault("material_intent", load_material_intent(d))
            # 新 attempt 清除取消标记；被取消的旧 attempt 仍留在 cancelled_attempts 里，
            # 供 submit_result 拒绝其迟到结果。
            if job.get("verdict") == "CANCELLED":
                job["verdict"] = "QUEUED"
            save_json_atomic(job, d / "job.json")
            cf = d / ".cancelled"
            if cf.exists():
                cf.unlink()
            return job_id, job
        d.mkdir(parents=True, exist_ok=True)
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ-") + uuid.uuid4().hex[:8]
        job = {
            "schema_version": "1.0.0", "job_id": job_id, "run_id": run_id,
            "attempt_id": uuid.uuid4().hex[:8], "mode": mode,
            "contract_version": CONTRACT_VERSION, "support_profile": support_profile,
            "input_path": str(input_path), "input_hash": ihash,
            "edit_scope": edit_scope or {"type": "whole_deck"},
            "preserve_design": bool(preserve_design),
            "target_environment": target_environment or {},
            "theme": ident["theme_id"], "theme_name": ident["name"],
            "theme_version": ident["theme_version"],
            "theme_registry_version": ident["registry_version"],
            "theme_config_digest": ident["config_digest"],
            "created_at": now_iso(),
            "verdict": "QUEUED", "stages": [],
            # 契约身份进任务身份：契约文件哈希随任务落盘，便于复核"规则有没有变过"。
            # 解析顺序：数据根/contracts → 产品包自身/contracts（安装后契约在代码旁边，
            # 不在数据根；只按数据根解析会让 `contracts_identity` 静默为空）。
            "contracts_identity": self.contracts_identity(),
        }
        if material_intent is not None:
            # 材料身份在建任务时就写死：此后台账缺失/被换都不能让它退回自基线路径。
            job["material_intent"] = material_intent
            save_json_atomic(material_intent, material_intent_path(d))
        save_json_atomic(job, d / "job.json")
        return job_id, job

    @contextmanager
    def _lock(self, job_id, purpose="run"):
        """任务写入权：按「数据根 + job_id」互斥，跨进程有效。

        用 `O_CREAT|O_EXCL` 建锁文件（既有机制），但补两件事：
        1. 锁里记录 pid/时间/用途，冲突时能说清"谁在写"；
        2. **同一进程内的嵌套调用视为重入**（例如 run 内部再走 validate），
           不再自己和自己冲突；跨进程仍严格互斥。
        """
        d = self._job_dir(job_id)
        lock = d / ".run.lock"
        acquired = False
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {now_iso()} {purpose}".encode())
            os.close(fd)
            acquired = True
        except FileExistsError:
            owner = None
            # 建锁与写 pid 之间有极短窗口：读到空文件时 owner 会是 None，
            # 冲突信息就少了"谁在写"。短暂重试一次读取，仍然读不到才放弃。
            for _ in range(20):
                try:
                    owner = int(lock.read_text(encoding="utf-8").split()[0])
                    break
                except Exception:  # noqa: BLE001
                    time.sleep(0.02)
            if owner != os.getpid():
                # 锁属主存活性检测：若原属主进程已死（异常退出/被kill），清除残留死锁并重新夺取
                if owner is not None and not self._alive(owner):
                    try:
                        lock.unlink()
                        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                        os.write(fd, f"{os.getpid()} {now_iso()} {purpose}".encode())
                        os.close(fd)
                        acquired = True
                    except Exception:
                        pass
                if not acquired:
                    raise GateError(
                        "GATE.ATTEMPT_CONFLICT",
                        json.dumps({"job_id": job_id, "owner_pid": owner,
                                    "purpose": purpose,
                                    "detail": "该任务已有活动的写入进程；拒绝并发写入同一候选/渲染。"},
                                   ensure_ascii=False))
        try:
            yield
        finally:
            if acquired:
                try:
                    lock.unlink()
                except OSError:
                    pass

    def _begin_attempt(self, d, job):
        """开新尝试：归档上一轮产物 + 让当前判定/导出现在失效。

        不删除任何旧文件：上一轮的 candidate/render/validation/findings 整体移到
        `history/<attempt>-<时间>/`，当前 attempt 从空目录重新构建。
        这样"历史有效版本可追溯"与"新尝试不能借用旧成功"同时成立。
        """
        prev = job.get("previous_attempt_id") or job.get("attempt_id")
        hist = d / "history"
        stamp = datetime.now(CST).strftime("%Y%m%dT%H%M%S")
        archived = None
        moved = []
        # 归档目录名里带秒级时间戳：同一秒内开两次尝试会算出同一个目录，
        # 第二次 shutil.move 撞上已存在的目标直接抛 shutil.Error，
        # 整次 run 以"未预期异常"告终（并发重试时实测可复现）。
        # 这里用"存在就换名"的方式保证归档目录唯一：历史只增不减、不覆盖。
        target_dir = hist / f"{prev}-{stamp}"
        n = 1
        while target_dir.exists():
            n += 1
            target_dir = hist / f"{prev}-{stamp}-{n}"
        for name in ("validation.json", "findings.json", "candidate"):
            src_p = d / name
            if src_p.exists():
                target_dir.mkdir(parents=True, exist_ok=True)
                dst = target_dir / name
                if dst.exists():
                    dst = target_dir / f"{name}-{n}"
                shutil.move(str(src_p), str(dst))
                moved.append(name)
                archived = str(target_dir)
        job["verdict"] = "RUNNING"
        job["stage"] = "run"
        job["current_valid"] = None
        job["download_ready"] = False
        # export_path 从"当前"移到"历史"：旧导出目录本身不删，但不再对应当前候选。
        if job.get("export_path"):
            job.setdefault("export_history", []).append(
                {"path": job["export_path"], "attempt_id": prev, "archived_at": now_iso(),
                 "note": "上一尝试的导出；新尝试未完成前不代表当前结果"})
            job["export_path"] = None
        job.setdefault("attempt_log", []).append(
            {"event": "BEGIN_ATTEMPT", "attempt_id": job.get("attempt_id"),
             "previous_attempt_id": prev, "at": now_iso(),
             "archived_to": archived, "moved": moved,
             "invalidated": ["validation", "findings", "candidate", "export_path", "preview"]})
        save_json_atomic(job, d / "job.json")
        self._stage(d.name, "begin_attempt",
                    "DONE", {"archived_to": archived, "moved": moved,
                             "previous_attempt_id": prev,
                             "note": "新尝试不继承旧 verdict/export/预览；历史版本已归档保留。"})
        return archived

    def _stage(self, job_id, name, status, detail=None):
        d = self._job_dir(job_id)
        job = load_json(d / "job.json")
        job["stages"].append({"stage": name, "status": status, "at": now_iso(),
                              "detail": detail or {}})
        job["stage"] = name
        job["last_activity"] = now_iso()
        job["heartbeat_at"] = now_iso()
        save_json_atomic(job, d / "job.json")

    # ---------------- 进程身份：取消与旧结果迟到 ----------------
    def _pid_file(self, job_id):
        return self._job_dir(job_id) / "worker.pid"

    def _render_pid_file(self, job_id):
        """渲染子进程 PID 落盘位置，供取消时连带回收渲染进程组。"""
        return self._job_dir(job_id) / "render.pid"

    # ---------------- 渲染缓存绑定（P3） ----------------
    def _render_cache_key(self, pptx):
        """缓存键 = 候选字节 + 字体配置 + 平台 + DPI + 渲染器身份。

        不含时间戳/随机目录；任何一项变化都必须让旧 PNG 失效。
        """
        env, rec = fontenv_mod.build_env(self.root)
        soffice, _src = render_mod.find_soffice()
        ver = render_mod.soffice_version(soffice) if soffice else None
        key = {
            "candidate_sha256": sha256_file(pptx),
            "policy": rec.get("policy"),
            "fontconfig_mode": rec.get("mode"),
            "fontconfig_ok": bool(rec.get("ok")),
            "fontconfig_sha256": rec.get("effective_conf_sha256"),
            "platform": rec.get("platform"),
            "dpi": 110,
            "renderer": "libreoffice",
            "renderer_version": ver,
        }
        return key, rec, ver

    def _prepare_render_dir(self, d, pptx):
        """把渲染目录绑定到当前候选/环境；不一致就整体归档后重建。"""
        key, rec, ver = self._render_cache_key(pptx)
        blob = json.dumps(key, sort_keys=True, ensure_ascii=False).encode("utf-8")
        key_sha = __import__("hashlib").sha256(blob).hexdigest()
        rd = d / "candidate" / "render"
        meta_p = rd / "render_meta.json"
        if rd.exists():
            old = load_json(meta_p)
            reason = None
            if not old:
                reason = "no_environment_binding"
            elif old.get("cache_key_sha256") != key_sha:
                reason = "cache_key_changed"
            if reason:
                hist = d / "candidate" / "render_history" / (
                    datetime.now(CST).strftime("%Y%m%dT%H%M%S") + "-" + reason)
                hist.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(rd), str(hist))
                self._stage(d.name, "render_cache_invalidated", "DONE",
                            {"reason": reason, "archived_to": str(hist),
                             "old_key": (old or {}).get("cache_key_sha256"),
                             "new_key": key_sha,
                             "note": "旧页图按事故证据保留，不再作为当前产物。"})
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "_cache_key.json").write_text(
            json.dumps({"cache_key": key, "cache_key_sha256": key_sha},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        return rd, key_sha

    def _write_render_meta(self, rd, key_sha, pptx, rr, glyph):
        meta = {
            "schema_version": "1.0.0", "cache_key_sha256": key_sha,
            "candidate_pptx": str(pptx), "candidate_sha256": sha256_file(pptx),
            "pdf": rr.get("pdf"), "pdf_sha256": rr.get("pdf_sha256"),
            "dpi": rr.get("dpi") or 110,
            "png_bindings": rr.get("png_bindings"),
            "font_environment": rr.get("font_environment"),
            "environment_fingerprint": rr.get("environment_fingerprint"),
            "pdf_fonts": rr.get("pdf_fonts"),
            "renderer_version": rr.get("soffice_version"),
            "glyph_quality": (glyph or {}).get("verdict"),
            "generated_at": now_iso(),
            "note": "页图与 PDF 的绑定；环境/候选任何一项变化都必须重建本目录。",
        }
        save_json_atomic(meta, Path(rd) / "render_meta.json")
        return meta

    def _cancel_flag(self, job_id):
        return self._job_dir(job_id) / ".cancelled"

    @staticmethod
    def _alive(pid):
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, ValueError, PermissionError):
            return False

    def _has_active_writer(self, job_id) -> bool:
        """是否仍有真实写入者在跑（锁文件里的 pid 存活，或 worker.pid 存活）。"""
        d = self._job_dir(job_id)
        for p, parse in ((d / ".run.lock", True), (self._pid_file(job_id), False)):
            if not p.exists():
                continue
            pid = None
            if parse:
                try:
                    pid = int(p.read_text(encoding="utf-8").split()[0])
                except Exception:  # noqa: BLE001
                    pid = None
            else:
                try:
                    pid = int(p.read_text(encoding="utf-8").split()[0])
                except Exception:  # noqa: BLE001
                    pid = None
            if pid and self._alive(pid):
                return True
            if parse and pid is None:
                return True   # 锁存在但内容不可解析：按"可能有人在写"处理，不抢
        return False

    #: worker 进程消失后，任务最多还能停在 RUNNING 多久（秒）才被回收。
    #: 给足一个启动窗口：worker 先写 job.json 再落 worker.pid，
    #: 这中间的空档不能被误判成"已死"。
    ABANDONED_RUN_GRACE_S = 120

    def _heartbeat_age_s(self, job) -> float | None:
        """距最后一次心跳多少秒；时间戳缺失或不可解析时返回 None。"""
        raw = job.get("heartbeat_at") or job.get("last_activity")
        if not raw:
            return None
        try:
            ts = datetime.fromisoformat(str(raw))
        except ValueError:
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds()

    def reap_abandoned_run(self, job_id, *, grace_s: float | None = None) -> dict | None:
        """worker 已不在而任务仍停在 RUNNING 时，按契约收尾为 GATE.INTERRUPTED。

        契约 SF10 的 GATE.INTERRUPTED 明确要求"进程中断不得长期停留 RUNNING"：
        worker 被 kill、机器重启或进程崩溃时没有人改写 job.json，工作台就会
        一直显示"制作中"。这里只做**如实收尾**，不重跑、不改候选、不碰导出。

        两个条件同时成立才回收，避免误杀刚启动还没写 worker.pid 的 worker：
        进程确实不在，且心跳已超过宽限期。返回回收记录，未回收返回 None。
        """
        d = self._job_dir(job_id)
        job = load_json(d / "job.json") if (d / "job.json").is_file() else None
        if not isinstance(job, dict) or job.get("verdict") != "RUNNING":
            return None
        if self._has_active_writer(job_id):
            return None
        age = self._heartbeat_age_s(job)
        limit = self.ABANDONED_RUN_GRACE_S if grace_s is None else grace_s
        if age is None or age < limit:
            return None
        detail = ("运行进程已不存在，任务停留在 RUNNING 超过 %d 秒未更新心跳；"
                  "按契约收尾为中断，不代表已完成。" % int(limit))
        self._stage(job_id, job.get("stage") or "run", "FAILED",
                    {"code": "GATE.INTERRUPTED", "detail": detail,
                     "heartbeat_age_s": round(age, 1),
                     "last_heartbeat_at": job.get("heartbeat_at")})
        self._mark_run_stage_failure(job_id, "GATE.INTERRUPTED", detail)
        return {"job_id": job_id, "reaped": True, "code": "GATE.INTERRUPTED",
                "heartbeat_age_s": round(age, 1), "at": now_iso()}

    def reap_all_abandoned_runs(self, *, grace_s: float | None = None) -> list:
        """扫描全部任务，回收所有 worker 已死却仍停在 RUNNING 的任务。"""
        root = self.root / "jobs"
        if not root.is_dir():
            return []
        out = []
        for d in sorted(root.glob("job-*")):
            try:
                rec = self.reap_abandoned_run(d.name, grace_s=grace_s)
            except Exception:  # noqa: BLE001 — 单个任务异常不能中断整轮清理
                continue
            if rec:
                out.append(rec)
        return out

    def cancel(self, job_id, grace=5.0):
        """取消：标记取消、终止真实子进程、释放锁，并记录被取消的 attempt。

        取消后旧子进程不得再覆盖新版本 —— 由 submit_result 的 attempt/run 身份校验兜底。
        **已完成且无活动写入者的任务**：取消是幂等无操作，不改 stage/verdict/候选/导出。
        """
        d = self._job_dir(job_id)
        job = load_json(d / "job.json")
        if (not self._has_active_writer(job_id)
                and job.get("verdict") in ("DEVELOPMENT_CANDIDATE", "FAILED",
                                           "NEEDS_REVIEW", "CANCELLED")):
            rec = {"job_id": job_id, "cancelled": False,
                   "reason": "NO_ACTIVE_ATTEMPT",
                   "detail": "该任务当前没有进行中的运行；取消是幂等无操作，"
                             "已完成结果、候选与固定导出均未改变。",
                   "unchanged": {"verdict": job.get("verdict"),
                                 "attempt_id": job.get("attempt_id"),
                                 "stage": job.get("stage"),
                                 "export_path": job.get("export_path")},
                   "at": now_iso()}
            # 只追加"收到但未改变状态"的事件，不调用 _stage（那会覆盖业务阶段）。
            job.setdefault("cancel_noops", []).append(rec)
            job["last_activity"] = now_iso()
            save_json_atomic(job, d / "job.json")
            return rec
        (d / ".cancelled").write_text(now_iso(), encoding="utf-8")

        pid_file = self._pid_file(job_id)
        pid = None
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text(encoding="utf-8").split()[0])
            except Exception:  # noqa: BLE001
                pid = None
        alive_before = self._alive(pid) if pid else None
        signal_used, alive_after = None, None
        if pid and alive_before:
            signal_used = "SIGTERM"
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            t0 = time.time()
            while time.time() - t0 < grace and self._alive(pid):
                time.sleep(0.1)
            if self._alive(pid):
                signal_used = "SIGTERM+SIGKILL"
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                time.sleep(0.2)
            alive_after = self._alive(pid)
        # 渲染子进程回收：渲染器（LibreOffice）会派生自己的子进程，
        # 只终止 worker 会把它们留成孤儿，取消后仍在写文件并污染下一次 attempt。
        rpid_file = self._render_pid_file(job_id)
        render_rec = {"pid_file": str(rpid_file), "pid": None,
                      "alive_before": None, "alive_after": None}
        if rpid_file.exists():
            try:
                rpid = int(rpid_file.read_text(encoding="utf-8").split()[0])
            except Exception:  # noqa: BLE001
                rpid = None
            render_rec["pid"] = rpid
            if rpid:
                render_rec["alive_before"] = self._alive(rpid)
                try:
                    os.killpg(os.getpgid(rpid), signal.SIGKILL)
                except Exception:  # noqa: BLE001
                    try:
                        os.kill(rpid, signal.SIGKILL)
                    except Exception:  # noqa: BLE001
                        pass
                # 等它真正消失：killpg 之后进程会先变成僵尸再被收养方回收，
                # 立刻查存活会把"已发信号但未回收"误记成"仍在运行"。
                deadline = time.time() + 5.0
                while time.time() < deadline and self._alive(rpid):
                    try:
                        os.waitpid(rpid, os.WNOHANG)
                    except ChildProcessError:
                        pass   # 不是本进程的子进程：等其父进程/收养方回收，不放弃等待
                    except Exception:  # noqa: BLE001
                        pass
                    time.sleep(0.1)
                render_rec["alive_after"] = self._alive(rpid)
        lock = d / ".run.lock"
        if lock.exists():
            try:
                lock.unlink()
            except OSError:
                pass
        job = load_json(d / "job.json")
        cancelled = job.setdefault("cancelled_attempts", [])
        if job["attempt_id"] not in cancelled:
            cancelled.append(job["attempt_id"])
        job["verdict"] = "CANCELLED"
        job["cancelled_at"] = now_iso()
        save_json_atomic(job, d / "job.json")
        rec = {"job_id": job_id, "run_id": job["run_id"], "cancelled_attempt": job["attempt_id"],
               "worker_pid": pid, "alive_before_cancel": alive_before,
               "signal_used": signal_used, "alive_after_cancel": alive_after,
               "render_subprocess": render_rec,
               "reap_note": ("alive_after_cancel / alive_after 表示'发信号后是否已被回收'，"
                             "不是'有没有发信号'；由调用方派生的 worker 需调用方 wait 才最终回收。"),
               "staging_retained": str(d / "staging" / job["attempt_id"])}
        # A21：stage 是流水线阶段（QUEUED / run / validate / export），"cancel" 不是阶段。
        # 取消是**生命周期事件**：进 stages 事件流并标 kind=lifecycle，
        # 但不改写 job.stage——否则"当前停在哪一步"被一个非阶段值覆盖，
        # 重跑/恢复就分不清上一真实阶段。
        job.setdefault("stages", []).append(
            {"stage": job.get("stage"), "kind": "lifecycle", "event": "cancel",
             "status": "DONE", "at": now_iso(), "detail": rec})
        job["lifecycle"] = "CANCELLED"
        job["last_activity"] = now_iso()
        save_json_atomic(job, d / "job.json")
        return rec

    def resume(self, job_id):
        """R05：任务进程被中断（崩溃/SIGKILL）后的**显式**恢复入口。

        中断会留下 .run.lock（旧进程没有机会清理）。恢复必须：
        1. 先确认锁里记录的进程已经不存在（否则拒绝，避免并发写入）；
        2. 再释放锁并开一个新的 attempt，旧 attempt 的 staged 产物保留待查。
        不做"自动抢锁"：进程还活着时抢锁会让两个实例同时写同一任务。
        """
        d = self._job_dir(job_id)
        job = load_json(d / "job.json")
        lock = d / ".run.lock"
        if not lock.exists():
            rec = {"job_id": job_id, "run_id": job["run_id"], "recovered": False,
                   "detail": "没有残留锁，无需恢复。"}
            self._stage(job_id, "resume", "SKIPPED", rec)
            return rec
        try:
            pid = int(lock.read_text(encoding="utf-8").split()[0])
        except Exception:  # noqa: BLE001
            pid = None
        if pid is not None and self._alive(pid):
            raise GateError("GATE.ATTEMPT_CONFLICT",
                            f"锁内进程 {pid} 仍然存活，不得在恢复名义下并发写入。")
        old_attempt = job["attempt_id"]
        lock.unlink()
        job["attempt_id"] = uuid.uuid4().hex[:8]
        job["recovered_at"] = now_iso()
        job["verdict"] = "QUEUED"
        job.setdefault("interrupted_attempts", []).append(old_attempt)
        job.setdefault("cancelled_attempts", [])
        if old_attempt not in job["cancelled_attempts"]:
            job["cancelled_attempts"].append(old_attempt)
        save_json_atomic(job, d / "job.json")
        rec = {"job_id": job_id, "run_id": job["run_id"], "recovered": True,
               "dead_pid": pid, "old_attempt": old_attempt,
               "new_attempt": job["attempt_id"],
               "staging_retained": str(d / "staging" / old_attempt)}
        self._stage(job_id, "resume", "DONE", rec)
        return rec

    def submit_result(self, job_id, attempt_id, run_id, artifact_dir):
        """真实的结果提交入口：任何迟到的旧结果都必须在这里被拒绝。

        身份三要素：attempt_id 必须等于当前 attempt、run_id 必须等于当前 run、
        该 attempt 未被取消。任一条不满足即拒绝，且**不触碰**当前候选/报告/有效版本。
        """
        d = self._job_dir(job_id)
        job = load_json(d / "job.json")
        problems = []
        if attempt_id != job["attempt_id"]:
            problems.append({"code": "GATE.STALE_ATTEMPT", "expected": job["attempt_id"],
                             "actual": attempt_id,
                             "detail": "提交的是旧 attempt 的结果；当前已有更新的 attempt。"})
        if run_id != job["run_id"]:
            problems.append({"code": "GATE.STALE_RUN", "expected": job["run_id"],
                             "actual": run_id, "detail": "提交结果不属于当前 run。"})
        if job.get("verdict") == "CANCELLED" or attempt_id in job.get("cancelled_attempts", []):
            problems.append({"code": "GATE.LATE_RESULT_REJECTED", "attempt_id": attempt_id,
                             "detail": "该 attempt 已被取消；取消后的结果不得写入正式产物。"})
        if problems:
            # 拒绝记录写独立的 append-only 文件，**不改动** job.json / 报告 / 候选 / 有效版本。
            # 否则"拒绝迟到结果"本身就会污染当前状态，使状态一致性无法验证。
            rec = {"at": now_iso(), "job_id": job_id, "attempt_id": attempt_id,
                   "run_id": run_id, "artifact_dir": str(artifact_dir),
                   "current_attempt_id": job["attempt_id"], "problems": problems}
            with open(d / "submit_rejections.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            raise GateError("GATE.LATE_RESULT_REJECTED", json.dumps(problems, ensure_ascii=False))
        # 通过身份校验后才落地。**先确认提交目录里确实有本次候选**：
        # 一个不含候选 PPTX 的目录（例如误传 staging 父目录）如果被接受，
        # 会把当前候选换成空壳，随后验收/导出全部失真。
        expected_name = Path(job["input_path"]).stem + ".themed.pptx"
        artifact_dir = Path(artifact_dir)
        if not (artifact_dir / expected_name).is_file():
            raise GateError("GATE.ARTIFACT_MISSING",
                            f"提交目录里没有本次候选 {expected_name}；"
                            f"目录 {artifact_dir} 不是本 attempt 的产物目录，"
                            "当前候选未被触碰。")
        # R07：候选写入必须"先写临时目录、再原子改名"。旧实现是 rmtree(candidate) 后
        # copytree，复制中途失败会把上一版候选直接抹掉，违背"失败不覆盖有效版本"。
        dest = d / "candidate"
        tmp = d / ("candidate.tmp-" + uuid.uuid4().hex[:8])
        if tmp.exists():
            shutil.rmtree(tmp)
        try:
            shutil.copytree(artifact_dir, tmp)
        except Exception as e:  # noqa: BLE001
            shutil.rmtree(tmp, ignore_errors=True)
            raise GateError("GATE.ARTIFACT_WRITE_FAILED",
                            f"候选写入中途失败（{type(e).__name__}: {e}）；"
                            "已清除半成品，上一版候选与有效版本未被触碰。")
        backup = None
        try:
            if dest.exists():
                backup = d / ("candidate.bak-" + uuid.uuid4().hex[:8])
                os.replace(dest, backup)   # 原子改名：瞬时，不存在半截状态
            os.replace(tmp, dest)          # 原子改名：要么新要么旧
        except Exception as e:  # noqa: BLE001
            if backup is not None and backup.exists() and not dest.exists():
                os.replace(backup, dest)   # 回滚到上一版
            shutil.rmtree(tmp, ignore_errors=True)
            raise GateError("GATE.ARTIFACT_WRITE_FAILED",
                            f"候选改名失败（{type(e).__name__}: {e}）；已回滚到上一版候选。")
        finally:
            if backup is not None and backup.exists():
                shutil.rmtree(backup, ignore_errors=True)
        self._stage(job_id, "submit_accepted", "DONE",
                    {"attempt_id": attempt_id, "artifact_dir": str(artifact_dir),
                     "atomic_swap": True})
        return {"accepted": True, "job_id": job_id, "attempt_id": attempt_id,
                "candidate": str(dest)}

    def publish_valid(self, job_id):
        """把通过验收的候选固化为本任务的「当前有效版本」。

        只有这里会写 valid/ 与 current_version.json；提交被拒绝时不得触碰。
        """
        d = self._job_dir(job_id)
        job = load_json(d / "job.json")
        v = load_json(d / "validation.json")
        gate_problems = self._layout_gate_evidence_problems(d, v or {})
        if gate_problems:
            raise GateError("GATE.RELEASE_BLOCKED", "；".join(gate_problems))
        if v["verdict"] == "FAILED":
            raise GateError("GATE.BLOCKING_FINDINGS", "验收未通过，不能固化为有效版本。")
        if v["verdict"] not in ("DEVELOPMENT_CANDIDATE", "READY"):
            raise GateError("GATE.RELEASE_BLOCKED",
                            f"当前判定为 {v['verdict']}（存在待裁定项），不能固化为有效版本。")
        valid = d / "valid"
        if valid.exists():
            shutil.rmtree(valid)
        shutil.copytree(d / "candidate", valid)
        cur = {"schema_version": "1.0.0", "job_id": job_id, "run_id": job["run_id"],
               "attempt_id": job["attempt_id"], "published_at": now_iso(),
               "candidate_pptx_sha256": v["candidate_pptx_sha256"],
               "validation_sha256": sha256_file(d / "validation.json"),
               "label": INTERNAL_CANDIDATE_LABEL}
        save_json_atomic(cur, d / "current_version.json")
        self._set_verdict(job_id, v["verdict"], {"valid_version": str(valid)})
        return cur

    def _set_verdict(self, job_id, verdict, extra=None):
        d = self._job_dir(job_id)
        job = load_json(d / "job.json")
        job["verdict"] = verdict
        job["verdict_at"] = now_iso()
        if extra:
            job.update(extra)
        save_json_atomic(job, d / "job.json")

    def _mark_run_stage_failure(self, job_id, code, detail, *, model_error=None):
        """Persist a terminal run failure and remove its stale worker marker.

        A rejected semantic-scene plan is a real failed run, not a running job.
        Keep the last stage record as the user-visible location, while making the
        verdict terminal so the Workbench cannot spin forever or offer a result.
        """
        d = self._job_dir(job_id)
        job = load_json(d / "job.json")
        if isinstance(job, dict):
            verdict = "CANCELLED" if code == "GATE.CANCELLED" else "FAILED"
            job.update({"verdict": verdict, "verdict_at": now_iso(),
                        "fail_reason": str(code)[:120],
                        "failure_detail": str(detail)[:800],
                        "run_finished_at": now_iso()})
            if model_error:
                # SceneModelError has already redacted credentials and bounds detail.
                job["model_error"] = model_error
            save_json_atomic(job, d / "job.json")
        try:
            self._pid_file(job_id).unlink()
        except OSError:
            pass

    # ---------------- 阶段 ----------------
    def precheck(self, job_id):
        d = self._job_dir(job_id)
        job = load_json(d / "job.json")
        src = self.vendor_dir()
        # 上游校验是**记录项**：副本里没有 vendor 仓库或没有 git 时必须如实登记，
        # 不能因为目录不存在就抛 FileNotFoundError —— 那会让干净副本无法启动预检。
        if not src.is_dir():
            ok_commit = "VENDOR_DIR_MISSING"
        else:
            try:
                ok_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(src),
                                           capture_output=True, text=True).stdout.strip() \
                    == "b6dcb0f325b3ea38a7ca3cd3c211bbba7fb436b2"
            except Exception:  # noqa: BLE001
                ok_commit = "GIT_UNAVAILABLE"
        info = {
            "input_kind": Path(job["input_path"]).suffix,
            "input_sha256": job["input_hash"],
            "upstream_verified": ok_commit,
            "real_renderer": "available" if render_mod.real_render_available() else "MISSING",
            "support_profile": job["support_profile"],
            "required_checks": P1_REQUIRED_CHECKS,
        }
        self._stage(job_id, "precheck", "DONE", info)
        return info

    def run(self, job_id, timeout=3600, hold_after_build=0,
           allow_text_card_fallback=False):
        """执行候选生成（不写正式版本）。

        产物先落到 staging/<attempt_id>/，再经 submit_result 这个真实入口发布为 candidate。
        这样"迟到的旧结果"只能通过同一个入口进入，便于在入口处按 attempt/run 身份拒绝。
        hold_after_build>0 时，在生成完毕、发布之前停留指定秒数（用于取消类故障测试）。
        """
        with self._lock(job_id):
            d = self._job_dir(job_id)
            job = load_json(d / "job.json")
            attempt_id = job["attempt_id"]
            run_id = job["run_id"]
            # 进程身份：取消时据此终止真实子进程
            (d / "worker.pid").write_text(f"{os.getpid()} {now_iso()}", encoding="utf-8")
            # P5：新的尝试不得继承旧 verdict / export / 预览。
            # 先把上一轮产物整体归档（保留字节，可追溯），再把当前指针清空。
            self._begin_attempt(d, job)
            # 候选目录只在 submit_result 通过身份校验后才建立；
            # 被取消/过期的 attempt 不得留下任何 candidate 痕迹。
            stage = d / "staging" / attempt_id
            if stage.exists():
                shutil.rmtree(stage)
            stage.mkdir(parents=True)
            t0 = time.time()

            # 1) 原始材料基线（在任何 normalize/清洗/转换之前）
            src = Path(job["input_path"])
            # A01：美化任务的来源基线缺失必须明确失败，模式不得改变，
            # 也不得从当前候选/计划重建一份替代原稿。
            mode_before = job.get("mode")
            if not src.exists() or not src.is_file():
                self._stage(job_id, "baseline", "FAILED",
                            {"input_path": str(src), "mode": mode_before,
                             "code": "INPUT.SOURCE_BASELINE_MISSING"})
                self._set_verdict(job_id, "FAILED",
                                  {"fail_reason": "INPUT.SOURCE_BASELINE_MISSING",
                                   "mode_unchanged": mode_before})
                raise GateError("INPUT.SOURCE_BASELINE_MISSING",
                                f"来源基线 {src} 不存在或不可读；"
                                f"任务模式仍为 {mode_before}，不从候选或计划重建原稿。")
            raw = d / "input_raw"
            if not raw.exists():
                raw.mkdir(parents=True)
            raw_copy = raw / src.name
            shutil.copyfile(src, raw_copy)
            baseline = {
                "schema_version": "1.0.0", "input_path": str(src),
                "input_sha256": sha256_file(src), "input_sha256_copy": sha256_file(raw_copy),
                "captured_at": now_iso(), "mode": mode_before,
                "note": "在任何上游 normalize、清洗或转换之前保存；原始用户文件未被修改。",
            }
            save_json_atomic(baseline, d / "baseline.json")
            self._stage(job_id, "baseline", "DONE", baseline)

            # 1b) 原始普查（A02）：包成员台账 / 名称歧义 / 严格 UTF-8，先于任何转换
            from . import census as census_mod
            try:
                cen = census_mod.census_package(raw_copy)
            except Exception as e:  # noqa: BLE001
                cen = {"schema_version": "1.0.0", "input": str(raw_copy),
                       "warnings": [{"what": "census_failed",
                                     "detail": f"{type(e).__name__}: {e}"}]}
            # 1c) 内容级普查（T1）：a:fld 字段、a:br 软换行、OMML 公式（成员内部）
            try:
                cc = census_mod.census_xml_content(raw_copy)
            except Exception as e:  # noqa: BLE001
                cc = {"schema_version": "1.0.0", "input": str(raw_copy),
                      "note": f"内容普查失败：{type(e).__name__}: {e}"}
            cen["content_census"] = cc
            save_json_atomic(cen, d / "raw_census.json")
            census_findings = census_mod.census_findings(cen) + census_mod.content_findings(cc)
            self._stage(job_id, "raw_census", "DONE",
                        {"members": cen.get("member_count"),
                         "conflicts": len(cen.get("conflicts", [])),
                         "decode_errors": len(cen.get("decode_errors", [])),
                         "kind_counts": cen.get("kind_counts", {}),
                         "fields": cc.get("counts", {}).get("fields", 0),
                         "soft_breaks": cc.get("counts", {}).get("soft_breaks", 0),
                         "omml": cc.get("counts", {}).get("omml", 0)})
            if census_findings:
                save_json_atomic(
                    {"schema_version": "1.0.0", "job_id": job_id, "run_id": run_id,
                     "attempt_id": attempt_id, "generated_at": now_iso(),
                     "stage": "raw_census", "findings": census_findings},
                    d / "findings.json")
                blocking = [f for f in census_findings if f["severity"] == "BLOCKING"]
                verdict = "FAILED" if blocking else "NEEDS_REVIEW"
                self._stage(job_id, "raw_census", verdict,
                            {"findings": len(census_findings),
                             "codes": [f["code"] for f in census_findings]})
                self._set_verdict(job_id, verdict,
                                  {"review_reason": "raw_census",
                                   "mode_unchanged": mode_before})
                return {"candidate": None,
                        "needs_review": not blocking,
                        "findings": census_findings,
                        "detail": "原始普查发现包边界问题（成员歧义/非法编码）；"
                                  "先于转换登记，未自动择一，也未自动转码。"}

            # 2) 独立提取（标准答案来自输入，不来自待验输出）
            try:
                doc = extract_any(raw_copy)
            except Exception as e:  # noqa: BLE001
                msg = f"{type(e).__name__}: {e}"
                code = ("INPUT.INVALID_ENCODING"
                        if ("UnicodeDecodeError" in msg or "decode" in msg.lower())
                        else "OBJECT.PARSE_ERROR")
                self._stage(job_id, "extract", "FAILED", {"code": code, "detail": msg})
                self._set_verdict(job_id, "FAILED",
                                  {"fail_reason": code, "mode_unchanged": mode_before})
                raise GateError(code, f"原始输入无法解析（{msg}）；"
                                      "不得因解析失败而跳过基线，也不得静默丢弃内容。")
            save_json_atomic(doc.to_dict(), d / "extracted.json")
            self._stage(job_id, "extract", "DONE",
                        {"pages": len(doc.pages), "objects": len(doc.objects),
                         "units": sum(len(p["units"]) for p in doc.pages),
                         "charts": len(getattr(doc, "charts", [])),
                         "tables": len(getattr(doc, "tables", []))})

            # 2b) C11：图表内嵌工作簿与显示缓存冲突必须在**美化之前**发现。
            # 冲突时保留双方原始值，记 NEEDS_REVIEW，不产生候选、不选择权威来源。
            if "chart.source_consistency" in required_checks_for(job["support_profile"]):
                try:
                    conflict_findings = checks_mod.check_chart_output_sources(doc)
                except Exception as e:  # noqa: BLE001
                    conflict_findings = [{
                        "code": "GATE.CHECKER_CRASH", "severity": "BLOCKING",
                        "origin": "chart_source", "slide": None,
                        "detail": f"图表来源核对异常：{type(e).__name__}: {e}"}]
                if conflict_findings:
                    save_json_atomic(
                        {"schema_version": "1.0.0", "job_id": job_id, "run_id": job["run_id"],
                         "attempt_id": attempt_id, "generated_at": now_iso(),
                         "stage": "pre_beautify", "findings": conflict_findings},
                        d / "findings.json")
                    self._stage(job_id, "chart_source_conflict", "NEEDS_REVIEW",
                                {"findings": len(conflict_findings),
                                 "codes": [f["code"] for f in conflict_findings]})
                    self._set_verdict(job_id, "NEEDS_REVIEW",
                                      {"review_reason": "INPUT.CHART_SOURCE_CONFLICT"})
                    return {"candidate": None, "needs_review": True,
                            "findings": conflict_findings,
                            "detail": "原始图表工作簿与显示缓存冲突；已保留双方数据，"
                                      "未指定权威来源前不美化。"}

            # 3) 主题应用（真实美化，产生新候选）—— 先落到 staging
            # A1：任务建档时冻结的主题身份必须与当前注册表一致；配置已升级则拒绝运行，
            # 不用旧任务背书新配置（旧报告/旧候选不得给新规则当证据）。
            try:
                ident = theme_mod.resolve_theme(job.get("theme"))
            except theme_mod.ThemeError as e:
                raise GateError(e.code, e.detail)
            frozen_digest = job.get("theme_config_digest")
            if frozen_digest and frozen_digest != ident["config_digest"]:
                raise GateError(
                    "THEME.CONFIG_STALE",
                    f"主题 {ident['theme_id']} 的有效配置已变化（任务冻结 "
                    f"{frozen_digest[:16]}… ≠ 当前 {ident['config_digest'][:16]}…）；"
                    "请按当前配置重新建立任务，不得沿用旧任务或旧报告。")
            themed = stage / (src.stem + ".themed.pptx")
            # 材料生成任务：最终主题路径启用可读性下限（监督 2026-10-20：
            # 生成端 22pt 正文被 body=15 的默认主题缩成 15pt，全稿 47 段实证）。
            # 原稿导入/保留路径不传下限，保持 min(主题, 来源) 只缩不放。
            m_intent = job.get("material_intent") or load_material_intent(d)
            theme_stats = theme_mod.apply_theme(
                raw_copy, themed, job.get("theme"), frozen=ident,
                preserve=bool(job.get("preserve_design")),
                size_floor=(theme_mod.MATERIAL_SIZE_FLOOR if m_intent else None))
            theme_stats["theme_identity_frozen_at_job"] = bool(frozen_digest)
            theme_stats["theme_identity_legacy_no_digest"] = not bool(frozen_digest)
            save_json_atomic(theme_stats, stage / "theme_applied.json")
            self._stage(job_id, "build", "DONE", {"staged": str(themed), **theme_stats})

            # 3b) 视觉升级阶段（主题声明 visual_upgrade）：关键词配图 + 统一转场 + 粒子。
            # 计划按页面语义在 run 时生成并随候选冻结（visual_plan.json）；
            # validate 的媒体绑定检查按该计划放行计划内加图，计划外新增仍阻断。
            vplan = None
            if ident.get("params", {}).get("visual_upgrade"):
                from . import visual as visual_mod
                from . import visual_fixes as vfix_mod
                from . import visual_plan as vplan_mod
                try:
                    vplan = vplan_mod.build_plan(str(themed))
                    pre_visual = stage / (src.stem + ".themed.previsual.pptx")
                    shutil.copyfile(themed, pre_visual)
                    visual_mod.visual_deck(
                        str(themed), str(themed),
                        images={int(k): v for k, v in vplan["images"].items()},
                        particle_pages={int(k): v for k, v in vplan["particles"].items()},
                        fixes=dict(vfix_mod.PAGE_FIXES))
                    save_json_atomic(vplan, stage / "visual_plan.json")
                    self._stage(job_id, "visual_upgrade", "DONE",
                                {"image_pages": sorted(int(k) for k in vplan["images"]),
                                 "particle_pages": sorted(int(k) for k in vplan["particles"]),
                                 "layout_fixes": sorted(vfix_mod.PAGE_FIXES),
                                 "plan_sha256": vplan.get("plan_sha256", "")})
                except GateError:
                    raise
                except Exception as e:  # noqa: BLE001
                    self._stage(job_id, "visual_upgrade", "FAILED",
                                {"detail": f"{type(e).__name__}: {e}"})
                    raise GateError("VISUAL.UPGRADE_FAILED",
                                    f"视觉升级阶段失败：{type(e).__name__}: {e}")

            # HG13：语义场景照片是所有主题/所有 PPTX 路径的独立阶段，
            # 不依赖 visual_upgrade 开关。按原始输入的具体子场景挑全稿最佳页，
            # 插入一张大面积真实场景图；相同子场景/图片不会被逐页重复铺图。
            from . import semantic_scenes as scene_mod
            self._stage(job_id, "semantic_scenes", "RUNNING",
                        {"rules_version": scene_mod.VERSION,
                         "note": "按全稿语义规划具体场景，检索授权图片并安全放置。"})
            try:
                scene_plan = scene_mod.build_plan(
                    job["input_path"], themed, cache_dir=stage / "scene_assets",
                    visual_plan=vplan,
                    allow_text_card_fallback=allow_text_card_fallback)
                save_json_atomic(scene_plan, stage / "scene_visual_plan.json")
                # 没有配图分两种：一种是**找不到/不合规**（真失败），一种是
                # 这份稿子**没地方放**（版位容量）。后者已如实登记在
                # unplaced_scenes 里，没有任何东西被伪造或替代，稿子照常交付。
                unplaced_only = bool(scene_plan.get("unplaced_scenes")) and not (
                    scene_plan.get("images") or scene_plan.get("text_cards")
                    or scene_plan.get("unresolved_scenes"))
                if scene_plan.get("unresolved_scenes") or (
                        not scene_plan.get("images") and not scene_plan.get("text_cards")
                        and not unplaced_only):
                    blockers = scene_plan.get("unresolved_scenes") or [{
                        "reason": "SCENE.NO_SCENE_IMAGES",
                        "detail": "整套演示文稿没有可嵌入的关联场景照片。"}]
                    self._stage(job_id, "semantic_scenes", "FAILED",
                                {"rules_version": scene_plan.get("rules_version"),
                                 "source_page_count": scene_plan.get("source_page_count"),
                                 "detected_source_languages": (scene_plan.get("language_support") or {}).get("detected_source_languages", []),
                                 "detected_scene_ids": scene_plan.get("detected_scene_ids"),
                                 "coverage_audit": {
                                     "status": (scene_plan.get("coverage_audit") or {}).get("status", "UNVERIFIED"),
                                     "pages_checked": (scene_plan.get("coverage_audit") or {}).get("pages_checked", 0),
                                     "missing_scene_count": len((scene_plan.get("coverage_audit") or {}).get("missing_scenes") or []),
                                     "merged_distinct_scene_count": len((scene_plan.get("coverage_audit") or {}).get("merged_distinct_scenes") or []),
                                 },
                                 "planned_scene_count": len(scene_plan.get("scenes") or []),
                                 "planned_photo_count": len(scene_plan.get("images") or {}),
                                 "text_card_count": len(scene_plan.get("text_cards") or {}),
                                 "unresolved_scenes": blockers,
                                 "plan_sha256": scene_plan.get("plan_sha256")})
                    detail = ("场景照片或安全版位未完整解决。"
                             if allow_text_card_fallback else
                             "场景照片或安全版位未完整解决；未开启说明卡片兜底，"
                             "整单阻断（可在生成时勾选「找不到配图时用说明卡片兜底」后重试）。")
                    raise GateError("VISUAL.SCENE_UNRESOLVED", detail)
                pre_scene = stage / (src.stem + ".themed.prescenes.pptx")
                scene_output = stage / (src.stem + ".themed.scenes.pptx")
                shutil.copyfile(themed, pre_scene)
                scene_actions = scene_mod.apply_plan(themed, scene_plan, scene_output)
                os.replace(scene_output, themed)
                if vplan is not None:
                    scene_mod.reconcile_visual_plan(vplan, scene_plan)
                    save_json_atomic(vplan, stage / "visual_plan.json")
                self._stage(job_id, "semantic_scenes", "DONE",
                            {"rules_version": scene_plan.get("rules_version"),
                             "source_page_count": scene_plan.get("source_page_count"),
                             "detected_source_languages": (scene_plan.get("language_support") or {}).get("detected_source_languages", []),
                             "detected_scene_ids": scene_plan.get("detected_scene_ids"),
                             "coverage_audit": {
                                 "status": (scene_plan.get("coverage_audit") or {}).get("status", "UNVERIFIED"),
                                 "pages_checked": (scene_plan.get("coverage_audit") or {}).get("pages_checked", 0),
                                 "missing_scene_count": len((scene_plan.get("coverage_audit") or {}).get("missing_scenes") or []),
                                 "merged_distinct_scene_count": len((scene_plan.get("coverage_audit") or {}).get("merged_distinct_scenes") or []),
                                 "response_sha256": (scene_plan.get("coverage_audit") or {}).get("response_sha256"),
                             },
                             "planned_scene_count": len(scene_plan.get("scenes") or []),
                             "image_pages": sorted({int(v["page"]) for v in
                                                     scene_plan.get("images", {}).values()}),
                             "inserted": scene_actions.get("inserted", 0),
                             "unresolved_scenes": [],
                             # 版位不足未放置的场景如实上报，不静默吞掉。
                             "unplaced_scenes": scene_plan.get("unplaced_scenes") or [],
                             # 说明卡片兜底如实披露：哪些场景、什么原因没能配上真图。
                             "text_card_count": len(scene_plan.get("text_cards") or {}),
                             "text_card_scenes": [
                                 {"scene_key": k, "page": v["page"],
                                  "fallback_reason": v.get("fallback_reason")}
                                 for k, v in (scene_plan.get("text_cards") or {}).items()],
                             "plan_sha256": scene_plan.get("plan_sha256"),
                             # 开跑前预检的端点健康摘要：内存压力等环境问题不阻断，
                             # 但要留下证据，事后能判断失败是不是环境造成的。
                             "model_health": scene_plan.get("model_health"),
                             "output_sha256": sha256_file(themed)})
            except scene_mod.SceneModelError as e:
                model_error = e.as_record()
                self._stage(job_id, "semantic_scenes", "FAILED",
                            {"code": e.code, "detail": e.detail,
                             "model_error": model_error})
                self._mark_run_stage_failure(job_id, e.code, e.detail,
                                              model_error=model_error)
                raise GateError(e.code, e.detail) from None
            except scene_mod.SemanticSceneError as e:
                self._stage(job_id, "semantic_scenes", "FAILED",
                            {"code": e.code, "detail": e.detail})
                self._mark_run_stage_failure(job_id, e.code, e.detail)
                raise GateError(e.code, e.detail) from None
            except GateError as e:
                self._mark_run_stage_failure(job_id, e.code, e.detail)
                raise
            except Exception as e:  # noqa: BLE001
                classified = scene_mod.classify_external_model_failure(e)
                if classified is not None:
                    model_error = classified.as_record()
                    self._stage(job_id, "semantic_scenes", "FAILED",
                                {"code": classified.code, "detail": classified.detail,
                                 "model_error": model_error})
                    self._mark_run_stage_failure(job_id, classified.code,
                                                 classified.detail,
                                                 model_error=model_error)
                    raise GateError(classified.code, classified.detail) from None
                detail = f"{type(e).__name__}: {e}"
                self._stage(job_id, "semantic_scenes", "FAILED",
                            {"code": "VISUAL.SCENE_PLAN_FAILED", "detail": detail})
                self._mark_run_stage_failure(job_id, "VISUAL.SCENE_PLAN_FAILED", detail)
                raise GateError("VISUAL.SCENE_PLAN_FAILED",
                                f"语义场景插图阶段失败：{detail}") from None

            # 4) 独立重读候选
            out_doc = extract_any(themed)
            save_json_atomic(out_doc.to_dict(), stage / "extracted_output.json")
            self._stage(job_id, "independent_reread", "DONE",
                        {"pages": len(out_doc.pages),
                         "units": sum(len(p["units"]) for p in out_doc.pages)})

            # 5) 停留窗口（取消类故障测试用）：生成完毕、发布之前
            if hold_after_build:
                waited = 0.0
                while waited < hold_after_build:
                    if self._cancel_flag(job_id).exists():
                        raise GateError("GATE.CANCELLED",
                                        f"attempt {attempt_id} 在发布前被取消；staging 产物保留但未发布。")
                    time.sleep(0.2)
                    waited += 0.2

            elapsed = time.time() - t0
            if elapsed > timeout:
                raise GateError("GATE.TIMEOUT", f"run 阶段耗时 {elapsed:.1f}s 超过预算 {timeout}s")

            # 6) 经真实提交入口发布（身份不符会被拒绝）
            res = self.submit_result(job_id, attempt_id, run_id, stage)
            try:
                (d / "worker.pid").unlink()
            except OSError:
                pass
            self._stage(job_id, "run", "DONE", {"elapsed_seconds": round(elapsed, 3)})
            final = d / "candidate" / (src.stem + ".themed.pptx")
            return {"candidate": str(final), "staging": str(stage),
                    "attempt_id": attempt_id, "run_id": run_id,
                    "submitted": res, "elapsed_seconds": round(elapsed, 3),
                    "input_doc": str(d / "extracted.json"),
                    "output_doc": str(d / "candidate" / "extracted_output.json")}

    def validate(self, job_id, approved_normalizations=None):
        """对当前候选执行 P1 到期检查，写 validation.json / findings.json。

        验收会**重渲染并重写** candidate/render 与 validation.json，属于真实写入者，
        必须与 run/repair 共用同一把任务写锁；否则两个进程会同时改同一候选。
        """
        with self._lock(job_id, "validate"):
            return self._validate_locked(job_id, approved_normalizations)

    def _validate_locked(self, job_id, approved_normalizations=None):
        d = self._job_dir(job_id)
        job = load_json(d / "job.json")
        cand = d / "candidate"
        pptx = cand / (Path(job["input_path"]).stem + ".themed.pptx")
        if not pptx.exists():
            raise GateError("GATE.ARTIFACT_MISSING", "候选 PPTX 不存在；先运行 run。")
        from . import census as census_mod

        # T2：放行依据的可信来源 = 原始输入本身。预期集合在验收时从原始输入
        # 重新推导（包级 + 内容级普查 + 哈希核对）；派生台账（raw_census.json /
        # extracted.json）是运行产物，提交方改写它们不能降低放行标准。
        src = Path(job["input_path"])
        census = {}
        if src.exists():
            try:
                actual_hash = sha256_file(src)
                if actual_hash != job["input_hash"]:
                    raise GateError(
                        "EVIDENCE.STALE",
                        f"输入文件哈希与冻结任务不符：当前 {actual_hash[:16]}…，"
                        f"冻结时 {job['input_hash'][:16]}…；不得用替换后的输入验收。")
                from . import census as census_mod
                census = census_mod.census_package(src)
                census["content_census"] = census_mod.census_xml_content(src)
            except GateError:
                raise
            except Exception as e:  # noqa: BLE001
                census = {"warnings": [{"what": "census_failed",
                                        "detail": f"{type(e).__name__}: {e}"}]}
        try:
            in_doc = extract_any(src)
        except Exception as e:  # noqa: BLE001
            # A01：验收时来源基线必须还在；不在就明确失败，不从候选反推标准答案
            save_json_atomic({"schema_version": "1.0.0", "job_id": job_id,
                              "run_id": job["run_id"], "attempt_id": job["attempt_id"],
                              "generated_at": now_iso(), "stage": "validate_source_missing",
                              "findings": [{"code": "INPUT.SOURCE_BASELINE_MISSING",
                                            "severity": "BLOCKING", "origin": "runner",
                                            "slide": None,
                                            "detail": f"验收时来源基线不可用：{type(e).__name__}: {e}"}]},
                             d / "findings.json")
            self._set_verdict(job_id, "FAILED",
                              {"fail_reason": "INPUT.SOURCE_BASELINE_MISSING"})
            raise GateError("INPUT.SOURCE_BASELINE_MISSING",
                            f"验收需要原始来源基线；{type(e).__name__}: {e}")
        findings = []
        # W01：预期必需集合由 profile + 原始普查 + 提取台账共同推导，不从报告反推
        exp = expected_required_checks(job["support_profile"], census, in_doc)
        # RS-9：新管线任务带有 JobOptions（evidence/pipeline/options.json）时，
        # 条件必检项（配图账本/转场/研究证据/版式复核）并入必需集合——只加不减。
        rs_options_path = d / "evidence" / "pipeline" / "options.json"
        rs_options = load_json(rs_options_path) if rs_options_path.is_file() else None
        if rs_options:
            exp = expected_required_checks(job["support_profile"], census, in_doc,
                                           rs_options=rs_options)
        ledger, mplan, mstatus, mreason = load_material_source(
            d, job.get("material_intent") or load_material_intent(d))
        exp["material_source"] = ledger
        exp["material_plan"] = mplan
        exp["material_status"] = mstatus
        exp["material_reason"] = mreason
        req = exp["required"]
        # 材料任务：**无论台账是否还在**，来源覆盖都是必需检查。
        # 台账缺失/损坏/未冻结/身份不符 → NOT_EVALUABLE（≠EXECUTED）→ 出口拒绝，
        # 绝不退回"普通 PPTX 自基线路径"。
        if mstatus != "NOT_MATERIAL":
            req = list(dict.fromkeys(list(req) + ["source.fact_coverage"]))
            exp["required"] = req
            if mstatus != "OK":
                findings.append({
                    "code": "GATE.MATERIAL_SOURCE_UNAVAILABLE", "severity": "BLOCKING",
                    "origin": "source_coverage", "slide": None,
                    "detail": f"材料任务的来源核对不可执行（{mstatus}）：{mreason}。"
                              "不得以普通 PPTX 自基线路径放行。"})
        try:
            out_doc = extract_any(pptx)
            out_doc_for_checks = out_doc
        except Exception as e:  # noqa: BLE001
            # 候选读不出来必须记为阻断，绝不能因异常跳过检查而放行
            out_doc_for_checks = {"pages": [], "objects": [], "warnings": []}
            findings.append({"code": "GATE.CANDIDATE_PARSE_ERROR", "severity": "BLOCKING",
                             "origin": "runner", "slide": None,
                             "detail": f"候选 PPTX 无法解析：{type(e).__name__}: {e}"})
        out_doc = out_doc_for_checks
        out_pages = out_doc.pages if hasattr(out_doc, "pages") else out_doc.get("pages", [])
        # T1：动态字段显示值允许按冻结规则变化 → 逐字比较前先遮罩。
        # 遮罩用的显示值取自**原始输入与候选各自**的原始普查，不从派生台账反推。
        src_dynamic = checks_mod.dynamic_displays_by_slide(
            (census or {}).get("content_census", {}) or {})
        try:
            out_dynamic = checks_mod.dynamic_displays_by_slide(
                census_mod.census_xml_content(pptx))
        except Exception:  # noqa: BLE001
            out_dynamic = {}
        try:
            findings += checks_mod.check_content_fidelity(
                in_doc, out_doc, approved_normalizations,
                src_dynamic=src_dynamic, out_dynamic=out_dynamic)
        except Exception as e:  # noqa: BLE001
            findings.append({"code": "GATE.CHECKER_CRASH", "severity": "BLOCKING",
                             "origin": "content_fidelity", "slide": None,
                             "detail": f"内容保真检查异常：{type(e).__name__}: {e}"})
        try:
            findings += checks_mod.check_native_editability(pptx, out_doc)
        except Exception as e:  # noqa: BLE001
            findings.append({"code": "GATE.CHECKER_CRASH", "severity": "BLOCKING",
                             "origin": "native_editability", "slide": None,
                             "detail": f"原生可编辑性检查异常：{type(e).__name__}: {e}"})

        # P2 对象子范围：备注页属、隐藏页状态、组结构、图片绑定
        notes_status = hidden_status = group_status = media_status = "NOT_APPLICABLE"
        if "content_fidelity.notes" in req:
            try:
                findings += checks_mod.check_notes_relations(in_doc, out_doc)
                notes_status = "EXECUTED"
            except Exception as e:  # noqa: BLE001
                notes_status = "CRASHED"
                findings.append({"code": "GATE.CHECKER_CRASH", "severity": "BLOCKING",
                                 "origin": "notes", "slide": None,
                                 "detail": f"备注关系检查异常：{type(e).__name__}: {e}"})
        if "content_fidelity.hidden_state" in req:
            try:
                findings += checks_mod.check_hidden_state(in_doc, out_doc)
                hidden_status = "EXECUTED"
            except Exception as e:  # noqa: BLE001
                hidden_status = "CRASHED"
                findings.append({"code": "GATE.CHECKER_CRASH", "severity": "BLOCKING",
                                 "origin": "hidden_state", "slide": None,
                                 "detail": f"隐藏状态检查异常：{type(e).__name__}: {e}"})
        if "object.group_structure" in req:
            try:
                findings += checks_mod.check_group_structure(
                    Path(job["input_path"]), pptx)
                group_status = "EXECUTED"
            except Exception as e:  # noqa: BLE001
                group_status = "CRASHED"
                findings.append({"code": "GATE.CHECKER_CRASH", "severity": "BLOCKING",
                                 "origin": "group_structure", "slide": None,
                                 "detail": f"组结构检查异常：{type(e).__name__}: {e}"})
        if "object.media_binding" in req:
            # 旧视觉计划与 HG13 语义场景计划合并为一个逐页图片身份账本。
            # 计划损坏时阻断，不能退回仅按图片数量放行。
            vplan_pages, vplan_img_sets, vplan_replaced = {}, {}, {}
            plan_error = None
            for plan_name in ("visual_plan.json", "scene_visual_plan.json"):
                plan_path = cand / plan_name
                if not plan_path.is_file():
                    continue
                try:
                    plan_doc = load_json(plan_path)
                    for key, spec in (plan_doc.get("images") or {}).items():
                        page = int((spec or {}).get("page") or key)
                        sha = (spec or {}).get("sha256")
                        file_path = (spec or {}).get("file")
                        if not sha and file_path and Path(file_path).is_file():
                            sha = hashlib.sha256(Path(file_path).read_bytes()).hexdigest()
                        if not sha:
                            raise ValueError(f"{plan_name} 中图片缺少身份哈希")
                        vplan_img_sets.setdefault(page, set()).add(sha)
                        rep_sha = (spec or {}).get("replaced_visual_sha256")
                        if rep_sha:
                            vplan_replaced.setdefault(page, []).append(rep_sha)
                except Exception as e:  # noqa: BLE001
                    plan_error = f"{plan_name}: {type(e).__name__}: {e}"
                    break
            if plan_error:
                media_status = "FAILED"
                findings.append({"code": "FIDELITY.IMAGE_PLAN_UNVERIFIED",
                                 "severity": "BLOCKING", "origin": "media_binding",
                                 "slide": None, "actual": plan_error,
                                 "detail": "配图计划不可读或身份不完整，拒绝退回数量兜底。"})
            else:
                vplan_pages = {page: len(shas) for page, shas in vplan_img_sets.items()}
                vplan_imgs = {page: sorted(shas) for page, shas in vplan_img_sets.items()}
                try:
                    findings += checks_mod.check_media_binding(
                        pptx, in_doc, out_doc, planned_pages=vplan_pages,
                        in_pptx_path=Path(job["input_path"]),
                        planned_images=vplan_imgs or None,
                        replaced_images=vplan_replaced or None)
                    media_status = "EXECUTED"
                except Exception as e:  # noqa: BLE001
                    media_status = "CRASHED"
                    findings.append({"code": "GATE.CHECKER_CRASH", "severity": "BLOCKING",
                                     "origin": "media_binding", "slide": None,
                                     "detail": f"图片绑定检查异常：{type(e).__name__}: {e}"})

        # A03：几何必须来自实际候选 PPTX（不读布局记录）
        geometry_status = "NOT_APPLICABLE"
        if "object.actual_geometry" in req:
            try:
                findings += checks_mod.check_geometry_bounds(pptx, out_doc)
                geometry_status = "EXECUTED"
            except Exception as e:  # noqa: BLE001
                geometry_status = "CRASHED"
                findings.append({"code": "GATE.CHECKER_CRASH", "severity": "BLOCKING",
                                 "origin": "actual_pptx_geometry", "slide": None,
                                 "detail": f"实际几何检查异常：{type(e).__name__}: {e}"})

        # P2：规则表关系核验（关闭自动修复的纯检测）
        table_status = "NOT_APPLICABLE"
        table_summary = {}
        if "table.relation_binding" in req:
            try:
                src_tables = checks_mod._tables_of(in_doc)
                out_tables = checks_mod._tables_of(out_doc)
                table_summary = {"source_tables": len(src_tables), "output_tables": len(out_tables),
                                 "source_rows": sum(len(t.rows) for t in src_tables),
                                 "output_rows": sum(len(t.rows) for t in out_tables)}
                findings += checks_mod.check_table_relations(in_doc, out_doc)
                table_status = "EXECUTED"
            except Exception as e:  # noqa: BLE001
                table_status = "CRASHED"
                findings.append({"code": "GATE.CHECKER_CRASH", "severity": "BLOCKING",
                                 "origin": "table", "slide": None,
                                 "detail": f"表格关系检查异常：{type(e).__name__}: {e}"})

        # T1：独立解析一致性（普查器 vs 业务提取器，同一份原始输入）
        census_consistency_status = "NOT_APPLICABLE"
        if "parser.census_consistency" in req:
            try:
                findings += checks_mod.check_census_consistency(src)
                census_consistency_status = "EXECUTED"
            except Exception as e:  # noqa: BLE001
                census_consistency_status = "CRASHED"
                findings.append({"code": "GATE.CHECKER_CRASH", "severity": "BLOCKING",
                                 "origin": "census_consistency", "slide": None,
                                 "detail": f"独立解析一致性检查异常：{type(e).__name__}: {e}"})

        # T1：字段与软换行的来源→候选保真（事件级）。与 census_consistency 是两条
        # 不同口径：后者查"两个解析器读同一份输入是否一致"，本项查"候选是否改了内容"。
        field_break_status = "NOT_APPLICABLE"
        if "field.break_fidelity" in req:
            try:
                findings += checks_mod.check_field_break_fidelity(
                    src, pptx, approved_normalizations,
                    src_cc=(census or {}).get("content_census"))
                field_break_status = "EXECUTED"
            except Exception as e:  # noqa: BLE001
                field_break_status = "CRASHED"
                findings.append({"code": "GATE.CHECKER_CRASH", "severity": "BLOCKING",
                                 "origin": "field_break_fidelity", "slide": None,
                                 "detail": f"字段/软换行保真检查异常：{type(e).__name__}: {e}"})

        # P2：双解析对象集合一致性（R09）。两个视角对不上时，任何保真核验的分母都不可信。
        parser_status = "NOT_APPLICABLE"
        if "parser.object_agreement" in req:
            try:
                findings += checks_mod.check_parser_agreement(pptx)
                parser_status = "EXECUTED"
            except Exception as e:  # noqa: BLE001
                parser_status = "CRASHED"
                findings.append({"code": "GATE.CHECKER_CRASH", "severity": "BLOCKING",
                                 "origin": "parser_agreement", "slide": None,
                                 "detail": f"双解析一致性检查异常：{type(e).__name__}: {e}"})

        # P2 图表：原生性（E02）+ 系列—类别—数值绑定（C07）+ 工作簿/缓存一致（C11 复验）
        chart_status = "NOT_APPLICABLE"
        chart_conflict_status = "NOT_APPLICABLE"
        chart_summary = {}
        if "chart.relation_binding" in req:
            try:
                src_charts = checks_mod._charts_of(in_doc)
                out_charts = checks_mod._charts_of(out_doc)
                chart_summary = {"source_charts": len(src_charts),
                                 "output_charts": len(out_charts),
                                 "types": sorted({c.chart_type for c in src_charts})}
                findings += checks_mod.check_chart_relations(in_doc, out_doc)
                chart_status = "EXECUTED"
            except Exception as e:  # noqa: BLE001
                chart_status = "CRASHED"
                findings.append({"code": "GATE.CHECKER_CRASH", "severity": "BLOCKING",
                                 "origin": "chart", "slide": None,
                                 "detail": f"图表关系检查异常：{type(e).__name__}: {e}"})
            try:
                findings += checks_mod.check_chart_output_sources(out_doc)
                chart_conflict_status = "EXECUTED"
            except Exception as e:  # noqa: BLE001
                chart_conflict_status = "CRASHED"
                findings.append({"code": "GATE.CHECKER_CRASH", "severity": "BLOCKING",
                                 "origin": "chart_source", "slide": None,
                                 "detail": f"图表来源核对异常：{type(e).__name__}: {e}"})

        # 真实渲染门禁：渲染重试受同一总预算约束，并在重试前确认任务未被取消/未过期
        def _cancelled() -> bool:
            return (d / ".cancelled").exists() or \
                job["attempt_id"] in (load_json(d / "job.json").get("cancelled_attempts", []))

        # P3：渲染目录必须先按"候选 + 字体环境 + 渲染参数"绑定；对不上就把旧目录
        # 整体移到 render_history（保留字节），再在新目录重渲染。
        # 不能"目录存在就复用"——事故里正是复用了字体环境已变的坏 PNG。
        rdir, cache_key = self._prepare_render_dir(d, pptx)
        rr = render_mod.render_real(pptx, rdir, cancel_check=_cancelled,
                                    pid_file=str(self._render_pid_file(job_id)),
                                    data_root=self.root)
        pdf_pages = None
        if rr.get("pdf"):
            pdf_pages, err = render_mod.pdf_page_count(rr["pdf"])
            rr["pdf_page_count"] = pdf_pages
            rr["pdf_page_count_error"] = err
            # 页集合按明确的导出规则比较：隐藏页不进入 PDF，因此用"应导出页数"比对，
            # 而不是用总页数。两侧数字都落盘，避免用渲染器退出码代替页数核对。
            rr["pptx_slides"] = len(out_pages)
            rr["pptx_hidden_pages"] = sum(
                1 for p in out_pages if isinstance(p, dict) and p.get("hidden"))
            rr["pptx_slides_exportable"] = len(out_pages) - rr["pptx_hidden_pages"]
            rr["page_count_rule"] = "隐藏页不导出；PDF 页数应等于非隐藏页数"
            rr["counts_consistent"] = (
                pdf_pages == rr["pptx_slides_exportable"]) if pdf_pages is not None else None
            stats, err = render_mod.pdf_cjk_stats(rr["pdf"])
            rr["pdf_text_stats"] = stats
            rr["pdf_text_error"] = err
            # 中文出字核对：输入有中文却没有中文出现在渲染文本层 → 静默丢字，必须阻断。
            # 口径：按**导出范围**统计。隐藏页不进 PDF，因此隐藏页里的中文
            # 不能算进"应该在渲染产物里出现"的分母——否则"只有隐藏页含中文"
            # 的正常导出会被全局哨兵误报。两个数字都落盘，便于复核差别。
            cjk_all = 0
            cjk_exportable = 0
            for pg, u in in_doc.all_units():
                if not any("\u4e00" <= ch <= "\u9fff" for ch in u.text):
                    continue
                cjk_all += 1
                if not (isinstance(pg, dict) and pg.get("hidden")):
                    cjk_exportable += 1
            in_cjk = cjk_exportable
            rr["cjk_units_in_input"] = in_cjk
            rr["cjk_units_in_input_all_pages"] = cjk_all
            rr["cjk_gate_scope"] = "exportable_pages_only（隐藏页不计入分母）"
            rr["cjk_glyphs_in_render"] = (stats or {}).get("cjk_chars")
        if rr["code"] != "RENDER.OK":
            findings.append({"code": rr["code"], "severity": "BLOCKING", "origin": "render",
                             "slide": None, "detail": rr["detail"],
                             "renderer": rr.get("renderer"), "exit_code": rr.get("exit_code")})
        # 渲染器可能对损坏文件仍退出 0 并产出 PDF；页数不一致必须阻断，不能算渲染通过
        if rr.get("counts_consistent") is False:
            findings.append({"code": "RENDER.PAGE_COUNT_MISMATCH", "severity": "BLOCKING",
                             "origin": "render", "slide": None,
                             "expected": f"应导出页数 {rr.get('pptx_slides_exportable')}"
                                         f"（总 {rr.get('pptx_slides')} 页，"
                                         f"隐藏 {rr.get('pptx_hidden_pages')} 页）",
                             "actual": f"真实渲染 PDF 页数 {rr.get('pdf_page_count')}",
                             "detail": "按「隐藏页不导出」规则，PDF 页数与非隐藏页数不一致；"
                                       "渲染器退出码 0 不能代替页数核对。"})
        if rr["code"] == "RENDER.OK" and rr.get("counts_consistent") is False:
            rr["code"] = "RENDER.PAGE_COUNT_MISMATCH"
        # 渲染成功但中文没出字：只看退出码会完全漏掉，必须单独拦截
        f_cjk = render_mod.cjk_gate_finding(rr, rr.get("cjk_units_in_input", 0))
        if f_cjk:
            findings.append(f_cjk)
            rr["code"] = "RENDER.CJK_GLYPH_MISSING"

        # 字形质量（真正的判据）：文本层有字 ≠ 字形画出来了。
        # 旧门禁只看文本层汉字数，事故里 2314 个字全部是方框却照样通过。
        glyph = None
        if rr.get("pdf"):
            # ---- 可信预期：**渲染之前**就从来源台账建立"每页应显示多少中文" ----
            # 不从渲染后的 PDF 反推预期，也不因为候选已漏字就把预期改成 0。
            # 来源读不出来时按"预期无法建立"处理，不得回落成 expected=0 放行。
            glyph_expected = glyph_mod.expected_cjk_by_page(in_doc)
            try:
                glyph = glyph_mod.analyze_pdf(rr["pdf"], rdir / "png",
                                              dpi=rr.get("dpi") or 110)
            except Exception as e:  # noqa: BLE001
                glyph = {"verdict": "NOT_EVALUABLE",
                         "error": f"{type(e).__name__}: {e}", "pages": []}
            # 判定适用性：分母来自可信预期，不是输出侧
            glyph = glyph_mod.apply_expectation(glyph, glyph_expected)
            rr["glyph_quality"] = {"verdict": glyph.get("verdict"),
                                   "summary": glyph.get("summary"),
                                   "engine": glyph.get("engine"),
                                   "applicability": glyph.get("applicability")}
            rr["glyph_expectation"] = glyph.get("expectation")
            rr["cjk_units_in_input"] = int(
                (glyph.get("expectation") or {}).get("expected_cjk_units") or 0)
            findings += glyph_mod.quality_findings(glyph, glyph_expected)
            if glyph.get("verdict") == "MISSING":
                rr["code"] = ("RENDER.GLYPH_MISSING"
                              if glyph.get("applicability") == "APPLICABLE"
                              else "RENDER.CJK_TEXT_PRESENT_NOT_RENDERED")
            self._write_render_meta(rdir, cache_key, pptx, rr, glyph)

        # R5 材料生成：来源侧覆盖核对在**写 findings 之前**执行，
        # 否则其 finding 不会进入 findings.json，也不会计入 blocking_findings。
        if exp.get("material_source"):
            try:
                src_findings = source_fidelity_mod.check_fact_coverage(
                    exp["material_source"], pptx, exp.get("material_plan"))
            except Exception as e:  # noqa: BLE001
                src_findings = [{"code": "GATE.CHECKER_CRASH", "severity": "BLOCKING",
                                 "origin": "source_coverage", "slide": None,
                                 "detail": f"来源侧核对异常：{type(e).__name__}: {e}"}]
            findings += src_findings
            src_blocking = [f for f in src_findings if f.get("severity") == "BLOCKING"]
            exp["source_fact_coverage_status"] = "FAILED" if src_blocking else "EXECUTED"
            exp["required"] = list(dict.fromkeys(list(req) + ["source.fact_coverage"]))
            req = exp["required"]

        # 主题: 对比度与"是否真的套用"的账本来自**真实应用器**（theme_applied.json），
        # 不在这里事后估算。保留原设计 ≠ 主题已应用，两者分别记账。
        theme_stats = load_json(cand / "theme_applied.json") or {}
        theme_application = theme_stats.get("theme_application")
        contrast = theme_stats.get("contrast") or {}
        # 未达门槛清单由应用器写在顶层（保留原色与业务色的结果都在里面），
        # 而 contrast 是逐行明细。两处合并取，避免"账本有、判定看不见"。
        below_items = list(theme_stats.get("below_threshold") or []) \
            or list(contrast.get("below_threshold") or [])
        theme_contrast_status = "EXECUTED"
        if theme_stats.get("preserve_design"):
            same = (theme_stats.get("input_sha256") == theme_stats.get("output_sha256"))
            if not same:
                findings.append({"code": "THEME.PRESERVE_MUTATED", "severity": "BLOCKING",
                                 "origin": "theme", "slide": None,
                                 "detail": "保留原设计路径改动了字节；它与输入必须逐字节一致。"})
                theme_contrast_status = "FAILED"
        else:
            for item in contrast.get("unsafe") or []:
                findings.append({
                    "code": "THEME.UNSAFE_CONVERSION", "severity": "BLOCKING",
                    "origin": "theme", "slide": item.get("slide"),
                    "expected": f"文字与实际底色 ≥ {item.get('orig_ratio')}",
                    "actual": f"改色后 {item.get('theme_ratio')}",
                    "detail": f"{item.get('shape')}：{item.get('reason')}"})
                theme_contrast_status = "FAILED"
            # 低于适用门槛的对象：**不记为通过**，哪怕它是原稿本来就低。
            # 原稿既有只影响"是不是本次改坏"，不影响"最终是否达标"这一结论。
            seen = set()
            for item in below_items:
                key = (item.get("slide"), item.get("shape"))
                if key in seen:
                    continue
                seen.add(key)
                pre = item.get("preexisting")
                findings.append({
                    "code": "THEME.CONTRAST_BELOW_THRESHOLD", "severity": "REVIEW",
                    "origin": "theme", "slide": item.get("slide"),
                    "shape": item.get("shape"),
                    "expected": f"该字号/字重适用 {item.get('required')}:1",
                    "actual": f"实测 {item.get('ratio')}:1",
                    "detail": (f"{item.get('shape')}（{item.get('kind')}）"
                               + (f" 文本 {item.get('text')!r}" if item.get("text") else "")
                               + (f"，{item.get('font_pt')}pt"
                                  + ("，粗体" if item.get("bold") else "")
                                  if item.get("font_pt") else "")
                               + f"：最终仍低于 {item.get('required')}:1；"
                               + ("原稿本来就低、本次未恶化——需要你决定是否调整该处设计"
                                  if pre else "本次转换后未达标"))})
            for item in contrast.get("unknown_backdrop") or []:
                findings.append({
                    "code": "THEME.BACKDROP_UNVERIFIED", "severity": "REVIEW",
                    "origin": "theme", "slide": item.get("slide"),
                    "detail": f"{item.get('shape')}：{item.get('reason')}"
                              "（渐变/图片/半透明底色，无法据此证明对比度）"})
                theme_contrast_status = "NOT_EVALUABLE"

        # HG13：配对渲染硬门禁。调用 make_scene_suppressed_pptx 获得无场景参考件，
        # 用同版本真实渲染器渲染两份件，生成 WG-SEMANTIC-SCENE-RENDER-EVIDENCE/1 证据，
        # 再由 validate_complete_output 执行结构 + 像素双重核验。
        scene_gate_status = "NOT_EVALUABLE"
        scene_gate_report = {"status": "NOT_EVALUABLE", "findings": []}
        scene_plan_file = cand / "scene_visual_plan.json"
        try:
            from . import semantic_scenes as scene_mod
            if not scene_plan_file.is_file():
                scene_gate_status = "FAILED"
                scene_gate_report = {
                    "schema_version": "WG-COMPLETE-SEMANTIC-SCENE-GATE/1",
                    "gate_id": "HG13_SEMANTIC_SCENE_COVERAGE",
                    "status": "FAILED", "blocking": True,
                    "findings": [{"code": "SCENE.PLAN_MISSING", "severity": "BLOCKING",
                                  "detail": "候选目录中缺少冻结场景计划 scene_visual_plan.json。"}],
                    "unverified": [],
                }
                findings.extend(scene_gate_report["findings"])
            else:
                plan_data = json.loads(scene_plan_file.read_text(encoding="utf-8"))
                planned_images = plan_data.get("images") or {}
                # 没有配图分两种，run 阶段（见 semantic_scenes 调用处）已经按这个
                # 口径放行过一次："版位容量不足"（unplaced_scenes，如实登记、
                # 没有伪造替代）允许稿子照常交付；只有"找不到/不合规"
                # （unresolved_scenes，真失败）或两者皆空（没有一个有依据的
                # 具体场景）才阻断。validate 阶段之前对这两种原因不加区分，
                # 一律 BLOCKING——这让 run 阶段明确放行过的版位容量受限稿件，
                # 到 validate 必然又被此门禁拦下，两处口径互相矛盾
                # （2026-10-27 job-c915c8c7deff / job-a7f6f5400f70：场景规划
                # 找到了有依据的场景，但稿面确实放不下，仍被判 BLOCKING）。
                capacity_only = (not planned_images and bool(plan_data.get("unplaced_scenes"))
                                 and not plan_data.get("unresolved_scenes")
                                 and not plan_data.get("text_cards"))
                if not planned_images and not capacity_only:
                    scene_gate_status = "FAILED"
                    scene_gate_report = {
                        "schema_version": "WG-COMPLETE-SEMANTIC-SCENE-GATE/1",
                        "gate_id": "HG13_SEMANTIC_SCENE_COVERAGE",
                        "status": "FAILED", "blocking": True,
                        "findings": [{"code": "SCENE.NO_SCENE_PICTURES", "severity": "BLOCKING",
                                      "detail": "冻结场景计划中没有计划嵌入的场景照片。"}],
                        "unverified": [],
                    }
                    findings.extend(scene_gate_report["findings"])
                elif capacity_only:
                    scene_gate_status = "NOT_EVALUABLE"
                    scene_gate_report = {
                        "schema_version": "WG-COMPLETE-SEMANTIC-SCENE-GATE/1",
                        "gate_id": "HG13_SEMANTIC_SCENE_COVERAGE",
                        "status": "NOT_EVALUABLE", "blocking": False,
                        "findings": [],
                        "unverified": [{"code": "SCENE.CAPACITY_NO_SAFE_AREA",
                                        "detail": ("所有规划场景都有原文依据，但稿面没有空间可安全放置照片"
                                                   "（已逐一记录在 unplaced_scenes，不是找不到/不合规）；"
                                                   "本稿未嵌入场景照片，按容量限制如实交付，未伪造或替代任何内容。"),
                                        "unplaced_scenes": plan_data.get("unplaced_scenes")}],
                    }
                else:
                    # 1. 制造仅移除标记场景图片的抑制参考件
                    suppressed_dir = d / "scene_suppressed_render"
                    suppressed_dir.mkdir(parents=True, exist_ok=True)
                    suppressed_pptx = suppressed_dir / f"{pptx.stem}-suppressed.pptx"
                    suppressed_identity = scene_mod.make_scene_suppressed_pptx(pptx, suppressed_pptx)

                    # 2. 用同版本真实渲染器渲染抑制参考件
                    supp_render_dir = suppressed_dir / "render"
                    supp_rr = render_mod.render_real(
                        suppressed_pptx, supp_render_dir,
                        cancel_check=_cancelled,
                        pid_file=str(self._render_pid_file(job_id)),
                        data_root=self.root,
                    )

                    if supp_rr.get("code") != "RENDER.OK" or not supp_rr.get("pdf") or not supp_rr.get("png_dir"):
                        scene_gate_status = "NOT_EVALUABLE"
                        scene_gate_report = {
                            "schema_version": "WG-COMPLETE-SEMANTIC-SCENE-GATE/1",
                            "gate_id": "HG13_SEMANTIC_SCENE_COVERAGE",
                            "status": "NOT_EVALUABLE", "blocking": True,
                            "findings": [],
                            "unverified": [{"code": "SCENE.SUPPRESSED_RENDER_FAILED",
                                            "detail": f"无场景参考件渲染失败：{supp_rr.get('detail') or supp_rr.get('code')}"}],
                        }
                    elif rr.get("code") != "RENDER.OK" or not rr.get("pdf") or not rr.get("png_dir"):
                        scene_gate_status = "NOT_EVALUABLE"
                        scene_gate_report = {
                            "schema_version": "WG-COMPLETE-SEMANTIC-SCENE-GATE/1",
                            "gate_id": "HG13_SEMANTIC_SCENE_COVERAGE",
                            "status": "NOT_EVALUABLE", "blocking": True,
                            "findings": [],
                            "unverified": [{"code": "SCENE.CANDIDATE_RENDER_FAILED",
                                            "detail": f"候选渲染失败：{rr.get('detail') or rr.get('code')}"}],
                        }
                    else:
                        # 3. 构造逐页渲染证据
                        cand_png_dir = Path(rr["png_dir"])
                        supp_png_dir = Path(supp_rr["png_dir"])
                        pages_evidence = []
                        total_pages = len(out_pages)
                        for page_num in range(1, total_pages + 1):
                            cand_png = cand_png_dir / f"page-{page_num:02d}.png"
                            if not cand_png.is_file():
                                cand_png = cand_png_dir / f"page-{page_num}.png"
                            supp_png = supp_png_dir / f"page-{page_num:02d}.png"
                            if not supp_png.is_file():
                                supp_png = supp_png_dir / f"page-{page_num}.png"
                            pages_evidence.append({
                                "page": page_num,
                                "png_path": str(cand_png),
                                "png_sha256": sha256_file(cand_png) if cand_png.is_file() else "",
                                "suppressed_png_path": str(supp_png),
                                "suppressed_png_sha256": sha256_file(supp_png) if supp_png.is_file() else "",
                            })

                        render_evidence = {
                            "schema": "WG-SEMANTIC-SCENE-RENDER-EVIDENCE/1",
                            "candidate_pptx_sha256": sha256_file(pptx),
                            "scene_plan_sha256": (load_json(scene_plan_file) or {}).get("plan_sha256"),
                            "scene_suppressed_pptx_sha256": suppressed_identity["scene_suppressed_pptx_sha256"],
                            "policy_sha256": scene_mod.POLICY_SHA256,
                            "rendered_pdf_sha256": rr.get("pdf_sha256"),
                            "rendered_pdf_path": str(rr.get("pdf")),
                            "suppressed_rendered_pdf_sha256": supp_rr.get("pdf_sha256"),
                            "suppressed_rendered_pdf_path": str(supp_rr.get("pdf")),
                            "renderer": {
                                "name": "libreoffice",
                                "version": str(rr.get("soffice_version") or "unknown"),
                            },
                            "pages": pages_evidence,
                        }

                        # 4. 调用 Codex 实现的 validate_complete_output
                        scene_gate_report = scene_mod.validate_complete_output(
                            src, pptx, scene_plan_file, render_evidence,
                            asset_dir=cand / "scene_assets",
                            require_visual_review=True,
                        )
                        gate_status = scene_gate_report.get("status")
                        gate_blocking = scene_gate_report.get("blocking", True)
                        gate_findings = scene_gate_report.get("findings") or []
                        gate_unverified = scene_gate_report.get("unverified") or []

                        if gate_status == "EXECUTED" and not gate_blocking and not gate_findings and not gate_unverified:
                            scene_gate_status = "EXECUTED"
                        elif gate_findings or gate_status == "FAILED":
                            scene_gate_status = "FAILED"
                        else:
                            scene_gate_status = "NOT_EVALUABLE"

                        for item in gate_findings:
                            findings.append({**item, "origin": "semantic_scene_gate", "slide": item.get("slide")})
                        if gate_unverified:
                            for item in gate_unverified:
                                findings.append({
                                    "code": item.get("code", "SCENE.UNVERIFIED"),
                                    "severity": "REVIEW",
                                    "origin": "semantic_scene_gate",
                                    "slide": item.get("slide"),
                                    "detail": item.get("detail", "场景门禁存在未核验项"),
                                })
        except Exception as e:  # noqa: BLE001
            scene_gate_status = "NOT_EVALUABLE"
            scene_gate_report = {"status": "NOT_EVALUABLE", "findings": [
                {"code": "SCENE.CHECKER_CRASH", "severity": "REVIEW",
                 "detail": f"HG13 检查器异常：{type(e).__name__}: {e}"}]}
            findings.extend([{**item, "origin": "semantic_scene_gate", "slide": None}
                             for item in scene_gate_report["findings"]])
        scene_gate_run = (d / "semantic_scene_gate_runs" /
                          f"{sha256_file(pptx)[:16]}-{time.time_ns()}")
        scene_gate_run.mkdir(parents=True, exist_ok=False)
        scene_gate_report["candidate_pptx_sha256"] = sha256_file(pptx)
        scene_gate_report["source_pptx_sha256"] = sha256_file(src) if src.is_file() else None
        scene_plan_file = cand / "scene_visual_plan.json"
        scene_gate_report["scene_plan_sha256"] = (sha256_file(scene_plan_file)
                                                    if scene_plan_file.is_file() else None)
        scene_gate_report["run_directory"] = str(scene_gate_run)
        save_json_atomic(scene_gate_report, scene_gate_run / "semantic_scene_gate.json")

        # HG01–HG12 是实际候选的独立全页硬门禁，运行在工作台 validate 之内。
        # 即使门禁崩溃、无字版渲染缺失或原生复核未提供，也记 REVIEW/NOT_EVALUABLE，
        # 绝不让一个缺证据的候选继续 publish/export。
        layout_gate_status, layout_gate_summary, layout_gate_findings = \
            self._run_layout_hard_gate(job_id, job, pptx, rr.get("pdf"), exp,
                                       cancel_check=_cancelled)
        findings.extend(layout_gate_findings)
        save_json_atomic({"schema_version": "1.0.0", "job_id": job_id, "run_id": job["run_id"],
                          "attempt_id": job["attempt_id"], "generated_at": now_iso(),
                          "findings": findings}, d / "findings.json")

        blocking = [f for f in findings if f["severity"] == "BLOCKING"]
        reviewing = [f for f in findings if f["severity"] == "REVIEW"]
        # W01：预期集合必须被实际报告覆盖。缺一条（报告漏项）与"状态非 EXECUTED"
        # 是两种不同失败，都要拦；空集合也必须拦。
        not_impl = [c for c in req if c not in IMPLEMENTED_CHECKS]
        not_evaluable = [c for c in req
                         if (c == "render.real_render" and rr["code"] != "RENDER.OK")
                         or (c == "layout.hard_gate"
                             and layout_gate_status == "NOT_EVALUABLE")
                         or (c == "visual.semantic_scene_coverage"
                             and scene_gate_status == "NOT_EVALUABLE")]
        checks_map = {
            "content_fidelity.required_text": "EXECUTED",
            "content_fidelity.occurrence": "EXECUTED",
            "content_fidelity.sign": "EXECUTED",
            "native_editability.visible_native": "EXECUTED",
            "object.actual_geometry": geometry_status,
            "content_fidelity.hidden_state": hidden_status,
            "object.group_structure": group_status,
            "parser.census_consistency": census_consistency_status,
            "render.real_render": ("EXECUTED" if rr["code"] == "RENDER.OK"
                                   else ("FAILED" if rr["code"] in
                                         ("RENDER.PAGE_COUNT_MISMATCH", "RENDER.EXECUTION_FAILED",
                                          "RENDER.CJK_GLYPH_MISSING",
                                          "RENDER.PNG_RASTERIZATION_FAILED",
                                          "RENDER.GLYPH_MISSING",
                                          "RENDER.FONTCONFIG_RESOURCE_MISSING")
                                         else "NOT_EVALUABLE")),
            # 字形：MISSING=FAILED；无法评价(NOT_EVALUABLE/过多页证据不足)=NOT_EVALUABLE；
            # 没有中文的稿=EXECUTED（分母为 0，不是"跳过"）。
            # 只有"可信预期明确为 0"才是真正的不适用；预期 > 0 且输出侧
            # 确实评估通过（OK）同样记 EXECUTED。其余一律 NOT_EVALUABLE。
            "render.glyph_quality": _glyph_check_status(glyph),
            "theme.contrast": theme_contrast_status,
            "layout.hard_gate": layout_gate_status,
            "visual.semantic_scene_coverage": scene_gate_status,
        }
        if "table.relation_binding" in req:
            checks_map["table.native_present"] = table_status
            checks_map["table.relation_binding"] = table_status
        if "parser.object_agreement" in req:
            checks_map["parser.object_agreement"] = parser_status
        if "chart.relation_binding" in req:
            checks_map["chart.native_present"] = chart_status
            checks_map["chart.relation_binding"] = chart_status
            checks_map["chart.source_consistency"] = chart_conflict_status
        if "field.break_fidelity" in req:
            checks_map["field.break_fidelity"] = field_break_status
        if "content_fidelity.notes" in req:
            checks_map["content_fidelity.notes"] = notes_status
        if "object.media_binding" in req:
            checks_map["object.media_binding"] = media_status
        # R5：来源侧覆盖核对的状态在此并入报告（checks_map 构造之后才赋值）。
        # 材料任务即便台账不可用也必须出现在报告里，状态为 NOT_EVALUABLE，
        # 使"必需检查未实际执行通过"这一既有拦截生效。
        if exp.get("material_status", "NOT_MATERIAL") != "NOT_MATERIAL":
            checks_map["source.fact_coverage"] = exp.get(
                "source_fact_coverage_status",
                "EXECUTED" if exp.get("material_source") is not None else "NOT_EVALUABLE")

        # RS-9：条件必检项执行（W4 接线）。函数由 W1/W2/W3 交付；未就位/异常/超时
        # 一律 NOT_EVALUABLE——缺证据不能当通过（下面 missing_from_report 之前并入，
        # 使"到期但未执行"与"未实现"走同一条既有拦截）。
        if rs_options:
            try:
                from wengao_ppt import gate_rs
                for _rs_name, _rs_status in gate_rs.run_rs_checks(pptx, d, rs_options).items():
                    checks_map[_rs_name] = _rs_status
            except Exception:  # noqa: BLE001 —— 接线层异常不得凭空放行
                pass

        # 报告漏项：预期必需但 checks 里根本没有这一条 → 单独列出，不能被"全绿"掩盖
        missing_from_report = [c for c in req if c not in checks_map]
        if missing_from_report:
            findings.append({"code": "GATE.CHECK_NOT_IMPLEMENTED", "severity": "BLOCKING",
                             "origin": "required_check_coverage", "slide": None,
                             "expected": missing_from_report, "actual": sorted(checks_map),
                             "detail": "预期必需检查未出现在报告中；"
                                       "不得因报告里剩下的项全绿就放行。"})
            blocking = blocking + [f for f in findings[-1:]]
        if not req:
            findings.append({"code": "GATE.CHECK_NOT_IMPLEMENTED", "severity": "BLOCKING",
                             "origin": "required_check_coverage", "slide": None,
                             "detail": "预期必需检查集合为空；空集合不得放行。"})
            blocking = blocking + [f for f in findings[-1:]]
        # 必需检查"无法评价"与"失败"必须同样拦：缺证据不能当通过。
        not_evaluable_checks = [k for k, val in checks_map.items()
                                if val == "NOT_EVALUABLE"]
        if not_evaluable_checks:
            findings.append({"code": "GATE.CHECK_NOT_EVALUABLE", "severity": "REVIEW",
                             "origin": "required_check_coverage", "slide": None,
                             "expected": "必需检查可评价且通过",
                             "actual": not_evaluable_checks,
                             "detail": "这些必需检查无法给出结论（缺证据/不可计算）；"
                                       "不得以'没报错'代替通过。"})
        # 收口重算：上面几步可能追加了 findings（漏项/空集合/无法评价）。
        # 必须在判定与落盘**之前**重算，否则会出现"报告说 1 条阻断、findings.json 里没有"。
        blocking = [f for f in findings if f.get("severity") == "BLOCKING"]
        reviewing = [f for f in findings if f.get("severity") == "REVIEW"]
        save_json_atomic({"schema_version": "1.0.0", "job_id": job_id,
                          "run_id": job["run_id"], "attempt_id": job["attempt_id"],
                          "generated_at": now_iso(), "stage": "validate",
                          "blocking": len(blocking), "review": len(reviewing),
                          "findings": findings}, d / "findings.json")
        if blocking:
            verdict = "FAILED"
        elif reviewing:
            # REVIEW 级 finding（如图表来源冲突）不得放行为候选：先人工裁定
            verdict = "NEEDS_REVIEW"
        else:
            verdict = "DEVELOPMENT_CANDIDATE"
        validation = {
            "schema_version": "1.0.0", "job_id": job_id, "run_id": job["run_id"],
            "attempt_id": job["attempt_id"], "contract_version": CONTRACT_VERSION,
            "support_profile": job["support_profile"], "generated_at": now_iso(),
            # D10-B：验收记录携带主题身份（与任务冻结值一致时才可用于导出）
            "theme": {
                "theme_id": job.get("theme"),
                "theme_name": job.get("theme_name"),
                "theme_version": job.get("theme_version"),
                "theme_registry_version": job.get("theme_registry_version"),
                "config_digest": job.get("theme_config_digest"),
                "frozen_at_job": bool(job.get("theme_config_digest")),
                "legacy_no_digest": not bool(job.get("theme_config_digest")),
            },
            "candidate_pptx": str(pptx), "candidate_pptx_sha256": sha256_file(pptx),
            "input_sha256": job["input_hash"],
            "checks": checks_map,
            "required_checks": req,
            "required_checks_derived_from": exp["derived_from"],
            "content_derived_scope_gaps": exp["scope_gaps"],
            "missing_from_report": missing_from_report,
            "table_summary": table_summary,
            "chart_summary": chart_summary,
            "not_implemented_required_checks": not_impl,
            "not_evaluable_required_checks": not_evaluable,
            "render": rr,
            "layout_hard_gate": layout_gate_summary,
            "semantic_scene_gate": scene_gate_report,
            # 字形证据、环境指纹、主题账本：都随验收落盘，便于复核"是谁判通过的"。
            "glyph_quality": {
                "verdict": (glyph or {}).get("verdict"),
                "engine": (glyph or {}).get("engine"),
                "params": (glyph or {}).get("params"),
                "summary": (glyph or {}).get("summary"),
                "boundary": (glyph or {}).get("boundary"),
                "pages": (glyph or {}).get("pages"),
                "pdf_sha256": (glyph or {}).get("pdf_sha256"),
            },
            "font_environment": rr.get("font_environment"),
            "environment_fingerprint": rr.get("environment_fingerprint"),
            "render_cache_key_sha256": cache_key,
            "theme_application": theme_application,
            "preserve_design": bool(job.get("preserve_design")),
            "theme_contrast": {
                "checked": (contrast or {}).get("checked"),
                "min_ratio": (contrast or {}).get("min_ratio"),
                "violations": (contrast or {}).get("violations"),
                "kept_original": (contrast or {}).get("kept_original"),
                "preexisting_low": (contrast or {}).get("preexisting_low"),
                "unsafe": (contrast or {}).get("unsafe"),
                "unknown_backdrop": (contrast or {}).get("unknown_backdrop"),
                # 结论口径：上面 checked 只是"算了多少个对象"，不等于达标。
                # compliant 才回答"是不是全部达到各自门槛"。
                "compliant": not bool(below_items
                                      or (contrast or {}).get("unsafe")
                                      or (contrast or {}).get("unknown_backdrop")),
                "below_threshold": below_items,
                "protected_business_colors": (theme_stats.get("protected_business_colors")
                                               if theme_stats else []) or [],
                "contrast_claim": (theme_stats.get("contrast_claim")
                                   if theme_stats else None),
                "preserve_limits": (theme_stats.get("preserve_limits")
                                     if theme_stats else []) or [],
                "note": "对比度按**文字实际相邻底色**计算；EXECUTED 只表示检查跑过。"
                        "compliant=false 表示有对象最终低于它适用的门槛——即使原因是原稿本来如此，"
                        "也不记为通过。保留原设计只披露原始限制，不声称达标。",
            },
            "blocking_findings": len(blocking),
            "review_findings": len(reviewing),
            "total_findings": len(findings),
            "verdict": verdict,
            "release_ready": False,
            "release_block_reason": ("完整发布 profile 的必需检查未全部实现/通过；"
                                     "本阶段只能产出 DEVELOPMENT_CANDIDATE。"),
        }
        save_json_atomic(validation, d / "validation.json")
        self._stage(job_id, "validate", "DONE", {"blocking": len(blocking), "total": len(findings)})
        # 终态 verdict 落盘：验收已经**结束**，任务状态不能再停在 RUNNING。
        # 旧行为只写 validation.json 就返回，job.json 的 verdict 停留在 RUNNING，
        # 于是「已完成的验收判定为 FAILED」与「任务显示运行中」并存，
        # 页面会把失败显示成持续运行。这里补上**同一份已写盘判定**的落点：
        # 复用既有 job.json 的 verdict 字段与 _set_verdict，不引入第二套状态机，
        # 也不改变任何判定——只是把判定结果写回它应该在的地方。
        # 导出/下载资格仍以 validation.json + 门禁为准，此处只修状态投影。
        self._set_verdict(job_id, verdict, {
            "validated_at": now_iso(),
            "validation_verdict": verdict,
            "blocking_findings": len(blocking),
            "validation_attempt_id": job.get("attempt_id"),
            "validation_run_id": job.get("run_id"),
        })
        return validation

    # ---------------------------------------------------------------- D1：有限确定性修复
    # ---- 修前证据隔离保留（D1 收口：原失败候选/台账/渲染的真实字节不得丢失）----
    @staticmethod
    def _repair_pre_snapshot(job_dir, cand_dir, cand_pptx, pre_dir):
        """把**本轮修前**的真实字节复制到该轮次专属目录。

        为什么要每轮一份、且不再用共享的 candidate.pre_repair：
        共享目录会被第 2 轮覆盖，导致"原始失败候选"的字节在第 2 轮后不可考。

        渲染目录用**移动**而非删除：既让后续 validate 必然重新渲染（重新绑定证据），
        又把修前那一组 PDF/PNG 的真实字节完整保留在轮次目录里，不被 rmtree 抹掉。
        """
        import shutil as _sh
        pre_dir.mkdir(parents=True, exist_ok=True)
        kept = {"dir": str(pre_dir), "files": {}, "render_moved": False}
        _sh.copyfile(cand_pptx, pre_dir / cand_pptx.name)
        kept["files"][cand_pptx.name] = sha256_file(cand_pptx)
        for rel in ("findings.json", "validation.json", "job.json"):
            s = job_dir / rel
            if s.exists():
                _sh.copyfile(s, pre_dir / rel)
                kept["files"][rel] = sha256_file(s)
        render_dir = cand_dir / "render"
        pre_render = pre_dir / "render"
        if render_dir.exists() and not pre_render.exists():
            _sh.move(str(render_dir), str(pre_render))
            kept["render_moved"] = True
            kept["render_files"] = sorted(
                p.name for p in pre_render.rglob("*") if p.is_file())
        return kept

    @staticmethod
    def _repair_restore_pre(cand_dir, pre_dir):
        """未采纳本轮独立候选时，把隔离保留的渲染放回原位，恢复修前状态。"""
        import shutil as _sh
        render_dir = cand_dir / "render"
        pre_render = pre_dir / "render"
        if pre_render.exists() and not render_dir.exists():
            _sh.move(str(pre_render), str(render_dir))
            return True
        return False

    @staticmethod
    def _finding_sig(f):
        """finding 的稳定签名，用于判断"修后是否仍是同一条阻断"。"""
        ref = f.get("source_ref") or {}
        return (
            f.get("code"), f.get("reason_code"),
            json.dumps(f.get("expected"), ensure_ascii=False, sort_keys=True),
            ref.get("slide"), ref.get("object"),
            f.get("subject"), f.get("column_header"),
            f.get("object_path"), f.get("para_idx"),
        )

    def repair(self, job_id, max_rounds=2):
        """D1 有限确定性修复（首批白名单）。

        链路（与任务书逐条对应）：
          修复计划先行 → 最多两轮 → 独立候选 → 保存后独立重读 →
          当前任务全部必需检查重新执行（重跑 validate，内含真实渲染与证据重绑定）。

        本方法**不提交、不固化、不放行**：副作用与过期结果不得在此提交。
        是否采纳由调用方决定，且只能经 submit_result（按 attempt 身份校验）进入。

        D10-A/X5：同 job 写入互斥（.run.lock，与 run 共用；取消不依赖该锁），
        轮次目录按 attempt 归属（repair/<attempt_id>/round_N）。

        返回修复报告 dict；不抛 GateError（失败也写进报告），除非候选/基线缺失。
        """
        d = self._job_dir(job_id)
        job = load_json(d / "job.json")
        v = load_json(d / "validation.json")
        findings = load_json(d / "findings.json").get("findings", [])
        cand = d / "candidate"
        pptx = cand / (Path(job["input_path"]).stem + ".themed.pptx")
        if not pptx.exists():
            raise GateError("GATE.ARTIFACT_MISSING", "候选 PPTX 不存在；先运行 run。")
        src = Path(job["input_path"])
        if not src.exists():
            raise GateError("INPUT.SOURCE_BASELINE_MISSING",
                            "修复需要原始来源基线（唯一来源的判定依据）。")

        # D10-A/X5：同 job 写入互斥 + attempt 归属的轮次目录。
        # repair 全程持 .run.lock（与 run 同一把锁）：并发的 run/repair 写同一任务
        # 会被 GATE.ATTEMPT_CONFLICT 明确拒绝，而不是交错写坏候选/渲染/状态。
        # 取消不依赖该锁（cancel 直接写标记并释放锁），随时可登记生效。
        with self._lock(job_id):
            return self._repair_locked(job_id, max_rounds, d, job, v, findings,
                                       pptx, src)

    def _repair_locked(self, job_id, max_rounds, d, job, v, findings, pptx, src):
        """repair 的持锁主体：由 repair() 在 .run.lock 内调用。

        轮次目录按 attempt 归属（repair/<attempt_id>/round_N）：旧 attempt 的
        修前快照与恢复不会被新 attempt 的轮次覆盖或混用。
        """
        attempt_id = job["attempt_id"]
        cand = d / "candidate"
        from . import repair as repair_mod
        from . import census as census_mod
        src_cc = census_mod.census_xml_content(src)

        # 修复计划必须绑定：任务/尝试身份、输入基线、候选哈希、契约/规则版本（§5.1）
        contracts_id = self.contracts_identity()
        report = {"schema_version": "1.0.0", "job_id": job_id,
                  "run_id": job["run_id"], "attempt_id": job["attempt_id"],
                  "support_profile": job.get("support_profile"),
                  "contract_version": job.get("contract_version"),
                  "contracts_identity": contracts_id,
                  "source_baseline": {"path": str(src),
                                      "sha256": sha256_file(src)},
                  "candidate_pptx_before_sha256": sha256_file(pptx),
                  "verdict_before": v["verdict"],
                  "rounds": [], "whitelist": list(repair_mod.WHITELIST),
                  "rounds_allowed": max_rounds}

        # ---- 第 1 轮计划（修复计划先行）----
        plan = repair_mod.plan_repairs(findings, src, pptx, src_cc=src_cc,
                                       round_no=1, max_rounds=max_rounds)
        plan_cand_sha = sha256_file(pptx)      # 计划期的候选哈希，供应用前复查比对
        report["plan_round1"] = plan.to_dict()
        if plan.normal_input:
            report["outcome"] = "NO_REPAIR_NEEDED"
            report["note"] = ("正常稿（无 finding）：不误修，未产出任何修复动作，"
                              "候选与有效版本均未触碰。")
            save_json_atomic(report, d / "repair_report.json")
            return report
        if not plan.actions:
            report["outcome"] = "REFUSED_ALL"
            report["note"] = (f"有 {len(findings)} 条 finding，但白名单内无可用修复"
                              f"（拒修 {len(plan.refused)} 条）；未改候选。")
            save_json_atomic(report, d / "repair_report.json")
            return report

        rounds_used = 0
        round_findings = findings
        for rnd in range(1, max_rounds + 1):
            rounds_used = rnd
            rdir = d / "repair" / attempt_id / f"round_{rnd}"
            independent = rdir / "repaired.pptx"

            # ---- 应用动作前复查：候选身份未变、尝试未过期（§5.2）----
            # 计划生成后候选被改动 / 对象映射变化 / 尝试过期 → 拒绝旧计划，
            # 不把旧值写进新候选。
            plan_cand_sha = sha256_file(pptx)
            job_now = load_json(d / "job.json")
            stale = None
            if rnd > 1 and plan_cand_sha != report["rounds"][-1].get(
                    "candidate_pptx_after_sha256"):
                stale = ("候选在上一轮验收后被改动，与当前计划不再对应；"
                         "拒绝应用旧计划")
            if job_now.get("attempt_id") != report["attempt_id"] \
                    or job_now.get("run_id") != report["run_id"]:
                stale = ("任务尝试身份已变化（attempt/run 与计划时不同）；"
                         "身份过期，拒绝应用旧计划")
            # 取消优先：该 attempt 已被取消 → 不再写入、不再推进（不自动重试）
            if self._cancel_flag(job_id).exists() \
                    or job_now.get("attempt_id") in (job_now.get("cancelled_attempts") or []) \
                    or job_now.get("verdict") == "CANCELLED":
                stale = ("该 attempt 已被取消（.cancelled / cancelled_attempts）："
                         "停止修复写入，不采纳、不提交、不自动重试")
            if stale:
                report["rounds"].append(
                    {"round": rnd, "stopped_reason": stale,
                     "candidate_sha_at_plan": plan_cand_sha})
                report["outcome"] = "PLAN_STALE"
                report["rounds_used"] = rounds_used
                report["note"] = ("应用动作前的身份复查未通过：不把旧计划写进候选，"
                                  "不提交、不触碰有效版本。")
                save_json_atomic(report, d / "repair_report.json")
                return report
            # ---- 应用动作**前**再复查一次（§5.2）：候选哈希 / 来源身份 / 候选 actual ----
            # 计划生成时查过一次还不够：这里在真正落盘前再查，防止"计划后、应用前"的漂移。
            sha_at_apply = sha256_file(pptx)
            pre_ok, pre_why = repair_mod.verify_before_apply(
                plan, pptx, src_cc=src_cc, cand_sha_at_plan=plan_cand_sha)
            if not pre_ok:
                report["rounds"].append(
                    {"round": rnd, "stopped_reason": pre_why,
                     "candidate_sha_at_apply": sha_at_apply})
                report["outcome"] = "PLAN_STALE"
                report["rounds_used"] = rounds_used
                report["note"] = ("应用动作前的复查未通过（候选/来源身份漂移）："
                                  "不把旧计划写进候选，不提交、不触碰有效版本。")
                save_json_atomic(report, d / "repair_report.json")
                return report

            # ---- 修前真实字节隔离保留（每轮一份；渲染移入轮次目录，不删除）----
            pre_kept = self._repair_pre_snapshot(d, cand, pptx, rdir / "pre")
            applied, ok = repair_mod.apply_repairs(plan, pptx, independent)
            reread = repair_mod.reread_verify(independent, applied) if ok else []
            rec = {"round": rnd,
                   "independent_candidate": str(independent.relative_to(d)),
                   "independent_candidate_sha256": (sha256_file(independent) if ok else None),
                   "pre_snapshot": pre_kept,
                   "applied": applied, "apply_ok": ok, "reread": reread,
                   "reread_all_ok": bool(reread) and all(x.get("reread_ok") for x in reread),
                   "refused_count": len(plan.refused)}
            if not ok or not rec["reread_all_ok"]:
                # 不采纳 → 把隔离保留的渲染放回原位，候选与证据恢复修前状态
                rec["pre_render_restored"] = self._repair_restore_pre(cand, rdir / "pre")
                rec["stopped_reason"] = ("独立候选应用失败" if not ok
                                         else "保存后独立重读不符；不采纳、不提交")
                report["rounds"].append(rec)
                report["outcome"] = "REREAD_FAILED"
                report["rounds_used"] = rounds_used      # 早退也必须留下轮次账
                report["note"] = ("保存后独立重读未逐条落地；按铁律不采纳该候选、"
                                  "不提交、不触碰有效版本。修前候选/台账/渲染已原样保留。")
                save_json_atomic(report, d / "repair_report.json")
                return report

            # ---- 写入**之后**、采纳之前再查一次取消：取消发生在写入过程中也要停下 ----
            job_after_write = load_json(d / "job.json")
            if self._cancel_flag(job_id).exists() \
                    or job_after_write.get("attempt_id") in (
                        job_after_write.get("cancelled_attempts") or []) \
                    or job_after_write.get("verdict") == "CANCELLED":
                rec["pre_render_restored"] = self._repair_restore_pre(cand, rdir / "pre")
                rec["stopped_reason"] = ("写入过程中该 attempt 被取消：独立临时候选不采纳，"
                                         "不提交、不导出、不自动重试")
                report["rounds"].append(rec)
                report["outcome"] = "CANCELLED_DURING_WRITE"
                report["rounds_used"] = rounds_used
                report["note"] = ("取消发生在修复写入过程中：独立临时候选与失败证据按隔离保留，"
                                  "候选/台账/渲染恢复修前状态；旧有效版本与原始输入未被触碰。")
                save_json_atomic(report, d / "repair_report.json")
                return report

            # ---- 采纳为当前候选，重新走验收入口（全部必需检查重执行 + 渲染证据重绑定）----
            self._stage(job_id, "repair", "DONE",
                        {"round": rnd, "actions": len(applied),
                         "independent": str(independent)})
            shutil.copyfile(independent, pptx)          # 采纳：候选目录内替换
            # 修前渲染已移入 repair/round_N/pre/render 保留；此处不 rmtree，
            # 由 validate 重新渲染 → 渲染证据与修后候选重新绑定。
            v2 = self.validate(job_id)                  # 全部必需检查重新执行

            # ---- 溯源：原 finding → 动作/拒修原因 → 修后是否仍阻断 ----
            after_sigs = {self._finding_sig(x)
                          for x in load_json(d / "findings.json").get("findings", [])}
            trace = []
            for i, f in enumerate(round_findings):
                act = next((a for a in plan.actions if a.finding_index == i), None)
                rf = next((r for r in plan.refused if r.get("finding_index") == i), None)
                trace.append({
                    "finding_index": i,
                    "code": f.get("code"), "reason_code": f.get("reason_code"),
                    "severity": f.get("severity"),
                    "disposition": ("action" if act else ("refused" if rf else "unmapped")),
                    "action_kind": (act.kind if act else None),
                    "refuse_reason": (rf.get("why") if rf else None),
                    "still_blocking_after": self._finding_sig(f) in after_sigs,
                })
            rec["finding_trace"] = trace
            rec["finding_trace_complete"] = all(t["disposition"] != "unmapped"
                                                for t in trace)
            rec["refused_records_kept"] = len(plan.refused)
            rec["partial_refusal_note"] = (
                "部分拒修不等于失败：本轮 verdict 只由重新验收给出；"
                "拒修记录一律保留，不删除拒修记录换取通过。")
            rec["verdict_after"] = v2["verdict"]
            rec["blocking_after"] = v2["blocking_findings"]
            rec["required_checks_after"] = v2["required_checks"]
            rec["checks_after"] = v2["checks"]
            rec["render_code_after"] = v2["render"].get("code")
            rec["render_pdf_pages_after"] = v2["render"].get("pdf_page_count")
            rec["render_png_count_after"] = v2["render"].get("png_count")
            rec["candidate_pptx_after_sha256"] = v2.get("candidate_pptx_sha256")
            report["rounds"].append(rec)

            if v2["verdict"] != "FAILED" and v2["blocking_findings"] == 0:
                report["outcome"] = "REPAIRED_AND_REVALIDATED"
                report["note"] = (f"第 {rnd} 轮修复后重新验收：{v2['verdict']}，"
                                  f"阻断 {v2['blocking_findings']} 条；"
                                  "渲染证据已随重新验收重新绑定。")
                report["rounds_used"] = rounds_used
                save_json_atomic(report, d / "repair_report.json")
                return report

            # ---- 仍有阻断：若还有轮次则按新 finding 重新计划 ----
            new_findings = load_json(d / "findings.json").get("findings", [])
            round_findings = new_findings
            plan = repair_mod.plan_repairs(new_findings, src, pptx, src_cc=src_cc,
                                           round_no=rnd + 1, max_rounds=max_rounds)
            plan_cand_sha = sha256_file(pptx)  # 新一轮计划也要记录计划期哈希
            report[f"plan_round{rnd + 1}"] = plan.to_dict()
            if not plan.actions:
                report["outcome"] = "REPAIR_EXHAUSTED"
                report["note"] = (f"第 {rnd} 轮后仍有阻断，但白名单内无更多可用修复"
                                  f"（拒修 {len(plan.refused)} 条）；停止，不超两轮。")
                report["rounds_used"] = rounds_used
                save_json_atomic(report, d / "repair_report.json")
                return report

        report["outcome"] = "ROUNDS_EXCEEDED"
        report["forbidden_code"] = "REPAIR.FORBIDDEN_CONTENT_CHANGE"
        report["note"] = (f"超过允许的 {max_rounds} 轮仍未收敛；"
                          "按规格记 REPAIR.FORBIDDEN_CONTENT_CHANGE 并停止。")
        report["rounds_used"] = rounds_used
        save_json_atomic(report, d / "repair_report.json")
        return report

    def promote_to_release(self, job_id):
        """正式放行：任一必需检查未 EXECUTED、有阻断项、或证据不绑定当前候选，都必须被拦截。

        这是 R10 的可执行行为：完整发布 profile 必需检查未实现时，正式 READY 必须被阻止。
        """
        d = self._job_dir(job_id)
        v = load_json(d / "validation.json")
        problems = []
        # W01：以**预期必需集合**为准做覆盖核验，而不是"报告里有的都 PASS 就算过"。
        req = v.get("required_checks") or []
        if not req:
            problems.append({"code": "GATE.CHECK_NOT_IMPLEMENTED", "check": "(required_checks)",
                             "detail": "预期必需检查集合为空；不得以此放行。"})
        for name in req:
            if name not in v["checks"]:
                problems.append({"code": "GATE.CHECK_NOT_IMPLEMENTED", "check": name,
                                 "status": "MISSING_FROM_REPORT",
                                 "detail": f"预期必需检查 {name} 未出现在报告中"
                                           "（报告漏项不得靠其余项全绿通过）"})
                continue
            if v["checks"][name] != "EXECUTED":
                problems.append({"code": "GATE.CHECK_NOT_IMPLEMENTED", "check": name,
                                 "status": v["checks"][name],
                                 "detail": f"必需检查 {name} 未实际执行通过"
                                           f"（{v['checks'][name]}）"})
        for name, status in v["checks"].items():
            if status != "EXECUTED":
                problems.append({"code": "GATE.CHECK_NOT_IMPLEMENTED", "check": name,
                                 "status": status,
                                 "detail": f"已报告检查 {name} 未实际执行通过（{status}）"})
        if v["not_implemented_required_checks"]:
            problems.append({"code": "GATE.CHECK_NOT_IMPLEMENTED",
                             "checks": v["not_implemented_required_checks"],
                             "detail": "存在未实现的必需检查"})
        if v.get("missing_from_report"):
            problems.append({"code": "GATE.CHECK_NOT_IMPLEMENTED",
                             "checks": v["missing_from_report"],
                             "detail": "报告缺少预期必需检查条目"})
        for detail in self._layout_gate_evidence_problems(d, v):
            problems.append({"code": "GATE.LAYOUT_HARD_GATE_BLOCKED",
                             "check": "layout.hard_gate", "detail": detail})
        if v["blocking_findings"]:
            problems.append({"code": "GATE.BLOCKING_FINDINGS",
                             "count": v["blocking_findings"],
                             "detail": "存在未解决的阻断性问题"})
        # 证据绑定：候选哈希必须与 validation.json 记录一致
        if Path(v["candidate_pptx"]).exists():
            actual = sha256_file(v["candidate_pptx"])
            if actual != v["candidate_pptx_sha256"]:
                problems.append({"code": "EVIDENCE.STALE", "check": "candidate_pptx_sha256",
                                 "expected": v["candidate_pptx_sha256"], "actual": actual,
                                 "detail": "候选在验收后被修改；旧证据不能给新候选签发 READY"})
        else:
            problems.append({"code": "GATE.ARTIFACT_MISSING", "detail": "候选 PPTX 不存在"})
        if problems:
            raise GateError("GATE.RELEASE_BLOCKED",
                            json.dumps(problems, ensure_ascii=False))
        return {"released": True, "job_id": job_id, "run_id": v["run_id"],
                "released_at": now_iso()}

    # ---------------------------------------------------------------- D10-A：导出资格
    def _export_verify_evidence(self, job, v):
        """核对 validation.json 与当前可信状态一致，并取出**已验证的预期哈希**。

        预期哈希只来自此前有效验收记录（validation.json / render 证据），
        绝不把当前候选重新计算的哈希当作预期值。身份缺失或不一致 → 拒绝导出。
        必需检查集合用"profile ∪ 原始普查 ∪ 输入台账"重新推导（可信集合），
        不从报告实际列举的项目倒推。
        """
        problems = []
        problems.extend(self._layout_gate_evidence_problems(
            self._job_dir(job["job_id"]), v))
        if v.get("run_id") != job["run_id"]:
            problems.append(f"验收记录 run_id {v.get('run_id')} 与当前 run {job['run_id']} 不一致")
        if v.get("support_profile") != job.get("support_profile"):
            problems.append("验收记录 support_profile 与当前任务不一致")
        if v.get("attempt_id") != job["attempt_id"]:
            problems.append("验收记录 attempt 与当前 attempt 不一致")
        if v.get("input_sha256") != job.get("input_hash"):
            problems.append("验收记录 input_sha256 与冻结任务输入不一致")
        if v.get("contract_version") != CONTRACT_VERSION:
            problems.append(f"验收记录契约版本 {v.get('contract_version')} 与当前 "
                            f"{CONTRACT_VERSION} 不一致（规则可能已变，需重新验收）")
        # D10-B：主题身份绑定。任务冻结了摘要时，验收记录必须携带同一摘要；
        # 否则视为"用别的主题/旧配置的验收结果"给当前候选背书 → 拒绝。
        j_digest = job.get("theme_config_digest")
        v_theme = v.get("theme") or {}
        if j_digest and (not v_theme.get("config_digest")):
            problems.append("验收记录缺少主题配置摘要；无法确认该报告对应当前主题配置")
        elif j_digest and v_theme.get("config_digest") != j_digest:
            problems.append(
                f"验收记录主题配置摘要 {str(v_theme.get('config_digest'))[:16]}… 与当前任务 "
                f"{j_digest[:16]}… 不一致（不得跨主题复用验收结果）")
        elif v_theme.get("theme_id") and v_theme.get("theme_id") != job.get("theme"):
            problems.append(f"验收记录 theme_id {v_theme.get('theme_id')} 与当前任务 "
                            f"{job.get('theme')} 不一致")
        # R5 材料生成：来源侧覆盖核对必须是**已执行且无阻断**的必需检查，
        # 否则不得导出。DEVELOPMENT_CANDIDATE 不是漏内容的豁免。
        # 判定用**建任务时冻结的材料身份**，不是"台账文件此刻在不在"——
        # 台账被删/被换/未冻结时仍必须拒绝导出。
        m_intent = job.get("material_intent") or load_material_intent(self._job_dir(job["job_id"]))
        if m_intent:
            _, _, m_status, m_reason = load_material_source(self._job_dir(job["job_id"]), m_intent)
            req_v = set(v.get("required_checks") or [])
            if m_status != "OK":
                problems.append(f"材料任务的来源核对不可执行（{m_status}）：{m_reason}；"
                                "不得导出候选，也不得退回普通 PPTX 路径。")
            if "source.fact_coverage" not in req_v:
                problems.append("材料生成任务的验收记录缺少必需检查 source.fact_coverage；"
                                "来源侧核对未执行，不得导出。")
            elif (v.get("checks") or {}).get("source.fact_coverage") != "EXECUTED":
                problems.append("来源侧事实覆盖核对未通过（source.fact_coverage != EXECUTED）；"
                                "存在确定遗漏时不得导出候选。")

        src = Path(job["input_path"])
        if not src.exists():
            raise GateError("INPUT.SOURCE_BASELINE_MISSING",
                            "导出时来源基线不可用；不得用候选反推原稿。")
        if sha256_file(src) != job["input_hash"]:
            raise GateError("EVIDENCE.STALE",
                            "输入文件哈希与冻结任务不符；不得用替换后的输入导出。")
        if v.get("blocking_findings"):
            problems.append(f"验收记录仍有 {v.get('blocking_findings')} 条阻断")
        from . import census as census_mod
        census = census_mod.census_package(src)
        census["content_census"] = census_mod.census_xml_content(src)
        in_doc = extract_any(src)
        exp = expected_required_checks(job.get("support_profile"), census, in_doc)
        missing = [c for c in exp["required"] if c not in (v.get("required_checks") or [])]
        if missing:
            problems.append(f"当前 profile 的必需检查未被验收记录覆盖：{missing}")
        not_exec = [c for c in (v.get("required_checks") or [])
                    if (v.get("checks") or {}).get(c) != "EXECUTED"]
        if not_exec:
            problems.append(f"必需检查未全部实际执行通过：{not_exec}")
        rr = v.get("render") or {}
        expected = {"pptx": v.get("candidate_pptx_sha256"),
                    "pdf": rr.get("pdf_sha256"),
                    "png": {b.get("page_no"): b.get("png_sha256")
                            for b in rr.get("png_bindings", [])}}
        if not expected["pptx"]:
            problems.append("验收记录缺少 candidate_pptx_sha256（无预期身份可核对）")
        if rr.get("code") == "RENDER.OK" and not expected["pdf"]:
            problems.append("验收记录缺少 render.pdf_sha256（无预期身份可核对）")
        if problems:
            raise GateError("EVIDENCE.STALE", "；".join(problems))
        return expected

    def _export_stage_copy(self, job, v, expected, stage):
        """把候选与渲染产物复制到独立暂存目录，并逐项对照**已验证的预期哈希**。

        返回 (render_artifacts, staged_relpaths)。任何一项哈希不符 → GateError；
        暂存目录不进入成功交付入口，由调用方处置。
        """
        stem = Path(job["input_path"]).stem
        rr = v.get("render") or {}
        staged = {}
        src_pptx = Path(v["candidate_pptx"])
        if not src_pptx.exists():
            raise GateError("GATE.ARTIFACT_MISSING", "候选 PPTX 不存在；先运行并验收。")
        dst_pptx = stage / (stem + ".wengao.pptx")
        shutil.copyfile(src_pptx, dst_pptx)
        staged["pptx"] = {"rel": dst_pptx.name, "expected": expected["pptx"]}

        render_artifacts = {"pdf": None, "png": [], "pdf_pages": None}
        if rr.get("pdf") and Path(rr["pdf"]).exists():
            dst_pdf = stage / (stem + ".wengao.pdf")
            shutil.copyfile(rr["pdf"], dst_pdf)
            staged["pdf"] = {"rel": dst_pdf.name, "expected": expected["pdf"]}
        png_dir = rr.get("png_dir")
        if png_dir and Path(png_dir).exists():
            pd_out = stage / "pages"
            pd_out.mkdir(parents=True, exist_ok=True)
            # 按验收记录的绑定逐页复制；文件名以 png_dir 内实际文件为准，
            # 预期哈希只认 png_bindings 里登记的值（page_no → png_sha256）。
            for src_png in sorted(Path(png_dir).glob("*.png")):
                shutil.copyfile(src_png, pd_out / src_png.name)
                page_no = None
                for b in rr.get("png_bindings", []):
                    if Path(b.get("file", "")).name == src_png.name:
                        page_no = b.get("page_no")
                        break
                staged[f"png:{src_png.name}"] = {
                    "rel": f"pages/{src_png.name}", "page_no": page_no,
                    "expected": (expected["png"] or {}).get(page_no)}
        # 逐项核对暂存副本与已验证预期哈希（不是"复制前后一致"）
        mismatches = []
        for key, item in staged.items():
            actual = sha256_file(stage / item["rel"])
            if item["expected"] is None:
                mismatches.append({"item": key, "problem": "缺少预期哈希",
                                   "actual": actual})
            elif actual != item["expected"]:
                mismatches.append({"item": key, "expected": item["expected"],
                                   "actual": actual})
        if mismatches:
            raise GateError("EVIDENCE.STALE",
                            "暂存副本与验收记录的预期哈希不一致："
                            + json.dumps(mismatches, ensure_ascii=False))
        if staged.get("pdf"):
            render_artifacts["pdf"] = staged["pdf"]["rel"]
            render_artifacts["pdf_sha256"] = staged["pdf"]["expected"]
        render_artifacts["png"] = sorted(item["rel"] for k, item in staged.items()
                                         if k.startswith("png:"))
        render_artifacts["pdf_pages"] = rr.get("pdf_page_count")
        render_artifacts["pptx_slides"] = rr.get("pptx_slides")
        render_artifacts["counts_consistent"] = rr.get("counts_consistent")
        render_artifacts["renderer"] = rr.get("soffice_version")
        render_artifacts["png_renderer"] = rr.get("png_renderer")
        render_artifacts["png_bindings"] = rr.get("png_bindings", [])
        return render_artifacts, staged

    def _export_event(self, job_id, event, **kw):
        """导出事件的 append-only 记录（提交/中止及顺序证据），不参与成功判定。"""
        d = self._job_dir(job_id)
        rec = {"at": now_iso(), "job_id": job_id, "event": event, **kw}
        with open(d / "export_events.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def export(self, job_id, outdir=None):
        """导出开发候选：只绑定当前输入/输出/规则/执行身份，不冒充正式交付。

        D10-A（V9 准入复核登记缺陷的修复）。完整链路：
          1) 入口门禁：取消 / 判定 / attempt 身份 / 验收记录存在（见下）。
          2) 可信证据核对（_export_verify_evidence）：run/profile/attempt/输入/
             契约版本绑定；必需检查集合从可信来源重新推导；预期哈希取自
             此前有效验收记录（不把当前候选哈希当作预期）。
          3) 独立暂存复制 + 逐项哈希核对（_export_stage_copy）：PPTX、PDF、
             全部预期 PNG 逐一对照已验证哈希。
          4) 发布前复查：在身份保护下重新读取取消/attempt/证据状态。
          5) 受保护提交：暂存 → 原子换入（备份→改名→清理）；失败回滚。
        本方法不持 .run.lock：长时间复制不会阻塞取消登记；取消随时生效，
        由第 4 步的身份复查拦截"复制期间被取消"的导出。
        """
        d = self._job_dir(job_id)
        job = load_json(d / "job.json")
        vpath = d / "validation.json"
        if not vpath.exists():
            raise GateError("GATE.ARTIFACT_MISSING",
                            "尚无验收记录（validation.json 不存在）；先运行并验收后才能导出。")
        v = load_json(vpath)
        if job.get("verdict") == "CANCELLED" \
                or job["attempt_id"] in (job.get("cancelled_attempts") or []) \
                or (d / ".cancelled").exists():
            raise GateError("GATE.LATE_RESULT_REJECTED",
                            f"任务 {job_id} 的当前 attempt 已被取消；"
                            "取消后的结果不得导出为候选。")
        if v["verdict"] == "FAILED":
            raise GateError("GATE.BLOCKING_FINDINGS",
                            f"验收未通过（阻断 {v.get('blocking_findings')} 条），不能导出为候选。")
        if v["verdict"] not in ("DEVELOPMENT_CANDIDATE", "READY"):
            raise GateError("GATE.RELEASE_BLOCKED",
                            f"当前判定为 {v['verdict']}（存在待裁定项），不能导出为候选。")
        if v.get("attempt_id") != job["attempt_id"]:
            raise GateError("EVIDENCE.STALE",
                            f"验收记录绑定的是 attempt {v.get('attempt_id')}，"
                            f"当前 attempt 为 {job['attempt_id']}；"
                            "旧证据不能导出为新 attempt 的候选，请重新运行并验收。")
        # 统一交付资格：当前 attempt 的验收 + 候选哈希 + 判定 + 事故台账。
        # 旧逻辑只看 job.verdict 与 attempt_id，事故里"坏结果仍可下载"就是从这里漏的。
        state = incident_mod.delivery_state(d, self.root)
        if not state["allowed"]:
            raise GateError(state["code"] or "GATE.RELEASE_BLOCKED",
                            "；".join(x.get("detail", "") for x in state["reasons"]))
        cand = d / "candidate"
        # 导出目录按 support_profile 分流：把 P2 产物写进 p1-candidates 会让
        # "当前候选"的归属说不清，也会让按目录核对的证据串到别的阶段。
        if outdir:
            out = Path(outdir)
        else:
            prof = (job.get("support_profile") or SUPPORT_PROFILE)
            slot = "p2-candidates" if str(prof).startswith("P2") else "p1-candidates"
            # D10-B：导出目录名携带主题身份，避免同 run 不同主题互相覆盖/混淆
            tid = job.get("theme") or "theme-unknown"
            out = self.root / "reports" / slot / f"{job['run_id']}--{tid}"
        # ---- 2) 可信证据核对：预期哈希来自此前有效验收记录 ----
        expected = self._export_verify_evidence(job, v)
        # ---- 3) 独立暂存复制 + 逐项哈希核对（不触碰既有交付目录）----
        out.parent.mkdir(parents=True, exist_ok=True)
        stage = out.parent / (".export-stage-" + uuid.uuid4().hex[:8])
        stage.mkdir(parents=True)
        try:
            render_artifacts, staged = self._export_stage_copy(job, v, expected, stage)
        except GateError:
            shutil.rmtree(stage, ignore_errors=True)
            self._export_event(job_id, "ABORTED_STAGE_VERIFY", outdir=str(out),
                               detail="暂存副本与预期哈希不符或身份缺失")
            raise
        # ---- 4) 发布前复查：取消/attempt/证据在复制期间未变化 ----
        job_now = load_json(d / "job.json")
        v_now = load_json(vpath)
        stale = None
        if job_now.get("verdict") == "CANCELLED" \
                or job_now["attempt_id"] in (job_now.get("cancelled_attempts") or []) \
                or (d / ".cancelled").exists():
            stale = "任务在暂存复制期间被取消"
        elif job_now["attempt_id"] != job["attempt_id"]:
            stale = "任务 attempt 在暂存复制期间已变更"
        elif (v_now.get("verdict"), v_now.get("attempt_id"),
              v_now.get("candidate_pptx_sha256"), v_now.get("run_id")) != \
                (v["verdict"], v["attempt_id"], v["candidate_pptx_sha256"], v["run_id"]):
            stale = "验收证据在暂存复制期间已变化"
        if stale:
            shutil.rmtree(stage, ignore_errors=True)
            self._export_event(job_id, "ABORTED_PRE_PUBLISH", outdir=str(out),
                               detail=stale, staged_dir=str(stage))
            raise GateError("GATE.LATE_RESULT_REJECTED",
                            f"{stale}；暂存产物未发布，不产生成功交付。")
        # ---- 5) 受保护提交：暂存 → 原子换入（备份→改名→清理），失败回滚 ----
        bak = None
        if out.exists():
            bak = out.parent / (out.name + ".bak-" + uuid.uuid4().hex[:8])
            os.replace(out, bak)
        try:
            os.replace(stage, out)
        except Exception as e:  # noqa: BLE001
            if bak is not None and bak.exists() and not out.exists():
                os.replace(bak, out)
            shutil.rmtree(stage, ignore_errors=True)
            self._export_event(job_id, "ABORTED_COMMIT", outdir=str(out),
                               detail=f"{type(e).__name__}: {e}")
            raise GateError("GATE.ARTIFACT_WRITE_FAILED",
                            f"导出提交失败（{type(e).__name__}: {e}）；已回滚。")
        if bak is not None and bak.exists():
            shutil.rmtree(bak, ignore_errors=True)
        stem = Path(job["input_path"]).stem
        manifest = {
            "schema_version": "1.0.0", "label": INTERNAL_CANDIDATE_LABEL,
            "run_id": job["run_id"], "attempt_id": job["attempt_id"],
            "job_id": job_id, "contract_version": CONTRACT_VERSION,
            "support_profile": job["support_profile"], "exported_at": now_iso(),
            # D10-B：交付身份携带主题（id + 版本 + 有效配置摘要）
            "theme": {
                "theme_id": job.get("theme"),
                "theme_name": job.get("theme_name"),
                "theme_version": job.get("theme_version"),
                "registry_version": job.get("theme_registry_version"),
                "config_digest": job.get("theme_config_digest"),
                "frozen_at_job": bool(job.get("theme_config_digest")),
                "legacy_no_digest": not bool(job.get("theme_config_digest")),
            },
            "input": {"path": job["input_path"], "sha256": job["input_hash"]},
            "output": {"pptx": str(out / (stem + ".wengao.pptx")),
                       "pptx_sha256": expected["pptx"]},
            "validation": {"path": str(vpath),
                           "sha256": sha256_file(vpath),
                           "verdict": v["verdict"], "release_ready": v["release_ready"]},
            "render": v.get("render", {}),
            "render_artifacts": render_artifacts,
            "export_gate": {
                "expected_hashes_from": "validation.json（此前有效验收记录；非导出时重算）",
                "verified": staged,
                "pre_publish_recheck": "PASSED",
                "commit_strategy": "staging → atomic swap（bak → rename → cleanup）",
                "cancelled_at_gate": job.get("cancelled_at"),
                "note": "export 不持 .run.lock；取消可随时登记，由发布前身份复查拦截。",
            },
            "delivery_eligibility": {
                "allowed": True, "code": "OK",
                "checked_at": now_iso(),
                "rule": "当前 attempt 验收 + 候选哈希一致 + 判定允许 + 未被事故失效",
                "incidents_consulted": state.get("incidents") or [],
            },
            "glyph_quality": v.get("glyph_quality", {}).get("verdict"),
            "theme_application": v.get("theme_application"),
            "release_statement": "开发候选，非完整产品 READY；不含 P2—P5 能力与发布门禁。",
        }
        save_json_atomic(manifest, out / "release_manifest.json")
        self._export_event(job_id, "COMMITTED", outdir=str(out),
                           attempt_id=job["attempt_id"],
                           pptx_sha256=expected["pptx"],
                           label=INTERNAL_CANDIDATE_LABEL)
        self._set_verdict(job_id, v["verdict"], {"export_path": str(out)})
        return manifest
