# 稳稿 PPT（WenGao PPT）

稳稿 PPT 是一款遵循**本地优先（Local-First）**与**确定性契约（Deterministic Contract）**原则的高质量 PPTX 自动化生成与智能美化引擎。

> **核心设计哲学**：所有评估结论必须由独立验收器对磁盘落盘产物（PPTX / PDF / 渲染像素 / 字形轮廓）进行物理重读验证，**绝不采信生成器自报**；未通过硬性质量门禁（Hard Gates）的产物绝不静默放行或自动标记就绪。

---

## 0. 先读这一段：当前状态与信任边界

| 项 | 值 | 依据 |
| :--- | :--- | :--- |
| 交付状态 | `DEVELOPMENT_CANDIDATE`，`release_ready=false` | 本仓现阶段不得标记为就绪；入口不得绕过运行器自行签发 READY |
| 自有代码版本 | `0.1.0` | `wengao_ppt/__init__.py` 的 `__version__`，实测 `python -m wengao_ppt.cli --version` → `0.1.0` |
| 是否已公开发布 | **否**。未发布到 GitHub / SkillHub | 发布前清单见 `SKILL.md` 末节 |
| 许可证（自有代码） | **待所有者选定**，暂无 `LICENSE` 文件 | 见 `THIRD-PARTY-NOTICES.md` 与 `reports/p2-license-release-20261023/LICENSE-MATRIX.md`（BLK-01） |

**本文档中命令的可信等级**（不得自行抬级）：

| 标记 | 含义 |
| :--- | :--- |
| `【已实测】` | 本文档作者在本候选机上真实执行过，输出见 P2 交付报告 |
| `【草案·未验证】` | 尚未在干净环境做安装验证，只是按代码与设计写出的用法，不得当作已验证步骤 |

真实端到端能力的现状（不夸大）：

- **26 语言真实工作台链路：0/26**。本机既无可用本机模型也无 LibreOffice，历史真实任务 `job-167a1306f549` 为 `FAILED` / `semantic_scenes` / `VISUAL.SCENE_UNRESOLVED`。离线语言识别与 26 份规划器输入样例已过单测，**不等于**真实链路可用。
- **50 套真实生成：0/50**。
- 已知未完成项清单见 `reports/` 下各轮报告，以及本文件第 10 节「功能与限制」。

---

## 1. 快速开始

> 除标注 `【已实测】` 的命令外，以下均为 `【草案·未验证】`：**尚未在干净环境完成一次完整安装验证**，请按实际环境核对后再对外发布。
> 端口约定：请自行选择未被占用的本机端口。**不得占用 8821**（那是另一个正式入口，与本项目无关，不得切换或覆盖）。

```bash
# 1. 安装核心 Python 依赖（推荐 Python 3.10+）【草案·未验证】
pip install -r requirements.txt

# 2. 安装并激活受控版本（程序与用户数据分离，写入隔离版本目录）【草案·未验证】
python -m wengao_ppt.install install --package . --activate

# 3. 启动本地工作台（仅监听本机回环 127.0.0.1，不对外暴露网络）【草案·未验证】
<安装根>/bin/wengao start --port 8823

# 4. 浏览器访问工作台交互界面
# 打开 http://127.0.0.1:8823/
```

仅查询入口可用性（**已实测**，本机候选自带 venv，Python 3.13）：

```bash
cd <候选目录>
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m wengao_ppt.cli --help     # exit 0
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m wengao_ppt.cli --version  # 0.1.0
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m wengao_ppt.install --help # exit 0
```

### 1.1 命令行（CLI）直接调用

稳稿 PPT 提供统一且唯一的命令行入口，所有操作均通过标准 CLI 参数执行（`--help` 实测输出的子命令：**from-material / edit / edit-list / edit-restore / precheck / run / cancel / submit / publish / validate / export / status(job-status) / incident-record / incident-list / cleanup**）：

