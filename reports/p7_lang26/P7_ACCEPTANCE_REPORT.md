# P7 工作包交付报告：现有 26 种语言真实全链验收

- **候选**：`wengao-visual-v13-material-fix`
- **候选根目录**：`/Users/huangyingteng/WorkBuddy/2026-10-12-00-54-41/wengao-ppt/build/wengao-visual-v13-material-fix`
- **报告生成时间（UTC）**：2026-10-23T15:09:23Z
- **承接者声明**：本工作包为**独立执行者**，未修改任何产品源码，**未触碰 `workbench/server.py`**
- **总体判定**：**NOT_ACCEPTED — 真实全链 0/26**

---

## 0. 结论先行

本轮**没有**任何语言达成"真实全链通过"。真实可达成的只有两个**子系统级**证据：

| 层 | 结果 | 性质 |
|---|---|---|
| L1 语言识别（离线） | **26/26 PASS** | 真实执行，但不构成全链通过 |
| L5a 字体资源覆盖 | **26/26 PASS** | 真实执行，子系统级，**不等于产品排版验收** |
| L5b 产品排版链路 | **0/26 BLOCKED** | 无真实产物可验 |
| L2 / L3 / L6 / L7 / L8 / L9 | **0/26 BLOCKED** | 模型网关不可用 |
| L4 图片检索与插入 | **0/26 BLOCKED** | 未获外部图库授权 |
| **真实全链完成** | **0/26** | 与契约记录一致，未改善 |

根因单一且可验证：**本机不存在可用的语义模型网关**。这已由 socket 层与 HTTP 层双重探测、并由一次真实端到端生成调用（exit=4）证实。

---

## 1. 基线与边界遵守情况

### 1.1 基线记录

| 项 | 值 |
|---|---|
| 项目根 | `/Users/huangyingteng/WorkBuddy/2026-10-12-00-54-41/wengao-ppt` |
| 是否 Git 仓库 | **否**（根目录无 `.git`；仅 `vendor/luobo-ppt` 含 `.git`） |
| 候选 | `build/wengao-visual-v13-material-fix` |
| 范围契约 | `contracts/language_acceptance_scope_v1.json`，SHA `13f2191fbf060efddfe83fb335de939ec065b515cb31c2e00db8d82ceba220ef` |
| 契约 `scope_id` | `existing-matrix-26-v1`（与契约一致，未扩范围） |
| 基线快照 | `reports/p7_baseline/baseline_sha.json` |

> 因项目根非 Git 仓库，基线以 **SHA256 + mtime** 固化替代 git SHA。

### 1.2 边界遵守（逐条自检）

| 约束 | 遵守情况 |
|---|---|
| 只改自己工作包文件 | ✅ 仅新增 `scripts/p7_language_chain_audit.py` 与 `reports/p7_lang26/*` |
| 不覆盖他人未提交改动 | ✅ 未修改任何既有文件 |
| 不编辑 `workbench/server.py` | ✅ **全程未触碰**（见 §1.3） |
| 不切换/覆盖正式入口 8821 | ✅ 8821 全程未被占用、未被访问 |
| 不影响其他项目 | ✅ 仅读取系统与候选目录 |
| 不重启/改写共享服务 | ✅ 模型网关仅做只读 socket/HTTP 探测 |
| 不下载模型、不购买额度、不输出密钥 | ✅ 无 |
| 不用 fixture/mock/replay/旧任务顶替 | ✅ 见 §4 与 §7 |

### 1.3 并发冲突观察（重要）

`workbench/server.py` 在本次工作期间被**其他承接者**两次修改，本人未介入：

| 时点 | SHA256 | 字节数 |
|---|---|---|
| 基线（23:04:08） | `7baf415dc5da0cf819fd0d56d1c36198a5d756d3bbaddf9c8110938fcbac600a` | 130143 |
| 交付时（复测） | `450fcc855cec660bea1725ccf9321c784f9775469d198354ba0f54612684d911` | 133948 |

