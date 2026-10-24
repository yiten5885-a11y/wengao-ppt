"""wengao_ppt.pipeline.generate —— 生成模式主管线（W4 拥有）。

顺序固定（任务书 §4 / W4 指令 §5）：
  read → research（W3）→ plan（W2）→ images（W1）→ compose（W2）
       → transition（W1）→ render + 全部 check_*（W2 渲染 + 各窗口检查）

每一步发 C2 事件；失败语义严格按 C8：
  - 缺图/缺数据      → 降级并记录，照常出稿
  - 错图/无许可/重复 → 阻断该图，场景降级 T4
  - 编造数字/溢出    → 阻断（由各 check_* 表达）
  - 任何阶段可取消；冲突裁决后只重跑 plan + compose + check（配图靠 W1 幂等账本复用）
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from wengao_ppt import events as ev_mod
from wengao_ppt.contracts_rs import validate
from wengao_ppt.pipeline import modules as mod
from wengao_ppt.pipeline.decisions import load_decisions, open_conflicts, record_decision
from wengao_ppt.pipeline.metrics import summarize


class GeneratePipeline:
    def __init__(self, project_root, options: Optional[dict] = None,
                 sink: Optional[Callable[[dict], None]] = None):
        self.root = Path(project_root)
        self.options = options or {}
        self.sink = sink

    # ------------------------------------------------------------ 基础设施
    def _emitter(self, job_id: str, job_dir: Path) -> ev_mod.EventEmitter:
        em = ev_mod.EventEmitter(job_id, job_dir=job_dir)
        if self.sink is not None:
            # 接受可调用对象或列表（列表用 append 收集，便于测试与 SSE 桥接）
            em.subscribe(self.sink if callable(self.sink) else self.sink.append)
        return em

    def _job_dir(self, job_id: str) -> Path:
        d = self.root / "jobs" / job_id
        (d / "evidence").mkdir(parents=True, exist_ok=True)
        return d

    def _save(self, job_dir: Path, rel: str, obj) -> None:
        p = job_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # ------------------------------------------------------------ 主流程
    def run(self, material_text: str, job_id: Optional[str] = None,
            cancel_check: Optional[Callable[[], bool]] = None) -> dict:
        job_id = job_id or f"job-{uuid.uuid4().hex[:12]}"
        job_dir = self._job_dir(job_id)
        # JobOptions 落盘：runner 门禁据此并入 RS 条件必检项（只加不减）
        self._save(job_dir, "evidence/pipeline/options.json", self.options)
        em = self._emitter(job_id, job_dir)
        t0 = time.monotonic()
        result = {"job_id": job_id, "job_dir": str(job_dir),
                  "status": "RUNNING", "modules": mod.provenance(), "stages": []}

        def cancelled() -> bool:
            return bool(cancel_check and cancel_check())

        # ---- read ----
        em.running("read", "正在读取材料")
        material_text = material_text or ""
        (job_dir / "evidence" / "pipeline").mkdir(parents=True, exist_ok=True)
        (job_dir / "evidence" / "pipeline" / "material.txt").write_text(
            material_text, encoding="utf-8")
        try:
            from wengao_ppt.language_support import detect_language_hint
            hint = detect_language_hint(material_text) or {}
            language = hint.get("language") or "zh"
        except Exception:  # noqa: BLE001
            hint, language = {}, "zh"
        if self.options.get("language") in (None, "auto"):
            self.options["language"] = language
        em.done("read", f"已读取材料，识别为{_lang_name(language)}",
                counts={"chars": len(material_text)},
                refs=["evidence/pipeline/material.txt"])
        result["language"] = language

        if cancelled():
            return self._finish(result, em, "CANCELLED", "任务已取消")

        # ---- research（W3）----
        bundle = None
        if self.options.get("research", {}).get("enabled", True):
            em.running("research", "正在联网检索行业数据")
            run_research, _ = mod.get("run_research")
            try:
                bundle = run_research(material_text, self.options, str(job_dir),
                                      em.as_callable())
                self._save(job_dir, "evidence/research/bundle.json", _to_dict(bundle))
                em.done("trend", _research_summary(bundle),
                        counts=_research_counts(bundle),
                        refs=["evidence/research/bundle.json"])
            except Exception as e:  # noqa: BLE001 —— 研究失败按 C8 降级，不阻断
                em.warn("research", "联网检索未完成，对应内容将省略")
                result["research_error"] = f"{type(e).__name__}"
        else:
            em.skipped("research", "已关闭联网检索，对比与趋势页将省略")

        # ---- plan（W2）----
        em.running("layout", "正在规划页面")
        plan_deck, _ = mod.get("plan_deck")
        decisions = load_decisions(job_dir)
        try:
            plan = plan_deck(material_text, _to_dict(bundle), decisions,
                             self.options, str(job_dir), em.as_callable())
            validate(plan)
            self._save(job_dir, "evidence/layout/plan.json", plan)
        except Exception as e:  # noqa: BLE001
            em.failed("layout", "页面规划失败，无法出稿")
            result["plan_error"] = f"{type(e).__name__}: {e}"
            return self._finish(result, em, "FAILED", "页面规划失败")
        n_pages = len(plan.get("pages") or [])
        em.done("layout", f"已规划 {n_pages} 页", counts={"pages": n_pages},
                refs=["evidence/layout/plan.json"])

        # ---- images（W1）----
        requests = []
        for page in plan.get("pages") or []:
            for slot in page.get("image_slots") or []:
                scene = slot.get("scene")
                if scene:
                    scene = dict(scene)
                    scene.setdefault("page_id", page.get("page_id"))
                    scene.setdefault("slot_id", slot.get("slot_id"))
                    requests.append(scene)
        resolutions = []
        if self.options.get("images", {}).get("enabled", True) and requests:
            em.running("images", f"正在为 {len(requests)} 个场景配图")
            resolve_scenes, _ = mod.get("resolve_scenes")
            try:
                resolutions = resolve_scenes(requests, self.options, str(job_dir),
                                             em.as_callable())
                validate_list = [validate(r) for r in resolutions]
            except Exception as e:  # noqa: BLE001
                em.warn("images", "配图阶段异常，相关场景按未配图降级")
                result["images_error"] = f"{type(e).__name__}"
                resolutions = []
        else:
            em.skipped("images", "本稿没有需要配图的场景" if not requests
                       else "配图已关闭")
        # 账本总是落盘（空列表也写）：界面"配图"标签与门禁从磁盘读，不信内存
        self._save(job_dir, "evidence/images/ledger.json",
                   {"schema": "rs.image_ledger.v1", "resolutions": resolutions})
        from wengao_ppt.pipeline.metrics import count_tiers
        tiers = count_tiers(resolutions)
        em.done("images", _images_summary(tiers), counts=tiers,
                refs=["evidence/images/ledger.json"] if resolutions else [])

        if cancelled():
            return self._finish(result, em, "CANCELLED", "任务已取消")

        # ---- compose（W2）----
        deck_path = job_dir / "candidate" / "deck.pptx"
        deck_path.parent.mkdir(parents=True, exist_ok=True)
        em.running("layout", "正在排版成稿")
        compose_deck, _ = mod.get("compose_deck")
        place_image, _ = mod.get("place_image")
        try:
            compose_report = compose_deck(plan, resolutions,
                                          self.options.get("theme"), deck_path,
                                          place_image, em.as_callable())
            self._save(job_dir, "evidence/layout/layout_report.json", compose_report)
            em.done("layout", "排版完成", counts={"pages": n_pages},
                    refs=["evidence/layout/layout_report.json"])
        except Exception as e:  # noqa: BLE001
            em.failed("layout", "排版失败，无法出稿")
            result["compose_error"] = f"{type(e).__name__}: {e}"
            return self._finish(result, em, "FAILED", "排版失败")
        result["deck_pptx"] = str(deck_path)

        # ---- transition（W1）----
        if self.options.get("transition", {}).get("enabled", True):
            em.running("transition", "正在写入统一转场")
            apply_transitions, _ = mod.get("apply_transitions")
            roles = {p.get("index"): p.get("role") for p in plan.get("pages") or []}
            try:
                tr = apply_transitions(str(deck_path), str(deck_path),
                                       self.options.get("transition") or {}, roles)
                self._save(job_dir, "evidence/transition/transition_report.json", tr)
                em.done("transition", _transition_summary(tr))
            except Exception as e:  # noqa: BLE001
                em.warn("transition", "转场写入未完成，不影响出稿")
                result["transition_error"] = f"{type(e).__name__}"
        else:
            em.skipped("transition", "转场已关闭")

        # ---- render + checks ----
        em.running("check", "正在逐页渲染并核对")
        pages_dir = job_dir / "evidence" / "layout" / "pages"
        render_pages, _ = mod.get("render_pages")
        checks = {}
        try:
            rendered = render_pages(deck_path, pages_dir, cancel_check=cancel_check)
            self._save(job_dir, "evidence/layout/render.json", _jsonable(rendered))
            checks["render"] = {"status": rendered.get("status", "UNKNOWN")}
        except Exception as e:  # noqa: BLE001
            rendered = {}
            checks["render"] = {"status": "FAILED", "reason": f"{type(e).__name__}"}
        for name in ("check_image_ledger", "check_transitions", "check_layout"):
            rep = mod.run_check(name, deck_path, job_dir)
            checks[name] = {"status": rep.get("status", "UNKNOWN"),
                            "reason": rep.get("reason", "")}
        # W3 的研究证据复核（真模块就位即生效）
        try:
            verify_bundle, _ = mod.get("verify_bundle")
            vr = verify_bundle(job_dir)
            checks["verify_bundle"] = {"status": vr.get("status", "UNKNOWN"),
                                       "reason": vr.get("reason", "")}
        except Exception as e:  # noqa: BLE001
            checks["verify_bundle"] = {"status": "FAILED", "reason": f"{type(e).__name__}"}
        self._save(job_dir, "evidence/pipeline/checks.json", checks)

        blocking = [k for k, v in checks.items()
                    if v.get("status") in ("FAILED", "BLOCKING")]
        if blocking:
            em.failed("check", "核对未通过：" + "、".join(blocking))
            result["status"] = "BLOCKED"
        else:
            degraded = tiers.get("t4", 0) > 0 or any(
                v.get("status") == "NOT_EVALUABLE" for v in checks.values())
            em.done("check", _check_summary(checks, degraded),
                    counts={"degraded_pages": tiers.get("t4", 0)})
            result["status"] = "DELIVERED_WITH_DEGRADES" if degraded else "DELIVERED"

        result["checks"] = checks
        result["pages_rendered"] = len(rendered.get("pngs") or []) if isinstance(rendered, dict) else 0
        result["open_conflicts"] = open_conflicts(_to_dict(bundle), decisions)
        return self._finish(result, em, result["status"], "")

    # ------------------------------------------------------------ 局部重跑
    def rerun_after_decisions(self, job_dir: Path, cancel_check=None) -> dict:
        """冲突裁决后只重跑 plan + compose + check；配图复用 W1 幂等账本。"""
        job_dir = Path(job_dir)
        job_id = job_dir.name
        em = self._emitter(job_id, job_dir)
        material = (job_dir / "evidence" / "pipeline" / "material.txt").read_text(
            encoding="utf-8") if (job_dir / "evidence" / "pipeline" / "material.txt").is_file() else ""
        bundle = _load_json(job_dir / "evidence" / "research" / "bundle.json")
        decisions = load_decisions(job_dir)
        em.running("layout", "已按你的选择重排相关页面")
        plan_deck, _ = mod.get("plan_deck")
        plan = plan_deck(material, bundle, decisions, self.options, str(job_dir),
                         em.as_callable())
        self._save(job_dir, "evidence/layout/plan.json", plan)
        resolutions = (_load_json(job_dir / "evidence" / "images" / "ledger.json") or {}).get("resolutions", [])
        deck_path = job_dir / "candidate" / "deck.pptx"
        compose_deck, _ = mod.get("compose_deck")
        place_image, _ = mod.get("place_image")
        compose_deck(plan, resolutions, self.options.get("theme"), deck_path,
                     place_image, em.as_callable())
        em.done("layout", "重排完成")
        return {"job_id": job_id, "status": "RERUN_DONE", "deck_pptx": str(deck_path),
                "open_conflicts": open_conflicts(bundle, decisions)}

    # ------------------------------------------------------------ 收尾
    def _finish(self, result: dict, em, status: str, note: str) -> dict:
        from wengao_ppt.pipeline.metrics import deck_delivered, scene_coverage
        resolutions = (_load_json(Path(result["job_dir"]) / "evidence" / "images" / "ledger.json") or {}).get("resolutions", [])
        result["status"] = status
        result["metrics"] = summarize(result.get("deck_pptx"), resolutions,
                                      result.get("checks"))
        result["metrics"]["scene_coverage"] = scene_coverage(resolutions)
        result["metrics"]["deck_delivered"] = deck_delivered(result.get("deck_pptx"))
        result["elapsed_s"] = None
        if note:
            result["note"] = note
        try:
            self._save(Path(result["job_dir"]), "evidence/pipeline/run.json", result)
        except OSError:
            pass
        return result


# ---------------------------------------------------------------- 帮助函数
def _lang_name(code: str) -> str:
    return {"zh": "中文", "en": "英文", "ja": "日文", "ar": "阿拉伯文",
            "hi": "印地文", "ru": "俄文"}.get(code, code or "未知语言")


def _to_dict(bundle):
    if bundle is None:
        return None
    if hasattr(bundle, "to_dict"):
        return bundle.to_dict()
    return bundle


def _load_json(p: Path):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _research_summary(bundle) -> str:
    d = _to_dict(bundle) or {}
    facts = len(d.get("facts") or [])
    views = len(d.get("views") or [])
    discarded = len(d.get("discarded") or [])
    if facts == 0 and views == 0:
        return "未检索到可靠数据，对比与趋势页将省略"
    return f"已核对 {facts} 项事实，生成 {views} 个对比视图（丢弃 {discarded} 条）"


def _research_counts(bundle) -> dict:
    d = _to_dict(bundle) or {}
    return {"facts": len(d.get("facts") or []), "views": len(d.get("views") or []),
            "discarded": len(d.get("discarded") or [])}


def _images_summary(tiers: dict) -> str:
    parts = []
    if tiers.get("t1"):
        parts.append(f"{tiers['t1']} 张原稿图")
    if tiers.get("t2"):
        parts.append(f"{tiers['t2']} 张检索图")
    if tiers.get("t3"):
        parts.append(f"{tiers['t3']} 张本机生成")
    if tiers.get("t4"):
        parts.append(f"{tiers['t4']} 页未配图")
    return "、".join(parts) if parts else "本稿无需配图"


def _transition_summary(tr: dict) -> str:
    if tr.get("status") == "NOT_EVALUABLE":
        return "转场模块未接入，本稿暂无转场"
    n = tr.get("applied") or 0
    return f"已为 {n} 页写入统一转场" if n else "转场已处理"


def _check_summary(checks: dict, degraded: bool) -> str:
    ok = sum(1 for v in checks.values() if v.get("status") in ("OK", "PASS"))
    total = len(checks)
    if degraded:
        return f"逐条通过 {ok}/{total}，存在降级项，详见报告"
    return f"逐条通过 {ok}/{total}"


def _jsonable(obj):
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return {"note": "结果不可 JSON 序列化", "type": type(obj).__name__}