```bash
# 流程 A：美化已有 PPTX【草案·未验证】
python -m wengao_ppt.cli precheck --input presentation.pptx --mode beautify --theme wengao-basic
python -m wengao_ppt.cli run --job-id <job-id> --timeout 900
python -m wengao_ppt.cli validate --job-id <job-id>
python -m wengao_ppt.cli export --job-id <job-id> --outdir ./output

# 流程 B：从原始材料直接生成演示文稿【草案·未验证】
python -m wengao_ppt.cli from-material \
  --input strategy_report.md \
  --pages 12 \
  --model qwen3.8:27b-wb65536 \
  --theme wengao-basic \
  --accept
```

---

## 2. 系统与依赖

| 依赖组件 | 推荐版本 / 要求 | 作用与说明 |
| :--- | :--- | :--- |
| **操作系统** | macOS（Apple Silicon M 系列推荐） / Linux / Windows WSL2 | macOS 环境可直接利用 Metal 硬件加速运行本地大模型 |
| **Python** | 3.10 — 3.13 | 核心开发与运行环境（本候选自带 venv 为 3.13） |
| **LibreOffice** | 7.6+ / 24.x | 无头真实渲染（Headless Rendering）、PDF 生成及字形轮廓核验 |
| **本机推理运行时** | llama-server（llama.cpp）b10500+ 或 Ollama | 加载本地 GGUF 权重并提供本机 HTTP 推理接口 |
| **Fontconfig** | 2.13+ | 统一跨平台字体解析；包内含 `assets/fontconfig/fonts.conf`，**不随包分发字体文件** |

Python 第三方依赖（`requirements.txt`，由源码真实 import 生成）：`Pillow`、`lxml`、`openpyxl`、`pypdf`、`pypdfium2`、`python-docx`、`python-pptx`。

> **版本锁定口径**：`requirements.txt` 注释里的“已验证版本”是某一轮全链验收当时实际使用的版本（macOS / Python 3.13.12 / 2026-10-20）；本机受管环境的真实安装版本与之存在差异（例如本候选自带 venv 实测为 Pillow 13.0.0、pypdf 6.21.2）。三种口径（requirements 注释 / 本机安装 / PyPI 最新）已并列记录在 `LICENSE-MATRIX.md` 第 2 节，**需要安装请先自行在目标环境验证**，不要跨平台套用。

---

## 3. 安装 / 启动 / 升级 / 回滚

安装入口的全部子命令（`python -m wengao_ppt.install --help` **已实测**）：
`install / upgrade / rollback / status / versions / start / launcher / cli / uninstall`。

```bash
# 安装一个包目录【草案·未验证】
python -m wengao_ppt.install install --package <包目录> [--label L] [--activate]

# 升级（先自测通过才切指针）【草案·未验证】
python -m wengao_ppt.install upgrade --package <新包目录> [--label L]

# 回退 【草案·未验证】
python -m wengao_ppt.install rollback [--to L]

# 卸载：默认只预演；--apply 也只把程序目录移入 quarantine/，用户数据原地保留【草案·未验证】
python -m wengao_ppt.install uninstall [--apply]
```

安装布局（程序与数据分离）：

- 安装根默认 `~/.wengao-ppt`（`--root` 或环境变量 `WENGAO_HOME` 可改）；
- 程序在 `versions/<label>/`，配置与任务在 `data/`（可通过 `WENGAO_DATA_ROOT` 指定）；
- 启动器端口可由 `WENGAO_PORT` 覆盖（默认 8791）；
- macOS 也可双击 `<安装根>/启动稳稿PPT.command` 启动（端口已占用时只提示地址，不重复启动）。

**安装排雷规则**（已实现且已由专门测试覆盖）：安装复制会排除 `jobs/`、`reports/`、`material_tasks/`、`wb_loopback_data/`、`workbench_previews/`、`workbench_uploads/`、`.wengao_scene_assets/`、`.venv/`、`.git/`、`__pycache__/`、各类日志与临时文件；复制后若仍出现这些成员会**失败关闭**，不留“半脏”安装。

---

## 4. 模型配置 / 费用 / 数据去向

### 4.1 怎么配