同时 `reports/` 下已存在 `p1-candidates`、其他工作包目录，佐证存在并行承接者。
**按任务书条款 1，本工作包对 `server.py` 的任何需求将以接口提案形式提交给该负责人串行集成，不会直接编辑。**

---

## 2. 改动文件清单（本工作包全部新增，无修改）

| 文件 | SHA256 | 字节 | 说明 |
|---|---|---|---|
| `scripts/p7_language_chain_audit.py` | `b3e4ae5ed2b90b6aa307a2982a56722c093380427833c24e9194cbdcb86a647d` | 18273 | 审计脚本（只读，不改产品行为） |
| `reports/p7_lang26/language_chain_audit.json` | `bb513b098c2f6bf7a734c2a3556ff52e869f7a8af5eaf8f21f9ee1edce2299cc` | 84015 | 机器可读矩阵证据（终态） |
| `reports/p7_lang26/P7_ACCEPTANCE_REPORT.md` | — | — | 本报告（**自指，SHA 不由本文件声明**，以文件系统为准） |
| `reports/p7_lang26/matrix_table.md` | `2ddef2e7086a6cb9281d6b952f9ba58f90b825f9fd2bd57d3c547794fdc0f7e6` | 3769 | 矩阵表格 |
| `reports/p7_lang26/delivery_sha.txt` | — | — | 输入/产物 SHA 清单 |
| `reports/p7_lang26/probe/*` | — | — | 真实端到端失败留证 |
| `reports/p7_baseline/baseline_sha.json` | — | — | 基线快照 |
| `reports/p7_lang26/P7_ACCEPTANCE_REPORT.md` | — | — | 本报告 |

---

## 3. 设计与接口说明

### 3.1 脚本设计

`scripts/p7_language_chain_audit.py` 是**只读审计器**，不改产品行为、不签发 verdict。

```
usage: python scripts/p7_language_chain_audit.py [--outdir DIR]
```

输出 JSON 结构（写入 `reports/p7_lang26/language_chain_audit.json`）：

```jsonc
{
  "package": "P7-26-language-real-chain-acceptance",
  "scope_contract_sha256": "...",          // 范围契约指纹，防范围漂移
  "environment_probes": {
    "model_gateway":  { "available": false, "endpoints": [...] },
    "renderer":       { "renderer_binary_present": true,  "found": [...] },
    "photo_library":  { "authorized": false, "status": "NOT_AUTHORIZED_IN_THIS_PACKAGE" }
  },
  "blockers": { "L2": "...", "L4": "...", ... },
  "summary":  { "L1_recognition_pass": "26/26", ... },
  "matrix":   [ { "language_code": "ar", "L1_text_recognition": "PASS", ... } ]
}
```

### 3.2 层判定规则（对应契约 `required_layers`）

| 层 | 判定方式 | 本轮结果依据 |
|---|---|---|
| L1 text_recognition | 调用 `wengao_ppt.language_support.detect_language_hint()` 对**真实材料**判定，与 manifest 声明语言比对 | 真实执行 |
| L2 semantic_planning | 需模型网关 | 探测 → BLOCKED |
| L3 visual_query_generation | 依赖 L2 | BLOCKED |
| L4 photo_retrieval_insertion | 需外部图库授权 | 探测 → BLOCKED |
| L5a 字体资源覆盖 | PIL 真实渲染判定（见 §3.3） | 真实执行 |
| L5b 产品排版链路 | 需真实生成产物 | BLOCKED |
| L6–L9 | 依赖 L6 生成产物 | BLOCKED |

### 3.3 L5a 检测方法与其方法学自检

**方法**：加载系统全部 370 个真实字体文件，用 PIL 对每个字符渲染，与必然缺失的 `U+FFFF` 渲染结果比对；mask 相同 or bbox 零面积 → 判为未覆盖。

**自检（防止"必然通过的标准答案"）**：

