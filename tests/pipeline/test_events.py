"""tests/pipeline/test_events.py —— C2 事件流与共享资源锁测试（Step 0.4/0.5）。

注：锁测试使用项目内临时目录（build/test-tmp/），不依赖系统 /var/folders——
CI/沙箱环境对 pytest tmp_path 的目录创建可能受限。
"""
from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path

import pytest

from wengao_ppt import events
from wengao_ppt.events import EventEmitter
from wengao_ppt.resource_lock import (ResourceLockError, ResourceLockTimeout,
                                      llm, resource_lock)

ROOT = Path(__file__).resolve().parents[2]
TMP_BASE = ROOT / "build" / "test-tmp"


@pytest.fixture()
def local_tmp():
    TMP_BASE.mkdir(parents=True, exist_ok=True)
    d = TMP_BASE / f"t{threading.get_ident()}-{len(list(TMP_BASE.iterdir()))}"
    d.mkdir(parents=True, exist_ok=True)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_emit_never_raises_and_assigns_seq(local_tmp):
    em = EventEmitter("job-t1", job_dir=local_tmp)
    got = []
    em.subscribe(lambda ev: got.append(ev))
    e1 = em.emit("read", "running", "正在读取材料")
    e2 = em.done("read", "已锁定 38 条必讲事实", counts={"facts": 38})
    assert e1["seq"] == 1 and e2["seq"] == 2
    assert e1["stage"] == "read" and e1["status"] == "running"
    assert len(got) == 2


def test_events_persisted_to_evidence(local_tmp):
    em = EventEmitter("job-t2", job_dir=local_tmp)
    em.emit("images", "done", "9 张实拍，3 张生成，2 页未配图")
    p = local_tmp / "evidence" / "pipeline" / "events.jsonl"
    assert p.is_file()
    rec = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
    assert rec["stage"] == "images"
    assert events.load_events(local_tmp)[0]["seq"] == 1


def test_summary_clipped_to_40():
    em = EventEmitter("job-t3")
    ev = em.emit("check", "done", "长" * 80)
    assert len(ev["summary"]) <= 40


def test_unknown_stage_falls_back_not_raise():
    em = EventEmitter("job-t4")
    ev = em.emit("publish", "done", "未登记阶段")
    assert ev["stage"] == "check"  # 落盘可识别，不抛异常


def test_subscriber_exception_does_not_break_emit():
    em = EventEmitter("job-t5")
    em.subscribe(lambda ev: 1 / 0)
    ev = em.emit("layout", "done", "版式完成")
    assert ev["status"] == "done"


def test_resource_lock_exclusive(local_tmp):
    order = []

    def worker():
        with resource_lock("llm", root=local_tmp, timeout_s=5):
            order.append("in")
            threading.Event().wait(0.15)
            order.append("out")

    ts = [threading.Thread(target=worker) for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    # 三把锁互斥：in/out 必须严格交替，不得出现连续两个 in
    assert order[0] == "in" and order[1] == "out"
    assert "".join(order) == "inout" * 3


def test_resource_lock_timeout(local_tmp):
    with resource_lock("gen", root=local_tmp, timeout_s=5):
        with pytest.raises(ResourceLockTimeout):
            with resource_lock("gen", root=local_tmp, timeout_s=0.3):
                pass


def test_resource_lock_rejects_unknown_name():
    with pytest.raises(ResourceLockError):
        with resource_lock("database"):
            pass


def test_lock_log_written(local_tmp):
    with llm(root=local_tmp):
        pass
    log = (local_tmp / "locks.jsonl").read_text(encoding="utf-8").splitlines()
    assert any('"event": "acquired"' in line for line in log)
    assert any('"event": "released"' in line for line in log)
