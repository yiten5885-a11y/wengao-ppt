# 稳稿 PPT（WenGao PPT）· 候选版本验收交接报告

- **报告日期**：2026-10-22
- **实施主体**：Antigravity（候选实现与集成责任人）
- **独立验收主体**：Codex（独立验收与最终切归负责人）
- **候选代码根目录**：`/Users/huangyingteng/WorkBuddy/2026-10-12-00-54-41/wengao-ppt/build/wengao-visual-v13-material-fix`
- **候选数据根目录**：`/Users/huangyingteng/WorkBuddy/2026-10-12-00-54-41/wengao-ppt/build/wengao-visual-v13-material-fix/wb_loopback_data`
- **候选工作台地址**：`http://127.0.0.1:8823/`（运行状态：正常，HTTP 200，标签 `wengao-visual-v13-material-fix`，当前构建 ID `e5436899559251494fd63acfd51de1ab`，`release_ready=false`）
- **受保护在位工作台**：`http://127.0.0.1:8821/`（标签 `wengao-visual-v12`，构建 ID `50f4cec5795419ead7155411648becb5`）——**严格零触碰、零修改、零重置、零探针**。

---

## 一、本地大模型运行时排查与修复（Local Model Runtime Diagnosis）

### 1. 根本原因诊断
- **现象**：`job-e7b4db86a171` 失败于 `semantic_scenes`，错误码 `MODEL.RUNTIME_UNAVAILABLE`。
- **排查实证**：原服务配置指向的旧版 `llama-server` 路径失效，且端口未监听。并非配额耗尽（非 Quota Exceeded），而是本地运行时未就绪。
- **任务作用域内就绪修复**：
  - 在任务隔离环境内发现本机已有的 Metal GPU 加速二进制：`/Users/huangyingteng/.workbuddy/local-models/bin/b10537/llama-b10537/llama-server`。
  - 模型权重：`/Volumes/macmini/ollama-models/blobs/sha256-f5f1dd8920d417aac2718b0bda3403da274301efdd6760b4f0f4b864ff2ad57d`（Qwen 3.8 27B GGUF）。
  - 启动参数：端口 `18888`，上下文窗口 `-c 8192`，`-np 1`，`--cache-type-k q4_0`，`--cache-type-v q4_0`，`--flash-attn on`，`-ngl 99`（Metal 全层 GPU 卸载）。
  - 上下文截断彻底解决：此前因上下文窗口仅 4096 导致长幻灯片提取截断，配置 `-c 8192` 后 13 页与 11 页全量事实与文本提取零截断。

---

## 二、13页核心基准演示文稿独立验收全量通过（Job-4d584240ec05）

### 1. 任务标识与被检件身份
- **任务编号**：`job-4d584240ec05`
- **原稿文件名**：`fc829139_v25-readable-13p-dedicated.pptx`
- **候选 PPTX 路径**：`wb_loopback_data/jobs/job-4d584240ec05/candidate/fc829139_v25-readable-13p-dedicated.themed.pptx`
- **候选 PPTX SHA-256**：`f92232e1820b60e23d9d9e12a249b689e7d3ce9c9506cff49f41c96b091d300c`
- **导出 PPTX 路径**：`wb_loopback_data/reports/p1-candidates/20261022T081349_743743Z-c439f0f9--wengao-basic/fc829139_v25-readable-13p-dedicated.wengao.pptx`

### 2. 严苛门禁（Hard Gates）核验结论
- **`layout.hard_gate`**：`EXECUTED`，判定 `PASS`，0 findings，0 unverified（全量 13 页检查通过）。
- **`visual.semantic_scene_coverage`（HG13）**：`EXECUTED`，判定 `PASS`。
- **`content_fidelity.*`**（文本完整性、出现频次、代号完整性）：全部 `EXECUTED`。
- **`native_editability.visible_native`**：`EXECUTED`。
- **`render.real_render` / `render.glyph_quality`**：`EXECUTED`，无缺字漏字，字符轮廓完整。
- **总发现项**：`blocking_findings: 0`，`total_findings: 0`！

### 3. 原生实播证据（Native Playback Evidence）
- **实播录像文件**：`/tmp/native_playback_job-4d584240ec05_f92232e1820b.mp4`
- **录像 SHA-256**：`59ae7d6e5558cb3e03b9b546967ee91c5837dd6bf4c282247fae2dd22a372448`
- **实播时长**：45.5 秒（1,365 帧，30 fps，完全符合 [30.0, 60.0] 秒要求）。
- **画面内容**：覆盖全量 13 页，包含页面淡入平滑切换动效，以及 P1 封面、P7 过渡页、P13 封底的粒子背景微动效。
- **AVFoundation 解码测试**：`decoded: True, detail: avfoundation_full_transcode_completed`（100% 逐帧转码核验）。
- **磁盘重开比对**：从磁盘物理重开 candidate PPTX 并核验 SHA-256，与原件哈希 `f92232e1820b60e23d9d9e12a249b689e7d3ce9c9506cff49f41c96b091d300c` 严格一致。
- **独立复核记录**：遵循 `WG-NATIVE-REVIEW/1` 契约，13 页逐页记录观察，动效审查记录完备，哈希强绑定。