- 负向对照 `U+10FFFD` / `U+1D167` / `U+11F00` / `U+10F70` → 均判 **未覆盖** ✅
- `U+1E900`（Adlam）判为覆盖 → 经核对系统确有 `NotoSansAdlam-Regular.ttf`，**是真覆盖**，非误判
- 正向对照 `a`/`中`/`あ`/`한`/`ا`/`ক`/`த`/`త`/`ค`/`ह` → 均判 **覆盖** ✅

**重要限定**：L5a 只证明"系统存在可显示该字符的字体"，**不证明产品排版链路正确选用了该字体**。
因此 RTL 方向、孟加拉文/泰米尔文/泰卢固文复杂文字整形与换行、混排、长文本这些**重点项，本轮一律 BLOCKED**，不以 L5a 顶替。

---

## 4. 环境真实探测结果

### 4.1 模型网关 —— 不可用（根因）

| 端点 | TCP 连接 | HTTP |
|---|---|---|
| `127.0.0.1:11434` | `ConnectionRefusedError [Errno 61]` | `URLError [Errno 61]` |
| `127.0.0.1:18888` | `ConnectionRefusedError [Errno 61]` | `URLError [Errno 61]` |

- `lsof` 可见 pid 92300 名为 `ollama` 处于 LISTEN，但 **IPv4 `127.0.0.1`、IPv6 `::1`、`localhost` 三种解析全部拒绝连接**；经代理访问则统一返回 **502**。
- 已排除代理干扰：探测前清除 `HTTP(S)_PROXY`/`ALL_PROXY`，并改用原始 socket 验证。
- **未重启、未改写**任何共享 Ollama / 代理 / CLI。

### 4.2 渲染器

`/Applications/LibreOffice.app/Contents/MacOS/soffice` **存在**。但渲染需要上游生成产物（PPTX），本轮无产物，故未执行渲染（不做无产物渲染来凑数）。

### 4.3 图库授权

未发现任何已授权图库凭据（`PEXELS`/`UNSPLASH`/`PIXABAY`/`IMAGE_API`/`STOCK_API` 环境变量均为空）。按条款 2，**不自行发起外部图库请求、不付费**，故 L4 记 `NOT_AUTHORIZED_IN_THIS_PACKAGE`。

### 4.4 工作台（8823）

在线，`GET /api/bootstrap` → `200`，`label=DEVELOPMENT_CANDIDATE`，`release_ready=false`。
8821 未被占用、未被访问。

---

## 5. 真实端到端失败证据（非推断）

一次**真实**调用，未使用 `--replay-plan`（replay 属条款 3 明令禁止项）：

```bash
.venv/bin/python -m wengao_ppt.cli from-material \
  --input samples/open-source-50-decks/multilingual-inputs/L01-en.md \
  --outdir reports/p7_lang26/probe
```

| 字段 | 值 |
|---|---|
| 退出码 | **4** |
| `material_verdict` | **PLAN_FAILED** |
| `generated_pptx` | **null** |
| `model_error.code` | `MODEL.HTTP_ERROR` |
| `model_error.provider` | `ollama-local` |
| `model_error.status_code` | **502** |
| 请求模型 | `qwen3.8:27b-wb65536` |
| 输入 SHA256 | `16ecaa4b7e350f8b50a0233a4068bd4eb559b1278b0179c53cf6633e6e615e12`（`L01-en.md`） |
| 时间（UTC） | 2026-10-23T15:06:24Z（started→finished 同秒失败） |
| `facts_frozen` | 14（事实冻结成功，规划阶段失败） |

留证：`reports/p7_lang26/probe/run_record.json`、`probe_stdout.txt`、`probe_stderr.txt`。

---

## 6. 26 种语言 × 9 层验收矩阵

状态口径：`PASS` = 有真实执行证据；`BLOCKED` = 依赖缺失，无证据；**不出现由 fixture 推出的 PASS**。

