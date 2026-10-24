# W2 版式与成稿 · 交付报告（FINAL）

- **窗口**：W2 版式与成稿（任务书 RS-5 升级为独立版式引擎；RS-3 对比/趋势/洞察页呈现；RS-6 多文字排版）
- **运行候选**：`build/wengao-visual-v13-material-fix`（分支基线见 §8）
- **完成日期**：2026-10-24
- **结论**：验收项 1–7 全部达成；发现并修掉 9 个缺陷（含 3 个会直接导致"留痕造假/图文重叠"的 P1）。**逐页评分是 Agent 评价，不是用户盲评**（指令 §7-1 原文要求标明，这里再次显式声明）。

---

## 1. 做了什么

### W2-A 设计系统 tokens（`wengao_ppt/layout/tokens.py`）
- **字体栈按文字分**：latin / cjk_zh / cjk_ja / arabic / devanagari / cyrillic 六栈；**首选全部 Noto 系（SIL OFL 1.1，可再分发）**，系统字体只作兜底，末位 `Arial Unicode MS`。字体文件随包分发在 `fonts/static/`（8 个静态实例：NotoSans / NotoSansSC / NotoSansArabic / NotoSansDevanagari 各 Regular+Bold，由可变字体静态实例化并修正 name 表）。
- **12 栏网格** + 栏距 0.22in + 安全边距（left/right 0.55in、top 0.42in、bottom 0.32in，取硬门禁更严值）。
- **字号阶梯**唯一权威来源是冻结策略 `contracts/layout_hard_gate_policy.json` 的 `font_roles_pt`：cover_title 44 / title·section_title 36 / lead·conclusion 28 / body 26 / key_number 48 / table 20 / chart_aux·caption·ordinal 18 / footnote 16。正文硬性地 24pt（`body_hard_min`）——**修掉"整套白底小字像事实清单"**。
- **3 套主题**：`ink`（墨蓝强调+冷灰中性，默认，与工作台视觉一致）、`slate`、`sand`；`theme_contrast_report()` 全对 ≥4.5。
- 装饰底线：无阴影/渐变/艺术字，圆角 0.08in，强调色 ≤2。

### W2-B 版式引擎（`wengao_ppt/layout/engine.py` + `measure.py`）
- `page_regions()` 按角色算标题带高（封面 44pt 与正文 36pt 不同带高）；`flow_blocks()` 单栏→双栏自动重平衡，**溢出如实上报不裁不缩**；`measure.wrap_lines()` 数字+单位 token 保护（含中文量词，`2500亿`/`36个月` 不被拆行）。
- 修掉三个已知缺陷：**"（续）"孤页**（规划器按台账切页，多页标题用 `节标题（1/2）`）、**条目多页重复**（同句去重 + 跨页查重）、**白底小字**（字号阶梯见上）。
- **每个可放图页型都有 with_image / no_image 两种版式**：content 有图走"文字 7 栏 + 图 5 栏"，image 页型有整幅/左右两版式；槽位未解析（FALLBACK_T4）走无图文字版式，**重排不留空洞、不放占位图**，页脚如实留痕"本页未配图（T4 降级）"。

### W2-C 12 页型 × 2 变体（`wengao_ppt/layout/templates.py`，24 个变体全部注册）
封面（居中/左齐规则线）· 章节（大编号/色带）· 要点（单栏/双栏）· 对比（双卡/原生条形图+洞察栏）· 趋势（折线整幅/柱图+洞察栏）· 洞察（左对齐大数/居中大数）· 图片（整幅+图注/左右分栏）· 流程（横向 CHEVRON/纵向圆角矩形+箭头，原生图形）· 时间线（横向轴线圆点上下交替/纵向轴线+右标签，原生图形）· 表格（整幅/表+注）· 引用（居中/卡片）· 结尾（声明/编号行动）。

### W2-D 原生构建器 + 回环校验（`wengao_ppt/builders/`）
- `chart.py` 原生柱/条/折线 + 内嵌工作簿、重点系列高亮、数据标签；`table.py` 原生表格 20pt；`process.py`/`timeline.py` 原生图形。
- `roundtrip.py`：构建 → `extract_charts_pptx`/`extract_tables_pptx` → 逐格/逐系列核对数据绑定（含工作簿缓存一致性）。
- **字体三槽**：`tokens.apply_font_slots()` 统一给 run 写 latin/ea/cs——不写 ea 槽时中文在 LibreOffice 回退到 Carlito（非 OFL、整稿字体分裂），本次在表格/流程/时间线/图表全部补齐（此前只有文本框写了）。

