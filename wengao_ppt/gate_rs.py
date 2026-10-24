"""wengao_ppt.gate_rs —— RS 条件必检项注册与接线（W4 拥有，Step 0.5 / 门禁接线）。

任务书 RS-8 与 W4 指令 §8：把各窗口交付的 check_layout、check_image_ledger、
check_transitions、verify_bundle 注册进门禁；**不放宽既有门禁**——新检查只在
 JobOptions 打开对应能力时到期，未交付时记 NOT_EVALUABLE（阻断），绝不冒充通过。

与既有 runner 的关系：
- `extend_expected(exp, options)` 只做**加法**：把 RS 条件项并入 exp["required"]
  并登记 derived_from；不得删除或放宽任何既有项。
- `run_rs_checks(pptx, job_dir, options)` 在 runner 的 checks_map 构造后执行
  到期项，返回 {检查名: 状态}；模块未就位 → NOT_EVALUABLE。
"""
from __future__ import annotations

from typing import Optional

#: RS 检查注册表：门禁名 → (模块函数, 到期条件说明, 面向用户的中文名)
RS_CHECK_REGISTRY = {
    "image.ledger": {
        "fn": "check_image_ledger",
        "when": "images.enabled",
        "label": "配图账本",
        "owner": "W1",
    },
    "transition.uniform": {
        "fn": "check_transitions",
        "when": "transition.enabled",
        "label": "转场一致性",
        "owner": "W1",
    },
    "research.bundle_verified": {
        "fn": "verify_bundle",
        "when": "research.enabled",
        "label": "研究证据核对",
        "owner": "W3",
    },
    "layout.compose_check": {
        "fn": "check_layout",
        "when": "always",
        "label": "版式复核",
        "owner": "W2",
    },
}

#: 状态归一化：模块报告状态 → runner checks_map 语义
_STATUS_MAP = {
    "OK": "EXECUTED", "PASS": "EXECUTED",
    "NOT_EVALUABLE": "NOT_EVALUABLE",
    "FAILED": "FAILED", "BLOCKING": "FAILED",
}


def rs_required_checks(options: Optional[dict]) -> list:
    """按 JobOptions 推导到期的 RS 检查名（不含既有项）。"""
    if not options:
        return []
    out = []
    for name, spec in RS_CHECK_REGISTRY.items():
        cond = spec["when"]
        if cond == "always":
            out.append(name)
            continue
        group, _, key = cond.partition(".")
        if (options.get(group) or {}).get(key, False):
            out.append(name)
    return sorted(out)


def extend_expected(exp: dict, options: Optional[dict]) -> dict:
    """把 RS 条件项并入 expected_required_checks 的返回值（只加不减）。"""
    if not isinstance(exp, dict):
        return exp
    for name in rs_required_checks(options):
        if name not in exp["required"]:
            exp["required"].append(name)
        exp.setdefault("derived_from", {}).setdefault(name, []).append(
            f"rs_options:{RS_CHECK_REGISTRY[name]['when']}")
    exp["required"] = sorted(set(exp["required"]))
    exp["rs_checks"] = {name: RS_CHECK_REGISTRY[name] for name in rs_required_checks(options)}
    return exp


def _status_of(report: dict) -> str:
    if not isinstance(report, dict):
        return "NOT_EVALUABLE"
    return _STATUS_MAP.get(report.get("status", ""), "NOT_EVALUABLE")


def run_rs_checks(pptx_path, job_dir, options: Optional[dict]) -> dict:
    """执行到期的 RS 检查，返回 {门禁名: checks_map 状态}。

    任何异常/超时/未就位都记 NOT_EVALUABLE——缺证据不能当通过。
    """
    from wengao_ppt.pipeline import modules as mod
    out = {}
    for name in rs_required_checks(options):
        spec = RS_CHECK_REGISTRY[name]
        try:
            rep = mod.run_check(spec["fn"], pptx_path, job_dir)
        except Exception:  # noqa: BLE001
            rep = {"status": "NOT_EVALUABLE"}
        out[name] = _status_of(rep)
    return out


def registry_view() -> dict:
    """给 /api/meta 的注册视图：每项是否已接真模块。"""
    from wengao_ppt.pipeline import modules as mod
    view = {}
    for name, spec in RS_CHECK_REGISTRY.items():
        try:
            _, src = mod.get(spec["fn"])
        except KeyError:
            src = "missing"
        view[name] = {"label": spec["label"], "owner": spec["owner"],
                      "when": spec["when"], "module": src,
                      "wired": src.startswith("real:")}
    return view
