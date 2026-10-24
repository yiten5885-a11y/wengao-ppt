"""wengao_ppt.pipeline.modules —— 契约模块注册表：真实优先，桩兜底，桩永不伪造成功。

并行开发约定（00_总览与拼接.md §4.3）：W1/W3 先交 fixture，W2 用 fixture 与桩开发；
W4 用桩搭管线与界面，真模块到位再替换。本模块把"替换"变成一行注册表改动。

三条铁律（对应任务书"不得看到失败后缩小范围抬高完成率"）：
1. 桩**不造假数据**：不生成假图片、不伪造检索结果、不返回 PASS。
2. 桩的检查一律 `NOT_EVALUABLE` + 原因（既有 runner 语义：缺项即阻断 READY）。
3. 每次运行的模块来源写进 run.json（`modules` 字段），真实/回放分开记账。
"""
from __future__ import annotations

import importlib
from typing import Callable, Optional

#: 契约函数名 → 候选导入路径（按优先级）。真模块就位即自动启用。
REGISTRY: dict[str, list[str]] = {
    "run_research": ["wengao_ppt.research.runner:run_research"],
    "verify_bundle": ["wengao_ppt.research.verify:verify_bundle"],
    "plan_deck": ["wengao_ppt.planner.plan:plan_deck",
                  "wengao_ppt.layout.planner:plan_deck"],
    "compose_deck": ["wengao_ppt.layout.compose:compose_deck",
                     "wengao_ppt.builders.compose:compose_deck"],
    "recompose_slide": ["wengao_ppt.layout.compose:recompose_slide"],
    "insert_slide": ["wengao_ppt.layout.compose:insert_slide"],
    "render_pages": ["wengao_ppt.layout.render:render_pages"],
    "resolve_scenes": ["wengao_ppt.scene_images:resolve_scenes",
                       "wengao_ppt.visual_plan:resolve_scenes"],
    "place_image": ["wengao_ppt.scene_images:place_image"],
    "apply_transitions": ["wengao_ppt.transitions:apply_transitions"],
    "check_image_ledger": ["wengao_ppt.scene_images:check_image_ledger"],
    "check_transitions": ["wengao_ppt.transitions:check_transitions"],
    "check_layout": ["wengao_ppt.layout.checks:check_layout"],
}

_cache: dict[str, tuple[Optional[Callable], str]] = {}

#: 各 check 的传参约定（C6 写的是 pptx_or_job_dir，两种实现都存在：
#: W2 的 check_layout 收 PPTX 路径；W1 的账本类检查收 job_dir）。
CHECK_ARGS = {
    "check_layout": "pptx",
    "check_image_ledger": "job_dir",
    "check_transitions": "job_dir",
}

#: 检查看门狗超时（秒）。并行开发期间被调模块可能死循环（实测 W2 measure.wrap_lines
#: 曾无限循环）；超时记 FAILED 并保留现场信息，绝不无限等待、绝不假装通过。
CHECK_TIMEOUT_S = {
    "check_layout": 120,
    "check_image_ledger": 60,
    "check_transitions": 60,
}
DEFAULT_CHECK_TIMEOUT_S = 90


def _call_with_timeout(fn, arg, timeout_s: float):
    """在看门狗线程里调用 fn(arg)；超时抛 TimeoutError。"""
    import threading
    box = {}

    def worker():
        try:
            box["result"] = fn(arg)
        except BaseException as e:  # noqa: BLE001 —— 原样传给调用方处理
            box["error"] = e

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        raise TimeoutError(f"检查执行超过 {timeout_s:.0f}s 未返回")
    if "error" in box:
        raise box["error"]
    return box.get("result")


def run_check(name: str, pptx_path, job_dir) -> dict:
    """按约定调用检查函数；异常/超时一律转成 FAILED 报告（绝不假装通过）。"""
    fn, src = get(name)
    arg = pptx_path if CHECK_ARGS.get(name) == "pptx" else job_dir
    try:
        rep = _call_with_timeout(fn, arg,
                                 CHECK_TIMEOUT_S.get(name, DEFAULT_CHECK_TIMEOUT_S))
        if not isinstance(rep, dict):
            return {"schema": "rs.check_report.v1", "check": name,
                    "status": "FAILED", "reason": f"返回值不是报告对象（{src}）"}
        rep.setdefault("check", name)
        rep.setdefault("status", "UNKNOWN")
        return rep
    except TimeoutError as e:
        return {"schema": "rs.check_report.v1", "check": name,
                "status": "FAILED", "reason": f"检查超时：{e}（模块 {src} 可能在并行开发中）",
                "module": src}
    except Exception as e:  # noqa: BLE001
        return {"schema": "rs.check_report.v1", "check": name,
                "status": "FAILED", "reason": f"{type(e).__name__}: {e}",
                "module": src}


