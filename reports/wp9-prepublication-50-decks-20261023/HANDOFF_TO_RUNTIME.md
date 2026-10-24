# WP9 · 移交运行时负责人：50 套真实 PPT 的可复现执行流程

门禁已就绪并通过负例验证；**50 套真实生成尚未执行**，本文件是执行方的最小可操作清单。
执行前请确认你具备：`arm64` 解释器（能导入 `pptx`/`pypdfium2`/`PIL`）、LibreOffice（`soffice`）、
候选 workbench 实例（**不得占用正式入口 8821**）。

> 当前状态（2026-10-24）：候选开发实例 8823 可访问，8821 连接拒绝且未修改；本机当前观测为 arm64、候选 `.venv` CPython 3.13.16，`soffice` 可运行但版本为 `LibreOfficeDev 26.8.0.0.alpha0`，需在批次运行记录中固定并评估。真正阻断生产门禁的是 pinned trust store 仍为 `UNPROVISIONED`，签发者/运行时/审校者公钥角色均为空；50 套生产 manifest 为 0 项。普通 Qwen 文本探针成功不代表 Workbench 工具调用和结果续行已经通过；最近一次模型错误为 `502 Local Ollama connection failed`，额度原因未证实。旧报告里的 x86_64/无渲染器/8823 不可达只代表旧时点。

## 0. 先由发布责任人安全配置签名信任根

**当前 pinned trust store 是空的，50 套门禁会 fail-closed。没有完成本节前，不要开始创建声称可计入发布验收的签名批次。**

1. 由项目发布责任人批准三类角色的身份与职责：发布负责人冻结签名、Workbench 运行时签名、具备语言与视觉审校资格的独立审校者。运行时签名只能在真实任务完成后，由服务端依据任务数据库/磁盘重算绑定字段签发；模型输出和运行人员自报不能签发 PASS。
2. 由各责任角色在受控密钥管理环境中持有各自 Ed25519 私钥；私钥不得写入候选源码、工作区、批次证据、测试 fixture 或日志，也不得交给生成模型。独立审校者不能由同一个运行时密钥替代。
3. 将经发布责任人核验的**公钥**、key id、active 状态及审校者适用的语言/审校范围写入 `contracts/prepublication_50_decks_trust_store_v1.json`。不得为让门禁变绿而临时生成自签名或测试公钥，也不得接受 manifest 自带的信任根。
4. 对 trust store 字节 SHA256 做双人复核，按受控代码审查同步更新 `scripts/prepublication_50_decks_gate.py` 中的 `TRUST_STORE_SHA256_PIN`。信任库、pin、门禁测试均属于冻结候选身份的一部分；任何变更后都要重跑门禁负例与候选全套测试、重新冻结，并先确认空/伪造记录仍被阻断。
5. 发布责任人使用冻结签名密钥对新的冻结清单签名；Workbench runtime 与独立审校者分别使用各自密钥为真实运行记录和逐页审查记录签名。先用一套真实非发布样本完成端到端签名验证，再开始 50 套计数批次。

如果目前没有可验证的密钥持有人、职责授权或安全签发器，保持 `UNPROVISIONED` 并继续 BLOCKED；不要用本地生成的临时密钥绕过这个治理缺口。

## 0. 记录要求（每一套都要留）

| 记录项 | 要求 |
|--------|------|
| 实际 provider / model | 真实值，不得用模型列表或 `/api/tags` 代替真实推理 |
| 执行环境 | OS、CPU 架构、Python 实现/版本、实际渲染器/版本、候选 `requirements.txt` SHA；必须出现在 entry 与签名 runtime record 中且字段一致 |
| 独立 task ID | 每套唯一，对应磁盘上真实存在的运行记录，禁止重复任务/同一文件 |
| 时间 | ISO-8601 带时区，且不早于冻结时间 |
| 输入/输出文件 SHA256 | 由门禁重算比对，不得手写 |
| 图片来源与授权 | `image_provider` ∈ {openverse, wikimedia_commons, pexels}；授权取值见身份契约；保留来源页/许可证链接 |
| 页面范围 | `rendered_pages`、`reviewed_pages`、预览与审查记录须完整覆盖 `1..page_count` |
| 失败状态 | 失败即如实记录，不得降级为 WARN 或移除条目让分母变小 |
| 日志 | 不得包含密钥、私有原稿正文 |

