# 第三方许可与来源声明（THIRD-PARTY NOTICES）

> **这不是法律意见。** 本文件只陈述**事实与义务清单**；凡涉及许可证选择、服务条款解释、衍生作品定性、再分发是否允许，一律由**项目所有者/法务**确认。代理不代签条款、不代办账户与实名操作。

完整按版本与 SHA 核对的清单见：`reports/p2-license-release-20261023/LICENSE-MATRIX.md`
（机读版：`SOURCE_INVENTORY.json`；合成副本迭代：`BASELINE_SHAS.txt`）。

---

## A. 自有代码

| 项 | 值 |
| :--- | :--- |
| 名称 | 稳稿 PPT（WenGao PPT） |
| 版本 | `0.1.0`（`wengao_ppt/__init__.py`） |
| 许可证 | **MIT**（项目所有者于 2026-11-04 亲自选定；仓库根已有 `LICENSE` 文件。版权行可由所有者改署法定名称，不影响许可效力） |
| 发布状态 | 许可证已确定。GitHub / SkillHub 上架仍须所有者明确授权推送后执行（授权未给出，未上架） |

---

## B. 上游代码：luobo-ppt（MIT）

| 项 | 值 |
| :--- | :--- |
| 来源 | `https://github.com/zhouwei713/luobo-ppt.git`（契约声明值，见 `contracts/upstream_baseline.json`） |
| 固定提交 | `b6dcb0f325b3ea38a7ca3cd3c211bbba7fb436b2`（本机 `git rev-parse HEAD` 实测一致） |
| 固定树 | `ed28a29ddfd5e9666fa0dc9949e2e0658d6786f5`（实测一致） |
| 跟踪文件数 | 76（实测一致） |
| 检出目录 | `vendor/luobo-ppt` |
| 许可证 | **MIT**，版权所有 (c) 2026 zhouluobo（周萝卜） |
| 许可文件 | `vendor/luobo-ppt/LICENSE`（随发行包保留全文） |

**MIT 义务（按上游许可文本）**：所有副本或实质部分中必须包含上述版权声明与许可声明。

**声明中的偏差（须知情）**：本机 `vendor/luobo-ppt/.git` 的 `remote.origin.url` **不是** GitHub 地址，而是一条本机 bundle 路径（已在 `LICENSE-MATRIX.md` 中脱敏展示）。这意味着第三方无法按该 remote 复现上游获取过程。处置选项（由所有者决定）：按 `AGENTS.md` 第 1 节从 GitHub 重新克隆并逐文件核对树哈希，或明确不随包分发 `vendor/luobo-ppt/.git`。

**是否衍生作品**：候选 `wengao_ppt/` 代码中未发现 `luobo` / `zhouwei713` 之类的上游版权痕迹；同一命名文件仅 `render.py`（前 1200 字符相似度约 0.13）；运行期只把 `vendor/luobo-ppt` 用于 `precheck` 的固定提交核验（`git rev-parse HEAD` 比对），没有 import 或执行上游脚本。**是否构成法律意义上的衍生作品不由本文件判断**，需法务定性。

---

## C. 图标：Lucide Icons（ISC）

| 项 | 值 |
| :--- | :--- |
| 许可文件 | `vendor/luobo-ppt/assets/LICENSE-lucide.txt`（注意：早期文档写作 `vendor/luobo-ppt/LICENSE-lucide.txt`，路径有误，已更正） |
| 许可证 | ISC |
| 版权 | 其中一部分源自 Cole Bemis 2013–2022 的 Feather（MIT），其余由 Lucide Contributors 2022 持有 |
| 义务 | 随图库保留 ISC 版权声明 |

---

## D. Python 运行时依赖

安装时由 pip 拉取（不随包分发）：`Pillow`、`lxml`、`openpyxl`、`pypdf`、`pypdfium2`、`python-docx`、`python-pptx`。
版本与 SPDX 标识的三口径对照（requirements 注释 / 本机 dist-info / PyPI 最新）见 `LICENSE-MATRIX.md` 第 2 节，此处不重复、不自行取舍。

