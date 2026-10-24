# 模块盘点报告（Step 0.2）

日期：2026-10-24　作者：W4（owner）　依据：`WENGAO_DIRECTION_RESET_20261024.md` §1、`00_总览与拼接.md` §8

## 1. 比对对象与方法

| 对象 | 位置 | 说明 |
|---|---|---|
| A. 运行候选 | `wengao-ppt/build/wengao-visual-v13-material-fix/` | 当前实际运行的候选，W4 于 2026-10-23 14:10 PDT 做本地快照 |
| B. v16 快照 | 桌面 `稳稿PPT-开发包/稳稿PPT_v16验收_20261017T151500/源码候选/github-source/` | 2026-10-17 验收快照 |
| C. 场景配图交付包 | 桌面 `稳稿PPT-场景配图交付-20261023/code/` | 2026-10-23 场景提供方交付的 5 个文件 |

方法：逐文件清单对比（文件名集合）+ 内容二进制比对（cmp）。只统计 `wengao_ppt/**/*.py` 与 `workbench/**`。

## 2. 总量

| 指标 | A 运行候选 | B v16 快照 |
|---|---|---|
| `wengao_ppt` py 文件数 | **64** | 18 |
| `workbench/` | server.py 137KB / index.html 124KB / make_flow.py 19KB | server.py / index.html / make_flow.py（均存在，内容有差异） |
| 测试 | **348 个**（343 passed + 3 环境路径失败 + 2 skipped，见 §5） | — |

## 3. A 相对 B 的新增模块（46 个 py，按来源分组）

**场景链（2026-10-23 上午，上一会话交付）**：
`scene_images.py`(100KB)、`semantic_scenes.py`(186KB)、`visual_plan.py`、`scene_model.py`、`language_support.py`(43KB)、`visual.py`、`visual_fixes.py`、`backdrop.py`、`incident.py`、`refine.py`、`readability.py`、`glyphcheck.py`、`fontenv.py`、`cleanup.py`、`label_extract.py`(有差异) 等。

**并行窗口今日新建（2026-10-23 14:00–14:10 PDT，W2/W3 已在共享目录直接动工——见 §6）**：
- `wengao_ppt/research/**`（20 个 py：plan/extract/fetch/ground/llm/highlight/compare/verify/report/runner + search/{base,searxng,direct,mock}）→ **W3**
- `wengao_ppt/layout/**`（8 个 py：tokens/engine/measure/templates/compose/checks）→ **W2**
- `wengao_ppt/builders/**`（5 个 py：chart/table/process/timeline）→ **W2**
- `wengao_ppt/planner/**`（2 个 py：fact_ledger）→ **W2**

**B 有而 A 无的模块：无。** A 是 B 的超集（文件名维度）。

## 4. 共有但内容有差异的文件（9 个）

`runner.py`、`checks.py`、`cli.py`、`install.py`、`local_edit.py`、`render.py`、`source_fidelity.py`、`theme.py`、`label_extract.py`。
其中 `runner.py` 差异最大（184KB，P1/P2/ALWAYS 必需检查、semantic_scenes 阶段、材料任务身份冻结均在此）。

## 5. A 与 C 的比对（场景链交付一致性）

| 文件 | C 交付包 | A 候选 | 判定 |
|---|---|---|---|
| install.py | 43,533 B | 43,533 B | 一致 |
| visual_plan.py | 17,906 B | 17,906 B | 一致 |
| scene_images.py | 99,082 B | **100,459 B** | **不一致（候选更大）** |
| semantic_scenes.py | 180,437 B | **186,075 B** | **不一致（候选更大）** |
| language_support.py | 41,697 B | **43,280 B** | **不一致（候选更大）** |

结论：候选中的场景链三个文件在交付包之后被再次修改（候选 mtime 为 2026-10-23 06:31–12:07）。与任务书 §1「交付时另一进程并发改了 scene_images.py」的证据互相印证。**W1 接手时的基线是候选版本，不是交付包版本**；交付包中的版本仅作历史参照。

## 6. 对窗口文档的更正（任务书 §8 第 2 项要求 W4 统一更正）

1. **`vendor/luobo-ppt` 实际存在**（`vendor/luobo-ppt/`，内含独立 `.git`）。预检曾报 `VENDOR_DIR_MISSING` 与当前事实不符，以本目录为准；独立 git 仓库不计入主仓库提交（见 `.gitignore` 与 init_repo.sh 处理）。
2. **`tools/searxng/` 已存在**（W3 今日新建），SearXNG 相关工件归 W3 独占（OWNERS.yaml 已登记）。
3. **`language_support.py` 存在于候选**（v16 快照没有），"运行候选里 v16 快照未见"的旧描述作废；它归 W4 只读维护（其他窗口不得改）。
4. **`contracts/errors/layout.json`（W2）、`research.json`（W3）已存在**，格式不统一（一个用 `rs.error_codes.v1` schema 壳，一个用 description）。`scripts/aggregate_error_codes.py` 已能兼容两者并检测重复；格式统一走变更请求单，不强行改他人文件。
5. **"221 个测试"的表述已过时**：候选当前为 **348 个测试**。基线结果（本机 venv，Python 3.13.12，2026-10-23）：343 passed、2 skipped、3 failed。3 个失败（`test_release_copy_gate_enforcement.py`）全部因为硬编码路径 `/Users/huangyingteng/.cache/codex-runtimes/.../python3` 在本机不存在——**属环境差异，非代码回归，在 Mac mini 本机会通过**。后续"不回归"以本报告的 343+3+2 为基线，不再引用 221。

## 7. 协调事实（必须报告）

并行窗口 W2/W3 于 **2026-10-23 14:00–14:10**（Step 0 完成前）直接在共享候选目录创建了 `research/`、`layout/`、`builders/`、`planner/`。即：**worktree 隔离尚未生效，多 agent 并发写同一工作目录的风险已经存在**（任务书 §1 中"另一进程并发改 scene_images.py"的教训）。处置：

1. W4 已在本机快照建立 git 基线（`e29161b`），交付包内含 `scripts/init_repo.sh` 供 Mac mini 本机执行，建立唯一基线。
2. `OWNERS.yaml` + `scripts/ownership_guard.py` 已交付，**其余窗口分支必须过 guard 才能合并**（越界即拒绝，已实测）。
3. 建议立即停止在共享目录直接改动：W1/W2/W3 迁到 `worktrees/<w1|w2|w3>`（命令见 `reports/handoff/STEP0_DONE.md`），把已在共享目录产生的改动按 OWNERS 归属各自提交到各自分支。
