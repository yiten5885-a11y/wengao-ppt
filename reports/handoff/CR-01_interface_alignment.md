# W4 → 各窗口：接口对齐与缺陷报告（2026-10-24）

W4（owner）在搭管线与新界面时实测发现的契约分歧与缺陷。按公共契约规则：
**改契约只有一条路——在 reports/handoff/ 提交变更请求单，由 W4 决定**。
本文即请求单与缺陷报告的汇总。

## CR-01（给 W2）：compose_deck 的 resolutions 参数形状【已对齐/已修复】

- **冻结契约 C6**：`resolve_scenes(...) -> list[SceneResolution]`；
  `compose_deck(plan, resolutions, theme, out_path, place_image, emit)`——resolutions
  自然应是同一个 list。
- **W2 当前实现**（`wengao_ppt/layout/compose.py`）：`resolutions: dict | None`，
  按 `{scene_id: SceneResolution}` 消费；传 list 会 `AttributeError: 'list' object has no attribute 'get'`。
- **W4 临时处置**：`pipeline/modules.py::_adapt_compose_deck` 按被调函数签名标注
  自动转换（list ↔ dict），两条路都能跑。
- **处理状态（2026-10-24 已完成）**：W2 已将 `compose_deck` 签名更新为 `resolutions: list | None`，
  内部直接兼容消费 `list[SceneResolution]`（自动建索引）与兼容旧 dict；`_adapt_compose_deck` 自动失效直连。
  单测 `tests/test_w2_planner_builders.py::test_compose_deck_accepts_list_resolutions_contract_c6` 已通过。

## BUG-W2-01（给 W2，阻塞级）：check_layout 在 measure.wrap_lines 死循环【已修复】

- **现象**：对含特定文本块（演示：bignumber "46.8%" + bullets）的候选 PPTX 调用
  `wengao_ppt.layout.checks.check_layout`，线程永久卡在
  `layout/measure.py:67 wrap_lines`（`block_height_emu` → 换行循环不终止）。
  堆栈见 W4 调试记录（faulthandler dump）。
- **影响**：任何调用方（管线/runner 门禁）都会挂起。
- **W4 临时处置**：`pipeline/modules.py::run_check` 加看门狗（layout 120s，其余 60–90s），
  超时记 FAILED 并保留现场——**不会无限等待，也不会假装通过**。
- **处理状态（2026-10-24 已完成）**：根因修毕：在 `layout/measure.py::wrap_lines` 中对数字 token
  保护逻辑加上单调前进量保证（`j = max(j, i + 1)` 且 `assert j > i` / `j <= i` 兜底），
  并增加 `max_iterations` 迭代安全守卫。
  回归用例 `tests/test_w2_layout_engine.py::test_wrap_lines_terminates_on_bignumber_percent_and_bullets` 已覆盖通过。

## 接口备忘（给 W1，M1/M2 交付时对照）

`pipeline/modules.py` 的注册表已为以下函数留位，签名以 C6 为准：

| 函数 | 签名要点 | 备注 |
|---|---|---|
| `resolve_scenes(requests, options, job_dir, emit) -> list[SceneResolution]` | 每个请求必有一条返回；永不抛 UNRESOLVED_SCENES；幂等 | 请求来自 `plan.pages[].image_slots[].scene`（C3 形状） |
| `place_image(slide, rect, asset, fit="cover", alt_text="") -> shape_id` | 由 W2 compose_deck 作为参数注入 | 位置由 W2 给，W1 不决定放哪 |
| `apply_transitions(pptx_in, pptx_out, policy, roles) -> TransitionReport` | roles={index: role}；幂等 | 整稿统一转场族 |
| `check_image_ledger(job_dir) -> CheckReport` | 收 **job_dir**（读 evidence/images/ledger.json） | 与 check_layout 传参不同，见 CHECK_ARGS |
| `check_transitions(job_dir) -> CheckReport` | 收 **job_dir** | 同上 |

## 接口备忘（给 W3）

- `run_research(material_text, options, job_dir, emit)` 已按 C6 对接成功，**真模块已启用**；
  emit 收部分 dict（适配器补信封）工作正常。
- `verify_bundle(job_dir)` 已注册；门禁在 `research.enabled=true` 时到期
  （`research.bundle_verified`），未交付/失败记 NOT_EVALUABLE（阻断）——不会假装通过。
- 红队三条（空结果/数据冲突/伪造快照）请在 M3 用固定 fixture 覆盖；W4 的门禁侧已就位。

## 协调提醒（全体）

Step 0 完成前，`research/`、`layout/`、`builders/`、`planner/` 已在**共享候选目录**直接
产生（2026-10-23 14:00–14:10）。请尽快迁到各自 worktree（命令见
`reports/rs-w4/STEP0_DONE.md` §2）；合并前必须过 `scripts/check_all.sh`（含 ownership_guard）。

## 已交付的 W4 接口（供各窗口只读使用）

- `wengao_ppt/events.py`：C2 事件（`emit` 适配器已支持部分 dict 调用）。
- `wengao_ppt/netsafe.py`：网络安全（`research/netsafe_compat.py` 已改为薄垫片，import 路径不变）。
- `wengao_ppt/resource_lock.py`：llm/gen/vision 三把跨进程锁（C9）。
- `wengao_ppt/contracts_rs.py` + `contracts/rs/*.schema.json`：契约校验（正反例在 fixtures/）。
- `wengao_ppt/pipeline/`：主管线（真实模块优先、诚实桩兜底）。
- `wengao_ppt/gate_rs.py`：RS 条件必检项注册与执行。
