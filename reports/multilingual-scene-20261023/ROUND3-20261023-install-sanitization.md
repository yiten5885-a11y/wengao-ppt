# 稳稿 PPT — 剩余项推进记录（第 3 轮）

- 时间：2026-10-23 04:10 PDT
- 候选：`/Volumes/huangyingteng/WorkBuddy/2026-10-12-00-54-41/wengao-ppt/build/wengao-visual-v13-material-fix`
- 结论：**1 项完成（build_id 重算）、1 项真实修复（安装打包泄漏）、其余仍阻塞或未实施**。未发布，`release_ready=false`。

## 1. 环境复核（每次开工必查，本轮结果与上轮一致）

| 探测 | 结果 | 含义 |
| --- | --- | --- |
| `curl 127.0.0.1:8823` | **502** | 本机无 8823 监听；502 来自环境代理（`HTTP_PROXY=127.0.0.1:52719`） |
| `curl --noproxy '*' 127.0.0.1:11434/api/tags` | 连接失败 | 无本机 Ollama/模型网关 |
| `command -v soffice` | 未找到 | 无 LibreOffice，无法真实渲染/预览 |

结论：26 语言真实工作台链路与 50 套真实生成在本机**不可能完成**，不是流程选择问题。以下两项继续记为阻塞，并附精确原因。

## 2. build_id 权威重算（已完成）

在候选原路径（不是镜像）上计算：

```
AUTHORITATIVE_BUILD_ID 347ce8ae9772784a0216bac6c4e69c65
```

- 改动前的 `73cd8292102ede04b15c4fa9264da66b` 已失效，不再对应当前源码。
- 镜像上算得 `698e06c57cef917e52d76bc5d3405ecc`，与权威值不同：镜像缺少候选中的部分成员，**以 347ce8ae… 为准**。
- 按 F1：任何 50 套批次都必须在此新身份冻结后重新开始；本轮**未起批、未冻结**。

## 3. 新修复：安装打包泄漏（真实缺陷，已修并测试）

### 3.1 缺陷

`wengao_ppt/install.py` 的 `_copy_into_versions()` 原实现为：

```python
shutil.copytree(pkg, staging)      # 无任何排除规则
```

候选构建目录同时是工作目录，里面有 `jobs/`（真实任务输入）、`reports/`（验收报告）、`wb_loopback_data/`（回环工作台数据与用户上传）、`.wengao_scene_assets/`（场景缓存）、`workbench_server_8823.log`。安装时这些会被**整目录复制**进 `<root>/versions/<label>/`，等于把客户私有材料与日志装进程序版本。

实测（构造含私有成员的包后安装）：

```
LEAKED  jobs
LEAKED  reports
LEAKED  wb_loopback_data
LEAKED  .wengao_scene_assets
LEAKED  workbench_server_8823.log
```

这直接违反 G1（用户数据与程序文件隔离）与 G5（消毒审计）。

### 3.2 修复

- 新增 `_PACKAGE_EXCLUDE_DIRS` / `_PACKAGE_EXCLUDE_SUFFIXES` / `_PACKAGE_EXCLUDE_FILES`，用 `ignore=_package_ignore` 只复制程序成员。
- 新增 `_forbidden_members()`：复制完成后**再次扫描**，若仍出现运行期成员，删除 staging 并失败关闭（`stage=package_sanitization`），不静默继续。
- `do_install()` 捕获该失败并返回退出码 4。

排除项：`jobs`、`reports`、`material_tasks`、`wb_loopback_data`、`workbench_previews`、`workbench_uploads`、`.wengao_scene_assets`、`.fontconfig`、`.lo-profile`、`__pycache__`、`.git`、`.venv`、`node_modules`、各类缓存目录；`*.log`、`*.pyc`、`*.tmp`、`*.sqlite*`、`*.db`；`.DS_Store`、`probe.txt`。

### 3.3 修复后实测

