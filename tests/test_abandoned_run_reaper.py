from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from wengao_ppt.runner import Runner


def _iso(seconds_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


class AbandonedRunReaperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wengao-reaper-")
        self.addCleanup(self.tmp.cleanup)
        self.runner = Runner(self.tmp.name)

    def _job(self, job_id, *, verdict="RUNNING", heartbeat_ago=10_000, pid=None):
        d = Path(self.tmp.name) / "jobs" / job_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "job.json").write_text(json.dumps({
            "job_id": job_id, "verdict": verdict, "stage": "semantic_scenes",
            "stages": [], "heartbeat_at": _iso(heartbeat_ago),
            "last_activity": _iso(heartbeat_ago),
        }, ensure_ascii=False), encoding="utf-8")
        if pid is not None:
            (d / "worker.pid").write_text(f"{pid} {_iso(heartbeat_ago)}", encoding="utf-8")
        return d

    def _load(self, job_id):
        return json.loads((Path(self.tmp.name) / "jobs" / job_id / "job.json")
                          .read_text(encoding="utf-8"))

    # ---------------------------------------------------------------- 应当回收
    def test_dead_worker_long_silent_is_reaped(self):
        # pid 1 不属于本进程组，但一定存活；用一个几乎不可能存在的 pid 模拟已死。
        self._job("job-dead", pid=999_999)
        rec = self.runner.reap_abandoned_run("job-dead")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["code"], "GATE.INTERRUPTED")
        job = self._load("job-dead")
        self.assertEqual(job["verdict"], "FAILED")
        self.assertEqual(job["fail_reason"], "GATE.INTERRUPTED")
        self.assertTrue(job.get("run_finished_at"))

    def test_reaping_records_a_stage_so_the_reason_is_visible(self):
        self._job("job-dead", pid=999_999)
        self.runner.reap_abandoned_run("job-dead")
        stages = self._load("job-dead")["stages"]
        self.assertTrue(stages)
        self.assertEqual(stages[-1]["status"], "FAILED")
        self.assertEqual(stages[-1]["detail"]["code"], "GATE.INTERRUPTED")

    def test_missing_pid_file_and_long_silence_is_reaped(self):
        self._job("job-nopid", pid=None)
        self.assertIsNotNone(self.runner.reap_abandoned_run("job-nopid"))

    def test_stale_pid_file_is_removed(self):
        d = self._job("job-dead", pid=999_999)
        self.runner.reap_abandoned_run("job-dead")
        self.assertFalse((d / "worker.pid").exists())

    # ---------------------------------------------------------------- 不应回收
    def test_live_worker_is_never_reaped(self):
        self._job("job-live", pid=os.getpid())
        self.assertIsNone(self.runner.reap_abandoned_run("job-live"))
        self.assertEqual(self._load("job-live")["verdict"], "RUNNING")

    def test_recent_heartbeat_is_not_reaped(self):
        # worker 刚起、还没写 worker.pid 的空档不能被误杀。
        self._job("job-young", heartbeat_ago=5, pid=None)
        self.assertIsNone(self.runner.reap_abandoned_run("job-young"))
        self.assertEqual(self._load("job-young")["verdict"], "RUNNING")

    def test_finished_jobs_are_left_alone(self):
        for verdict in ("DEVELOPMENT_CANDIDATE", "FAILED", "CANCELLED", "NEEDS_REVIEW"):
            with self.subTest(verdict=verdict):
                jid = f"job-{verdict.lower()}"
                self._job(jid, verdict=verdict, pid=999_999)
                self.assertIsNone(self.runner.reap_abandoned_run(jid))
                self.assertEqual(self._load(jid)["verdict"], verdict)

    def test_unparsable_heartbeat_is_not_reaped(self):
        d = self._job("job-badts", pid=999_999)
        job = json.loads((d / "job.json").read_text(encoding="utf-8"))
        job["heartbeat_at"] = "not-a-timestamp"
        job["last_activity"] = "not-a-timestamp"
        (d / "job.json").write_text(json.dumps(job), encoding="utf-8")
        self.assertIsNone(self.runner.reap_abandoned_run("job-badts"))

    def test_missing_job_is_not_an_error(self):
        self.assertIsNone(self.runner.reap_abandoned_run("job-does-not-exist"))

    # ---------------------------------------------------------------- 批量
    def test_sweep_reaps_only_the_abandoned_ones(self):
        self._job("job-a", pid=999_999)
        self._job("job-b", pid=os.getpid())
        self._job("job-c", verdict="FAILED", pid=999_999)
        reaped = {r["job_id"] for r in self.runner.reap_all_abandoned_runs()}
        self.assertEqual(reaped, {"job-a"})

    def test_reaping_is_idempotent(self):
        self._job("job-dead", pid=999_999)
        self.assertIsNotNone(self.runner.reap_abandoned_run("job-dead"))
        self.assertIsNone(self.runner.reap_abandoned_run("job-dead"))


if __name__ == "__main__":
    unittest.main()
