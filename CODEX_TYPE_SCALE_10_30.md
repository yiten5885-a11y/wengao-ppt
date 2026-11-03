# 修改指令：原稿字号体系从"一律 ≥24pt"改为"10–30pt 分级 + 按页适配"

- 任务标识：WENGAO-TYPE-SCALE-10-30
- 工作目录：`~/WorkBuddy/2026-10-12-00-54-41/wengao-ppt/build/wengao-visual-v13-material-fix`
- 验证样本：`workbench_uploads/00180522__Token_.pptx`（《从卖流量到卖Token 副本》，10 页），任务 `job-f51a976c5258`，主题 `wengao-minimal`
- 工作台：`http://127.0.0.1:8823`（启动命令见第 7 节）

---

## 0. 背景（为什么要改）

现行规则（2026-10-21 "字太小"要求）是：所有正文至少 24pt、标题 36–40、关键数字 44–56，原稿小字一律抬到下限。

这份样本按 10–14pt 设计（字号分布：9.75pt×10、10.5pt×63、11.25pt×39、12pt×19、12.75pt×30、13.5pt×27，其余为少量 15–18pt 正文和 27pt 以上的标题/大数字），第 3、4、6 页是密集卡片。抬到 24pt 以后内容在原版式里放不下，结果是：

- 排版门禁 FAIL，共 579 条发现，其中 OWNER_BOX_OVERFLOW 87 条，HG02 字符缺失/对不上 330 条，REGION_INTRUSION 19 条；
- 只挪位置解决不了（已修过"推出页面"的问题，见 `wengao_ppt/visual.py` 的 `text_frame_no_wrap` 与 `push_blocked_off_canvas`，**这两处保留，不要回退**）。

新目标：**字号在 10–30pt 范围内按角色分级，按每页实际空间选能放下的最大档**。保持层级清晰，不再用一个全局下限强推。

---

## 1. 新字号体系（唯一事实来源）

新建 `wengao_ppt/type_scale.py`，只放数据和纯函数。下列四处全部从这里读，**不得各写一份**：主题、精修、门禁脚本、门禁政策文件。

```python
TYPE_SCALE_VERSION = "WG-TYPE-20261027.1"
TYPE_MIN_PT, TYPE_MAX_PT = 10.0, 30.0

# 角色 -> (下限, 优选上限)。按页适配时在 [下限, 上限] 内取能放下的最大值。
ROLE_RANGE_PT = {
    "title":      (24, 30),   # 页标题
    "key_number": (24, 30),   # 关键数字（原 44–56，封顶 30）
    "lead":       (16, 22),   # 导语/副标题/卡片大标题
    "conclusion": (16, 22),   # 关键结论
    "body":       (13, 18),   # 正文（密集页可到 13，宽松页 16–18）
    "table":      (11, 14),
    "label":      (11, 14),   # 卡片内小标签、眉题、等级标签
    "chart_aux":  (10, 12),
    "ordinal":    (12, 16),   # 序号徽标
    "footer":     (10, 11),   # 页脚、页码
    "source":     (10, 11),   # 来源标注
}
# 同一页内必须满足的层级（左 > 右，按该页实际使用字号的中位数比较）
HIERARCHY = [("title", "lead"), ("lead", "body"), ("body", "label"), ("label", "footer")]
TITLE_BODY_MIN_RATIO = 1.4   # 页标题字号 / 正文字号 ≥ 1.4
```

**装饰性大字**（原稿中 ≥60pt 的单个数字/1–2 字符，如 82.5/96/225pt 的背景序号）归为角色 `decor`：

- 不受 10–30 上限约束，保留原字号；
- 必须被门禁单独计数并写进报告，且不得遮挡正文（HG04 照查）。

其他所有内容文字都必须落在 [10, 30]。

---

## 2. 改哪里、怎么改

### 2.1 `wengao_ppt/theme.py`

- `READABILITY_SIZE_FLOOR`（约第 209 行）：从 `type_scale.ROLE_RANGE_PT` 派生，不再写死 36/24/28/24/16/18/20。`_classify()` 只有 title/subtitle/heading/body/note 五档，映射关系为：
  - title → title
  - subtitle → lead
  - heading → lead
  - body → body
  - note → footer
- 每个主题的 `sizes` 是**上限闸**，全部改成 ≤30。例如：
  - `wengao-basic` 的 title 36 → 30；
  - `wengao-visual-dark` 的 title 38 → 30。
  - 主题之间的风格差异体现在区间内的偏好（例如极简主题 body 偏好 14），不再靠超过 30 来体现。