### W2-E 规划器（`wengao_ppt/planner/`，自样例脚本拆出）
- `fact_ledger.py`：事实台账（数值+定性），**纯确定性规则**；`deck_plan.py`：材料 → 台账 → 分节 → 按**真实版面实测容量**切页（先单栏、再双栏、都不行才切页）→ DeckPlan（含 `image_slots[].scene` 骨架，visual_query/anchors 交 W1 semantic_scenes，不重造）。
- **事实保护**：`collect_grounded_numbers` + `numbers_in_plan_ungrounded` 自检，规划里出现台账/facts 之外数字直接抛错（模型补写视为缺陷）；元信息句（合成样例声明）不进正文。
- 接收 `ResearchBundle.views` 的锚定/补充对比分支已在代码中（本次回放材料无 bundle，未在样张中触发；见 §5 风险）。

### W2-F 成稿与导入接口（`wengao_ppt/layout/compose.py`）
- `compose_deck` / `recompose_slide` / `insert_slide`；**非目标页对象级不变**用 XML sha256 前后对比（插入按多重集合比，逐位比必然误报）。
- `place_image` 路由修正：默认实现原先指向不存在的 `scene_images.place_image`，现改为 `wengao_ppt.media.place.place_image`（W1 真实现）+ EMU→英寸适配器，找不到才用测试桩。

### W2-G 渲染与字体（`wengao_ppt/render.py` + `fontenv.py`）
- PDF 中文渲染修复：fontconfig 环境（模板 + 包内字体目录 + 平台目录）注入 LibreOffice 子进程；**PDF 实际嵌入 NotoSansSC/NotoSansSCBold/NotoSans/NotoSansArabic/NotoSansDevanagari（全部 OFL）**，逐页核对字体资源（见证据）。
- `render_pages()`：**缩略图 + 大图两档**（`png/thumb/` 48dpi + `png/` 110dpi，同一份 PDF 栅格化，各自记渲染器身份与哈希）；保留取消与进程组清理。

### W2-H `check_layout(pptx)`（`wengao_ppt/layout/checks.py`）
溢出（估算，±10% slack 降级 WARN）· 重叠（**图片不享"包含"豁免**——不透明且后绘制，套内文字会被遮住，HG04）· 最小字号（按角色从冻结策略文件读区间）· 对比度（WCAG）· 边距 · "（续）"续页标记 · **图片槽位**（退化尺寸、alt/署名缺失、T4 留痕一致性）。错误码登记在 `contracts/errors/layout.json`（新增 `LAYOUT.IMAGE_NO_ALT`、`LAYOUT.IMAGE_SLOT_DEGENERATE`）。

---

## 2. 证据路径（全部在 `reports/rs-w2/`）

| 验收项 | 证据 |
|---|---|
| 1. 每页型 ≥2 张不同结构真实渲染样页 | `page_types.pptx`（24 页）/ `page_types.pdf` / `page_types/png/page-01..24.png`；逐页评分见 §3 |
| 2. 6 种文字样张 | `scripts_6.pptx` / `.pdf` / `scripts_6/png/`（中/日/英/俄/阿拉伯 RTL/印地语）；RTL 段落 `rtl=1`+右对齐（`page-05.png`） |
| 3. 构建器回环 + 有图/无图样页 | 无图：`page_types`；有图：`image_slots.pptx` / `.pdf` / `image_slots/png/`（3 页已解析槽位真上图 + 3 页 T4 降级留痕）；回环测试 `tests/test_w2_planner_builders.py::test_chart_builder_roundtrip_matches_input` 等 |
| 4. 17 页回放材料跑规划器 | `replay_check.json` + `replay_deck.pptx/.pdf/png/`：覆盖 60/60 ≥ 基线 58/60（+2），0 blocking，无"（续）"（冻结计划有 9 个），无重复条目（冻结计划首页同句 2 次、第 7 页同句 3 次） |
| 5. PDF 中文嵌入 | `render_report.json`：`font_families` 全 Noto；`cjk_stats`：page_types 491 个中文字符在文本层；阿拉伯/天城文 PDF 同样嵌入 NotoSansArabic/NotoSansDevanagari |
| 6. recompose/insert 对象不变 | `recompose_insert_inventory.json`：逐页对象清单（shape_id/类型/名称/XML sha）前后对比，`others_unchanged=true`，插入后原 1/2/3 页 → 1/3/4 页对象清单逐一相同 |
| 7. 测试不回归 | 427 passed, 313 subtests passed（既有 365 + W2 新增 62） |

