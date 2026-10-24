# 工作包 12｜P2 许可证、素材来源与中英文发布说明 —— 交付报告

- 承接者：P2 工作包（本轮）
- 目标候选：`build/wengao-visual-v13-material-fix`
- 基线时刻：2026-10-24 00:12 (+08:00)（在此之前先做基线 SHA 盘点）
- 报告位置：本文件 + `LICENSE-MATRIX.md` + `SANITIZATION-REPORT.md` + `SOURCE_INVENTORY.json` + `release_copy_build.json`
- **本轮没有修改任何不属于本工作包的代码文件**；特别地，`workbench/server.py`、`wengao_ppt/*.py`（除读取外）、
  `tests/*`（他人文件未被修改）、`contracts/*`、`vendor/*` 一律未改动。

---

## 1. 基线（动手前先上锁）

源候选中**没有 git 仓库**（`git rev-parse` 失败：`fatal: not a git repository`），因此基线改用
**文件级 SHA256 清单**。

| 项 | 值 |
| :--- | :--- |
| 源候选根目录 | `/Users/huangyingteng/WorkBuddy/2026-10-12-00-54-41/wengao-ppt/build/wengao-visual-v13-material-fix` |
| 成员数（排除 `__pycache__ / .pytest_cache / .venv / .git`） | **2594** |
| 树摘要 | `061d65cce833209d70e87de74049eaea5417e3a13423733e6e320c4d231d4334` |
| 清单文件 | `BASELINE_SHAS.txt`（sha256 / 字节 / mtime / 相对路径）、`baseline_tree.json` |

并发情况：动手前确认其他工作包正在改 `wengao_ppt/scene_model.py`、`scene_images.py`、`semantic_scenes.py`、
`tests/test_multilingual_scene_acceptance.py`、`workbench/server.py`、`scripts/layout_gate.py` 等。
本轮因此只在**自己的文件**上落笔（README / README_EN / SKILL / THIRD-PARTY-NOTICES / LICENSE-STATUS 的
取代标注 / 3 个 `scripts/p2_*.py` / `reports/p2-license-release-20261023/`）。
他人文件中的缺陷（本机绝对路径残留）**只上报、不代改**，见第 7 节。

---

## 2. 改动文件清单（修改前 / 修改后 SHA256，前 16 位）

| 文件 | 修改前 (before_p2/) | 修改后 | 字节 前→后 | 说明 |
| :--- | :--- | :--- | ---: | :--- |
| `README.md` | `a129ea2dc8019e92` | `bc6bd8409c7329bf` | 10480 → 22740 | 补齐全部必需章节；保留原有全部事实；命令分级标记 |
| `README_EN.md` | `8853b83870cd624e` | `1d8c715f7a25655e` | 8337 → 21661 | 与中文版同构的英文版 |
| `SKILL.md` | `fd45e0eba0050533` | `88c772a931385597` | 6494 → 10554 | 补 `slug/version/displayName/summary/category/tags` 等元数据与上架检查 |
| `LICENSE-STATUS.md` | `a353b9b340c9b6e8` | `d6aa46826cd46f6c` | 4167 → 4969 | **保留原文**，仅加"已被取代"横幅并列出三处实测不一致 |

修改前版本已备份在 `before_p2/`（4 个文件），diff 可逐字复核。

## 2.1 新增文件

| 文件 | sha256（前 16） | 字节 | 用途 |
| :--- | :--- | ---: | :--- |
| `THIRD-PARTY-NOTICES.md` | `48377f2c12a960e9` | 6362 | 第三方许可与义务清单（面向最终用户） |
| `scripts/p2_release_sanitizer.py` | `8464f72aae509410` | 9786 | 只读静态扫描：结构 + 内容规则 |
| `scripts/p2_build_release_copy.py` | `d6dbd7310990ad0c` | 18500 | 生成消毒副本 + 排除/脱敏台账 + 副本复扫 |
| `scripts/p2_build_license_matrix.py` | `5124de5ff2d394c1` | 27151 | 现场实测生成许可证矩阵与来源清单 |
| `reports/.../BASELINE_SHAS.txt` | `4504fed97fb72871` | 458478 | 2594 个成员的基线哈希清单 |
| `reports/.../baseline_tree.json` | `477fa55586d4df83` | 267 | 树摘要与生成时间 |
| `reports/.../scan_raw.json` | `a574ae50bfb61faf` | 14962 | 源候选扫描原始结果（最终一次运行） |
| `reports/.../SOURCE_INVENTORY.json` | `b766a77dd3a400eb` | 24220 | 来源清单（机读，最终一次运行） |
| `reports/.../LICENSE-MATRIX.md` | `62349bdb804702a7` | 12814 | **许可证矩阵主文档** |
| `reports/.../SANITIZATION-REPORT.md` | `f1838264a8bdb8b9` | 10735 | 消毒与扫描审计报告 |
| `reports/.../release_copy_build.json` | `6518f21c6f1260fe` | 84466 | 副本构建台账（排除/脱敏/复扫） |
| `reports/.../RELEASE_COPY_SHAS.txt` | `58efc7dec9697050` | 28284 | 副本逐文件哈希（验收者可用） |
| `reports/.../pypi_evidence/*.json` | — | — | 7 份依赖元数据原始响应（含响应 sha256） |
| `build/release-sanitized-20261023/wengao-ppt/` | 副本 manifest `8909e467912a9800…` | 233 文件 | **可发布消毒副本** |

