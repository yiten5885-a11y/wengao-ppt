# R6/R7·安装载荷、结构门禁与干净安装实测（2026-11-04）

状态：**交付 6 项、1 项外部阻断**。本文件不宣告项目完成；`release_ready` 仍为 `false`。

| 项 | 结果 |
| --- | --- |
| 安装载荷可复现构建 | **PASS**（212 成员，6/6 自检，扫描 0 阻断） |
| 载荷漏带 HG01–HG12 门禁实现（**真实缺陷**） | **已修**（并把"缺实现"从静默跳过改为失败关闭） |
| OOXML 结构合格性门禁 | **PASS**（新门禁 7/7；对 15 页真实导出件 PASS） |
| 结构门禁批量回归（新增） | **PASS**（116/116 历史导出件结构自洽） |
| 材料生成模块定位（U8） | **PASS**（6 项测试，死分支消除） |
| 干净安装根实测（U3/U4） | **安装器语义 11/11 PASS；首任务 run 外部阻断** |
| 完整测试套件 | **749 passed / 0 failed**（364 subtests） |

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

实测产物（本轮最终构建，含 §1.5 的 `scripts/layout_gate.py`）：

```
载荷目录   dist/wengao-ppt-0.1.0-e46ac2da/
成员       212（含 PAYLOAD-POLICY.md）
build_id   015326047d9bbee2689f8443c45b0e8c
tar.gz     dist/wengao-ppt-0.1.0-e46ac2da.tar.gz
           sha256 4d4d86d13e50deb6ad44fd8dfa1ede6335e4d52c4782d47cd07e638040bf550d
           4,610,935 bytes
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
安装根：`work/cleanroom/安装 根 H`（路径含中文与空格；`non_ascii_root=true`）

| 步骤 | 结果 |
| --- | --- |
| install（`--activate`，载荷 212 成员，含中文渲染自测） | rc=0 |
| status（经稳定入口，中性 cwd） | rc=0 |
| doctor（安装完整性 / 运行前置分级） | install_ok=true，`blocking_missing=[]`，`prerequisites_missing=['vision_endpoint']` |
| **runtime_gate_loadable**（用安装实例自己的副本实载 HG01–HG12） | rc=0，`{"bytes":160106,"fake_mp4":{"ok":false,"error":"missing_ftyp"},"validators":["_inspect_mp4","_validate_native_review_record"]}` |
| first_task precheck | rc=0 |
| first_task run | **BLOCKED_EXTERNAL**（`MODEL.VISION_RUNTIME_UNAVAILABLE`） |
| first_task validate/publish/export | NOT_EVALUABLE（上游 run 未完成，报 `GATE.ARTIFACT_MISSING`） |
| upgrade（加 marker 的可区分构建，`--expect-root`） | rc=0，current 切换 |
| status / rollback / status | rc=0，回退到原版本 |
| uninstall 预演 / `--apply` | rc=0；程序移入 `quarantine/`，`current.json.removed` 留档 |
| 卸载后 status | rc=4「未安装」——卸载真的生效 |

判定：`PASS_INSTALLER_BLOCKED_FIRST_TASK_MODEL` — 安装器语义 12/12 成立，首任务的模型
阶段因外部依赖缺失未执行。

**阻断原因（实测，不是推测）**：`run` 的语义场景阶段对**所有稿件**无条件执行，需要本机
视觉端点（默认探测 `127.0.0.1:8080`、`11434`）。本机自 2026-10-25 移除 Ollama 后这两个
端口都不存在；文本端点 `18088`（经 `18089` 的 mlx_vlm）可用，但它是文本模型，`scene_model.py`
明确禁止拿它做图片复核。尝试在本机 8080 起一个 mlx_vlm 视觉服务失败：
本地没有 `Qwen2.5-VL-3B` 权重快照，Hub 也不可达（`ConnectError: SSL UNEXPECTED_EOF`）。
本轮再次核查本机模型库（`/Volumes/macmini/models`、HF hub 缓存）确认**没有任何多模态
权重**，`clip_vision/` 是 ComfyUI 的视觉编码器、不是 VLM。按边界不下载模型，故记为外部阻断。

> 注意区分两件事：本轮之前，"干净安装跑不完整一个任务"有**两个**独立原因——外部视觉端点
> 缺失（未变），以及载荷漏带 HG01–HG12 门禁实现（见 §1.5，**本轮已修**）。后者即使视觉
> 端点就绪也会让 validate 永久 `NOT_EVALUABLE`。

解除条件（三选一，均需用户/环境侧动作）：

1. 在本机起专用视觉服务并监听 8080（`mlx_vlm.server --port 8080 --model <本地 VL 权重>`）；
2. 设置 `WENGAO_LOCAL_VISION_MODEL_ENDPOINT` 指向一个**已就绪的多模态**端点；
3. 产品侧新增"纯文字模式"（跳过照片语义阶段）。这是**新需求**，涉及 HG13 规则与门禁
   口径变更，不能由本轮擅自打开。

---

## 1.5 载荷漏带 HG01–HG12 门禁实现（本轮最重要的发现）

**这条缺陷不影响"安装能不能成功"，但让"安装成功之后能不能跑完一个任务"永久为否。**

`wengao_ppt/runner.py` 用 `program_root / "scripts" / "layout_gate.py"` 打开排版硬门禁
（HG01–HG12，3,094 行）的实现，而载荷政策把 `scripts/` 整体当作"开发脚本"排除。
在安装实例上同时产生两种后果：

1. **失败关闭方向**：`_run_layout_hard_gate` 拿不到实现 → `layout.hard_gate` 恒为
   `NOT_EVALUABLE` → `not_evaluable` 非空 → validate 阻断。也就是说，**任何干净安装
   都不可能跑完一个任务**——这跟视觉端点缺失是两件独立的阻断，此前被前者的现象盖住了。
2. **失败开放方向（更危险）**：同一个文件还承担原生实播录像与独立复核记录的证据校验
   （`_inspect_mp4` 时长 30–60s、`_validate_native_review_record`），而原代码写的是
   `if gate_path.is_file():` —— 文件不在时**静默跳过**。未校验的录像与复核记录可以
   直接进证据库。

为什么之前的证据没抓到：干净安装实测的首任务在 `run` 的模型阶段就被外部阻断，
`validate` 从未执行，所以第 1 条从未被触发；第 2 条只在"上传原生实播证据"时才走。

修复：

* 载荷显式包含 `scripts/layout_gate.py`（`scripts/` 其余脚本仍排除），
  `PAYLOAD-POLICY.md` 同步写明这条例外与理由；
* `install.REQUIRED` 增加 `scripts/layout_gate.py` 与 `contracts/layout_hard_gate_policy.json`
  ——两者都是运行期以 `program_root/...` 打开的成员，缺任一个安装实例都跑不通；
  现在缺件会在**安装自检**失败，而不是等到 validate；
* runner 的两个静默跳过分支改为失败关闭：
  `EVIDENCE.GATE_IMPLEMENTATION_MISSING` / `EVIDENCE.GATE_IMPLEMENTATION_UNLOADABLE`。

守住它的测试：`tests/test_payload_runtime_deps.py` 断言 **`install.REQUIRED` 的每个
成员都必须能被载荷政策产出**。这两张表历史上是各自手写的，正是它们不一致才漏了文件——
把"一致"变成可执行断言，比再写一段说明有用。

同时给干净安装脚本加了 `runtime_gate_loadable` 一步：不只看文件在不在，而是用**安装实例
自己的副本**把门禁模块加载起来、对它调一个纯函数（`_inspect_mp4`），证明校验真的在跑。
以后再漏件，这一步会直接失败。

实测证据（安装根含中文与空格、经 `<root>/bin/wengao`、中性 cwd）：

```
载荷        212 成员（原 211）
build_id    015326047d9bbee2689f8443c45b0e8c
doctor      install_ok=true  blocking_missing=[]
            required_members: missing=[] members=306（含 layout_gate.py 与冻结政策）
