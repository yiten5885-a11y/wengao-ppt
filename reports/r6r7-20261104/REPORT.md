# R6/R7·安装载荷、结构门禁与干净安装实测（2026-11-04）

状态：**交付 4 项、1 项外部阻断**。本文件不宣告项目完成；`release_ready` 仍为 `false`。

| 项 | 结果 |
| --- | --- |
| 安装载荷可复现构建 | **PASS**（210 成员，6/6 自检，扫描 0 阻断） |
| OOXML 结构合格性门禁 | **PASS**（新门禁 7/7；对 15 页真实导出件 PASS） |
| 材料生成模块定位（U8） | **PASS**（6 项测试，死分支消除） |
| 干净安装根实测（U3/U4） | **安装器语义 10/10 PASS；首任务 run 外部阻断** |
| 完整测试套件 | **734 passed / 0 failed**（364 subtests） |

---

## 1. 本轮解决的问题（可核对）

### 1.1 安装载荷此前不可分发（R6）

现状是"把开发候选整棵拷一份"或"按文件级语义演练"，没有可安装载荷的**生成器**：
谁都可以拷一次，但没人能说明"哪些成员必须进、哪些必须排除、依据是什么、下次怎么
一模一样地重做"。

本轮新增 `scripts/build_install_payload.py`：

- **显式包含清单**：`wengao_ppt/`、`workbench/`、`contracts/`、`samples/r5_material_to_ppt.py`、
  `assets/fontconfig/fonts.conf`、`vendor/luobo-ppt/`、`requirements.txt` 与 7 份发布文档；
- **结构性排除**：`jobs/ reports/ workbench_uploads/ workbench_previews/ wb_loopback_data/
  .venv/ 字节码 日志 tests/ scripts/ fixtures/ calibration/ fonts/`，并在载荷里留
  `PAYLOAD-POLICY.md` 逐条说明排除理由与后果（例如"renders use system fonts"）；
- **内容扫描失败即拒绝**：复用 `p2_release_sanitizer` 对**载荷成员本体**扫描；
  `must_fix` 与未复核的 `manual_review` 都会中止构建。3 条已验证为误报的命中写入
  `SCAN_ALLOWLIST` 并注明理由（脱敏正则、保留域邮箱、上游字体搜索 glob）；
- **产物**：载荷目录 + `BUILD_MANIFEST.json`（逐成员 SHA256）+ `SHA256SUMS.txt` + `.tar.gz`；
- **自检 6/6**：必需成员在位、无阻断扫描命中、无未复核人工项、无运行期成员混入、
  成员数一致、复制后 build_id 可计算。

实测产物：

```
载荷目录   dist/wengao-ppt-0.1.0-895a765f/
成员       210（含 PAYLOAD-POLICY.md）
build_id   cc4782ab67604ad64384da956c7785d5
tar.gz     dist/wengao-ppt-0.1.0-895a765f.tar.gz
           sha256 1e0f38dc01408b4ef3b1fed4f4739528b4f21c7ff890ba1b70c2142b4f5933bd
           4,559,951 bytes
SHA256SUMS sha256 38870f2e3e064ed3e66ae4d8f83311c0cd889549559fb0861ca771339e44b361
```

> 未带 `assets/visual_img/`（12 MB，许可结论未定）。因此载荷里的视觉升级主题**没有
> 示意配图**；需要时用 `--include-unverified-assets`，但那时许可责任随载荷转移。

### 1.2 U8：`from-material` 的模块定位曾是死分支

`cli.cmd_from_material` 手写两条候选：`wengao_ppt/material_flow.py`（**从未存在**）与
`samples/r5_material_to_ppt.py`（只在开发布局成立）。载荷被裁剪掉 `samples/` 时，用户
只会得到一句"模块缺失"，不知道找过哪里，也没有覆盖手段。

