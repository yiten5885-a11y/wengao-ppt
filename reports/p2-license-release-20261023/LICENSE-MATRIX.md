# 稳稿 PPT · 许可证矩阵与来源清单（P2）

> 生成时间：`2026-10-24T00:39:54+0800`　生成器：`scripts/p2_build_license_matrix.py`（所有哈希/版本为现场实测）
> 候选基线：`reports/p2-license-release-20261023/BASELINE_SHAS.txt`，成员数 2594，树摘要 `061d65cce833209d70e87de74049eaea5417e3a13423733e6e320c4d231d4334`

**本文件不是法律意见。** 凡标注 `阻断 / 待确认` 的条目必须由项目所有者拍板；代理不代签服务条款、不代做账户/实名操作、不给许可结论。

## 0. 阻断项总表（必须先看完这一段）

| 编号 | 事项 | 为什么阻断发布 |
| :--- | :--- | :--- |
| BLK-01 | 自有代码 LICENSE 未定 |  `LICENSE`（自有代码许可）文件尚不存在，所有者未选定许可证；README/SKILL/SkillHub 元数据里的 `license` 字段因此不能填写。未解决前不得对外发布。 |
| BLK-02 | 五张 AI 生成配图缺少可核验生成证据 | `assets/visual_img/*.png` 的来源仅在 `wengao_ppt/visual_plan.py` 的 `source`字符串里自述为 ImageGen + 时间，没有 provider/model/任务 ID/输入提示词/输出落盘记录可核对；PNG 内不含任何 `tEXt/iTXt/zTXt` 元数据块，也没有 AI 生成标识。按证据基线，这属于**未验证**，不能作为“已署名、已标识”的证据。需所有者确认：补齐生成证据与 AI 标识，或移除/替换这 5 张资产。 |
| BLK-03 | Pexels 示例照片的再分发权未清 | `assets/visual_img/semantic_scenes/` 下 5 张 JPEG 的资产清单自述 `NOT_CLEARED_FOR_SOURCE_BUNDLE`，且 Pexels 服务条款对“整包再分发”是否允许需要所有者/法务确认。已据此在对外副本中默认排除这些文件。 |
| BLK-04 | 上游 vendored 仓库的 remote 指向本机 bundle | `vendor/luobo-ppt/.git` 的 `remote.origin.url` 不是 GitHub URL，而是一台机器的本地 bundle 路径（已脱敏展示）。这使“从 GitHub 重新拉取 upstream”这一步无法被复现，且第三方同步 `vendor/.git` 时会连带暴露本机路径。处理建议：按 AGENTS.md 第 1 节从 GitHub 重新克隆并逐 file 核对 tree sha，或明确不随包分发 `.git`。 |
| BLK-05 | 上游携带的文档图片无逐图权属说明 | `vendor/luobo-ppt/docs/images/` 与 `assets/style_gallery.jpg`（上游自带）未附逐图来源与许可说明，随包分发时的权利依据只能追溯到上游 MIT LICENSE 全文。是否随包分发由所有者决定（默认策略：随 MIT 全文保留，但标记未逐图核实）。 |
| BLK-06 | 依赖版本的许可证标识存在多口径 | 同一依赖在 PyPI 元数据、本机 dist-info 与 requirements.txt 注释里的版本/许可标识并不完全一致（见第三节），以哪个为准应由所有者确认后再写死。 |

> 本矩阵不代替律师/法务意见；凡标注“阻断 / 待确认”的项必须由项目所有者拍板。代理不得代签任何服务条款、不得代做实名/账户操作。
> 未跑或测不到的内容一律写 `NOT_MEASURED`，不口头声称完成，不用依赖列表、`/api/tags`、fixture、回放或自报结果充数。

## 1. 上游厂商代码（vendored upstream）

