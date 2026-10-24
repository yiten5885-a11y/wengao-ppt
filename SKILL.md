---
name: wengao-ppt
slug: wengao-ppt
displayName: 稳稿 PPT
version: 0.1.0-dev-candidate
summary: 本地优先的 PPTX 生成与美化；产物必须经独立验收器重读落盘文件才算数，生成器自报一律不采信。
description: 从原始材料（md/txt/docx）生成 PPTX、或美化已有 PPTX，并由独立验收器重读磁盘上的 PPTX/PDF/渲染像素/字形轮廓给出结论。当用户要做述职汇报、经营评审、投标演示、培训课件，或需要把长材料变成结构规范、事实不丢字、配图有来源且合规的原生 PPTX 时使用。当前为开发候选：母语以外的 26 种语言只有离线识别与规划器样例的单测证据，真实端到端 0/26；50 套真实生成 0/50；需要本机模型（llama-server/Ollama）与 LibreOffice 才能跑真实链路和渲染门禁。
license: PENDING_OWNER_DECISION
category: 办公效率
tags: ["PPTX", "演示文稿", "本地模型", "独立验收", "多语言", "场景配图", "中文排版"]
homepage: PENDING_OWNER_DECISION
---

# 稳稿 PPT（WenGao PPT）

> 安装位置：把本目录整体放到 Agent 的 skills 目录下，目录名必须是 `wengao-ppt`
> （与 `name` 字段一致）；运行数据写在独立数据根，不写进程序目录。

## 安装依赖

```bash
pip install -r requirements.txt
```

必需第三方依赖（由源码真实 import 生成）：
- `Pillow`
- `lxml`
- `openpyxl`
- `pypdf`
- `pypdfium2`
- `python-docx`
- `python-pptx`

另需本机安装 **LibreOffice**（真实 PDF/PNG 渲染依赖）。

> 开发期另可用 `runner/validate_contracts.py` 校验契约与上游基线，
> 该脚本需要 `vendor/luobo-ppt` 的 git 检出，纯安装包内跑不通（非产品缺陷）。

## 安装 / 启动 / 升级（用户级入口）

```bash
cd <解压后的包目录>
python -m wengao_ppt.install install --package . --activate

# 安装后写入与版本无关的稳定入口（任意目录可调用，不需要 PYTHONPATH）
<安装根>/bin/wengao status
<安装根>/bin/wengao start --port 8792
<安装根>/bin/wengao upgrade --package <新包目录>
<安装根>/bin/wengao rollback
<安装根>/bin/wengao uninstall          # 预演；--apply 只移动程序，保留数据
```

安装根默认 `~/.wengao-ppt`（`--root` 或 `WENGAO_HOME` 可改）；
程序在 `versions/<label>/`，配置与任务在 `data/`；升级先自测再切 `current`。
macOS 也可双击 `<安装根>/启动稳稿PPT.command` 启动（端口已占用时只提示地址，不重复启动）。

## 唯一入口（产品 CLI）

```bash
python -m wengao_ppt.cli --help
```

工作台（仅本机回环，不公开部署）：

```bash
python workbench/server.py --port 8791
# 浏览器打开 http://127.0.0.1:8791/
```

## 标准流程

**A. 美化已有 PPTX**（工作台页面 ①—⑦ 等价）：

```bash
python -m wengao_ppt.cli precheck --input <file.pptx> --mode beautify --theme wengao-basic
python -m wengao_ppt.cli run      --job-id <JOB>
python -m wengao_ppt.cli validate --job-id <JOB>
python -m wengao_ppt.cli publish  --job-id <JOB>
python -m wengao_ppt.cli export   --job-id <JOB>
```

**B. 由原始材料生成**（输入 `.md/.markdown/.txt/.text/.docx`；`.pptx` 会被明确拒绝）：

```bash
python -m wengao_ppt.cli from-material --input <材料> --pages 12     --model <本地模型> --outdir <输出目录> [--accept]
```

**C. 局部修改与历史版本**（四类：文字 / 表格单元格 / 图表数据点 / 受控图片资产）：

```bash
python -m wengao_ppt.cli edit        --job-id <JOB> --page 1 --shape "Title 1"                                      --field text --new-value "…"
python -m wengao_ppt.cli edit-list   --job-id <JOB>
python -m wengao_ppt.cli edit-restore --job-id <JOB> --version-id <VERSION>
```

## 多语言语义场景配图（候选功能）

当前版本的 26 种语言目标名单为：阿拉伯语、孟加拉语、德语、英语、西班牙语、波斯语、法语、印地语、印尼语、意大利语、日语、韩语、马来语、荷兰语、波兰语、葡萄牙语、俄语、斯瓦希里语、泰米尔语、泰卢固语、泰语、土耳其语、乌克兰语、乌尔都语、越南语和中文。希腊语、希伯来语是额外的脚本提示，不属于这版 26 种语言的验收名单；其他语言放到后续版本。每页会先在本地从原文识别语言和脚本，再把未删减原文及识别上下文交给场景规划器。离线识别和 26 份规划器输入样例已通过单元测试；这不等于每种语言的真实模型语义、字体排版、照片检索和插入都已验收。离线语言识别只提供提示；未识别或混合语言不会单独导致拒绝，语义模型仍须按原文理解并给出可核验的逐页原文证据。

场景描述保留来源页语言，图片检索使用简短、中性的英文查询；原文、指标和引文不发送给图库。相同场景即使跨语言出现，也使用同一 `canonical_scene_id`，全稿只放一张图；同一主题下不同的具体场景可以各配一张不同图片。模型无法理解、原文证据不匹配、没有安全版位或找不到合规图片时，任务阻断，不用文字卡片或无关图片替代。候选代码和规则已加入；端到端模型、素材授权和最终导出验收仍需完成后才能发布。

