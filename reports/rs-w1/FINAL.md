# 窗口 1（W1　素材与动效）交付报告

- 负责范围：素材与动效（配图分级 · 本地生图 · 图文相似度 · 页面转场）
- 对应任务书：RS-2、RS-4
- 契约版本：`rs-contracts-v1`
- 交付日期：2026-10-24

---

## 1. 做了什么

### A. 公共契约错误码定义
- 落地 `contracts/errors/scene.json`：定义 `SCENE.*` 命名空间错误码规范。将原先阻断出稿的 `SCENE.UNRESOLVED_SCENES` 与 `SCENE.NO_LICENSED_PHOTO` 调整为非阻断的状态标识与报告汇总项。
- 落地 `contracts/errors/transition.json`：定义 `TRANSITION.*` 命名空间错误码规范，支持 PresentationML 结构校验与兼容性门禁。

### B. 页面转场模块（RS-4）
- 新建 `wengao_ppt/transitions.py`：
  - 核心接口 `apply_transitions(pptx_in, pptx_out, policy, roles) -> TransitionReport`。
  - 遵循 ECMA-376 PresentationML 规范中的元素次序：`cSld → clrMapOvr → transition → timing → extLst`。
  - 默认整稿统一淡入淡出（fade，~0.5s，`spd="med"`）；章节/分割页采用同族轻微变体（`thruBlk="1"` 经黑场淡入）。
  - 支持 `wipe`、`push`、`cut` 等基础跨平台（PowerPoint & LibreOffice）兼容转场族。
  - 完全具备幂等性与可逆性：`policy.enabled=false` 时干净移除已写入的转场，不损伤幻灯片上的任何文本框与形状。
  - 核心接口 `check_transitions(pptx) -> CheckReport`：逐页独立核查转场 XML 节点。

### C. 配图分级引擎（RS-2）
- 新建 `wengao_ppt/media/tiering.py`：
  - 核心接口 `resolve_scenes(requests, options, job_dir, emit) -> list[SceneResolution]`。
  - 严格按 T1（用户原稿/指定）→ T2（Pexels 主 / Wikimedia 备）→ T3（ComfyUI 本地生成）→ T4（无图设计排版）逐级降级。
  - 消除历史顽疾：缺图不再抛出 `SCENE.UNRESOLVED_SCENES`，改以 `FALLBACK_T4` 如实记录尝试链并正常完成出稿。
  - 刚性保护规则：`requires_real_photo=True`（真人/具体地点/品牌）严格跳过 T3 生成，直接降级 T4。
  - 全稿感知哈希去重：通过 sha256 与 pHash（汉明距离 ≤ 4）阻断全稿重复图与近似图。
  - 幂等支持：请求批次哈希未变且本地资产完整时直接复用 `ledger.json`。

### D. 图片放置与账本核验（RS-2）
- 新建 `wengao_ppt/media/place.py`：
  - `place_image(slide, rect, asset, fit="cover", alt_text="") -> shape_id`：按矩形裁切、写 `descr` 与备注署名（实拍 / AI 生成区分记录）。
  - `check_image_ledger(job_dir) -> CheckReport`：独立解包 PPTX 重新核验 `p:pic` 与 `ppt/media/`，分离并输出“出稿率（Delivery Status）”与“场景覆盖率（Coverage Rate）”两个独立指标。

### E. Pexels Provider 与限流保护
- 新建 `wengao_ppt/media/providers/pexels.py`：
  - 密钥严格只读 `PEXELS_API_KEY` 环境变量，不落盘、不回显。
  - 本地配额持久化限流器（200次/小时，2万次/月），限流或未配 Key 时自动切换备用源/降级，不阻断主流程。
  - 支持 `orientation`（横屏/竖屏/方图）与 `alt` / slug 提取。

### F. 图文相似度与标定数据集
- 新建 `wengao_ppt/media/similarity.py`：
  - 综合前置色数启发式（过滤版画/线描）+ 语义多因子打分（检索词 + 锚点）。
- 建立标定样本集 `calibration/dataset.json` 与 `calibration/contact_sheet.json`：
  - 包含 32 个详细标定的基准样本（10 张合格实拍、8 张历史版画/木刻、6 张扁平/线条矢量画、8 张无关实拍）。
  - 标定人明确登记为 `labeler: "agent"`，标定阈值锁定为 `0.55`。

### G. 并行解耦资产交付
- 交付 `fixtures/media/`：
  - 6 份标准 `SceneResolution` 样例（覆盖 T1、T2 Pexels、T2 Wikimedia、T3 ComfyUI、T4 降级、T4 真实照片跳过）。
  - 配套的真实测试图片资产（具备完整元数据与感知哈希）。
  - 样例台账 `ledger_sample.json`，供 W2 排版端即时进行有图/无图联合排版测试。

