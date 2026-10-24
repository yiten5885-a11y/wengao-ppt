# WP9 工作包交付报告：开源前 50 套真实 PPT 防伪发布门禁与身份重算

- **工作包编号**：WP9 (Work Package 9)
- **候选分支/目录**：`wengao-visual-v13-material-fix`
- **执行时间（本地时间）**：2026-10-23 23:57:30 CST
- **基线快照记录**：`reports/wp9-prepublication-50-decks-20261023/BASELINE_SHAS.txt`
- **目标契约**：`contracts/prepublication_50_decks_gate_v1.json`（只读消费）
- **实施契约**：`contracts/prepublication_50_decks_identity_v1.json`（WP9 拥有）
- **当前发布门禁结论**：**BLOCKED（0/50 真实有效生成套数达标）** — 严格 Fail-Closed，拒绝合成夹具冒充，阻断发布。

---

## 1. 背景与审计问题定位

在上一轮审计中发现，原发布门禁仅核验 `verdict=PASS` 与 `status=REVIEWED_AND_VERIFIED` 字段，导致存在重大合规风险：
1. **夹具冒充产物**：仅对 50 套合成材料输入（`L01-en.md` .. `L50-nl.md`）进行了离线文本审查，即在 `input-manifest.json` 中标记 `ALL_50_REVIEWED_AND_VERIFIED`，被误认作 50 套 PPT 已生成完成。
2. **缺乏身份重算机制**：只要声明了 `PASS` 或提供了自报数据，旧逻辑无法验证磁盘上是否真实存在 PPTX、真实插入图片、逐页渲染图、下载产物和任务运行记录。
3. **未绑定代码与素材冻结 SHA**：代码发生变动后，旧任务结果未被判废。

为此，WP9 实施了完整的 **Fail-Closed 身份重算与防伪门禁**。

---

## 2. 门禁设计与核心机制

### 2.1 身份重算原则（Identity Recomputation）
任何条目的状态必须由门禁从磁盘物理文件中直接重算，声明值仅用作索引：
- **材料重算**：从源材料文件重算 SHA-256，必须与声明严格一致。
- **PPTX 重算**：从生成的 PPTX 文件重算 SHA-256，解压解析 `ppt/slides/slide*.xml` 中的 `<p:pic>` 标签及 `ppt/media/` 实际媒体，重算真实插入的场景图片数与真实页数；**任何自报图片数与重算不符直接 BLOCKED**。
- **冻结构建重算（build_sha256）**：门禁运行时刻对全部冻结源码/素材文件按字典序重算哈希链，确保生成环境与当前时刻一致。任一文件发生变动，旧批次结果全部失效（`STALE_RESULT_BUILD_MISMATCH`）。
- **任务运行记录绑定**：校验 `run_record.json`，核验 task_id、deck_id、source_sha256、candidate_pptx_sha256、build_sha256 与 `status == "COMPLETED"`。
- **逐页渲染与预览覆盖**：检查 $1..page\_count$ 每一页的渲染预览文件是否存在且哈希一致，缺失或页数不全即阻断（`UNRENDERED_PAGES` / `PREVIEW_PAGE_COVERAGE_INCOMPLETE`）。
- **下载产物一致性**：检查下载目录 PPTX 与候选 PPTX 字节及 SHA-256 绝对一致，杜绝下载损坏或假文件。
- **人工审校记录绑定**：核验审校记录中的 `candidate_pptx_sha256`、覆盖页码与 `reviewer_id`。
- **图库与许可证白名单**：仅放行 `openverse`、`wikimedia_commons`、`pexels`，且必须具备 CC0 / CC BY 4.0 / Pexels License 合法链接。

---

## 3. 改动与交付文件清单

| 文件路径 | 状态 | SHA-256 | 作用说明 |
|---|---|---|---|
| `contracts/prepublication_50_decks_identity_v1.json` | 新增 | `69874caca3f864f619bec46761bb1299e5b15a3327bcb150c485e1b7c7f5f1e7` | 50 套发布门禁身份契约（WP9 拥有） |
| `scripts/prepublication_50_decks_gate.py` | 新增 | `c177193b9918b8cfcf5f6819bb5d9928d9560cf8ea55098b10529f59b3915f83` | 门禁可执行脚本（支持 `freeze` 与 `verify`） |
| `tests/test_prepublication_50_decks_gate_negatives.py` | 新增 | `99c8ae91f5aa471b881eb84344aabd91c334f072a006a8306ddca95ae07f3e96` | 34 项负例与机制控制单元测试集 |
| `reports/wp9-prepublication-50-decks-20261023/BASELINE_SHAS.txt` | 新增 | `11b1e52a2fb10833e62ebd461f059041c4ce30c8b7ea3cedfeb710e3630f61e9` | 工作包启动前源码基线哈希记录 |
| `reports/wp9-prepublication-50-decks-20261023/demo/*` | 新增 | （各演示文件） | 机制运行演示夹具与阻断结果 |
| `contracts/prepublication_50_decks_gate_v1.json` | **只读** | `3059ff772dbd755dd70a99ae6f319b97d7a0277c87cd95893a4263f0ce282312` | 基础契约（WP9 严禁修改，保持原样） |
| `workbench/server.py` | **未触碰** | `450fcc855cec660bea1725ccf9321c784f9775469d198354ba0f54612684d911` | 保持负责人独占，WP9 未做任何编辑 |