```
clean  jobs
clean  reports
clean  wb_loopback_data
clean  .wengao_scene_assets
clean  workbench_server_8823.log
```

程序成员（`wengao_ppt/`、`contracts/`、`assets/`、`README.md`）仍完整安装，已由测试断言。

### 3.4 安装自测的真实结果（不得当通过）

同一台机器上跑 `install`，11 项自测：

| 检查 | 结果 |
| --- | --- |
| import / cli --help / 必需资源在位 / .py 可编译 / 核心模块可导入 | OK |
| 字体配置可解析 / 字体缓存写在数据根 | OK |
| **中文渲染冒烟** | **FAIL：本机未找到 LibreOffice；中文渲染冒烟未执行（不是通过）** |
| 模块来自本版本目录 / 生成冒烟输入 / precheck 冒烟 | OK |

即：安装自测整体 `ok=false`，且原因是如实报告“未执行”，不是假通过。这也说明**安装链路未验收通过**。

## 4. 本轮测试

| 范围 | 结果 |
| --- | --- |
| 全量 `pytest tests -q` | **218 passed, 276 subtests passed**（3m24s） |
| 新增 `tests/test_install_sanitization.py` | **5 passed, 16 subtests** |

上轮基线 208/260 → 本轮 218/276，零失败、零回归。

改动文件：

| 文件 | SHA-256 |
| --- | --- |
| `wengao_ppt/install.py` | `209c3ed31af5aa7b722e61b3f4ee808ca3e50f4b6122f62963cae92481b19257` |
| `tests/test_install_sanitization.py` | `d0faef4ed656c2a4ed55b2dad8c60f7692808311eaa8e0cbd0bb408dd4497c69` |

## 5. 仍未实施 / 仍阻塞（逐项对应你的清单）

| 项 | 状态 | 证据 / 原因 |
| --- | --- | --- |
| 26 语言真实工作台链路 0/26 | **阻塞** | 8823 不可达（502 经代理）、无本机模型、无渲染器；无法完成上传→推理→出图→渲染→导出 |
| 50 套 0/50 | **阻塞** | 同上；且 build_id 已变为 `347ce8ae…`，需先冻结新身份再起批 |
| 排版硬门禁 12 项（HG01–HG12） | **未实施** | 候选中只有策略契约 `contracts/layout_hard_gate_policy.json`；`wengao_ppt/checks.py` 有 34 个内容/保真检查函数，但**没有 HG 标识的 12 项实现**。门禁运行在监督侧 `Documents/Codex/2026-10-20/n-2/work/strict-qa/`（`audit_layout_export.py` 等），不在候选包内，本轮未改 |
| P2/P5/P10 版式缺陷 | **未实施** | 需真实渲染比对；本机无 LibreOffice。产品侧相关修复逻辑在 `readability.py`（已含 HG04/HG05/HG06 对齐注释），本轮未动 |
| 原生播放门禁 | **未实施** | 同上，需渲染与视频解码证据；本机无渲染器 |
| 29 个 alpha/透明对象未解析项 | **未实施** | 需对应候选产物与渲染证据，本机不可得 |
| 安装 / 许可 / 文档 / 消毒 | **部分完成** | 打包泄漏已修复并测试（见第 3 节）；安装自测因缺渲染器**未通过**；许可证清单、README/快速开始、消毒审计报告未完成 |
| 灰度版画扫描识别 | **未修复（已知限制）** | 现有 `photographic_asset_check()` 只识别平涂矢量图（色数<1200 且前八色>0.55）。灰度版画扫描色调连续，无模型无法与黑白照片可靠区分；强行加启发式会误杀合法黑白照片，故不加，需本机视觉复核（当前不可用） |

## 6. 声明

- 未发布 GitHub / SkillHub，`release_ready` 保持 `false`。
- 未触碰 8821、用户任务数据、共享 CLI、系统代理、Ollama；未下载模型、未购买额度、未调用 Claude 或付费模型。
- 未把模型列表、离线 fixture、历史回放或自评当作真实验收结果。
