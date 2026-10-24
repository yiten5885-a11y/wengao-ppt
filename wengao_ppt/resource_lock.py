"""wengao_ppt.resource_lock —— 共享机器的资源锁（Step 0.4 新建）。

背景（任务书 §2.5、公共契约 C9）：四个窗口共用同一台 Mac mini、同一个 Ollama。
不得重启共享 Ollama；调用 Qwen / ComfyUI / 相似度模型一律走本模块的三把锁排队：
  - llm   ：语义模型（Qwen 规划/抽取）
  - gen   ：本地生图（ComfyUI）
  - vision：图文相似度模型（CLIP/SigLIP 类）

实现要点：
- 跨进程：fcntl.flock 文件锁（macOS 可用），工作台服务与 CLI 子进程同时生效。
- 不重入、不饿死：等待方按 FCFS 轮询（LOCK_POLL_S），超时抛 `ResourceLockTimeout`，
  错误码 `PIPE.RESOURCE_BUSY`，由调用方按 C8 降级（例如 T2→T3→T4）。
- 可观测：每次等待/持有写 `locks.jsonl`（不含任何用户内容），供 RS-7 诊断排队时长。
- 锁目录可用环境变量 `WENGAO_LOCK_DIR` 覆盖；默认随项目根 `.locks/`。
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

VALID_LOCKS = ("llm", "gen", "vision")
LOCK_POLL_S = 0.25
DEFAULT_TIMEOUT_S = 900  # 排队上限；单个模型调用本身的超时由调用方自行控制


class ResourceLockError(Exception):
    """锁本身使用错误（名称非法等）。"""


class ResourceLockTimeout(Exception):
    """等待超过 timeout_s 仍未获得锁。错误码 PIPE.RESOURCE_BUSY。"""

    code = "PIPE.RESOURCE_BUSY"


def lock_root(root: Optional[Path] = None) -> Path:
    if root is not None:
        return Path(root)
    env = os.environ.get("WENGAO_LOCK_DIR")
    if env:
        return Path(env)
    # 默认：项目根下的 .locks/（与本模块位置无关，安装实例以数据根为准）
    return Path(__file__).resolve().parents[1] / ".locks"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _append_log(root: Path, name: str, event: str, waited_s: float, held_s: float, owner: str) -> None:
    try:
        root.mkdir(parents=True, exist_ok=True)
        with (root / "locks.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "lock": name, "event": event, "owner": owner,
                "waited_s": round(waited_s, 3), "held_s": round(held_s, 3),
                "at": _now(),
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 观测日志写失败不得影响主流程


@contextmanager
def resource_lock(name: str, timeout_s: float = DEFAULT_TIMEOUT_S,
                  root: Optional[Path] = None, owner: str = ""):
    """获取一把共享资源锁。用法：with resource_lock("llm"): ..."""
    if name not in VALID_LOCKS:
        raise ResourceLockError(f"未知资源锁 {name!r}；可用：{'、'.join(VALID_LOCKS)}")
    base = lock_root(root)
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ResourceLockError(f"锁目录不可用：{base}（{e}）") from e
    path = base / f"{name}.lock"
    owner = owner or f"pid{os.getpid()}"
    started = time.monotonic()
    fh = path.open("a+")
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as e:
                if e.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.monotonic() - started > timeout_s:
                    raise ResourceLockTimeout(
                        f"等待资源锁 {name} 超过 {timeout_s:.0f}s 仍被占用；"
                        f"按契约 C8 降级处理，不得无限排队。")
                time.sleep(LOCK_POLL_S)
        waited = time.monotonic() - started
        _append_log(base, name, "acquired", waited, 0.0, owner)
        try:
            yield name
        finally:
            held = time.monotonic() - started - waited
            _append_log(base, name, "released", waited, max(held, 0.0), owner)
    finally:
        if acquired:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()


@contextmanager
def llm(**kw):
    with resource_lock("llm", **kw):
        yield


@contextmanager
def gen(**kw):
    with resource_lock("gen", **kw):
        yield


@contextmanager
def vision(**kw):
    with resource_lock("vision", **kw):
        yield
