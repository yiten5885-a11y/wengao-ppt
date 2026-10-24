# 50 套门禁合并说明（Step 0.8，RS-8 前置）

日期：2026-10-24　作者：W4（owner）　依据：任务书 RS-0 0.8 / RS-8

## 1. 两份门禁的判定

| 项 | P8 基线 | WP9 交付 |
|---|---|---|
| 门禁规范 | `contracts/prepublication_50_decks_gate_v1.json`（gate_id `PREPUBLICATION_50_REAL_DECKS_GATE`，target 50，scope `existing-matrix-26-v1`，8 条 disqualification_rules，14 字段 manifest，release_claim_policy） | — |
| 身份契约 | — | `contracts/prepublication_50_decks_identity_v1.json`（15 类 reject_classes、freeze_policy、build_sha256 定义、deck_entry_required_fields） |
| 可执行门禁 | — | `scripts/prepublication_50_decks_gate.py`（fail-closed，34 测试，含负例） |

**合并决定（与任务书一致）：以 WP9 的 fail-closed 脚本为唯一权威执行入口；P8 的 gate 契约降级为"被只读消费的基础契约"（WP9 身份契约已如此声明：该文件在 P8 基线中被列为不得修改）。P8 的独有项并入身份契约/执行语义，详见 §3。**

候选目录中两份契约均已存在且互相引用；候选中的身份契约与 WP9 交付包中的副本存在差异（以候选为准，差异不影响枚举字段，WP9 包副本标 SUPERSEDED，不再回写）。

## 2. P8 独有项 → 并入位置

| P8 独有项 | 现状 | 并入决定 |
|---|---|---|
| `independent_content_fidelity_verdict` / `layout_and_placement_safety_verdict` / `visual_review_verdict` 三个独立判定字段 | WP9 身份契约未单列 | **并入 RS-8 扩展**：每套 deck 的 manifest 必填这三个字段，且必须为 PASS；缺一即拒（no_scene_image 同级） |
| `release_claim_policy`（未满 50 不得声称完成/多语言全支持） | WP9 以 reject_classes 覆盖大部分 | 保留 P8 原文为**唯一对外声称政策**，RS-8 门禁在输出里附带该政策哈希 |
| `required_acceptance_scope: existing-matrix-26-v1` | WP9 按 language_codes 取自 26 矩阵 | 保持；RS-6 用户确认后仅调整"真实链路抽样"范围，26 语言矩阵本身不动 |
| `target_real_decks: 50` | 同 | 不变 |
| `disqualification_rules` 8 条 | 与 WP9 reject_classes 高度重叠（is_mock/synthetic/replay/scene_images<1 两边都有） | WP9 的 15 类为超集，**以 WP9 为准**；P8 清单标注 SUPERSEDED-BY-WP9 |

## 3. RS-8 触发时必须做的身份契约扩展（已备好补丁，未到触发条件不执行）

`scripts/rs8_extend_identity_contract.py`（随本交付包提供）做且只做：

1. `image_provider` 允许集合加入 `ai_generated_local`（Pexels 已在集合内）。
2. `image_license` 同步加入 AI 生成许可取值（本地模型许可串，以 W1 登记的 ComfyUI 模型许可为准）。
3. 新增 `generator` / `prompt_digest` 登记项（与 C3 SceneResolution 的 asset 字段同名同义）。
4. manifest 必填增加 §2 的三个独立判定字段。
5. 每套可重算证据增加研究证据：`evidence/research/{bundle.json, queries.json, snapshots/*}` 与快照 sha256 对账。
6. 同步更新 `tests/test_prepublication_50_decks_gate_negatives.py` 负例（AI 生成登记缺失、prompt_digest 缺失、三个判定字段缺失各一条）。

补丁脚本为 fail-closed：目标文件与期望基线不一致时拒绝修改并报告，不静默改契约。

## 4. 冻结与执行顺序（不变）

触发条件：RS-2/3/4 完成 + RS-7 三档通过 → owner 确定 freeze 清单 → `scripts/prepublication_50_decks_gate.py` 执行 → **独立验收者**复跑 `verify`（执行方不得自评）。冻结后任何代码/素材/模型策略变更 → 整批作废重跑（身份契约 freeze_policy）。
