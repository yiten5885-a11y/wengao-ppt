---
name: wengao-ppt
display_name: 稳稿 PPT
display_name_en: WenGao PPT
description: 美化已有 PPTX 或由原始材料（md/txt/docx）生成专业 PPTX；本地优先，所有结论由独立验收器重读磁盘上的真实产物核验，不采信生成器自报。当用户需要整理/美化演示文稿排版，或把一份会议纪要/报告/大纲转成结构规范的 PPTX 时触发。
description_zh: 本地优先的 PPTX 生成与美化引擎。美化已有 PPTX 支持零配置（只需 LibreOffice）；由原始材料生成 PPTX 与语义场景配图需要你自备模型后端（本机或云端）。所有"完成"结论都由独立验收器重新读取磁盘上的 PPTX/PDF/渲染像素核验，不采信生成器自己的报告。当前为开发候选版，非生产就绪。
description_en: A local-first PPTX generation and beautification engine. Plain beautify works with zero model configuration (LibreOffice only); generating a PPTX from raw material and semantic scene photos need your own model backend (local or cloud). Every "done" verdict comes from an independent verifier that re-reads the actual PPTX/PDF/rendered pixels on disk — the generator's own self-report is never trusted. Currently a development candidate, not production-ready.
examples_zh:
  - "帮我把这份 PPT 重新排版，用商务主题，不要改内容"
  - "根据这份会议纪要生成一份 12 页的 PPT"
  - "这份演示文稿排版很乱，帮我美化一下，保留原来的设计风格"
examples_en:
  - "Reformat this PPTX with the business theme, don't change the content"
  - "Generate a 12-page PPTX from these meeting notes"
  - "This deck's layout is a mess — clean it up but keep its own design"
version: 1.0.0
author: "黄应腾"
---

# 技能指令正文

你（WorkBuddy 本地助理）在用户需要 **① 美化一份已有 PPTX** 或 **② 用一段
文字材料生成一份新 PPTX** 时，使用这个技能。两条路径互不相同，不要混用；
用户给的是 `.pptx` 就走①，给的是 `.md/.markdown/.txt/.text/.docx` 就走②。

## 0. 首次使用：确认/安装稳稿 PPT（幂等，可重复执行）

1. 执行 `scripts/setup.sh`。它会：
   - 检查 `~/.wengao-ppt-skill/wengao-ppt` 是否已是一个干净的 git 检出；
     不是就从 `https://github.com/yiten5885-a11y/wengao-ppt.git` 克隆
     （公开仓库，MIT 协议，含一个 git submodule，脚本会自动
     `git submodule update --init`）。
   - 建一个专用虚拟环境并安装 `requirements.txt` 里的依赖。
   - 打印 `python -m wengao_ppt.cli --version` 的真实输出作为验收证据。
2. 检查本机是否已装 LibreOffice（`soffice --version`，或 macOS 上
   `/Applications/LibreOffice.app`、`~/.wengao_ppt/libreoffice/LibreOffice.app`
   等常见位置）。**没装就如实告诉用户**："需要先安装 LibreOffice
   （https://www.libreoffice.org/download/download/），这是真实渲染/导出
   PPTX 的硬依赖，技能本身不能替你装系统级软件。"不要跳过这一步假装能跑。
3. 上面两步任何一步失败，把真实报错转述给用户，不要编造"应该是什么原因"。

## 1. 场景 A：美化已有 PPTX

默认走**零配置路径**（不需要任何模型后端，只需要 LibreOffice）：

```bash
cd ~/.wengao-ppt-skill/wengao-ppt
PY=.venv/bin/python
$PY -m wengao_ppt.cli precheck --input "<用户的.pptx路径>" --mode beautify --theme <主题>
# 上一步返回 job_id，下面都要用它
$PY -m wengao_ppt.cli run --job-id <job_id> --skip-scene-photos
$PY -m wengao_ppt.cli validate --job-id <job_id>
$PY -m wengao_ppt.cli publish --job-id <job_id>
$PY -m wengao_ppt.cli export --job-id <job_id> --outdir "<导出目录>"
```

可选主题（从 `wengao_ppt/theme.py` 的注册表读取，不要凭记忆拼主题名）：
`wengao-basic`（默认）/ `wengao-business`（商务清晰）/ `wengao-dark`（深色
科技）/ `wengao-minimal`（极简阅读）/ `wengao-adaptive`（保留原稿设计，
只修结构，不换肤——原稿本来就有成熟设计时用这个）。

如果用户明确想要"配真实场景照片"（不是上面这条默认零配置路径），去掉
`--skip-scene-photos`，但**执行前必须先提醒用户**：这需要本机已启动的
模型服务（llama-server/Ollama）或已配置的云端 API Key，没配会在 `run`
阶段直接失败（错误码 `MODEL.RUNTIME_UNAVAILABLE`），这是产品的真实硬
依赖，不是技能或网络问题，不要误导用户去重试或换网络。

## 2. 场景 B：由原始材料生成 PPTX

```bash
$PY -m wengao_ppt.cli from-material --input "<材料.md/.docx/.txt路径>" \
    --pages <目标页数> --model <本地模型名> --outdir "<导出目录>"
```

执行前必须提醒用户：这条路径**离不开模型后端**（本机或云端），用途是
"先冻结必讲事实台账，再由模型做内容规划"，没配模型会直接失败。页数契约
为 10—20 页、16:9；内容明显不足或超量时，稳稿 PPT 会如实报告区间冲突，
不会为了凑页数复制内容——不要代它承诺"肯定能凑够页数"。

## 3. 局部修改（可选，用户要求改某一处而不是重新全套生成时用）

```bash
$PY -m wengao_ppt.cli edit --job-id <job_id> --page <页码> --shape "<形状名>" \
    --field text --new-value "<新内容>"
$PY -m wengao_ppt.cli edit-list --job-id <job_id>       # 看历史版本
$PY -m wengao_ppt.cli edit-restore --job-id <job_id> --version-id <版本号>
```

支持四类：文字、表格单元格、图表数据点、受控图片资产。**不支持跨页共享
图表部件的编辑**，稳稿 PPT 会明确拒绝，不要重复尝试或绕过。

## 4. 结果判定纪律（必须遵守，不得省略）

- 只有 `validate` 返回 `"verdict": "DEVELOPMENT_CANDIDATE"` 且
  `"blocking_findings": 0` 才算真正做完；`run` 的 exit code 是 0 不等于
  任务成功，不要只看这个就跟用户说"搞定了"。
- 任何阶段报错（`precheck`/`run`/`validate`/`publish`/`export`），把
  JSON 里的 `gate`/`detail`/`code` 原样转述给用户，不要替它翻译成一句
  "大概是哪里的问题"糊弄过去。
- `export` 成功后，把它返回的真实文件路径（`.pptx`/`.pdf`/逐页图）
  原样告诉用户，不要自己拼路径猜测。
- 当前交付状态是**开发候选版，不是生产就绪版本**：26 种非中文语种、50
  套完整验收场景尚未全部跑出真实端到端证据；如实告知用户，不要替产品
  拔高状态。

## 5. 费用与数据去向（必须主动告知用户，不要等用户问）

- 零配置美化（`--skip-scene-photos`）：没有任何第三方调用，不产生费用。
- 语义场景配图 / 材料生成：如果用户配的是云端模型，原稿内容摘录与候选
  配图会发给该云端服务商，调用费用由用户自己的账户承担；如果是本机模型，
  不出本机、不产生 API 费用，但需要本机算力。
- 技能本身不读取、不回显用户的任何 API Key 明文。
