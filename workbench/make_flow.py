"""D10-C WP2：「开始制作」受控编排入口。

设计要点（严格对应任务书 WP2 验收项）：

1. **不是顺序调用脚本**。本模块是一个**服务端状态机**：`/api/make` 只负责受理
   请求并冻结身份，真正推进由后台编排线程按阶段推进，前端只**读取真实状态**。
   前端不参与判定成功/失败，也不自己串联 run/validate/publish。

2. **复用现有 runner**：每一阶段都经**真实 CLI 入口**（`python -m wengao_ppt.cli`）
   的 precheck → run → validate → publish → export，不另建第二套成功/失败判定。

3. **终态停止推进**：validate 判定为 `FAILED` / `NEEDS_REVIEW`，或任务被取消，
   编排立即停止，**不再**进入 publish/export。

4. **auto-repair 全程关闭**：编排只调用 run/validate/publish/export，
   从不调用 repair 入口，也不设置任何自动修复开关。

5. **冻结**：主题 ID 与输入在 `precheck` 成功那一刻冻结进 job.json
   （job_id 本身由 输入哈希｜模式｜契约版本｜profile｜主题ID｜主题版本｜配置摘要 决定），
   后续阶段一律从 job.json 读回，不接受前端再次传入。

6. **重复点击后端保护**：同一 (input, theme) 组合由 job_id 天然幂等；
   同一 job 的编排单飞（`op_busy`），第二次点击返回既有编排，不新开一条。

7. **迟到结果不覆盖当前任务**：编排只推进**当前 attempt**；
   job_id 与 attempt_id 在编排开始时固定，阶段推进前校验 job.json 中的
   attempt_id 仍是编排持有的那个，否则停止推进（迟到结果无法接管）。

8. **取消真实可用**：`/api/make/{id}/cancel` 走真实 CLI cancel。

状态只来自 runner 落盘文件（job.json / validation.json / export_events.jsonl），
进程内只保存"编排是否在跑"这一位，避免第二套真值。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable

# 阶段推进的终态：到达即停止，不再进入 publish/export
STOP_VERDICTS = {"FAILED", "NEEDS_REVIEW"}
CANCELLED_MARK = ".cancelled"

# 每阶段的真实超时（秒）。渲染/验收较慢，单独给足。
STAGE_TIMEOUT = {
    "precheck": 300,
    "run": 1200,
    "validate": 1800,
    "publish": 600,
    "export": 600,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def load_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def last_json(stdout: str):
    dec = json.JSONDecoder()
    idx, obj = 0, None
    while True:
        i = stdout.find("{", idx)
        if i < 0:
            break
        try:
            obj, end = dec.raw_decode(stdout[i:])
            idx = i + end
        except ValueError:
            idx = i + 1
    return obj


class MakeOrchestrator:
    """服务端受控编排：受理一次「开始制作」，后台按阶段推进真实 CLI。"""

    def __init__(self, project_root: Path = ROOT, program_root: Path | None = None):
        self.root = Path(project_root)
        # 程序目录（代码/契约/样例）可与数据根分离：安装实例下样例随程序走。
        self.prog = Path(program_root) if program_root else self.root
        self._flows = {}        # job_id -> flow dict
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- 状态
    def _flow(self, job_id):
        with self._lock:
            f = self._flows.get(job_id)
            return dict(f) if f else None

    def _set(self, job_id, **kw):
        with self._lock:
            f = self._flows.setdefault(job_id, {"job_id": job_id,
                                                "state": "PENDING",
                                                "stage": None,
                                                "history": []})
            f.update(kw)
            return dict(f)

    def _append(self, job_id, stage, state, detail=None):
        with self._lock:
            f = self._flows.setdefault(job_id, {"job_id": job_id, "history": []})
            f["history"].append({"stage": stage, "state": state,
                                 "detail": detail, "at": now_iso()})
            f["stage"] = stage
            f["state"] = state

    def busy(self, job_id):
        f = self._flow(job_id)
        return bool(f and f.get("state") == "RUNNING")

    # ---------------------------------------------------------------- CLI
    def cli(self, *args, timeout=600):
        # 代码在程序目录、数据在数据根：子进程 cwd 与 PYTHONPATH 必须指向程序目录，
        # 否则安装实例（数据根 ≠ 程序目录）下 `-m wengao_ppt.cli` 会找不到模块。
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
               "PYTHONPATH": str(self.prog) +
               (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")}
        proc = subprocess.run(
            [PY, "-m", "wengao_ppt.cli", "--project-root", str(self.root), *args],
            capture_output=True, text=True, cwd=str(self.prog),
            env=env, timeout=timeout)
        return proc.returncode, last_json(proc.stdout or ""), \
            (proc.stdout or "")[-1500:], (proc.stderr or "")[-1500:]

    # ---------------------------------------------------------------- 真实落盘状态
    def job_dir(self, job_id):
        return self.root / "jobs" / job_id

    def job_json(self, job_id):
        return load_json(self.job_dir(job_id) / "job.json")

    def validation_json(self, job_id):
        return load_json(self.job_dir(job_id) / "validation.json")

    def is_cancelled(self, job_id):
        return (self.job_dir(job_id) / CANCELLED_MARK).exists()

    def current_attempt(self, job_id):
        job = self.job_json(job_id) or {}
        return job.get("attempt_id"), job.get("run_id"), job.get("theme")

    # ---------------------------------------------------------------- 编排推进
    def _advance(self, job_id, held_attempt, held_run):
        """按 precheck(已完成) → run → validate → publish → export 推进。

        返回最终 flow 快照。任何终态失败/取消立即停止。
        """
        # --- 阶段 2：run ---
        if self._guard(job_id, held_attempt, held_run):
            return self._flow(job_id)
        self._append(job_id, "run", "RUNNING")
        run_args = ["run", "--job-id", job_id, "--timeout", "900"]
        if (self._flow(job_id) or {}).get("allow_text_card_fallback"):
            run_args.append("--allow-text-card-fallback")
        rc, obj, out, err = self.cli(*run_args, timeout=STAGE_TIMEOUT["run"])
        self._append(job_id, "run", "DONE" if rc == 0 else "FAILED",
                     {"rc": rc, "tail": (out or err)[-400:]})
        if rc != 0:
            return self._flow(job_id)
        if self._guard(job_id, held_attempt, held_run):
            return self._flow(job_id)

        # --- 阶段 3：validate ---
        self._append(job_id, "validate", "RUNNING")
        rc, obj, out, err = self.cli("validate", "--job-id", job_id,
                                     timeout=STAGE_TIMEOUT["validate"])
        v = self.validation_json(job_id) or {}
        verdict = v.get("verdict") or (obj or {}).get("verdict")
        blocking = v.get("blocking_findings")
        self._append(job_id, "validate",
                     "DONE" if rc in (0, 2) else "ERROR",
                     {"rc": rc, "verdict": verdict,
                      "blocking_findings": blocking,
                      "release_ready": v.get("release_ready")})
        # 终态停止：FAILED / NEEDS_REVIEW 一律不进入 publish/export
        if verdict in STOP_VERDICTS:
            self._append(job_id, "stop", "STOPPED",
                         {"reason": f"verdict={verdict}，按要求停止推进，不发布不导出"})
            return self._flow(job_id)
        if self._guard(job_id, held_attempt, held_run):
            return self._flow(job_id)

        # --- 阶段 4：publish ---
        self._append(job_id, "publish", "RUNNING")
        rc, obj, out, err = self.cli("publish", "--job-id", job_id,
                                     timeout=STAGE_TIMEOUT["publish"])
        self._append(job_id, "publish", "DONE" if rc == 0 else "FAILED",
                     {"rc": rc, "tail": (out or err)[-300:]})
        if rc != 0:
            return self._flow(job_id)

        # --- 阶段 5：export（固定交付 + 清单登记哈希）---
        self._append(job_id, "export", "RUNNING")
        rc, obj, out, err = self.cli("export", "--job-id", job_id,
                                     timeout=STAGE_TIMEOUT["export"])
        job = self.job_json(job_id) or {}
        self._append(job_id, "export", "DONE" if rc == 0 else "FAILED",
                     {"rc": rc, "export_path": job.get("export_path")})
        self._append(job_id, "complete",
                     "COMPLETED" if rc == 0 else "FAILED", None)
        return self._flow(job_id)

    def _guard(self, job_id, held_attempt, held_run):
        """推进前的真实护栏：取消 / attempt 漂移 → 停止推进并登记。"""
        if self.is_cancelled(job_id):
            self._append(job_id, "guard", "STOPPED", {"reason": "任务已取消"})
            return True
        cur_attempt, cur_run, _ = self.current_attempt(job_id)
        if held_attempt and cur_attempt and cur_attempt != held_attempt:
            self._append(job_id, "guard", "STOPPED",
                         {"reason": f"attempt 漂移：编排持有 {held_attempt}，"
                                    f"落盘为 {cur_attempt}；迟到结果不得接管当前任务"})
            return True
        return False

    # ---------------------------------------------------------------- 受理
    def start(self, source: str, theme: str, allow_text_card_fallback: bool = False):
        """受理一次「开始制作」。重复点击返回既有编排，不新开。"""
        from wengao_ppt import theme as theme_mod
        theme = theme or theme_mod.DEFAULT_THEME_ID
        if theme not in theme_mod.THEMES:
            return {"accepted": False,
                    "error": {"code": "THEME.UNKNOWN",
                              "message": f"主题 {theme!r} 不在可信主题注册表内；"
                                         f"可用：{'、'.join(sorted(theme_mod.THEMES))}。"}}

        p = Path(source)
        if not p.is_absolute():
            p = self.root / source
            if not p.exists():
                p = self.prog / source
        p = p.resolve()
        # 来源白名单：样例目录（数据根或程序目录）或上传目录
        upload_dir = self.root / "workbench_uploads"
        allowed = False
        for base in (self.root / "samples", self.prog / "samples", upload_dir):
            try:
                p.relative_to(base.resolve())
                allowed = True
                break
            except ValueError:
                pass
        if not allowed:
            return {"accepted": False,
                    "error": {"code": "WB.BAD_SOURCE",
                              "message": "文件来源不在允许的样例/上传目录内。"}}
        if not p.is_file() or p.suffix.lower() != ".pptx":
            return {"accepted": False,
                    "error": {"code": "WB.BAD_SOURCE",
                              "message": "仅支持存在的 .pptx 原稿。"}}

        # 阶段 1：precheck（真实 CLI）。主题与输入在此刻冻结进 job 身份。
        rc, obj, out, err = self.cli("precheck", "--input", str(p),
                                     "--mode", "beautify", "--theme", theme,
                                     timeout=STAGE_TIMEOUT["precheck"])
        if rc != 0 or not obj or not obj.get("job_id"):
            return {"accepted": False,
                    "precheck": {"rc": rc, "gate": (obj or {}).get("gate"),
                                 "detail": (obj or {}).get("detail")
                                           or (out or err)[-500:]},
                    "error": {"code": "GATE.PRECHECK_FAILED",
                              "message": "预检未通过，未创建可制作任务。"}}
        job_id = obj["job_id"]

        # 重复点击保护：同一 job 已在编排中 → 返回既有编排
        existing = self._flow(job_id)
        if existing and existing.get("state") in ("RUNNING", "PENDING"):
            return {"accepted": True, "job_id": job_id, "theme": theme,
                    "resumed": True, "flow": existing,
                    "note": "该组合已在编排中，未重复启动。"}

        held_attempt, held_run, frozen_theme = self.current_attempt(job_id)
        self._set(job_id, state="RUNNING", stage="precheck",
                  history=[], finished_at=None,
                  theme=frozen_theme, held_attempt=held_attempt,
                  held_run=held_run, started_at=now_iso(), input_path=str(p),
                  allow_text_card_fallback=bool(allow_text_card_fallback))
        self._append(job_id, "precheck", "DONE",
                     {"rc": rc, "input_sha256": obj.get("input_sha256"),
                      "frozen_theme": frozen_theme})

        def _worker():
            try:
                self._advance(job_id, held_attempt, held_run)
            except Exception as e:  # noqa: BLE001
                self._append(job_id, "orchestrator", "ERROR",
                             {"error": f"{type(e).__name__}: {e}"})
            finally:
                with self._lock:
                    f = self._flows.get(job_id)
                    if f and f.get("state") == "RUNNING":
                        f["state"] = "DONE"
                        f["finished_at"] = now_iso()

        threading.Thread(target=_worker, daemon=True).start()
        return {"accepted": True, "job_id": job_id, "theme": theme,
                "resumed": False, "flow": self._flow(job_id)}

    # ---------------------------------------------------------------- 取消
    def cancel(self, job_id):
        """真实取消：走真实 CLI cancel，由 Runner 处理信号与终态登记。"""
        rc, obj, out, err = self.cli("cancel", "--job-id", job_id, timeout=180)
        self._append(job_id, "cancel", "REQUESTED", {"rc": rc})
        return {"cancel_rc": rc, "record": obj,
                "stdout_tail": (out or "")[-600:], "stderr_tail": (err or "")[-300:]}

    # ---------------------------------------------------------------- 只读快照
    def snapshot(self, job_id):
        """给前端读的真实状态：编排进程内状态 + runner 落盘状态。"""
        job = self.job_json(job_id) or {}
        v = self.validation_json(job_id) or {}
        flow = self._flow(job_id) or {"job_id": job_id, "state": "UNKNOWN",
                                      "history": []}
        scene_stage = next((x for x in reversed(job.get("stages") or [])
                            if x.get("stage") == "semantic_scenes"), None)
        scene_stage_info = None
        if scene_stage:
            detail = scene_stage.get("detail") or {}
            unresolved = detail.get("unresolved_scenes") or []
            scene_stage_info = {
                "status": scene_stage.get("status"),
                "at": scene_stage.get("at"),
                "failure_code": detail.get("code"),
                "failure_detail": detail.get("detail"),
                "model_error": detail.get("model_error"),
                "rules_version": detail.get("rules_version"),
                "detected_source_languages": [str(x) for x in
                                               detail.get("detected_source_languages", [])
                                               if isinstance(x, str) and x.isascii()
                                               and x.isalpha() and 2 <= len(x) <= 3],
                "detected_scene_count": len(detail.get("detected_scene_ids") or []),
                "planned_photo_count": detail.get("planned_photo_count"),
                "inserted": detail.get("inserted"),
                "image_pages": detail.get("image_pages") or [],
                "unresolved_scene_count": len(unresolved),
            }
        scene_gate = v.get("semantic_scene_gate") or {}
        scene_summary = scene_gate.get("summary") or {}
        struct_summary = scene_summary.get("structural") if isinstance(scene_summary.get("structural"), dict) else scene_summary
        pixel_summary = scene_summary.get("rendered_pixel_visibility") if isinstance(scene_summary.get("rendered_pixel_visibility"), dict) else {}
        scene_gate_info = None
        if scene_gate:
            gate_findings = scene_gate.get("findings") or []
            gate_unverified = scene_gate.get("unverified") or []
            scene_gate_info = {
                "status": scene_gate.get("status"),
                "blocking": scene_gate.get("blocking", True),
                "planned_scene_count": struct_summary.get("planned_scene_count"),
                "embedded_scene_picture_count": struct_summary.get(
                    "embedded_scene_picture_count"),
                "unique_embedded_photo_count": struct_summary.get(
                    "unique_embedded_photo_sha256_count"),
                "rendered_scene_count": pixel_summary.get("rendered_scene_count"),
                "findings": len(gate_findings),
                "blocking_findings": sum(1 for f in gate_findings
                                          if f.get("severity") == "BLOCKING"),
                "unverified": len(gate_unverified),
            }
        return {
            "job_id": job_id,
            "orchestration": flow,
            "runner": {
                "verdict": job.get("verdict"),
                "stage": job.get("stage"),
                "attempt_id": job.get("attempt_id"),
                "run_id": job.get("run_id"),
                "theme": job.get("theme"),
                "theme_name": job.get("theme_name"),
                "theme_version": job.get("theme_version"),
                "theme_config_digest": job.get("theme_config_digest"),
                "export_path": job.get("export_path"),
                "input_path": job.get("input_path"),
                "input_hash": job.get("input_hash"),
                "semantic_scene_stage": scene_stage_info,
            },
            "validation": {
                "verdict": v.get("verdict"),
                "blocking_findings": v.get("blocking_findings"),
                "total_findings": v.get("total_findings"),
                "release_ready": v.get("release_ready"),
                "render_code": (v.get("render") or {}).get("code"),
                "candidate_pptx_sha256": v.get("candidate_pptx_sha256"),
                "layout_hard_gate": v.get("layout_hard_gate"),
                "semantic_scene_gate": scene_gate_info,
            },
            "cancelled": self.is_cancelled(job_id),
            "auto_repair": "OFF（编排从不调用 repair 入口）",
        }