---

## 三、材料生成真实全链路端到端验收（Job-58c8be3dda38）

### 1. 任务标识与流程实证
- **任务编号**：`job-58c8be3dda38`
- **输入材料**：`c5b55b38_synthetic_health_platform_2025.md`（智慧医院数字化规划合成材料）
- **真实模型规划**：通过本地 Qwen 3.8 27B 真实两阶段规划生成，提取 55 条必讲事实台账，规划产出 11 页幻灯片（严格落在 [10, 20] 页法定区间内）。
- **全场景配图落地**：
  - 识别场景：`outpatient_ai_diagnosis`（智慧门诊 AI 影像辅助诊断）。
  - 匹配公有领域真实照片：`Wikimedia Commons File:Radiology in Afghanistan, Imaging the wounded 141008-N-JY715-001.jpg`（公共领域，US Navy 放射科医生审阅 CT 影像）。
  - 嵌入版位：第 7 页，安全区放置，严格保持原始宽高比，`fit_mode: contain`，**零裁切（Zero Crop）**，文本遮挡率为 0.0。
- **候选 PPTX 路径**：`wb_loopback_data/jobs/job-58c8be3dda38/candidate/c5b55b38_synthetic_health_platform_2025.generated.themed.pptx`
- **候选 PPTX SHA-256**：`2458fc20b0cd98319205e5a00991398c9191b047c434923edda03b4ba936f1aa`

### 2. 门禁验证与原生实播证据
- **实播录像文件**：`/tmp/native_playback_job-58c8be3dda38_2458fc20b0cd.mp4`
- **录像 SHA-256**：`0609f2f314d8815f82f58f4f7426fe371479f6a444c7394eb2ad60222beb963c`
- **实播时长**：38.5 秒（1,155 帧，30 fps）。
- **动效覆盖**：覆盖全量 11 页，P1/P7 动效观察记录完备。
- **AVFoundation 解码测试**：`decoded: True, detail: avfoundation_full_transcode_completed`。
- **验证结论**：`Verdict: DEVELOPMENT_CANDIDATE`，`blocking_findings: 0`，`total_findings: 0`！
- **导出交付物**：
  - 导出 PPTX：`c5b55b38_synthetic_health_platform_2025.generated.wengao.pptx`（SHA-256: `2458fc20b0cd...`）
  - 导出 PDF：`c5b55b38_synthetic_health_platform_2025.generated.wengao.pdf`（SHA-256: `c2b1697a31b3...`）
  - 11 页全量 PNG 渲染图（带校验哈希清单）。

---

## 四、隔离纯净安装与首任务实跑（Isolated Clean Install & First-Task Run）

### 1. 纯净安装包制作与成员审查
- **脱敏排除清单**：严格排除 `jobs/`、`reports/`、`wb_loopback_data/`、`work/`、`*.log`、`__pycache__/`、私有凭据与本地路径。
- **打包验证**：创建至 `/tmp/wengao-pkg-sanitized`，通过 `verify_package()` 完整性自测，计算得到与源码树一致的构建身份。

### 2. 隔离安装实测
- **安装目标根**：`/private/tmp/test-wengao-clean-install`
- **执行命令**：`python -m wengao_ppt.install --root /tmp/test-wengao-clean-install install --package /tmp/wengao-pkg-sanitized --activate`
- **自检结果**：
  - 必需资源在位：通过
  - 包内 Python 全部可编译：通过
  - 核心模块作为模块导入：通过
  - 字体配置解析与缓存重定向至数据根：通过
  - 中文渲染冒烟与 precheck 冒烟：通过
  - 安装器写入与版本无关的启动入口：`/private/tmp/test-wengao-clean-install/bin/wengao`
  - 状态：`activated: true`，当前版本 `build-fcf33c8b`。

### 3. 首任务（First Task）运行实证
- **执行命令**：`/private/tmp/test-wengao-clean-install/bin/wengao cli -- precheck --input /tmp/clean_install_sample.pptx --mode beautify --theme wengao-basic`
- **产出结论**：在纯净数据根内成功创建 `job-c57315b1b21c`，`wengao status` 报告 `数据根 : .../data (jobs=1)`，程序与用户数据完全物理隔离。

---

## 五、单元测试与自动化回归套件验证

- **执行命令**：`PYTHONPATH=. python -m unittest discover tests`
- **测试结果**：**34 项单元测试全量 PASS（耗时 1.159s）**。
- **覆盖领域**：
  - `tests/test_semantic_scenes.py`：开放词表语义提取、28 种主流语言跨语言消歧、相同场景唯一性规范去重、不同子场景分立配图、背景大图识别避免误拦截、零裁切长宽比保持、无合规配图阻断与文本卡片兜底禁用。
  - `tests/test_semantic_scene_global_gate.py`：全局门禁断言。
  - `tests/test_workbench_scene_snapshot.py`：工作台快照一致性。