- `_capped()`：保留"过大压下"。"过小抬起"改为只抬到**角色下限**，不再抬到旧的 24。
- 第 667–690 行（对所有主题复用 `refine_mod.rule_readability_floor`）：改为调用新的按页适配函数（见 2.2）。
- `MATERIAL_SIZE_FLOOR`（材料生成路径）：**本次不改数值**。在注释里写明它与原稿路径的区别，并在报告里单列"材料路径未改"。

### 2.2 `wengao_ppt/refine.py`

- 删除 `READABILITY_ROLE_PT`，改为引用 `type_scale.ROLE_RANGE_PT`。`tests/test_refine_readability_floor.py` 同步修改。
- 用新函数 `fit_page_type_scale(shs, sw, sh_, acts)` 替换 `rule_readability_floor` 的逻辑（旧函数名可以保留为薄包装，避免调用点大改）。算法如下：
  1. 用 `infer_text_roles()` 得到本页每个文本框的角色（与门禁 `infer_roles` 同口径，不改判定）。
  2. 为本页选一个**档位系数** `k ∈ [0, 1]`，每个角色的目标字号 = `下限 + k × (上限 − 下限)`，取 0.5pt 整数倍。同一角色在同一页用同一字号。
  3. 从 `k = 1` 开始向下二分，找到满足下面全部条件的最大 `k`：
     - a. 每个文本框按目标字号测得的内容高度 ≤ 可用高度。可用高度 = 到下方同列最近块（或卡片底边）的距离减去 `FLOOR_MIN_GAP_IN`。
     - b. 不折行框（`text_frame_no_wrap`）的单行宽度 ≤ 可用宽度。可用宽度 = 到右侧最近块、所属卡片右内边距、页面右安全边距三者中最近的距离。
     - c. 文字仍在所属卡片内，并满足卡片内边距：`max(0.10in, 0.35em)`。
     - d. 全部内容在画布安全边距内：左右 0.40in、上 0.30in、下 0.15in。
  4. 若 `k = 0`（全部取下限）仍放不下，允许在**本页**做以下几何调整，按顺序尝试，每一步都不改文字：
     - 不折行框改为 `wrap="square"`，宽度设为可用宽度，高度随内容；
     - 在卡片内部把同卡片的文本块做等距重排（只在卡片范围内动，卡片本身不动）；
     - 同列卡片整体下移（复用 `_plan_cascade`，放不下就返回 None）。
  5. 仍放不下：保留原字号，前提是原字号 ≥10。原字号 <10 的抬到 10。在 `acts` 里登记 `type_scale_no_fit`，写明页、形状和缺多少英寸。**不得 autofit、不得缩到 10 以下、不得出页。**
  6. 校验 `HIERARCHY` 与 `TITLE_BODY_MIN_RATIO`。不满足时只允许上调高层级角色（在其区间内），仍不满足就登记 `type_scale_hierarchy_violation`。
- 计算高度、宽度时，凡涉及折行估算一律用已经修好的不折行判断（`Sh.line_count` 已处理 `wrap="none"`）。

### 2.3 `wengao_ppt/visual.py`

- `fix_stacked_text_overlap` 保持当前修复，不要回退。
- 把第 ~1089 行注释里 "body text is laid out at that range's floor already" 的说法更新为新体系的描述。
- 若 2.2 已经保证了几何，这一步应当基本不再触发 `pushed_by`。验收时统计这个数，作为侧面指标。

### 2.4 `scripts/layout_gate.py` 与门禁政策（受冻结身份保护，必须按流程改）

- `ROLE_PT`（第 52 行）改为从 `type_scale.ROLE_RANGE_PT` 导入。门禁要和产品用**同一张表**。
- HG07 规则改为：
  - 每个内容 run 的字号必须在其角色区间内；
  - 新增两条检查：
    - `TYPE_SCALE_HIERARCHY`：同页层级倒挂；
    - `TYPE_SCALE_OUT_OF_RANGE`：<10 或 >30 且不是 `decor`；
  - `decor` 单独列入 `decor_count`，不算违规。
- `contracts/layout_hard_gate_policy.json`：
  - `font_roles_pt` 段改成新区间；
  - `policy_version` 从 `WG-LAYOUT-20261021.1` 升为 `WG-LAYOUT-20261027.1`；
  - 新增 `type_scale_version` 字段。