---

## 4. 负例验证覆盖矩阵（8+ 类阻断全覆盖）

在 `tests/test_prepublication_50_decks_gate_negatives.py` 中实现了真实磁盘字节扰动的负例测试，共 34 项测试全部通过：

| 负例类别 | 测试用例名 | 触发阻断判定码 | 结果 |
|---|---|---|---|
| **1. 缺项** | `test_missing_required_field_blocks` | `DOWNLOAD_ARTIFACT_MISSING` | PASS (BLOCKED) |
| | `test_missing_review_evidence_blocks` | `IDENTITY_FIELD_MISSING` | PASS (BLOCKED) |
| | `test_escape_path_rejected` | `SOURCE_FILE_MISSING` | PASS (BLOCKED) |
| **2. 错哈希** | `test_wrong_source_hash_blocks` | `SOURCE_IDENTITY_MISMATCH` | PASS (BLOCKED) |
| | `test_wrong_pptx_hash_blocks` | `PPTX_IDENTITY_MISMATCH` | PASS (BLOCKED) |
| | `test_download_failure_hash_mismatch_blocks` | `DOWNLOAD_ARTIFACT_HASH_MISMATCH` | PASS (BLOCKED) |
| | `test_forged_review_record_hash_mismatch_blocks` | `EVIDENCE_HASH_MISMATCH` | PASS (BLOCKED) |
| **3. 重复身份** | `test_duplicate_identity_blocks` | `DUPLICATE_IDENTITY` | PASS (BLOCKED) |
| | `test_duplicate_task_id_blocks` | `DUPLICATE_IDENTITY` | PASS (BLOCKED) |
| | `test_duplicate_entries_are_detected_by_copy` | `DUPLICATE_IDENTITY` | PASS (BLOCKED) |
| **4. 旧结果** | `test_stale_result_build_mismatch_blocks` | `STALE_RESULT_BUILD_MISMATCH` | PASS (BLOCKED) |
| | `test_stale_timestamp_before_freeze_blocks` | `STALE_RESULT_TIMESTAMP` | PASS (BLOCKED) |
| | `test_frozen_code_change_invalidates_batch` | `FROZEN_FILE_HASH_MISMATCH`, `STALE_RESULT_BUILD_MISMATCH` | PASS (BLOCKED) |
| **5. 伪造 PASS** | `test_forged_pass_without_artifacts_blocks` | `CANDIDATE_PPTX_MISSING` | PASS (BLOCKED) |
| | `test_forged_pass_verdict_only_blocks` | `VERDICT_NOT_PASS` | PASS (BLOCKED) |
| | `test_forged_review_record_wrong_binding_blocks` | `REVIEW_BINDING_MISMATCH` | PASS (BLOCKED) |
| **6. 无图/假图数** | `test_no_scene_image_blocks` | `NO_SCENE_IMAGE_INSERTED` | PASS (BLOCKED) |
| | `test_self_reported_image_count_mismatch_blocks` | `SELF_REPORTED_IMAGE_COUNT_MISMATCH` | PASS (BLOCKED) |
| **7. 预览缺失** | `test_missing_preview_blocks` | `PREVIEW_ARTIFACT_MISSING` | PASS (BLOCKED) |
| | `test_preview_page_coverage_incomplete_blocks` | `PREVIEW_PAGE_COVERAGE_INCOMPLETE` | PASS (BLOCKED) |
| **8. 下载缺失/损坏** | `test_missing_download_blocks` | `DOWNLOAD_ARTIFACT_FILE_MISSING` | PASS (BLOCKED) |
| | `test_download_failure_hash_mismatch_blocks` | `DOWNLOAD_ARTIFACT_HASH_MISMATCH` | PASS (BLOCKED) |
| **9. 页面未渲染/未审查**| `test_unrendered_pages_blocks` | `UNRENDERED_PAGES` | PASS (BLOCKED) |
| | `test_review_page_coverage_incomplete_blocks` | `REVIEW_PAGE_COVERAGE_INCOMPLETE` | PASS (BLOCKED) |
| **10. 伪造非真机标志** | `test_synthetic_fixture_flag_blocks` | `DISQUALIFYING_FLAG_NOT_FALSE` | PASS (BLOCKED) |
| | `test_replay_flag_blocks` | `DISQUALIFYING_FLAG_NOT_FALSE` | PASS (BLOCKED) |
| | `test_mock_flag_blocks` | `DISQUALIFYING_FLAG_NOT_FALSE` | PASS (BLOCKED) |
| **11. 真实数量不足** | `test_deck_count_below_50_blocks` | `DECK_COUNT_MISMATCH`, `REAL_DECK_COUNT_BELOW_TARGET` | PASS (BLOCKED) |
| **12. 图库合规性** | `test_image_provider_not_allowed_blocks` | `IMAGE_PROVIDER_NOT_ALLOWED` | PASS (BLOCKED) |
| | `test_image_license_url_missing_blocks` | `IMAGE_LICENSE_URL_MISSING` | PASS (BLOCKED) |
| **正控机制校验** | `test_complete_evidence_is_not_blocked` | verdict: `PASS`（说明门禁具备放行能力） | PASS |

