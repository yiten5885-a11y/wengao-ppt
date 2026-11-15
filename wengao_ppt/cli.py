"""唯一命令行入口。

    python3 -m wengao_ppt.cli precheck --input X [--mode beautify]
    python3 -m wengao_ppt.cli run      --job-id J [--timeout 900]
    python3 -m wengao_ppt.cli validate --job-id J
    python3 -m wengao_ppt.cli export   --job-id J [--outdir DIR]

Skill 入口不得绕过本运行器自行签发 READY。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

from . import __version__
from .core import load_json, save_json_atomic
from .runner import (Runner, GateError, SUPPORT_PROFILE, INTERNAL_CANDIDATE_LABEL,
                     P1_REQUIRED_CHECKS, MATERIAL_LEDGER_FILE, MATERIAL_PLAN_FILE)


def _sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _now_iso():
    return datetime.now().astimezone().isoformat()


def _emit(title, obj):
    """输出一条命令结果。

    标题行走 **stderr**：stdout 必须是一个可直接 `json.loads` 的 JSON。
    旧行为把 `===== xxx =====` 打在 stdout 前面，脚本按"stdout 即 JSON"解析会
    直接失败（auto30 里 status/edit-list 都受影响），只能靠 last_json 兜底。
    人看的标题不占 stdout，机器看的 JSON 不被污染。
    """
    print(f"===== {title} =====", file=sys.stderr)
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _cli_run(args, project_root=None, timeout=1800):
    """跑一次真实 CLI 子进程，返回 (rc, stdout, stderr)。"""
    import os
    import subprocess
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run(
        [sys.executable, "-m", "wengao_ppt.cli", "--project-root",
         str(project_root or Path(__file__).resolve().parents[1]), *args],
        capture_output=True, text=True,
        cwd=str(Path(__file__).resolve().parents[1]), env=env, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def cmd_from_material(a):
    """由**原始材料**生成 PPTX（与已有的 PPTX 美化路径明确区分）。

    复用 samples/r5_material_to_ppt.py 的已验证链路：
      提取 → 冻结事实台账 → 本地模型分块计划 → 原生 PPTX
    材料输出**不进入**候选/发布流程：需显式 `--accept` 才会走
    precheck/run/validate/export，避免绕过验收门禁。
    """
    import importlib.util
    # 材料生成模块的定位集中在 `wengao_ppt.material_flow`：显式搜索顺序 +
    # 可覆盖（$WENGAO_MATERIAL_FLOW）+ 缺件时输出**实际尝试过的路径**。
    # 旧实现在这里手写两条候选，其中 `wengao_ppt/material_flow.py` 是从未
    # 发布过的死分支，缺件报错也不说明找过哪里（U8）。
    from . import material_flow
    search_extra = []
    if getattr(a, "project_root", None):
        search_extra.append(Path(a.project_root))
    try:
        mod_path = material_flow.resolve(search_extra)
    except material_flow.MaterialFlowMissing as exc:
        print(json.dumps({"gate": "GATE.MATERIAL_PATH_MISSING",
                          "detail": "材料生成模块缺失（samples/r5_material_to_ppt.py）；"
                                    "已尝试的路径见 searched。载荷若被裁剪掉 samples/，"
                                    "请重新生成安装载荷或设置 $WENGAO_MATERIAL_FLOW。",
                          "searched": exc.searched},
                         ensure_ascii=False, indent=2))
        return 4
    spec = importlib.util.spec_from_file_location("r5_material_to_ppt", mod_path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    src = Path(a.input)
    if not src.is_absolute():
        src = Path(a.project_root) / src
    if not src.is_file():
        print(json.dumps({"gate": "INPUT.NOT_FOUND",
                          "detail": f"材料不存在：{src}"}, ensure_ascii=False, indent=2))
        return 4
    suf = src.suffix.lower()
    if suf not in (".md", ".markdown", ".txt", ".text", ".docx"):
        print(json.dumps({"gate": "INPUT.UNSUPPORTED_MATERIAL_TYPE",
                          "detail": "材料生成仅支持 .md/.markdown/.txt/.docx；"
                                    "已有 PPTX 请用 precheck/run（美化路径）。"},
                         ensure_ascii=False, indent=2))
        return 4

    argv = ["--material", str(src), "--pages", str(a.pages),
            "--model", a.model, "--outdir", a.outdir]
    if a.single_pass:
        argv.append("--single-pass")
    if getattr(a, "cancel_file", None):
        argv += ["--cancel-file", str(a.cancel_file)]
    if getattr(a, "replay_plan", None):
        argv += ["--replay-plan", str(a.replay_plan)]
    sys.argv = ["r5_material_to_ppt"] + argv
    # 材料模块自己的进度行（[facts]/[plan]/[pages]…）留在 stdout 会让
    # "stdout 即 JSON" 再次失效；它们只是给人看的，统一转到 stderr。
    import contextlib
    with contextlib.redirect_stdout(sys.stderr):
        rc = m.main()

    run_record = Path(a.outdir) / "run_record.json"
    rec = json.loads(run_record.read_text(encoding="utf-8")) if run_record.is_file() else {}
    pptx = rec.get("pptx", {}).get("path")
    if pptx:
        # 运行记录里存的路径可能是：①绝对路径；②相对**程序根**（材料模块 rel()
        # 的口径，产物在程序根内时）；③相对**数据根/项目根**。三种布局都要能
        # 还原：依次尝试 项目根 → 程序根 → outdir 同名文件，取第一个真实存在的。
        # 旧实现只按 project_root 解析：隔离数据根位于程序根**内部**时
        # （回环候选 wb_loopback_data）会出现 wb_loopback_data/wb_loopback_data
        # 双重前缀 → accept 段 INPUT.NOT_FOUND（2026-10-20 冒烟实测复现）。
        _p = Path(pptx)
        if _p.is_absolute():
            pptx = str(_p)
        else:
            _prog_root = Path(mod_path).resolve().parents[1]
            _outdir = Path(a.outdir)
            _outdir = _outdir if _outdir.is_absolute() else Path(a.project_root) / _outdir
            _cands = [Path(a.project_root) / _p, _prog_root / _p,
                      _outdir / _p.name]
            pptx = str(next((c for c in _cands if c.is_file()), _cands[0]))
    # ---- 退出码必须反映真实结果：没产出可验收的 PPTX 就不是成功 ----
    # 旧实现无论如何都 return 0，调用方（工作台 spawn、脚本、CI）按退出码判断时
    # 会把"分页区间不满足/页数不一致/模块崩溃"当成成功。
    verdict = rec.get("verdict")
    # 材料链路里唯一的成功判定是 GENERATED_PENDING_VALIDATION；其余（PLAN_FAILED /
    # PAGE_RANGE_UNSATISFIED / PAGE_COUNT_MISMATCH / CANCELLED / PLAN_ASSET_INVALID）
    # 都不是成功。用"只认成功值"的口径，新增失败分支也不会漏。
    ok = (rc == 0 and bool(rec) and bool(pptx)
          and verdict == "GENERATED_PENDING_VALIDATION")
    if not ok:
        code = "GATE.MATERIAL_GENERATION_FAILED"
        model_error = rec.get("model_error") if isinstance(rec, dict) else None
        if isinstance(model_error, dict) and model_error.get("code") in {
                "MODEL.QUOTA_EXCEEDED", "MODEL.RATE_LIMITED",
                "MODEL.AUTH_FAILED", "MODEL.HTTP_ERROR",
                "MODEL.TRANSPORT_ERROR", "MODEL.REQUEST_TIMEOUT"}:
            code = model_error["code"]
        if verdict == "PAGE_COUNT_MISMATCH":
            code = "RENDER.PAGE_COUNT_MISMATCH"
        elif verdict == "CANCELLED":
            code = "GATE.CANCELLED"
        detail = (f"材料生成未产出可验收的 PPTX（模块返回码 {rc}，"
                  f"run_record 判定 {verdict or '缺失'}，pptx={pptx or '无'}）。")
        if isinstance(model_error, dict):
            detail += (f" 模型调用 {model_error.get('code') or 'MODEL.ERROR'}"
                       f"（HTTP {model_error.get('status_code') or '—'}）："
                       f"{model_error.get('detail') or '无诊断详情'}")
        if not rec:
            detail += f" run_record.json 不存在或无法解析：{run_record}"
        conflict = rec.get("page_range_conflict") or {}
        # 失败时 stdout 只输出**一个** JSON（错误信封），不再先打印一份成功样式的
        # 运行摘要再打印错误：两份 JSON 会让"取最后一个"这类解析方式继续踩坑。
        print(json.dumps({"error": {"code": code, "detail": detail},
                          "model_error": model_error,
                          "job_created": False,
                          "material": {
                              "input": str(src),
                              "input_kind": rec.get("extraction", {}).get("input_kind"),
                              "facts_frozen": rec.get("fact_ledger", {}).get("count"),
                              "plan_mode": rec.get("plan", {}).get("mode"),
                              "plan_slides": rec.get("plan", {}).get("slides"),
                              "generated_pptx": pptx,
                          },
                          "material_verdict": verdict,
                          "page_range_conflict": conflict or None,
                          "artifacts": {"run_record": str(run_record),
                                        "outdir": str(a.outdir)}},
                         ensure_ascii=False, indent=2))
        print(f"[from-material] {code}: {detail}", file=sys.stderr)
        return 4
    _emit("from_material", {
        "input": str(src), "input_kind": rec.get("extraction", {}).get("input_kind"),
        "facts_frozen": rec.get("fact_ledger", {}).get("count"),
        "plan_mode": rec.get("plan", {}).get("mode"),
        "plan_chunks": rec.get("plan", {}).get("chunk_count"),
        "plan_slides": rec.get("plan", {}).get("slides"),
        "generated_pptx": pptx,
        "verdict": rec.get("verdict"),
        "accepted_into_pipeline": False,
        "next": ("材料生成**不会自动进入**候选/发布；如需验收请对该 PPTX 执行 "
                 "precheck → run → validate → publish → export"),
    })
    if a.accept and pptx:
        # 材料身份在**建任务时**冻结：来源材料 + 冻结台账 + 内容计划的哈希一起写进
        # 任务身份。此后台账被删/被换/未冻结都仍然要求来源核对并拒绝导出，
        # 不会退回"普通 PPTX 自基线路径"。
        src_dir = Path(a.outdir)
        src_dir = src_dir if src_dir.is_absolute() else Path(a.project_root) / src_dir
        ledger_p = src_dir / "fact_ledger.json"
        plan_p = src_dir / "content_plan.json"
        a2 = argparse.Namespace(project_root=a.project_root, input=pptx,
                                 mode="beautify",
                                 theme=getattr(a, "theme", "wengao-business"),
                                 support_profile=SUPPORT_PROFILE,
                                 material_source=str(src),
                                 material_ledger=str(ledger_p),
                                 material_plan=str(plan_p))
        rc2 = cmd_precheck(a2)
        # 台账/计划已在 cmd_precheck 建任务时快照进任务目录；这里只回显实际命中的任务，
        # 不再按 mtime 猜目录并二次复制（那会误命中并发任务、也无法修正内容）。
        try:
            for d in sorted((Path(a.project_root) / "jobs").glob("job-*"),
                            key=lambda q: q.stat().st_mtime, reverse=True):
                jj = d / "job.json"
                if not jj.is_file():
                    continue
                jd_json = json.loads(jj.read_text(encoding="utf-8"))
                if (jd_json.get("input_path") == str(pptx)
                        and jd_json.get("material_intent", {}).get("material")):
                    _emit("material_source_linked",
                          {"job_id": d.name,
                           "material_intent": "jobs/<job>/material_intent.json",
                           "fact_ledger": "jobs/<job>/material_fact_ledger.json",
                           "content_plan": "jobs/<job>/material_content_plan.json",
                           "required_check": "source.fact_coverage（导出前必须 EXECUTED）"})
                    break
        except Exception:  # noqa: BLE001
            pass
        return rc2
    return 0


def cmd_precheck(a):
    """预检入口：损坏/非 PPTX 输入在此拒绝（GateError → 退出码 4），不建任务。"""
    r = Runner(a.project_root)
    intent = None
    src = getattr(a, "material_source", None)
    led = getattr(a, "material_ledger", None)
    pln = getattr(a, "material_plan", None)
    if src or led or pln:
        # 材料身份在**建任务时**冻结：记录来源材料、台账与计划的哈希。
        # 此后台账缺失/被删/被换都不能让它退回普通 PPTX 自基线路径。
        if not (src and led and pln):
            print(json.dumps({"gate": "GATE.MATERIAL_INTENT_INCOMPLETE",
                              "detail": "材料身份必须同时给出 --material-source / "
                                        "--material-ledger / --material-plan。"},
                             ensure_ascii=False, indent=2))
            return 4
        intent = {
            "schema_version": "1.0.0",
            "material": True,
            "source_material": str(src),
            "source_material_sha256": _sha256(src),
            "ledger_sha256": _sha256(led),
            "plan_sha256": _sha256(pln),
            "frozen_before_model_call": True,
            "frozen_at": _now_iso(),
            "note": "身份在建任务时冻结；候选不得改写台账或计划。",
        }
    job_id, job = r.create_job(a.input, a.mode, theme=a.theme,
                               support_profile=a.support_profile,
                               material_intent=intent,
                               preserve_design=bool(getattr(a, "preserve_design", False)))
    if intent is not None:
        # 材料任务：把**冻结身份的台账与计划**快照进任务目录。
        # 只记录哈希而工作目录里没有文件，会让来源核对永远 NOT_EVALUABLE
        # （`--accept` 与直接 `precheck --material-*` 必须行为一致）。
        jd = r.jobs / job_id
        jd.mkdir(parents=True, exist_ok=True)
        shutil.copy2(led, jd / MATERIAL_LEDGER_FILE)
        shutil.copy2(pln, jd / MATERIAL_PLAN_FILE)
    info = r.precheck(job_id)
    _emit("precheck", {"job_id": job_id, "run_id": job["run_id"], "attempt_id": job["attempt_id"],
                       "verdict": "QUEUED",
                       "material_intent": bool(intent), **info})
    return 0


def cmd_run(a):
    r = Runner(a.project_root)
    info = r.run(a.job_id, timeout=a.timeout, hold_after_build=a.hold_after_build,
                allow_text_card_fallback=a.allow_text_card_fallback,
                skip_scene_photos=a.skip_scene_photos)
    _emit("run", info)
    return 0


def cmd_cancel(a):
    r = Runner(a.project_root)
    rec = r.cancel(a.job_id, grace=a.grace)
    _emit("cancel", rec)
    return 0


def cmd_submit(a):
    """真实的结果提交入口；迟到的旧结果必须被拒绝（退出码 3）。"""
    r = Runner(a.project_root)
    try:
        rec = r.submit_result(a.job_id, a.attempt_id, a.run_id, a.artifact)
    except GateError as e:
        # detail 可能是结构化 problems（身份不符）或一句人话（如产物缺失）。
        # 一律当 JSON 解析会让后者抛 JSONDecodeError，把"已拒绝"变成"崩了"。
        try:
            problems = json.loads(e.detail)
        except Exception:  # noqa: BLE001
            problems = [{"code": e.code, "detail": str(e.detail)}]
        print(json.dumps({"rejected": True, "code": e.code,
                          "problems": problems}, ensure_ascii=False, indent=2))
        return 3
    _emit("submit", rec)
    return 0


def cmd_publish(a):
    r = Runner(a.project_root)
    cur = r.publish_valid(a.job_id)
    _emit("publish_valid", cur)
    return 0


def _gate_cli(fn):
    def w(a):
        try:
            return fn(a)
        except GateError as e:
            print(json.dumps({"gate": e.code, "detail": e.detail}, ensure_ascii=False, indent=2))
            return 4
    return w


def cmd_validate(a):
    r = Runner(a.project_root)
    v = r.validate(a.job_id)
    _emit("validate", {"verdict": v["verdict"], "blocking_findings": v["blocking_findings"],
                       "total_findings": v["total_findings"],
                       "checks": v["checks"],
                       "not_implemented_required_checks": v["not_implemented_required_checks"],
                       "render_code": v["render"].get("code"),
                       "release_ready": v["release_ready"]})
    return 0 if v["verdict"] != "FAILED" else 2


def cmd_export(a):
    r = Runner(a.project_root)
    m = r.export(a.job_id, a.outdir)
    _emit("export", m)
    return 0


def cmd_incident_record(a):
    """登记事故：只绑定哈希与原因，不删除、不改写历史报告。"""
    from . import incident as incident_mod
    root = Path(getattr(a, "project_root", "."))
    arts = []
    for pth in (a.artifact or []):
        p = Path(pth)
        if not p.is_file():
            print(json.dumps({"error": {"code": "INCIDENT.ARTIFACT_MISSING",
                                        "detail": f"{pth} 不存在，未登记"}},
                             ensure_ascii=False, indent=2))
            return 4
        arts.append(incident_mod.sha256_file(p))
    rec = incident_mod.append_entry(root, {
        "incident_id": a.incident_id or None,
        "reason": a.reason, "job_ids": a.job_id or [], "artifact_sha256": arts,
        "artifacts": [str(x) for x in (a.artifact or [])],
        "scope": a.scope, "source_report": a.source_report, "note": a.note,
        "recorded_by": "cli incident-record",
    })
    _emit("incident-record", {"recorded": rec, "ledger": str(incident_mod.ledger_path(root)),
                              "note": "登记后，普通接纳/导出/固定下载会按本台账拒绝。"})
    return 0


def cmd_incident_list(a):
    from . import incident as incident_mod
    root = Path(getattr(a, "project_root", "."))
    d = incident_mod.load(root)
    out = {"ledger": d.get("path"), "incidents": d.get("incidents", [])}
    if d.get("corrupt"):
        out["corrupt"] = d["corrupt"]
    if a.job_id:
        out["delivery"] = incident_mod.delivery_state(
            Path(root) / "jobs" / a.job_id, root)
    _emit("incident-list", out)
    return 0


def cmd_status(a):
    d = Path(a.project_root) / "jobs" / a.job_id
    _emit("status", load_json(d / "job.json"))
    return 0


def cmd_cleanup(a):
    """A24：上传件/预览图的回收入口（只读清单；实际移动本轮未开放，暂缓清理）。"""
    from . import cleanup as cleanup_mod
    root = Path(getattr(a, "project_root", "."))
    res = cleanup_mod.plan(root, target=a.target, older_than_hours=a.older_than_hours,
                           job_id=a.job_id, limit=a.limit)
    if not res.get("ok"):
        _emit_cli_error(res.get("code") or "WB.CLEANUP_FAILED",
                        res.get("detail") or "清理不可用。", command="cleanup")
        return 4
    # 2026-10-18 复核：--apply 在普通产品中不开放；带了就如实告知未执行。
    res["execution_open"] = cleanup_mod.EXECUTION_OPEN
    res["applied"] = False
    if getattr(a, "apply", False) and not cleanup_mod.EXECUTION_OPEN:
        res["deferred"] = True
        res["note"] = cleanup_mod.EXECUTION_DEFERRED_NOTE
    _emit("cleanup", res)
    return 0


def _edit_common(p):
    p.add_argument("--job-id", required=True)
    p.add_argument("--page", type=int, required=True)
    p.add_argument("--shape", required=True)
    p.add_argument("--field", required=True,
                   choices=["text", "cell", "chart_value", "image"])
    p.add_argument("--new-value", required=True)
    p.add_argument("--row", type=int, help="field=cell 的行号")
    p.add_argument("--col", type=int, help="field=cell 的列号")
    p.add_argument("--series", type=int, help="field=chart_value 的系列序号")
    p.add_argument("--index", type=int, help="field=chart_value 的数据点序号")
    p.add_argument("--asset", help="field=image 的受控本地资产路径")


def cmd_edit(a):
    """授权局部修改：必须明确到页/对象/字段/新值，产出**独立候选**。"""
    from .local_edit import EditScopeError, apply_local_edit, list_versions

    d = Path(a.project_root) / "jobs" / a.job_id
    # 复用 runner 的取消语义：已请求取消的任务不得继续修改
    if (d / "cancel.request").is_file():
        _emit("edit", {"status": "CANCELLED",
                       "detail": "该任务已请求取消；取消后不推进有效版本。"})
        return 4
    edit = {"page": a.page, "shape": a.shape, "field": a.field,
            "new_value": a.new_value}
    for k in ("row", "col", "series", "index", "asset"):
        v = getattr(a, k, None)
        if v is not None:
            edit[k] = v
    try:
        rec = apply_local_edit(d, edit)
    except EditScopeError as e:
        _emit("edit", {"gate": e.code, "detail": e.detail})
        return 4
    rec["versions"] = list_versions(d)
    _emit("edit", rec)
    return 0


def cmd_edit_list(a):
    from .local_edit import list_versions
    d = Path(a.project_root) / "jobs" / a.job_id
    _emit("edit-list", {"job_id": a.job_id, "versions": list_versions(d)})
    return 0


def cmd_edit_restore(a):
    from .local_edit import EditScopeError, restore_version
    d = Path(a.project_root) / "jobs" / a.job_id
    try:
        rec = restore_version(d, a.version_id)
    except EditScopeError as e:
        _emit("edit-restore", {"gate": e.code, "detail": e.detail})
        return 4
    _emit("edit-restore", rec)
    return 0


def _emit_cli_error(code, detail, **extra):
    payload = {"error": {"code": code, "detail": detail}}
    payload.update(extra)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"[wengao] {code}: {detail}", file=sys.stderr)


def _precheck_job_ref(a):
    """所有"按任务操作"的子命令共用同一道前置判定。

    旧行为：`validate/export --job-id job-nope` 直接读 job.json，
    FileNotFoundError 栈打到 stderr、退出码 1、没有 JSON 错误码；
    而 `edit-list` 对同样的输入返回 rc=0 + 空列表。同一入口两种行为。
    这里统一成：目录/登记不可读 → 结构化 JSON + 退出码 4。
    """
    jid = getattr(a, "job_id", None)
    if not jid:
        return None
    jd = Path(getattr(a, "project_root", ".")) / "jobs" / jid
    if not jd.is_dir():
        _emit_cli_error("JOB.NOT_FOUND", f"任务不存在：{jid}", job_id=jid)
        return 4
    if load_json(jd / "job.json") is None:
        _emit_cli_error("BASELINE.UNREADABLE",
                        f"任务 {jid} 的登记文件 job.json 缺失或无法解析，"
                        "无法确定该任务的状态与基线。", job_id=jid)
        return 4
    return None


#: 这些子命令的 --job-id 必须指向一个真实存在的任务
JOB_SCOPED_CMDS = {"run", "validate", "export", "cancel", "status", "submit",
                   "publish", "edit", "edit-list", "edit-restore",
                   "job-status"}  # job-status 是 status 的别名（A29）


def _default_project_root() -> str:
    """数据根的默认值必须与 runner / workbench 一致。

    旧行为：只认 `--project-root`，默认值写死成包目录；调用方按
    `WENGAO_DATA_ROOT` 约定指定了隔离数据根，CLI 仍去读安装根下的 data，
    于是"工作台看的任务"和"CLI 改的任务"不是同一份。
    这里改成：显式参数优先 → WENGAO_DATA_ROOT → 包目录。
    """
    return os.environ.get("WENGAO_DATA_ROOT") or str(Path(__file__).resolve().parents[1])


class _CliParser(argparse.ArgumentParser):
    """参数错误也要走同一条"结构化失败"的路。

    argparse 默认把用法说明打到 stderr、退出码 2，stdout 什么都没有——
    脚本按"stdout 即 JSON"解析时会拿到空字符串，还得额外判断 rc=2 与 rc=4 的区别。
    这里把参数错误也变成 stdout 的结构化错误（退出码 4），用法说明仍留在 stderr。
    """

    def error(self, message):  # noqa: D102
        self.print_usage(sys.stderr)
        _emit_cli_error("CLI.BAD_ARGS", message,
                        command=getattr(self, "_cli_cmd", None))
        raise SystemExit(4)


def main(argv=None):
    ap = _CliParser(prog="wengao-ppt", description=__doc__)
    ap.add_argument("--project-root", default=None,
                    help="数据根（jobs/reports）。缺省按 WENGAO_DATA_ROOT，再退回包目录。")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True, parser_class=_CliParser)

    p = sub.add_parser("from-material")
    p.add_argument("--input", required=True,
                   help="原始材料：.md/.markdown/.txt/.docx（PPTX 美化请用 precheck）")
    p.add_argument("--pages", type=int, default=12)
    p.add_argument("--model", default="qwen3.8:27b-wb65536")
    p.add_argument("--outdir", required=True)
    p.add_argument("--theme", default="wengao-business",
                   help="材料候选进入验收时使用的主题（受注册表约束）")
    p.add_argument("--single-pass", action="store_true",
                   help="单次规划（短材料对照）；默认两阶段分块")
    p.add_argument("--cancel-file", default=None,
                   help="取消标记文件：存在时在计划/构建闸门处停止（不产出可下载结果）")
    p.add_argument("--replay-plan", default=None,
                   help="回放冻结的内容计划（不调用模型）；仅用于演示/回归，产物明确标记 replay")
    p.add_argument("--accept", action="store_true",
                   help="生成后立即对该 PPTX 执行 precheck，进入候选/验收流程")
    p.set_defaults(func=cmd_from_material)

    p = sub.add_parser("edit", help="授权局部修改（页/对象/字段/新值）")
    _edit_common(p)
    p.set_defaults(func=cmd_edit)

    p = sub.add_parser("edit-list", help="列出该任务的局部修改版本")
    p.add_argument("--job-id", required=True)
    p.set_defaults(func=cmd_edit_list)

    p = sub.add_parser("edit-restore", help="恢复历史版本（需重新完整性校验）")
    p.add_argument("--job-id", required=True)
    p.add_argument("--version-id", required=True)
    p.set_defaults(func=cmd_edit_restore)

    p = sub.add_parser("precheck")
    p.add_argument("--input", required=True)
    p.add_argument("--mode", default="beautify")
    p.add_argument("--theme", default="wengao-basic")
    p.add_argument("--support-profile", default=SUPPORT_PROFILE)
    p.add_argument("--material-source", default=None,
                   help="材料任务：原始材料路径（与 ledger/plan 一起在建任务时冻结身份）")
    p.add_argument("--material-ledger", default=None,
                   help="材料任务：模型调用前冻结的事实台账 JSON")
    p.add_argument("--material-plan", default=None,
                   help="材料任务：内容计划 JSON")
    p.add_argument("--preserve-design", action="store_true",
                   help="保留原设计：不改背景/形状/文字样式，只做检查与可信预览。"
                        "它与主题应用是不同任务身份，不得互相当作已完成。")
    p.set_defaults(func=_gate_cli(cmd_precheck))

    p = sub.add_parser("run")
    p.add_argument("--job-id", required=True)
    p.add_argument("--timeout", type=int, default=3600)
    p.add_argument("--hold-after-build", type=float, default=0,
                   help="生成完毕、发布之前停留的秒数（取消类故障测试用）")
    p.add_argument("--allow-text-card-fallback", action="store_true", default=False,
                   help="默认关闭。开启后，找不到可核验合规配图的场景改用"
                        "说明文字卡片代替，不再整单阻断；卡片内容如实标注"
                        "为未配图兜底，从不冒充真实照片。")
    p.add_argument("--skip-scene-photos", action="store_true", default=False,
                   help="默认关闭。开启后，整单不执行语义场景配图阶段"
                        "（不探测、不调用任何本机/云端模型），适用于没有配置"
                        "模型后端、只想要结构化美化的场景。validate 会如实把"
                        "该任务标注为'用户主动跳过'，不冒充已评估或已配图。")
    p.set_defaults(func=_gate_cli(cmd_run))

    p = sub.add_parser("cancel")
    p.add_argument("--job-id", required=True)
    p.add_argument("--grace", type=float, default=5.0)
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("submit")
    p.add_argument("--job-id", required=True)
    p.add_argument("--attempt-id", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--artifact", required=True)
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("publish")
    p.add_argument("--job-id", required=True)
    p.set_defaults(func=_gate_cli(cmd_publish))

    p = sub.add_parser("validate")
    p.add_argument("--job-id", required=True)
    # 与 run/export 同一契约：GateError 以退出码 4 + 结构化 JSON 上报，不吐堆栈
    p.set_defaults(func=_gate_cli(cmd_validate))

    p = sub.add_parser("export")
    p.add_argument("--job-id", required=True)
    p.add_argument("--outdir")
    p.set_defaults(func=_gate_cli(cmd_export))

    # A29：`status` 在这里是**任务状态**（必须 --job-id），与安装脚本里的
    # `wengao status`（安装状态）同名不同义。help 里写明，避免调用方混用。
    p = sub.add_parser("status", aliases=["job-status"],
                       help="查看**任务**状态（需要 --job-id）；"
                            "安装状态请用安装脚本的 wengao status")
    p.add_argument("--job-id", required=True)
    p.set_defaults(func=cmd_status)

    # ---- 事故台账：登记/查看"已确认不可交付"的结果 ----
    p = sub.add_parser("incident-record",
                       help="登记一条事故（append-only，绑定文件哈希；不改历史报告）")
    p.add_argument("--incident-id", default=None)
    p.add_argument("--reason", required=True)
    p.add_argument("--job-id", action="append", default=[])
    p.add_argument("--artifact", action="append", default=[],
                   help="受影响文件路径（登记其 SHA256）；可重复")
    p.add_argument("--scope", default="artifacts", choices=["artifacts", "all"])
    p.add_argument("--source-report", default=None)
    p.add_argument("--note", default=None)
    p.set_defaults(func=cmd_incident_record)

    p = sub.add_parser("incident-list", help="列出事故台账与当前交付资格")
    p.add_argument("--job-id", default=None)
    p.set_defaults(func=cmd_incident_list)

    # A24：上传件/预览图只增不减，此前既没有 HTTP 入口也没有命令行入口。
    # 只列清单；实际移动（--apply）本轮未开放（暂缓清理），带了只告知未执行。
    p = sub.add_parser("cleanup",
                       help="列出自检可回收的上传件/预览图（只读；实际移动本轮未开放）")
    p.add_argument("--target", default="uploads", choices=["uploads", "previews"])
    p.add_argument("--older-than-hours", type=float, default=None)
    p.add_argument("--job-id", default=None)
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--apply", action="store_true",
                   help="（本轮未开放）实际移动暂缓；带此参数只会在响应中说明未执行")
    p.set_defaults(func=cmd_cleanup)

    a = ap.parse_args(argv)
    if not getattr(a, "project_root", None):
        a.project_root = _default_project_root()
    if getattr(a, "cmd", None) in JOB_SCOPED_CMDS:
        bad = _precheck_job_ref(a)
        if bad is not None:
            return bad
    try:
        return a.func(a)
    except GateError as e:
        # 已登记的拒绝原因：结构化输出，退出码 4（不是"崩了"）
        _emit_cli_error(e.code, str(e.detail), command=a.cmd)
        return 4
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        # 未预期异常也只能是"结构化失败"，不能再把 Python 栈当接口返回；
        # 栈仍打到 stderr，便于排查，但 stdout 永远是可解析的 JSON。
        import traceback
        _emit_cli_error("CLI.EXECUTION_FAILED",
                        f"{type(e).__name__}: {e}", command=a.cmd)
        traceback.print_exc(file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