- `wengao_ppt/runner.py`：
  - 同步更新 `LAYOUT_GATE_POLICY_BUNDLE_SHA256`、`LAYOUT_GATE_POLICY_DOCUMENT_SHA256`（第 82 行附近）；
  - 第 545 行附近对 `policy_version` 的硬校验改为新版本号。
- 在 `VERSIONS.md` 追加一条记录：旧规则、新规则、原因（本文件第 0 节）、新旧哈希。

### 2.5 测试

- 修改 `tests/test_refine_readability_floor.py`：断言改为基于新区间。
- 新增 `tests/test_type_scale_10_30.py`，至少覆盖：
  1. 密集卡片页（构造 3 张卡片 × 4 行 10.5pt 文本）：适配后所有 run 在区间内、无出页、无卡片溢出，且 body ≥13；
  2. 宽松页（一个标题 + 一段正文）：取到区间上限（title 30、body 18）；
  3. 层级：任一页 `title / body ≥ 1.4`；
  4. 不折行框超宽时会转成折行，而不是伸出卡片；
  5. 装饰性 225pt 单字 `decor` 保留原字号且被计数；
  6. 放不下时登记 `type_scale_no_fit`，原字号 ≥10 的保持不变；
  7. 门禁 `ROLE_PT` 与 `type_scale.ROLE_RANGE_PT` 是同一对象或内容一致（防止再次分叉）。
- 全量测试：`./.venv/bin/python -m pytest -q -p no:cacheprovider tests`，必须全部通过（当前基线 580 passed）。

---

## 3. 边界（不能做的事）

- **不改任何文字内容**：不删字、不改写、不加省略号、不拆段。
- 不用 `normAutofit` / `spAutoFit` 让渲染器缩字，不设 `fontScale`。
- 任何内容文字不得 <10pt；除 `decor` 外不得 >30pt。
- 不移动或缩放图片、图表、卡片背景的**宽度**。卡片只允许整体下移（2.2 第 4 步）。
- 不改 `infer_roles` / `infer_text_roles` 的角色判定逻辑。如果发现判定错误，单独登记，不在本任务里改。
- 不改语义配图、云端模型、重试逻辑（`scene_model.py` 刚修过 SSLEOFError，不要动）。
- 遵守 `CURRENT_TASK.md` 的"明确保留"：
  - 不删除、不移动 `jobs/`、`reports/`、`release_candidate*` 下的既有文件；
  - 不 `rm -rf`、不推送、不调用未授权云端模型。
- 冻结身份文件（政策 JSON、门禁脚本）的修改必须同步更新哈希和 `VERSIONS.md`。不得为了让门禁通过而放宽与字号无关的阈值，包括 HG02/HG03/HG04/HG05/HG06/HG09 的任何数值。

---

## 4. 细节要求

- 字号取 0.5pt 的整数倍，同页同角色同字号。不同页之间同角色的字号差 ≤4pt，避免翻页时忽大忽小。
- 行距保持原稿行距比例。若原稿未设置，正文按 1.2 倍、标题按 1.1 倍。
- 中文与西文 run 同字号，不因字体回退单独缩放。
- 折行后不得出现单字孤行（HG08 ORPHAN_CHAR_LINE）。可以在可用宽度内微调框宽，沿用现有 `widen_for_orphan_char` 逻辑。
- 第 3 页卡片里"组织/平台/业务/用户"这类 label 和它下面的说明必须仍在同一张卡片里（HG05 CARD_TEXT_ESCAPED = 0）。
- 对比度（HG09）不在本任务范围内，但不得因为改字号而**新增** HG09 发现。

---

## 5. 怎么样才算改好了（验收标准，全部满足）

在样本 `job-f51a976c5258` + `wengao-minimal` 上重跑（第 7 节）：

| 项目 | 要求 | 当前值（2026-10-27 21:43） |
|---|---|---|
| 编排结果 | 不再是 `verdict=FAILED` 停止 | STOPPED（FAILED） |
| 校验阻断项 `blocking_findings` | 0 | 1 |
| 排版门禁 `layout_hard_gate.verdict` | PASS | FAIL |
| HG02 CHAR_MISSING / UNMATCHED / INVISIBLE / PARTIAL | 0 / 0 / 0 / 0 | 106 / 224 / 21 / 26 |
| HG03 GLYPH_COLLISION | 0 | 1 |
| HG04 LATER_SHAPE_OCCLUSION | 0 | 7 |
| HG05（CANVAS_MARGIN、OWNER_BOX_OVERFLOW、CARD_*） | 0 | 4 + 87 + 6 |
| HG06 REGION_GAP / INTRUSION / FOOTER_GAP | 0 | 5 / 19 / 5 |
| HG07（含新 HIERARCHY / OUT_OF_RANGE） | 0 | 6 |
| HG08 ORPHAN_CHAR_LINE | 0 | 4 |
| HG09 LOCAL_CONTRAST | 不多于修改前 | 55 |
| RENDER.REQUIRED_CONTENT_CLIPPED | 0 | 0 |