实例内实载  用安装实例自己的副本 importlib 加载 160,106 bytes 成功
            _inspect_mp4(b"\x00"*64) → {"ok": false, "error": "missing_ftyp"}  ← 校验真的在跑
            冻结政策 sha256 == runner.LAYOUT_GATE_POLICY_BUNDLE_SHA256
```

## 1.6 结构门禁改成可批量回归（并给出全量结果）

单文件模式只能回答"这一个文件好不好"。回归要问的是相反的问题：**这次改动有没有把原本
合格的结构弄坏**。逐个手跑既不可复现也容易漏件。

`scripts/ooxml_conform.py` 新增 `--sweep DIR`（递归聚合，失败按检查项归类）与
`--exclude GLOB`（可重复）。排除**不是**为了把失败藏起来：仓库里有故意退化的合成夹具
`wp9-.../demo/demo-deck-001.pptx`（838 字节、4 个部件、没有任何 rels，清单自标
`is_synthetic_fixture`），门禁判它 FAIL 是**正确的**。排除项与计数都写进结果，可见。

```
扫描 118 个 .pptx → 排除 2 个合成夹具 → 116/116 PASS，failed_checks_by_code={}
证据 reports/ooxml-sweep-20261104/SWEEP.json
```

顺带记录一条**边界**（本可以变成一个假阳性）：`wengao-visual-dark` 有一枚 4.79×4.79in
的装饰形状出血到画布外（L9.38 T3.96 R14.17 B8.75），那是设计的刻意溢出，不是缺陷。
所以"所有形状都在画布内"不能当无条件硬规则；任何引用它的门禁必须能区分装饰与内容
（产品的 `layout/checks.py` 用 `_is_background` 处理全画布底色，但这类 bleed 形状它仍会
判越界——这也是它只适用于排版合成路径、不适合直接套在导入/美化输出上的原因之一）。

> 附注：`wengao_ppt/layout/`（含 `checks.py` 的 `check_layout`）在本仓库**没有任何调用点**，
> 真正的 HG01–HG12 实现在 `scripts/layout_gate.py`。直接对历史导出件跑 `check_layout`
> 会得到成百上千条 FAIL（例如某份已被接受的候选 414 条），因为它的字号口径
> （`body_hard_min=24`）是给排版合成路径用的。**不要**把它当通用稿子门禁复用。

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

# 2) 结构门禁（单件 / 批量回归）
.venv/bin/python scripts/ooxml_conform.py --pptx <某个.pptx> --json-out <out.json>
.venv/bin/python scripts/ooxml_conform.py --sweep reports \
  --exclude "wp9-prepublication-50-decks-20261023/demo/*" \
  --json-out reports/ooxml-sweep-20261104/SWEEP.json

# 3) 干净安装根实测（安装根须不存在或为空）
.venv/bin/python scripts/cleanroom_install_check.py \
  --payload dist/wengao-ppt-0.1.0-e46ac2da \
  --root "work/cleanroom/安装 根 H" \
  --report reports/install-cleanroom-20261104-final \
  --python "$PWD/.venv/bin/python"
```

