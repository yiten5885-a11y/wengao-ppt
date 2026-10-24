# WP9 · P1 开源前 50 套真实 PPT 与防伪门禁 — 设计与交付说明

> 2026-10-24 监督复核：门禁已改为固定完整候选清单，候选根与运行证据根可以分离；冻结重算与 49 个门禁负例、329 项候选全套测试均有本地运行记录。当前 50 套生产验收仍为 **BLOCKED**：冻结信任库未配置签发者/运行时/审校者公钥，清单 0 项、真实通过 0/50。不要把旧环境诊断当成当前事实；当前观测见 §6。

- 构建根：`/Users/huangyingteng/WorkBuddy/2026-10-12-00-54-41/wengao-ppt/build/wengao-visual-v13-material-fix`
- 基线快照：`BASELINE_SHAS.txt`（95 项，SHA256；本仓库非 Git 仓库，沿用 P8 包的 SHA256+mtime 基线约定）
- 交付时间（UTC）：见 `SHA256.txt`

## 1. 结论（先说结果）

| 项目 | 状态 | 说明 |
|------|------|------|
| 门禁落成可执行、fail-closed | **阶段通过（有真实运行证据）** | 2026-10-24：49 个门禁测试及候选全套 329 个测试通过；冻结身份重算无 findings。当前冻结信任库未配置，生产清单仍被阻断，见本地独立证据目录 |
| **50/50 真实套数** | **BLOCKED（未达标，不宣称完成）** | 当前清单 0 项、真实通过 0/50；信任库未配置使门禁拒绝签名证据。候选实例 8823 可访问，但仍是开发实例；另有模型工具续行与最终渲染环境资格待验证，见 §6 和 `HANDOFF_TO_RUNTIME.md` |

**50 套真实生成在本工作包内未达到 50/50，不得据此宣称项目完成。**

## 2. 改动文件清单（全部为 WP9 新增，零修改他人文件）

| 文件 | 动作 | 归属 |
|------|------|------|
| `contracts/prepublication_50_decks_identity_v1.json` | 新增 | WP9（本包拥有） |
| `scripts/prepublication_50_decks_gate.py` | 新增 | WP9 |
| `tests/test_prepublication_50_decks_gate_negatives.py` | 新增 | WP9 |
| `reports/wp9-prepublication-50-decks-20261023/*` | 新增 | WP9 |

**未修改**（遵守"只改自己工作包的文件"与并发约定）：

- `contracts/prepublication_50_decks_gate_v1.json` —— 只读消费。该文件被 P8 工作包在 2026-10-23 08:20 的
  `reports/p8-gate-50-decks-20261023/BASELINE_SHAS.txt` 明确列为"本包不得修改"，本包按只读输入使用。
- `workbench/server.py`（08:09 被他人修改）、`scripts/layout_gate.py`（08:22 被他人修改）及其他全部既有文件。
- 未触碰 8821 正式入口、未重启/改写共享 Ollama、代理、CLI 或其他会话。

## 3. 设计：身份必须从磁盘重算

原契约仅要求 `verdict=PASS` 与 `status=REVIEWED_AND_VERIFIED` 之类的**声明字段**，这些可以是自报。
本门禁把声明字段降级为**索引**，全部结论改为从磁盘重算：

1. **输入/输出身份**：重算 `source_file` 与 `candidate_pptx` 的 SHA256，与声明比对。
2. **真实插图数**：直接解压 PPTX，统计各 slide XML 中的 `p:pic` 数与 `ppt/media/` 图片数，取较大值作为实际插入数；
   与 `actual_scene_images_inserted` 不符即判 `SELF_REPORTED_IMAGE_COUNT_MISMATCH`，<1 即判 `NO_SCENE_IMAGE_INSERTED`。
3. **构建身份 `build_sha256`**：门禁从签名冻结清单中的 `root` 定位候选根，并按固定规则重新枚举候选文件；新增、删除、改动、漏项或多项均阻断。把每个文件**实测**哈希按路径排序拼成 `<sha>  <path>` 再取 SHA256。冻结集任一字节变化都会使本批整体失效。
4. **任务运行记录**：`run_record` 必须存在、哈希匹配，且 `task_id/deck_id/source_sha256/candidate_pptx_sha256/build_sha256/runtime_environment/status=COMPLETED` 与磁盘一致；环境字段还要绑定候选 `requirements.txt` 的实测 SHA。
5. **逐页覆盖**：`page_scope.rendered_pages`、`reviewed_pages`、`preview_artifacts`、审查记录的 `pages` 都必须完整覆盖 `1..page_count`；
   页数本身也由 PPTX 重算，不接受声明页数。