def _import(path: str) -> Optional[Callable]:
    mod_name, _, fn_name = path.partition(":")
    try:
        mod = importlib.import_module(mod_name)
        fn = getattr(mod, fn_name, None)
        return fn if callable(fn) else None
    except Exception:  # noqa: BLE001 —— 模块未就位是正常并行状态，不是错误
        return None


def _adapt_compose_deck(fn: Callable) -> Callable:
    """并行期适配：C6 冻结契约定 `resolutions` 为 list[SceneResolution]；
    W2 当前实现按 {scene_id: SceneResolution} dict 消费。这里按被调函数的
    签名标注自动转换，两边都对。契约对齐后（变更请求单 CR-01）本适配自动失效。
    """
    import inspect
    try:
        ann = inspect.signature(fn).parameters["resolutions"].annotation
        wants_dict = "dict" in str(ann)
    except (KeyError, TypeError, ValueError):
        wants_dict = False
    if not wants_dict:
        return fn

    def _wrapped(plan, resolutions, theme, out_path, place_image=None, emit=None):
        as_dict = resolutions
        if isinstance(resolutions, list):
            as_dict = {r.get("scene_id"): r for r in resolutions if isinstance(r, dict)}
        return fn(plan, as_dict, theme, out_path, place_image, emit)
    _wrapped.__name__ = "compose_deck"
    return _wrapped


def get(name: str) -> tuple[Callable, str]:
    """返回 (实现, 来源)。来源形如 'real:wengao_ppt.research.runner' 或 'stub'。"""
    if name in _cache:
        return _cache[name]
    for path in REGISTRY.get(name, []):
        fn = _import(path)
        if fn is not None:
            if name == "compose_deck":
                fn = _adapt_compose_deck(fn)
            _cache[name] = (fn, f"real:{path}")
            return _cache[name]
    stub = _STUBS.get(name)
    if stub is None:
        raise KeyError(f"契约函数 {name} 没有注册实现，也没有桩")
    _cache[name] = (stub, "stub")
    return _cache[name]


def provenance() -> dict:
    """本次运行的模块来源表（写进 run.json，真实/回放分开记账）。"""
    out = {}
    for name in REGISTRY:
        try:
            _, src = get(name)
            if src == "stub" and name == "render_pages":
                # 桩壳内部调用的是真渲染器 render.render_real，单独标注
                try:
                    from wengao_ppt import render as _r
                    src = ("real:render.render_real(via-stub-wrapper)"
                           if _r.real_render_available() else "stub")
                except Exception:  # noqa: BLE001
                    pass
            out[name] = src
        except KeyError:
            out[name] = "missing"
    return out


# ---------------------------------------------------------------- 桩实现
def _stub_run_research(material_text, options, job_dir, emit):
    """研究关闭/未就位时的合法空 bundle（C6：关闭时也返回合法 bundle）。"""
    from wengao_ppt.research.types import ResearchBundle  # W3 已交付的类型
    import datetime
    bundle = ResearchBundle(
        bundle_id="rb-stub", created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        material_language=str(options.get("language") or "zh"),
        enabled=False, queries=[], facts=[], views=[], discarded=[], omissions=[],
        report_path="evidence/research/report.md")
    try:
        from wengao_ppt.research.report import write_reports_and_queries
        write_reports_and_queries(bundle, job_dir)
    except Exception:  # noqa: BLE001
        pass
    return bundle


def _stub_verify_bundle(job_dir):
    """没有 W3 真校验器时：只报未评估，不假装通过。"""
    return {"status": "NOT_EVALUABLE",
            "reason": "verify_bundle 真模块未接入（W3 M3 交付后自动启用）",
            "checked": 0, "rejected": []}


def _stub_plan_deck(material, bundle, decisions, options, job_dir, emit):
    """最小规划器：按段落切页，不含图片槽（诚实——桩不发明场景）。"""
    import re
    text = (material or "").strip()
    title = (text.splitlines() or ["未命名演示"])[0][:40] or "未命名演示"
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    pages = [{
        "page_id": "p01", "index": 1, "role": "cover", "page_type": "cover",
        "variant": "a", "density": "standard", "title": title,
        "blocks": [{"kind": "text", "text": title}], "image_slots": [],
        "footnote": None, "research_view_ref": None,
    }]
    for i, p in enumerate(paras[:11], start=2):
        pages.append({
            "page_id": f"p{i:02d}", "index": i, "role": "content",
            "page_type": "points", "variant": "a", "density": "standard",
            "title": p.splitlines()[0][:30], "blocks": [{"kind": "bullets", "items": p.splitlines()[:5]}],
            "image_slots": [], "footnote": None, "research_view_ref": None,
        })
    return {"schema": "rs.deck_plan.v1", "deck_id": f"deck-stub-{abs(hash(text)) % 10**8:08d}",
            "language": str(options.get("language") or "zh"), "mode": options.get("mode") or "generate",
            "pages": pages}


