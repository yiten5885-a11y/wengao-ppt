"""wengao_ppt.pipeline.beautify —— 美化模式：包装 MakeOrchestrator + C2 事件流 + 新检查。

任务书 §4：美化模式沿用 make_flow.py 的 MakeOrchestrator（真实 CLI 状态机，
不另建第二套成功/失败判定），W4 只做三件事：
  1. 把编排的阶段历史翻译成 C2 事件（8 步时间线）；
  2. 编排结束后接 render_pages 与全部 check_*（W1/W2/W3 交付的检查）；
  3. 冲突/降级语义与 generate 一致（C8）。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Optional

from wengao_ppt import events as ev_mod
from wengao_ppt.pipeline import modules as mod

# MakeOrchestrator 阶段 → 工作台 8 步时间线的 stage
STAGE_MAP = {
    "precheck": "read",
    "run": "layout",           # run 内部含 semantic_scenes 配图，见 semantic_scene_stage
    "validate": "check",
    "publish": "check",
    "export": "check",
    "complete": "check",
    "stop": "check",
    "cancel": "check",
    "guard": "check",
}


class BeautifyPipeline:
    def __init__(self, project_root, sink: Optional[Callable[[dict], None]] = None):
        self.root = Path(project_root)
        self.sink = sink
        self._orch = None

    def _orchestrator(self):
        if self._orch is None:
            from workbench.make_flow import MakeOrchestrator
            self._orch = MakeOrchestrator(project_root=self.root)
        return self._orch

    def _emitter(self, job_id: str) -> ev_mod.EventEmitter:
        em = ev_mod.EventEmitter(job_id, job_dir=self.root / "jobs" / job_id)
        if self.sink is not None:
            em.subscribe(self.sink if callable(self.sink) else self.sink.append)
        return em

    def start(self, source_pptx: str, theme: Optional[str] = None) -> dict:
        """受理一次美化制作（复用 MakeOrchestrator.start 的真实预检与单飞保护）。"""
        orch = self._orchestrator()
        accepted = orch.start(source_pptx, theme or "")
        if not accepted.get("accepted"):
            return accepted
        job_id = accepted["job_id"]
        em = self._emitter(job_id)
        em.running("read", "已接收原稿，正在预检冻结任务身份")
        accepted["events_url"] = f"/api/jobs/{job_id}/events"
        return accepted

    def stream_until_done(self, job_id: str, poll_s: float = 1.0,
                          timeout_s: float = 3600) -> list:
        """轮询编排真实状态，把新阶段翻译成 C2 事件；返回全部事件。"""
        orch = self._orchestrator()
        em = self._emitter(job_id)
        seen = set()
        out = []
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            snap = orch.snapshot(job_id)
            for h in (snap.get("orchestration") or {}).get("history") or []:
                key = (h.get("stage"), h.get("at"))
                if key in seen:
                    continue
                seen.add(key)
                stage = STAGE_MAP.get(h.get("stage"), "check")
                state = h.get("state")
                status = {"RUNNING": "running", "DONE": "done",
                          "FAILED": "failed", "STOPPED": "warn",
                          "REQUESTED": "warn", "COMPLETED": "done",
                          "ERROR": "failed"}.get(state, "done")
                out.append(em.emit(stage, status, _stage_summary(h)))
            flow_state = (snap.get("orchestration") or {}).get("state")
            if flow_state in ("DONE", None) and seen:
                break
            time.sleep(poll_s)
        # 收尾：渲染 + 检查（与 generate 同一套 C8 语义）
        out += self.finish_checks(job_id)
        return out

    def finish_checks(self, job_id: str) -> list:
        job_dir = self.root / "jobs" / job_id
        em = self._emitter(job_id)
        job = _load_json(job_dir / "job.json") or {}
        deck = job.get("export_path") or job.get("candidate_pptx")
        deck_path = Path(deck) if deck else None
        out = []
        if deck_path and deck_path.is_file():
            em.running("check", "正在逐页渲染并核对")
            render_pages, _ = mod.get("render_pages")
            pages_dir = job_dir / "evidence" / "layout" / "pages"
            try:
                rendered = render_pages(deck_path, pages_dir)
                (job_dir / "evidence" / "layout").mkdir(parents=True, exist_ok=True)
                (job_dir / "evidence" / "layout" / "render.json").write_text(
                    _json(rendered), encoding="utf-8")
                out.append(em.done("check", f"已渲染 {len(rendered.get('pngs') or [])} 页"))
            except Exception as e:  # noqa: BLE001
                out.append(em.warn("check", f"渲染未完成：{type(e).__name__}"))
            for name in ("check_image_ledger", "check_transitions", "check_layout"):
                rep = mod.run_check(name, deck_path, job_dir)
                status = rep.get("status", "UNKNOWN")
                out.append(em.emit("check", "done" if status in ("OK", "PASS")
                                   else ("warn" if status == "NOT_EVALUABLE" else "failed"),
                                   f"{_check_label(name)}：{_check_text(rep)}"))
        else:
            out.append(em.warn("check", "本稿没有可核对的导出文件"))
        return out


def _stage_summary(h: dict) -> str:
    stage = h.get("stage")
    state = h.get("state")
    label = {"precheck": "预检", "run": "制作", "validate": "验收",
             "publish": "发布", "export": "导出", "complete": "完成",
             "stop": "停止", "cancel": "取消", "guard": "护栏"}.get(stage, stage)
    detail = h.get("detail") or {}
    verdict = detail.get("verdict") if isinstance(detail, dict) else None
    if verdict:
        return f"{label}：{verdict}"
    return f"{label}：{state}"


def _check_label(name: str) -> str:
    return {"check_image_ledger": "配图账本", "check_transitions": "转场",
            "check_layout": "版式"}.get(name, name)


def _check_text(rep: dict) -> str:
    status = rep.get("status", "UNKNOWN")
    if status in ("OK", "PASS"):
        return "通过"
    if status == "NOT_EVALUABLE":
        return "未评估（模块未接入）"
    return rep.get("reason") or "未通过"


def _load_json(p: Path):
    import json
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _json(obj) -> str:
    import json
    try:
        return json.dumps(obj, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return json.dumps({"note": "不可序列化", "type": type(obj).__name__},
                          ensure_ascii=False)