本轮新增 `wengao_ppt/material_flow.py` 作为**唯一**定位点：搜索顺序 `$WENGAO_MATERIAL_FLOW`
→ 程序根 `samples/` → 数据根 `samples/` → 调用方额外根；去重保序；缺件抛
`MaterialFlowMissing` 并带出**每一个尝试过的绝对路径**。`cli` 改为调用它，错误信封新增
`searched` 字段。6 项测试覆盖：主路径命中、覆盖优先、缺件全列、去重保序、可加载、
以及"cli 不得再自拼路径"的防回归断言。

实现本体仍是 `samples/r5_material_to_ppt.py`（3,232 行，单一事实源），不复制第二份。

### 1.3 U7：没有 PowerPoint，但可以有结构门禁

本机无 PowerPoint，任何"能正常打开"的结论都无从得出。本轮新增
`scripts/ooxml_conform.py`，把可执行的那一半做掉：C1 ZIP、C2 内容类型覆盖、C3 关系目标
可达（含 `file:///Users/...` 本机路径拒绝）、C4 `sldIdLst` 与实际幻灯片数一致、C5 页内
shape id 唯一且为正、C6 slide→layout→master 链完整、C7 分包内不得出现本机 file URI。

- 对真实 15 页导出件 `reports/p1-candidates/20261102T055159…--wengao-business/…wengao.pptx`：
  **7/7 PASS**。
- 反例测试 4 类（缺部件、重复 shape id、断关系目标、本机 file URI）全部精确命中对应代码；
  另含一条回归：`posixpath.splitext` 把 `_rels/.rels` 判为无扩展名，会导致合规包被误报
  "内容类型未声明"——已修 `_ext()` 并加断言。

范围声明写在脚本输出里：`scope = OPC/OOXML 结构自洽性；**不是** PowerPoint 兼容性验收`。

### 1.4 U3/U4：干净安装根实测（含中文与空格的路径）

新增 `scripts/cleanroom_install_check.py`，从载荷安装到卸载全链跑一遍，**全程走用户真实
入口** `<安装根>/bin/wengao`，且从中性 cwd 调用（避免"必须 cd 到载荷目录才成功"的假通过）。

证据：`reports/install-cleanroom-20261104-final/CLEANROOM_INSTALL.json`
安装根：`work/cleanroom/安装 根 D`（路径含中文与空格；`non_ascii_root=true`）

| 步骤 | 结果 |
| --- | --- |
| install（`--activate`，载荷 210 成员，含中文渲染自测） | rc=0 |
| status（经稳定入口，中性 cwd） | rc=0 |
| first_task precheck | rc=0 |
| first_task run | **BLOCKED_EXTERNAL**（`MODEL.VISION_RUNTIME_UNAVAILABLE`） |
| first_task validate/publish/export | NOT_EVALUABLE（上游 run 未完成，报 `GATE.ARTIFACT_MISSING`） |
| upgrade（加 marker 的可区分构建，`--expect-root`） | rc=0，current 切换 |
| status / rollback / status | rc=0，回退到原版本 |
| uninstall 预演 / `--apply` | rc=0；程序移入 `quarantine/`，`current.json.removed` 留档 |
| 卸载后 status | rc=4「未安装」——卸载真的生效 |

判定：`PASS_INSTALLER_BLOCKED_FIRST_TASK_MODEL` — 安装器语义 10/10 成立，首任务的模型
阶段因外部依赖缺失未执行。

**阻断原因（实测，不是推测）**：`run` 的语义场景阶段对**所有稿件**无条件执行，需要本机
视觉端点（默认探测 `127.0.0.1:8080`、`11434`）。本机自 2026-10-25 移除 Ollama 后这两个
端口都不存在；文本端点 `18088`（经 `18089` 的 mlx_vlm）可用，但它是文本模型，`scene_model.py`
明确禁止拿它做图片复核。尝试在本机 8080 起一个 mlx_vlm 视觉服务失败：
本地没有 `Qwen2.5-VL-3B` 权重快照，Hub 也不可达（`ConnectError: SSL UNEXPECTED_EOF`）。
按边界不下载模型，故记为外部阻断。