---

## 3. 设计 / 接口说明

### 3.1 `p2_release_sanitizer.py`（只读扫描器）

- 输入：`--root <目录>`；输出：`--json-out <文件>`，同时打印摘要。
- 规则：12 条显式规则，分级 `must_fix`（本机绝对路径、私钥 PEM、Bearer、`sk-`/AKIA、
  键值赋值的疑似凭证）与 `manual_review`（邮箱、手机号、`~/Library` 简写、代理环境变量）。
- 只扫文本后缀（`.py .md .json .html .sh .csv …`），二进制文件单独计数而不是"当作通过"。
- 退出码：`0` 无命中 / `2` 有 must_fix / `3` 仅 manual / `4` 根目录不存在。
- 设计取舍：规则写在模块常量 `RULES` 里供副本生成器**复用同一套规则**，避免两边漂移。

### 3.2 `p2_build_release_copy.py`（副本生成器）

- 输入：`--src --out --report [--include-unverified-assets] [--apply]`；默认只预览，`--apply` 才写盘。
- 三步：① 按 `EXTRA_EXCLUDE_FILES`（逐文件 + 理由）与 `UNVERIFIED_ASSETS`（逐张资产 + 理由）过滤；
  ② 对必须进副本但带本机路径的文件做**确定性替换**，记入 `redaction_ledger`（含前后 sha256）；
  ③ 复制完成后用上面同一套规则**对副本本体再扫一遍**，出现 `must_fix` 立即退出码 2（失败关闭）。
- 白名单机制（`ALLOWLIST`）：要求"规则名 + 片段仍存在于文件中"，一旦对方改行就自动失效回到 must_fix，
  不做永久豁免；白名单条目在报告里**仍然列出**，不隐藏。
- 目标已存在时拒绝覆盖（`OUT_EXISTS_REFUSING_TO_OVERWRITE`），避免把上一版半吊子产物当新产物。

### 3.3 `p2_build_license_matrix.py`（矩阵生成器）

- 输入：`--root --out`；产出 `SOURCE_INVENTORY.json` + `LICENSE-MATRIX.md`。
- **脚本内不写死任何哈希或版本号**：上游 HEAD/tree/tracked/remote 现场 `git` 实测并与契约声明比对；
  依赖版本取自三口径（候选 venv 的 dist-info 目录名、本机受管环境 `importlib.metadata`、PyPI 原始响应）；
  图片资产逐个重算 sha256 并与资产清单自述值比对；PNG 元数据块逐个解析；字体文件全仓扫描。
- 测不到的一律写 `NOT_MEASURED`，不猜；法律性质判断不输出，只给事实与阻断编号 BLK-01…06。

---

## 4. 可复现命令

```bash
ROOT=/Users/huangyingteng/WorkBuddy/2026-10-12-00-54-41/wengao-ppt/build/wengao-visual-v13-material-fix
PY=/Users/huangyingteng/.workbuddy/binaries/python/envs/default/bin/python
OUT=$ROOT/reports/p2-license-release-20261023
COPY=/Users/huangyingteng/WorkBuddy/2026-10-12-00-54-41/wengao-ppt/build/release-sanitized-20261023/wengao-ppt
cd "$ROOT"

# ① 基线
"$PY" - <<'EOF'   # 见附录 A：生成 BASELINE_SHAS.txt / baseline_tree.json
EOF

# ② 许可证矩阵与来源清单（需先拉 PyPI 元数据，见 LICENSE-MATRIX.md 第 5 节）
"$PY" scripts/p2_build_license_matrix.py --root . --out "$OUT"

# ③ 源候选只读扫描
"$PY" scripts/p2_release_sanitizer.py --root . --json-out "$OUT/scan_raw.json"

# ④ 生成消毒副本（预览 → 确认 → 写盘）
"$PY" scripts/p2_build_release_copy.py --src . --out "$COPY" --report "$OUT"
"$PY" scripts/p2_build_release_copy.py --src . --out "$COPY" --report "$OUT" --apply

# ⑤ 候选可选：仅安装打包相关的既有测试
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_install_sanitization.py -q -p no:cacheprovider
```

