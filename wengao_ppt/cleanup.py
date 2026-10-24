"""上传/预览目录的清理入口（auto30 A24）。

设计前提：工作台的 uploads/ 与 previews/ 只增不减，此前没有任何入口可以回收，
也没有任何办法先看清"会被清掉的是什么"。

清理由此按三条约束实现：
1. **不做永久删除**：被清理的文件只是移到 `<数据根>/_trash/<时间戳>/`，
   保留相对路径与 sha256 清单，可原样取回（产品侧"原件永久删除 0"的同一口径）。
2. **默认 dry-run**：不传 `apply` 时只列清单，调用方必须先看到清单再决定。
3. **不碰任务目录**：jobs/ 是证据与身份所在，绝不在清理范围内；
   只清理派生的上传件与预览图（丢了可以重新上传/重新渲染）。

2026-10-18 有限准入复核（MAINFLOW-REPAIR-30 复核轮）修订：
原任务书本轮**明确暂缓清理**。移入 _trash 仍是文件变更，不因"永久删除 0"
自动合规；因此**实际移动入口在普通产品中不开放**——HTTP 与 CLI 都只保留
只读清单（dry-run），不再接受执行。`apply_plan` 代码保留（不删除、不回退
整个 v30），但没有任何产品入口调用它；是否重新开放执行需单独一轮决定。
"""
from __future__ import annotations

import json
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))

#: 实际移动（apply）是否在普通产品中开放。本轮复核决定：**不开放**（暂缓清理）。
#: 置 True 即恢复 v30 行为（入口直接移入 _trash）；改动需单独一轮并重新回归。
EXECUTION_OPEN = False

#: 入口不接受执行时返回的说明（写进响应，调用方不靠猜）
EXECUTION_DEFERRED_NOTE = (
    "实际移动本轮未开放（暂缓清理）：本响应仍是只读清单，未移动任何文件；"
    "移入 _trash 也属文件变更，不因永久删除为 0 而自动合规。")

# 允许清理的目标：只含**可重建的派生目录**
TARGETS = {
    "uploads": "workbench_uploads",
    "previews": "workbench_previews",
}


def _now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def _sha256(p: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def target_dir(root, target: str) -> Path:
    root = Path(root)
    if target == "trash":
        return root / "_trash"
    if target not in TARGETS:
        raise KeyError(target)
    return root / TARGETS[target]


def plan(root, target: str = "uploads", older_than_hours=None,
         job_id: str | None = None, limit: int = 200) -> dict:
    """列出将被清理的文件（不做任何动作）。"""
    base = target_dir(root, target)
    out = {"root": str(Path(root)), "target": target, "dir": str(base),
           "dry_run": True, "items": [], "total_bytes": 0, "truncated": False}
    if not base.is_dir():
        out.update(ok=False, code="WB.CLEANUP_DIR_MISSING",
                   detail=f"目录不存在：{base}（还没有产生过可清理内容）。")
        return out
    cutoff = None
    if older_than_hours:
        try:
            cutoff = time.time() - float(older_than_hours) * 3600.0
        except (TypeError, ValueError):
            cutoff = None
    files = [p for p in sorted(base.rglob("*")) if p.is_file()
             and p.name != "manifest.json"]
    if job_id:
        files = [p for p in files if job_id in str(p.relative_to(base))]
    for p in files:
        if cutoff is not None and p.stat().st_mtime > cutoff:
            continue
        if len(out["items"]) >= limit:
            out["truncated"] = True
            break
        st = p.stat()
        out["items"].append({
            "rel": str(p.relative_to(base)), "bytes": st.st_size,
            "modified_at": datetime.fromtimestamp(
                st.st_mtime).isoformat(timespec="seconds"),
            "sha256": _sha256(p) if st.st_size <= (64 << 20) else None,
        })
        out["total_bytes"] += st.st_size
    out.update(ok=True, code=None, count=len(out["items"]))
    return out


def apply_plan(root, target: str = "uploads", older_than_hours=None,
               job_id: str | None = None, limit: int = 200) -> dict:
    """按同一条件把文件**移入回收目录**；不做永久删除。"""
    p = plan(root, target, older_than_hours, job_id, limit)
    if not p.get("ok"):
        p["dry_run"] = False
        return p
    base = Path(p["dir"])
    stamp = datetime.now(CST).strftime("%Y%m%dT%H%M%S")
    trash = Path(root) / "_trash" / f"{stamp}-{target}"
    trash.mkdir(parents=True, exist_ok=True)
    moved, failed, total = [], [], 0
    for it in p["items"]:
        src = base / it["rel"]
        dst = trash / it["rel"]
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            moved.append(it["rel"])
            total += it["bytes"]
        except OSError as e:  # noqa: BLE001
            failed.append({"rel": it["rel"], "error": f"{type(e).__name__}: {e}"})
    manifest = {"action": "move_to_trash", "target": target,
                "source_dir": str(base), "trash_dir": str(trash),
                "created_at": _now_iso(), "older_than_hours": older_than_hours,
                "job_id": job_id, "moved": moved, "failed": failed,
                "bytes": total,
                "note": "只做移动、不做删除；按 sha256 清单可原样取回。"}
    (trash / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    p.update(dry_run=False, trash_dir=str(trash), moved=moved, failed=failed,
             bytes=total, count=len(moved),
             detail=("已移入回收目录（未删除，可原样取回）；"
                     "预览图与上传件都是可重建的派生内容，任务目录不在清理范围内。"))
    return p