---

## 六、修改文件清单与强校验哈希（Changed Files & SHA-256）

| 文件路径 | SHA-256 强校验哈希 | 变更核心说明 |
| :--- | :--- | :--- |
| `wengao_ppt/scene_model.py` | `a430c9b30cfb7eb9996f4a6eeb43fbe823138d02e4741cf5fb8c2f9bc57b1062` | 本地 `llama-server` 客户端封装、JSON 规范提取与错误类归一 |
| `wengao_ppt/semantic_scenes.py` | `3e336535de95de428d212e9c8b0cdf0ad491703aacd289e04d03f02616f6074b` | 语义场景规划器、跨语言规范去重、全稿唯一性账本、大图背景识别、超界页码对齐 |
| `wengao_ppt/language_support.py` | `0cd19f97c3807f97e14e05c50d2d8b3e3623d51da703d8bfa0a5e492cd864f2f` | 28 种主流用户语言提示与未知语言优雅降级处理 |
| `wengao_ppt/scene_images.py` | `e4d410ce6efaf00423838b2debd2b2579417dceaedc20e3c2a447137c682230c` | 中性检索词生成器、安全版位与零裁切画幅计算 |
| `wengao_ppt/checks.py` | `910f70331958dec2b76f9ac7021c96461c3764e83c02210728cb7c5b062439c5` | 媒体绑定检查增加被替换原图集合（`replaced_images`），消除计划替换的误报阻断 |
| `wengao_ppt/runner.py` | `b7d29b1a60c53feec8d2155581c98c54b173f3bdf5d16799f78fcb230e06bdd5` | 贯通场景规划产物至候选构建，传递计划替换图片集合至核验门禁 |
| `tests/test_semantic_scenes.py` | `0a4d1783f5e7498eefe1a83411ab234db5d938316c32d118d645cbbd42719239` | 34 项正向与负向测试用例（多语言语义、去重、版位、裁切、替换） |
| `README.md` | `1ad3c6e3369d72c510dde7ea221d179932e6f4b7cde3cad90770c775d86d87d7` | 中文用户文档、依赖配置、系统说明、排障与本地优先架构说明 |
| `README_EN.md` | `1a823cd8a8b1fe60959156c13f68f0d284bbf4f0e80783115f54463e907d97eb` | 英文用户文档与规格说明 |
| `LICENSE-STATUS.md` | `a353b9b340c9b6e8b6b408ea645706fd150bc6de6de01869131d0eff5bd985cf` | 第三方组件与示例照片版权台账，加入 Wikimedia Commons 公有领域/CC0 条目 |

---

## 七、关键技术决策与误报阻断修复复盘

1. **幻灯片全屏背景图误判为遮挡卡片（False Blocker Fix）**：
   - 原 `_is_background_shape` 仅匹配 `rect`/`auto_shape`，遇全屏图片背景直接判定为前景卡片并计算面积碰撞，导致背景图被误判为遮挡。
   - **修复**：更新 `_is_background_shape` 支持识别 `area_ratio >= 0.85` 的全屏图片背景，并在几何分析与放置质量检测中准确传导 `slide_height`。
2. **计划替换图片与原图绑定的误报不一致（Media Binding False Positive Fix）**：
   - 原 `check_media_binding` 仅校验“输入全部图片 ＋ 计划新增图片 ＝ 候选全部图片”。当对低质原图进行正向场景图替换时，原图被替换移除，导致误报 `FIDELITY.IMAGE_IDENTITY_MISMATCH`。
   - **修复**：在 `checks.py` 中引入 `replaced_images` 白名单参数，从 `scene_visual_plan.json` 中读取被明确标记为计划替换的原图哈希，从预期集合中扣除，使得合法替换顺利通过完整性门禁。
3. **超长文稿模型生成页码超出范围防御（Page Clamping & Rebinding）**：
   - 本地模型在极端长上下文中偶发输出超出当前演示文稿最大页码的数字（如 13 页输出 Page 18）。
   - **修复**：在 `_normalize_semantics` 中比对证据文本与各页真实内容，将脱靶页码重新精准吸附至最匹配的有效幻灯片页，并在全量页面分配完备后安全丢弃冗余孤儿行。

---

## 八、发布限制与向 Codex 移交声明

1. **`release_ready` 状态保持**：
   - 候选工作台当前标签保持为 `DEVELOPMENT_CANDIDATE`，`release_ready` 严格保持为 `false`。
2. **正式切归前置条件**：
   - 所有证据、录像、哈希与纯净安装包已准备就绪，提交由独立验收人 Codex 进行最终核实验收。
   - 待 Codex 审查通过后，由 Codex 依授权将本候选版本合并切归至受保护工作台。
3. **第三方平台发布授权边界**：
   - GitHub 与 SkillHub 公开上架操作严禁越权。账户认证、2FA、商业主体和法律协议留待用户本人亲自签署确认。