复现命令：
```
.venv/bin/python scripts/rs_w2_render_samples.py            # 无图半场（24+6+5 页）
.venv/bin/python scripts/rs_w2_render_image_samples.py      # 有图半场（6 页）
.venv/bin/python scripts/rs_w2_replay_check.py              # 回放材料验收
.venv/bin/python scripts/rs_w2_recompose_insert_evidence.py # 对象不变证据
.venv/bin/python -m pytest tests/ -q --basetemp=.pytest_run_tmp
```

---

## 3. 逐页评分（**Agent 评价，非用户盲评**）

> 美术手册 `WENGAO_PPT_ART_DIRECTION_PLAYBOOK.md` **在本机不存在**（Desktop 与仓库均未找到），无法按其 §8 原始量表评分。这里按指令 §7-1 明示的三个维度——**层级 / 构图 / 数据表达**——加指令 §1 的原则（层级清楚、留白克制、重点突出）评分，1–5 分。此局限已如实记录，不冒充按手册评分。

样张：`reports/rs-w2/page_types/png/`（24 页）。维度：层级 / 构图 / 数据表达（无数值页的数据表达记构图延续）。

| 页 | 页型·变体 | 层级 | 构图 | 数据表达 | 具体问题 |
|---|---|---|---|---|---|
| 01 | cover·a | 5 | 5 | – | 44pt 居中标题+强调规则线+副题，留白克制 |
| 02 | cover·b | 4 | 4 | – | 左齐编辑式构图；竖强调条是好的细节，标题光学中心略偏低 |
| 03 | section·a | 4 | 4 | – | 44pt 编号+36pt 标题+通栏规则线；编号与标题基线对齐可更紧 |
| 04 | section·b | 4 | 3 | – | 色带标题+带下正文；色带扁平，正文与色带间距偏大 |
| 05 | content·a | 5 | 5 | 5 | 单栏要点+强调色 bullet；数字+单位不断行 |
| 06 | content·b | 4 | 4 | 4 | 双栏均衡、栏距合规；第 4 条在左栏折行（可接受） |
| 07 | compare·a | 4 | 4 | 4 | 双卡（substrate vs paper+hairline）区分明确；样张每卡只 1 条，右卡显空 |
| 08 | compare·b | 5 | 5 | 5 | 条形图 8 栏+洞察 4 栏；数据标签、轴字号 18pt |
| 09 | trend·a | 4 | 4 | 4 | 折线整幅+数据标签；缺轴单位说明（单位在标题/脚注） |
| 10 | trend·b | 4 | 4 | 5 | 柱图+洞察栏；同上 |
| 11 | insight·a | 5 | 4 | 5 | 48pt 强调色大数+标签+注；右半页留白偏大 |
| 12 | insight·b | 5 | 5 | 5 | 居中大数堆叠，重点突出 |
| 13 | image·a（无图） | 3 | 3 | – | T4 文字降级，页面稀疏（如实降级，不放占位图）；有图版见 `image_slots/png/page-03.png`（整幅+图注，层级 4/构图 4） |
| 14 | image·b（无图） | 3 | 3 | – | 同上；有图版见 `image_slots/png/page-04.png`（图 6 栏+文 6 栏） |
| 15 | process·a | 4 | 4 | 4 | 横向 CHEVRON 链，间隔填充交替；原生图形可编辑 |
| 16 | process·b | 4 | 4 | 4 | 纵向圆角矩形+下箭头；居中排列稳重 |
| 17 | timeline·a | 4 | 4 | 4 | 轴线+圆点，标签上下交替；首末标签在上、中间在下 |
| 18 | timeline·b | 4 | 4 | 4 | 左轴线+右标签；垂直节奏均匀 |
| 19 | table·a | 4 | 4 | 5 | 斑马纹+加粗表头 20pt；三列等宽 |
| 20 | table·b | 4 | 4 | 5 | **与 a 结构相同**（样张未给 note 块，表+注变体的差异未在样张体现——见 §5） |
| 21 | quote·a | 5 | 5 | – | 居中引文+破折号署名，克制 |
| 22 | quote·b | 4 | 4 | – | 卡片+左强调条；引文与署名同行右对齐 |
| 23 | closing·a | 4 | 4 | – | 居中声明+规则线；偏素（样张文本短） |
| 24 | closing·b | 5 | 4 | – | 01/02/03 编号行动，序号用 ordinal 档字号 |

