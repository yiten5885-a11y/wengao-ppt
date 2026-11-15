# 稳稿 PPT · 上架材料（2026-11-15 复核版）

> 本文件由本轮真实复核产生，取代 `reports/release_prep/skillhub_submission/`（2026-10-20，
> 已过时：当时的候选目录 `release_candidate_v*` 不是真正的当前代码线）。
> 本轮核验的是 `build/wengao-visual-v13-material-fix/`（HEAD `40a3edf7`，87 commits，
> 本地领先 `origin/main` 12 个提交，远端 `git@github.com:yiten5885-a11y/wengao-ppt.git`）。

> **2026-11-15 当天追加（所有者已决策，见下方 §0）**：§2 的方案 B 已实现并验证
> （commit `59c41c7`，HEAD 变为 `59c41c7a2ee89f1b551646ad2b993dd4e0cb4e72`）；
> 本地领先的提交已 `git push` 到 `origin/main`（`544a3bc..59c41c7`）；用现有 Gemini
> Key 补跑云端端到端验证的尝试已执行，结果见 §0.3（账户欠费，不是代码问题）。

## 0. 本轮（同一天内）追加结论

### 0.1 §2 方案 B：已实现、已验证

新增 `wengao_ppt.cli run --skip-scene-photos`：开启后不探测、不调用任何本机/云端
模型，直接写入诚实标注 `skipped_by_user=True` 的空场景计划；`scene_stage_verdict`
与 `scene_no_photo_outcomes` 两个纯函数各加一条独立短路分支，不与"模型判定零场景
触发硬规则"的既有纪律混用；`validate` 的 HG13 门禁据此走既有 `capacity_only` 分支
判 `EXECUTED`/非阻断。

**真实前后对比**（同一份合成 3 页 `.pptx`，本机确认无任何模型端点可达）：

| | 命令 | 结果 |
| --- | --- | --- |
| 之前 | `run --allow-text-card-fallback` | `FAILED` / `MODEL.RUNTIME_UNAVAILABLE`（preflight 阶段即失败，该标志覆盖不到"端点连不上"） |
| 之后 | `run --skip-scene-photos` → `validate` → `publish` → `export` | **全部真实跑通**：`validate` 13/13 checks 全 `EXECUTED`（含 `visual.semantic_scene_coverage`）、0 blocking、`RENDER.OK`；`export` 产出真实 `.pptx`/`.pdf`/3 张逐页 PNG，SHA256 已绑定；`publish` 判定 `DEVELOPMENT_CANDIDATE` |

回归：`scripts/run_full_regression.sh` 862→**864 passed**（新增 2 例分别锁定两个
短路分支）、1 skipped（原有、与本次改动无关）、0 errors。

同时顺带把 README.md/README_EN.md 的许可证状态行、SKILL.md 的
`license`/`homepage`/`description_en` 与上架检查表同步成已核验的真实状态（MIT
已决策、仓库地址已可填），不是代码逻辑变更。

### 0.2 Git 推送：已完成

`git push origin main` → `544a3bc..59c41c7  main -> main`，GitHub 仓库页面现在
展示的就是本轮修复后的代码。

### 0.3 云端端到端验证：已尝试，账户欠费，非代码缺陷

按所有者授权，用其现有 Gemini Key 补了一次真实调用：通过项目自带的
`cloud_model_config.save()` 把 `text_model` 临时设为 `gemini-3.8-flash`（复用
已配置的 vision_model 同名模型），对同一份合成稿重新 `run --allow-text-card-fallback`
（不带新加的 skip 标志，走的是原有的强制语义场景路径，验证"云端配置齐全时原有
路径确实能跑"）。

**真实结果**：`MODEL.CLOUD_HTTP_ERROR`，HTTP 402，Gemini 返回
`"Your prepayment credits are depleted... RESOURCE_EXHAUSTED"`——**账户没有预付
余额，不是代码或配置问题**。项目自身的错误分类与详情提示（引导去
`ai.studio/projects` 管理账单）工作正常，没有被误判为别的错误类型。

验证完成后，`text_model` 已改回原值（空字符串），`cloud_model.json` 恢复到本轮
开始前的状态（只留一份 `.bak-20261104-vision-cloud` 历史备份，本轮产生的临时
备份已清理）——**没有在账户配置上留下本轮改动的痕迹**。