| 环境变量 | 默认值 / 行为 | 用途 |
| :--- | :--- | :--- |
| `WENGAO_LOCAL_MODEL_ENDPOINT` | 未设置时依次探测 `http://127.0.0.1:18888`、`http://127.0.0.1:11434`，都不通时退回 `http://127.0.0.1:18888` | 本机推理端点 |
| `WENGAO_FALLBACK_MODEL_ENDPOINT` | `http://127.0.0.1:18888` | 主端点失败后的本机回退端点 |
| `WENGAO_HOME` | `~/.wengao-ppt` | 安装根 |
| `WENGAO_DATA_ROOT` | 缺省时退回包目录 | 数据根（`jobs/` `reports/`） |
| `WENGAO_ASSETS_ROOT` | 空 | pip 安装布局下显式声明受控素材根 |
| `WENGAO_PORT` | `8791` | 启动器端口 |
| `PEXELS_API_KEY` | 空 | 可选的 Pexels 检索（见 4.3） |

关于资源本体：模型调用仅指向**本机**端点（`127.0.0.1`）；安装包**不含模型权重**，也**不含任何字体文件**（全仓实测字体文件数量 = **0**，详见 `LICENSE-MATRIX.md` 第 3.3 节）。

### 4.2 费用

- 使用本机模型（llama.cpp / Ollama 加载本机 GGUF）时**不产生 API 计费**；成本是本机算力与显存/内存占用。
- 若改用任何云端模型或有密钥的图库 API，**费用由使用者承担**；本项目不会代付、不会代购额度。
- 未做任何外部付费调用之前，本节所述“零费用”只适用于本机推理路径。

### 4.3 数据去向（哪些东西会离开本机）

| 场景 | 是否会离开本机 | 说明 |
| :--- | :--- | :--- |
| 本机模型 when activated | 否 | 文本送至 `127.0.0.1` 上的本机进程 |
| Pexels 场景检索（**可选**，需配置 `PEXELS_API_KEY`） | **是**，向 `https://api.pexels.com` 发送 HTTPS 请求 | 只发送**短小、中性的英文场景查询词**（如 `supermarket checkout`）；不发送页面原文、引文、备注或业务数字。密钥只从启动工作台的进程环境读取，**不写入任务记录、PPT 或日志** |
| Commons / Openverse 检索（无密钥路径） | **是**，向公共 API 发送 HTTPS 请求 | 同上，仅发送中性查询词 |
| 遥测 / 埋点 | 否 | 默认关闭；调试包不自动外发源文档 |

> 上述 Pexels / Commons / Openverse 链路仍须完成真实工作台端到端验收后才能算“可用”；当前本机验收结果为失败（见第 0 节）。

---

## 5. 合成示例（可以直接跑，无需真实企业数据）

包内自带**合成**样例，`samples/material_replay/`：

| 文件 | sha256（前 16 位） | 字节 | 说明 |
| :--- | :--- | ---: | :--- |
| `annual_review_synthetic.md` | `659255db46d1aba1…` | 2882 | 合成业务材料（虚构机构名与数字，已在本文件头部标注为合成样例） |
| `annual_review_17p.plan.json` | `9f68e6809144447a…` | 18510 | 17 页冻结计划回放（不调用模型） |
| `annual_review_17p_deduplicated.plan.json` | `b1053544284d62e6…` | 16576 | 同上基础上做事实去重的回放计划 |
| `index.json` | `4ce15ec92335b25f…` | 955 | 回放包索引与“不冒充新模型调用”的说明 |

回放包的意义：**只回放已冻结计划、不调用模型**，因此可以在没有本机模型、没有 LibreOffice 的机器上验证“构建段”是否把事实按计划落到 PPTX；但它**不能证明**模型规划、真实渲染与门禁链路可用。

> 另有 `samples/open-source-50-decks/multilingual-inputs/`（50 份多语言合成输入，文件头均声明为虚构案例），用于多语言验收；其制作批次记录在同目录 `generation-evidence/` 下，**其中的检查结论由对应工作包负责，请勿把半成品结论当作最终验收**。

---

## 6. 四大核心业务链路