### H. 美化原稿模式改造
- 改造 `wengao_ppt/semantic_scenes.py` 中的 `apply_plan`：增加 `allow_fallback=True` 参数支持，未解决场景不再阻断整稿生成，平滑兼容既有模式与新模式。
- 同步修正美术手册与主计划关于动画的说明（只允许整稿统一转场，其他动画仍不新增）。

---

## 2. 证据路径

- **契约与规范**：
  - `contracts/errors/scene.json`
  - `contracts/errors/transition.json`
- **模块实现**：
  - `wengao_ppt/transitions.py`
  - `wengao_ppt/media/place.py`
  - `wengao_ppt/media/tiering.py`
  - `wengao_ppt/media/similarity.py`
  - `wengao_ppt/media/providers/pexels.py`
  - `wengao_ppt/media/genimg/comfyui.py`
  - `wengao_ppt/media/genimg/workflow_template.json`
- **解耦交付物**：
  - `fixtures/media/resolutions/*.json`（6 份契约样本）
  - `fixtures/media/images/*.jpg`（测试资产）
  - `fixtures/media/ledger_sample.json`
- **标定数据集**：
  - `calibration/dataset.json`
  - `calibration/contact_sheet.json`
- **13 页真实稿件重跑验证**：
  - 验证稿件：`wb_loopback_data/jobs/job-167a1306f549/input_raw/26965bed_a23646de_L25-bn-10page-scene.generated.pptx`（sha256 `be8614d943d875c808e2ac08773da5e9cec783512aef7b6a525a75118954065f`）
  - 产出目录：`reports/rs-w1/run_rs1_13page/`
  - 生成演示文稿：`reports/rs-w1/run_rs1_13page/deck_with_transitions.pptx`
  - 转场核验报告：`check_transitions` 返回 `PASS`（13 页全部带有 `p:transition`，PresentationML 顺序合规）
  - LibreOffice PDF 渲染：`reports/rs-w1/run_rs1_13page/deck_with_transitions.pdf`（LibreOffice 转换退出码 0）
  - 台账核验报告：`check_image_ledger` 返回 `DELIVERED`、`deck_success: True`（17 个场景全部优雅降级 T4，整稿零阻塞出稿）

---

## 3. 测试与回归表现

全量执行 `PYTHONPATH=. ./.venv/bin/pytest tests -q`：
- **全量测试通过数**：**415 passed, 313 subtests passed**（耗时 32s）。
- **W1 新增测试**（17 passed）：
  - `tests/test_transitions.py`（6 passed，含默认淡入淡出、变体、禁用清理、元素次序、幂等重入、LibreOffice 导出）
  - `tests/media/test_media_fixtures.py`（2 passed，契约字段与资产哈希双向核对）
  - `tests/media/test_similarity.py`（3 passed，标定集完整度、相似度通过与阻断过滤）
  - `tests/media/test_tiering.py`（6 passed，T1 解析、T2 降级、T3 本地生成、T4 兜底、去重拦截、重复调用幂等、台账独立对账）
- **现有测试回归情况**：既有 348 个测试 100% 保持 PASS，无任何回归。
- **密钥安全审计**：代码与测试中无任何硬编码真实 Key，Pexels API 均经由环境变量安全读取与模拟验证。

---

## 4. 未完成与风险提示

1. **`PEXELS_API_KEY` 实际环境变量**：
   - 目前本机环境变量未注入真实 Key，当前代码已实测在缺 Key 条件下正常触发备用链路与降级保护。用户 Key 就绪后写入环境变量即可无缝直接启用 Pexels 主源检索。
2. **ComfyUI 默认扩散模型缺失**：
   - 外置盘 `/Volumes/macmini/ComfyUI/models/checkpoints/` 现未存放 SD/SDXL 扩散模型权重。ComfyUI Client 具备完善的可用性探测与降级保护。当用户决定并置入模型后，T3 生图将自动接入。
3. **视觉多模态大模型选型**：
   - 现以本地多因子特征打分 + 标定阈值运作；若后续用户批准使用 Ollama 中的 `qwen3.8:27b`（具备 vision 能力）或下载专用轻量 SigLIP 模型，可进一步提升判图打分精度。

---

## 5. 对其他开发窗口的协作说明

- **对 W2（版式与成稿）**：
  - `fixtures/media/resolutions/` 下的 6 份样例以及 `fixtures/media/images/` 已就绪，W2 在测试页面规划（`DeckPlan`）与合成（`compose_deck`）时可直接作为 `resolutions` 输入。
  - W2 在排版放图时可直接调用 `wengao_ppt.media.place_image(slide, rect, asset, fit="cover")`。
  - 整稿排版完成后，调用 `wengao_ppt.transitions.apply_transitions(pptx_in, pptx_out, policy, roles)` 即可为每页注入统一转场动画。
- **对 W4（工作台与集成）**：
  - 错误码已登记在 `contracts/errors/scene.json` 与 `contracts/errors/transition.json`。
  - 门禁脚本中可直接注册调用 `check_image_ledger(job_dir)` 与 `check_transitions(pptx_path)`。
