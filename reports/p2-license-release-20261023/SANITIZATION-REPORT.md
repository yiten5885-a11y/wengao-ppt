# P2 发布消毒与扫描审计报告

生成时间：2026-10-24 00:3x (+08:00)
源候选：`build/wengao-visual-v13-material-fix`
副本输出：`build/release-sanitized-20261023/wengao-ppt`
工具：`scripts/p2_release_sanitizer.py`（只读扫描）、`scripts/p2_build_release_copy.py`（生成 + 复扫）

> 一句话结论：**副本侧干净（0 个 must_fix 残留），但源候选侧仍有 20 个文件残留本机绝对路径，未修**。
> 副本干净**不等于**源倉干净；源侧问题的修复责任在各自文件所属的工作包，本轮不代改他人文件。

---

## 1. 基线与产物身份

| 项 | 值 |
| :--- | :--- |
| 源候选成员数（排除 `__pycache__/.pytest_cache/.venv/.git`） | **2594** |
| 源候选树摘要 | `061d65cce833209d70e87de74049eaea5417e3a13423733e6e320c4d231d4334`（`BASELINE_SHAS.txt` / `baseline_tree.json`） |
| 副本成员数（manifest 条目） | **232** |
| 副本 manifest 聚合摘要 | `8909e467912a9800de2ba24eb195b82037c0d3cee8531caf9d2fec0d55abfdb9` |
| 额外生成文档 | `ASSET-EXCLUSIONS.md`（不在 manifest 内，独立复核时作为已知差异列出） |
| 被排除成员 | **31**（10 张图片资产 + 21 个源码/测试/工具文件） |
| 内容脱敏处数 | **1** |

---

## 2. 政策（显式清单，不用通配掩盖）

**结构性排除目录**：`jobs`、`reports`、`material_tasks`、`wb_loopback_data`、`workbench_previews`、
`workbench_uploads`、`.wengao_scene_assets`、`.fontconfig`、`.lo-profile`、`versions`、`rollback-20261021-0908`、
`__pycache__`、`.git`、`.hg`、`.svn`、`.venv`、`venv`、`env`、`node_modules`、`.pytest_cache`、`.mypy_cache`、
`.ruff_cache`、`.ipynb_checkpoints`。

**结构性排除后缀**：`.log .pyc .pyo .tmp .sqlite .sqlite3 .db .swp .pid .lock`；
**排除文件**：`.DS_Store`、`probe.txt`、`Thumbs.db`、`workbench_server.log`、`workbench_server_8823.log`。

该政策与 `wengao_ppt/install.py` 的发布载荷政策同源（并额外覆盖了一次性开发与审计脚本）。

---

## 3. 逐文件的额外排除（21 项，含理由）

| 相对路径 | 源 sha256（前 12） | 理由 |
| :--- | :--- | :--- |
| `scripts/inspect_r3*.py`（11 个） | `9daf2d6ea2b7…` 等 | dev-only：文件头写死本机候选绝对路径 |
| `scripts/inspect_baseline_style.py` | `3aabae70a258…` | dev-only：一次性版式检查脚本 |
| `scripts/run_gate_13p.py` | `fff25395c777…` | dev-only：写死外部监督工作区绝对路径 |
| `scripts/run_gate_negative_cases.py` | `a7c415e1e7c8…` | dev-only：写死外部监督工作区绝对路径 |
| `scripts/run_negative_regression.py` | `ab8b7c8dc7af…` | dev-only：写死外部监督工作区绝对路径 |
| `scripts/run_p0_final_gate.py` | `01791cfc27bf…` | dev-only：一次性门禁脚本，含本机绝对路径 |
| `scripts/p2_release_sanitizer.py` | `8464f72aae50…` | P2 打包审计工具，不属于用户运行载荷 |
| `scripts/p2_build_release_copy.py` | `d6dbd7310990…` | 同上 |
| `scripts/p2_build_license_matrix.py` | `5124de5ff2d3…` | 同上 |
| `tests/test_multilingual_26_acceptance_gate.py` | `10e8fcca00b5…` | 第 45 行硬编码 `/Users/<owner>/Documents/Codex/...`；源侧问题已上报该文件所属工作包 |

