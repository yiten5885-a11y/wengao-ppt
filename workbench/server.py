"""稳稿 PPT 本地工作台（D10-A）：仅监听本机回环地址的轻量页面 + 后端。

边界（任务书 §5）：
  - 只绑定 127.0.0.1，不公开部署；不暴露任意文件读写、命令执行或无约束外联。
  - 所有运行/验收/取消/导出都经**真实 CLI 入口**（python -m wengao_ppt.cli），
    页面只读 runner 落盘的真实状态（job.json / validation.json / findings.json /
    export_events.jsonl），不维护第二套成功/失败判定。
  - 自动修复（repair）不在本工作台暴露；不调用任何模型或生图服务。
  - 预览文件服务有严格白名单（任务候选 render 目录 / 原稿预览缓存目录），
    上传文件名经清洗，不直接拼接磁盘路径。

用法：
    python3 workbench/server.py [--port 8765]
然后浏览器打开 http://127.0.0.1:8765/
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROG = Path(__file__).resolve().parents[1]          # 程序目录（代码/契约/样例）
# 数据根（jobs/results/uploads）：优先 WENGAO_DATA_ROOT（与 wengao_ppt 其余模块同一口径），
# 其次由 main() 的 --project-root 覆盖，最后才回落到程序目录。
# 旧实现直接等于程序目录，且在 WB = Workbench() 时 mkdir，于是任何一次 import
# 都会把 workbench_uploads/ 与 workbench_previews/ 写进冻结的版本目录。
ROOT = Path(os.environ.get("WENGAO_DATA_ROOT") or PROG)
sys.path.insert(0, str(PROG))
PY = sys.executable
UPLOAD_DIR = ROOT / "workbench_uploads"
PREVIEW_DIR = ROOT / "workbench_previews"
MAX_UPLOAD = 64 * 1024 * 1024
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
FILE_NAME = re.compile(r"^(?:png/page-\d+\.png|[A-Za-z0-9._-]+\.pdf)$")


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat()


# 回环地址绝不走系统代理：工作台及其子进程（材料 CLI、场景模型、ComfyUI）
# 大量调用 http://127.0.0.1:*；若服务带着 HTTP(S)_PROXY 启动，这些调用会被
# 代理拦截（2026-10-27 SSLEOFError）。只豁免回环，外网请求仍按既有代理配置。
for _lp_k in ("no_proxy", "NO_PROXY"):
    _lp_v = os.environ.get(_lp_k, "")
    if "127.0.0.1" not in _lp_v:
        _lp_hosts = "127.0.0.1,localhost,::1"
        os.environ[_lp_k] = (f"{_lp_v},{_lp_hosts}" if _lp_v else _lp_hosts)
del _lp_k, _lp_v


def load_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _pid_alive(pid) -> bool:
    """进程是否存活（只用于判断锁/pid 文件是否仍代表一个写者）。"""
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:  # noqa: BLE001
        return False
    return True


#: 材料链路的**规范拒绝**判定（与 /api/material/from-material 的 REFUSAL_VERDICTS
#: 同一口径）：生成器按契约拒绝不是进程崩溃。任务面板必须给出具体拒绝原因，
#: 而不是把 rc!=0 一律标成"任务进程意外退出"（2026-10-27 mt-c4b795f2 误报根因）。
MT_REFUSAL_VERDICTS = {"PLAN_FAILED", "PLAN_ASSET_INVALID",
                       "PAGE_RANGE_UNSATISFIED", "PAGE_COUNT_MISMATCH",
                       "CANCELLED"}


def _mt_refusal_reason(rr: dict) -> str:
    """把 run_record 里的规范拒绝翻译成给用户看的具体原因。"""
    verdict = rr.get("verdict")
    if verdict == "PAGE_RANGE_UNSATISFIED":
        c = rr.get("page_range_conflict") or {}
        rng = c.get("range") or ["?", "?"]
        problems = "；".join(str(p) for p in (c.get("problems") or []))
        return (f"材料按契约拒绝：分页后 {c.get('pages')} 页，"
                f"超出冻结区间 {rng[0]}—{rng[1]}（{problems}）。"
                "材料全量必讲内容无法落入该页数区间，已按契约留痕拒绝，"
                "详见运行记录 page_range_conflict / fact_budget。")
    if verdict == "PLAN_ASSET_INVALID":
        return "材料按契约拒绝：计划声明了无受控资产的 image_caption 页。"
    if verdict == "PLAN_FAILED":
        err = str((rr.get("plan") or {}).get("error") or "")
        return f"材料按契约拒绝：内容计划失败。{err}" if err else \
            "材料按契约拒绝：内容计划失败。"
    if verdict == "PAGE_COUNT_MISMATCH":
        return (f"材料按契约拒绝：构建页数与计划不一致。"
                f"{rr.get('page_count_source') or ''}")
    return f"材料按契约拒绝：{verdict or '未知判定'}"


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


def child_env(root: Path) -> dict:
    """子进程环境：程序从 PROG 导入、数据落 ROOT。

    安装布局下 cwd 可能是数据根（没有 wengao_ppt/），因此必须显式把 PROG 放进
    PYTHONPATH，并把数据根通过 WENGAO_DATA_ROOT 传给产品；否则 `-m wengao_ppt.cli`
    在数据根里找不到模块（旧实现只在开发布局下碰巧成立）。
    """
    e = {k: v for k, v in os.environ.items() if k != "PYTHONHOME"}
    e["PYTHONPATH"] = str(PROG)
    e["PYTHONDONTWRITEBYTECODE"] = "1"
    e["WENGAO_DATA_ROOT"] = str(root)
    # Do not pin every child to the optional LocalQwen bridge. When no explicit
    # endpoint is configured, scene_model._loopback_endpoint() probes the
    # existing loopback bridge and Ollama API, then uses the live local service.
    # Hard-coding 18888 here bypassed that discovery and made the Workbench
    # fail even while the configured Qwen model was already serving on 11434.
    if "WENGAO_LOCAL_MODEL_ENDPOINT" in os.environ:
        e["WENGAO_LOCAL_MODEL_ENDPOINT"] = os.environ["WENGAO_LOCAL_MODEL_ENDPOINT"]
    else:
        e.pop("WENGAO_LOCAL_MODEL_ENDPOINT", None)
    return e


def cli(root: Path, *args, timeout=1200):
    """真实 CLI 入口。返回 (returncode, parsed_last_json, stdout_tail, stderr_tail)。"""
    proc = subprocess.run(
        [PY, "-m", "wengao_ppt.cli", "--project-root", str(root), *args],
        capture_output=True, text=True, cwd=str(PROG),
        env=child_env(root), timeout=timeout)
    return proc.returncode, last_json(proc.stdout), proc.stdout[-2000:], proc.stderr[-2000:]


def sha256_file(p: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


class Workbench:
    """进程内只保存"哪个操作在进行中"；权威状态一律读 runner 落盘文件。"""

    def __init__(self):
        self.ops = {}                      # job_id -> {op, state, pid, started_at, ...}
        self.ops_lock = threading.Lock()
        # 不在这里 mkdir：目录由写入点按需创建，main() 启动时按真实数据根创建。
        # 否则 WB = Workbench() 的 import 副作用会污染程序版本目录。

    # ---------------- 操作单飞 ----------------
    def op_busy(self, job_id):
        with self.ops_lock:
            op = self.ops.get(job_id)
            return op if op and op.get("state") == "running" else None

    def op_start(self, job_id, op, pid=None):
        with self.ops_lock:
            self.ops[job_id] = {"op": op, "state": "running", "pid": pid,
                                "started_at": now_iso()}

    def op_finish(self, job_id, **kw):
        with self.ops_lock:
            op = self.ops.get(job_id) or {}
            op.update(kw)
            op["state"] = "done"
            op["finished_at"] = now_iso()
            self.ops[job_id] = op

    def writer_owner(self, job_id):
        """跨进程写者探测：锁文件或 worker.pid 里记录的进程是否仍存活。

        内存里的 ops 只覆盖**本服务进程**；另一个服务实例或直接跑的 CLI 不会被它看见。
        因此这里读真实的锁/pid 文件，把"谁在写"作为 HTTP 拒绝依据。
        """
        d = ROOT / "jobs" / job_id
        for name, purpose_default in ((".run.lock", None), ("worker.pid", "run")):
            f = d / name
            if not f.is_file():
                continue
            pid, purpose = None, purpose_default
            try:
                parts = f.read_text(encoding="utf-8").split()
                pid = int(parts[0])
                if len(parts) > 2:
                    purpose = parts[2]
            except Exception:  # noqa: BLE001
                pid = None
            if pid is None:
                return {"pid": None, "purpose": purpose, "file": name,
                        "note": "文件存在但无法解析 pid；按可能有写者处理"}
            if pid != os.getpid() and _pid_alive(pid):
                return {"pid": pid, "purpose": purpose, "file": name}
        return None

    def spawn(self, job_id, op, args):
        """以后台 CLI 子进程启动操作；**子进程确认取得写入权后才算已启动**。"""
        busy = self.op_busy(job_id)
        if busy:
            return {"error": {"code": "WB.OP_IN_PROGRESS",
                              "message": f"该任务已有进行中的操作（{busy['op']}），"
                                         "请等它结束或先取消。"}}
        owner = self.writer_owner(job_id)
        if owner:
            return {"error": {"code": "WB.JOB_BUSY",
                              "message": f"该任务已有活动写入进程（pid {owner.get('pid')}"
                                         f"{'，' + str(owner.get('purpose')) if owner.get('purpose') else ''}）；"
                                         "未启动第二个写入者。请等它结束或先取消。",
                              "owner": owner}}
        proc = subprocess.Popen(
            [PY, "-m", "wengao_ppt.cli", "--project-root", str(ROOT), *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=str(PROG), env=child_env(ROOT))
        # 接收边界：worker 真正拿到写入权（写出 worker.pid / 锁）或进程已退出，才判定结果。
        # 不能"Popen 成功"就当任务已开始 —— 否则重复 run 会先 202，再在后台静默失败。
        jd = ROOT / "jobs" / job_id
        deadline = time.time() + 3.0
        while time.time() < deadline and proc.poll() is None:
            if (jd / "worker.pid").is_file() or (jd / ".run.lock").is_file():
                break
            time.sleep(0.05)
        if proc.poll() is not None and proc.returncode != 0:
            out, err = proc.communicate()
            payload = last_json(out or "") or {}
            code = payload.get("gate") or (payload.get("error") or {}).get("code") or "WB.OP_FAILED"
            if code == "GATE.ATTEMPT_CONFLICT":
                owner2 = self.writer_owner(job_id) or {}
                return {"error": {"code": "WB.JOB_BUSY",
                                  "message": "该任务已有活动写入进程；本次未启动第二个写入者。",
                                  "detail": payload.get("detail"),
                                  "owner": owner2}}
            return {"error": {"code": code,
                              "message": payload.get("detail") or (err or "")[-300:]}}
        self.op_start(job_id, op, pid=proc.pid)

        def _wait():
            out, err = proc.communicate()
            self.op_finish(job_id, returncode=proc.returncode,
                           result=last_json(out or ""),
                           stdout_tail=(out or "")[-2000:], stderr_tail=(err or "")[-2000:])

        threading.Thread(target=_wait, daemon=True).start()
        return {"started": True, "op": op, "pid": proc.pid, "job_id": job_id}

    # ---------------- 任务状态（全部来自真实落盘文件）----------------
    @staticmethod
    def _reap_abandoned(job_id=None):
        """读状态前先收尾"worker 已死却还停在 RUNNING"的任务。

        契约 GATE.INTERRUPTED 要求进程中断后不得长期停留 RUNNING。worker 被
        kill、崩溃或机器重启时没有人改写 job.json，界面就会一直显示"制作中"。
        在这里顺带回收，读到的状态才是真的。清理失败不影响状态读取。
        """
        try:
            from wengao_ppt.runner import Runner
            r = Runner(ROOT)
            if job_id:
                return r.reap_abandoned_run(job_id)
            return r.reap_all_abandoned_runs()
        except Exception:  # noqa: BLE001 — 清理是尽力而为，不能挡住状态查询
            return None

    def job_state(self, job_id):
        d = ROOT / "jobs" / job_id
        self._reap_abandoned(job_id)
        job = load_json(d / "job.json")
        if job is None:
            return None
        v = load_json(d / "validation.json")
        findings = load_json(d / "findings.json")
        theme_stats = load_json(d / "candidate" / "theme_applied.json")
        census = load_json(d / "raw_census.json")
        events = []
        ev = d / "export_events.jsonl"
        if ev.exists():
            events = [json.loads(x) for x in ev.read_text(encoding="utf-8").splitlines()
                      if x.strip()][-10:]
        cand_render = d / "candidate" / "render"
        pngs = sorted(p.name for p in (cand_render / "png").glob("*.png")) \
            if (cand_render / "png").exists() else []
        from wengao_ppt import incident as incident_mod
        try:
            delivery = incident_mod.delivery_state(d, ROOT)
        except Exception as e:  # noqa: BLE001
            delivery = {"allowed": False, "code": "EVIDENCE.UNREADABLE",
                        "reasons": [{"code": "EVIDENCE.UNREADABLE",
                                     "detail": f"{type(e).__name__}: {e}"}]}
        meta = load_json(cand_render / "render_meta.json") or {}
        render_binding = {
            "bound": bool(meta),
            "candidate_sha256": meta.get("candidate_sha256"),
            "pdf_sha256": meta.get("pdf_sha256"),
            "glyph_quality": meta.get("glyph_quality"),
            "fontconfig_sha256": ((meta.get("font_environment") or {})
                                  .get("effective_conf_sha256")),
            "pdf_fonts": meta.get("pdf_fonts"),
            "reasons": ([] if meta.get("candidate_sha256") ==
                        (sha256_file(d / "candidate" / (Path(job.get("input_path") or "").stem
                                                        + ".themed.pptx"))
                         if (d / "candidate" / (Path(job.get("input_path") or "").stem
                                                + ".themed.pptx")).is_file() else None)
                        else ["页图没有绑定到当前候选（旧图或未渲染）"]),
        }
        with self.ops_lock:
            op = self.ops.get(job_id)
        return {
            # A9：三种状态接口（/api/jobs/<id>、/api/jobs、/api/history）此前形状不一：
            # 这里顶层没有 verdict/stage，调用方要各自去 job.verdict 里取。
            # 顶层镜像一份（不删 job 里的原字段，避免破坏既有调用方），
            # 语义固定为 job.json 的登记值；验收判定另见 validation.verdict。
            "job_id": job.get("job_id") or job_id,
            "verdict": job.get("verdict"),
            "stage": job.get("stage"),
            "attempt_id": job.get("attempt_id"),
            "export_path": job.get("export_path"),
            "export_ready": bool(job.get("export_path")),
            "verdict_source": "job.json",
            "job": job,
            "validation": v,
            "findings": findings,
            "theme_applied": theme_stats,
            "raw_census": census,
            "export_events": events,
            "candidate_png_pages": pngs,
            "delivery": delivery,
            "render_binding": render_binding,
            "theme_application": (theme_stats or {}).get("theme_application"),
            "preserve_design": bool(job.get("preserve_design")),
            "candidate_pdf": (cand_render / "png").exists() and
            sorted(cand_render.glob("*.pdf"))[0].name if cand_render.exists()
            and sorted(cand_render.glob("*.pdf")) else None,
            "op": op,
        }

    def jobs_list(self):
        out = []
        jd = ROOT / "jobs"
        if not jd.exists():
            return out
        self._reap_abandoned()
        for d in sorted(jd.iterdir(), key=lambda p: p.name):
            job = load_json(d / "job.json")
            if not job:
                continue
            v = load_json(d / "validation.json")
            try:
                from wengao_ppt import incident as incident_mod
                dl = incident_mod.delivery_state(d, ROOT)
                dl_view = {"allowed": dl["allowed"], "code": dl["code"]}
            except Exception as e:  # noqa: BLE001
                dl_view = {"allowed": False, "code": "EVIDENCE.UNREADABLE",
                           "detail": f"{type(e).__name__}: {e}"}
            out.append({
                "job_id": job.get("job_id"), "run_id": job.get("run_id"),
                "attempt_id": job.get("attempt_id"), "mode": job.get("mode"),
                "theme": job.get("theme"), "theme_name": job.get("theme_name"),
                "theme_version": job.get("theme_version"),
                "theme_config_digest": job.get("theme_config_digest"),
                "verdict": job.get("verdict"),
                "stage": job.get("stage"), "created_at": job.get("created_at"),
                "last_activity": job.get("last_activity"),
                "input_path": job.get("input_path"),
                "input_hash": (job.get("input_hash") or "")[:16],
                "validation_verdict": (v or {}).get("verdict"),
                "delivery": dl_view,
                "export_path": job.get("export_path"),
            })
        out.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        return out

    # ---------------- 导出结果索引（用于下载绑定）----------------
    def export_index(self, job_id):
        """读取该任务**最近一次成功导出**的目录与其清单，返回 (导出目录, {相对名: 预期哈希})。

        预期哈希只取自导出清单（release_manifest.json / manifest.json），
        不重新信任"当前磁盘上的候选"。清单里没有登记的文件一律不可下载。
        """
        d = ROOT / "jobs" / job_id
        job = load_json(d / "job.json")
        if not job:
            return None, None, "WB.JOB_NOT_FOUND", "任务不存在。"
        # 交付资格先于"导出目录存在"：事故里导出目录与清单都还在、哈希也一致，
        # 但结果本身不可读——只按目录/哈希判断会把坏结果继续当成品发出去。
        from wengao_ppt import incident as incident_mod
        state = incident_mod.delivery_state(d, ROOT)
        if not state["allowed"]:
            return (None, None, state["code"] or "EXPORT.NOT_CURRENT",
                    "；".join(x.get("detail", "") for x in state["reasons"]))
        outdir = job.get("export_path")
        if not outdir:
            return None, None, "EXPORT.NOT_READY", "该任务尚无成功导出结果；先完成验收并导出。"
        base = Path(outdir)
        if not base.is_dir():
            return None, None, "EXPORT.NOT_READY", "导出目录当前不可用。"
        man = load_json(base / "release_manifest.json") or load_json(base / "manifest.json")
        if not man:
            return None, None, "EXPORT.MANIFEST_MISSING", "导出目录缺少清单，无法绑定下载。"
        out = (man.get("output") or {})
        ra = (man.get("render_artifacts") or {})
        binds = {b.get("page_no"): b.get("png_sha256")
                 for b in (ra.get("png_bindings") or [])}
        index = {}
        if out.get("pptx") and out.get("pptx_sha256"):
            index[Path(out["pptx"]).name] = {"kind": "pptx", "expected": out["pptx_sha256"]}
        pdf_rel = ra.get("pdf")
        if pdf_rel and ra.get("pdf_sha256"):
            index[Path(pdf_rel).name] = {"kind": "pdf", "expected": ra["pdf_sha256"]}
        for rel in (ra.get("png") or []):
            pn = re.search(r"(\d+)", Path(rel).stem)
            exp = binds.get(int(pn.group(1))) if pn else None
            index[rel] = {"kind": "png", "expected": exp}
        return base, index, None, None

    # ---------------- 原稿预览（真实渲染，按输入哈希缓存）----------------
    def original_preview(self, job_id):
        d = ROOT / "jobs" / job_id
        job = load_json(d / "job.json")
        if not job:
            return None
        src = Path(job["input_path"])
        if not src.exists():
            return {"error": {"code": "INPUT.SOURCE_BASELINE_MISSING",
                              "message": "原稿文件当前不可用。"}}
        h = sha256_file(src)[:16]
        cache = _preview_cache_dir("original", src)
        pdf = cache / (src.stem + ".pdf")
        if pdf.exists():
            pngs = sorted(p.name for p in (cache / "png").glob("*.png")) \
                if (cache / "png").exists() else []
            return {"cached": True, "input_sha256_16": h, "pdf": pdf.name,
                    "pages": pngs}
        from wengao_ppt import render as render_mod
        if not render_mod.real_render_available():
            return {"error": {"code": "RENDER.NOT_AVAILABLE",
                              "message": "本机未找到 LibreOffice，无法生成原稿预览。"}}
        cache.mkdir(parents=True, exist_ok=True)
        rr = render_mod.render_real(src, cache, data_root=ROOT)
        pngs = sorted(p.name for p in (cache / "png").glob("*.png")) \
            if (cache / "png").exists() else []
        return {"cached": False, "input_sha256_16": h, "code": rr.get("code"),
                "renderer": rr.get("soffice_version"), "pdf": pdf.name if pdf.exists() else None,
                "pages": pngs, "detail": rr.get("detail")}


WB = Workbench()

# D10-C WP2：「开始制作」受控编排（服务端状态机，复用真实 CLI runner）
try:
    from .make_flow import MakeOrchestrator  # noqa: E402
except ImportError:
    from make_flow import MakeOrchestrator  # noqa: E402

MAKE = MakeOrchestrator(ROOT, PROG)


# ================================================================ 实例 / 材料 / 编辑 / 历史
# 约定：本区块只做“薄接口”——参数校验、调用既有 CLI 或既有 local_edit 函数、
# 读取真实落盘结果；不新增第二套 runner、不自行判定成功。
MATERIAL_TASKS: dict = {}
MT_LOCK = threading.Lock()
MATERIAL_PAGES = (10, 20)
MATERIAL_SUFFIXES = (".md", ".markdown", ".txt", ".text", ".docx")


def _instance_info() -> dict:
    """实例自述：开发实例还是安装实例、程序/数据根、构建身份、任务数。

    监督 2026-10-20 15:12 实证修复：旧实现仅凭 INSTALL_RECORD.json **存在**
    就判定"安装实例"——候选开发树里遗留的 v30r4-design 旧记录把开发候选
    误判成安装实例并继承旧身份（label/build_id 都不是当前候选）。
    判定改依**安装布局**（PROG 位于 versions/ 之下）；记录存在但其
    build_id 与当前程序聚合不一致时如实标 MISMATCH——不继承、不掩盖。
    build_id 对两类实例都计算：install.build_id 已排除数据根/日志/渲染
    profile 等运行期成员（wb_loopback_data、jobs/、reports/、*.log…），
    程序成员聚合稳定，连续两次只读访问得到同一身份（监督复现口径）。
    """
    from wengao_ppt import __version__
    from wengao_ppt.install import build_id
    rec = load_json(PROG / "INSTALL_RECORD.json") or {}
    installed = PROG.parent.name == "versions"
    bid = None
    try:
        bid = build_id(PROG)
    except Exception:  # noqa: BLE001
        bid = None
    rec_state = None
    if rec:
        rec_bid = rec.get("build_id")
        if bid and rec_bid:
            rec_state = "MATCH" if rec_bid == bid else "MISMATCH"
        else:
            rec_state = "PRESENT_UNVERIFIED"
    jobs = len(list((ROOT / "jobs").glob("job-*"))) if (ROOT / "jobs").is_dir() else 0
    return {
        "kind": "installed" if installed else "development",
        "kind_label": "安装实例" if installed else "开发实例",
        "label": PROG.name,
        "build_id": bid,
        "build_id_scope": ("程序成员聚合（代码/资源/契约/vendor/样例/工作台 UI；"
                           "已排除数据根 wb_loopback_data、jobs/、reports/、"
                           "workbench_uploads/previews、*.log 等运行期文件）"),
        "runner_version": __version__,
        "program_dir": str(PROG),
        "data_root": str(ROOT),
        "data_root_is_program": str(ROOT) == str(PROG),
        "python": sys.executable,
        "jobs": jobs,
        "install_record": ({k: rec.get(k) for k in
                            ("label", "build_id", "installed_at", "source_package")}
                           if rec else None),
        "install_record_state": rec_state,
        "note": ("安装实例使用安装根的 data/；初始没有任务是正常现象，不代表历史丢失。"
                 if installed else
                 "开发实例使用开发项目根作为数据根；与安装实例的数据互不影响。"),
    }


def _replay_packs() -> list:
    idx = load_json(PROG / "samples" / "material_replay" / "index.json") or {}
    out = []
    for e in idx.get("packs") or []:
        mp = PROG / "samples" / "material_replay" / str(e.get("material"))
        pp = PROG / "samples" / "material_replay" / str(e.get("plan"))
        if mp.is_file() and pp.is_file():
            out.append({"id": e.get("id"), "name": e.get("name"),
                        "material": mp.name, "plan": pp.name,
                        "pages": e.get("pages"), "note": e.get("note"),
                        "plan_sha256": sha256_file(pp)})
    return out


def _mt_dir() -> Path:
    d = ROOT / "material_tasks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _mt_save(rec: dict) -> None:
    (_mt_dir() / f"{rec['task_id']}.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")


def _mt_recent_timestamp(value, *, seconds: float = 45.0) -> bool:
    """Return true for a recent ISO timestamp used to bridge worker handoff races."""
    if not isinstance(value, str) or not value:
        return False
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds()
        return -5.0 <= age <= seconds
    except (TypeError, ValueError, OverflowError):
        return False


def _mt_downstream_recently_started(rec: dict, linked_job: dict | None) -> bool:
    """Do not fail a material task while its runner child is being handed off.

    The from-material CLI exits before the asynchronous run/validate/export CLI
    child has necessarily written its lock. A fresh runner activity or a fresh
    running advance event is therefore an active handoff, not a dead worker.
    """
    if not isinstance(linked_job, dict) or linked_job.get("verdict") not in (
            "QUEUED", "RUNNING"):
        return False
    if _mt_recent_timestamp(linked_job.get("last_activity")):
        return True
    for event in reversed(rec.get("advance") or []):
        if (event.get("stage") in ("run", "validate", "export")
                and event.get("state") == "running"):
            return _mt_recent_timestamp(event.get("at"))
    return False


def _mt_load(tid: str):
    p = _mt_dir() / f"{tid}.json"
    if not p.is_file():
        return None
    rec = load_json(p)
    if not isinstance(rec, dict):
        return None

    modified = False
    job_id = rec.get("job_id")
    linked_job = None
    if isinstance(job_id, str) and re.fullmatch(r"job-[A-Za-z0-9_-]+", job_id):
        jd = ROOT / "jobs" / job_id
        if (jd / "job.json").is_file():
            linked_job = load_json(jd / "job.json")

    # 1. 以 runner 的权威终态驱动 API：若 runner job 已终结（FAILED / CANCELLED），API 不得继续显示 running
    if linked_job and isinstance(linked_job, dict):
        runner_verdict = linked_job.get("verdict")
        if runner_verdict in ("FAILED", "CANCELLED"):
            if rec.get("state") == "running" or rec.get("verdict") != runner_verdict:
                rec["state"] = "failed" if runner_verdict == "FAILED" else "cancelled"
                rec["verdict"] = runner_verdict
                rec["finished_at"] = rec.get("finished_at") or linked_job.get("run_finished_at") or now_iso()
                rec["fail_reason"] = linked_job.get("fail_reason") or rec.get("fail_reason")
                reason = (linked_job.get("failure_detail")
                          or linked_job.get("fail_reason")
                          or rec.get("stopped_reason")
                          or f"下游流水线已终结（{runner_verdict}）")
                rec["stopped_reason"] = reason
                adv = rec.setdefault("advance", [])
                if adv and adv[-1].get("state") == "running":
                    adv[-1]["state"] = "failed" if runner_verdict == "FAILED" else "cancelled"
                    adv[-1]["rc"] = 1
                if not adv or adv[-1].get("stage") != "stop":
                    adv.append({
                        "stage": "stop",
                        "at": now_iso(),
                        "state": "stop",
                        "reason": reason,
                    })
                modified = True

    # 2. 存活性与死锁检测：材料 CLI 退出与 runner 子进程建锁之间有一个
    # 短暂交接窗口。锁存活或最近的 RUNNING 活动都要覆盖这个窗口；仅在
    # 进程、锁和宽限期全部消失后才纠正为失败。
    pid_alive = _pid_alive(rec.get("pid")) if rec.get("state") in ("running", "failed") else False
    job_active = False
    if linked_job and job_id:
        jd = ROOT / "jobs" / job_id
        lock_file = jd / ".run.lock"
        if lock_file.is_file():
            try:
                lock_pid = int(lock_file.read_text(encoding="utf-8").split()[0])
                if _pid_alive(lock_pid):
                    job_active = True
            except Exception:
                pass

    active_handoff = _mt_downstream_recently_started(rec, linked_job)
    false_exit = (rec.get("stopped_reason") == "任务进程意外退出"
                  and rec.get("state") == "failed")
    if false_exit and (job_active or active_handoff):
        # A prior status poll may have observed the gap before .run.lock existed.
        # Recover only this exact synthetic failure when the runner is still live.
        rec["state"] = "running"
        rec.pop("finished_at", None)
        rec.pop("stopped_reason", None)
        if rec.get("fail_reason") in (None, "GATE.WORKER_EXITED"):
            rec.pop("fail_reason", None)
        rec["advance"] = [
            event for event in (rec.get("advance") or [])
            if not (event.get("stage") == "stop"
                    and event.get("reason") == "任务进程意外退出")
        ]
        modified = True

    hb_stale = True
    _hb = rec.get("heartbeat_at")
    if _hb:
        try:
            hb_stale = (datetime.now(timezone.utc).astimezone()
                        - datetime.fromisoformat(str(_hb))).total_seconds() > 90
        except Exception:  # noqa: BLE001
            hb_stale = True
    if (rec.get("state") == "running" and not pid_alive and not job_active
            and not active_handoff and hb_stale):
        # worker 进程与下游流水线均已退出，且没有刚启动的 runner 交接，纠正为终态。
        # 心跳宽限 90 秒：CLI 刚退出、收尾线程尚未写回终态的窗口内不得误判为
        # "任务进程意外退出"（runner 心跳间隔约 2 秒，收尾只需写几个文件）。
        if linked_job and linked_job.get("verdict") in ("FAILED", "CANCELLED"):
            runner_verdict = linked_job.get("verdict")
            rec["state"] = "failed" if runner_verdict == "FAILED" else "cancelled"
            rec["verdict"] = runner_verdict
            rec["fail_reason"] = linked_job.get("fail_reason") or rec.get("fail_reason")
            reason = (linked_job.get("failure_detail")
                      or linked_job.get("fail_reason")
                      or rec.get("stopped_reason")
                      or "下游任务执行失败")
            rec["stopped_reason"] = reason
        else:
            rec["state"] = "failed"
            rec["verdict"] = rec.get("verdict") or "FAILED"
            rec["stopped_reason"] = rec.get("stopped_reason") or "任务进程意外退出"
        rec["finished_at"] = rec.get("finished_at") or now_iso()
        adv = rec.setdefault("advance", [])
        if adv and adv[-1].get("state") == "running":
            adv[-1]["state"] = "failed"
            adv[-1]["rc"] = 1
        if not adv or adv[-1].get("stage") != "stop":
            adv.append({
                "stage": "stop",
                "at": now_iso(),
                "state": "stop",
                "reason": rec["stopped_reason"],
            })
        modified = True

    if modified:
        _mt_save(rec)
    return rec


def _mt_list() -> list:
    out = []
    for p in sorted(_mt_dir().glob("mt-*.json")):
        r = _mt_load(p.stem)
        if r:
            out.append(r)
    out.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return out[:20]


def _mt_stage(rec: dict) -> dict:
    """进度只来自真实产物/真实阶段：台账 → 计划 → PPTX → 运行记录 → 运行/验收/导出。"""
    out = Path(rec.get("outdir") or "")
    running = rec.get("state") == "running"
    failed = rec.get("state") in ("failed", "cancelled")
    has_ledger = (out / "fact_ledger.json").is_file()
    has_plan = (out / "content_plan.json").is_file()
    pptx = sorted(out.glob("*.generated.pptx")) if out.is_dir() else []
    rec_path = out / "run_record.json"
    rr = load_json(rec_path) if rec_path.is_file() else None
    if rr:
        stage, pct = "已写出运行记录", 95
    elif pptx:
        stage, pct = "已生成 PPTX，正在写运行记录", 85
    elif has_plan:
        stage, pct = "构建原生 PPTX", 55
    elif has_ledger:
        stage, pct = ("回放冻结计划" if rec.get("mode") == "replay"
                      else "生成内容计划（本地模型）"), 30
    else:
        stage, pct = "提取材料并冻结必讲事实", 10
    adv = rec.get("advance") or []
    if adv:
        last = adv[-1]
        step = last.get("stage")
        if step in ("run", "validate", "export"):
            label = {"run": "渲染候选并核对对象",
                     "validate": "来源与契约核对",
                     "export": "固定交付导出"}[step]
            stage, pct = label, {"run": 70, "validate": 86, "export": 94}[step]
        elif step == "stop":
            stage, pct = "已停止推进：" + str(last.get("reason") or ""), 95
    if rec.get("cancel_requested_at") and running:
        stage += "（已登记取消，等待闸门）"
    elif failed:
        pct = 0
        if rec.get("state") == "cancelled":
            stage = "任务已取消" + (f"：{rec.get('stopped_reason')}" if rec.get('stopped_reason') else "")
        else:
            reason = rec.get("stopped_reason") or rec.get("fail_reason") or "任务执行失败"
            stage = f"任务失败：{reason}"
    return {"stage": stage,
            "percent": pct if running else (100 if rec.get("state") == "done" else 0),
            "facts_frozen": (rr or {}).get("fact_ledger", {}).get("count") if rr else None,
            "plan_mode": ((rr or {}).get("plan") or {}).get("mode"),
            # 判定以任务最终登记为准；run_record 里的 verdict 只反映“生成段”的初始判定
            "verdict": rec.get("verdict") or (rr or {}).get("verdict"),
            "generation_verdict": (rr or {}).get("verdict"),
            "advance": adv[-6:],
            "generated_pptx_abs": str(pptx[0]) if pptx else None}


def _mt_public(rec: dict) -> dict:
    st = _mt_stage(rec)
    elapsed = None
    try:
        from datetime import datetime as _dt
        started = rec.get("created_at")
        if started:
            t0 = _dt.fromisoformat(started)
            t1 = _dt.fromisoformat(rec["finished_at"]) if rec.get("finished_at") else _dt.now(t0.tzinfo)
            elapsed = round((t1 - t0).total_seconds(), 1)
    except Exception:  # noqa: BLE001
        pass
    auto = bool(rec.get("auto_advance"))
    # 材料任务在首次自动核对遇到原生证据缺口后会停下。用户补入真实录像并
    # 重新验证/导出时，材料卡片必须读取同一 job 的最新权威状态，而非一直
    # 显示首次 NEEDS_REVIEW。只有生成 PPTX、Runner 输入身份和来源哈希完全
    # 相同的关联才可用于状态合并。
    job_id = rec.get("job_id")
    linked_validation = None
    linked_job = None
    linked = False
    if isinstance(job_id, str) and re.fullmatch(r"job-[A-Za-z0-9_-]+", job_id):
        jd = ROOT / "jobs" / job_id
        linked_job = load_json(jd / "job.json")
        linked_validation = load_json(jd / "validation.json")
        source = Path((linked_job or {}).get("input_path") or "")
        if source and not source.is_absolute():
            source = PROG / source
        source = source.resolve() if source else source
        candidate = (jd / "candidate" /
                     (Path((linked_job or {}).get("input_path") or "").stem
                      + ".themed.pptx"))
        generated_sha = rec.get("generated_sha256")
        linked = bool(
            isinstance(linked_job, dict) and isinstance(linked_validation, dict)
            and source.is_file() and candidate.is_file() and generated_sha
            and linked_job.get("input_hash") == generated_sha
            and sha256_file(source) == generated_sha
            and linked_validation.get("input_sha256") == generated_sha
            and linked_validation.get("candidate_pptx_sha256")
            == sha256_file(candidate))
    # 以 runner 的权威终态驱动：若 runner job 存在且有明确终态（如 FAILED / CANCELLED）
    runner_terminal_verdict = None
    if isinstance(linked_job, dict) and linked_job.get("verdict") in ("FAILED", "CANCELLED", "DEVELOPMENT_CANDIDATE", "NEEDS_REVIEW"):
        runner_terminal_verdict = linked_job.get("verdict")

    effective_verdict = (linked_validation or {}).get("verdict") if linked else None
    if not effective_verdict:
        effective_verdict = runner_terminal_verdict or rec.get("verdict")

    effective_export_path = ((linked_job or {}).get("export_path") if linked else None)         or rec.get("export_path")
    layout_gate = ((linked_validation or {}).get("layout_hard_gate") if linked else None)
    if not isinstance(layout_gate, dict):
        layout_gate = ((rec.get("validation") or {}).get("layout_hard_gate") or {})

    # 严禁将失败结果标成可下载：
    # 只有当任务状态为 done、exit code 为 0、且权威 verdict 明确为 DEVELOPMENT_CANDIDATE（auto模式）
    # 或 GENERATED_PENDING_VALIDATION（非 auto 模式且下游无失败）、且确实产生导出文件时，才允许 download_ready
    ok_verdicts = ("DEVELOPMENT_CANDIDATE",) if auto else ("GENERATED_PENDING_VALIDATION",)
    is_terminal_failure = (rec.get("state") in ("failed", "cancelled")
                           or effective_verdict in ("FAILED", "CANCELLED")
                           or (linked_job and linked_job.get("verdict") in ("FAILED", "CANCELLED")))
    ok = (rec.get("state") == "done" and rec.get("returncode") == 0
          and bool(rec.get("generated_pptx"))
          and effective_verdict in ok_verdicts
          and not is_terminal_failure
          and not rec.get("cancel_requested_at"))

    fail_reason = (linked_job or {}).get("fail_reason") or rec.get("fail_reason")
    stopped_reason = (None if linked and effective_verdict == "DEVELOPMENT_CANDIDATE"
                      and effective_export_path else (
                          rec.get("stopped_reason")
                          or (linked_job or {}).get("failure_detail")
                          or (linked_job or {}).get("fail_reason")
                      ))

    download_ready = bool(auto and effective_export_path and ok and not is_terminal_failure)
    base = {k: rec.get(k) for k in
            ("task_id", "state", "mode", "theme", "pages", "source_name",
             "source_sha256", "replay", "outdir", "job_id", "verdict",
             "generated_pptx", "generated_sha256", "pages_actual", "facts_frozen",
             "plan_mode", "model_error", "created_at", "finished_at", "returncode",
             "cancel_requested_at", "stdout_tail", "stderr_tail", "frozen_note",
             "auto_advance", "model_usage", "export_path", "validation", "stopped_reason",
             "heartbeat_at", "fail_reason")}
    base.update(st)
    base.update({"elapsed_seconds": elapsed, "success": bool(ok),
                 "verdict": effective_verdict,
                 "fail_reason": fail_reason,
                 "export_path": effective_export_path,
                 "layout_hard_gate": layout_gate,
                 "candidate_pptx_sha256": ((linked_validation or {}).get(
                     "candidate_pptx_sha256") if linked else None),
                 "stopped_reason": stopped_reason,
                 "download_ready": download_ready,
                 "refused": bool(rec.get("state") in ("done", "failed") and not ok)})
    return base


def _mt_status(tid: str):
    rec = _mt_load(tid)
    if not rec:
        return 404, {"error": {"code": "WB.TASK_NOT_FOUND", "message": "材料任务不存在。"}}
    return 200, {"task": _mt_public(rec)}


def _mt_cancel(tid: str):
    rec = _mt_load(tid)
    if not rec:
        return 404, {"error": {"code": "WB.TASK_NOT_FOUND", "message": "材料任务不存在。"}}
    if rec.get("state") not in ("running",):
        return 200, {"cancelled": False, "task": _mt_public(rec),
                     "note": "任务已结束，取消不适用。"}
    cf = Path(rec["cancel_file"])
    cf.parent.mkdir(parents=True, exist_ok=True)
    cf.write_text(now_iso(), encoding="utf-8")
    rec["cancel_requested_at"] = now_iso()

    # 终止正在执行的材料生成子进程
    pid = rec.get("pid")
    if pid and _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

    # 若已有下游 job_id，通知 runner 取消下游流水线并回收渲染器子进程
    job_id = rec.get("job_id")
    if job_id:
        try:
            cli(ROOT, "cancel", "--job-id", job_id, timeout=30)
        except Exception:
            pass

    rec["state"] = "cancelled"
    rec["verdict"] = "CANCELLED"
    rec["stopped_reason"] = "用户已取消任务"
    rec["finished_at"] = now_iso()
    rec["heartbeat_at"] = now_iso()
    adv = rec.setdefault("advance", [])
    if adv and adv[-1].get("state") == "running":
        adv[-1]["state"] = "cancelled"
    if not adv or adv[-1].get("stage") != "stop":
        adv.append({"stage": "stop", "at": now_iso(), "state": "stop", "reason": "用户已取消任务"})
    _mt_save(rec)
    return 200, {"cancelled": True, "task": _mt_public(rec),
                 "note": "已取消任务并终止相关进程。"}


def _mt_retry(tid: str):
    rec = _mt_load(tid)
    if not rec:
        return 404, {"error": {"code": "WB.TASK_NOT_FOUND", "message": "材料任务不存在。"}}
    if rec.get("state") == "running" and rec.get("pid") and _pid_alive(rec["pid"]):
        return 409, {"error": {"code": "WB.TASK_RUNNING", "message": "该任务仍在运行中，请先取消再重试。"}}
    body = {
        "mode": rec.get("mode") or "model",
        "theme": rec.get("theme"),
        "pages": rec.get("pages") or 12,
        "source": rec.get("source"),
        "replay_id": (rec.get("replay") or {}).get("pack"),
        "auto_advance": rec.get("auto_advance", True),
    }
    return _mt_start(body)


def _mt_advance(tid: str, job_id: str, cancel_file: Path):
    """生成完成后的薄编排：run → validate →（判定通过才）export。

    与 workbench/make_flow.py 同一原则：只调用真实 CLI，不自行判定成功；
    FAILED / NEEDS_REVIEW / 取消 / 导出失败一律停止推进，并登记停止原因。
    """
    def stamp(stage, **kw):
        r = _mt_load(tid) or {}
        r["heartbeat_at"] = now_iso()
        r.setdefault("advance", []).append({"stage": stage, "at": now_iso(), **kw})
        if kw.get("state") == "stop" and kw.get("reason"):
            r["stopped_reason"] = kw["reason"]
        _mt_save(r)

    def cancelled():
        r = _mt_load(tid) or {}
        return Path(cancel_file).exists() or bool(r.get("cancel_requested_at"))

    if cancelled():
        stamp("stop", state="stop", reason="已取消，未继续推进")
        return
    stamp("run", state="running")
    try:
        rc, _obj, out, err = cli(ROOT, "run", "--job-id", job_id, timeout=3600)
    except subprocess.TimeoutExpired:
        rc = 124
        out, err = "", "CLI run 超时（超过 3600 秒）"
    except Exception as exc:
        rc = 1
        out, err = "", f"CLI run 异常: {exc}"

    stamp("run", state="done" if rc == 0 else "failed", rc=rc,
          tail=None if rc == 0 else ((out or err) or "")[-300:])
    if rc != 0:
        jj = load_json(ROOT / "jobs" / job_id / "job.json") or {}
        reason = jj.get("failure_detail") or jj.get("fail_reason") or ("运行阶段失败（rc=%s）" % rc)
        r = _mt_load(tid) or {}
        r["fail_reason"] = jj.get("fail_reason") or "GATE.CLI_FAILED"
        r["stopped_reason"] = reason
        r["verdict"] = jj.get("verdict") or "FAILED"
        r["state"] = "failed"
        r["finished_at"] = jj.get("run_finished_at") or now_iso()
        _mt_save(r)
        stamp("stop", state="stop", reason=reason)
        return

    if cancelled():
        stamp("stop", state="stop", reason="已取消，未进入核对")
        return

    stamp("validate", state="running")
    try:
        rc, _obj2, out2, err2 = cli(ROOT, "validate", "--job-id", job_id, timeout=300)
    except subprocess.TimeoutExpired:
        rc = 124
        out2, err2 = "", "CLI validate 超时（超过 300 秒）"
    except Exception as exc:
        rc = 1
        out2, err2 = "", f"CLI validate 异常: {exc}"

    v = load_json(ROOT / "jobs" / job_id / "validation.json") or {}
    verdict = v.get("verdict") or ("FAILED" if rc != 0 else None)
    r = _mt_load(tid) or {}
    r["verdict"] = verdict
    r["heartbeat_at"] = now_iso()
    r["validation"] = {"verdict": verdict,
                       "blocking_findings": v.get("blocking_findings"),
                       "checks": v.get("checks")}
    _mt_save(r)
    stamp("validate", state="done" if rc == 0 else "failed", rc=rc, verdict=verdict)
    if verdict != "DEVELOPMENT_CANDIDATE":
        reason = "判定 %s，未进入导出（不把未通过结果做成可下载）" % verdict
        r["fail_reason"] = v.get("blocking_code") or f"GATE.VERDICT_{verdict}"
        r["stopped_reason"] = reason
        r["state"] = "failed" if verdict in ("FAILED", None) else "done"
        r["finished_at"] = now_iso()
        _mt_save(r)
        stamp("stop", state="stop", reason=reason)
        return

    if cancelled():
        stamp("stop", state="stop", reason="已取消，未导出")
        return

    stamp("export", state="running")
    try:
        rc, _obj3, out3, err3 = cli(ROOT, "export", "--job-id", job_id, timeout=300)
    except subprocess.TimeoutExpired:
        rc = 124
        out3, err3 = "", "CLI export 超时（超过 300 秒）"
    except Exception as exc:
        rc = 1
        out3, err3 = "", f"CLI export 异常: {exc}"

    job = load_json(ROOT / "jobs" / job_id / "job.json") or {}
    r = _mt_load(tid) or {}
    r["export_path"] = job.get("export_path")
    r["heartbeat_at"] = now_iso()
    if rc != 0 or not job.get("export_path"):
        r["fail_reason"] = "GATE.EXPORT_FAILED"
        r["stopped_reason"] = "导出阶段失败（rc=%s）" % rc
        r["state"] = "failed"
    else:
        r["state"] = "done"
    r["finished_at"] = now_iso()
    _mt_save(r)
    stamp("export", state="done" if rc == 0 else "failed", rc=rc,
          export_path=job.get("export_path"))
    if rc != 0:
        stamp("stop", state="stop", reason="导出阶段失败（rc=%s）" % rc)


def _material_model_usage(mode: str) -> dict:
    """Declare model/network stages without conflating replayed planning with scene planning."""
    return {
        "content_plan": ("frozen_replay_no_model_call" if mode == "replay"
                         else "local_model_may_be_called"),
        "semantic_scene_planning": "local_model_may_be_called",
        "photo_search": "provider_network_may_be_used",
    }


def _mt_start(body: dict):
    from wengao_ppt import theme as theme_mod
    mode = str(body.get("mode") or "model")
    theme = str(body.get("theme") or theme_mod.DEFAULT_THEME_ID)
    if theme not in theme_mod.THEMES:
        return 400, {"error": {"code": "THEME.UNKNOWN",
                               "message": "主题 %r 不在注册表内。" % theme}}
    try:
        pages = int(body.get("pages") or 12)
    except (TypeError, ValueError):
        return 400, {"error": {"code": "WB.BAD_PAGES", "message": "页数必须是整数。"}}
    if not (MATERIAL_PAGES[0] <= pages <= MATERIAL_PAGES[1]):
        return 400, {"error": {"code": "WB.PAGE_RANGE",
                               "message": "材料生成页数范围为 %d—%d 页。"
                                          % MATERIAL_PAGES}}
    if mode not in ("model", "replay"):
        return 400, {"error": {"code": "WB.BAD_MODE",
                               "message": "mode 只能是 model 或 replay。"}}

    pack, plan_path = None, None
    if mode == "replay":
        pack = next((p for p in _replay_packs() if p["id"] == body.get("replay_id")), None)
        if pack is None:
            return 400, {"error": {"code": "WB.BAD_REPLAY",
                                   "message": "回放包不存在或缺少材料/计划文件。"}}
        src = PROG / "samples" / "material_replay" / pack["material"]
        plan_path = PROG / "samples" / "material_replay" / pack["plan"]
    else:
        raw = str(body.get("source") or "")
        if not raw:
            return 400, {"error": {"code": "WB.BAD_SOURCE", "message": "缺少材料来源。"}}
        p = Path(raw)
        if not p.is_absolute():
            p = (ROOT / raw) if (ROOT / raw).exists() else (PROG / raw)
        src = p.resolve()
        allowed = False
        for b in (PROG / "samples", UPLOAD_DIR):
            try:
                src.relative_to(b.resolve())
                allowed = True
                break
            except ValueError:
                pass
        if not allowed:
            return 400, {"error": {"code": "WB.BAD_SOURCE",
                                   "message": "材料来源不在允许的样例/上传目录内。"}}
        if not src.is_file():
            return 400, {"error": {"code": "WB.BAD_SOURCE", "message": "材料文件不存在。"}}
        if src.suffix.lower() not in MATERIAL_SUFFIXES:
            msg = ("这是已有 PPTX，请用「已有 PPTX 美化」流程。"
                   if src.suffix.lower() == ".pptx"
                   else "材料生成仅支持 .md/.markdown/.txt/.docx。")
            return 400, {"error": {"code": "WB.BAD_SOURCE", "message": msg}}

    tid = "mt-" + os.urandom(4).hex()
    outdir = ROOT / "reports" / "r5" / ("wb_%s_%s" % (src.stem[:40], tid))
    tdir = _mt_dir() / tid
    tdir.mkdir(parents=True, exist_ok=True)
    cancel_file = tdir / "cancel.request"
    rec = {
        "task_id": tid, "state": "running", "created_at": now_iso(),
        "mode": mode, "theme": theme, "pages": pages,
        "source": str(src), "source_sha256": sha256_file(src),
        "source_name": src.name,
        "replay": ({"pack": pack["id"], "plan": str(plan_path),
                    "plan_sha256": sha256_file(plan_path),
                    "note": "只跳过内容规划模型；下游语义配图仍可能调用本地模型，照片检索可能联网。"} if pack else None),
        "outdir": str(outdir), "cancel_file": str(cancel_file),
        "job_id": None, "verdict": None, "generated_pptx": None,
        "auto_advance": bool(body.get("auto_advance")),
        "model_usage": _material_model_usage(mode),
        "advance": [],
        "model": "qwen3.8:27b-wb65536",
        "frozen_note": "材料哈希、页数、主题、回放计划在建任务前登记；生成期间不改。",
    }
    _mt_save(rec)
    args = [PY, "-m", "wengao_ppt.cli", "--project-root", str(ROOT),
            "from-material", "--input", str(src), "--pages", str(pages),
            "--model", rec["model"], "--outdir", str(outdir),
            "--theme", theme, "--accept", "--cancel-file", str(cancel_file)]
    if plan_path is not None:
        args += ["--replay-plan", str(plan_path)]
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, cwd=str(PROG), env=child_env(ROOT))
    rec["pid"] = proc.pid
    _mt_save(rec)

    t_start = time.time()
    rec["heartbeat_at"] = now_iso()
    _mt_save(rec)

    def _wait():
        timeout_s = 1800  # 30 分钟材料全链硬超时
        timed_out = False
        while proc.poll() is None:
            time.sleep(1.0)
            elapsed = time.time() - t_start
            cur = _mt_load(tid) or rec
            if cur.get("cancel_requested_at") or Path(cancel_file).exists():
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                break
            if elapsed > timeout_s:
                timed_out = True
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                break
            cur["heartbeat_at"] = now_iso()
            _mt_save(cur)

        out, err = proc.communicate()
        saved = _mt_load(tid) or rec
        rec_path = outdir / "run_record.json"
        rr = load_json(rec_path) if rec_path.is_file() else None
        linked = last_json(out or "") or {}
        job_id = linked.get("job_id") if isinstance(linked, dict) else None
        gp = (rr or {}).get("pptx", {}).get("path")
        if not job_id and gp:
            gpath = Path(str(gp))
            if not gpath.is_absolute():
                gpath = PROG / gpath
            for jd in sorted((ROOT / "jobs").glob("job-*"),
                             key=lambda q: q.stat().st_mtime, reverse=True):
                jj = load_json(jd / "job.json") or {}
                if jj.get("input_path") == str(gpath):
                    job_id = jd.name
                    break

        cancelled = bool(saved.get("cancel_requested_at") or Path(cancel_file).exists())
        saved.update({
            "job_id": job_id,
            "verdict": ("CANCELLED" if cancelled else ("FAILED" if timed_out else (rr or {}).get("verdict"))),
            "generated_pptx": (rr or {}).get("pptx", {}).get("path"),
            "generated_sha256": (rr or {}).get("pptx", {}).get("sha256"),
            "pages_actual": (rr or {}).get("pptx", {}).get("slides"),
            "facts_frozen": (rr or {}).get("fact_ledger", {}).get("count"),
            "plan_mode": ((rr or {}).get("plan") or {}).get("mode"),
            "model_error": (rr or {}).get("model_error"),
            "stdout_tail": (out or "")[-3000:], "stderr_tail": (err or "")[-1500:],
            "heartbeat_at": now_iso(),
        })
        if timed_out:
            saved["stopped_reason"] = f"材料生成超时（超过 {timeout_s} 秒未完成）"
            saved["fail_reason"] = "GATE.TIMEOUT"
        elif cancelled:
            saved["stopped_reason"] = "已取消生成"
        _mt_save(saved)
        (tdir / "run.log").write_text((out or "") + "\n[stderr]\n" + (err or ""),
                                      encoding="utf-8")

        # 生成段完成后再按需推进：run → validate →（通过才）export。
        # 与 make_flow 同样的原则：只调真实 CLI，任一终态失败/取消立即停，不做第二套判定。
        if not cancelled and not timed_out and saved.get("auto_advance") and job_id and proc.returncode == 0 \
                and saved.get("verdict") == "GENERATED_PENDING_VALIDATION":
            _mt_advance(tid, job_id, cancel_file)

        done = _mt_load(tid) or saved
        if cancelled or done.get("verdict") == "CANCELLED":
            done["state"] = "cancelled"
            done["verdict"] = "CANCELLED"
            done["stopped_reason"] = done.get("stopped_reason") or "已取消生成"
        elif timed_out or done.get("fail_reason") == "GATE.TIMEOUT":
            done["state"] = "failed"
            done["verdict"] = "FAILED"
            done["stopped_reason"] = done.get("stopped_reason") or "材料生成超时"
        elif (proc.returncode != 0 and isinstance(rr, dict)
              and rr.get("verdict") in MT_REFUSAL_VERDICTS):
            # 规范拒绝（rc=4 等受控退出）：不是进程崩溃。判定与具体原因
            # 直接取自本次运行记录，覆盖轮询竞态可能写入的"意外退出"。
            done["state"] = "failed"
            done["verdict"] = rr.get("verdict")
            done["fail_reason"] = f"MATERIAL.{rr.get('verdict')}"
            done["stopped_reason"] = _mt_refusal_reason(rr)
        elif done.get("verdict") == "FAILED" or done.get("fail_reason") or (proc.returncode != 0):
            done["state"] = "failed"
            done["verdict"] = done.get("verdict") or "FAILED"
            done["stopped_reason"] = done.get("stopped_reason") or "任务执行失败"
        elif done.get("verdict") != "DEVELOPMENT_CANDIDATE" and done.get("stopped_reason"):
            done["state"] = "failed" if proc.returncode != 0 or done.get("verdict") == "FAILED" else "done"
        else:
            done["state"] = "done"
        done["finished_at"] = done.get("finished_at") or now_iso()
        done["returncode"] = proc.returncode
        done["heartbeat_at"] = now_iso()
        _mt_save(done)

    threading.Thread(target=_wait, daemon=True).start()
    return 202, {"task": _mt_public(rec),
                 "note": "已启动真实生成；进度来自落盘产物，不来自计时器。"}


def _baseline_state(job_id: str) -> dict:
    """可编辑基线的**唯一判定点**：路径 + 不可用时的真实原因与下一步。

    /objects、/edit、版本列表都走这里，保证三处口径一致；
    不再把"还没导出"说成 `SCOPE.UNREQUESTED_CHANGE`（那是范围违规，不是缺基线）。
    """
    import sys
    sys.path.insert(0, str(PROG))
    from wengao_ppt.local_edit import resolve_baseline
    return resolve_baseline(ROOT / "jobs" / job_id)


def _edit_base(job_id: str):
    """兼容旧调用点：返回 (path, source)；不可用时 (None, None)。"""
    st = _baseline_state(job_id)
    if st.get("ok") and st.get("path"):
        return Path(st["path"]), st.get("source")
    return None, None


def _job_objects(job_id: str):
    jd = ROOT / "jobs" / job_id
    if not jd.is_dir():
        return 404, {"error": {
            "code": "WB.JOB_NOT_FOUND", "message": "任务不存在。",
            "reason_code": "JOB.NOT_FOUND",
            "next_actions": [{"action": "list_jobs", "endpoint": "GET /api/jobs",
                              "label": "先确认任务 id；任务列表里没有就是没有建过"}]}}
    if load_json(jd / "job.json") is None:
        # 任务目录在、但登记文件读不出来：这**不是**"任务不存在"，
        # 报 404 会让人以为任务丢了（auto30 第 2 条的同类问题）。
        st0 = _baseline_state(job_id)
        return 200, {"job_id": job_id, "editable": False,
                     "reason_code": st0.get("code") or "BASELINE.UNREADABLE",
                     "reason": st0.get("detail"),
                     "next_actions": st0.get("next_actions") or [],
                     "objects_source": None, "pages": []}
    state = _baseline_state(job_id)
    if not state.get("ok"):
        # 页面照常返回 200（前端要能渲染"为什么不能编辑"），但原因必须具体、
        # 并且带可执行的下一步；不再只给一句笼统的"需先导出一次"。
        return 200, {"job_id": job_id, "editable": False,
                     "reason_code": state.get("code"),
                     "reason": state.get("detail"),
                     "next_actions": state.get("next_actions") or [],
                     "objects_source": None, "pages": []}
    base = Path(state["path"])
    source = state.get("source")
    import sys as _sys
    _sys.path.insert(0, str(PROG))
    from wengao_ppt.local_edit import open_baseline
    opened = open_baseline(base)
    if not opened["ok"]:
        return 200, {"job_id": job_id, "editable": False,
                     "reason_code": opened["code"],
                     "reason": opened["detail"],
                     "next_actions": opened.get("next_actions") or [],
                     "objects_source": source, "pages": []}
    prs = opened["presentation"]
    pages = []
    SW = float(prs.slide_width or 1)
    SH = float(prs.slide_height or 1)
    for no, slide in enumerate(prs.slides, 1):
        objs = []
        for sh in slide.shapes:
            text = sh.text_frame.text if sh.has_text_frame else ""
            item = {"name": sh.name, "shape_id": sh.shape_id,
                    "type": str(sh.shape_type), "text": (text or "")[:100],
                    "fields": []}
            # 真实几何（相对整页的比例）；None 表示该对象无几何，前端不画选框
            try:
                if None not in (sh.left, sh.top, sh.width, sh.height):
                    item["box"] = {"x": round(float(sh.left) / SW, 6),
                                   "y": round(float(sh.top) / SH, 6),
                                   "w": round(float(sh.width) / SW, 6),
                                   "h": round(float(sh.height) / SH, 6)}
            except Exception:  # noqa: BLE001
                pass
            if (text or "").strip():
                item["fields"].append("text")
            if getattr(sh, "has_table", False) and sh.has_table:
                item["table"] = {"rows": len(sh.table.rows),
                                 "cols": len(sh.table.columns),
                                 "header": [c.text for c in sh.table.rows[0].cells][:6],
                                 "corner": (sh.table.cell(0, 0).text if len(sh.table.rows) else "")}
                item["fields"].append("cell")
            if getattr(sh, "has_chart", False) and sh.has_chart:
                try:
                    plot = sh.chart.plots[0]
                    cats = [str(c) for c in list(plot.categories)]
                    series = []
                    for i, s in enumerate(sh.chart.series):
                        try:
                            vals = [float(v) for v in list(s.values)]
                        except Exception:  # noqa: BLE001
                            vals = []
                        series.append({"name": s.name or ("系列%d" % i), "values": vals[:12]})
                    item["chart"] = {
                        "series": len(sh.chart.series),
                        "points": len(cats),
                        "series_names": [s["name"] for s in series],
                        "categories": cats[:12],
                        "values": series}
                except Exception:  # noqa: BLE001
                    item["chart"] = {"series": len(sh.chart.series), "points": None}
                item["fields"].append("chart_value")
            if int(getattr(sh, "shape_type", 0) or 0) == 13:
                item["image"] = True
                item["fields"].append("image")
            objs.append(item)
        pages.append({"page": no, "objects": objs})
    editable_n = sum(1 for pg in pages for o in pg["objects"] if o["fields"])
    payload = {"job_id": job_id, "editable": True, "objects_source": source,
               "baseline": str(base), "baseline_sha256": sha256_file(base),
               "slide": {"width_emu": int(SW), "height_emu": int(SH),
                         "ratio": round(SW / SH, 6)},
               "pages": pages,
               "editable_objects": editable_n,
               "total_objects": sum(len(pg["objects"]) for pg in pages),
               "note": "对象清单与几何均来自基线 PPTX 的后端解析；可编辑字段由对象类型决定。"}
    if editable_n == 0:
        # 基线能打开，但没有任何"可改字段"的对象：这是"无可编辑对象"，
        # 与"没有基线"是两件事，必须分开说。
        payload.update(reason_code="OBJECT.NO_EDITABLE",
                       reason=("基线里没有可编辑字段的对象（文本/表格/图表/图片都没有）。"
                               "这不是权限或范围问题，而是这一稿没有可改的原生对象。"),
                       next_actions=[{"action": "inspect_pages",
                                      "endpoint": "GET /api/jobs/%s/pages" % job_id,
                                      "label": "先看页图，确认内容是否被转成了图片"},
                                     {"action": "rebuild",
                                      "endpoint": "POST /api/make",
                                      "label": "若内容整体被栅格化，需要从原稿重新生成"}])
    return 200, payload


def _baseline_file(job_id: str, allow_original: bool = False):
    """预览/编辑用的真实文件：valid/ → 导出目录 →（可选）原稿。返回 (Path, 来源标签)。"""
    p, src = _edit_base(job_id)
    if p is not None:
        return p, src
    if allow_original:
        job = load_json(ROOT / "jobs" / job_id / "job.json") or {}
        ip = job.get("input_path")
        if ip and Path(ip).is_file():
            return Path(ip), "original"
    return None, None


def _preview_env_key() -> str:
    """预览缓存键里的字体环境段。

    原稿预览/基线预览此前只按文件哈希缓存：换字体配置后仍会复用旧的方块页图
    （事故里的"缓存没绑定环境"同样适用于预览）。这里把有效配置哈希并入目录名，
    旧目录原样保留，不删除以消灭失败样例。
    """
    try:
        from wengao_ppt import fontenv
        _env, rec = fontenv.build_env(ROOT)
        return (rec.get("effective_conf_sha256") or "nofc")[:8]
    except Exception:  # noqa: BLE001
        return "nofc"


def _preview_cache_dir(side: str, f: Path) -> Path:
    """预览缓存目录的**唯一构造点**（写入与取图共用）。

    事故后预览目录名带字体环境段；如果取图接口还按旧命名去找，
    就会出现「/pages 说有 13 页、取图 404」，或反过来取到旧环境目录里的方块图。
    """
    h = sha256_file(f)[:16]
    if side == "baseline":
        return PREVIEW_DIR / "baseline" / (h + "-" + _preview_env_key())
    return PREVIEW_DIR / (h + "-" + _preview_env_key())


def _edit_cache_dir(f: Path) -> Path:
    """编辑前后预览缓存：按**文件字节 + 字体环境**绑定，与候选/原稿同一口径。"""
    return PREVIEW_DIR / "edits" / (sha256_file(f)[:16] + "-" + _preview_env_key())


def _render_pages(file: Path, cache: Path):
    """把一份真实 PPTX 渲染成逐页 PNG（按文件哈希缓存），返回 (pages, pdf, 错误码)。"""
    pdfs = sorted(cache.glob("*.pdf")) if cache.is_dir() else []
    pngs = sorted((cache / "png").glob("*.png")) if (cache / "png").is_dir() else []
    if not (pdfs and pngs):
        from wengao_ppt import render as render_mod
        if not render_mod.real_render_available():
            return [], None, "RENDER.NOT_AVAILABLE"
        cache.mkdir(parents=True, exist_ok=True)
        rr = render_mod.render_real(file, cache, data_root=ROOT)
        if rr.get("code") != "RENDER.OK":
            return [], None, rr.get("code") or "RENDER.EXECUTION_FAILED"
        pdfs = sorted(cache.glob("*.pdf"))
        pngs = sorted((cache / "png").glob("*.png"))
    return ["png/" + x.name for x in pngs], (pdfs[0].name if pdfs else None), None


def _preview_slide_numbers(pptx: Path | None, pages: list) -> dict:
    """Bind rendered PDF positions to native slide numbers, excluding hidden slides."""
    if pptx is None:
        return {}
    try:
        from pptx import Presentation
        slides = list(Presentation(str(pptx)).slides)
        hidden = [n for n, slide in enumerate(slides, 1)
                  if slide._element.get("show") in ("0", "false", "off")]
        visible = [n for n in range(1, len(slides) + 1) if n not in hidden]
        numbers = visible if len(pages) == len(visible) else (
            list(range(1, len(slides) + 1)) if len(pages) == len(slides) else [])
        return {"total_slides": len(slides), "hidden_slides": hidden,
                "page_numbers": numbers}
    except Exception:
        return {}


def _pages_of(job_id: str, side: str):
    """统一的逐页预览接口数据源；side ∈ baseline / original / candidate。"""
    if load_json(ROOT / "jobs" / job_id / "job.json") is None:
        return 404, {"error": {"code": "WB.JOB_NOT_FOUND", "message": "任务不存在。"}}
    if side == "candidate":
        cache = ROOT / "jobs" / job_id / "candidate" / "render"
        cdir = ROOT / "jobs" / job_id / "candidate"
        pptx = sorted(cdir.glob("*.pptx")) if cdir.is_dir() else []
        if not cache.is_dir():
            return 200, {"job_id": job_id, "side": side, "available": False,
                         "reason": "候选尚未渲染；先运行任务。", "pages": []}
        meta = load_json(cache / "render_meta.json") or {}
        cur_sha = sha256_file(pptx[0]) if pptx else None
        if not meta:
            return 200, {"job_id": job_id, "side": side, "available": False,
                         "reason": "页图缺少环境/候选绑定（事故缓存或旧版本），"
                                   "不能作为当前可信预览；请重新验收。",
                         "pages": [], "code": "RENDER.UNBOUND_CACHE"}
        if cur_sha and meta.get("candidate_sha256") != cur_sha:
            return 200, {"job_id": job_id, "side": side, "available": False,
                         "reason": "页图对应的是另一版候选（哈希不符），不拿旧图冒充当前结果。",
                         "pages": [], "code": "RENDER.STALE_BINDING"}
        from wengao_ppt import incident as incident_mod
        dl = incident_mod.delivery_state(ROOT / "jobs" / job_id, ROOT)
        if not dl["allowed"] and dl["code"] == "INCIDENT.BLOCKED":
            return 200, {"job_id": job_id, "side": side, "available": False,
                         "reason": "该结果已被事故台账标记为不可交付，页图不再作为可信预览："
                                   + "；".join(x.get("detail", "") for x in dl["reasons"][:2]),
                         "pages": [], "code": "INCIDENT.BLOCKED"}
        pages, pdf, err = _render_pages_from_cache(cache)
        return 200, {"job_id": job_id, "side": side, "available": bool(pages),
                     "source": "candidate",
                     "file_sha256": sha256_file(pptx[0]) if pptx else None,
                     "pages": pages, "pdf": pdf, "code": err,
                     **_preview_slide_numbers(pptx[0] if pptx else None, pages),
                     "reason": None if pages else "候选页图不可用。"}
    if side not in ("baseline", "original"):
        return 400, {"error": {"code": "WB.BAD_SIDE",
                               "message": "side 只能是 baseline / original / candidate。"}}
    if side == "baseline":
        f, src = _baseline_file(job_id, allow_original=False)
        if f is None:
            return 200, {"job_id": job_id, "side": side, "available": False,
                         "reason": "该任务还没有可用基线（需先运行/验收或导出一次）。",
                         "pages": []}
    else:
        job = load_json(ROOT / "jobs" / job_id / "job.json") or {}
        f = Path(job.get("input_path") or "")
        src = "original"
        if not f.is_file():
            return 200, {"job_id": job_id, "side": side, "available": False,
                         "reason": "原稿文件当前不可用。", "pages": []}
    h = sha256_file(f)
    # A23：未改过的任务里，"基线"就是已验收的那一版候选，两栏页图字节完全相同。
    # 这本身是对的（没有改动就不该有差异），但接口此前不说，界面只能显示两栏一样的图，
    # 看起来像"渲染失败/取错了文件"。这里如实标出"与当前候选同一份字节"。
    cdir = ROOT / "jobs" / job_id / "candidate"
    cands = sorted(cdir.glob("*.pptx")) if cdir.is_dir() else []
    cand_sha = sha256_file(cands[0]) if cands else None
    identical = bool(cand_sha) and cand_sha == h
    cache = _preview_cache_dir(side, f)
    pages, pdf, err = _render_pages(f, cache)
    return 200, {"job_id": job_id, "side": side, "available": bool(pages),
                 "source": src, "file_sha256": h, "pages": pages, "pdf": pdf,
                 **_preview_slide_numbers(f, pages),
                 "cached": (cache / "png").is_dir(),
                 "code": err,
                 "candidate_sha256": cand_sha,
                 "identical_to_candidate": identical,
                 "note": ("本栏与「候选」是同一份字节（该任务还没有产生改动），"
                          "两栏页图相同不是取错文件。" if identical else None),
                 "reason": None if pages else ("预览渲染不可用（%s）。" % err)}


def _render_pages_from_cache(cache: Path):
    pngs = sorted((cache / "png").glob("*.png")) if (cache / "png").is_dir() else []
    pdfs = sorted(cache.glob("*.pdf"))
    return (["png/" + x.name for x in pngs],
            (pdfs[0].name if pdfs else None), None if pngs else "RENDER.NO_PAGES")


def _edit_record(job_id: str, vid: str):
    d = ROOT / "jobs" / job_id
    if load_json(d / "job.json") is None:
        return None
    return load_json(d / "versions" / ("%s.json" % vid))


def _render_edit_pair(job_id: str, vid: str, rec: dict):
    out = {}
    for side, key in (("before", "pre_copy"), ("after", "candidate_pptx")):
        p = Path(str(rec.get(key) or ""))
        if not p.is_file():
            return None, "%s 文件缺失" % side
        h = sha256_file(p)[:16]
        cache = _edit_cache_dir(p)
        pdfs = sorted(cache.glob("*.pdf"))
        pngs = sorted((cache / "png").glob("*.png"))
        if not (pdfs and pngs):
            cache.mkdir(parents=True, exist_ok=True)
            from wengao_ppt import render as render_mod
            if not render_mod.real_render_available():
                return None, "RENDER.NOT_AVAILABLE"
            rr = render_mod.render_real(p, cache, data_root=ROOT)
            if rr.get("code") != "RENDER.OK":
                return None, rr.get("code") or "RENDER.EXECUTION_FAILED"
            pdfs = sorted(cache.glob("*.pdf"))
            pngs = sorted((cache / "png").glob("*.png"))
        out[side] = {"sha256": sha256_file(p),
                     "pdf": pdfs[0].name if pdfs else None,
                     "pages": ["png/" + x.name for x in pngs],
                     **_preview_slide_numbers(p, pngs),
                     "cache": str(cache)}
    return out, None


def _history_data() -> dict:
    jobs = []
    for j in WB.jobs_list():
        d = ROOT / "jobs" / j["job_id"]
        vers = []
        vdir = d / "versions"
        if vdir.is_dir():
            for f in sorted(vdir.glob("*.json")):
                r = load_json(f) or {}
                if f.name.startswith("restore_"):
                    vers.append({"version_id": f.stem, "kind": "restore",
                                 "created_at": r.get("restored_at"),
                                 "restored_from": r.get("restored_from"),
                                 "sha256": r.get("restored_sha256"),
                                 "requires_revalidation": True})
                else:
                    vers.append({"version_id": f.stem, "kind": "edit",
                                 "created_at": r.get("created_at"),
                                 "edit": r.get("edit"),
                                 "old_value": r.get("old_value"),
                                 "new_value": r.get("new_value"),
                                 "sha256": r.get("candidate_sha256"),
                                 "base_sha256": r.get("base_sha256"),
                                 "untouched_objects_verified": r.get("untouched_objects_verified")})
        vers.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        updated = max([x.get("created_at") or "" for x in vers] +
                      [j.get("last_activity") or "", j.get("created_at") or ""])
        jobs.append({"job_id": j["job_id"], "mode": j.get("mode"),
                     "theme": j.get("theme"), "theme_name": j.get("theme_name"),
                     "theme_version": j.get("theme_version"),
                     "created_at": j.get("created_at"), "updated_at": updated,
                     "verdict": j.get("validation_verdict") or j.get("verdict"),
                     "stage": j.get("stage"),
                     "input": Path(j.get("input_path") or "").name,
                     "input_hash": j.get("input_hash"),
                     "export_path": j.get("export_path"),
                     "export_ready": bool(j.get("export_path")),
                     "editable_baseline": bool(_edit_base(j["job_id"])[0]),
                     "versions": vers})
    return {"jobs": jobs, "count": len(jobs),
            "note": "版本身份来自 versions/*.json 与 job.json；不以文件名或修改时间作为唯一依据。"}


class BodyAlreadyConsumedError(Exception):
    """FIX-W4-001：请求体已被前置逻辑读取，不可重复消费。"""


# FIX-W4-001：请求体缓存的"未设置"哨兵。请求体本身可能是 JSON null，
# 不能用 None 兼作"尚未读取"的标记，否则二次读取会误判为已消费。
_BODY_UNSET = object()


class Handler(BaseHTTPRequestHandler):
    server_version = "WenGaoWorkbench/1.0"
    protocol_version = "HTTP/1.1"

    # ---------------- 基础工具 ----------------
    def _send(self, code, obj, ctype="application/json"):
        body = b"" if getattr(self, "_head_only", False) else \
            json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_file(self, path: Path):
        import mimetypes
        data = b"" if getattr(self, "_head_only", False) else path.read_bytes()
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _send_html(self, path: Path):
        """按原始字节发送本地页面（不做任何 JSON 转义）。"""
        data = b"" if getattr(self, "_head_only", False) else path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _err(self, code, gw_code, message, extra=None):
        payload = {"error": {"code": gw_code, "message": message}}
        if extra:
            payload["error"].update(extra)
        self._send(code, payload)

    def _body_json(self):
        cached = getattr(self, "_body_cached_json", _BODY_UNSET)
        if cached is not _BODY_UNSET:
            return cached
        if getattr(self, "_body_consumed", False):
            raise BodyAlreadyConsumedError("请求体已被前置逻辑读取，不可重复消费。")
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 1 << 20:
            self._body_cached_json = {}
            return {}
        self._body_consumed = True
        try:
            raw = self.rfile.read(n)
            self._body_cached_json = json.loads(raw.decode("utf-8"))
            return self._body_cached_json
        except Exception:  # noqa: BLE001
            self._body_cached_json = {}
            return {}

    def log_message(self, fmt, *args):
        sys.stderr.write("[workbench] %s %s\n" % (now_iso(), fmt % args))

    # ---------------- 路由 ----------------
    def do_HEAD(self):
        # 存在性探测（新界面页图探测用）：与 GET 同路由，只回状态码与头
        self._head_only = True
        try:
            self.do_GET()
        finally:
            self._head_only = False

    def do_GET(self):
        self._body_consumed = False
        self._body_cached_json = _BODY_UNSET
        try:
            u = urllib.parse.urlparse(self.path)
            path = u.path
            q = urllib.parse.parse_qs(u.query)
            # RS-9 新工作台界面端点（/ui 与 /api/... 新路由）；旧路由保持原样
            try:
                from workbench import ui_api
                if ui_api.dispatch_get(self, path, q):
                    return
            except ImportError:
                pass
            if path in ("/", "/index.html"):
                # 页面必须按**原始字节**发送：不能走 _send()（那会对整个 HTML 做
                # json.dumps，把 <!DOCTYPE …> 变成带引号的 JSON 字符串，浏览器只能
                # 显示转义后的源码）。与 _send_file 同一处理方式。
                return self._send_html(Path(__file__).parent / "index.html")
            if path == "/api/bootstrap":
                return self._bootstrap()
            if path == "/api/jobs":
                # A30：任务数会一直增长，列表支持 limit/offset 并把 total 一起返回，
                # 让调用方能判断"还有没有下一页"，而不是每次拉全量。
                try:
                    limit = int((q.get("limit") or ["0"])[0] or 0)
                except ValueError:
                    limit = 0
                try:
                    offset = int((q.get("offset") or ["0"])[0] or 0)
                except ValueError:
                    offset = 0
                all_jobs = WB.jobs_list()
                total = len(all_jobs)
                page = all_jobs[offset:] if not limit else all_jobs[offset:offset + limit]
                return self._send(200, {"jobs": page, "total": total,
                                        "limit": limit or None, "offset": offset,
                                        "has_more": (offset + len(page)) < total})
            m = re.fullmatch(r"/api/jobs/([A-Za-z0-9-]+)/baseline", path)
            if m:
                # A20：不伪造 current_version。可编辑基线就是"登记的交付目录里那一稿"，
                # 有就有、没有就说没有，并给出原因与下一步。
                jid = m.group(1)
                if not (ROOT / "jobs" / jid).is_dir():
                    return self._err(404, "WB.JOB_NOT_FOUND", "任务不存在。",
                                     extra={"reason_code": "JOB.NOT_FOUND"})
                st = _baseline_state(jid)
                if not st.get("ok"):
                    return self._send(200, {"job_id": jid, "available": False,
                                            "reason_code": st.get("code"),
                                            "reason": st.get("detail"),
                                            "next_actions": st.get("next_actions") or []})
                p = Path(st["path"])
                return self._send(200, {"job_id": jid, "available": True,
                                        "source": st.get("source"),
                                        "path": str(p),
                                        "sha256": sha256_file(p),
                                        "bytes": p.stat().st_size,
                                        "modified_at": datetime.fromtimestamp(
                                            p.stat().st_mtime).isoformat(timespec="seconds"),
                                        "detail": st.get("detail"),
                                        "note": "当前可编辑基线；没有单独的“当前版本指针”，"
                                                "改稿产生的是独立候选（versions/）。"})
            m = re.fullmatch(r"/api/jobs/([A-Za-z0-9-]+)", path)
            if m:
                st = WB.job_state(m.group(1))
                if st is None:
                    return self._err(404, "WB.JOB_NOT_FOUND", "任务不存在。")
                return self._send(200, st)
            m = re.fullmatch(r"/api/jobs/([A-Za-z0-9-]+)/original", path)
            if m:
                r = WB.original_preview(m.group(1))
                if r is None:
                    return self._err(404, "WB.JOB_NOT_FOUND", "任务不存在。")
                return self._send(200, r)
            m = re.fullmatch(r"/api/jobs/([A-Za-z0-9-]+)/result", path)
            if m:
                return self._job_result(m.group(1))
            m = re.fullmatch(r"/api/jobs/([A-Za-z0-9-]+)/native-review-copy", path)
            if m:
                return self._native_review_copy(m.group(1), q)
            m = re.fullmatch(r"/api/jobs/([A-Za-z0-9-]+)/download", path)
            if m:
                return self._download(m.group(1), q)
            # D10-C WP2：「开始制作」编排只读状态
            m = re.fullmatch(r"/api/make/([A-Za-z0-9-]+)", path)
            if m:
                if WB.job_state(m.group(1)) is None:
                    return self._err(404, "WB.JOB_NOT_FOUND", "任务不存在。")
                return self._send(200, MAKE.snapshot(m.group(1)))
            # D09：局部修改版本列表（只读）
            m = re.fullmatch(r"/api/jobs/([A-Za-z0-9-]+)/edits", path)
            if m:
                return self._job_edits(m.group(1))
            # USER-ENTRY：实例自述 / 历史 / 材料任务 / 编辑对象与版本
            if path == "/api/meta":
                return self._meta()
            if path == "/api/history":
                return self._history()
            if path == "/api/material/tasks":
                return self._material_list()
            m = re.fullmatch(r"/api/material/tasks/(mt-[0-9a-f]{8})", path)
            if m:
                return self._material_one(m.group(1))
            m = re.fullmatch(r"/api/jobs/([A-Za-z0-9-]+)/objects", path)
            if m:
                return self._job_objects(m.group(1))
            # UI-PREMIUM：逐页真实预览（baseline / original / candidate）
            m = re.fullmatch(r"/api/jobs/([A-Za-z0-9-]+)/pages", path)
            if m:
                side = (q.get("side") or ["baseline"])[0]
                code, payload = _pages_of(m.group(1), side)
                return self._send(code, payload)
            m = re.fullmatch(r"/api/jobs/([A-Za-z0-9-]+)/edits/(v\d{4})/preview", path)
            if m:
                return self._edit_preview_file(m.group(1), m.group(2), q)
            m = re.fullmatch(r"/api/jobs/([A-Za-z0-9-]+)/edits/(v\d{4}|restore_v\d{4})", path)
            if m:
                return self._edit_detail(m.group(1), m.group(2))
            if path == "/api/preview/file":
                return self._preview_file(q)
            return self._err(404, "WB.NOT_FOUND", "接口不存在。")
        except BodyAlreadyConsumedError as e:
            return self._err(500, "WB.BODY_ALREADY_CONSUMED", str(e))
        except Exception as e:  # noqa: BLE001
            return self._err(500, "WB.INTERNAL", f"{type(e).__name__}: {e}")

    def do_POST(self):
        self._body_consumed = False
        self._body_cached_json = _BODY_UNSET
        try:
            u = urllib.parse.urlparse(self.path)
            path = u.path
            # RS-9 新工作台界面端点（/ui 与 /api/... 新路由）；旧路由保持原样
            try:
                from workbench import ui_api
                if ui_api.dispatch_post(self, path):
                    return
            except ImportError:
                pass
            if path == "/api/upload":
                return self._upload()
            if path == "/api/material/upload":
                return self._material_upload()
            if path == "/api/jobs":
                return self._create_job()
            # D10-C WP2：「开始制作」受控编排入口
            if path == "/api/make":
                return self._make_start()
            # R5：材料生成入口（与已有 PPTX 美化路径明确区分）
            if path == "/api/from-material":
                return self._from_material()
            m = re.fullmatch(r"/api/make/([A-Za-z0-9-]+)/cancel", path)
            if m:
                if WB.job_state(m.group(1)) is None:
                    return self._err(404, "WB.JOB_NOT_FOUND", "任务不存在。")
                return self._send(200, MAKE.cancel(m.group(1)))
            m = re.fullmatch(r"/api/jobs/([A-Za-z0-9-]+)/(run|validate|cancel|export)", path)
            if m:
                return self._job_action(m.group(1), m.group(2))
            m = re.fullmatch(r"/api/jobs/([A-Za-z0-9-]+)/native-playback", path)
            if m:
                return self._native_playback_upload(m.group(1))
            # D09：授权局部修改（必须明确到页/对象/字段/新值）
            m = re.fullmatch(r"/api/jobs/([A-Za-z0-9-]+)/edit", path)
            if m:
                return self._job_edit(m.group(1))
            # USER-ENTRY：材料任务 / 编辑版本（渲染、恢复、接纳）/ 图片资产
            if path == "/api/material/tasks":
                return self._material_start()
            m = re.fullmatch(r"/api/material/tasks/(mt-[0-9a-f]{8})/cancel", path)
            if m:
                return self._material_cancel(m.group(1))
            m = re.fullmatch(r"/api/material/tasks/(mt-[0-9a-f]{8})/retry", path)
            if m:
                return self._material_retry(m.group(1))
            m = re.fullmatch(r"/api/jobs/([A-Za-z0-9-]+)/edits/(v\d{4})/render", path)
            if m:
                return self._edit_render(m.group(1), m.group(2))
            m = re.fullmatch(r"/api/jobs/([A-Za-z0-9-]+)/edits/(v\d{4})/restore", path)
            if m:
                return self._edit_restore(m.group(1), m.group(2))
            m = re.fullmatch(r"/api/jobs/([A-Za-z0-9-]+)/edits/(v\d{4})/adopt", path)
            if m:
                return self._edit_adopt(m.group(1), m.group(2))
            if path == "/api/assets":
                return self._asset_upload()
            # A24：上传/预览只增不减，此前没有回收入口。
            # 默认 dry-run（只列清单），apply=true 才真正动作，且只移入 _trash，不删除。
            if path == "/api/cleanup":
                return self._cleanup()
            return self._err(404, "WB.NOT_FOUND", "接口不存在。")
        except BodyAlreadyConsumedError as e:
            return self._err(500, "WB.BODY_ALREADY_CONSUMED", str(e))
        except Exception as e:  # noqa: BLE001
            return self._err(500, "WB.INTERNAL", f"{type(e).__name__}: {e}")

    # ---------------- D09：授权局部修改 ----------------
    EDIT_FIELDS = ("text", "cell", "chart_value", "image")
    EDIT_EXTRA = {"cell": ("row", "col"), "chart_value": ("series", "index"),
                  "image": ("asset",)}

    def _job_edits(self, job_id: str):
        """局部修改版本列表（只读）。"""
        import sys
        sys.path.insert(0, str(ROOT))
        from wengao_ppt.local_edit import list_versions
        if not (ROOT / "jobs" / job_id).is_dir():
            return self._err(404, "WB.JOB_NOT_FOUND", "任务不存在。",
                             extra={"reason_code": "JOB.NOT_FOUND"})
        return self._send(200, {"job_id": job_id,
                                "versions": list_versions(ROOT / "jobs" / job_id)})

    def _job_edit(self, job_id: str):
        """执行一次授权局部修改；服务端不猜测参数，缺项直接 400 拒绝。"""
        import sys
        sys.path.insert(0, str(ROOT))
        from wengao_ppt.local_edit import EditScopeError, apply_local_edit, list_versions
        if not (ROOT / "jobs" / job_id).is_dir():
            return self._err(404, "WB.JOB_NOT_FOUND", "任务不存在。",
                             extra={"reason_code": "JOB.NOT_FOUND"})
        body = self._body_json() or {}
        page = body.get("page")
        shape = body.get("shape")
        field = body.get("field")
        new_value = body.get("new_value")
        missing = [k for k, v in (("page", page), ("shape", shape),
                                  ("field", field), ("new_value", new_value))
                   if v in (None, "")]
        if field not in self.EDIT_FIELDS:
            missing.append("field(必须是 " + "/".join(self.EDIT_FIELDS) + ")")
        for key in self.EDIT_EXTRA.get(field, ()):
            if body.get(key) in (None, ""):
                missing.append(f"{field}.{key}")
        if missing:
            return self._err(400, "WB.EDIT_NOT_EXPLICIT",
                             "局部修改必须明确到页/对象/字段/新值；缺少：" +
                             "、".join(missing))
        edit = {"page": int(page), "shape": str(shape),
                "field": str(field), "new_value": str(new_value)}
        for key in self.EDIT_EXTRA.get(field, ()):
            v = body[key]
            edit[key] = int(v) if key in ("row", "col", "series", "index") else str(v)
        # A18/A16：调用方可以把它取对象清单时拿到的 baseline_sha256 带回来；
        # 不一致就是"你改的不是你看到的那一版"，拒绝而不是静默改错文件。
        if body.get("expected_base_sha256"):
            edit["expected_base_sha256"] = str(body["expected_base_sha256"])
        jd = ROOT / "jobs" / job_id
        if (jd / "cancel.request").is_file():
            return self._err(409, "WB.CANCELLED",
                             "该任务已请求取消；取消后不推进有效版本。")
        try:
            rec = apply_local_edit(jd, edit)
        except EditScopeError as e:
            extra = {}
            if str(e.code).startswith(("BASELINE.", "OBJECT.", "VERSION.")):
                st = _baseline_state(job_id)
                if not st.get("ok"):
                    extra["next_actions"] = st.get("next_actions") or []
            return self._err(409, e.code, e.detail, extra=extra or None)
        rec["versions"] = list_versions(jd)
        # A16：与其它写接口（adopted/restored/created）一致，给明确成功标志。
        rec["ok"] = True
        rec["edited"] = True
        return self._send(200, rec)

    # ---------------- R5：材料生成（区别于 PPTX 美化）----------------
    MATERIAL_SUFFIXES = (".md", ".markdown", ".txt", ".text", ".docx")

    def _from_material(self):
        """由原始材料生成 PPTX。与 /api/make（已有 PPTX 美化）严格区分。

        - 只接受 samples/ 或上传目录内的材料；
        - 只接受 .md/.markdown/.txt/.text/.docx；传入 .pptx 一律拒绝并提示走美化路径；
        - 生成结果**不自动进入**候选/发布，需再走 result/download 的既有门禁；
        - 同步执行并返回真实 run_record（前端据其显示真实阶段）。
        """
        body = self._body_json() or {}
        src = body.get("source") or ""
        pages = int(body.get("pages") or 12)
        model = body.get("model") or "qwen3.8:27b-wb65536"
        if not src:
            return self._err(400, "WB.BAD_SOURCE", "缺少材料来源。")

        # A26：路径解析要先"选候选"再"判允许"，否则文件不存在时会退化到
        # PROG 下的同名路径，于是明明只是文件没了，却被判成"来源不在允许目录内"。
        p = Path(src)
        cands = [p] if p.is_absolute() else [ROOT / src, PROG / src]
        bases = [(PROG / "samples").resolve(), UPLOAD_DIR.resolve()]

        def _within(x: Path) -> bool:
            try:
                xr = x.resolve()
            except OSError:  # noqa: BLE001
                return False
            for b in bases:
                try:
                    xr.relative_to(b)
                    return True
                except ValueError:
                    continue
            return False

        chosen = None
        for c in cands:
            if c.exists():
                chosen = c.resolve()
                break
        if chosen is None:
            # 一个候选都不存在：路径落在允许的命名空间里 → 只是文件没了（404）；
            # 路径本身越界 → 才是"来源不被允许"（400）。
            if any(_within(c) for c in cands):
                return self._err(
                    404, "WB.SOURCE_NOT_FOUND",
                    f"材料文件不存在：{Path(src).name}（检查路径或先上传）。")
            return self._err(400, "WB.BAD_SOURCE",
                             "材料来源不在允许的样例/上传目录内。")
        p = chosen
        if not _within(p):
            return self._err(400, "WB.BAD_SOURCE",
                             "材料来源不在允许的样例/上传目录内。")
        # A26：先区分"根本不存在"与"是个目录"——两者的下一步完全不同
        # （前者要改路径/重新上传，后者要指定到文件），不能都叫 WB.BAD_SOURCE。
        if p.is_dir():
            return self._err(400, "WB.SOURCE_IS_DIR",
                             f"传入的是目录而不是文件：{p.name}；请指定到具体材料文件。")
        if p.suffix.lower() not in self.MATERIAL_SUFFIXES:
            if p.suffix.lower() == ".pptx":
                return self._err(400, "WB.BAD_SOURCE",
                                 "这是已有 PPTX，请用「开始制作」美化路径；"
                                 "材料生成只接受 .md/.markdown/.txt/.docx。")
            return self._err(400, "WB.BAD_SOURCE",
                             "材料生成仅支持 .md/.markdown/.txt/.docx。")

        outdir = ROOT / "reports" / "r5" / f"wb_{p.stem}"
        import time as _time
        _t0 = _time.time()
        proc = subprocess.run(
            [PY, "-m", "wengao_ppt.cli", "--project-root", str(ROOT),
             "from-material", "--input", str(p), "--pages", str(pages),
             "--model", model, "--outdir", str(outdir)],
            capture_output=True, text=True, cwd=str(ROOT),
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=3600)
        rec_path = outdir / "run_record.json"
        # 只信**本次请求期间写出**的运行记录：同一材料目录会被复用，
        # 上一次留下的旧记录不得冒充本次结果（否则会报出错误的 verdict/产物路径）。
        fresh = rec_path.is_file() and rec_path.stat().st_mtime >= _t0 - 1
        usable = fresh and proc.returncode == 0
        rec = (load_json(rec_path) or {}) if usable else {}
        ok = proc.returncode == 0 and fresh and bool(rec.get("pptx"))
        verdict = rec.get("verdict") if usable else None
        # 规划/分页/资产这类**规范拒绝**不是服务器故障：生成器按契约拒绝了，
        # 必须回 4xx 并把拒绝码与原因带给前端，而不是 500。
        # 只有子进程本身失败（rc != 0）才是真正的内部错误。
        REFUSAL_VERDICTS = {"PLAN_FAILED", "PLAN_ASSET_INVALID",
                            "PAGE_RANGE_UNSATISFIED", "PAGE_COUNT_MISMATCH",
                            "CANCELLED"}
        if ok:
            status = 200
        elif proc.returncode == 0 and verdict in REFUSAL_VERDICTS:
            status = 422
        else:
            status = 500
        reason = ""
        if verdict == "PAGE_RANGE_UNSATISFIED":
            reason = json.dumps(rec.get("page_range_conflict") or {},
                                ensure_ascii=False)
        elif verdict == "PLAN_ASSET_INVALID":
            reason = json.dumps(rec.get("plan_asset_problems") or [],
                                ensure_ascii=False)
        elif verdict == "PLAN_FAILED":
            reason = str((rec.get("plan") or {}).get("error") or "")
        elif verdict == "PAGE_COUNT_MISMATCH":
            reason = str(rec.get("page_count_source") or "")
        return self._send(status, {
            "generated": ok,
            "refused": (not ok) and usable,
            "record_fresh": fresh, "record_used": usable,
            "mode": "from_material",
            "distinct_from": "/api/make（已有 PPTX 美化）",
            "returncode": proc.returncode,
            "input": str(p.relative_to(ROOT)),
            "facts_frozen": (rec.get("fact_ledger") or {}).get("count"),
            "plan_mode": (rec.get("plan") or {}).get("mode"),
            "plan_chunks": (rec.get("plan") or {}).get("chunk_count"),
            "generated_pptx": (rec.get("pptx") or {}).get("path"),
            "verdict": verdict,
            "refusal_reason": reason,
            "accepted_into_pipeline": False,
            "stdout_tail": (proc.stdout or "")[-600:],
            "stderr_tail": (proc.stderr or "")[-400:],
        })

    # ---------------- D10-C WP2：开始制作 ----------------
    def _make_start(self):
        """受理「开始制作」。前端只提交原稿与主题，成功与否由后端编排判定。"""
        body = self._body_json() or {}
        src = body.get("source") or ""
        theme = body.get("theme") or ""
        # 默认开启：找不到可核验配图不再整单阻断稿子，改用如实标注的说明
        # 卡片兜底（从不顶替已有图片、从不假装是真实照片）。调用方显式传
        # false 才会退回"找不到图就整单阻断"的严格模式。
        allow_text_card_fallback = bool(body.get("allow_text_card_fallback", True))
        if not src:
            return self._err(400, "WB.BAD_SOURCE", "缺少原稿来源。")
        r = MAKE.start(src, theme, allow_text_card_fallback=allow_text_card_fallback)
        if not r.get("accepted"):
            # A14：/api/make（新入口）与 /api/jobs（旧入口）此前错误体不同、
            # 且都返回 200，调用方只能靠 body 猜。这里与 /api/jobs 取齐：
            # 422 + 同一信封（error.code 与其它拒绝一致，同时保留 accepted/gate/detail）。
            payload = dict(r)
            err = payload.get("error") or {}
            code = (err.get("code") or payload.get("gate")
                    or "GATE.PRECHECK_FAILED")
            msg = (err.get("message") or payload.get("detail")
                   or payload.get("gate") or "预检未通过，未创建任务。")
            payload["error"] = {
                "code": code, "message": msg,
                "reason_code": payload.get("reason_code") or code,
                "next_actions": (payload.get("next_actions")
                                 or err.get("next_actions")
                                 or [{"action": "upload", "endpoint": "POST /api/upload",
                                      "label": "重新上传一份能被解析的 .pptx 原稿"}]),
            }
            return self._send(422, payload)
        return self._send(202, r)

    # ---------------- 实例 / 材料 / 编辑 / 历史（USER-ENTRY） ----------------
    def _meta(self):
        from wengao_ppt import __version__, theme as theme_mod
        # A15：/api/meta 之前缺 runner_version/label，调用方只能再打 /api/bootstrap。
        # 两者定位不同（bootstrap 是"首屏一次拿全"，meta 是"随时问身份"），
        # 但产品身份这类字段不该只在一处出现，这里补齐。
        try:
            from wengao_ppt import gate_rs
            rs_checks = gate_rs.registry_view()
        except Exception:  # noqa: BLE001
            rs_checks = {}
        return self._send(200, {
            "product": "稳稿 PPT 本地工作台",
            "runner_version": __version__,
            "label": "DEVELOPMENT_CANDIDATE",
            "release_ready": False,
            "instance": _instance_info(),
            "themes": theme_mod.list_themes(),
            "theme_default": theme_mod.DEFAULT_THEME_ID,
            "material": {"packs": _replay_packs(),
                         "pages_range": list(MATERIAL_PAGES),
                         "suffixes": list(MATERIAL_SUFFIXES),
                         "default_model": "qwen3.8:27b-wb65536",
                         "model_note": "真实生成会调用本地模型进行内容规划与语义配图；体验回放只跳过内容规划模型，下游语义配图仍可能调用本地模型，照片检索也可能联网。回放不计作真实模型生成证据。",
                         "pexels_api_configured": bool(os.environ.get("PEXELS_API_KEY")),
                         "pexels_source_url": "https://www.pexels.com/"},
            "rs_checks": rs_checks,
            "rs_note": "RS 条件必检项：JobOptions 打开对应能力时到期；模块未就位记 "
                       "NOT_EVALUABLE（阻断），不冒充通过。",
            "bind": "127.0.0.1（仅本机回环）",
        })

    def _material_start(self):
        code, payload = _mt_start(self._body_json() or {})
        return self._send(code, payload)

    def _material_list(self):
        return self._send(200, {"tasks": [_mt_public(r) for r in _mt_list()],
                                "note": "任务记录落盘在 <数据根>/material_tasks；刷新页面据此恢复显示。"})

    def _material_one(self, tid):
        code, payload = _mt_status(tid)
        return self._send(code, payload)

    def _material_cancel(self, tid):
        code, payload = _mt_cancel(tid)
        return self._send(code, payload)

    def _material_retry(self, tid):
        code, payload = _mt_retry(tid)
        return self._send(code, payload)

    def _job_objects(self, job_id):
        code, payload = _job_objects(job_id)
        return self._send(code, payload)

    def _edit_detail(self, job_id, vid):
        rec = _edit_record(job_id, vid)
        if not rec:
            return self._err(404, "VERSION.NOT_FOUND", "版本不存在。")
        prev = {}
        for side, key in (("before", "pre_copy"), ("after", "candidate_pptx")):
            p = Path(str(rec.get(key) or ""))
            if not p.is_file():
                prev[side] = {"available": False}
                continue
            cache = _edit_cache_dir(p)
            pngs = sorted(x.name for x in (cache / "png").glob("*.png"))
            pdfs = sorted(x.name for x in cache.glob("*.pdf"))
            prev[side] = {"available": bool(pngs), "pages": pngs,
                          "pdf": pdfs[0] if pdfs else None,
                          "sha256": sha256_file(p)}
        return self._send(200, {"record": rec, "preview": prev,
                                "requires_revalidation": True,
                                "note": "修改候选是独立文件，不覆盖有效版本；接纳/恢复后都需重新验收。"})

    def _edit_render(self, job_id, vid):
        rec = _edit_record(job_id, vid)
        if not rec:
            return self._err(404, "VERSION.NOT_FOUND", "版本不存在。")
        sides, err = _render_edit_pair(job_id, vid, rec)
        if err:
            return self._err(409, err, "预览渲染失败或不可用。")
        return self._send(200, {"rendered": True, "sides": sides,
                                "page": (rec.get("edit") or {}).get("page"),
                                "note": "修改前后由同一渲染器真实渲染，按文件哈希缓存。"})

    def _edit_preview_file(self, job, vid, q):
        side = (q.get("side") or [""])[0]
        name = (q.get("name") or [""])[0]
        if not re.fullmatch(r"[A-Za-z0-9-]+", job) or not re.fullmatch(r"v\d{4}", vid):
            return self._err(400, "WB.BAD_FILE", "非法版本参数。")
        if side not in ("before", "after"):
            return self._err(400, "WB.BAD_SIDE", "非法预览侧。")
        if not FILE_NAME.fullmatch(name):
            return self._err(400, "WB.BAD_FILE", "非法文件名。")
        rec = _edit_record(job_id=job, vid=vid)
        if not rec:
            return self._err(404, "VERSION.NOT_FOUND", "版本不存在。")
        key = "pre_copy" if side == "before" else "candidate_pptx"
        p = Path(str(rec.get(key) or ""))
        if not p.is_file():
            return self._err(404, "WB.FILE_MISSING", "源文件不存在。")
        base = _edit_cache_dir(p).resolve()
        target = (base / name).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            return self._err(403, "WB.PATH_ESCAPE", "路径越界，已拒绝。")
        if not target.is_file():
            return self._err(404, "WB.FILE_MISSING", "预览尚未生成；先请求渲染。")
        return self._send_file(target)

    def _edit_restore(self, job_id, vid):
        import sys as _sys
        _sys.path.insert(0, str(PROG))
        from wengao_ppt.local_edit import EditScopeError, restore_version
        d = ROOT / "jobs" / job_id
        if load_json(d / "job.json") is None:
            return self._err(404, "WB.JOB_NOT_FOUND", "任务不存在。")
        try:
            rec = restore_version(d, vid)
        except EditScopeError as e:
            return self._err(409, e.code, e.detail)
        return self._send(200, {"restored": True, "record": rec,
                                "requires_revalidation": True,
                                "job_verdict_unchanged": (load_json(d / "job.json") or {}).get("verdict"),
                                "note": "恢复产物是新候选：当前任务判定不变，必须重新验收后才可导出。"})

    def _edit_adopt(self, job_id, vid):
        from wengao_ppt import theme as theme_mod
        rec = _edit_record(job_id, vid)
        if not rec:
            return self._err(404, "VERSION.NOT_FOUND", "版本不存在。")
        src = Path(str(rec.get("candidate_pptx") or ""))
        if not src.is_file():
            return self._err(409, "WB.FILE_MISSING", "修改候选文件不存在。")
        job = load_json(ROOT / "jobs" / job_id / "job.json") or {}
        theme = job.get("theme") if job.get("theme") in theme_mod.THEMES else theme_mod.DEFAULT_THEME_ID
        rc, obj, out, err = cli(ROOT, "precheck", "--input", str(src),
                                "--mode", "beautify", "--theme", theme)
        ok = rc == 0 and bool((obj or {}).get("job_id"))
        return self._send(200 if ok else 409, {
            "adopted": ok,
            "job_id": (obj or {}).get("job_id"),
            "source_version": vid,
            "theme": theme,
            "precheck": obj,
            "detail": None if ok else ((obj or {}).get("detail") or (out or err)[-400:]),
            "note": ("已把修改候选作为**新任务**送入既有验收链；原件与旧任务不变。"
                     if ok else "接纳被后端拒绝；未创建任务。"),
        })

    def _asset_upload(self):
        if getattr(self, "_body_consumed", False):
            return self._err(500, "WB.BODY_ALREADY_CONSUMED", "请求体已被前置逻辑读取，不可重复消费。")
        self._body_consumed = True
        raw = self.headers.get("X-Filename") or ""
        name = urllib.parse.unquote(raw)
        name = SAFE_NAME.sub("_", Path(name).name)[:80]
        if not name.lower().endswith((".png", ".jpg", ".jpeg")):
            return self._err(400, "WB.BAD_ASSET", "图片资产只接受 .png/.jpg/.jpeg。")
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return self._err(400, "WB.EMPTY_UPLOAD", "上传内容为空。")
        if n > 8 * 1024 * 1024:
            return self._err(413, "WB.ASSET_TOO_LARGE", "图片资产上限 8 MiB。")
        dest_dir = ROOT / "assets"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / (os.urandom(4).hex() + "_" + name)
        with open(dest, "wb") as f:
            remaining = n
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 1 << 20))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)
        return self._send(200, {"uploaded": True, "path": str(dest),
                                "name": dest.name, "bytes": dest.stat().st_size,
                                "note": "资产落在数据根的 assets/（受控目录），不接受任意本机路径。"})

    def _history(self):
        return self._send(200, _history_data())

    # ---------------- 具体处理 ----------------
    def _bootstrap(self):
        from wengao_ppt import __version__, theme as theme_mod
        # 契约与样例随**程序**走；jobs/previews 随**数据根**走（两者可分离）
        matrix = load_json(PROG / "contracts" / "support_matrix_v1.json") or {}
        samples = []
        for sub in ("material", "d10a", "d10b"):
            base = PROG / "samples" / sub
            if base.exists():
                samples += [{"path": str(p.relative_to(PROG)), "name": p.name,
                             "bytes": p.stat().st_size}
                            for p in sorted(base.rglob("*.pptx"))]
        return self._send(200, {
            "product": "稳稿 PPT 本地工作台",
            "runner_version": __version__,
            "label": "DEVELOPMENT_CANDIDATE",
            "release_ready": False,
            "bind": "127.0.0.1（仅本机回环）",
            # D10-B：主题清单来自可信注册表，含 theme_version 与有效配置摘要
            "themes": theme_mod.list_themes(),
            "theme_registry_version": theme_mod.REGISTRY_VERSION,
            "theme_default": theme_mod.DEFAULT_THEME_ID,
            "support_profile_default": "P1_TEXT_LIMITED",
            "support_matrix": {
                "scope_statement": matrix.get("scope_statement"),
                "modes": sorted((matrix.get("modes") or {}).keys()),
                "unsupported": [o.get("object") for o in (matrix.get("object_matrix") or [])
                                if o.get("status") == "unsupported"],
            },
            "samples": samples,
            "jobs": WB.jobs_list(),
            "instance": _instance_info(),
            "material": {"packs": _replay_packs(),
                         "pages_range": list(MATERIAL_PAGES),
                         "suffixes": list(MATERIAL_SUFFIXES),
                         "default_model": "qwen3.8:27b-wb65536",
                         "pexels_api_configured": bool(os.environ.get("PEXELS_API_KEY")),
                         "pexels_source_url": "https://www.pexels.com/"},
        })

    def _upload_bytes(self, allowed_suffixes, *, type_code, type_message):
        if getattr(self, "_body_consumed", False):
            return self._err(500, "WB.BODY_ALREADY_CONSUMED", "请求体已被前置逻辑读取，不可重复消费。")
        self._body_consumed = True
        raw_name = self.headers.get("X-Filename") or ""
        name = urllib.parse.unquote(raw_name)
        name = SAFE_NAME.sub("_", Path(name).name)[:80] or "upload.pptx"
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return self._err(400, "WB.BAD_UPLOAD_LENGTH", "上传长度无效。")
        if n <= 0:
            return self._err(400, "WB.EMPTY_UPLOAD", "上传内容为空。")
        if n > MAX_UPLOAD:
            return self._err(413, "WB.UPLOAD_TOO_LARGE", f"文件超过 {MAX_UPLOAD // 1048576} MiB 上限。")
        suffix = Path(name).suffix.lower()
        if suffix not in allowed_suffixes:
            return self._err(400, type_code,
                             f"{type_message}（收到 {suffix or '无后缀'}）。")
        dest = UPLOAD_DIR / (os.urandom(4).hex() + "_" + name)
        partial = dest.with_name(dest.name + ".partial")
        remaining = n
        try:
            with open(partial, "xb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 1 << 20))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
            if remaining:
                partial.unlink(missing_ok=True)
                return self._err(400, "WB.INCOMPLETE_UPLOAD", "上传内容不完整，未保留该文件。")
            partial.replace(dest)
        except Exception:
            partial.unlink(missing_ok=True)
            raise
        return self._send(200, {"uploaded": True, "path": str(dest),
                                "name": dest.name, "bytes": dest.stat().st_size,
                                "sha256": sha256_file(dest)})

    def _upload(self):
        # Existing-deck upload remains PPTX-only; materials use their own route.
        return self._upload_bytes(
            (".pptx",), type_code="WB.BAD_UPLOAD_TYPE",
            type_message="原稿上传只接受 .pptx；图片资产请走 /api/assets")

    def _material_upload(self):
        # Keep material ingest distinct from the existing-deck route so a text file
        # can never be mistaken for a presentation upload.
        return self._upload_bytes(
            self.MATERIAL_SUFFIXES, type_code="WB.BAD_MATERIAL_TYPE",
            type_message="材料上传仅接受 .md/.markdown/.txt/.text/.docx；已有 PPTX 请走演示工作室")

    def _create_job(self):
        """创建任务并预检：来源仅限样例目录或上传目录，经真实 CLI precheck 入口。"""
        from wengao_ppt import theme as theme_mod
        body = self._body_json()
        src = body.get("source") or ""
        theme = body.get("theme") or theme_mod.DEFAULT_THEME_ID
        # 主题只接受注册表内的 ID（不接受路径/URL/自定义配置）；与 CLI 使用同一注册表。
        if theme not in theme_mod.THEMES:
            return self._send(400, {"error": {
                "code": "THEME.UNKNOWN",
                "message": f"主题 {theme!r} 不在可信主题注册表内；可用："
                           f"{'、'.join(sorted(theme_mod.THEMES))}。"}})
        p = Path(src)
        if not p.is_absolute():
            # 样例随程序走（安装实例的数据根里没有 samples/），上传件在数据根。
            # 找不到时**仍按 ROOT 解析**：否则相对路径会落到 PROG 下面，
            # 后面"不在允许目录内"的判断会抢在"文件不存在"之前，
            # 把"路径写错了"报成"来源不允许"（auto30 A26 的同一类混淆）。
            p = (ROOT / src) if (ROOT / src).exists() else (
                (PROG / src) if (PROG / src).exists() else (ROOT / src))
            # A27：/api/upload 返回的是绝对路径，但调用方常只用 `name` 字段（
            # 前缀过的上传文件名）。这里补一次"上传目录里按名字找"的兜底，
            # 不再让调用方自己拼路径。
            if not p.exists():
                direct = UPLOAD_DIR / Path(src).name
                hit = ([direct] if direct.is_file()
                       else sorted(UPLOAD_DIR.glob("*_" + Path(src).name)))
                if hit:
                    p = hit[-1]
        p = p.resolve()
        allowed = False
        for base in (PROG / "samples", UPLOAD_DIR):
            try:
                p.relative_to(base.resolve())
                allowed = True
                break
            except ValueError:
                pass
        if not allowed:
            return self._err(400, "WB.BAD_SOURCE",
                             "文件来源不在允许的样例/上传目录内；不接受任意磁盘路径。")
        if not p.is_file():
            return self._err(400, "WB.BAD_SOURCE", "文件不存在。")
        if p.suffix.lower() != ".pptx":
            return self._err(400, "WB.BAD_SOURCE",
                             "本轮工作台仅支持普通 .pptx 原稿（其他输入形态归 P4）。")
        rc, obj, out, err = cli(ROOT, "precheck", "--input", str(p),
                                "--mode", "beautify", "--theme", theme)
        if rc != 0 or not obj:
            # 预检未通过：如实回传后端原因码与说明，前端不得自行改写为"可处理"。
            # A12/A13/A14：此前这里是 HTTP 200 + {"created":false,"gate":…}，
            # 而同接口的其它错误（主题不在注册表等）是 4xx + {"error":{"code":…}}——
            # 调用方必须写两套分支才知道"到底成没成"。
            # 现在统一成 422 + 单一信封：既有 error.code（与其它拒绝一致），
            # 也保留 created/gate/detail（旧调用方不用改就能读）。
            gate = (obj or {}).get("gate") or (obj or {}).get("code")
            detail = (obj or {}).get("detail") or (out or err)[-600:]
            return self._send(422, {
                "created": False, "rc": rc, "gate": gate, "detail": detail,
                "error": {"code": gate or "GATE.PRECHECK_FAILED",
                          "message": detail or "预检未通过，未创建任务。",
                          "reason_code": "GATE.PRECHECK_FAILED",
                          "next_actions": [
                              {"action": "upload", "endpoint": "POST /api/upload",
                               "label": "重新上传一份能被解析的 .pptx 原稿"},
                              {"action": "precheck",
                               "endpoint": "CLI: wengao-ppt precheck --input <file>",
                               "label": "先在命令行跑一次预检看真实原因码"}]}})
        return self._send(200, {"created": True, "rc": rc, "precheck": obj})

    def _cleanup(self):
        """回收上传件/预览图：**只列清单**（实际移动本轮未开放，见 cleanup.EXECUTION_OPEN）。"""
        from wengao_ppt import cleanup as cleanup_mod
        body = self._body_json() or {}
        target = body.get("target") or "uploads"
        if target not in ("uploads", "previews"):
            return self._err(400, "WB.BAD_CLEANUP_TARGET",
                             "target 只能是 uploads / previews；"
                             "任务目录（jobs/）不在清理范围内。")
        # 2026-10-18 复核：apply 入口在普通产品中不开放（暂缓清理）。
        # 请求方带了 apply=true 时如实告知"未执行"，不静默忽略、不移动文件。
        requested_apply = bool(body.get("apply"))
        try:
            res = cleanup_mod.plan(ROOT, target=target,
                                   older_than_hours=body.get("older_than_hours"),
                                   job_id=body.get("job_id"),
                                   limit=int(body.get("limit") or 200))
        except (TypeError, ValueError) as e:  # noqa: BLE001
            return self._err(400, "WB.BAD_CLEANUP_PARAM",
                             f"清理参数不合法：{type(e).__name__}: {e}")
        if not res.get("ok"):
            return self._err(404, res.get("code") or "WB.CLEANUP_FAILED",
                             res.get("detail") or "清理不可用。")
        res["execution_open"] = cleanup_mod.EXECUTION_OPEN
        res["applied"] = False
        if requested_apply and not cleanup_mod.EXECUTION_OPEN:
            res["deferred"] = True
            res["note"] = cleanup_mod.EXECUTION_DEFERRED_NOTE
        res["next_actions"] = [{
            "action": "review",
            "label": ("以上为只读清单，未移动任何文件；实际移动本轮未开放"
                      "（暂缓清理），需要时按 _trash 里的 manifest.json 取回历史内容。")}]
        return self._send(200, res)

    def _job_action(self, job_id, action):
        if WB.job_state(job_id) is None:
            return self._err(404, "WB.JOB_NOT_FOUND", "任务不存在。")
        if action in ("run", "validate"):
            if action == "run":
                # 配图环节可能需要本机生图兜底（每张数十秒到数分钟），900 秒对
                # 多场景稿件不够用，会被误判为 VISUAL.SCENE_PLAN_FAILED/timeout。
                args = ["run", "--job-id", job_id, "--timeout", "3600"]
                run_body = self._body_json() or {}
                # 默认开启（同 /api/make）：显式传 false 才退回严格整单阻断。
                if run_body.get("allow_text_card_fallback", True):
                    args.append("--allow-text-card-fallback")
                # 取消测试接缝：仅在显式开启时接受 hold（生成完毕、发布之前停留 N 秒），
                # 让"运行中取消"有真实可观测的时间窗；默认不启用。
                if os.environ.get("WENGAO_WB_HOLD_SEAM") == "1":
                    try:
                        hold = float(run_body.get("hold") or 0)
                    except (TypeError, ValueError):
                        hold = 0.0
                    if hold > 0:
                        args += ["--hold-after-build", str(hold)]
                r = WB.spawn(job_id, "run", args)
            else:
                r = WB.spawn(job_id, "validate", ["validate", "--job-id", job_id])
            if "error" in r:
                return self._send(409, r)
            return self._send(202, r)
        if action == "cancel":
            busy = WB.op_busy(job_id)
            rc, obj, out, err = cli(ROOT, "cancel", "--job-id", job_id, timeout=120)
            return self._send(200, {"cancel_rc": rc, "record": obj,
                                    "op_was_running": bool(busy),
                                    "stdout_tail": out[-800:], "stderr_tail": err[-400:]})
        if action == "export":
            busy = WB.op_busy(job_id)
            if busy:
                return self._send(409, {"error": {
                    "code": "WB.OP_IN_PROGRESS",
                    "message": f"该任务已有进行中的操作（{busy['op']}）；"
                               "导出只允许对当前静止状态执行。"}})
            rc, obj, out, err = cli(ROOT, "export", "--job-id", job_id, timeout=300)
            if rc != 0:
                # 门禁拒绝：如实返回后端码与细节（前端不得自行判定成功）
                gate = obj or {}
                detail = gate.get("detail") or gate.get("gate") or out[-500:]
                return self._send(200, {"exported": False, "rc": rc,
                                        "gate": gate.get("gate") or gate.get("code"),
                                        "detail": detail})
            return self._send(200, {"exported": True, "rc": rc, "manifest": obj})
        return self._err(400, "WB.BAD_ACTION", "未知操作。")

    def _job_result(self, job_id):
        """结果清单：只列出导出清单登记过的可下载项（名字 + 预期哈希 + 下载链接）。"""
        if WB.job_state(job_id) is None:
            return self._err(404, "WB.JOB_NOT_FOUND", "任务不存在。")
        base, index, code, msg = WB.export_index(job_id)
        if code:
            # A22：重跑会把上一尝试的导出目录移进 export_history，旧文件其实还在盘上。
            # 这里把"上一次交付在哪、还在不在、哈希多少"如实列出来，
            # 让"上一版还能按身份取回"有入口，而不是只剩一句 GATE.NOT_VALIDATED。
            job = load_json(ROOT / "jobs" / job_id / "job.json") or {}
            hist = []
            for h in list(reversed(job.get("export_history") or []))[:5]:
                hp = Path(h.get("path") or "")
                man = (load_json(hp / "release_manifest.json")
                       or load_json(hp / "manifest.json") or {}) if hp.is_dir() else {}
                pptx = sorted(hp.rglob("*.pptx"))[0] if hp.is_dir() and sorted(hp.rglob("*.pptx")) else None
                hist.append({"path": str(hp), "attempt_id": h.get("attempt_id"),
                             "archived_at": h.get("archived_at"),
                             "still_on_disk": hp.is_dir(),
                             "pptx": str(pptx) if pptx else None,
                             "pptx_sha256": sha256_file(pptx) if pptx else None,
                             "manifest_sha256": ((man.get("output") or {})
                                                 .get("sha256")),
                             "note": h.get("note")})
            return self._send(200, {"ready": False, "code": code, "message": msg,
                                    "previous_exports": hist,
                                    "note": ("列表里的旧导出仍按身份可追溯，"
                                             "但它们不属于当前尝试的结果，"
                                             "不能当作当前可交付成品。") if hist else None})
        items = []
        for rel, meta in sorted(index.items()):
            items.append({
                "kind": meta["kind"], "name": rel,
                "expected_sha256": meta["expected"],
                "bytes": (base / rel).stat().st_size if (base / rel).is_file() else None,
                "url": f"/api/jobs/{job_id}/download?name={urllib.parse.quote(rel)}",
            })
        return self._send(200, {"ready": True, "job_id": job_id,
                                "export_dir": str(base), "items": items})

    def _native_review_copy(self, job_id, query):
        """Serve the exact candidate only for native inspection, outside the export list."""
        import hashlib
        from wengao_ppt.core import sha256_file
        job_dir = ROOT / "jobs" / job_id
        job = load_json(job_dir / "job.json")
        validation = load_json(job_dir / "validation.json")
        if not job or not validation:
            return self._err(404, "WB.NATIVE_REVIEW_NOT_READY",
                             "当前任务尚无候选验收记录。")
        candidate = job_dir / "candidate" / (
            Path(job.get("input_path") or "").stem + ".themed.pptx")
        if not candidate.is_file():
            return self._err(404, "WB.NATIVE_REVIEW_CANDIDATE_MISSING",
                             "当前候选 PPTX 不存在。")
        busy = WB.op_busy(job_id)
        owner = WB.writer_owner(job_id)
        if busy or owner:
            return self._send(409, {"error": {
                "code": "WB.OP_IN_PROGRESS",
                "message": "该任务正在修改或验收；等本轮结束后再取复核副本。",
                "operation": busy, "writer": owner}})
        data = candidate.read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        expected = (query.get("sha256") or [None])[0]
        if actual != validation.get("candidate_pptx_sha256") or expected != actual:
            return self._err(409, "EVIDENCE.STALE",
                             "候选已变化；刷新工作台后再取原生复核副本。")
        if (validation.get("layout_hard_gate") or {}).get("status") == "EXECUTED":
            return self._err(409, "WB.NATIVE_REVIEW_NOT_REQUIRED",
                             "当前候选的原生硬门禁已完整执行；不提供待复核副本。")
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.presentationml.presentation")
        self.send_header("Content-Disposition",
                         f'attachment; filename="wengao-native-review-{actual[:12]}.pptx"')
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-WG-Review-Only", "true")
        self.send_header("X-WG-Candidate-SHA256", actual)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _native_playback_upload(self, job_id):
        """Receive bounded MP4 + reviewer JSON, then bind both to current job candidate."""
        import base64
        import binascii
        from wengao_ppt.runner import Runner, GateError
        if WB.job_state(job_id) is None:
            return self._err(404, "WB.JOB_NOT_FOUND", "任务不存在。")
        busy = WB.op_busy(job_id)
        owner = WB.writer_owner(job_id)
        if busy or owner:
            return self._send(409, {"error": {
                "code": "WB.OP_IN_PROGRESS",
                "message": "该任务正在修改或验收；等本轮结束后再附加证据。",
                "operation": busy, "writer": owner}})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        max_bytes = 128 * 1024 * 1024
        if n <= 0:
            return self._err(400, "WB.NATIVE_VIDEO_EMPTY", "录像上传内容为空。")
        if n > max_bytes:
            return self._err(413, "WB.NATIVE_VIDEO_TOO_LARGE",
                             "原生实播录像上限为 128 MiB。")
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "video/mp4":
            return self._err(415, "WB.NATIVE_VIDEO_TYPE",
                             "原生实播证据只接受 MP4 视频。")
        expected_candidate_sha = (self.headers.get("X-WG-Expected-Candidate-SHA256") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_candidate_sha):
            return self._err(400, "WB.NATIVE_CANDIDATE_IDENTITY",
                             "缺少当前候选的完整 SHA-256 身份。")
        validation = load_json(ROOT / "jobs" / job_id / "validation.json") or {}
        if validation.get("candidate_pptx_sha256") != expected_candidate_sha:
            return self._err(409, "EVIDENCE.STALE",
                             "该 SHA 尚未由当前任务验收记录绑定；请刷新任务状态后重试。")
        if (validation.get("layout_hard_gate") or {}).get("status") == "EXECUTED":
            return self._err(409, "WB.NATIVE_REVIEW_NOT_REQUIRED",
                             "当前候选的原生硬门禁已完整执行；不再接收额外复核证据。")
        record_b64 = self.headers.get("X-WG-Native-Review-Record-B64") or ""
        if not record_b64 or len(record_b64) > 48 * 1024:
            return self._err(400, "WB.NATIVE_REVIEW_RECORD",
                             "缺少复核记录，或 Base64 记录超过 48 KiB 限额。")
        if getattr(self, "_body_consumed", False):
            return self._err(500, "WB.BODY_ALREADY_CONSUMED", "请求体已被前置逻辑读取，不可重复消费。")
        self._body_consumed = True
        video_bytes = self.rfile.read(n)
        if len(video_bytes) != n:
            return self._err(400, "WB.NATIVE_VIDEO_TRUNCATED", "录像上传未完整接收。")
        try:
            record_bytes = base64.b64decode(record_b64.encode("ascii"), validate=True)
            record = json.loads(record_bytes.decode("utf-8"))
        except (UnicodeError, ValueError, binascii.Error, json.JSONDecodeError):
            return self._err(400, "WB.NATIVE_REVIEW_RECORD",
                             "复核记录不是有效 Base64 UTF-8 JSON。")
        if not isinstance(record, dict):
            return self._err(400, "WB.NATIVE_REVIEW_RECORD",
                             "复核记录顶层必须是 JSON 对象。")
        try:
            attached = Runner(ROOT).attach_native_playback_evidence(
                job_id, video_bytes, record, expected_candidate_sha)
        except GateError as exc:
            return self._err(409, exc.code, exc.detail)
        return self._send(201, {"ok": True, **attached,
                                "validation_required": True})

    def _download(self, job_id, q):
        """按导出清单下载成品：只认清单登记过的名字，发出前逐字节核对整文件 SHA256。

        整文件 SHA256 在这里是**下载绑定**依据：磁盘文件与清单登记不符即拒绝发送，
        不做任何元数据剥离，也不把"看起来一样"当作通过。
        """
        if WB.job_state(job_id) is None:
            return self._err(404, "WB.JOB_NOT_FOUND", "任务不存在。")
        base, index, code, msg = WB.export_index(job_id)
        if code:
            # 交付资格类拒绝是"当前不可交付"，不是服务端故障：用 409 明确表达。
            status = 409 if code in ("EXPORT.NOT_READY", "INCIDENT.BLOCKED",
                                     "EVIDENCE.STALE", "GATE.NOT_VALIDATED",
                                     "GATE.IN_PROGRESS", "GATE.RELEASE_BLOCKED",
                                     "EVIDENCE.UNREADABLE") else 500
            return self._send(status, {"error": {"code": code, "message": msg}})
        name = (q.get("name") or [""])[0]
        meta = index.get(name)
        if meta is None:
            return self._err(404, "WB.BAD_FILE",
                             "该文件不在导出清单内；只允许下载清单登记的结果。")
        target = (base / name).resolve()
        try:
            target.relative_to(base.resolve())
        except ValueError:
            return self._err(403, "WB.PATH_ESCAPE", "路径越界，已拒绝。")
        if not target.is_file():
            return self._err(404, "WB.FILE_MISSING", "结果文件不存在。")
        if not meta.get("expected"):
            return self._send(409, {"error": {
                "code": "EVIDENCE.STALE",
                "message": "清单未登记该文件的预期哈希，无法完成下载绑定。"}})
        actual = sha256_file(target)
        if actual != meta["expected"]:
            return self._send(409, {"error": {
                "code": "EVIDENCE.STALE",
                "message": "结果文件与导出清单登记的哈希不一致，已拒绝下载。",
                "expected": meta["expected"], "actual": actual}})
        import mimetypes
        data = target.read_bytes()
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition",
                         f'attachment; filename="{target.name}"')
        self.send_header("X-Verified-Sha256", actual)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _preview_file(self, q):
        job = (q.get("job") or [""])[0]
        side = (q.get("side") or [""])[0]
        name = (q.get("name") or [""])[0]
        if not re.fullmatch(r"[A-Za-z0-9-]+", job) or not FILE_NAME.fullmatch(name):
            return self._err(400, "WB.BAD_FILE", "非法文件参数。")
        if side == "candidate":
            d = ROOT / "jobs" / job
            base = (d / "candidate" / "render").resolve()
            # 与 /pages 用**同一个资格判断**：失效结果不提供页图，
            # 页图还必须绑定到当前候选，不能把旧图当当前结果。
            from wengao_ppt import incident as incident_mod
            dl = incident_mod.delivery_state(d, ROOT)
            if not dl["allowed"] and dl["code"] == "INCIDENT.BLOCKED":
                return self._err(409, "INCIDENT.BLOCKED",
                                 "该结果已被事故台账标记为不可交付，页图不再提供。")
            jobj = load_json(d / "job.json") or {}
            stem = Path(jobj.get("input_path") or "").stem
            cand = d / "candidate" / f"{stem}.themed.pptx"
            meta = load_json(base / "render_meta.json") or {}
            if not meta:
                return self._err(409, "RENDER.UNBOUND_CACHE",
                                 "页图缺少候选/环境绑定，不能作为当前预览。")
            if cand.is_file() and meta.get("candidate_sha256") != sha256_file(cand):
                return self._err(409, "RENDER.STALE_BINDING",
                                 "页图对应的是另一版候选，不提供旧图。")
        elif side == "baseline":
            f, _src = _baseline_file(job, allow_original=False)
            if f is None or not f.is_file():
                return self._err(404, "WB.NO_BASELINE", "该任务还没有可用基线。")
            base = _preview_cache_dir("baseline", f).resolve()
        elif side == "original":
            st = WB.job_state(job)
            if st is None:
                return self._err(404, "WB.JOB_NOT_FOUND", "任务不存在。")
            src = Path(st["job"]["input_path"])
            if not src.exists():
                return self._err(404, "INPUT.SOURCE_BASELINE_MISSING", "原稿不可用。")
            base = _preview_cache_dir("original", src).resolve()
        else:
            return self._err(400, "WB.BAD_SIDE", "非法预览侧。")
        target = (base / name).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            return self._err(403, "WB.PATH_ESCAPE", "路径越界，已拒绝。")
        if not target.is_file():
            return self._err(404, "WB.FILE_MISSING", "文件不存在。")
        return self._send_file(target)


def main():
    global ROOT, UPLOAD_DIR, PREVIEW_DIR, MAKE
    ap = argparse.ArgumentParser(description="稳稿 PPT 本地工作台（仅回环）")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--project-root", default=str(ROOT),
                    help="数据根（jobs/results/uploads）；默认=程序目录。"
                         "安装布局下由 wengao.install 指向共享数据根。")
    args = ap.parse_args()
    # 数据根与程序目录分离：程序来自当前版本目录，运行数据落共享根（不写进版本目录）
    ROOT = Path(args.project_root).resolve()
    ROOT.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR = ROOT / "workbench_uploads"
    PREVIEW_DIR = ROOT / "workbench_previews"
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    MAKE = MakeOrchestrator(ROOT, PROG)
    # 安全边界：无论参数如何，只绑本机回环
    host = "127.0.0.1"
    srv = ThreadingHTTPServer((host, args.port), Handler)
    srv.daemon_threads = True
    print(f"稳稿 PPT 本地工作台已启动：http://{host}:{args.port}/ "
          f"（仅本机回环；DEVELOPMENT_CANDIDATE / release_ready=false）", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
