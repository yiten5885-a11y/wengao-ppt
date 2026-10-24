# W4 交付报告（FINAL）

日期：2026-10-24　窗口：W4 工作台与集成（owner）　分支：`rs/w4-workbench`（本地快照 6 个提交）
交付位置：Mac mini 桌面 `稳稿PPT-W4交付-20261024/`（本报告所在包的根）

## 1. 做了什么（按任务书条目）

### RS-0 仓库与并发收口（Step 0）——✅ 完成（Mac mini 侧执行一条命令即生效）

| 项 | 交付物 |
|---|---|
| 0.1 git 基线 | `scripts/init_repo.sh`（.gitignore 含密钥/权重/输出/缓存；基线提交 + SHA-256 清单） |
| 0.2 模块盘点 | `reports/rs-w4/module_inventory.md`（v13 候选 vs v16 快照 vs 交付包逐文件比对；更正 5 处文档描述；测试基线 221→348） |
| 0.3 独占护栏 | `OWNERS.yaml` + `scripts/ownership_guard.py`（实测：归属通过 / 跨窗口拒绝 / 无主文件拒绝） |
| 0.4 安全抽取 | `wengao_ppt/netsafe.py`（自 W3 compat 纯搬移，行为不变）+ `research/netsafe_compat.py` 薄垫片；`wengao_ppt/resource_lock.py`（llm/gen/vision 跨进程锁，超时按 C8 降级） |
| 0.5 契约冻结 | `contracts/rs/*.schema.json` 6 份 + `wengao_ppt/contracts_rs.py` 零依赖校验器 + `wengao_ppt/events.py`（C2）+ `contracts/errors/pipe.json` + `scripts/aggregate_error_codes.py`；每份契约正反例测试 |
| 0.6 一条命令 | `fixtures/{media,layout,research,pipeline}/` + `scripts/check_all.sh`（pytest + schema + ownership） |
| 0.7 冻结与开工 | `reports/rs-w4/STEP0_DONE.md`（worktree/tag 命令 + 给其他窗口的三条硬规则） |
| 0.8 门禁合并 | `reports/rs-w4/gate-merge-p8-wp9.md`（以 WP9 fail-closed 脚本为唯一执行入口；P8 三个独立判定字段并入 RS-8 扩展）+ `scripts/rs8_extend_identity_contract.py`（预置补丁，未到触发条件不执行） |

### RS-1 本机基线重跑——⏳ 脚本就绪，执行阻塞已如实报告

`scripts/rs1_baseline.py` + `reports/rs-w4/rs1-baseline/REPORT.md`。
**阻塞（不自行绕过）**：本交付机（MacBook Pro）无 LibreOffice；Mac mini 的 Ollama 跨网不可达
（Bonjour 被网络策略阻止）。本机**有** Ollama（qwen3.8:27b-wb65536 实测可生成），但缺渲染器，
按任务书 §2.3"验证在 Mac mini 本机运行"，RS-1 执行留给本机，脚本一条命令可跑。

### 管线编排——✅ 完成（真实模块优先 + 诚实桩兜底）

`wengao_ppt/pipeline/`：`generate.py`（read→research→plan→images→compose→transition→
render+checks，每步 C2 事件；C8 降级/阻断语义；取消；冲突裁决后局部重跑）、
`beautify.py`（包装 MakeOrchestrator，阶段历史翻译成事件流）、`edit.py`（local_edit +
recompose/insert + 版本）、`modules.py`（注册表：**真模块就位即自动启用**——W3 的
run_research/verify_bundle、W2 的 compose_deck/check_layout 已被真实接线）、
`options.py`/`decisions.py`/`metrics.py`（出稿率与场景覆盖率分开记账）。

**桩的三条铁律**：不造假数据、检查桩一律 NOT_EVALUABLE、模块来源写进 run.json。

### RS-9 工作台界面——✅ 完成（真实浏览器走查通过）

`workbench/ui/`（index.html + styles.css + app/render/actions 三 JS 模块）+ `workbench/ui_api.py`。
- 三栏结构、8 步时间线（C2 事件 SSE）、待确认卡（裁决→局部重排）、来源/配图/预览三标签、
  本页检查、缩略条、导出（附来源与署名清单）、本机运行探测面板（6 项真实探测，无猜测）。
- 硬约束：无 CDN（系统字体栈）、WCAG AA 对比度、焦点环、键盘可达、窄屏堆叠、
  中文优先无内部码、aria-live 反馈。
- **走查**：`reports/rs-w4/ui-walkthrough/` 10 个状态截图 + walkthrough.json，**0 JS 错误**
  （Playwright + 系统 Chrome 真实点击；这是现有工作台从未做过的）。
