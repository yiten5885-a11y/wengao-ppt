"""wengao_ppt.pipeline.edit —— 编辑模式：local_edit + W2 页级重排/插页 + 未触及页保真。

任务书/W4 指令 §5：
- 编辑模式沿用 local_edit.py（限定范围修改、共享资源检测、版本历史）；
- 页级重排 / 插页调用 W2 的 recompose_slide / insert_slide（未就位时诚实降级）；
- 之后对**未触及的页**跑现有保真门禁（local_edit 自身保证非目标不变，
  这里只做候选可开性 + check_layout 复核）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from wengao_ppt import events as ev_mod
from wengao_ppt.pipeline import modules as mod


class EditPipeline:
    def __init__(self, project_root, sink: Optional[Callable[[dict], None]] = None):
        self.root = Path(project_root)
        self.sink = sink

    def _emitter(self, job_id: str) -> ev_mod.EventEmitter:
        em = ev_mod.EventEmitter(job_id, job_dir=self.root / "jobs" / job_id)
        if self.sink is not None:
            em.subscribe(self.sink if callable(self.sink) else self.sink.append)
        return em

    # ------------------------------------------------------------ 局部修改
    def apply_edit(self, job_id: str, edit: dict, base_pptx=None) -> dict:
        """一次授权局部修改（local_edit）。事件 + 结果；越界即抛 EditScopeError。

        base_pptx：调用方明确指定的基线（服务端从 /baseline 状态取）；
        缺省时由 local_edit 自己按"已登记交付目录"解析，解析不到会报
        BASELINE.NOT_EXPORTED 并给下一步，不猜测、不从候选反推。
        """
        from wengao_ppt.local_edit import apply_local_edit
        job_dir = self.root / "jobs" / job_id
        em = self._emitter(job_id)
        em.running("layout", "正在按你的选择修改这一页")
        result = apply_local_edit(job_dir, edit, base_pptx=base_pptx)
        candidate = Path(result.get("candidate_pptx") or "")
        rep = {"status": "UNKNOWN"}
        if candidate.is_file():
            rep = mod.run_check("check_layout", candidate, job_dir)
        status = rep.get("status", "UNKNOWN")
        em.emit("check", "done" if status in ("OK", "PASS") else
                ("warn" if status == "NOT_EVALUABLE" else "failed"),
                f"版式复核：{_text(rep)}")
        em.done("layout", "修改已生成为新版本，未触及的页保持不变")
        return {"job_id": job_id, "edit": edit, "local_edit": _plain(result),
                "check": rep}

    # ------------------------------------------------------------ 页级重排
    def recompose_page(self, job_id: str, page_index: int, variant: str = "a") -> dict:
        """换版式：W2 recompose_slide（未接入时如实报告，不假装成功）。"""
        job_dir = self.root / "jobs" / job_id
        em = self._emitter(job_id)
        em.running("layout", f"正在把第 {page_index} 页换成更合适的版式")
        from wengao_ppt.local_edit import resolve_baseline
        state = resolve_baseline(job_dir)
        if not state.get("ok"):
            em.failed("layout", "没有可修改的基线，请先完成制作并导出")
            return {"job_id": job_id, "status": "NO_BASELINE",
                    "reason": state.get("detail")}
        fn, src = mod.get("recompose_slide")
        out = job_dir / "versions" / f"recompose_p{page_index}_{variant}.pptx"
        out.parent.mkdir(parents=True, exist_ok=True)
        rep = fn(state["path"], page_index, variant, None, out)
        if rep.get("_stub"):
            em.warn("layout", "版式引擎尚未接入，暂时不能换版式")
            return {"job_id": job_id, "status": "NOT_AVAILABLE", "report": rep,
                    "module": src}
        em.done("layout", "已生成新版式候选")
        return {"job_id": job_id, "status": "OK", "report": rep,
                "candidate": str(out), "module": src}

    # ------------------------------------------------------------ 插页
    def insert_page(self, job_id: str, after_index: int, page_spec: dict) -> dict:
        """在导入 PPT 中插入新页（如检索得到的对比页）。"""
        job_dir = self.root / "jobs" / job_id
        em = self._emitter(job_id)
        em.running("layout", f"正在第 {after_index} 页后插入新页面")
        from wengao_ppt.local_edit import resolve_baseline
        state = resolve_baseline(job_dir)
        if not state.get("ok"):
            em.failed("layout", "没有可修改的基线，请先完成制作并导出")
            return {"job_id": job_id, "status": "NO_BASELINE",
                    "reason": state.get("detail")}
        fn, src = mod.get("insert_slide")
        out = job_dir / "versions" / f"insert_after_{after_index}.pptx"
        out.parent.mkdir(parents=True, exist_ok=True)
        rep = fn(state["path"], after_index, page_spec, None, out)
        if rep.get("_stub"):
            em.warn("layout", "版式引擎尚未接入，暂时不能插入页面")
            return {"job_id": job_id, "status": "NOT_AVAILABLE", "report": rep,
                    "module": src}
        em.done("layout", "已生成插入新页后的候选")
        return {"job_id": job_id, "status": "OK", "report": rep,
                "candidate": str(out), "module": src}

    # ------------------------------------------------------------ 版本
    def list_versions(self, job_id: str) -> dict:
        from wengao_ppt.local_edit import list_versions
        return {"job_id": job_id, "versions": list_versions(self.root / "jobs" / job_id)}

    def restore_version(self, job_id: str, version_id: str) -> dict:
        from wengao_ppt.local_edit import restore_version
        em = self._emitter(job_id)
        em.running("layout", "正在恢复历史版本")
        result = restore_version(self.root / "jobs" / job_id, version_id)
        em.done("layout", "已恢复到所选版本")
        return {"job_id": job_id, "version_id": version_id, "result": _plain(result)}


def _text(rep: dict) -> str:
    status = rep.get("status", "UNKNOWN")
    if status in ("OK", "PASS"):
        return "通过"
    if status == "NOT_EVALUABLE":
        return "未评估（模块未接入）"
    return rep.get("reason") or "未通过"


def _plain(obj):
    import json
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return {"note": "结果不可序列化", "type": type(obj).__name__}