| 语言 | 材料数 | RTL | CJK | L1 识别 | L2 语义 | L3 视觉查询 | L4 图片插入 | L5 排版 | L6 生成 | L7 渲染 | L8 预览 | L9 下载 | 全链 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `ar` | 4 | 是 | - | PASS | BLOCKED | BLOCKED | BLOCKED | **BLOCKED**(L5a PASS 1.0) | BLOCKED | BLOCKED | BLOCKED | BLOCKED | **否** |
| `bn` | 4 | - | - | PASS | BLOCKED | BLOCKED | BLOCKED | **BLOCKED**(L5a PASS 1.0) | BLOCKED | BLOCKED | BLOCKED | BLOCKED | **否** |
| `de` | 1 | - | - | PASS | BLOCKED | BLOCKED | BLOCKED | **BLOCKED**(L5a PASS 1.0) | BLOCKED | BLOCKED | BLOCKED | BLOCKED | **否** |
| `en` | 4 | - | - | PASS | BLOCKED | BLOCKED | BLOCKED | **BLOCKED**(L5a PASS 1.0) | BLOCKED | BLOCKED | BLOCKED | BLOCKED | **否** |
| `es` | 4 | - | - | PASS | BLOCKED | BLOCKED | BLOCKED | **BLOCKED**(L5a PASS 1.0) | BLOCKED | BLOCKED | BLOCKED | BLOCKED | **否** |
| `fa` | 1 | 是 | - | PASS | BLOCKED | BLOCKED | BLOCKED | **BLOCKED**(L5a PASS 1.0) | BLOCKED | BLOCKED | BLOCKED | BLOCKED | **否** |
| `fr` | 4 | - | - | PASS | BLOCKED | BLOCKED | BLOCKED | **BLOCKED**(L5a PASS 1.0) | BLOCKED | BLOCKED | BLOCKED | BLOCKED | **否** |
| `hi` | 4 | - | - | PASS | BLOCKED | BLOCKED | BLOCKED | **BLOCKED**(L5a PASS 1.0) | BLOCKED | BLOCKED | BLOCKED | BLOCKED | **否** |
| `id` | 1 | - | - | PASS | BLOCKED | BLOCKED | BLOCKED | **BLOCKED**(L5a PASS 1.0) | BLOCKED | BLOCKED | BLOCKED | BLOCKED | **否** |
| `it` | 1 | - | - | PASS | BLOCKED | BLOCKED | BLOCKED | **BLOCKED**(L5a PASS 1.0) | BLOCKED | BLOCKED | BLOCKED | BLOCKED | **否** |
| `ja` | 1 | - | 是 | PASS | BLOCKED | BLOCKED | BLOCKED | **BLOCKED**(L5a PASS 1.0) | BLOCKED | BLOCKED | BLOCKED | BLOCKED | **否** |
| `ko` | 1 | - | 是 | PASS | BLOCKED | BLOCKED | BLOCKED | **BLOCKED**(L5a PASS 1.0) | BLOCKED | BLOCKED | BLOCKED | BLOCKED | **否** |
| `ms` | 1 | - | - | PASS | BLOCKED | BLOCKED | BLOCKED | **BLOCKED**(L5a PASS 1.0) | BLOCKED | BLOCKED | BLOCKED | BLOCKED | **否** |
| `nl` | 1 | - | - | PASS | BLOCKED | BLOCKED | BLOCKED | **BLOCKED**(L5a PASS 1.0) | BLOCKED | BLOCKED | BLOCKED | BLOCKED | **否** |
| `pl` | 1 | - | - | PASS | BLOCKED | BLOCKED | BLOCKED | **BLOCKED**(L5a PASS 1.0) | BLOCKED | BLOCKED | BLOCKED | BLOCKED | **否** |
| `pt` | 4 | - | - | PASS | BLOCKED | BLOCKED | BLOCKED | **BLOCKED**(L5a PASS 1.0) | BLOCKED | BLOCKED | BLOCKED | BLOCKED | **否** |
| `ru` | 1 | - | - | PASS | BLOCKED | BLOCKED | BLOCKED | **BLOCKED**(L5a PASS 1.0) | BLOCKED | BLOCKED | BLOCKED | BLOCKED | **否** |
| `sw` | 1 | - | - | PASS | BLOCKED | BLOCKED | BLOCKED | **BLOCKED**(L5a PASS 1.0) | BLOCKED | BLOCKED | BLOCKED | BLOCKED | **否** |
| `ta` | 1 | - | - | PASS | BLOCKED | BLOCKED | BLOCKED | **BLOCKED**(L5a PASS 1.0) | BLOCKED | BLOCKED | BLOCKED | BLOCKED | **否** |
| `te` | 1 | - | - | PASS | BLOCKED | BLOCKED | BLOCKED | **BLOCKED**(L5a PASS 1.0) | BLOCKED | BLOCKED | BLOCKED | BLOCKED | **否** |
| `th` | 1 | - | - | PASS | BLOCKED | BLOCKED | BLOCKED | **BLOCKED**(L5a PASS 1.0) | BLOCKED | BLOCKED | BLOCKED | BLOCKED | **否** |
| `tr` | 1 | - | - | PASS | BLOCKED | BLOCKED | BLOCKED | **BLOCKED**(L5a PASS 1.0) | BLOCKED | BLOCKED | BLOCKED | BLOCKED | **否** |
| `uk` | 1 | - | - | PASS | BLOCKED | BLOCKED | BLOCKED | **BLOCKED**(L5a PASS 1.0) | BLOCKED | BLOCKED | BLOCKED | BLOCKED | **否** |
| `ur` | 1 | 是 | - | PASS | BLOCKED | BLOCKED | BLOCKED | **BLOCKED**(L5a PASS 1.0) | BLOCKED | BLOCKED | BLOCKED | BLOCKED | **否** |
| `vi` | 1 | - | - | PASS | BLOCKED | BLOCKED | BLOCKED | **BLOCKED**(L5a PASS 1.0) | BLOCKED | BLOCKED | BLOCKED | BLOCKED | **否** |
| `zh` | 4 | - | 是 | PASS | BLOCKED | BLOCKED | BLOCKED | **BLOCKED**(L5a PASS 1.0) | BLOCKED | BLOCKED | BLOCKED | BLOCKED | **否** |