**覆盖差异（照实写）**：副本因此**少一个测试用例**（26 语言名单范围 + 50 份材料门禁）。若要在副本里跑它，需由该测试所属工作包先把硬编码路径改成相对路径或环境变量。这不是“顺手清理”，而是一次明确登记的差异。

---

## 4. 被排除的图片资产（10 张）

5 张 AI 生成 PNG（`15e43e0cf78e…` 等）＋ 5 张 Pexels JPEG（`3ab2b539e4e1…` 等），
理由分别见 `LICENSE-MATRIX.md` 的 BLK-02 与 BLK-03。副本内 `ASSET-EXCLUSIONS.md` 有完整表格与源哈希。

**已知后果（不是可以静默忽略的差异）**：`wengao_ppt/visual_plan.py` 的旧关键词素材库按绝对路径读这 5 张
PNG；资产缺失时该入口在读文件处会抛 `FileNotFoundError`。这是**静态核读**结论，没有在副本上真实跑到那一步
（本机无可用本机模型、无 LibreOffice）。运行时图库本应由用户自备素材或经授权的检索链路提供。

---

## 5. 内容脱敏台账（1 处）

| 文件 | 原片段 | 替换为 | 次数 | 源 sha256 | 副本 sha256 |
| :--- | :--- | :--- | ---: | :--- | :--- |
| `samples/.../generation-evidence/batch-25-28-language-reclassification.json` | `/Users/huangyingteng/Documents/Codex/2026-10-20/n-2` | `<REDACTED_LOCAL_PATH>` | 1 | `1f789cfad200…` | `314048c55044…` |

替换是确定性的，验收者可用同一脚本重跑比对前后哈希。**源候选没有被改动**，权威证据仍在源候选里。

---

## 6. 扫描结果

### 6.1 源候选（`scripts/p2_release_sanitizer.py --root <候选>`）

见 `scan_raw.json`：`must_fix 23`、`manual_review 9`。（本轮基线时刻的快照；之后他人又新增了含路径的脚本，
因此实际残留数已上升，见第 7 节。）

### 6.2 副本（生成后自动复扫）

```
copy files scanned   : 233
copy rescan must_fix : 0
copy rescan manual   : 6
copy rescan allowed  : 4
```

4 条白名单项（均已人工逐条确认，且**要求片段仍存在于文件中，否则自动失效**）：

| 文件:行 | 规则 | 为什么允许 |
| :--- | :--- | :--- |
| `tests/test_scene_images_pexels.py:48 / :102` | `api_key_assign` | 字符串自述 `test-only-not-a-real-api-key`，是占位符不是凭证 |
| `tests/test_semantic_scenes.py:1114` | `abs_user_path` | `/Users/example/...` 是文档示例占位符 |
| `wengao_ppt/scene_model.py:68` | `abs_private_tmp` | 那是把本机路径从报错文本里抹掉的**脱敏正则** |

6 条 `manual_review`（未清为零，逐条列明）：

| 文件 | 规则 | 判定 |
| :--- | :--- | :--- |
| `wengao_ppt/scene_images.py:1023 / :1069` | `email` | User-Agent 里写的是 `https://wengao.local; team@wengao.local`，**不是真实可达联系方式**。这是所有者待办：对外发布前应换成真实联系地址（很多图库 API 要求 UA 可联系） |
| `tests/test_scene_images_pexels.py:42` | `email` | `https://user@www.pexels.com/...` 形式的 URL 夹具，属于测试用构造串 |
| `tests/test_scene_provider_transport.py:141` | `email` | `https://user:pw@example.com/x.jpg`，保留示例域名，非真实凭证 |
| `scripts/p7_language_chain_audit.py:48` | `home_tilde_note` | `~/Library/Fonts` 是本機字体发现路径，正常 |
| `vendor/luobo-ppt/scripts/common.py:248` | `home_tilde_note` | 上游字体发现逻辑，正常 |

### 6.3 独立复核（另一份实现，避免“同一套规则自证”）

```
manifest_entries        : 232
sha_mismatch_or_missing : 0
unlisted_extra_files    : 1  ('ASSET-EXCLUSIONS.md' — 生成文档，已知)
--- independent sensitive scan on the COPY ---
  /Users/huangyingteng        : 0
  /WorkBuddy/2026-10-12-00-54-41 : 0
  /Documents/Codex            : 0
  BEGIN * PRIVATE KEY         : 0
  gh_token_* tokens           : 0
  cn_id_18                    : 1  ← 人工复核为**误报**：命中内容是
     `historical_generation_evidence_sha256` 的 64 位十六进制摘要
     （`31a0081da1e5…4e9`）的前 18 位恰好全为数字，不是身份证号
```