---

## 5. 可复现执行命令与实测输出

### 5.1 运行单元测试集
```bash
.venv/bin/pytest -v tests/test_prepublication_50_decks_gate_negatives.py
```
**实测输出**：
```
============================= test session starts ==============================
platform darwin -- Python 3.13.16, pytest-9.1.1, pluggy-1.7.0
rootdir: /Users/huangyingteng/WorkBuddy/2026-10-12-00-54-41/wengao-ppt/build/wengao-visual-v13-material-fix
collected 34 items

tests/test_prepublication_50_decks_gate_negatives.py::PositiveControl::test_complete_evidence_is_not_blocked PASSED [  2%]
tests/test_prepublication_50_decks_gate_negatives.py::NegativeCases::test_deck_count_below_50_blocks PASSED [  5%]
tests/test_prepublication_50_decks_gate_negatives.py::NegativeCases::test_download_failure_hash_mismatch_blocks PASSED [  8%]
tests/test_prepublication_50_decks_gate_negatives.py::NegativeCases::test_duplicate_identity_blocks PASSED [ 11%]
tests/test_prepublication_50_decks_gate_negatives.py::NegativeCases::test_duplicate_task_id_blocks PASSED [ 14%]
tests/test_prepublication_50_decks_gate_negatives.py::NegativeCases::test_escape_path_rejected PASSED [ 17%]
tests/test_prepublication_50_decks_gate_negatives.py::NegativeCases::test_forged_pass_verdict_only_blocks PASSED [ 20%]
tests/test_prepublication_50_decks_gate_negatives.py::NegativeCases::test_forged_pass_without_artifacts_blocks PASSED [ 23%]
tests/test_prepublication_50_decks_gate_negatives.py::NegativeCases::test_forged_review_record_hash_mismatch_blocks PASSED [ 26%]
tests/test_prepublication_50_decks_gate_negatives.py::NegativeCases::test_forged_review_record_wrong_binding_blocks PASSED [ 29%]
tests/test_prepublication_50_decks_gate_negatives.py::NegativeCases::test_frozen_code_change_invalidates_batch PASSED [ 32%]
tests/test_prepublication_50_decks_gate_negatives.py::NegativeCases::test_image_license_url_missing_blocks PASSED [ 35%]
tests/test_prepublication_50_decks_gate_negatives.py::NegativeCases::test_image_provider_not_allowed_blocks PASSED [ 38%]
tests/test_prepublication_50_decks_gate_negatives.py::NegativeCases::test_missing_download_blocks PASSED [ 41%]
tests/test_prepublication_50_decks_gate_negatives.py::NegativeCases::test_missing_freeze_manifest_blocks PASSED [ 44%]
tests/test_prepublication_50_decks_gate_negatives.py::NegativeCases::test_missing_preview_blocks PASSED [ 47%]
tests/test_prepublication_50_decks_gate_negatives.py::NegativeCases::test_missing_required_field_blocks PASSED [ 50%]
tests/test_prepublication_50_decks_gate_negatives.py::NegativeCases::test_missing_review_evidence_blocks PASSED [ 52%]
tests/test_prepublication_50_decks_gate_negatives.py::NegativeCases::test_mock_flag_blocks PASSED [ 55%]
tests/test_prepublication_50_decks_gate_negatives.py::NegativeCases::test_no_scene_image_blocks PASSED [ 58%]
tests/test_prepublication_50_decks_gate_negatives.py::NegativeCases::test_preview_page_coverage_incomplete_blocks PASSED [ 61%]
tests/test_prepublication_50_decks_gate_negatives.py::NegativeCases::test_replay_flag_blocks PASSED [ 64%]
tests/test_prepublication_50_decks_gate_negatives.py::NegativeCases::test_review_page_coverage_incomplete_blocks PASSED [ 67%]
tests/test_prepublication_50_decks_gate_negatives.py::NegativeCases::test_run_record_not_completed_blocks PASSED [ 70%]
tests/test_prepublication_50_decks_gate_negatives.py::NegativeCases::test_self_reported_image_count_mismatch_blocks PASSED [ 73%]
tests/test_prepublication_50_decks_gate_negatives.py::NegativeCases::test_stale_result_build_mismatch_blocks PASSED [ 76%]
tests/test_prepublication_50_decks_gate_negatives.py::NegativeCases::test_stale_timestamp_before_freeze_blocks PASSED [ 79%]
tests/test_prepublication_50_decks_gate_negatives.py::NegativeCases::test_synthetic_fixture_flag_blocks PASSED [ 82%]
tests/test_prepublication_50_decks_gate_negatives.py::NegativeCases::test_unrendered_pages_blocks PASSED [ 85%]
tests/test_prepublication_50_decks_gate_negatives.py::NegativeCases::test_wrong_pptx_hash_blocks PASSED [ 88%]
tests/test_prepublication_50_decks_gate_negatives.py::NegativeCases::test_wrong_source_hash_blocks PASSED [ 91%]
tests/test_prepublication_50_decks_gate_negatives.py::BuildIdentityTests::test_build_identity_detects_missing_frozen_file PASSED [ 94%]
tests/test_prepublication_50_decks_gate_negatives.py::BuildIdentityTests::test_build_identity_recomputed_from_disk PASSED [ 97%]
tests/test_prepublication_50_decks_gate_negatives.py::BuildIdentityTests::test_duplicate_entries_are_detected_by_copy PASSED [100%]

============================== 34 passed in 0.20s ==============================
```