**汇总**

| 指标 | 结果 |
|---|---|
| 范围内语言数 | 26（严格等于契约 `acceptance_language_codes`，未扩范围） |
| L1 识别 PASS | 26/26 |
| L5a 字体资源覆盖 PASS | 26/26 |
| L5b 产品排版链路 PASS | **0/26** |
| L2/L3/L6/L7/L8/L9 PASS | **0/26** |
| L4 图片插入 PASS | **0/26** |
| **真实全链完成** | **0/26** |

**重点项单独覆盖情况**（任务书点名）

| 重点项 | 语言 | 本轮状态 |
|---|---|---|
| RTL（阿拉伯语/波斯语/乌尔都语） | `ar` `fa` `ur` | L1 PASS；字体资源可覆盖；**RTL 方向/整形/换行渲染 = BLOCKED（无产物）** |
| 孟加拉文复杂文字 | `bn` | L1 PASS；L5a PASS；**复杂文字整形与换行渲染 = BLOCKED** |
| 泰米尔/泰卢固字体覆盖 | `ta` `te` | L1 PASS；L5a PASS（覆盖率 1.0，字符集 38/44）；**产品排版渲染 = BLOCKED** |
| 混排、长文本 | 全部 26 | **BLOCKED**（需真实生成产物才能判定） |

---

## 7. 反作弊声明（对应条款 3）

本工作包**未**使用以下任何方式产出 PASS：

1. 未使用模型列表或 `/api/tags` 作为完成证据 —— `/api/tags` 仅用于连通性探测，且探测结果是**失败**，反而构成阻断证据；
2. 未使用 fixture 计入真实完成数 —— 既有 `test_multilingual_*` 的 18 项通过仅作为**回归无破坏**的旁证，**未**计入任何语言的完成数；
3. 未使用 mock / replay —— 真实调用中**未**传 `--replay-plan`；
4. 未重放旧任务、未引用作者自报 PASS；
5. 未降低字号/遮挡/缺图标准、未放宽阈值、未缩小覆盖分母 —— 分母恒为契约固定的 26；
6. 未把正文移入备注制造 PASS。