图片图库：**仅用 Openverse / Wikimedia Commons（CC0 / Public Domain，无需密钥）**；
如需 Pexels 须另行授权并以环境变量提供 key，不得回显或落盘。

## 1. 冻结代码与素材

```bash
cd <BUILD_ROOT>
python3 scripts/prepublication_50_decks_gate.py freeze \
  --root . --out <EVIDENCE_ROOT>/freeze.json
```

冻结器会自动完整枚举候选源码根内的代码、资源、契约、示例、依赖声明、测试、vendor 与说明文件；调用者不能用 glob 缩小范围。它排除固定的运行期目录（例如 jobs、reports、wb_loopback_data、work、缓存）和已登记的历史生成证据目录；included 目录中的符号链接会失败闭锁。冻结清单的 `root` 指向 BUILD_ROOT，批次证据根可以独立于候选源码根。

记下输出中的 `frozen_at` 与文件数。**冻结后到 50 套跑完之前，任何代码/素材/模型策略变更都会使本批整体失效并重跑。**

## 2. 准备 50 个独立输入

- 50 个**内容互异**的真实材料文件（不同主题/语言/结构），任一 `source_sha256` 重复会被判 `DUPLICATE_IDENTITY`。
- 语言取自既有 26 语言矩阵（`language_codes`）。

## 3. 逐套真实生成（不得用 fixture / mock / replay）

对每套执行完整链路，并把每一步产物落到磁盘：

1. 经候选 workbench（**非 8821**）发起真实生成，记录实际 `task_id` 与运行记录 JSON；
2. 真实场景图片插入（实际调用图库并落盘，许可证入记录）；
3. 逐页渲染 + 逐页预览文件（每页一个文件）；
4. 下载产物，确认与生成的 PPTX **同一字节**（门禁会比对 SHA256）；
5. 独立的内容保真 / 版面安全 / 视觉复核，落审查记录 JSON（绑定 `deck_id` 与 `candidate_pptx_sha256`，逐页列出）。

`is_synthetic_fixture` / `is_replay` / `is_mock` 必须为 `false`——合成回放与测试夹具不计入。

## 4. 生成清单并跑门禁

```bash
# manifest.json 结构见 contracts/prepublication_50_decks_identity_v1.json 的 deck_entry_required_fields
python3 scripts/prepublication_50_decks_gate.py verify <EVIDENCE_ROOT>/manifest.json \
  --freeze-manifest <EVIDENCE_ROOT>/freeze.json \
  --json-out <EVIDENCE_ROOT>/verify_report.json
echo "EXIT=$?"   # 必须为 0；2 = BLOCKED
```

**验收判据**：退出码 0、`verdict=PASS`、`counted_real_decks=50`、50 个互异 `candidate_pptx_sha256`、
每套实际插入图片数 ≥ 1（由门禁从 PPTX 重算）、预览与下载齐全、页数全覆盖。

## 5. 独立验收

由**未参与生成**的验收者复跑上述 `verify` 命令并核对 `<EVIDENCE_ROOT>/verify_report.json`，
不得由执行方自行宣告完成。复跑时 evidence 根与 BUILD_ROOT 可分开；门禁从签名 freeze.root 重新枚举当前 build 身份，必须仍与冻结字节完全一致。

## 6. 失效与重跑条件

以下任一发生，本批 50 套结果整体失效，必须重跑：

- 冻结集中任一文件字节变化（门禁报 `FROZEN_FILE_HASH_MISMATCH` + `STALE_RESULT_BUILD_MISMATCH`）；
- 模型 provider / 模型版本 / 图库策略变化；
- 任一素材替换或新增主题/字体资源配置；
- 生成链路代码（含 `workbench/server.py`）变更。