---

## 5. 测试输出（真实执行，非 fixture / 非自报）

### 5.1 既有打包消毒测试（候选自带 venv，Python 3.13）

```
$ PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_install_sanitization.py -q -p no:cacheprovider
.....                                                    [100%]
5 passed, 16 subtests passed in 0.32s
EXIT=0
```

### 5.2 SKILL.md front-matter 可解析性（装了 PyYAML 后实跑）

```
YAML_OK keys= ['category','description','displayName','homepage','license','name','slug','summary','tags','version']
missing_required= []
version= 0.1.0-dev-candidate | license= PENDING_OWNER_DECISION
tags= ['PPTX','演示文稿','本地模型','独立验收','多语言','场景配图','中文排版']
desc_len= 257 summary_len= 48
```

### 5.3 副本构建 + 自动复扫

```
== BUILD SUMMARY ==
mode                 : apply
copied / skipped     : 232 / 31
redactions applied   : 1
copied_manifest_sha  : 8909e467912a9800de2ba24eb195b82037c0d3cee8531caf9d2fec0d55abfdb9
copy files scanned   : 233
copy rescan must_fix : 0
copy rescan manual   : 6
copy rescan allowed  : 4
   [ALLOWED] tests/test_scene_images_pexels.py:48  …
   [ALLOWED] tests/test_scene_images_pexels.py:102 …
   [ALLOWED] tests/test_semantic_scenes.py:1114     …
   [ALLOWED] wengao_ppt/scene_model.py:68           …
EXIT=0
```

### 5.4 副本结构与冒烟

```
违规目录 jobs/reports/wb_loopback_data/material_tasks/.venv/.git/__pycache__/.pytest_cache/
         workbench_uploads/workbench_previews/.fontconfig  → 全部 0 个
.log/.pyc/.DS_Store                                        → 0 个
CLI （副本目录内）: python -m wengao_ppt.cli --help  → exit 0
                    python -m wengao_ppt.cli --version → 0.1.0
compileall -x "(vendor|\.git|__pycache__)"            → exit 0（无语法错误）
副本文件数（清理 compileall 产生的 __pycache__ 后）  → 233
```

### 5.5 独立复核（换一份实现，避免同一套规则自证）

```
FINAL VERIFY on COPY
manifest_entries          : 232
sha_mismatch_or_missing   : 0
unlisted_extra_files      : 1  ('ASSET-EXCLUSIONS.md' — 生成文档，已知差异)
text_files_scanned        : 215
/Users/huangyingteng        : 0
/WorkBuddy/2026-10-12-00-54-41 : 0
/Documents/Codex            : 0
BEGIN * PRIVATE KEY         : 0
gh_token_* tokens           : 0
cn_id_18                    : 1  ← 人工复核为**误报**：命中内容是
                                     historical_generation_evidence_sha256 的
                                     64 位十六进制摘要，前 18 位恰好全为数字
副本与源的 README/README_EN/SKILL/LICENSE-STATUS/THIRD-PARTY-NOTICES/requirements 全部 SAME（字节一致）
```

### 5.6 许可证矩阵生成

```
wrote: reports/p2-license-release-20261023/SOURCE_INVENTORY.json
wrote: reports/p2-license-release-20261023/LICENSE-MATRIX.md
upstream head: b6dcb0f325b3ea38a7ca3cd3c211bbba7fb436b2
assets ai_png: 5 scene_manifest: 5
```

---

## 6. 输入 / 源码 / 构建 / 产物 身份

| 类别 | 对象 | 标识 |
| :--- | :--- | :--- |
| 输入（源候选） | 2594 个成员 | 树摘要 `061d65cce833…` |
| 上游依赖 | luobo-ppt @ `b6dcb0f325b3…` / tree `ed28a29ddfd5…` / 76 文件 | 实测与契约一致；**remote 指向本机 bundle（BLK-04）** |
| 自有源码 | `wengao_ppt/` 32 个顶层模块 | 聚合摘要 `4e92c24b8b0afba7…` |
| 构建工具 | 3 个 `scripts/p2_*.py` | `8464f72a…` / `d6dbd731…` / `5124de5f…` |
| 产物（副本） | 233 个文件 | 清单 `RELEASE_COPY_SHAS.txt`，聚合 `8909e467912a…` |
| 外部调用 | PyPI JSON API ×7（免费、匿名、无密钥） | 7/7 成功，原始响应在 `pypi_evidence/`，响应 sha256 已记录 |