| 项 | 实测值 | 与 AGENTS.md/契约声明是否一致 |
| :--- | :--- | :--- |
| 检出目录 | `vendor/luobo-ppt` | — |
| HEAD | `b6dcb0f325b3ea38a7ca3cd3c211bbba7fb436b2` | 一致 |
| tree | `ed28a29ddfd5e9666fa0dc9949e2e0658d6786f5` | 一致 |
| 跟踪文件数 | 76 | 契约声明 76 |
| 工作树干净 | True | — |
| 最后提交 | `2026-10-08T19:50:27+08:00 / zhouwei713` | — |
| remote.origin（已脱敏） | `/Users/<REDACTED>/WorkBuddy/2026-10-12-00-54-41/_d01_pkg/wengao-ppt-D01-upstream-fix/luobo-ppt.bundle` | **与声明的 GitHub URL 不一致 → BLK-04** |

### 1.1 上游随文件携带的许可文本（实测 sha256）

| 文件 | sha256 | 字节 | 首几行 | 义务摘要 |
| :--- | :--- | ---: | :--- | :--- |
| `vendor/luobo-ppt/LICENSE` | `39f501dd58369525…` | 1078 | MIT License / Copyright (c) 2026 zhouluobo (周萝卜) | 再分发时必须保留完整许可文本与版权声明 |
| `vendor/luobo-ppt/assets/LICENSE-lucide.txt` | `4225f14fa5babc9c…` | 968 | Icons in this folder are rasterized from Lucide (https://lucide.dev), via react- | 再分发时必须保留完整许可文本与版权声明 |

### 1.2 上游图片资产（无逐图权属说明 → BLK-05）

| 文件 | sha256 | 字节 |
| :--- | :--- | ---: |
| `vendor/luobo-ppt/docs/images/14-styles.jpg` | `f846ab336ad06c3b…` | 467687 |
| `vendor/luobo-ppt/docs/images/article-to-deck.jpg` | `32d449305e1406dc…` | 217525 |
| `vendor/luobo-ppt/docs/images/beautify-before-after.jpg` | `efa5fc6dd0374a5d…` | 81813 |
| `vendor/luobo-ppt/docs/images/equations-kept.jpg` | `8722cf2913dcaf6a…` | 76552 |
| `vendor/luobo-ppt/docs/images/follow-reference-image.jpg` | `da8c2d9f5c8facfd…` | 132379 |
| `vendor/luobo-ppt/docs/images/lite-preview.png` | `822b6fd225da8f3e…` | 393688 |
| `vendor/luobo-ppt/docs/images/logo.png` | `d340c043629865da…` | 106375 |
| `vendor/luobo-ppt/docs/images/math-deck-before-after.jpg` | `329c43299e56fddf…` | 180304 |
| `vendor/luobo-ppt/docs/images/one-deck-three-styles.jpg` | `f7a61e818c61ef27…` | 184505 |
| `vendor/luobo-ppt/docs/images/outline-capacity.png` | `7715b198b0bf3046…` | 240990 |
| `vendor/luobo-ppt/docs/images/qa-fidelity.png` | `3ac7c4c524b387e1…` | 137947 |
| `vendor/luobo-ppt/docs/images/rich-layouts.jpg` | `09ae9df734166c37…` | 197219 |
| `vendor/luobo-ppt/docs/images/screenshot-layouts.jpg` | `7d30336520eef686…` | 101249 |
| `vendor/luobo-ppt/docs/images/self-review.png` | `dc387adb78d9f8f8…` | 228231 |
| `vendor/luobo-ppt/assets/icons.png` | `96606e5c5f2951a4…` | 416176 |
| `vendor/luobo-ppt/assets/style_gallery.jpg` | `c907c6f9bdf68b46…` | 567398 |

## 2. 第三方依赖（运行时/构建时）

**证据口径说明**：同一依赖存在三个口径——① requirements.txt 注释里的“已验证版本”（列出当时用于全链验收的版本）；② 本机受管 Python 环境的真实安装（`importlib.metadata` 实测）；③ PyPI 官方元数据现场拉取（附响应 sha256）。三者不一致时本文件只并列事实，不定结论（→ BLK-06）。

| 依赖 | 候选 .venv 实测版本 | 本机受管环境版本/许可标识 | PyPI latest / SPDX | requirements.txt 注释版本 |
| :--- | :--- | :--- | :--- | :--- |
| `pillow` | 13.0.0 | 12.3.0 / MIT-CMU | 13.0.0 / MIT-CMU | 12.3.0 |
| `lxml` | 6.1.3 | 6.1.3 / BSD-3-Clause | 6.1.3 / — | 6.1.3 |
| `openpyxl` | 3.1.5 | 3.1.5 / MIT | 3.1.5 / — | 3.1.5 |
| `pypdf` | 6.21.2 | 6.20.0 / BSD-3-Clause | 6.21.2 / BSD-3-Clause | 6.20.0 |
| `pypdfium2` | 5.14.0 | 5.14.0 / BSD-3-Clause, Apache-2.0, dependency licenses | 5.14.0 / — | 5.14.0 |
| `python-docx` | 1.2.0 | 1.2.0 / MIT | 1.2.0 / — | 1.2.0 |
| `python-pptx` | 1.0.2 | 1.0.2 / MIT | 1.0.2 / — | 1.0.2 |

PyPI 元数据原始响应（可离线复核，附响应 sha256）：

- `lxml`: `https://pypi.org/pypi/lxml/json` → 最新版本 `6.1.3`，SPDX `未声明 SPDX 表达式（以本机 dist-info 口径为准）`，响应 sha256 `30dda8543e22202d…`
- `openpyxl`: `https://pypi.org/pypi/openpyxl/json` → 最新版本 `3.1.5`，SPDX `未声明 SPDX 表达式（以本机 dist-info 口径为准）`，响应 sha256 `2bbf01d2a1146e87…`
- `pillow`: `https://pypi.org/pypi/pillow/json` → 最新版本 `13.0.0`，SPDX `MIT-CMU`，响应 sha256 `56cd02ba0c8d4441…`
- `pypdf`: `https://pypi.org/pypi/pypdf/json` → 最新版本 `6.21.2`，SPDX `BSD-3-Clause`，响应 sha256 `1f11364f5b5ac69a…`
- `pypdfium2`: `https://pypi.org/pypi/pypdfium2/json` → 最新版本 `5.14.0`，SPDX `未声明 SPDX 表达式（以本机 dist-info 口径为准）`，响应 sha256 `e761a338dfa137de…`
- `python-docx`: `https://pypi.org/pypi/python-docx/json` → 最新版本 `1.2.0`，SPDX `未声明 SPDX 表达式（以本机 dist-info 口径为准）`，响应 sha256 `95eecaefd9734330…`
- `python-pptx`: `https://pypi.org/pypi/python-pptx/json` → 最新版本 `1.0.2`，SPDX `未声明 SPDX 表达式（以本机 dist-info 口径为准）`，响应 sha256 `f1822c66f354c5ec…`

## 3. 图片与其他资产来源清单

### 3.1 AI 生成配图（5 张 · **未验证** → BLK-02）

| 文件 | sha256 | 字节 | 尺寸 | PNG 文本元数据块 | 可核验生成证据 |
| :--- | :--- | ---: | :--- | :--- | :--- |
| `assets/visual_img/aisle_supermarket.png` | `15e43e0cf78e20a2…` | 1998421 | 1536×928 | **无** | **无**（仅 `visual_plan.py` 内自述字符串） |
| `assets/visual_img/desk_planning.png` | `87ec7621a77cf4c7…` | 1510722 | 1536×928 | **无** | **无**（仅 `visual_plan.py` 内自述字符串） |
| `assets/visual_img/factory_night.png` | `ccce4620229037ae…` | 2219196 | 1536×928 | **无** | **无**（仅 `visual_plan.py` 内自述字符串） |
| `assets/visual_img/promo_ecom.png` | `eb64bda6bc462e60…` | 2168653 | 1536×928 | **无** | **无**（仅 `visual_plan.py` 内自述字符串） |
| `assets/visual_img/skyline_city.png` | `3c64d9f1e4a6c82a…` | 1500716 | 1536×928 | **无** | **无**（仅 `visual_plan.py` 内自述字符串） |

事实陈述：这 5 张图目前只有**一处**来源陈述，即 `wengao_ppt/visual_plan.py` `LIBRARY[*]['source']` 里的 `ImageGen 2026-10-19T17:3x …` 文本（该文件 sha256：`0d780177fdb9906c…`，由生成器现场重算）。没有 provider/model、任务 ID、提示词或输出落盘记录可交叉核对；文件自身也不含任何文本元数据块。因此本候选的“AI 生成标识 / 署名”要求目前**无法举证**，不得在发布说明中写成已标识。

另外核实过的一种可能性：按文件 birthtime（`stat` 实测）追溯生成时间也无法成立——这 5 个文件的 birthtime 是候选构建时的复制时间，不是生图时间。这条记录在 `SOURCE_INVENTORY.json` 里没有落下，但可用下列命令复现： `python3 -c "import os;print(os.stat('assets/visual_img/promo_ecom.png').st_birthtime)"`

### 3.2 语义场景照片（Pexels · 再分发权未清 → BLK-03）

| 文件 | 场景 ID | 资产清单 sha256 | 实测 sha256 | 一致 | creator | license | Redistribution |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `consumer-shopping-4971925.jpeg` | `consumer.supermarket_shopping` | `baa246ab6ff9…` | `baa246ab6ff9…` | ✅ | Gustavo Fring | Pexels License | `NOT_CLEARED_FOR_SOURCE_BUNDLE` |
| `consumer-payment-receipt-3570244.jpeg` | `consumer.receipt_settlement` | `3ab2b539e4e1…` | `3ab2b539e4e1…` | ✅ | Hook Tell | Pexels License | `NOT_CLEARED_FOR_SOURCE_BUNDLE` |
| `factory-workers-32048366.jpeg` | `production.factory_workers` | `4cbc50349c48…` | `4cbc50349c48…` | ✅ | EqualStock India | Pexels License | `NOT_CLEARED_FOR_SOURCE_BUNDLE` |
| `production-line-modern-34718930.jpeg` | `production.capacity_line` | `2a075efb88b6…` | `2a075efb88b6…` | ✅ | Yetkin Ağaç | Pexels License | `NOT_CLEARED_FOR_SOURCE_BUNDLE` |
| `delivery-courier-6868460.jpeg` | `delivery.food_courier` | `4f517c92a344…` | `4f517c92a344…` | ✅ | Pexels contributor per page | Pexels License | `NOT_CLEARED_FOR_SOURCE_BUNDLE` |

### 3.3 字体与其他

- `assets/fontconfig/fonts.conf`：sha256 `57f8dfe288dd9fc3…`，3077 字节。作用是把本机已有字体纳入 fontconfig 解析，**不随包分发字体文件**。
- 全仓扫描到的字体文件数量（.ttf/.otf/.ttc/.woff）：**0**，文件列表：[]。这是“不随包分发字体”这一 README 主张的实测依据。

## 4. 自有源码

- `wengao_ppt/` 顶层模块数：**32**
- 模块集合摘要（绝对路径无关，路径+内容哈希聚合）：`4e92c24b8b0afba7c3d7c997c8854a06262199ef26e1b304ef3a92207ab582db`
- `requirements.txt` sha256：`46f664422610b240310771804cb5da43c22234205aa98c6b5dba3e50168d5516`
- 与上游代码的**实测**关系：候选 `wengao_ppt/` 内没有出现 `luobo`/`zhouwei713` 之类的上游版权痕迹；同名文件只有 `render.py`，前 1200 字符相似度约 0.13；`runner.py::vendor_dir()` 只用于 `precheck` 阶段的固定提交核验（`git rev-parse HEAD` 比对），没有 import/exec 上游脚本。这**不**构成“是否衍生作品”的法律结论，定性留待所有者/法务。

## 5. 外部调用记录（可复核，无付费模型调用）

| 时间 | 目标 | 用途 | 是否付费/是否需要密钥 | 结果 |
| :--- | :--- | :--- | :--- | :--- |
| 2026-10-24T00:39:54+0800 | PyPI JSON API（7 项依赖元数据） | 取版本与 SPDX 标识 | 免费公网匿名读、无密钥 | 7/7 成功，原始响应落在 `pypi_evidence/`；失败项（如有）在 SOURCE_INVENTORY.json 里带 `error` 字段 |