6. **下载一致性**：`download_artifact` 必须存在，且字节 SHA256 等于候选 PPTX 的 SHA256（防"生成与下载不是同一文件"）。
7. **重复身份**：`deck_id / task_id / source_sha256 / candidate_pptx_sha256 / review_evidence.sha256` 任一重复即阻断。
8. **路径安全**：所有引用路径拒绝绝对路径与 `..` 逃逸。

**fail-closed**：任一条目缺项、错哈希、证据缺失、重复、过期、伪造，或条目数不足 50，整体 `BLOCKED`；
门禁内部异常也返回 `BLOCKED`（`GATE_INTERNAL_ERROR`）；`PASS` 只在 50 条全部重算通过且 findings 为空时产生。

## 4. 接口

```bash
# 生成完整候选冻结清单；不可指定窄 glob。建议把批次证据放在候选根之外。
python3 scripts/prepublication_50_decks_gate.py freeze \
  --root <candidate_root> --out <evidence_root>/freeze.json

# 门禁校验（PASS 退出码 0；BLOCKED 退出码 2）
python3 scripts/prepublication_50_decks_gate.py verify <evidence_root>/manifest.json \
  --freeze-manifest <evidence_root>/freeze.json \
  --json-out <evidence_root>/verify_report.json
```

Python 接口：`audit_deck_manifest(manifest_path, freeze_path) -> dict`（发布目标固定为 50），
返回 `{schema, verdict, expected_count, manifest_entries, counted_real_decks, build_sha256, frozen_at, findings[]}`。

## 5. 真实执行证据

> 34 项测试是 2026-10-23 旧接口的历史记录，不能代表当前门禁。2026-10-24 的当前复核与运行日志保存在监督工作区 `work/strict-qa/prepublication-freeze-inventory-20261024/`；本次检查应以其中时间最新的冻结与日志为准。

| 证据 | 命令 | 结果 |
|------|------|------|
| 当前门禁负例套件 | `python3 -m unittest tests.test_prepublication_50_decks_gate_negatives -v` | **Ran 49 tests … OK**；监督工作区 `50-deck-gate-negative-after-doc-refresh.log` |
| 当前候选全套测试 | `python3 -m unittest discover -s tests -p 'test_*.py'` | **Ran 329 tests … OK**；监督工作区 `full-candidate-suite-after-doc-refresh.log` |
| CLI 端到端（历史） | `python3 scripts/prepublication_50_decks_gate.py verify reports/.../demo/manifest_demo.json --freeze-manifest reports/.../demo/freeze_demo.json` | `verdict=BLOCKED`、`counted_real_decks=0`、**退出码 2**，历史输出见 `demo/verify_stdout.txt` 与 `demo/verify_demo.json` |
| 当前完整候选冻结与门禁 | `freeze` 后以隔离空清单负例执行 `verify` | 冻结身份重算 `findings=[]`；负例结果 **BLOCKED**，`manifest_entries=0`、`counted_real_decks=0`、finding `TRUST_STORE_UNPROVISIONED`；证据见监督工作区 `candidate-freeze-final-audited.json`、`empty-manifest-negative-input.json` 与 `verify-blocked-final-audited.json`。空清单是负例输入，不是生产批次 |

负例覆盖的阻断类别（≥8 类要求已满足，实际 15 类）：

缺项 / 审查证据缺失、错哈希（来源、PPTX、审查记录）、重复身份（PPTX 复用、task_id 复用、整条复制）、
旧结果（build 不一致、时间戳早于冻结）、伪造 PASS（声称 PASS 但产物不在盘、`REVIEWED_AND_VERIFIED` 不等于 PASS）、
无图、自报图片数不符、预览缺失、预览页数覆盖不全、下载缺失、下载失败（哈希不符）、未渲染页、
合成夹具 / 重放 / mock 三种标志、审查记录绑定错误、审查页数覆盖不全、任务未完成、条目数不足 50、
冻结文件字节变化导致整批失效、冻结清单缺失、路径逃逸、图源/许可证不在允许集合。

