"""D09：授权局部修改与版本保护。

严格对应主计划 v3.1 §15 任务表：

    | D09 局部修改和版本 | D08 | 修改范围保护、外部手改接回、共享资源检查、恢复 |

到期要求（来自 `CLAUDE_MASTER_CONTINUE_R2_R7…md` §7.2，原文约束）：
1. 只在 D08 前置通过后实现授权局部修改；
2. 明确**页面/对象/字段及用户新值**（不接受"整篇重写"式请求）；
3. 保存前版、独立候选、验证**非目标不变**、共享资源保护、迟到取消；
4. 外部手改作为**新输入**；旧计划不能覆盖新输入；
5. 恢复历史版本仍检查完整性及当前适用要求，**不能盲用旧 READY**。

设计约束：
- 只改用户显式指定的那一个字段，其余对象一律不得触碰；
- 每次局部修改产生**独立候选**，不复用既有 valid/；
- 拒绝时使用**契约已登记**的错误码，不新造码：
  `SCOPE.UNREQUESTED_CHANGE` / `VERSION.USER_EDIT_OVERWRITTEN` / `VERSION.MERGE_CONFLICT`
  以及门禁码 `GATE.*`；
- 完整性以**整文件 SHA256** 判定，不靠"看起来一样"。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def load_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def save_json_atomic(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


class EditScopeError(Exception):
    """授权范围违规。使用契约已登记的错误码。"""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _resolve_shape(prs, page_no: int, shape_ref: str):
    """按页码 + 形状名/形状 id 精确定位对象；定位不到即拒绝，不做模糊猜测。"""
    if page_no < 1 or page_no > len(prs.slides):
        raise EditScopeError(
            "SCOPE.UNREQUESTED_CHANGE",
            f"页码 {page_no} 超出范围（共 {len(prs.slides)} 页）。")
    slide = prs.slides[page_no - 1]
    for shp in slide.shapes:
        if shp.name == shape_ref:
            return shp
        try:
            if str(shp.shape_id) == str(shape_ref):
                return shp
        except Exception:  # noqa: BLE001
            continue
    raise EditScopeError(
        "SCOPE.UNREQUESTED_CHANGE",
        f"第 {page_no} 页找不到对象 {shape_ref!r}；不做模糊匹配。")


def _shared_resources(prs):
    """收集跨页共享的资源（同一 rId 指向的同一 image/chart 部件）。

    共享对象被局部修改时必须拒绝：改一处会连带改变其他引用页，
    违反"非目标不变"。本函数返回 {rId: [page_no, ...]}。
    """
    from pptx.opc.constants import RELATIONSHIP_TYPE as RT
    shared = {}
    for i, slide in enumerate(prs.slides, start=1):
        for rid, rel in slide.part.rels.items():
            if rel.reltype in (RT.IMAGE, RT.CHART):
                shared.setdefault(rel.target_partname, []).append(i)
    return {str(k): v for k, v in shared.items() if len(v) > 1}


def _resolve_exported_baseline(job_dir: Path):
    """兼容入口：只返回路径（旧调用点）。新代码请用 `resolve_baseline()` 取原因。"""
    st = resolve_baseline(job_dir)
    return st["path"] if st["ok"] else None


def resolve_baseline(job_dir: Path) -> dict:
    """解析"当前可编辑基线"，并给出**可执行的原因**而不是一句笼统拒绝。

    旧实现把所有情形都折成 `SCOPE.UNREQUESTED_CHANGE / 没有可用的基线版本`，
    使用者看到的结论是"我改的东西不允许"，而真实原因是"还没导出"。
    这里把状态分开，每种都带 `code / detail / next_actions`，
    让 /objects 与 /edit 能返回**同一个判定**和下一步该做什么。

    返回：
        {"ok", "path", "source", "code", "detail", "next_actions", "job_dir"}

    基线顺序（A19/A20 修订：登记优先，遗留目录兜底）：
        1. `job.json` 里 `export_path` 登记的**固定交付目录**（不扫描、不猜最近一次）
        2. `jobs/<id>/valid/*.pptx`：runner **从不写** valid/，这条只作遗留兜底。

    为什么把登记的交付目录提前：valid/ 若存在，多半是外部手工放的旧文件，
    拿它当第一基线会让"改的是哪一稿"说不清；登记字段是可追溯的那一个。
    """
    d = Path(job_dir)
    out = {"ok": False, "path": None, "source": None, "code": None,
           "detail": "", "next_actions": [], "job_dir": str(d)}

    def fail(code, detail, actions):
        # 遗留兜底：runner 从不写 valid/，但外部手工放过的话仍可作为最后手段，
        # 并在 source 里标明它是 legacy，避免和"登记的交付目录"混淆。
        legacy = sorted(p for p in (d / "valid").glob("*.pptx") if p.is_file())
        if legacy:
            out.update(ok=True, path=legacy[0], source="valid(legacy)",
                       code=None, next_actions=[],
                       detail=("基线来自任务内 valid/ 目录（遗留位置，runner 不写这里）；"
                               "登记的交付目录当前不可用：%s" % code))
            return out
        out.update(code=code, detail=detail, next_actions=actions)
        return out

    job_file = d / "job.json"
    if not d.is_dir():
        return fail("BASELINE.UNREADABLE",
                    "任务目录不存在，无法确定可编辑基线。",
                    [{"action": "list_jobs", "endpoint": "GET /api/jobs",
                      "label": "先确认任务 id 是否正确"}])
    job = load_json(job_file)
    if job is None:
        return fail("BASELINE.UNREADABLE",
                    ("任务登记文件 job.json 缺失或无法解析（%s），"
                     "因此无法确定这一稿的基线文件。" % job_file.name),
                    [{"action": "inspect_job", "endpoint": "GET /api/jobs/%s" % d.name,
                      "label": "查看任务状态与产物；登记损坏时需重新建任务"},
                     {"action": "recreate", "endpoint": "POST /api/make",
                      "label": "用同一原稿重新建任务（原件不会被改动）"}])

    # 1) 固定交付目录（export_path 登记）：可追溯的那一个
    exp = job.get("export_path")
    if not exp:
        return fail("BASELINE.NOT_EXPORTED",
                    "这个任务还没有成功导出过，因此没有可编辑的基线文件。"
                    "（原因不是「修改范围不允许」，而是「还没有导出的稿子可改」。）",
                    [{"action": "validate", "endpoint": "POST /api/jobs/%s/validate" % d.name,
                      "label": "先验收当前候选"},
                     {"action": "export", "endpoint": "POST /api/jobs/%s/export" % d.name,
                      "label": "验收通过后导出；导出成功会自动产生基线"}])
    base = Path(exp)
    if not base.is_absolute():
        base = d.resolve().parents[1] / base
    base = base.resolve()
    if not base.is_dir():
        return fail("BASELINE.EXPORT_DIR_MISSING",
                    "任务登记了交付目录，但该目录已不存在：%s" % base,
                    [{"action": "export", "endpoint": "POST /api/jobs/%s/export" % d.name,
                      "label": "重新导出一次，重建交付目录"},
                     {"action": "inspect_job", "endpoint": "GET /api/jobs/%s" % d.name,
                      "label": "核对 export_path 登记是否与实际交付位置一致"}])
    cands = sorted(p for p in base.rglob("*.pptx") if p.is_file())
    if not cands:
        return fail("BASELINE.EXPORT_EMPTY",
                    "交付目录存在但里面没有 PPTX：%s" % base,
                    [{"action": "export", "endpoint": "POST /api/jobs/%s/export" % d.name,
                      "label": "重新导出一次"},
                     {"action": "inspect_job", "endpoint": "GET /api/jobs/%s" % d.name,
                      "label": "查看导出事件记录，确认上次导出是否被拒"}])
    out.update(ok=True, path=cands[0], source="export",
               detail="基线来自 job.json 登记的交付目录。",
               code=None, next_actions=[])
    return out


def open_baseline(path) -> dict:
    """打开基线并给出"能不能编辑 / 为什么不能"的判定（不修改文件）。"""
    from pptx import Presentation
    p = Path(path)
    if not p.is_file():
        return {"ok": False, "code": "BASELINE.NOT_EXPORTED",
                "detail": "基线文件已不在：%s" % p,
                "next_actions": [{"action": "reload", "endpoint": "GET /api/jobs/objects",
                                  "label": "重新解析可编辑对象"}]}
    try:
        prs = Presentation(str(p))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "code": "BASELINE.CORRUPT",
                "detail": ("基线文件打不开（%s）：%s。文件本身已损坏，"
                           "改它没有意义，需要重新导出一份。" % (type(e).__name__, e)),
                "next_actions": [{"action": "export", "endpoint": "POST /api/jobs/export",
                                  "label": "从原稿重新导出"},
                                 {"action": "parse_error", "endpoint": "GET /api/jobs",
                                  "label": "若反复损坏，按 OBJECT.PARSE_ERROR 上报"}]}
    return {"ok": True, "presentation": prs, "pages": len(prs.slides)}



def apply_local_edit(job_dir: Path, edit: dict, base_pptx: Path | None = None):
    """执行一次授权局部修改，产出**独立候选**并登记版本。

    edit 形如：
        {"page": 3, "shape": "标题 1", "field": "text", "new_value": "新标题"}

    返回 dict：候选路径、修改前后哈希、非目标不变证明、版本登记。
    任何越界/共享资源/未指定字段情形一律抛 EditScopeError。
    """
    from pptx import Presentation

    d = Path(job_dir)
    d.mkdir(parents=True, exist_ok=True)

    # ---- 1. 请求必须是"明确到字段"的授权修改 ----
    for key in ("page", "shape", "field", "new_value"):
        if key not in edit or edit[key] in (None, ""):
            raise EditScopeError(
                "SCOPE.UNREQUESTED_CHANGE",
                f"局部修改必须显式指定 {key}；不接受整篇重写或缺省字段。")
    field = str(edit["field"])
    if field not in ("text", "cell", "chart_value", "image"):
        raise EditScopeError(
            "SCOPE.UNREQUESTED_CHANGE",
            f"当前支持 field ∈ text/cell/chart_value/image；收到 {field!r}。")
    # 各字段类型的附加必填参数（缺一即视为未明确授权）
    EXTRA_REQUIRED = {
        "cell": ("row", "col"),
        "chart_value": ("series", "index"),
        "image": ("asset",),
    }
    for key in EXTRA_REQUIRED.get(field, ()):  # noqa: SIM118
        if key not in edit or edit[key] in (None, ""):
            raise EditScopeError(
                "SCOPE.UNREQUESTED_CHANGE",
                f"field={field} 必须显式指定 {key}；不接受缺省定位。")
    if "whole_deck" in edit or edit.get("scope") == "whole_deck":
        raise EditScopeError(
            "SCOPE.UNREQUESTED_CHANGE",
            "整篇重写不属于局部修改；请指明页/对象/字段。")

    # ---- 2. 基线：优先当前有效版本；否则用**固定交付目录**里已导出的候选 ----
    valid_pptx = d / "valid"
    if base_pptx is not None:
        state = {"ok": True, "path": Path(base_pptx), "source": "explicit",
                 "code": None, "detail": "调用方指定基线。", "next_actions": []}
    else:
        # runner 的"有效版本"是导出后的固定交付目录（export_path）。
        # 只接受已登记在 job.json 里的交付位置，不猜测、不从候选反推。
        state = resolve_baseline(d)
    if not state["ok"] or not Path(state["path"]).is_file():
        # 这里**不再**用 SCOPE.UNREQUESTED_CHANGE：请求本身没有越界，
        # 缺的是可编辑的基线。给出各自的真实原因与下一步。
        code = state["code"] or "BASELINE.NOT_EXPORTED"
        hint = "；".join(a["label"] for a in state.get("next_actions") or [])
        raise EditScopeError(code, state["detail"] + (("下一步：" + hint) if hint else ""))
    base = Path(state["path"])
    base_source = state["source"]
    base_sha = sha256_file(base)

    # ---- 3. 先确认基线本身能打开：文件坏了就说"坏了"，不要拿"哈希不一致"顶替 ----
    #     （哈希不一致只说明字节变了；字节变了且打不开时，真实原因是有文件损坏，
    #       对应的下一步是重新导出，而不是"等人工合并冲突"。）
    opened = open_baseline(base)
    if not opened["ok"]:
        raise EditScopeError(opened["code"], opened["detail"])
    prs = opened["presentation"]

    # ---- 3b. 乐观并发保护（A18 修订）----
    #     旧实现读 `current_version.json` 做冲突检测，但 runner 从不写这个文件，
    #     于是保护**永远不触发**（auto30 A18）。两种改法：伪造一个 current_version
    #     去喂它，或者换成"调用方声明它以为的基线"。后者不需要任何虚构状态：
    #     GET /objects 会回 baseline_sha256，客户端带着它回来，不一致就是真冲突。
    expected = edit.get("expected_base_sha256")
    if expected and str(expected).strip():
        if str(expected).strip().lower() != str(base_sha).lower():
            raise EditScopeError(
                "VERSION.MERGE_CONFLICT",
                f"基线在你取到对象清单之后变了：你声明的是 {str(expected)[:16]}…，"
                f"磁盘上现在是 {base_sha[:16]}…。请重新取一次对象清单再改，"
                f"不要在不确定的基线上修改。")

    # ---- 4. 共享资源保护：目标对象若为跨页共享资源则拒绝 ----
    #     注意顺序：共享检查必须**先于**"是否文本框"检查，
    #     否则共享图片会因"没有文本框"而被拒，掩盖真正的共享保护结论。
    shared = _shared_resources(prs)
    shp = _resolve_shape(prs, int(edit["page"]), str(edit["shape"]))
    # 4a. 目标对象**自身部件**若被多页共享 → 立即拒绝（这是共享保护的主防线）
    try:
        own_part = str(shp.part.partname)
    except Exception:  # noqa: BLE001
        own_part = None
    # graphicFrame 的图表走 r:id 指向 chart part，需单独解析
    chart_part = None
    if getattr(shp, "has_chart", False) and shp.has_chart:
        try:
            chart_part = str(shp.chart.part.partname)
        except Exception:  # noqa: BLE001
            chart_part = None
    for label, partname in (("对象部件", own_part), ("图表部件", chart_part)):
        if partname and partname in shared:
            raise EditScopeError(
                "SCOPE.UNREQUESTED_CHANGE",
                f"目标{label} {partname} 被第 {shared[partname]} 页共享；"
                "修改会连带影响其他页，拒绝（共享资源保护）。")
    # 4b. 目标对象引用的图片/图表资源若为跨页共享 → 拒绝
    try:
        blip_rids = [rId for rId in getattr(shp, "_element", [])
                    .xpath(".//@r:embed") or []]
    except Exception:  # noqa: BLE001
        blip_rids = []
    for rid in blip_rids:
        if rid in shared:
            raise EditScopeError(
                "SCOPE.UNREQUESTED_CHANGE",
                f"目标对象引用共享资源 {rid}（页 {shared[rid]}）；"
                "修改会连带影响其他页，拒绝（共享资源保护）。")

    # 4c. 按字段类型校验目标对象能力（共享保护已在 4a/4b 完成，优先于本步）
    field = str(edit["field"])
    if field == "text":
        if not (getattr(shp, "has_text_frame", False) and shp.has_text_frame):
            raise EditScopeError(
                "SCOPE.UNREQUESTED_CHANGE",
                "目标对象没有可编辑文本框。")
    elif field == "cell":
        if not (getattr(shp, "has_table", False) and shp.has_table):
            raise EditScopeError(
                "SCOPE.UNREQUESTED_CHANGE",
                "field=cell 要求目标是原生表格对象。")
        try:
            r_i, c_i = int(edit["row"]), int(edit["col"])
        except (TypeError, ValueError):
            raise EditScopeError(
                "SCOPE.UNREQUESTED_CHANGE",
                "row/col 必须是整数。") from None
        tbl = shp.table
        if not (0 <= r_i < len(tbl.rows) and 0 <= c_i < len(tbl.columns)):
            raise EditScopeError(
                "SCOPE.UNREQUESTED_CHANGE",
                f"单元格 ({r_i},{c_i}) 越界；表格为 "
                f"{len(tbl.rows)} 行 × {len(tbl.columns)} 列。")
    elif field == "chart_value":
        if not (getattr(shp, "has_chart", False) and shp.has_chart):
            raise EditScopeError(
                "SCOPE.UNREQUESTED_CHANGE",
                "field=chart_value 要求目标是原生图表对象。")
    elif field == "image":
        if not (getattr(shp, "shape_type", None) is not None
                and shp.shape_type == 13):  # MSO_SHAPE_TYPE.PICTURE
            raise EditScopeError(
                "SCOPE.UNREQUESTED_CHANGE",
                "field=image 要求目标是图片对象。")
        # 只接受**受控本地资产**：必须落在项目 samples/ 或显式授权目录下，
        # 不接受任意绝对路径、URL 或模型给出的路径。
        asset = Path(str(edit["asset"]))
        if not asset.is_absolute():
            asset = (Path(__file__).resolve().parents[1] / asset)
        asset = asset.resolve()
        allowed_roots = [Path(__file__).resolve().parents[1] / "samples",
                         Path(__file__).resolve().parents[1] / "assets"]
        # 工作台上传的受控资产目录（数据根下，安装布局也能用）；
        # 只有显式设置了 WENGAO_DATA_ROOT 才生效，仍不接受任意路径/URL。
        _dr = os.environ.get("WENGAO_DATA_ROOT")
        if _dr:
            allowed_roots.append(Path(_dr) / "assets")
        if not any(str(asset).startswith(str(r.resolve()) + "/")
                   or asset == r.resolve() for r in allowed_roots):
            raise EditScopeError(
                "SCOPE.UNREQUESTED_CHANGE",
                f"图片资产 {asset} 不在受控目录内；"
                "只接受 samples/、assets/（含工作台上传目录）下的本地资产，"
                "不接受任意路径或 URL。")
        if not asset.is_file():
            raise EditScopeError(
                "SCOPE.UNREQUESTED_CHANGE", f"图片资产不存在：{asset}")

    # ---- 5. 保存前版（非目标不变证明的基线）----
    versions = d / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    vid = f"v{len(list(versions.glob('*.json'))) + 1:04d}"
    pre_copy = versions / f"{vid}.before.pptx"
    shutil.copy2(base, pre_copy)

    # 逐对象快照：用于证明"只有目标变了"
    def snapshot(prs_):
        """逐对象快照：用于证明"只有目标变了"。

        文本对象取文本；表格取单元格文本；图片取内容 sha1；
        图表取系列数值 —— 否则换图/改图表会被误判为"没有实际修改"。
        """
        out = {}
        for i, slide in enumerate(prs_.slides, start=1):
            for s in slide.shapes:
                t = ""
                if getattr(s, "has_text_frame", False) and s.has_text_frame:
                    t = s.text_frame.text
                elif getattr(s, "has_table", False) and s.has_table:
                    t = "\u0001".join(c.text for r in s.table.rows for c in r.cells)
                elif getattr(s, "has_chart", False) and s.has_chart:
                    try:
                        t = "CHART:" + "|".join(
                            ",".join(str(v) for v in ser.values)
                            for ser in s.chart.series)
                    except Exception:  # noqa: BLE001
                        t = "CHART:<unreadable>"
                elif s.shape_type == 13:
                    try:
                        t = f"IMG:{s.image.sha1}"
                    except Exception:  # noqa: BLE001
                        t = "IMG:<unreadable>"
                out[f"{i}:{s.shape_id}:{s.name}"] = t
        return out

    before_map = snapshot(prs)

    # ---- 6. 执行唯一一处修改（按字段类型）----
    if field == "text":
        old_value = shp.text_frame.text
        shp.text_frame.text = str(edit["new_value"])
    elif field == "cell":
        cell = shp.table.cell(int(edit["row"]), int(edit["col"]))
        old_value = cell.text
        cell.text = str(edit["new_value"])
    elif field == "chart_value":
        chart = shp.chart
        plot = chart.plots[0]
        cats = list(plot.categories)
        s_i, p_i = int(edit["series"]), int(edit["index"])
        if s_i >= len(chart.series):
            raise EditScopeError(
                "SCOPE.UNREQUESTED_CHANGE",
                f"系列序号 {s_i} 越界（该图表共 {len(chart.series)} 个系列）。")
        ser = chart.series[s_i]
        vals = list(ser.values)
        if not (0 <= p_i < len(vals)):
            raise EditScopeError(
                "SCOPE.UNREQUESTED_CHANGE",
                f"数据点序号 {p_i} 越界（该系列共 {len(vals)} 个点）。")
        try:
            new_num = float(str(edit["new_value"]).strip())
        except ValueError:
            raise EditScopeError(
                "SCOPE.UNREQUESTED_CHANGE",
                f"chart_value 的 new_value 必须是数值，收到 "
                f"{edit['new_value']!r}。") from None
        old_value = vals[p_i]
        vals[p_i] = new_num
        from pptx.chart.data import CategoryChartData
        cd = CategoryChartData()
        cd.categories = cats
        for k, sr in enumerate(chart.series):
            cd.add_series(sr.name or f"系列{k+1}",
                          tuple(vals if k == s_i else list(sr.values)))
        chart.replace_data(cd)
        old_value = f"{cats[p_i] if p_i < len(cats) else p_i}={old_value}"
    else:  # image
        old_value = str(getattr(shp.image, "sha1", "") or "（原图片）")
        # 只替换**非共享**图片的图片关系目标，保持位置尺寸不变
        new_p = Path(str(edit["asset"]))
        if not new_p.is_absolute():
            new_p = Path(__file__).resolve().parents[1] / new_p
        img_part, rId = shp.part.get_or_add_image_part(str(new_p))
        blip = shp._element.blipFill.find(
            "{http://schemas.openxmlformats.org/drawingml/2006/main}blip")
        blip.set("{http://schemas.openxmlformats.org/officeDocument/2006/"
                 "relationships}embed", rId)

    # ---- 7. 非目标不变校验（逐对象对比，只允许目标键变化）----
    after_map = snapshot(prs)
    target_key = f"{int(edit['page'])}:{shp.shape_id}:{shp.name}"
    changed = [k for k in before_map if before_map[k] != after_map.get(k)]
    unexpected = [k for k in changed if k != target_key]
    if unexpected:
        raise EditScopeError(
            "SCOPE.UNREQUESTED_CHANGE",
            f"检测到非目标对象被改动：{unexpected[:5]}；已拒绝提交。")
    if not changed:
        raise EditScopeError(
            "SCOPE.UNREQUESTED_CHANGE",
            "新值与原值相同，未产生实际修改；不生成无意义版本。")

    # ---- 8. 产出独立候选（不覆盖 valid/）----
    cand_dir = d / "edit_candidates"
    cand_dir.mkdir(parents=True, exist_ok=True)
    cand = cand_dir / f"{vid}.{base.stem}.edited.pptx"
    prs.save(str(cand))

    record = {
        "version_id": vid,
        "created_at": now_iso(),
        "base_pptx": str(base),
        "base_sha256": base_sha,
        "base_source": base_source,
        "candidate_pptx": str(cand),
        "candidate_sha256": sha256_file(cand),
        "pre_copy": str(pre_copy),
        "edit": {k: edit[k] for k in ("page", "shape", "field", "new_value")},
        "old_value": old_value,
        "new_value": str(edit["new_value"]),
        "changed_objects": changed,
        "unexpected_changes": unexpected,
        "untouched_objects_verified": len(before_map) - len(changed),
        "shared_resources_blocked": sorted(shared),
    }
    save_json_atomic(versions / f"{vid}.json", record)
    return record


def restore_version(job_dir: Path, version_id: str):
    """恢复历史版本：**必须重新检查完整性与当前适用要求**，不得盲用旧 READY。

    返回恢复登记。恢复产物是独立候选（`edit_candidates/restore_*.pptx`），
    因此 `requires_revalidation` 恒为 **True**：这是一个真正写出新文件、
    需要走验收流程的操作，不存在"不写候选、不切版本"的 no-op 情形。

    为什么不给"字节相同"留免验口子（2026-10-18 收尾复核）：
    - 恢复**每次都创建新候选文件**，不是 no-op，所以没有"无需恢复"可言；
    - "产物字节与某次验收的候选相同"只说明没有引入新字节，不等于那次验收
      对当前状态仍然有效。导出门禁能拒绝过期证据（EVIDENCE.STALE）是有价值的
      下游保护，但**不能替上游"不需要复验"的错误承诺背书**——若上游先声明免验，
      调用方就没有理由再去走验收，失效证据会被直接当成有效。
    - 字节/attempt 相同只作为登记里的**说明性事实**保留，便于复核，
      不参与判定。
    """
    d = Path(job_dir)
    versions = d / "versions"
    rec_path = versions / f"{version_id}.json"
    rec = load_json(rec_path)
    if not rec:
        raise EditScopeError("VERSION.MERGE_CONFLICT",
                              f"版本 {version_id} 不存在。")

    pre = Path(rec["pre_copy"])
    if not pre.is_file():
        raise EditScopeError("VERSION.MERGE_CONFLICT",
                              f"版本 {version_id} 的前版文件缺失，无法校验完整性。")
    actual = sha256_file(pre)
    if actual != rec["base_sha256"]:
        raise EditScopeError(
            "VERSION.MERGE_CONFLICT",
            f"版本 {version_id} 前版完整性校验失败："
            f"记录 {rec['base_sha256'][:16]}… ≠ 实际 {actual[:16]}…")

    cand_dir = d / "edit_candidates"
    cand_dir.mkdir(parents=True, exist_ok=True)
    restored = cand_dir / f"restore_{version_id}.pptx"
    shutil.copy2(pre, restored)
    restored_sha = sha256_file(restored)

    # A17 收尾（2026-10-18）：恢复默认复验，不因字节相同而免验。
    # 字节与 attempt 的比较结果只写进登记供复核，不参与判定。
    val = load_json(d / "validation.json") or {}
    job = load_json(d / "job.json") or {}
    accepted_sha = val.get("candidate_pptx_sha256")
    val_attempt = val.get("attempt_id")
    cur_attempt = job.get("attempt_id")
    bytes_match = bool(accepted_sha) and accepted_sha == restored_sha
    attempt_current = bool(val_attempt) and val_attempt == cur_attempt
    basis = (
        "恢复产物是新建的独立候选 %s（%s），必须重新走验收；"
        "即使字节与此前某次验收的候选相同（%s），也不构成免验理由——"
        "那次验收是否对当前 attempt %s 仍然有效，由重新验收判定。"
        % (restored.name, restored_sha[:16] + "…",
           ("是" if bytes_match else "否"), (cur_attempt or "无")[:16]))
    out = {
        "restored_from": version_id,
        "restored_pptx": str(restored),
        "restored_sha256": restored_sha,
        "integrity_verified": True,
        "base_sha256": rec["base_sha256"],
        "restored_at": now_iso(),
        "accepted_candidate_sha256": accepted_sha,
        "accepted_attempt_id": val_attempt,
        "current_attempt_id": cur_attempt,
        # 以下两项是说明性事实，供复核比对；**不参与**免验判定。
        "identical_to_accepted_candidate": bytes_match,
        "identical_and_attempt_current": bytes_match and attempt_current,
        "revalidation_basis": basis,
        "note": ("恢复产物是新建的独立候选，必须重新验收；"
                 "不得直接沿用该版本的旧 verdict/READY；"
                 "合法复验后能否导出，仍由导出门禁独立核对"
                 "（attempt/契约/主题/输入/渲染/事故）。"),
        "requires_revalidation": True,
    }
    save_json_atomic(versions / f"restore_{version_id}.json", out)
    return out


def list_versions(job_dir: Path):
    d = Path(job_dir) / "versions"
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("v*.json")):
        r = load_json(p)
        if r:
            out.append({"version_id": r.get("version_id"),
                        "created_at": r.get("created_at"),
                        "edit": r.get("edit"),
                        "candidate_sha256": r.get("candidate_sha256")})
    return out