def _stub_resolve_scenes(requests, options, job_dir, emit):
    """桩：每个场景 FALLBACK_T4 + 原因。绝不返回假图（宁缺勿错）。"""
    out = []
    for req in requests or []:
        out.append({
            "schema": "rs.scene_resolution.v1",
            "scene_id": req.get("scene_id"),
            "status": "FALLBACK_T4",
            "asset": None,
            "attempts": [{"tier": "T2", "provider": "pexels",
                          "outcome": "MODULE_NOT_WIRED",
                          "detail": "resolve_scenes 真模块未接入（W1 M1/M2 交付后自动启用）",
                          "elapsed_s": 0.0}],
            "reason": "配图引擎未接入，按无图版式降级出稿",
        })
    return out


def _stub_compose_deck(plan, resolutions, theme, out_path, place_image, emit):
    """桩：用 python-pptx 建一份只有文字的最小 PPTX（无图片），标注桩来源。"""
    from pptx import Presentation
    from pptx.util import Inches
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    for page in plan.get("pages", []):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        box = slide.shapes.add_textbox(Inches(0.8), Inches(0.9), Inches(11.7), Inches(1.2))
        box.text_frame.text = page.get("title") or ""
        body = slide.shapes.add_textbox(Inches(0.8), Inches(2.4), Inches(11.7), Inches(4.2))
        tf = body.text_frame
        for blk in page.get("blocks", []):
            if blk.get("kind") == "bullets":
                for it in blk.get("items", []):
                    tf.add_paragraph().text = f"• {it}"
            elif blk.get("kind") == "text":
                tf.add_paragraph().text = blk.get("text", "")
    prs.save(str(out_path))
    return {"schema": "rs.compose_report.v1", "out_path": str(out_path),
            "pages": len(plan.get("pages", [])), "images_placed": 0,
            "note": "桩构建器：仅文字占位，等 W2 版式引擎接入"}


def _stub_recompose_slide(pptx_in, page_index, variant, theme, out_path):
    return {"schema": "rs.compose_report.v1", "out_path": str(out_path),
            "recomposed_page": page_index, "_stub": True,
            "note": "recompose_slide 真模块未接入（W2 M3）"}


def _stub_insert_slide(pptx_in, after_index, page_spec, theme, out_path):
    return {"schema": "rs.compose_report.v1", "out_path": str(out_path),
            "inserted_after": after_index, "_stub": True,
            "note": "insert_slide 真模块未接入（W2 M3）"}


def _stub_render_pages(pptx, outdir, cancel_check=None):
    """渲染优先用真 render.render_real（已存在）；其不可用才报未评估。"""
    from pathlib import Path
    from wengao_ppt import render as render_mod
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    if not render_mod.real_render_available():
        return {"pdf": None, "pngs": [], "fonts_report": None,
                "status": "NOT_EVALUABLE",
                "reason": "LibreOffice 不可用（render.real_render_available()=False）"}
    res = render_mod.render_real(Path(pptx), out, cancel_check=cancel_check)
    pngs = [str(p) for p in sorted(out.glob("*.png"))]
    return {"pdf": res.get("pdf") if isinstance(res, dict) else None,
            "pngs": pngs, "fonts_report": res.get("fonts") if isinstance(res, dict) else None,
            "status": "OK" if pngs else "FAILED", "raw": res if isinstance(res, dict) else None}


def _stub_place_image(slide, rect, asset, fit="cover", alt_text=""):
    raise NotImplementedError("place_image 真模块未接入（W1）；桩不插入任何图片")


def _stub_apply_transitions(pptx_in, pptx_out, policy, roles):
    """桩：不改文件，如实报告未执行。幂等（重复调用无副作用）。"""
    return {"schema": "rs.transition_report.v1", "applied": 0,
            "status": "NOT_EVALUABLE",
            "reason": "apply_transitions 真模块未接入（W1 M1）",
            "policy": policy}


def _stub_check(name: str, reason: str):
    def _check(pptx_or_job_dir):
        return {"schema": "rs.check_report.v1", "check": name,
                "status": "NOT_EVALUABLE", "reason": reason,
                "blocking": True, "findings": []}
    return _check


_STUBS: dict[str, Callable] = {
    "run_research": _stub_run_research,
    "verify_bundle": _stub_verify_bundle,
    "plan_deck": _stub_plan_deck,
    "compose_deck": _stub_compose_deck,
    "recompose_slide": _stub_recompose_slide,
    "insert_slide": _stub_insert_slide,
    "render_pages": _stub_render_pages,
    "resolve_scenes": _stub_resolve_scenes,
    "place_image": _stub_place_image,
    "apply_transitions": _stub_apply_transitions,
    "check_image_ledger": _stub_check("check_image_ledger", "W1 未接入"),
    "check_transitions": _stub_check("check_transitions", "W1 未接入"),
    "check_layout": _stub_check("check_layout", "W2 未接入"),
}