- 功能映射：`reports/rs-w4/feature-mapping.md`（旧 22 项功能逐项映射，19 已接、3 部分接、0 丢失）。

### 门禁接线——✅ 完成（不放宽既有门禁）

`wengao_ppt/gate_rs.py`：RS 条件必检项（配图账本/转场一致性/研究证据/版式复核）按
JobOptions 到期，并入 `expected_required_checks`（**只加不减**，不传 options 时行为与历史一致）；
未交付/异常/超时 → NOT_EVALUABLE（既有拦截逻辑自然阻断）。`/api/meta` 输出注册视图。
`wengao_ppt/env_probe.py` + `install probe` 子命令：SearXNG/ComfyUI/视觉模型/字体/LibreOffice/Ollama
探测，**只探测不安装**。

### RS-7 / RS-8 ——⏳ 运行手册与预置补丁就绪，待触发条件

`reports/rs-w4/rs7-verification-runbook.md`（1→5→20 三档、每档通过标准、证据清单）；
RS-8 扩展补丁与合并决定见 0.8。

## 2. 证据路径

```
reports/rs-w4/
  module_inventory.md         模块盘点（v13/v16/交付包比对）
  STEP0_DONE.md               Step 0 完成报告 + Mac mini 执行清单
  gate-merge-p8-wp9.md        50 套门禁合并决定 + RS-8 扩展规格
  rs1-baseline/REPORT.md      RS-1 模板与阻塞说明（run.json 执行后生成）
  rs7-verification-runbook.md 三档真实验证手册
  feature-mapping.md          旧功能 → 新界面映射（无缺项）
  ui-walkthrough/             10 张走查截图 + walkthrough.json + README
  FINAL.md                    本报告
reports/handoff/
  CR-01_interface_alignment.md  给 W1/W2/W3 的接口对齐与缺陷报告
```

## 3. 测试数

- 本机基线（快照时）：348 个（343 passed + 3 环境路径失败 + 2 skipped）。
- W4 新增：**51 个**（契约正反例 14、事件/锁 10、主管线 7、编辑/美化 4、门禁接线 5、
  探测 4、UI 静态 6、UI 走查脚本 1 套）。
- 当前全量：**399 个 = 385 passed + 3 failed（既有环境路径问题，Mac mini 本机可过）
  + 2 skipped + 9 errors（W3 今日新增的 tests/research 被本沙箱拒绝创建 /var/folders
  临时目录所致，非代码问题）**。
- `scripts/check_all.sh` 一条命令跑全（pytest + schema + ownership）。

## 4. 未完成与风险（如实）

1. **RS-1 执行**：缺 LibreOffice（本机）/ 跨网不可达（Mac mini Ollama）。脚本就绪。
2. **W2 缺陷 BUG-W2-01**：`check_layout` 在 `measure.wrap_lines` 死循环——管线已加看门狗
   （超时记 FAILED，不假装通过），但根因要 W2 修（见 CR-01）。
3. **W2 契约分歧 CR-01**：`compose_deck` 的 resolutions 形状（dict vs C6 的 list）——
   适配层已临时兼容，需 W2 对齐或提变更单。
4. **并行窗口未用 worktree**：14:00–14:10 已有四个模块目录在共享候选目录直接产生——
   已写进 module_inventory §7 与 STEP0_DONE，合并纪律靠 ownership_guard 把守。
5. **UI 三项部分接入**：主题选择器 UI、取消/重试按钮、对象级编辑面板（旧界面保留兜底），
   见 feature-mapping.md 第 1 节。
6. **资源锁默认值**：900s 排队上限未经实测，Mac mini 内存并发（Qwen+ComfyUI+视觉模型）
   实测后由 W4 调默认值。
7. **Pexels key / ComfyUI 模型许可**：未就绪/未登记（任务书 §6 两项，按报告流程走）。

## 5. 对其他窗口的反馈

见 `reports/handoff/CR-01_interface_alignment.md`：
- W1：五个函数的注册位与传参约定（check_image_ledger/check_transitions 收 **job_dir**，
  check_layout 收 **pptx**——适配在 `pipeline/modules.py::CHECK_ARGS`）。
- W2：CR-01 形状对齐 + BUG-W2-01 死循环修复（阻塞级）。
- W3：run_research 已真实接线成功；verify_bundle 门禁已注册；红队用例等 M3 fixture。

## 6. 应用方式（Mac mini 本机）

见交付包根目录 `APPLY.md`。摘要：`bash scripts/init_repo.sh` → 应用补丁或拷贝文件 →
`bash scripts/check_all.sh` → 按 `STEP0_DONE.md` §2 建 worktree。