### 5.2 运行真实门禁验证（退出码严格非零）
```bash
.venv/bin/python scripts/prepublication_50_decks_gate.py verify \
  reports/wp9-prepublication-50-decks-20261023/demo/manifest_demo.json \
  --freeze-manifest reports/wp9-prepublication-50-decks-20261023/demo/freeze_demo.json
```
**实测输出**（退出码为 2）：
```json
{
  "schema": "prepublication-50-decks-gate-audit/v1",
  "verdict": "BLOCKED",
  "expected_count": 50,
  "manifest_entries": 1,
  "counted_real_decks": 0,
  "build_sha256": "09009c1935151034c4c9b290a41e826a770cde57dc3bd2fd9f6abe1adec1c8f7",
  "frozen_at": "2026-10-23T15:50:11.802489+00:00",
  "base_contract": "contracts/prepublication_50_decks_gate_v1.json (read-only)",
  "identity_contract": "contracts/prepublication_50_decks_identity_v1.json",
  "findings": [
    {
      "code": "DECK_COUNT_MISMATCH",
      "expected": 50,
      "actual": 1
    },
    {
      "code": "DISQUALIFYING_FLAG_NOT_FALSE",
      "deck_id": "demo-deck-001",
      "field": "is_synthetic_fixture",
      "actual": true
    },
    {
      "code": "REAL_DECK_COUNT_BELOW_TARGET",
      "expected": 50,
      "verified": 0
    }
  ]
}
```

---

## 6. 真实失败与未解决风险

1. **真实 50 套生成尚未达成（当前 0/50）**：
   - 虽然 50 套材料文本（`L01-en.md` .. `L50-nl.md`）已准备就绪，但由于每套完整走完 UI 端到端制作流程（事实冻结规划、结构切块、PPTX 构造、排版主题、真实场景图检索与插入、LibreOffice 逐页渲染审查）耗时约 5-7 分钟，整批 50 套需数小时计算时间，目前在磁盘上**尚未存在 50 套通过全链审查的真实有效 PPTX**。
   - 依据 AGENTS.md 准则 4：“缺证据一律标为未验证/阻断，不伪造证据”。WP9 坚决拒绝用合成夹具或重放伪造通过，门禁真实返回 `BLOCKED`，严格防守开源发布标准。
2. **场景图版位与检索超时风险**：
   - 在真实任务实测（如 `job-7d16dede78b8`）中，偶发出现 `VISUAL.SCENE_UNRESOLVED`（场景图或安全版位未完整解决）；后续在执行 50 套批处理时需对网络图库及重试机制留足预算。
3. **独立验收与项目状态**：
   - 按照规范，开发候选状态维持为 `DEVELOPMENT_CANDIDATE`，发布状态为 `NOT_ACCEPTED`；本工作包完成后须交由独立验收者复跑，不得自行宣告整个项目完成。