| 业务链路 | 工作台入口 | 核心规则与门禁要求 |
| :--- | :--- | :--- |
| **已有 PPTX 美化** | 工作台「演示工作室」 / `precheck` | 完整保留用户所有原始事实、文字、备注与图表数据；应用角色字号下限与版面防遮挡约束；全稿语义规划真实场景配图 |
| **原始材料生成** | 工作台「材料生成」 / `from-material` | 提取并冻结必讲事实台账；本地模型确定全局结构、程序按章节注入冻结原句，避免逐章串行调用；页数必须严格符合 **10—20 页**区间契约，杜绝空话凑页或删事实缩页 |
| **受控局部修改** | 工作台「局部修改」 / `edit` | 必须精确指定 `(页码, 形状ID, 字段, 新值)`，生成独立版本分支；严格杜绝大模型自由发挥篡改未授权页面 |
| **版本管理与回滚** | 工作台「历史与恢复」 / `edit-restore` | 历史记录只追加不覆盖；恢复版本需经完整性比对校验；恢复后必须重新全量通过验收 |

---

## 7. 严苛硬性门禁体系（Hard Gates）

任何候选演示文稿在获得交付许可前，必须 100% 通过以下全量独立硬性门禁，无任何例外：

### 7.1 版面与可读性门禁（HG01—HG12）

- **HG01 文本完整性与零丢失**：原稿所有文本字符必须在最终 PPTX 中完整重现，不得丢失字符、数字或关键术语。
- **HG02 字符可见度与边界保障**：所有文字框必须位于幻灯片安全区域内，不可溢出画幅边界。
- **HG03/HG04 零碰撞与零遮挡**：任何文本块、卡片、图表与装饰形状之间必须保持安全间距，严禁文字被图文元素覆盖。
- **HG07 角色级字号下限**：强制执行清晰可读性标准（大标题 36–40pt、主标题 28–32pt、正文 20–24pt、注脚不低于 14pt）。
- **HG08 语义断行一致性**：中文长句必须在标点或合理语义切分点换行，严禁词汇中间生硬截断。
- **HG10 原生实播与重开核验**：必须提供 30–60 秒原生播放连续录像；放映结束后关闭并从磁盘重新打开该文件，核对 SHA-256 强校验哈希。

### 7.2 全稿语义场景配图门禁（HG13）

- **全场景真实摄影**：全稿贯穿场景图规划，拒绝千篇一律的库存模板图或无意义的色块卡片。
- **实际像素视觉复核**：每张候选照片必须由配置的本机多模态模型逐图查看，分别确认主体、动作和环境，并达到高置信度；图库标题或关键词匹配本身不能放行。证据绑定原图 SHA-256、最终嵌入图片、场景语义和固定审核提示词。模型不可用、图片不匹配或证据缺失均阻断生成，不以文字卡片替代。该规则已接入候选代码，**完整真实工作台验收仍未完成**。
- **多语言输入提示与场景规划**：本版先完成现有 26 种语言名单（阿拉伯语、孟加拉语、德语、英语、西班牙语、波斯语、法语、印地语、印尼语、意大利语、日语、韩语、马来语、荷兰语、波兰语、葡萄牙语、俄语、斯瓦希里语、泰米尔语、泰卢固语、泰语、土耳其语、乌克兰语、乌尔都语、越南语、中文）；希腊语和希伯来语只保留额外文字脚本提示，其他语言留待后续版本。每页先在本地从原文识别语言和脚本，再把未删减原文及语言上下文传给场景规划器，按来源语言提取关键词场景，并生成中性英文图库查询词。离线识别和 26 份规划器输入样例测试已通过；**这不等于 26 种语言都完成真实模型语义、排版、配图检索与插入验收**。目前仅中文和英语有完整端到端证据，其他语言仍按五层矩阵标为部分支持或待验证。该能力也不代表 UI 已本地化。
- **全稿去重与规范场景标识**：同一演示文稿内，同一具体场景仅允许出现一次；多语言同义表达规范映射至唯一定位 ID 并自动去重。
- **受控版权与零裁切契约**：每张嵌入图片必须具备完整的来源、许可证、创作者与唯一 SHA-256 标识；严格保持原始画幅比例，**零裁切（Zero-Crop Contain）**安全嵌入。

---

## 8. 兼容范围

