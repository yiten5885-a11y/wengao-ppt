"""workbench.ui_api —— 新工作台界面（RS-9）的后端接口。

边界（继承 server.py 顶部说明）：
- 只服务本机回环；所有数据来自后端事件与证据文件，**前端不编造**。
- 探测面板只显示真实探测结果；密钥只报"是否配置"，绝不回显。
- 新界面挂在 /ui 与 /api/... 新端点下；旧四页界面与旧路由保持原样。

端点一览：
GET  /ui                    新界面入口（index.html）
GET  /ui/app.js /ui/styles.css
GET  /api/overview          演示列表 + 计数（左栏）
GET  /api/localrun          本机运行探测（语义模型/联网检索/页面渲染/本地生图/视觉模型）
GET  /api/jobs/<id>/events  事件流（SSE follow 或一次性 JSON）
GET  /api/jobs/<id>/research  来源标签数据（ResearchBundle 摘要）
GET  /api/jobs/<id>/images    配图标签数据（W1 账本摘要）
GET  /api/jobs/<id>/checks    本页检查数据（各 check_* 报告汇总）
GET  /api/jobs/<id>/decisions 冲突裁决现状
GET  /api/jobs/<id>/attribution 导出用"来源与署名清单"
POST /api/ui/jobs/<id>/decisions  记录冲突裁决并局部重排（/api/jobs/<id>/decisions 亦兼容）
POST /api/ui/jobs/<id>/page-ops   页级操作（换版式/插页/换图/转场开关）（/api/jobs/<id>/page-ops 亦兼容）
POST /api/ui/jobs/<id>/export     导出 PPTX/PDF（可附来源与署名清单）
POST /api/ui/jobs/<id>/cancel     取消制作（写 cancel.request 并通知 runner）
POST /api/ui/jobs/<id>/retry      重试任务（/api/jobs/<id>/retry 亦兼容）
GET  /api/env/libreoffice/status  本机 LibreOffice 探测结果
GET  /api/env/libreoffice/plan    一键安装会拉取的版本/体积/校验值（只读，不下载）
POST /api/env/libreoffice/install 触发一键安装（异步，用进度端点轮询）
GET  /api/env/libreoffice/install/progress  安装进度轮询
GET  /api/env/cloud-model/status  云端模型配置状态（不回显 key 明文）
POST /api/env/cloud-model/config  保存云端模型配置（用户显式开启，默认关闭）
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
from pathlib import Path

# ---------------------------------------------------------------- 常量
UI_DIR = Path(__file__).resolve().parent / "ui"
SSE_HEARTBEAT_S = 15

# ---------------------------------------------------------------- LibreOffice 一键安装
# 单进程内存状态：本服务是单机单实例本地工具，不需要跨进程持久化安装进度。
_LO_INSTALL_LOCK = threading.Lock()
_LO_INSTALL_STATE: dict = {"stage": "idle", "detail": {}}


def _lo_install_worker():
    from wengao_ppt import libreoffice_setup as lo

    def progress(stage, detail):
        with _LO_INSTALL_LOCK:
            _LO_INSTALL_STATE["stage"] = stage
            _LO_INSTALL_STATE["detail"] = detail

    try:
        result = lo.install(progress=progress)
        with _LO_INSTALL_LOCK:
            _LO_INSTALL_STATE["stage"] = "succeeded"
            _LO_INSTALL_STATE["detail"] = result
    except Exception as e:  # noqa: BLE001
        code = getattr(e, "code", "LO_SETUP.UNEXPECTED_ERROR")
        detail = getattr(e, "detail", str(e))
        with _LO_INSTALL_LOCK:
            _LO_INSTALL_STATE["stage"] = "failed"
            _LO_INSTALL_STATE["detail"] = {"code": code, "detail": detail}


def _load_json(p: Path):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- 探测
def _probe_loopback(url: str, timeout_s: int = 5, retries: int = 2):
    """本机回环探测：失败重试（模型服务加载/高负载瞬间可能拒绝连接）。"""
    from wengao_ppt.netsafe import safe_urlopen
    last = None
    for i in range(retries + 1):
        try:
            return safe_urlopen(url, allow_loopback=True, timeout_s=timeout_s)
        except Exception as e:  # noqa: BLE001
            last = e
            if i < retries:
                time.sleep(0.5)
    raise last


def probe_localrun(root: Path) -> dict:
    """本机运行面板：只报真实探测结果。任何一项探测失败都如实显示，不猜。"""
    rows = []

    # 1. 语义模型（本机回环：mlx-dspark / 通用 llama.cpp 风格 / Ollama）
    model = {"key": "semantic_model", "label": "语义模型", "ok": False,
             "detail": "未探测到本机模型服务"}
    try:
        health = json.loads(_probe_loopback("http://127.0.0.1:18088/health").decode("utf-8"))
        model = {"key": "semantic_model", "label": "语义模型", "ok": True,
                 "detail": f"mlx-dspark，已加载 {health.get('model') or '模型'}",
                 "models": [health.get("model")] if health.get("model") else []}
    except Exception:
        try:
            ver = json.loads(_probe_loopback("http://127.0.0.1:11434/api/version").decode("utf-8")).get("version", "")
            tags = json.loads(_probe_loopback("http://127.0.0.1:11434/api/tags").decode("utf-8"))
            names = [m.get("name") for m in tags.get("models") or []]
            model = {"key": "semantic_model", "label": "语义模型", "ok": True,
                     "detail": f"Ollama {ver}，{len(names)} 个模型",
                     "models": names[:8]}
        except Exception as e:  # noqa: BLE001
            model["detail"] = f"未探测到（{type(e).__name__}）"
    rows.append(model)

    # 2. 联网检索（SearXNG，固定 127.0.0.1:8890）
    search = {"key": "web_search", "label": "联网检索", "ok": False,
              "detail": "未探测到检索服务"}
    try:
        json.loads(_probe_loopback("http://127.0.0.1:8890/stats").decode("utf-8"))
        search = {"key": "web_search", "label": "联网检索", "ok": True,
                  "detail": "SearXNG 本机自托管（8890）"}
    except Exception as e:  # noqa: BLE001
        search["detail"] = f"未探测到（{type(e).__name__}）"
    rows.append(search)

    # 3. 页面渲染（LibreOffice）
    render = {"key": "render", "label": "页面渲染", "ok": False,
              "detail": "未找到 LibreOffice"}
    try:
        from wengao_ppt import render as render_mod
        soffice, src = render_mod.find_soffice()
        if soffice:
            render = {"key": "render", "label": "页面渲染", "ok": True,
                      "detail": f"LibreOffice（{src}）"}
        else:
            render["detail"] = render_mod.real_render_available() and "可用" or "未找到 LibreOffice"
    except Exception as e:  # noqa: BLE001
        render["detail"] = f"探测失败（{type(e).__name__}）"
    rows.append(render)

    # 4. 本地生图（ComfyUI，默认端口 8188）
    gen = {"key": "local_gen", "label": "本地生图", "ok": False,
           "detail": "未探测到 ComfyUI"}
    try:
        stats = json.loads(_probe_loopback("http://127.0.0.1:8188/system_stats").decode("utf-8"))
        dev = ((stats.get("devices") or [{}])[0]).get("name", "")
        gen = {"key": "local_gen", "label": "本地生图", "ok": True,
               "detail": f"ComfyUI（{dev or '本机'}）"}
    except Exception as e:  # noqa: BLE001
        gen["detail"] = f"未探测到（{type(e).__name__}）"
    rows.append(gen)

    # 5. 视觉模型（图文相似度）：以 Ollama 上的 clip/siglip 类模型或本地标识为准
    vision = {"key": "vision_model", "label": "视觉模型", "ok": False,
              "detail": "未探测到相似度模型"}
    try:
        for m in model.get("models") or []:
            if any(k in str(m).lower() for k in ("clip", "siglip", "vision")):
                vision = {"key": "vision_model", "label": "视觉模型", "ok": True,
                          "detail": f"已就位（{m}）"}
                break
    except Exception:  # noqa: BLE001
        pass
    rows.append(vision)

    # 6. Pexels / Pixabay 密钥：只报是否配置
    import os
    rows.append({"key": "pexels", "label": "Pexels 检索", "ok": bool(os.environ.get("PEXELS_API_KEY")),
                 "detail": "密钥已配置" if os.environ.get("PEXELS_API_KEY") else "密钥未配置（先用备用图源与降级）"})
    rows.append({"key": "pixabay", "label": "Pixabay 检索", "ok": bool(os.environ.get("PIXABAY_API_KEY")),
                 "detail": "密钥已配置" if os.environ.get("PIXABAY_API_KEY") else "密钥未配置（先用备用图源与降级）"})

    return {"schema": "rs.localrun.v1", "probed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "rows": rows,
            "privacy": "全部探测在本机完成；联网检索只发送实体名和指标，不发送你的原文。"}


# ---------------------------------------------------------------- 摘要构造
def research_summary(job_dir: Path) -> dict:
    b = _load_json(job_dir / "evidence" / "research" / "bundle.json")
    if not b:
        return {"schema": "rs.research_summary.v1", "available": False,
                "reason": "本稿没有研究证据（可能未开启联网检索）"}
    facts = b.get("facts") or []
    views = b.get("views") or []
    sources = []
    for f in facts:
        if f.get("origin") != "WEB_RESEARCH":
            continue
        sources.append({
            "title": f.get("subject") or f.get("metric") or "外部数据",
            "publisher": f.get("publisher", ""),
            "date": (f.get("retrieved_at") or "")[:10],
            "url": f.get("url", ""),
            "quote": f.get("quote_span", ""),
            "verified": True,
            "grade": f.get("source_grade", ""),
            "fact_id": f.get("fact_id"),
        })
    discarded = [{"code": d.get("code", ""), "detail": d.get("detail", ""),
                  "url": d.get("url", "")} for d in (b.get("discarded") or [])]
    return {"schema": "rs.research_summary.v1", "available": True,
            "enabled": b.get("enabled", False),
            "counts": {"facts": len(facts), "views": len(views),
                       "sources": len(sources), "discarded": len(discarded)},
            "sources": sources, "discarded": discarded,
            "queries": b.get("queries") or [],
            "report_path": b.get("report_path", "")}


def images_summary(job_dir: Path) -> dict:
    led = _load_json(job_dir / "evidence" / "images" / "ledger.json")
    if not led:
        return {"schema": "rs.images_summary.v1", "available": False,
                "reason": "本稿没有配图账本"}
    tiles = []
    for r in led.get("resolutions") or []:
        asset = r.get("asset") or {}
        status = r.get("status", "")
        if status == "RESOLVED_T1":
            kind, label = "photo", "原稿图"
        elif status == "RESOLVED_T2":
            kind, label = "photo", f"实拍 · {asset.get('provider', '')}"
        elif status == "RESOLVED_T3":
            kind, label = "gen", "AI 生成 · 本机"
        else:
            kind, label = "none", "未配图"
        tiles.append({
            "scene_id": r.get("scene_id"), "kind": kind, "label": label,
            "reason": r.get("reason", ""),
            "path": asset.get("path"), "sha256": asset.get("sha256"),
            "license": asset.get("license"), "creator": asset.get("creator"),
            "source_url": asset.get("source_url"),
            "attribution": asset.get("attribution_text"),
            "model": asset.get("model"), "prompt_digest": asset.get("prompt_digest"),
        })
    counts = {"t1": 0, "t2": 0, "t3": 0, "t4": 0}
    for t in tiles:
        counts[{"photo": "t2", "gen": "t3", "none": "t4"}.get(t["kind"], "t2")] += 1
    for r in led.get("resolutions") or []:
        if r.get("status") == "RESOLVED_T1":
            counts["t1"] += 1
            counts["t2"] -= 1
    return {"schema": "rs.images_summary.v1", "available": True,
            "counts": counts, "tiles": tiles}


def checks_summary(job_dir: Path) -> dict:
    checks = _load_json(job_dir / "evidence" / "pipeline" / "checks.json") or {}
    rows = []
    label = {"render": "逐页渲染", "check_image_ledger": "配图账本",
             "check_transitions": "转场", "check_layout": "版式",
             "verify_bundle": "研究证据"}
    for k, v in checks.items():
        rows.append({"key": k, "label": label.get(k, k),
                     "status": v.get("status", "UNKNOWN"),
                     "reason": v.get("reason", "")})
    return {"schema": "rs.checks_summary.v1", "rows": rows,
            "blocking": [r["key"] for r in rows if r["status"] in ("FAILED", "BLOCKING")]}


def attribution_list(job_dir: Path) -> dict:
    """导出附带的来源与署名清单（数字可追到快照，图片可追到许可）。"""
    research = research_summary(job_dir)
    images = images_summary(job_dir)
    facts, imgs = [], []
    if research.get("available"):
        b = _load_json(job_dir / "evidence" / "research" / "bundle.json") or {}
        for f in b.get("facts") or []:
            if f.get("origin") == "WEB_RESEARCH":
                facts.append({
                    "metric": f.get("metric"), "value": f.get("value"),
                    "unit": f.get("unit"), "period": f.get("period"),
                    "publisher": f.get("publisher"), "url": f.get("url"),
                    "retrieved_at": f.get("retrieved_at"),
                    "snapshot_path": f.get("snapshot_path"),
                    "snapshot_sha256": f.get("snapshot_sha256"),
                    "quote_span": f.get("quote_span")})
    if images.get("available"):
        for t in images["tiles"]:
            if t["kind"] in ("photo", "gen"):
                imgs.append({"scene_id": t["scene_id"], "kind": t["kind"],
                             "license": t["license"], "creator": t["creator"],
                             "source_url": t["source_url"],
                             "attribution": t["attribution"],
                             "model": t["model"], "sha256": t["sha256"]})
    return {"schema": "rs.attribution.v1", "facts": facts, "images": imgs,
            "note": "随导出文件一起交付；每条外部数字可追到快照文件与 URL。"}


# ---------------------------------------------------------------- 路由
def _server_module():
    """惰性导入 server 模块拿 WB 单例与 ROOT（避免与 server 的循环 import）。"""
    from workbench import server as srv
    return srv


def _root_of(handler) -> Path:
    srv = _server_module()
    return Path(getattr(handler, "ROOT", None) or srv.ROOT)


def dispatch_get(handler, path: str, q: dict) -> bool:
    """处理新界面 GET 路由；返回 True 表示已响应。"""
    root = _root_of(handler)

    if path in ("/ui", "/ui/"):
        return handler._send_html(UI_DIR / "index.html")
    if path.startswith("/ui/"):
        rel = path[len("/ui/"):]
        if rel in ("app.js", "styles.css") or (rel.endswith(".js") or rel.endswith(".css")):
            f = UI_DIR / rel
            if f.is_file() and f.suffix in (".js", ".css"):
                return handler._send_file(f)
        return handler._err(404, "WB.NOT_FOUND", "界面资源不存在。")

    m = _match(r"/api/jobs/([A-Za-z0-9-]+)/events", path)
    if m:
        job_dir = root / "jobs" / m.group(1)
        if not job_dir.is_dir():
            return handler._err(404, "WB.JOB_NOT_FOUND", "任务不存在。")
        follow = (q.get("follow") or ["0"])[0] in ("1", "true")
        return _sse_or_json(handler, job_dir, follow)

    m = _match(r"/api/jobs/([A-Za-z0-9-]+)/research", path)
    if m:
        return handler._send(200, research_summary(root / "jobs" / m.group(1)))
    m = _match(r"/api/jobs/([A-Za-z0-9-]+)/pipeline-state", path)
    if m:
        # 新管线任务状态（旧 /api/jobs/<id> 只认 runner 的 job.json）
        run = _load_json(root / "jobs" / m.group(1) / "evidence" / "pipeline" / "run.json")
        if not run:
            return handler._err(404, "WB.JOB_NOT_FOUND", "任务不存在。")
        return handler._send(200, {
            "job_id": run.get("job_id"), "pipeline": True,
            "status": run.get("status"), "language": run.get("language"),
            "deck_pptx": run.get("deck_pptx"),
            "metrics": run.get("metrics"), "checks": run.get("checks"),
            "modules": run.get("modules"),
            "open_conflicts": run.get("open_conflicts"),
            "pages_rendered": run.get("pages_rendered"),
            "errors": {k: v for k, v in run.items() if k.endswith("_error")},
        })
    m = _match(r"/api/jobs/([A-Za-z0-9-]+)/images", path)
    if m:
        return handler._send(200, images_summary(root / "jobs" / m.group(1)))
    m = _match(r"/api/jobs/([A-Za-z0-9-]+)/checks", path)
    if m:
        return handler._send(200, checks_summary(root / "jobs" / m.group(1)))
    m = _match(r"/api/jobs/([A-Za-z0-9-]+)/objects", path)
    if m:
        return handler._send(200, _job_objects_summary(root / "jobs" / m.group(1)))
    m = _match(r"/api/jobs/([A-Za-z0-9-]+)/decisions", path)
    if m:
        return handler._send(200, _decisions_state(root / "jobs" / m.group(1)))
    m = _match(r"/api/jobs/([A-Za-z0-9-]+)/attribution", path)
    if m:
        return handler._send(200, attribution_list(root / "jobs" / m.group(1)))

    m = _match(r"/api/jobs/([A-Za-z0-9-]+)/page-image/(\d+)\.png", path)
    if m:
        job_dir = root / "jobs" / m.group(1)
        n = int(m.group(2))
        # 新管线逐页渲染图优先；旧候选渲染缓存兜底
        for cand in (job_dir / "evidence" / "layout" / "pages" / f"page-{n}.png",
                     job_dir / "evidence" / "layout" / "pages" / f"p{n:02d}.png",
                     job_dir / "candidate" / "render" / f"page-{n}.png"):
            if cand.is_file():
                return handler._send_file(cand)
        return handler._err(404, "WB.PAGE_IMAGE_MISSING", "该页渲染图不存在。")

    if path == "/api/localrun":
        return handler._send(200, probe_localrun(root))
    if path == "/api/overview":
        return handler._send(200, _overview(handler, root))
    if path == "/api/env/libreoffice/status":
        from wengao_ppt import libreoffice_setup as lo
        return handler._send(200, lo.status())
    if path == "/api/env/libreoffice/plan":
        from wengao_ppt import libreoffice_setup as lo
        try:
            return handler._send(200, lo.plan_install())
        except lo.LibreOfficeSetupError as e:
            return handler._send(502, {"error": {"code": e.code, "message": e.detail}})
    if path == "/api/env/libreoffice/install/progress":
        with _LO_INSTALL_LOCK:
            return handler._send(200, {"stage": _LO_INSTALL_STATE["stage"],
                                       "detail": _LO_INSTALL_STATE["detail"]})
    if path == "/api/env/cloud-model/status":
        from wengao_ppt import cloud_model_config as cmc
        return handler._send(200, cmc.status())
    return False


# FIX-W4-001: dispatch_post 自己处理的 POST 路由。
# 为避免与旧工作台路由冲突，新界面的 POST 路由优先使用 /api/ui/ 前缀；
# 且必须在任何请求体读取之前放行不属于自己的路径，避免读空套接字引发下游阻塞。
_POST_PATTERNS = (
    re.compile(r"^/api/ui/jobs/([A-Za-z0-9-]+)/(decisions|page-ops|export|cancel|retry)$"),
    re.compile(r"^/api/jobs/([A-Za-z0-9-]+)/(decisions|page-ops|retry)$"),
    re.compile(r"^/api/env/libreoffice/install$"),
    re.compile(r"^/api/env/cloud-model/config$"),
)


def _owns_post(path: str) -> bool:
    return any(p.match(path) for p in _POST_PATTERNS)


def dispatch_post(handler, path: str) -> bool:
    # FIX-W4-001：非本模块路由必须在任何读取之前放行，否则请求体被读空，
    # 旧路由再按 Content-Length 读取时会永久阻塞。
    if not _owns_post(path):
        return False
    root = _root_of(handler)
    body = handler._body_json() or {}

    m = _match(r"/api/(?:ui/)?jobs/([A-Za-z0-9-]+)/decisions", path)
    if m:
        job_dir = root / "jobs" / m.group(1)
        cid = str(body.get("conflict_id") or "")
        choice = str(body.get("choice") or "")
        try:
            from wengao_ppt.pipeline.decisions import record_decision
            record_decision(job_dir, cid, choice)
        except ValueError as e:
            return handler._err(400, "PIPE.CONTRACT_INVALID", str(e))
        # 局部重排：只重跑 plan + compose + check（配图靠幂等账本复用）
        rerun = None
        try:
            from wengao_ppt.pipeline.generate import GeneratePipeline
            from wengao_ppt.pipeline.options import normalize
            pipe = GeneratePipeline(root, normalize({"mode": "generate"}))
            rerun = pipe.rerun_after_decisions(job_dir)
        except Exception as e:  # noqa: BLE001
            rerun = {"status": "RERUN_FAILED", "reason": f"{type(e).__name__}"}
        return handler._send(200, {"recorded": True, "conflict_id": cid,
                                   "choice": choice, "rerun": rerun,
                                   "decisions": _decisions_state(job_dir)})

    m = _match(r"/api/(?:ui/)?jobs/([A-Za-z0-9-]+)/page-ops", path)
    if m:
        return handler._send(200, _page_ops(root, m.group(1), body))

    m = _match(r"/api/ui/jobs/([A-Za-z0-9-]+)/export", path)
    if m:
        return handler._send(200, _export(root, m.group(1), body))

    m = _match(r"/api/ui/jobs/([A-Za-z0-9-]+)/cancel", path)
    if m:
        jid = m.group(1)
        jdir = root / "jobs" / jid
        if jdir.is_dir():
            (jdir / "cancel.request").write_text("cancel requested\n", encoding="utf-8")
        try:
            srv = _server_module()
            if hasattr(srv, "MAKE"):
                srv.MAKE.cancel(jid)
        except Exception:
            pass
        return handler._send(200, {"status": "CANCELLED", "job_id": jid})

    m = _match(r"/api/(?:ui/)?jobs/([A-Za-z0-9-]+)/retry", path)
    if m:
        jid = m.group(1)
        jdir = root / "jobs" / jid
        req_cf = jdir / "cancel.request"
        if req_cf.is_file():
            req_cf.unlink(missing_ok=True)
        return handler._send(200, {"status": "RETRY_TRIGGERED", "job_id": jid})

    if path == "/api/env/libreoffice/install":
        from wengao_ppt import libreoffice_setup as lo
        with _LO_INSTALL_LOCK:
            if _LO_INSTALL_STATE["stage"] not in ("idle", "succeeded", "failed"):
                return handler._send(409, {"error": {"code": "LO_SETUP.ALREADY_RUNNING",
                                                      "message": "安装已在进行中。"}})
            if lo.status()["installed"]:
                _LO_INSTALL_STATE["stage"] = "succeeded"
                _LO_INSTALL_STATE["detail"] = {"ok": True, "already_installed": True,
                                               **lo.status()}
                return handler._send(200, {"status": "ALREADY_INSTALLED"})
            _LO_INSTALL_STATE["stage"] = "starting"
            _LO_INSTALL_STATE["detail"] = {}
        threading.Thread(target=_lo_install_worker, daemon=True).start()
        return handler._send(202, {"status": "INSTALL_STARTED"})

    if path == "/api/env/cloud-model/config":
        from wengao_ppt import cloud_model_config as cmc
        enabled = bool(body.get("enabled"))
        vision_enabled = body.get("vision_enabled")
        if vision_enabled is not None:
            vision_enabled = bool(vision_enabled)
        endpoint = str(body.get("endpoint") or "")
        text_model = str(body.get("text_model") or "")
        vision_model = str(body.get("vision_model") or "")
        image_gen_model = str(body.get("image_gen_model") or "")
        api_key = body.get("api_key")
        if api_key is not None:
            api_key = str(api_key)
        if enabled and (not endpoint or not text_model):
            return handler._send(400, {"error": {"code": "CLOUD_MODEL.CONFIG_INCOMPLETE",
                                                  "message": "启用云端模型需要填写端点与文本模型名。"}})
        if vision_enabled and not enabled and (not endpoint or not (vision_model or text_model)):
            return handler._send(400, {"error": {"code": "CLOUD_MODEL.CONFIG_INCOMPLETE",
                                                  "message": "启用云端视觉需要填写端点与图片复核模型名。"}})
        if (enabled or vision_enabled) and not (cmc.load()["api_key"] or (api_key or "").strip()):
            return handler._send(400, {"error": {"code": "CLOUD_MODEL.API_KEY_MISSING",
                                                  "message": "启用云端模型需要提供 API key。"}})
        result = cmc.save(enabled=enabled, endpoint=endpoint, api_key=api_key,
                          text_model=text_model, vision_model=vision_model,
                          image_gen_model=image_gen_model,
                          vision_enabled=vision_enabled)
        return handler._send(200, result)
    return False


# ---------------------------------------------------------------- 内部
def _match(pattern: str, path: str):
    import re
    return re.fullmatch(pattern, path)


def _sse_or_json(handler, job_dir: Path, follow: bool):
    if not follow:
        from wengao_ppt.events import load_events
        return handler._send(200, {"events": load_events(job_dir)})
    # SSE 模式：切换协议帧
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Connection", "close")
    handler.end_headers()

    def send(ev):
        if ev is None:
            handler.wfile.write(b": ping\n\n")
        else:
            handler.wfile.write(b"data: " + json.dumps(ev, ensure_ascii=False).encode("utf-8") + b"\n\n")
        handler.wfile.flush()

    from wengao_ppt.events import load_events
    sent = 0
    try:
        while True:
            evs = load_events(job_dir)
            for ev in evs[sent:]:
                send(ev)
            sent = max(sent, len(evs))
            time.sleep(1.0)
            send(None)
    except (BrokenPipeError, ConnectionResetError):
        pass
    return True


def _decisions_state(job_dir: Path) -> dict:
    from wengao_ppt.pipeline.decisions import load_decisions, open_conflicts
    bundle = _load_json(job_dir / "evidence" / "research" / "bundle.json")
    decisions = load_decisions(job_dir)
    conflicts = []
    for v in (bundle or {}).get("views") or []:
        for c in v.get("conflicts") or []:
            cid = c.get("conflict_id")
            conflicts.append({"conflict_id": cid,
                              "user_fact": c.get("user_fact"),
                              "web_fact": c.get("web_fact"),
                              "decision": decisions.get(cid, "open")})
    return {"schema": "rs.decisions.v1", "decisions": decisions,
            "conflicts": conflicts,
            "open": [c["conflict_id"] for c in conflicts if c["decision"] == "open"]}


def _overview(handler, root: Path) -> dict:
    srv = _server_module()
    jobs = srv.WB.jobs_list()
    seen = {j.get("job_id") for j in jobs}
    jd = root / "jobs"
    if jd.is_dir():
        for d in sorted(jd.iterdir()):
            if not d.is_dir() or d.name in seen:
                continue
            run = _load_json(d / "evidence" / "pipeline" / "run.json")
            if not run:
                continue  # 只收录新管线产生过 run.json 的任务
            plan = _load_json(d / "evidence" / "layout" / "plan.json") or {}
            jobs.append({
                "job_id": d.name,
                "run_id": None, "attempt_id": None,
                "mode": (run.get("modules") and "generate") or "generate",
                "theme": None, "theme_name": None, "theme_version": None,
                "theme_config_digest": None,
                "verdict": {"DELIVERED": "READY",
                            "DELIVERED_WITH_DEGRADES": "READY_WITH_EXCEPTIONS",
                            "BLOCKED": "FAILED", "FAILED": "FAILED",
                            "CANCELLED": "CANCELLED"}.get(run.get("status"), run.get("status")),
                "stage": "pipeline",
                "created_at": run.get("started_at"),
                "last_activity": run.get("finished_at"),
                "input_path": str(d / "evidence" / "pipeline" / "material.txt"),
                "input_hash": None,
                "validation_verdict": {"DELIVERED": "READY",
                                       "DELIVERED_WITH_DEGRADES": "READY_WITH_EXCEPTIONS",
                                       "BLOCKED": "FAILED", "FAILED": "FAILED",
                                       "CANCELLED": "CANCELLED"}.get(run.get("status")),
                "delivery": {"allowed": run.get("status", "").startswith("DELIVERED"),
                             "code": None},
                "deck_candidate": run.get("deck_pptx"),
                "pages": len(plan.get("pages") or []),
                "has_research": (d / "evidence" / "research" / "bundle.json").is_file(),
                "has_images": (d / "evidence" / "images" / "ledger.json").is_file(),
            })
    out = []
    for j in jobs:
        jid = j.get("job_id")
        jdir = root / "jobs" / str(jid)
        pages = j.get("pages") or 0
        if not pages:
            plan = _load_json(jdir / "evidence" / "layout" / "plan.json")
            if plan:
                pages = len(plan.get("pages") or [])
        out.append({**j, "pages": pages,
                    "has_research": (jdir / "evidence" / "research" / "bundle.json").is_file(),
                    "has_images": (jdir / "evidence" / "images" / "ledger.json").is_file()})
    return {"schema": "rs.overview.v1", "jobs": out,
            "counts": {"total": len(out),
                       "delivered": sum(1 for j in out if j.get("export_path"))}}


def _page_ops(root: Path, job_id: str, body: dict) -> dict:
    from wengao_ppt.pipeline.edit import EditPipeline
    op = str(body.get("op") or "")
    pipe = EditPipeline(root)
    if op == "recompose":
        return pipe.recompose_page(job_id, int(body.get("page") or 1),
                                   str(body.get("variant") or "a"))
    if op == "insert":
        return pipe.insert_page(job_id, int(body.get("after") or 1),
                                body.get("page_spec") or {})
    if op == "local_edit":
        return pipe.apply_edit(job_id, body.get("edit") or {})
    if op == "restore":
        return pipe.restore_version(job_id, str(body.get("version_id") or ""))
    if op == "transition":
        # 转场开关：改 JobOptions 并登记；真模块接入后由 W1 应用
        opt_path = root / "jobs" / job_id / "evidence" / "pipeline" / "options.json"
        opt = _load_json(opt_path) or {}
        tr = opt.get("transition") or {}
        tr["enabled"] = bool(body.get("enabled", True))
        if body.get("duration_ms"):
            tr["duration_ms"] = int(body["duration_ms"])
        opt["transition"] = tr
        opt_path.parent.mkdir(parents=True, exist_ok=True)
        opt_path.write_text(json.dumps(opt, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"status": "RECORDED", "transition": tr,
                "note": "已记录转场设置；重新出稿时生效"}
    return {"status": "UNKNOWN_OP", "op": op}


def _export(root: Path, job_id: str, body: dict) -> dict:
    """导出：优先复用 runner 已登记的成功导出；附带来源与署名清单。"""
    job_dir = root / "jobs" / job_id
    job = _load_json(job_dir / "job.json") or {}
    fmt = str(body.get("format") or "pptx")
    with_attr = bool(body.get("with_attribution", True))
    export_dir = job.get("export_path")
    result = {"job_id": job_id, "format": fmt, "export_dir": export_dir}
    if export_dir and Path(export_dir).is_dir():
        result["status"] = "OK"
        result["files"] = sorted(p.name for p in Path(export_dir).iterdir()
                                 if p.is_file() and p.suffix in (".pptx", ".pdf"))
    else:
        # 没有已登记导出时，用管线产出的候选（诚实标注未经导出门禁）
        cand = job_dir / "candidate" / "deck.pptx"
        if cand.is_file():
            result["status"] = "CANDIDATE_ONLY"
            result["files"] = [cand.name]
            result["note"] = "这是管线候选，尚未经导出门禁；请先在旧界面完成导出。"
        else:
            result["status"] = "NOT_READY"
            result["note"] = "本稿还没有可导出的文件。"
    if with_attr:
        attr = attribution_list(job_dir)
        p = job_dir / "evidence" / "pipeline" / "attribution.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(attr, ensure_ascii=False, indent=2), encoding="utf-8")
        result["attribution"] = attr
    return result


def _job_objects_summary(job_dir: Path) -> dict:
    """提取任务候选/基线 PPTX 中逐页的对象清单供新工作台对象级编辑面板使用。"""
    cand = job_dir / "candidate" / "deck.pptx"
    base = job_dir / "baseline.pptx"
    target = cand if cand.is_file() else (base if base.is_file() else None)
    if not target or not target.is_file():
        pts = list(job_dir.glob("*.pptx")) + list((job_dir / "candidate").glob("*.pptx"))
        target = pts[0] if pts else None
    if not target:
        return {"job_id": job_dir.name, "pages": [], "editable_objects": 0, "total_objects": 0}

    from pptx import Presentation
    try:
        prs = Presentation(str(target))
    except Exception as e:
        return {"job_id": job_dir.name, "error": str(e), "pages": []}

    pages = []
    editable_n = 0
    total_n = 0
    for idx, slide in enumerate(prs.slides, start=1):
        objs = []
        for shp in slide.shapes:
            total_n += 1
            fields = []
            txt = ""
            if shp.has_text_frame and shp.text_frame.text.strip():
                fields.append("text")
                txt = shp.text_frame.text.strip()
                editable_n += 1
            elif getattr(shp, "has_table", False):
                fields.append("table")
                editable_n += 1
            item = {
                "shape_id": shp.shape_id,
                "name": shp.name or f"Shape {shp.shape_id}",
                "type": str(shp.shape_type or ""),
                "text": txt,
                "fields": fields,
            }
            objs.append(item)
        pages.append({"page": idx, "objects": objs})

    return {
        "job_id": job_dir.name,
        "source": target.name,
        "pages": pages,
        "editable_objects": editable_n,
        "total_objects": total_n,
    }