**结论**：要拿到一次真正成功的"云端配图"端到端证据，需要所有者先给这个 Gemini
项目充值（或换一个有余额的 Key/模型）。在此之前，"有模型后端时原有路径能正常
走完"这一点，只能靠代码审查 + 单元/回归测试覆盖佐证，还没有一次真正跑到
`validate`/`export` 的云端真实证据——§2 方案 B（跳过开关）的证据是完整的，
方案 A 覆盖的"原有强制路径"证据仍不完整。

## 1. 复核方法与真实证据（状态词汇按项目 AGENTS.md）

| 检查 | 命令 | 结果 | 状态 |
| --- | --- | --- | --- |
| 上游基线一致性 | `vendor/luobo-ppt`: `git rev-parse HEAD` / `git status` | HEAD=`b6dcb0f3…` 与 `contracts/upstream_baseline.json` 声明一致；工作树干净 | 行为已测 / PASS |
| 全量回归测试 | `scripts/run_full_regression.sh`（隔离 basetemp，不改测试逻辑） | **862 passed, 1 skipped, 630 subtests passed, exit=0**（56.9s） | 行为已测 / PASS |
| CLI 入口 | `.venv/bin/python -m wengao_ppt.cli --version/--help` | `0.1.0`；exit=0 | 行为已测 / PASS |
| 真实任务：precheck | 合成 3 页 `.pptx`，`precheck --mode beautify --theme wengao-business` | 真实 job_id、真实 SHA256、`real_renderer: available`（已自动探测到用户级 LibreOffice） | 行为已测 / PASS |
| 真实任务：run（纯美化，无模型后端） | `run --job-id … --allow-text-card-fallback` | **FAILED / `MODEL.RUNTIME_UNAVAILABLE`**（本机 127.0.0.1:18088 无应答；云端配置 `text_model` 为空，不满足云端文本可用条件） | 行为已测 / 发现缺陷（见下） |
| LibreOffice 真实可用性 | `~/.wengao_ppt/libreoffice/LibreOffice.app` | 存在且可执行（`soffice --version` → 26.8.2.2） | 行为已测 / PASS |
| 契约/合规扫描 | 代码审查（`wengao_ppt/scene_model.py:1731-1737` 注释 + git log） | 语义场景配图阶段对**所有主题、所有路径（含纯美化）强制执行**，是 2026-11-05 "HG13" 提交起的既定产品决策，不是本轮新发现的回归 | 静态核读 + 行为已测，确认为既有设计 |
| 许可证 | `LICENSE`、`LICENSE-STATUS.md` | 自有代码 **MIT**（所有者 2026-11-04 决策），上游 `luobo-ppt` MIT、Lucide ISC 随包保留 | 行为已测（文件存在且内容核对） / PASS |
| 分发载荷卫生 | `dist/wengao-ppt-0.1.0-faeb303f/`（最新构建，`PAYLOAD-POLICY.md`） | 已排除 `jobs/reports/workbench_uploads` 等私有数据、`.venv`、测试/校准工具、22MB 字体样本、**`assets/visual_img/`（许可未决的示例配图）** | 行为已测 / PASS（载荷卫生合格） |
| SkillHub 元数据 | `SKILL.md` front-matter | 本轮已同步：`license: MIT`、`homepage` 已填仓库地址、补 `description_en` | 本轮修复已验证（文档同步，非代码逻辑变更） |

## 2. 唯一的产品级阻断发现（需所有者决策，详见 §4）

**没有可用的模型后端（本机 11434/18088 或已配置云端文本模型）时，产品无法完成任何任务——包括最基础的"美化已有 PPTX"，不需要材料生成也不例外。**