**没有**做任何付费调用、没有下载模型、没有购买额度、没有输出任何密钥、没有触碰 8821 / 果园 /
共享 Ollama / 代理 / 其他会话的 CLI。

---

## 7. 真实失败、未解决风险与归属

本轮**没有**把任何一项降级为 PASS。以下均为**未闭合**：

| # | 事项 | 状态 | 归属 |
| :--- | :--- | :--- | :--- |
| 1 | 自有代码 `LICENSE` 未定，`license` 元数据无法填写 | **阻断发布** | 所有者 |
| 2 | 五张 AI 生成配图无 provider/model/任务 ID 证据，文件也无 AI 标识元数据 | **阻断发布**（BLK-02） | 所有者 + 素材负责人 |
| 3 | Pexels 示例照片整包再分发权未确认 | **阻断发布**（BLK-03） | 所有者 / 法务 |
| 4 | `vendor/luobo-ppt` 的 git remote 指向本机 bundle，第三方无法复现获取 | **待确认**（BLK-04） | 所有者 |
| 5 | 上游 `docs/images` 图片无逐图权属说明 | **待确认**（BLK-05） | 所有者 |
| 6 | 依赖版本/许可三口径不一致（requirements 注释 vs 本机安装 vs PyPI） | **待确认**（BLK-06） | 所有者 |
| 7 | `wengao_ppt/scene_images.py` User-Agent 用不可达域名 `wengao.local` 作联系方式 | 待修 | 该产品负责人 |
| 8 | 源候选仍有 20 个文件写死本机绝对路径（13 个 inspect_r3*、4 个 *_gate、1 个测试、1 个 JSON、另有 `run_p0_final_gate.py`） | **源侧未修**（本轮不代改） | 各文件所属工作包 |
| 9 | 副本因排除上述测试而**少一个测试用例**（26 语言名单 + 50 份材料门禁） | 已登记的差异，非通过 | 该测试所属工作包 |
| 10 | 真实端到端链路仍为 0/26 语言、0/50 套；HG 门禁跑在监督侧 | **未验证**（沿用前几轮结论，本轮未声称改善） | 各链路工作包 |
| 11 | 干净安装验证未做，因此 README/SKILL 里的安装启动命令一律标 `【草案·未验证】` | **未验证** | P2 + 后续负责安装验收的一方 |

已知执行层面的小事故（照实记）：

- 首次 PyPI 批量拉取因网络较慢被中断 SIGTERM，改为逐个拉取后 7/7 完成；`INDEX.json` 因此不完整，
  矩阵生成器改为**直接读落盘原始响应并重算 sha256**，不依赖那个 INDEX（已在脚本里写死这一行为）。
- `compileall` 会在副本里留下 `__pycache__`（与 `PYTHONDONTWRITEBYTECODE=1` 无关），已清理并复核为 0。
- 过程中校正了旧文档的三处事实错误（Pillow 许可口径、lucide 许可文件路径、已不存在的两张 Wikimedia 图），
  旧文件保留在 `before_p2/` 与 `LICENSE-STATUS.md` 的"已被取代"横幅里，没有悄悄删除。

---

## 8. 给独立验收者的最小复跑清单

1. `sha256` 复核：`RELEASE_COPY_SHAS.txt` 的 232 条在副本上逐条重算必须全等（本轮结果：0 不一致）。
2. 重新执行第 4 节的 ②③④⑤，比对 `release_copy_build.json` 的 `counts` 与 `copy_rescan.counts`。
3. 检查 `LICENSE-MATRIX.md` 第 0 节的 6 个 BLK 是否仍 blocker；任何一个被宣称"已解决"，都要有对应的
   SHA/时间/执行者证据。
4. 确认第 7 节 11 条里没有哪条被单方面降级；本工作包**不宣告整个项目完成。**

---

## 附录 A：基线生成片段

```python
import hashlib, os, json, time
root = os.getcwd()
skip_dirs = {'__pycache__', '.pytest_cache', '.venv', '.git'}
rows = []
for dirpath, dirnames, filenames in os.walk('.'):
    dirnames[:] = [d for d in dirnames if d not in skip_dirs]
    for fn in sorted(filenames):
        p = os.path.join(dirpath, fn)
        rel = os.path.relpath(p, '.')
        h = hashlib.sha256()
        with open(p, 'rb') as f:
            for chunk in iter(lambda: f.read(1 << 20), b''):
                h.update(chunk)
        st = os.stat(p)
        rows.append((rel, h.hexdigest(), st.st_size, int(st.st_mtime)))
rows.sort()
tree = hashlib.sha256("\n".join("%s  %s" % (r[1], r[0]) for r in rows).encode()).hexdigest()
```