外部调用记录：本轮**唯一**的真实外部模型调用即 §5 那一次，provider=`ollama-local`、model=`qwen3.8:27b-wb65536`、结果=`FAILED`；无图片库调用；日志不含密钥或私人内容。

---

## 8. 未解决风险与解锁条件

| # | 风险 | 影响 | 解锁条件（最小可操作） |
|---|---|---|---|
| R1 | 无可用语义模型网关（11434/18888 均拒绝） | L2/L3/L6–L9 全部 26 语言不可验 | 由**共享服务负责人**启动/修复本地网关；或用户提供已授权的外部模型端点与凭据。**本工作包不得自行重启或改写共享 Ollama**（条款 2） |
| R2 | 图库未授权 | L4 全部不可验 | 用户提供已授权图库 provider、凭据、费用与数据去向说明，并确认许可条款 |
| R3 | 无生成产物 → 渲染/预览/下载不可验 | L5b/L7/L8/L9 | 依赖 R1 解除后重跑生成 |
| R4 | `workbench/server.py` 被并行修改（130143→133948 字节） | 集成冲突风险 | 若本包后续需接口改动，以**补丁提案**形式交该负责人串行集成 |
| R5 | 契约 `source_path` 指向外部 Codex 相对路径 | 可移植性风险；本机 50/50 文件实际存在于候选目录内 | 建议后续修正 manifest 为相对路径（本轮未改动，避免影响他人） |

---

## 9. 可复现命令（独立验收者请用）

```bash
cd /Users/huangyingteng/WorkBuddy/2026-10-12-00-54-41/wengao-ppt/build/wengao-visual-v13-material-fix

# 1) 复跑 26 语言链路审计（约 5 秒）
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/p7_language_chain_audit.py

# 2) 复现真实端到端失败（预期 exit=4）
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m wengao_ppt.cli from-material \
  --input samples/open-source-50-decks/multilingual-inputs/L01-en.md \
  --outdir /tmp/p7_reverify; echo "EXIT=$?"

# 3) 复现模型网关探测
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python - <<'PY'
import socket
for p in (11434,18888):
    s=socket.socket(); s.settimeout(3)
    try: s.connect(("127.0.0.1",p)); print(p,"OK")
    except Exception as e: print(p,"FAIL",e)
    finally: s.close()
PY

# 4) 回归既有测试（预期 18 passed, 65 subtests passed）
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest \
  tests/test_multilingual_26_acceptance_gate.py tests/test_multilingual_scene_acceptance.py -q
```

**本轮实际测试输出**

```
tests/test_multilingual_26_acceptance_gate.py  →  4 passed in 1.02s
tests/test_multilingual_scene_acceptance.py    →  14 passed, 65 subtests passed in 1.39s
合并复跑                                        →  18 passed, 65 subtests passed in 2.10s
```

以上既有测试通过**仅表示本工作包未造成回归**，按其性质属 fixture/样本级验证，**不计入 26 语言的真实完成数**。

**本工作包已完成的复现性自检**：连续两次执行审计脚本，对输出 JSON 做逐字段 diff，
除顶层 `generated_at_utc` 与各 probe 的 `checked_at_utc` 时间戳外**无任何字段差异**，
即矩阵结论可稳定复现。产品侧文件 `contracts/language_acceptance_scope_v1.json`
（`13f2191fbf060efd…`）与 `workbench/make_flow.py`（`15d92bba43b32ab0…`）SHA 与基线一致，
证实本工作包未改动任何既有文件。

---

## 10. 交付声明

- 本工作包**不宣告**任何语言全链通过，亦**不宣告**项目完成。
- 真实全链结果：**0/26**，与范围契约既有记录一致，本轮**无改善**（根因在环境依赖，非代码）。
- 按任务书条款 5，本交付须由**独立验收者**按 §9 命令复跑确认。