另外还需要满足：

1. **字号分布**：从候选 PPTX 读取全部内容 run，除 `decor` 外，最小值 ≥10、最大值 ≤30。每页 `title/body ≥ 1.4`。在报告里给出每页各角色字号表。
2. **密集页确实变大**：第 3、4、6 页正文中位数 ≥13pt（原稿约 10.5–13.5pt），标题 ≥24pt。
3. **其他主题不退化**：同一样本分别用 `wengao-basic`、`wengao-business`、`wengao-dark` 各跑一次，阻断项为 0；若不为 0，逐条说明原因。
4. **回归样本**：运行 `scripts/prepublication_50_decks_gate.py`（或项目现有的 50 份样本门禁），与修改前对比，通过数不得下降。给出前后数字。
5. **全量测试**：全部通过，新增测试在旧代码上失败、在新代码上通过（证明测试有效）。
6. **人工目检**：导出第 3、4、6、7 页的 PNG（候选目录 `candidate/render/` 下的 PDF 转图），确认：
   - 卡片内文字不贴边、不溢出；
   - 标题明显大于正文；
   - 整页没有大片空白，也没有挤成一团。
   - 截图路径写进报告。
7. **交付报告** `reports/type-scale-10-30/REPORT.md`，内容包括：
   - 改动文件清单；
   - 新旧哈希；
   - 上表前后对比；
   - 每页字号表；
   - `type_scale_no_fit` / `hierarchy_violation` 登记清单（期望为空，非空要解释）；
   - 未完成项。

---

## 6. 建议执行顺序

1. 新建 `type_scale.py` 和测试第 7 条（单一来源），先让门禁与产品共用同一张表。
2. 改 `refine.py` 的按页适配（2.2），先在纯函数层面写好测试 1–6。
3. 接入 `theme.py`（2.1），用下面的离线脚本对样本只跑主题这一步，看出页、溢出情况。
4. 改门禁与政策（2.4），更新哈希与 `VERSIONS.md`。
5. 重启工作台，重跑样本，按第 5 节逐项核对。
6. 跑其他主题与 50 份回归，写报告。

离线快速检查（只跑主题，不经过工作台）：

```bash
./.venv/bin/python -c "
from pptx import Presentation
from wengao_ppt.theme import apply_theme
apply_theme('workbench_uploads/00180522__Token_.pptx', '/tmp/ts.pptx', 'wengao-minimal')
p = Presentation('/tmp/ts.pptx'); H = p.slide_height
sizes = [r.font.size.pt for s in p.slides for sh in s.shapes if sh.has_text_frame
         for pa in sh.text_frame.paragraphs for r in pa.runs if r.font.size and r.text.strip()]
off = [(i, sh.shape_id) for i, s in enumerate(p.slides, 1) for sh in s.shapes
       if sh.has_text_frame and sh.text_frame.text.strip() and sh.top + sh.height > H]
print('min', min(sizes), 'max', max(sizes), 'offpage', off)"
```

---

## 7. 重跑样本的方法

```bash
cd ~/WorkBuddy/2026-10-12-00-54-41/wengao-ppt/build/wengao-visual-v13-material-fix
kill $(pgrep -f "server.py --port 8823")
nohup env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy \
  ./.venv/bin/python workbench/server.py --port 8823 >> workbench_server_8823.log 2>&1 &
curl -s --noproxy '*' -X POST -H 'Content-Type: application/json' \
  -d '{"source":"workbench_uploads/00180522__Token_.pptx","theme":"wengao-minimal"}' \
  http://127.0.0.1:8823/api/make
# 轮询结果（约 6–8 分钟）：
curl -s --noproxy '*' http://127.0.0.1:8823/api/make/job-f51a976c5258
# 结果文件：
#   jobs/job-f51a976c5258/findings.json
#   jobs/job-f51a976c5258/layout_gate_runs/<最新>/layout_gate.json
```

注意：

- 工作台启动时必须去掉代理环境变量，原因见 `workbench/server.py` 第 51 行注释；
- 语义配图走云端 Gemini，需要 Clash（127.0.0.1:7897）在线，连接偶发断开会自动重试 3 次。