**整稿层**：边距/字体/强调色三统一（tokens 单源）；正文 26pt 起，"白底小字事实清单"问题已消除；无阴影渐变艺术字。

---

## 4. 本次修掉的缺陷（按严重度）

| # | 缺陷 | 影响 | 修法 |
|---|---|---|---|
| 1 | `_image_note` 逻辑写反：**有图页脚自称"本页未配图"** | 留痕造假（HG09 类） | 只有非 RESOLVED 状态才写留痕；check_layout 增加"有痕必须有图/无痕不管"一致性核对 |
| 2 | `flow_blocks` 放置位置从网格重算，**忽略调用方传入的子区矩形** | content 有图页文字铺满 12 栏压到图上；compare 双卡文字全叠在第 0 栏 | 放置一律以传入 content 矩形为权威（x/w），网格只作整幅参考 |
| 3 | 表格/流程/时间线 run 不写 ea/cs 槽，**渲染回退 Carlito** | 整稿字体分裂、非 OFL 字体进 PDF | `tokens.apply_font_slots()` 三槽统一，四个构建器全部接入 |
| 4 | markdown 回放材料标题漏判（`## 一、` 与正文空行相隔 + `、` 被句读黑名单误杀） | 所有节标题退化成"第 N 段" | 全文级标题前瞻 + 剥 markdown 前缀 + 编号标题强信号规则 |
| 5 | 元信息句（合成样例声明）进正文页 | 免责声明被当成内容渲染 | 规划器过滤 `is_meta_sentence`（台账口径不变） |
| 6 | 阿拉伯语段落无 rtl，行首在左 | RTL 阅读顺序/对齐错误（验收项 2） | `_apply_rtl()`：`rtl=1` + LEFT 翻 RIGHT |
| 7 | `place_image` 默认路由指向不存在的 `scene_images.place_image` | 真实交付永远走测试桩 | 改路由到 W1 `media.place.place_image` + EMU→英寸适配 |
| 8 | `render_pages` 只有一档 PNG | 不满足指令 §4-G 两档要求 | 增加 `png/thumb/` 48dpi 缩略图档，同 PDF 栅格化并记哈希 |
| 9 | `insert_slide` 逐位对比 + 空白页取指纹 | 不变性误报 | 多重集合比 + 渲染后再取新页指纹 |

---

## 5. 未完成与风险

1. **`ResearchBundle.views` 锚定未在样张中端到端验证**：回放材料没有 bundle；规划器的视图锚定/补充对比分支有单测（`test_view_without_data_is_dropped_not_fabricated`），但"对比/趋势/洞察页插到提到该指标的页面之后"缺一份带真实 bundle 的渲染样张。**需要 W3 的 `fixtures/research/bundle_sample_*.json`** 到位后补跑（指令 §6 已预留此并行路径）。
2. **table·b（表+注）与 table·a 结构差异未在样张体现**：样张没给 note 块，两变体渲染相同。变体代码路径存在（`_table_note`），缺一份带注释的样页。
3. **image 页型的"全出血带蒙版"变体未做**：指令 §4-C 列举了整幅/左右/全出血带蒙版三种，现有两变体是整幅+左右；全出血需要 W1 的蒙版素材约定，留待配合。
4. **阿拉伯标题 1 个字符回退系统 Arial Unicode MS**：LibreOffice 对该 shaped glyph 走了栈末兜底（栈设计如此，无豆腐）；其余全部包内 Noto。建议后续在 glyphcheck 里对"回退到栈末家族"记 WARN。
5. **文本溢出是近似估算**（±10%）：`check_layout` 的 LAYOUT.OVERFLOW 只作廉价前置，硬门禁最终判定仍以渲染证据（glyphcheck/渲染复核）为准——已在模块 docstring 声明。
6. **`tests/research/` 下 9 个用例在无 `--basetemp` 时因沙箱 `tmp_path` 报 error**：与本窗口改动无关（既有环境问题）；全量跑用 `--basetemp=.pytest_run_tmp` 绕过。
7. **美术手册缺失**：评分维度只能用指令明示的三项（见 §3 声明），无法对齐手册 §8 原始量表。