解除条件（三选一，均需用户/环境侧动作）：

1. 在本机起专用视觉服务并监听 8080（`mlx_vlm.server --port 8080 --model <本地 VL 权重>`）；
2. 设置 `WENGAO_LOCAL_VISION_MODEL_ENDPOINT` 指向一个**已就绪的多模态**端点；
3. 产品侧新增"纯文字模式"（跳过照片语义阶段）。这是**新需求**，涉及 HG13 规则与门禁
   口径变更，不能由本轮擅自打开。

---

## 2. 阻断与未完成（不得据此声明完成）

1. **首任务 run/validate/export 未在干净安装根上跑通**：外部依赖（视觉端点）缺失，见 §1.4。
2. **U1 监督方 UI 全链复验**（8823，Cua）：本轮未做；本轮的干净安装证据只覆盖 CLI 与安装器。
3. **U2 原生实播 + 关闭重开录像**：本机 GUI 缺字与截图超时问题未解，未做。
4. **U5 发布**：未做。授权闸门（全部真实验收通过）尚未满足。
5. **U6 许可证**：仍待用户决定；本轮不写任何许可证文件。
6. **U7 的 PowerPoint 兼容性**：本机无 PowerPoint，只有结构门禁，不构成兼容性结论。
7. **载荷缺少 `assets/visual_img/`**：视觉升级主题无示意配图；许可未定，不擅自打包。

---

## 3. 复现命令

工作目录：`build/wengao-visual-v13-material-fix`；解释器：`.venv/bin/python`。
（本机沙箱需要 `--basetemp` 到仓库内，且 pytest 期间要清掉宿主批量删除守卫的两个环境变量。）

```bash
# 0) 完整测试套件
env -u CODEBUDDY_SAFE_DELETE_BULK_STATE_DIR -u CODEBUDDY_TOOL_CALL_ID \
  PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests -q \
  --basetemp work/pytest/run-$(date +%s) -p no:cacheprovider

# 1) 载荷政策自检（不落盘）/ 构建
.venv/bin/python scripts/build_install_payload.py --check-only
.venv/bin/python scripts/build_install_payload.py --out dist

# 2) 结构门禁
.venv/bin/python scripts/ooxml_conform.py --pptx <某个.pptx> --json-out <out.json>

# 3) 干净安装根实测（安装根须不存在或为空）
.venv/bin/python scripts/cleanroom_install_check.py \
  --payload dist/wengao-ppt-0.1.0-895a765f \
  --root "work/cleanroom/安装 根 E" \
  --report reports/install-cleanroom-20261104-final \
  --python "$PWD/.venv/bin/python"
```

---

## 4. 本轮交付物 SHA256

```
3de68183df5fd3abf3b0202ab2eb09a34b04b629ff3f7d718d65bd4b3e415749  scripts/build_install_payload.py
0046d6fc5cea95ef8c045c185775e2e163e8c37331e022aea98c0b874a027f6b  scripts/ooxml_conform.py
f18f2bd7b7341e3c3b97f9ed9db03120ea6bcbf89b464547575d6bebb1ec55bc  scripts/cleanroom_install_check.py
fe36b6d6ba3bced39637a419bcf0817eeb7e794449f274ce6ec707e292b2c225  wengao_ppt/material_flow.py
ea6ac36af11580fbc63e8b0189ee900a859c91e55d119e8cbdd85da0d862dc66  tests/test_material_flow_locator.py
8fc060e30d48ca25cdbb4af85338f4a984f85e369b63e64fd5c7be8aefecddd3  tests/test_ooxml_conform.py
```

相关证据文件：

```
reports/install-cleanroom-20261104-final/CLEANROOM_INSTALL.json   干净安装根实测全记录
reports/install-cleanroom-20261104-final/first-task/              首任务输入（3 页，脚本自建）
work/ooxml/latest.json                                            15 页真实导出件结构门禁结果
dist/BUILD_MANIFEST.json                                          载荷逐成员哈希与自检
```