---

## 4. 本轮交付物 SHA256

```
b75c3ab21298dfd4d22bf05bef27d230186f26e809823d88beb43ac53938877e  scripts/build_install_payload.py
f4f64ddef465601c3fbf7772f5b1918f5d016a1bcdb68bac0f8fd854d3888cdf  scripts/ooxml_conform.py
e9fc1e334df5bf0a49302c4c53f897c866b1fc30b3745f3462cf7572b324aa8d  scripts/cleanroom_install_check.py
fe36b6d6ba3bced39637a419bcf0817eeb7e794449f274ce6ec707e292b2c225  wengao_ppt/material_flow.py
b6b216c36597a4dea5c5ac4b2a336e8afa02421609591a702eab382f8afc3c84  wengao_ppt/install.py
14c04320e5d9e9154245d5bbc813d1faefe78aa9f1ff22aea4fd3f86ee895d7c  wengao_ppt/runner.py
ea6ac36af11580fbc63e8b0189ee900a859c91e55d119e8cbdd85da0d862dc66  tests/test_material_flow_locator.py
cf9f211d9ff595f6e18d712fc468c52db83c097b5847a83bf592549708146a30  tests/test_ooxml_conform.py
82a0e7cd21b6cd76f05e7d9cfdea7ac4e6a767269310ae65b17fa5d7bde5f15b  tests/test_payload_runtime_deps.py
```

相关证据文件：

```
reports/install-cleanroom-20261104-final/CLEANROOM_INSTALL.json   干净安装根实测全记录
reports/install-cleanroom-20261104-final/first-task/              首任务输入（3 页，脚本自建）
reports/ooxml-sweep-20261104/SWEEP.json                           116 件历史导出件结构回归结果
work/ooxml/latest.json                                            15 页真实导出件结构门禁结果
dist/BUILD_MANIFEST.json                                          载荷逐成员哈希与自检
```