> 复核口径：上面的独立扫描是**另一份实现**（不走 `RULES` 常量），目的是避免"同一套规则自己给自己放行"。
> 唯一报出的 1 条命中已人工判定为误报，判定依据写在上面；没有通过放宽规则来清零。

### 6.4 结构与冒烟

| 检查 | 结果 |
| :--- | :--- |
| 副本内 `jobs/ reports/ wb_loopback_data/ material_tasks/ .venv/ .git/ __pycache__/ .pytest_cache/ workbench_uploads/ workbench_previews/ .fontconfig/` | 全部 **0 个** |
| 副本内 `.log / .pyc / .DS_Store` | **0 个** |
| CLI 可用性（副本目录内，候选 venv） | `python -m wengao_ppt.cli --help` → exit 0；`--version` → `0.1.0` |
| 全量语法编译 `compileall -x "(vendor|\.git|__pycache__)"` | exit 0，无语法错误 |
| `pytest tests/test_install_sanitizer…`（候选侧打包消毒测试） | **5 passed, 16 subtests passed in 0.32s** |

> 说明：冒烟时用 `compileall` 会写入 `__pycache__`，已随后清理；清理后复核 `pycache=0 / pyc=0`，副本文件数回到 **233**。

---

## 7. 源侧仍未修的问题（不在本轮修改范围，需各自归属方处理）

| # | 文件 | 位置 | 问题 | 归属 |
| :--- | :--- | :--- | :--- | :--- |
| 1 | `scripts/inspect_r3*.py`（11 个） | 文件头 | 写死 `/Users/<owner>/WorkBuddy/.../wengao-visual-v13-material-fix` | 脚本作者 |
| 2 | `scripts/run_gate_13p.py`、`run_gate_negative_cases.py`、`run_negative_regression.py`、`run_p0_final_gate.py` | 常量 | 写死 `/Users/<owner>/Documents/Codex/2026-10-20/n-2/...` | 门禁脚本作者 |
| 3 | `tests/test_multilingual_26_acceptance_gate.py` | 第 45 行 | 候选清单里写死外部工作区备份路径 | 该测试所属工作包 |
| 4 | `samples/.../generation-evidence/batch-25-28-language-reclassification.json` | 第 6 行 | 同上（本轮仅在副本中脱敏，源侧未改） | 该证据所属工作包 |
| 5 | `wengao_ppt/scene_images.py` | 1023 / 1069 行 | User-Agent 使用不可达域名 `wengao.local` 作为联系方式 | 所有者/该产品负责人 |

这些文件本身是否也需要在发布包里出现，由各自工作包决定：本轮的策略是**开发期脚本不进发布载荷**，
但如果某个脚本是验收链路的一部分，那么正确的做法是**改掉硬编码路径**（改用相对路径或环境变量），而不是拷一份带本机路径的副本对外发布。

---

## 8. 可复现命令

```bash
ROOT=<候选目录>
PY=/Users/huangyingteng/.workbuddy/binaries/python/envs/default/bin/python

# ① 只读扫描源候选
"$PY" "$ROOT/scripts/p2_release_sanitizer.py" --root "$ROOT" \
      --json-out "$ROOT/reports/p2-license-release-20261023/scan_raw.json"

# ② 预览副本计划（不写盘）
"$PY" "$ROOT/scripts/p2_build_release_copy.py" --src "$ROOT" \
      --out <副本目录> --report "$ROOT/reports/p2-license-release-20261023"

# ③ 真正生成并自动复扫（must_fix>0 时退出码 2，失败关闭）
"$PY" "$ROOT/scripts/p2_build_release_copy.py" --src "$ROOT" \
      --out <副本目录> --report "$ROOT/reports/p2-license-release-20261023" --apply

# ④ 内部版（保留未清许可资产，**不得**用于对外发布）
#    在上面 ③ 的命令后追加 --include-unverified-assets
```

副本的详细机器可读记录：`release_copy_build.json`；副本逐文件哈希：`RELEASE_COPY_SHAS.txt`。