| 维度 | 支持情况 |
| :--- | :--- |
| 操作系统 | macOS（Apple Silicon 已实机操作过）/ Linux / Windows WSL2 —— **Linux 与 Windows 未在本机实机验证** |
| Python | 3.10 — 3.13（本机实测 3.13） |
| 输入格式（材料生成） | `.md / .markdown / .txt / .text / .docx`；`.pptx` 会被明确拒绝 |
| 输入格式（美化） | `.pptx` |
| 输出 | `.pptx`；可选导出 PDF / PNG（依赖 LibreOffice） |
| 主题 | `wengao-basic` / `wengao-business` / `wengao-dark` / `wengao-minimal` / `wengao-adaptive` |
| 页面比例 | 材料生成链路固定 16:9，页数契约 10—20 页 |
| 语言 | 26 种目标名单（见 7.2）；**仅中文与英语有完整端到端证据**，其余为部分支持/待验证 |
| 明确不支持 | 跨页共享图表部件的编辑（会被明确拒绝） |

---

## 9. 常见问题诊断与排查（Troubleshooting）

### 9.1 `MODEL.RUNTIME_UNAVAILABLE`
- **现象**：任务在内容规划或场景图匹配阶段失败，提示模型运行时不可用。
- **原因**：本机推理端点未启动或端口不通（`WENGAO_LOCAL_MODEL_ENDPOINT` / 默认探测 18888→11434 都不通）。
- **处理**：启动本机推理服务后重试，例如：
  ```bash
  llama-server -m /path/to/qwen-model.gguf --port 18888 --host 127.0.0.1 -c 4096 -np 1 --flash-attn on -ngl 99
  ```

### 9.2 `PAGE_RANGE_UNSATISFIED`
- **现象**：材料生成阶段被规范拒绝，未产出最终 PPTX。
- **原因**：输入文本篇幅过短或过长，排版分页后的实际页数未能落入 **10—20 页**的法定契约区间。
- **处理**：确保输入的 Markdown / 文档具备 7—10 个逻辑清晰的核心章节与充分的事实支撑（建议 40 条以上事实句）。

### 9.3 `LAYOUT.HARD_GATE_FAILED`
- **现象**：验证阶段报告版面硬门禁未通过。
- **原因**：检测到底色与文字对比度不足、字号低于角色下限，或缺少原生放映视频证据。
- **处理**：查看任务目录下 `layout_gate_runs/*/layout_gate.json` 的具体 `issues` 列表，补全录像或通过受控局部修改调整属性。

### 9.4 `SCENE.PHOTO_UNEXPECTED_CROP`
- **现象**：场景图门禁报警，阻断候选交付。
- **原因**：配图在放入版位时被拉伸变形或裁切了有效画面。
- **处理**：版位配置必须严格遵循 `contain` 约束，确保画面长宽比 100% 完整保留。

### 9.5 `SCENE.PEXELS_API_KEY_MISSING`
- **现象**：配图阶段提示 Pexels 检索需要密钥。
- **原因**：启动工作台的进程环境中没有配置 `PEXELS_API_KEY`。
- **处理**：在**启动工作台的那个进程**里配置该环境变量后重启；未配置时程序会继续尝试无密钥的 Commons/Openverse 来源（见第 4.3 节）。

### 9.6 安装阶段的中文渲染冒烟失败
- **现象**：`install selftest` 的中文渲染冒烟为 FAIL。
- **原因**：本机缺少 LibreOffice 或缺少可用中文字体。
- **处理**：安装 LibreOffice（7.6+ / 24.x）并确保系统存在中文字体；包内只提供 `fonts.conf` 解析规则，**不分发字体文件**。

---

## 10. 功能与限制（照实写，不含“按计划完成”口径）

**代码层面具备、可静态核读**（对应模块见 `wengao_ppt/`，是否通过真实端到端验收见下文）：

- 材料提取与事实冻结：`extractors.py`、`source_fidelity.py`、`label_extract.py`；
- 计划→构建→校验→导出链路：`runner.py`、`checks.py`、`language_support.py`、
  `semantic_scenes.py`、`scene_images.py`、`scene_model.py`、`visual_plan.py`、`render.py`；
- `.docx`/`.pptx` 解析与受控修改：`parsers_xml.py`、`extractors.py`、`local_edit.py`、`cleanup.py`、`page_catalog.py`；
- 主题与排印：`theme.py`、`refine.py`、`visual.py`、`visual_fixes.py`、`backdrop.py`、
  `readability.py`、`glyphcheck.py`、`fontenv.py`、`tables.py`、`charts.py`、`repair.py`；
