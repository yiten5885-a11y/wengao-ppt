# STEP0_DONE —— 仓库与契约冻结完成报告（Step 0.7）

日期：2026-10-24　作者：W4（owner）　tag：`rs-contracts-v1`（随本交付包在 Mac mini 本机基线提交后打）

## 1. 完成项与证据

| # | 事项 | 交付物 | 状态 |
|---|---|---|---|
| 0.1 | git init + .gitignore + 基线提交 + SHA-256 清单 | `scripts/init_repo.sh`（本机已实测：基线提交 335 文件）；清单 `reports/rs-w4/baseline_manifest.sha256` | ✅（Mac mini 侧执行一次即可） |
| 0.2 | 模块盘点 | `reports/rs-w4/module_inventory.md`（v13 vs v16 vs 交付包逐文件比对；更正 5 处文档描述；报告 221→348 测试基线） | ✅ |
| 0.3 | OWNERS + ownership_guard | `OWNERS.yaml`、`scripts/ownership_guard.py`（实测：归属正确通过、跨窗口越界拒绝、无主文件拒绝） | ✅ |
| 0.4 | netsafe / resource_lock 抽取 | `wengao_ppt/netsafe.py`（自 W3 netsafe_compat 纯搬移，行为不变）、`wengao_ppt/resource_lock.py`（llm/gen/vision 三把跨进程锁，排队超时 PIPE.RESOURCE_BUSY 按 C8 降级）；`research/netsafe_compat.py` 改为薄垫片 re-export | ✅ |
| 0.5 | 契约 Schema + 校验器 + 事件 + 错误码汇总 | `contracts/rs/*.schema.json`（6 份）、`wengao_ppt/contracts_rs.py`（零依赖校验器）、`wengao_ppt/events.py`（C2）、`contracts/errors/pipe.json`、`scripts/aggregate_error_codes.py`；正反例测试 26 个全绿 | ✅ |
| 0.6 | fixtures + check_all | `fixtures/{media,layout,research,pipeline}/`、`scripts/check_all.sh`（pytest + schema + ownership 一条命令） | ✅ |
| 0.7 | worktree + 分支 + tag | 见 §2 命令（**须在 Mac mini 本机执行**；本机快照已建 git 基线用于产出交付 diff） | ⏳ 待本机执行 |
| 0.8 | P8/WP9 门禁合并 | `reports/rs-w4/gate-merge-p8-wp9.md` + `scripts/rs8_extend_identity_contract.py`（RS-8 触发时才 apply 的预置补丁） | ✅（合并决定已定；RS-8 扩展待触发） |

## 2. Mac mini 本机执行清单（按序）

```bash
cd /Users/huangyingteng/WorkBuddy/2026-10-12-00-54-41/wengao-ppt/build/wengao-visual-v13-material-fix
# 1) 应用 W4 交付包（见交付包内 APPLY.md），然后：
bash scripts/init_repo.sh                 # 基线提交 + SHA-256 清单
git tag rs-contracts-v1                   # 契约冻结 tag
# 2) 建四个 worktree（W1/W2/W3 立即从共享目录迁入，停止直接写主工作目录）：
for w in w1 w2 w3 w4; do
  git worktree add ../worktrees/$w -b rs/$w-$( [ $w = w4 ] && echo workbench || ([ $w = w1 ] && echo material || ([ $w = w2 ] && echo layout || echo research)) )
done
# 3) 把今日已在共享目录产生的 research/、layout/、builders/、planner/ 改动
#    按 OWNERS.yaml 归属提交到 w3 / w2 分支（由各窗口自行 git mv / commit）。
```

## 3. 给其他窗口的三条硬规则（自本 tag 起生效）

1. **不得直接写主工作目录**；只在自己 worktree 改自己独占范围（`OWNERS.yaml`）。
2. 合并前必须过 `bash scripts/check_all.sh --base main --head <branch>`，含 `ownership_guard.py` 越界拒绝。
3. 改契约只有一条路：`reports/handoff/` 提交变更请求单，W4（owner）决定并通知。

## 4. 已知阻塞（不自行绕过，按任务书 §6 报告）

1. `PEXELS_API_KEY` 未就绪（用户申请中）：RS-2 先用 Wikimedia/Openverse + T3/T4 跑通降级，key 到位再切主源。
2. ComfyUI 已装 checkpoint 名称与许可未登记：W1 开工时列出，商用/再分发限制先报用户。
3. RS-1 需要 Mac mini 本机的真实 Qwen + LibreOffice；本交付机（MacBook Pro）两者皆不可用（Ollama 跨网不可达、无 LibreOffice），脚本与报告模板已备，执行须在本机。
4. 内存：Qwen + ComfyUI + 相似度模型并发需先实测，`resource_lock` 的排队超时默认 900s，实测后由 W4 调默认值。