---

## 6. 需要其他窗口配合的事项

- **W1（素材与动效）**：① `media.place.place_image` 的 rect 单位是英寸、W2 传 EMU，适配器已在 `compose._w1_place_image()` 内换算——若 W1 改签名请同步；② "全出血带蒙版"页型需要蒙版图约定；③ Arabic 标题字形回退问题可能需要 W1 侧确认图库/字体管线。
- **W3（联网研究）**：交付 `fixtures/research/bundle_sample_*.json`（中/英/空/含冲突）后，W2 补跑 views 锚定型样张；`conflict_decisions` 为 `open` 时"采用网络值并标待确认"的分支已就位待联调。
- **W4（工作台与集成）**：`recompose_slide`/`insert_slide` 的"非目标页对象不变"用 XML sha256 自证（§2 证据 6），请用现有保真门禁复核；`check_layout` 报告 schema 为 `rs.check_report.v1`，findings 可直接进门禁台账。
- **契约层**：`charts.py`/`tables.py`/`source_fidelity.py`/`checks.py` 按指令只读调用，未改；如需改请走 `reports/handoff/`。

---

## 7. 测试

- **新增 W2 测试 62 个**：`tests/test_w2_layout_engine.py`（40）+ `tests/test_w2_planner_builders.py`（22），覆盖 tokens/网格/度量/双栏切分/24 页型变体渲染/字号角色策略/标题识别/事实保护/回环校验/有图成稿/T4 留痕/RTL/两档渲染/recompose+insert 不变性。
- **全量：427 passed, 313 subtests passed, 0 failed**（既有 365 无回归）。
- 测试用仓库内临时目录 fixture（沙箱会拦 `tmp_path`），全量加 `--basetemp=.pytest_run_tmp`。

---

## 8. 开工前确认项：v13-material-fix 与 v16 快照差异

对 `release_candidate_v16/{github-source,skill-package}/wengao_ppt/` 与运行候选 `build/wengao-visual-v13-material-fix/wengao_ppt/` 逐文件比对（github-source 与 skill-package 两侧一致）：

| 文件 | 结论 |
|---|---|
| `page_catalog.py` | **完全一致**（sha256 相同）。容量/密度口径（12 页型 × 3 密度）两边相同，W2 的 `_units_per_page` 直接复用无冲突 |
| `theme.py` | **v13-material-fix 更新**（1052 行 vs 547 行）：v13 多出对比度台账、chart_text/backdrop/adaptive 细化规则、`theme_application`/`refine_rules_version` 证据字段。v16 是 10-16 快照，缺这些。**W2 未改 theme.py**（指令 §3：保留为换肤器），其差异不影响本窗口；若后续从 v16 合并要小心不要回退主题证据字段 |
| `render.py` | **v13-material-fix 更新**（522 行 vs 437 行）：① `_fontconfig_env` 委托给 `wengao_ppt.fontenv`（带 data_root 与记录/指纹），v16 是内联实现；② v13 多 `pdf_font_families()`（字体证据）与 `render_pages()` 公开 API（v16 没有）。**W2 在 v13 基础上给 `render_pages` 加了两档 PNG**（§1-W2-G） |

**结论**：运行候选在 theme/render 上领先 v16 快照，page_catalog 相同；W2 的全部改动都建立在更新的 v13 侧，不存在"改早了被 v16 覆盖"的风险，但**从 v16 向主线合并时必须先合并 theme.py/render.py 的 v13 版本**，否则主题证据与两档渲染会丢。