- 根因：`semantic_scenes`（语义场景配图）阶段在 2026-11-05 的 HG13 提交后，对所有主题、所有输入路径强制执行，不受 `visual_upgrade` 开关影响；`--allow-text-card-fallback` 只覆盖"模型在线但找不到合规配图"的场景，不覆盖"模型端点本身连不上"的场景——两者目前触发同一个前置连通性探测，探测失败直接判定 `GateError`，不进入可降级的分支。
- 影响：**一个全新买家，装好依赖、不配任何模型/API Key，拿一份现成 PPTX 走最简单的美化流程，也会 100% 失败**，报错信息虽然准确可操作（"启动本机语义模型服务，或在云端设置里启用云端文本模型"），但对非技术买家而言门槛很高。
- 不是回归：这是有记录的产品决策（commit `b56a886`/`3a5a96b` 起的说明注明"语义场景照片是所有主题/所有 PPTX 路径的独立阶段"），本轮只是把它的范围后果（纯美化也会被拦）第一次写清楚并实测验证。
- 两条可选处置（§4 请所有者选择）：
  - **A（不改代码，诚实定位）**：保持现状，在上架文案与系统需求里显著标注"必须先配置本机或云端多模态模型"，把产品定位为开发者/技术用户工具，价格与受众随之收窄。
  - **B（加一道真正的降级开关）**：新增一个显式参数（如 `--no-scene-photos` 或等价的 precheck 选项），让纯美化路径可以彻底跳过语义配图阶段（不降级为文字卡片，而是不进入该阶段），把"有没有真实照片"做成用户主动选择而非强制依赖；材料生成模式仍保留现状（它本来就需要模型做内容规划，配图只是其中一个可选增强）。工程量：修改 `runner.py` 的阶段触发条件 + 新增 2-3 条回归测试，预计 0.5-1 天。

## 3. 其它已核对、非阻断的事实

- 本地有 **12 个提交未推送到 `origin/main`**（含 HG13 修复、云端视觉回退、主题 2.0、渲染器路径修复、图库配图审查放宽）。不推送不影响本地运行，但 GitHub 仓库页面当前展示的是旧版本。
- `assets/visual_img/semantic_scenes/` 下的示例配图许可复杂（5 张 Pexels「禁止单独打包转售」+ 2 张公共领域），**分发载荷已正确排除整个目录**，不构成上架法律风险；但也意味着买家默认装好后没有任何示例配图，必须自备 `WENGAO_ASSETS_ROOT` 或启用云端生图。
- 1 个测试在本沙箱环境里仍报 `SKIPPED`（`tests/test_transitions.py:144`，判断逻辑只认 `/Applications/LibreOffice.app`，没跟进 2026-11-05 加入的用户级安装路径探测）；真实渲染器探测（`runner.py`）已经修好且实测 `available`，这只是一条测试自身的次要遗留，不影响产品行为。
- `cloud_model.json` 已配置 Gemini vision/image-gen，但 `text_model` 字段为空字符串，导致云端文本路径判定为不可用、回退去探测本机端点。是否补上 `text_model` 并据此产生一次真实的云端端到端验证，属于会消耗该账户 API 额度的操作，本轮未代为决定（详见 §4）。

## 4. 当天已决策、已执行的三件事（原始提问保留存档）

1. ~~§2 的 A/B 怎么选~~ → **选了 B，已实现并验证（§0.1）。**
2. ~~是否推送本地领先的提交~~ → **已推送（§0.2）。**
3. ~~是否用 Gemini Key 补验证~~ → **已尝试，账户欠费，非代码问题（§0.3）。**

仍然悬而未决的只剩一件事，不是我能替你决定的：**给这个 Gemini 项目充值（或换一个
有余额的 Key），才能拿到一次云端路径的真实成功证据。** 在此之前，"原有强制配图
路径、云端配置齐全时能正常走完"只有代码与单测佐证，没有一次真正跑到底的任务
作为证据；"零配置跳过配图"的新开关证据是完整的。

## 5. 结论（按项目状态词汇，不越权签发 verdict）

本轮不签发 `release_ready`/最终 verdict（项目规则禁止模型写入该字段）。基于以上真实证据：

- 工程质量、测试覆盖、合规卫生：**阶段通过**（相对"本地优先、买家自带模型"的定位）。
- 即插即用（零配置）体验：**本轮已达标**——`--skip-scene-photos` 让"美化已有 PPTX"
  在零模型配置下也能真实跑完 `precheck→run→validate→publish→export`（§0.1 证据）。
- 原有的"强制语义配图"路径：**修复已验证**（回归层面），**行为已测但云端成功
  证据缺失**（§0.3：账户欠费挡住了一次真正的云端端到端成功）。
- 上架流程（SkillHub/GitHub 账号登录、2FA、实名、签署条款）：**我不能代办**，需你本人完成；本轮只准备材料。