- 事故台账（append-only，绑定文件哈希）：`incident.py`；
- 用户级安装/升级/回退：`install.py`（含复制排除与失败关闭）。

**实测仍未闭环的部分**（不要当成已完成）：

1. 26 语言真实工作台链路 **0/26**；50 套真实生成 **0/50**（本机无可用本机模型、无 LibreOffice）。
2. 排版硬门禁 HG01—HG12：候选中只有策略契约 `contracts/layout_hard_gate_policy.json`，`checks.py` 内无 HG 实现；门禁当前跑在监督侧工作区。
3. P2/P5/P10 版式缺陷、原生播放门禁、29 个 alpha 对象：需真实渲染/解码证据，本机无渲染器。
4. 灰度版画识别：无模型时无法与黑白照片可靠区分，**不做启发式以免误杀**，保留为已知限制。
5. 五张 AI 生成配图缺少可核验的 provider/model/任务 ID 记录，且文件内无 AI 标识元数据 → 见 `LICENSE-MATRIX.md` BLK-02。
6. Pexels 示例照片的整包再分发权未清 → BLK-03；对外副本默认排除这些文件。

---

## 11. 版本记录

| 版本 / 候选 | 时间 | 变更要点 | 证据 |
| :--- | :--- | :--- | :--- |
| `wengao-visual-v13-material-fix`（本候选） | 2026-10-20 起多轮迭代 | ①语言层：修韩文汉字误判、URL 被判葡萄牙语、中英混排误判、sw/tr 召回为 0；②图库层：修代理 fake-IP 下候选被静默丢弃、失败列表误导、错误码混用、超时过短、长短语命中率 0、线描插画被当照片；③安装层：`copytree` 无排除导致 `jobs/ reports/ wb_loopback_data/` 被装进 `versions/<label>/` | 见桌面交付包 `00-交付说明.md`；测试基线 181→221 passed |
| P2 许可证与发布整理（本轮） | 2026-10-24 | 新增来源清单与许可证矩阵、`THIRD-PARTY-NOTICES.md`、发布消毒副本生成器与扫描器、补齐中英文 README 与 SKILL.md 元数据 | `reports/p2-license-release-20261023/` |
| 产品版本号 | — | `0.1.0`（`wengao_ppt/__init__.py`） | 实测 `--version` |

---

## 12. 第三方许可与版权说明（详见 `THIRD-PARTY-NOTICES.md`）

- **上游核心代码**：基于 luobo-ppt，**MIT**（`vendor/luobo-ppt/LICENSE`）。
- **第三方图标库**：Lucide Icons，**ISC**（实际路径 `vendor/luobo-ppt/assets/LICENSE-lucide.txt`；早期文档写的 `vendor/luobo-ppt/LICENSE-lucide.txt` 路径有误，已更正）。
- **Python 依赖**：Pillow / lxml / openpyxl / pypdf / pypdfium2 / python-docx / python-pptx，许可标识与版本口径见 `LICENSE-MATRIX.md` 第 2 节。
- **演示场景照片**：开发候选内的 Pexels 示例图**禁止作为图片包单独分发**；对外副本默认排除，详见副本内 `ASSET-EXCLUSIONS.md`。
- **自有代码许可**：**待所有者确定**，当前包内没有 `LICENSE` 文件；在此之前不得对外发布。

---

## 13. 发布前置警示

本候选**尚未**具备发布条件，至少存在以下必须由**项目所有者**处理的事项（代理不得代签、不得代办账户操作）：

1. 选定并写入自有代码 `LICENSE`；填写 README / SKILL.md / SkillHub 元数据中的 `license` 字段。
2. 决定五张 AI 生成配图的处置（补齐生成证据与 AI 标识，或替换/移除）。
3. 确认 Pexels 照片的整包再分发是否允许（法务/服务条款口径）。
4. 决定 `vendor/luobo-ppt` 是否随包分发，以及是否先按 GitHub 源重新核对（当前 vendor 的 git remote 指向本机 bundle，见 BLK-04）。
5. 确认依赖版本锁定口径（requirements 注释 / 本机安装 / PyPI 最新，见 BLK-06）。