机制正例 `test_complete_evidence_is_not_blocked` 证明门禁不是"一律拒绝"；
该用例使用**构造的测试素材**，明确不计入 50 套。

## 6. 当前阻塞与环境观测（2026-10-24）

下表区分当前可验证事实和历史诊断。环境具备候选开发实例与渲染器入口，不代表真实端到端批次已经运行或可被门禁接受。

| 项目 | 当前实测证据 | 后果 |
|------|----------|------|
| 候选实例 | 2026-10-24 03:38（上海）`http://127.0.0.1:8823/api/meta` 可读；实例标签 `DEVELOPMENT_CANDIDATE`、build `7881a4668f253002879dc3b2117437c9`、`release_ready=false` | 可作为候选开发/验收入口；不是正式入口，也不证明真实生成链通过 |
| 正式入口 | 同时段 8821 连接拒绝；未修改、未重启 | 正式工作台尚未恢复；不得把 8821 状态当候选 8823 不可达 |
| 机器与渲染器 | 本机 `arm64`；候选 `.venv` 为 CPython 3.13.16 arm64；当前 `soffice` 可运行，报告版本为 `LibreOfficeDev 26.8.0.0.alpha0` | 不再沿用旧 x86_64/缺渲染器结论；该 alpha 渲染器仍需在批次环境中固定并做兼容资格验证 |
| 冻结信任库 | `contracts/prepublication_50_decks_trust_store_v1.json` 状态 `UNPROVISIONED`，`freeze_signers`、`runtime_signers`、`reviewers` 均为空；门禁实报 `TRUST_STORE_UNPROVISIONED` | 不能接受 50 套签名记录；需由发布责任人按 `HANDOFF_TO_RUNTIME.md` 安全接入已授权公钥，再更新固定哈希并重新冻结。不得生成假公钥或把私钥放入仓库/证据目录 |
| 当前生产清单 | `manifest_entries=0`，`counted_real_decks=0`，无合格真实生成/下载/逐页审查批次 | 50/50 尚未开始，不得用 fixture、回放或测试构造件计数 |
| 模型续行 | 最近 WorkBuddy 项目请求记录 `502 Local Ollama connection failed`；额度原因未证实。单独普通 Qwen 文本探针曾答 `WENGAO_READY`，但 WorkBuddy 工具调用及结果续行未验证 | 不能据此宣称模型额度不足，也不能据普通回答宣称真实生成链恢复 |
| 材料任务 | 历史 Bengali 任务 `mt-08f63a27` 为 `FAILED`，场景照片/安全版位未解决，`download_ready=false` | 该任务未完成；不能将其计入 50 套 |

此前记录的 x86_64/Rosetta、arm64 二进制冲突、没有 LibreOffice 及 8823 不可达，均是旧时点诊断；本次复核已观察到 arm64 候选环境、soffice 和可达的 8823。它们不再列为当前已证实阻塞。若未来环境变化，需重新采集当时证据。

按任务书要求，缺证据一律标为未验证/阻断，不伪造证据。因此当前 50/50 仍记为 **BLOCKED**；合成回放与测试夹具不计入。

## 7. 未解决风险与已知限制

1. **真实性判定依赖声明 + 可重算证据**：若生成方声明 `is_synthetic_fixture=false` 并补齐全部可重算证据，
   门禁无法仅凭字节判断输入素材是否为真实业务材料。需人工抽检与来源登记兜底，不在门禁内降级。
2. **图片授权需外部抽检**：门禁只校验 `image_provider/image_license/license_url` 存在且取值在允许集合内，
   真实授权与数据去向需按任务书保留脱敏证据并抽检。已确认代码内建 Openverse / Wikimedia Commons（CC0/PD，无需密钥）与 Pexels（需 key）。
3. **并发**：P8 与 WP9 在 50 套门禁上范围重叠。本包通过"只读消费原契约 + 独立身份契约"避免写冲突，
   但两包若都产出 50 套清单，需由发布负责人在集成时裁定唯一权威清单。
4. **环境身份需另行记录**：候选源码/资源已用完整清单绑定；运行时解释器、依赖安装和渲染器版本仍应由批次运行记录明确记录并由可信 runtime signer 签名。
5. **50 套执行仍未开始**：需由具备 arm64 + LibreOffice 的运行时负责人执行，见 `HANDOFF_TO_RUNTIME.md`。