图库命中的照片还必须由配置的本机多模态模型按实际像素审核：主体、动作、环境都要清晰可见且达到高置信度；图片 SHA-256、审核输入像素、场景身份及固定审核提示词须互相绑定。模型不可用、审核不确定或证据缺失时失败关闭；额度、认证、超时等模型错误必须保留原始错误类别，不能改报为普通“图库无图”。图片审核不上传云端。

## 可用主题（从 `wengao_ppt/theme.py` 读取）

- `wengao-basic`（默认，旧默认兼容）
- `wengao-business`（商务清晰）
- `wengao-dark`（深色科技）
- `wengao-minimal`（极简阅读）
- `wengao-adaptive`（原稿适配精修）：识别并**保留原稿自身的设计语言**
  （底色/文字色/强调色/字体），不换肤；只按结构规则做布局、位置、尺寸、
  字号、间距与对齐的精修（`wengao_ppt/refine.py`，规则版本 1.0.0）。
  适用场景：原稿已有成熟设计（如深色金调），只想修结构缺陷而不想被统一换肤。

## 边界

- 交付状态为 `DEVELOPMENT_CANDIDATE`，`release_ready=false`。
- 入口不得绕过本运行器自行签发 READY。
- 工作台页面提供四条流程：美化、材料生成（含回放模式）、局部修改、历史与恢复；
  四条流程均已通过真实浏览器操作实测（导入、制作、局部编辑、历史恢复、取消重试、
  损坏输入拒绝；含隐藏页用例），安装/修改/恢复均经既有门禁。
- **不支持跨页共享图表部件的编辑**（会被明确拒绝）。
- 材料任务页数契约为 10—20 页、16:9；内容不足时如实报告区间冲突，不复制页面凑数。
- 端口自选空闲端口；**不得占用或切换 8821**（那是与本项目无关的正式入口）。

## 本机模型 / 费用 / 数据去向（详见 README）

- 端点：`WENGAO_LOCAL_MODEL_ENDPOINT`；未设置时依次探测 `http://127.0.0.1:18888`、
  `http://127.0.0.1:11434`；回退端点 `WENGAO_FALLBACK_MODEL_ENDPOINT`（默认 18888）。
  安装包不含模型权重，也不含字体文件。
- 费用：本机模型无 API 计费；若指向云端模型或需密钥的图库 API，费用由使用者承担。
- 数据去向：本机推理不出本机；可选的 Pexels / Commons / Openverse 检索会离开本机，
  只发送短小、中性的英文场景查询词，不发送原文、引文、备注或业务数字；
  `PEXELS_API_KEY` 只从启动工作台的进程环境读取，不写入任务、PPT 或日志。

## 命令可信等级（本书一切宣称以此为准）

本文档给出的命令只分两类，不得混用：

- `【已实测】`：`python -m wengao_ppt.cli --help` / `--version`（0.1.0）与
  `python -m wengao_ppt.install --help`，在本候选自带 venv（Python 3.13）上退出码 0。
- `【草案·未验证】`：**其余所有安装与启动命令**。尚未在干净环境做过完整安装验证，
  对外材料里必须保留这一标记，不得写成已验证步骤。

## SkillHub / GitHub 封面上架检查（截至 2026-10-24）

| # | 检查项 | 状态 |
| :--- | :--- | :--- |
| 1 | `name`（必需） | 已完成：`wengao-ppt` |
| 2 | `slug`（必需，SkillHub） | 已完成：`wengao-ppt` |
| 3 | `displayName`（必需，SkillHub） | 已完成：`稳稿 PPT` |
| 4 | `version`（必需，SkillHub） | 已完成：`0.1.0-dev-candidate`（对应 `__version__ = 0.1.0`） |
| 5 | `summary`（上架展示） | 已完成 |
| 6 | `description`（触发/边界说明） | 已完成（已写实到状态与限制） |
| 7 | `description_en` | **未完成**：英文描述尚未补进 front-matter（英文内容当前只在 `README_EN.md`） |
| 8 | `license`（必需） | **阻断**：自有代码许可证未定，当前填 `PENDING_OWNER_DECISION`，不得上架 |
| 9 | `category` / `tags` | 已填写（`category: 办公效率`）；**上架分类与标签仍建议由所有者按平台规范复核** |
| 10 | `homepage` / 仓库地址 | **阻断**：待所有者提供仓库地址后填写 |
| 11 | 封面图 / 截图 | **未完成**：候选 `assets/` 内 5 张 AI 生成图与 5 张 Pexels 图的许可均未理清（BLK-02 / BLK-03），不得用作封面 |
| 12 | README 可执行性 | 已完成（中英双语，命令已分级标记） |
| 13 | 第三方声明 | 已完成（`THIRD-PARTY-NOTICES.md` + `LICENSE-MATRIX.md`） |
| 14 | 发布消毒 | 已完成（工具 + 扫描 + 副本，见 `reports/p2-license-release-20261023/`） |
| 15 | 制品 CVE/EULA 等上架合规 | **未完成**：未经独立验收者复跑，不得自行宣告完成 |

上架还有一条硬门槛，与许可证无关：GitHub / SkillHub 的账户登录、2FA、实名或
主体绑定、法律协议签署等，必须由**项目所有者本人**完成；代理一律不代办。

## 权限与审计

- 第三方凭证（如 `PEXELS_API_KEY`）由所有者自行配置，代理不读取、不回显其值。
- 本轮外部调用：仅 PyPI 公共元数据匿名读取（7 项依赖），无付费模型调用，记录见
  `reports/p2-license-release-20261023/LICENSE-MATRIX.md` 第 5 节。
