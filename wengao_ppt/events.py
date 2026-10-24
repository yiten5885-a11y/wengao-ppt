"""wengao_ppt.events —— C2 事件传输（W4 拥有，其余窗口只调用 emit）。

公共契约 C2 的形状：
  {"job_id", "seq", "ts", "stage", "status", "summary", "counts", "refs"}
- 8 个 stage 与工作台步骤时间线一一对应：
  read / research / compare / trend / images / layout / transition / check
- status: running | done | warn | failed | skipped
- summary：面向用户的中文一句话，≤40 字，不含内部路径、堆栈、密钥。

纪律：
- emit 永远不抛异常（调用方不得因事件失败而中断主流程）。
- 窗口自己不得另起传输通道；长任务函数统一接收 `emit: Callable[[dict], None]`。
- 事件同时落盘 `evidence/pipeline/events.jsonl`（C7），门禁与界面从磁盘读。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

STAGES = ("read", "research", "compare", "trend", "images", "layout",
          "transition", "check")
STATUSES = ("running", "done", "warn", "failed", "skipped")
MAX_SUMMARY = 40

#: 界面步骤时间线的中文标签（不出现内部码）
STAGE_LABELS = {
    "read": "读取材料",
    "research": "联网检索",
    "compare": "横向对比",
    "trend": "纵向趋势",
    "images": "配图",
    "layout": "版式",
    "transition": "转场",
    "check": "核对",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _clip(text: str, limit: int = MAX_SUMMARY) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


class EventEmitter:
    """按 job 记 seq 的事件发射器；可多个订阅者（SSE、落盘、测试收集）。"""

    def __init__(self, job_id: str, job_dir: Optional[Path] = None):
        self.job_id = job_id
        self.job_dir = Path(job_dir) if job_dir else None
        self._seq = 0
        self._lock = threading.Lock()
        self._subs: list[Callable[[dict], None]] = []

    def subscribe(self, fn: Callable[[dict], None]) -> None:
        self._subs.append(fn)

    def as_callable(self) -> Callable[[dict], None]:
        """返回 C6 约定的 `emit: Callable[[dict], None]` 适配器。

        W1/W2/W3 的长任务函数调用 `emit({...部分字段...})`；适配器补全信封
        （schema/job_id/seq/ts）、裁剪 summary、校验 stage/status 后发射。
        """
        def _emit(partial: dict) -> None:
            p = partial or {}
            self.emit(p.get("stage") or "check",
                      p.get("status") or "done",
                      p.get("summary") or "",
                      counts=p.get("counts"), refs=p.get("refs"))
        return _emit

    def emit(self, stage: str, status: str, summary: str = "",
             counts: Optional[dict] = None, refs: Optional[list] = None) -> dict:
        """发一条事件。任何异常都被吞掉——emit 永远不抛。"""
        try:
            with self._lock:
                self._seq += 1
                seq = self._seq
            if stage not in STAGES:
                stage = "check"
            if status not in STATUSES:
                status = "done"
            ev = {
                "schema": "rs.event.v1",
                "job_id": self.job_id,
                "seq": seq,
                "ts": now_iso(),
                "stage": stage,
                "status": status,
                "summary": _clip(summary),
                "counts": counts or {},
                "refs": refs or [],
            }
            self._persist(ev)
            for fn in list(self._subs):
                try:
                    fn(ev)
                except Exception:  # noqa: BLE001 —— 订阅者失败不得影响发射方
                    pass
            return ev
        except Exception:  # noqa: BLE001
            return {"job_id": self.job_id, "stage": stage, "status": status,
                    "summary": _clip(summary), "dropped": True}

    # 便捷方法
    def running(self, stage, summary="", **kw):
        return self.emit(stage, "running", summary, **kw)

    def done(self, stage, summary="", **kw):
        return self.emit(stage, "done", summary, **kw)

    def warn(self, stage, summary="", **kw):
        return self.emit(stage, "warn", summary, **kw)

    def failed(self, stage, summary="", **kw):
        return self.emit(stage, "failed", summary, **kw)

    def skipped(self, stage, summary="", **kw):
        return self.emit(stage, "skipped", summary, **kw)

    def _persist(self, ev: dict) -> None:
        if not self.job_dir:
            return
        try:
            d = self.job_dir / "evidence" / "pipeline"
            d.mkdir(parents=True, exist_ok=True)
            with (d / "events.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except OSError:
            pass  # 落盘失败不阻断主流程（界面会显示事件不完整）


def load_events(job_dir: Path) -> list:
    """从磁盘读事件流（界面与门禁共用；不信内存自报）。"""
    p = Path(job_dir) / "evidence" / "pipeline" / "events.jsonl"
    out = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        return []
    return out


def make_emitter(job_id: str, job_dir: Optional[Path] = None) -> EventEmitter:
    return EventEmitter(job_id, job_dir)