已知需要留意的点：

- **pypdfium2** 的 dist-info 申报 `BSD-3-Clause, Apache-2.0, dependency licenses`，其 wheel 内含 PDFium 及其构建依赖（freetype、libjpeg-turbo、libpng、zlib、ICU、lcms、openjpeg、abseil 等）的许可文件。本项目不分发这些 wheel，但运行依赖于此，使用者在企业内分发时按自身合规要求处理。
- **Pillow** 当前 PyPI SPDX 为 `MIT-CMU`；此前项目文档写的 HPND 口径已过时，以 `LICENSE-MATRIX.md` 实测为准。

---

## E. 图像资产

### E.1 AI 生成配图（5 张 · **未清**）

文件：`assets/visual_img/{promo_ecom,factory_night,aisle_supermarket,desk_planning,skyline_city}.png`

- 现有来源陈述：仅 `wengao_ppt/visual_plan.py` 中 `LIBRARY[*]['source']` 的字符串（自称 `ImageGen 2026-10-19T17:3x …`）。
- 缺失：provider / model / 任务 ID / 输入提示词 / 输出落盘记录；文件内部无 `tEXt/iTXt/zTXt` 元数据块，也无 AI 生成标识。
- 结论：**不能认定已完成 AI 署名与 AI 生成标识**。处置（补齐证据并标注，或替换/移除）需所有者决定。对外副本默认已排除这 5 张（见副本内 `ASSET-EXCLUSIONS.md`）。

### E.2 Pexels 示例照片（5 张 · **未清**）

文件：`assets/visual_img/semantic_scenes/*.jpeg`，清单 `semantic_scene_assets.json` 自述 `NOT_CLEARED_FOR_SOURCE_BUNDLE`。

- 每张记录了场景 ID、创作者、来源页面、许可证 URL 与 sha256（本轮已逐张重算比对，全部一致）。
- 未清事项：Pexels 服务条款下"是否允许整包再分发"需所有者/法务确认。
- 已采取措施：对外副本默认排除；可被用户自有素材目录（`WENGAO_ASSETS_ROOT`）或经授权的检索链路替代。

### E.3 上游自带图片

`vendor/luobo-ppt/docs/images/*` 与 `vendor/luobo-ppt/assets/style_gallery.jpg` 由上游携带，**无逐图来源与许可说明**，权利依据只能追溯到上游 MIT 全文。是否随包分发的决定权在所有者；本轮默认策略是随 MIT 全文保留并标记为"未逐图核实"。

---

## F. 字体

仓库与发行包**不包含任何字体文件**（全仓扫描 `.ttf/.otf/.ttc/.woff*` = 0 个文件，见 `LICENSE-MATRIX.md` 第 3.3 节）。
包内仅提供 `assets/fontconfig/fonts.conf`，用于把**使用者本机已安装的字体**纳入 fontconfig 解析。中文能否正常渲染取决于使用者本机是否安装中文字体（如思源系列、苹方、微软雅黑等），字体授权由使用者自行负责。

---

## G. 面向最终用户的义务清单（发布前必须全部落到README/包内）

1. 保留 `vendor/luobo-ppt/LICENSE`（MIT 全文）与 `vendor/luobo-ppt/assets/LICENSE-lucide.txt`（ISC 全文）。
2. 保留本文件 `THIRD-PARTY-NOTICES.md`，以及 `LICENSE-MATRIX.md`（含按版本/SHA 的来源清单）。
3. 若对外副本包含任何未清资产，必须在副本显著位置说明其来源、许可与限制（见 `ASSET-EXCLUSIONS.md` 的做法）。
4. 自有代码 `LICENSE` 由所有者添加后再发布；在此之前 Release 只能标记为 `DEVELOPMENT_CANDIDATE`。
