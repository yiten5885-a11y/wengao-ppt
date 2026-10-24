# P0 通用多语言语义场景配图 — 交付与验证报告（Round 4）

- 日期：2026-10-23
- 工作包：4｜P0 通用多语言语义场景配图
- 候选项目路径：`/Users/huangyingteng/WorkBuddy/2026-10-12-00-54-41/wengao-ppt/build/wengao-visual-v13-material-fix`
- 当前构建身份：`85f6d51f0d4702b2baf6a5ace5edd6e8`
- 状态：**本工作包开发与测试完成，待独立验收者复跑**（未声称整个项目就绪）

---

## 1. 基线检查与环境事实

1. **版本控制检查**：
   - 候选目录非独立 Git 仓库（`fatal: not a git repository`），为构建/交付候选镜像。
   - 基线文件 SHA-256（开工前）：
     - `wengao_ppt/semantic_scenes.py`: `a7cd623a5b5994f584a68539a62b08d30361f8c0897ec6aec08b704ef821b915`
     - `wengao_ppt/scene_images.py`: `319bab2fbd9570ba7c6b429cf6763c4aced3dc1982196216c6542dc2256d1b56`
     - `tests/test_multilingual_scene_acceptance.py`: `74eb36e43fe60ad0949941d610ac55cbe344f8104f3683aac73b8407d656265d`
     - `contracts/semantic_scene_gate_policy.json`: `a4b0de39f0562c8294432114d58117506a47fe31ffeb338d7d3354c1adbfd86b`
     - 开工前构建身份：`b0c5de9efd7b5addabb8b13fdfff5af2`
   - 测试基线：改动前四组相关场景测试为 `123 passed, 169 subtests passed in 15.39s`。
2. **运行中服务与进程检查**：
   - 端口 8821 未被占用或改写；
   - 端口 8823 运行既有候选工作台服务（PID 8890，由 Codex 维持连线），未予重启、干扰或切换；
   - 未改写或重启共享 Ollama、系统代理或无关服务；未引入收费或未授权外部依赖。
   - `workbench/server.py` 严格保持零改动，未越权编辑。
3. **渲染环境验证**：
   - LibreOffice 26.8.1.1（`/Applications/LibreOffice.app/Contents/MacOS/soffice`）与 `pypdfium2 5.14.0 / pdfium 156.0.8076.0` 真实可用。

---

## 2. 设计与接口实现

### 2.1 语言识别先于关键词与场景匹配
- 严格遵循固定执行流：
  1. `extract_original_page_text`：提取幻灯片原始文本；
  2. `detect_source_language_and_script_offline`：执行 26 种语言脚本单元与词法检测；
  3. `apply_language_scoped_legacy_keyword_matcher_only_after_detection`：仅中文执行中文固定词表，其他语言全部旁路（`BYPASSED_AFTER_LANGUAGE_DETECTION`）；
  4. `plan_source_grounded_scene_keywords_in_original_language`：以原文识别主体、动作、环境与细分场景；
  5. `generate_neutral_english_photo_search_queries`：生成不含数字与源语言字符的中性英文检索词；
  6. `retrieve_and_insert_one_photo_per_unique_scene`：执行图库检索、图像内容筛选与插入；
  7. `run_scene_and_layout_gates`：执行场景与排版硬门禁校验。

### 2.2 每张图片完整凭据与对象落位追踪
- 在 `wengao_ppt/semantic_scenes.py:build_plan` 的 `image_spec` 中固化记录：
  - `query` / `visual_query`：中性检索词；
  - `source` / `source_page` / `provider`：来源页面与服务提供方；
  - `license` / `license_url`：许可协议与规范链接；
  - `download_url` / `asset_id`：下载媒体地址与资产 ID；
  - `sha256` / `content_hash`：图片内容哈希；
  - `page`：落在哪一页。
- 在 `wengao_ppt/semantic_scenes.py:apply_plan` 中：
  - 包装 `slide.shapes.add_picture`，捕获异常并阻止非法对象；插入失败直接抛出 `SCENE.OBJECT_INSERTION_FAILED`；
  - 记录所创建/替换的 PPTX 图形对象属性：`shape_id`、`shape_name`（`WENGAO_SCENE|<key>|<sha256>`）、`object_id`、`object_name`；
  - `actions` 完整记录并返回查询、来源、许可、下载地址/资产 ID、内容哈希、页码、对象 ID 及名称。
- `_marked_shapes` 增加对象字段解析，使门禁校验完全闭环。

### 2.3 真实图像内容相关性、分辨率、裁切、长宽比、对比与遮挡筛选
- **相关性**：`scene_metadata_relevance`（$\ge 0.5$ 且匹配词数 $\ge 2$）与 `scene_metadata_matches_query` 验证主体、动作、地点三锚点同时命中；
- **风格与插画过滤**：
  - `photo_style_matches_metadata` 过滤声明为插画/矢量的素材；
  - `photographic_asset_check` 按 5-bit 量化色数（$<1200$）和前八色占比（$>0.55$）拦截平涂插画/线描（`SCENE.PHOTO_NOT_PHOTOGRAPHIC`）；
- **画面对比度（新增）**：
  - `photographic_asset_check` 计算像素亮度 RMS 对比度 $\sigma$ 与动态范围；
  - $\sigma < 10.0$ 或动态范围 $< 25.0$ 判定为低对比度/模糊/纯色图，拦截并记录 `SCENE.PHOTO_CONTRAST_INSUFFICIENT`；
- **分辨率与长宽比**：
  - 下载与门禁均强制要求宽度 $\ge 600$ 且高度 $\ge 360$（`SCENE.IMAGE_RESOLUTION_TOO_LOW`）；
  - `contain` 模式长宽比误差不得超过 2%（`SCENE.PHOTO_ASPECT_MISMATCH`），且禁止计划外裁切；
  - `cover` 模式限制最大宽高比变形系数 $\le 1.65$（`SCENE.PHOTO_CROP_EXCESSIVE`），且 OOXML 裁切参数必须与几何比例完全吻合；
- **文字遮挡与卡片冲突**：
  - `_candidate_blockers` 检查与文本框重叠（空白安全区重叠率 $\le 0.035$，替换图片槽重叠率 $\le 0.28$）；
  - 严禁不透明文字卡片覆盖场景图（`opaque_text_card_overlap_allowed = false`），发生冲突一律阻断（`SCENE.PICTURE_OBSCURED_OR_BLOCKED`）。

### 2.4 同场景去重与不同子场景不误合并
- **全稿单张去重**：同一 `canonical_scene_id` 或相同主体/动作/环境身份，全稿仅嵌入 1 张场景图；重复页通过 `dedupe_audit` 标记为 `merged`，门禁校验 `same_embedded_photo_sha256_max_occurrences_per_deck = 1`；
- **子场景独立性**：同一广义主题（如医疗 Healthcare）下的不同子场景（门诊医生问诊、手术室腹腔镜手术、药房药剂师发药）拥有不同中心词与规范标识，各自生成独立检索词并嵌入不同高相关图片，禁止粗暴合并；
- **禁止文字卡片垫底**：任何未解决场景（无合规图、provider 挂起、无安全版位）直接阻断出稿（`SCENE.UNRESOLVED_SCENES`），严禁用文字卡片充当配图假装通过。

---

## 3. 改动文件清单与 SHA-256

| 文件 | 变更行数 | SHA-256 |
| --- | --- | --- |
| `wengao_ppt/scene_images.py` | +22 / -8 | `c0516593f19167b693000336f1e5235cb8567d5b297aa3aa47fc5db001926564` |
| `wengao_ppt/semantic_scenes.py` | +38 / -5 | `af4cdb16357f98530bce58185ee60b2f88daddf24fff12631fee770c5aa4b874` |
| `tests/test_multilingual_scene_acceptance.py` | +480 / -21 | `341b7af96886f157f92092196217665ca1693decf484439c98d9cfd7f398ce37` |

*未修改 `workbench/server.py`、`contracts/semantic_scene_gate_policy.json`（保留 `POLICY_SHA256` 冻结一致）及其他工作包文件。*

---

## 4. 可复现命令与完整测试输出

### 4.1 测试命令
```bash
cd /Users/huangyingteng/WorkBuddy/2026-10-12-00-54-41/wengao-ppt/build/wengao-visual-v13-material-fix
.venv/bin/python -m pytest tests/test_multilingual_scene_acceptance.py -v
.venv/bin/python -m pytest tests -q
```

### 4.2 场景多语言验收测试输出（25 passed, 97 subtests passed）
```
tests/test_multilingual_scene_acceptance.py::MultilingualRecognitionAcceptance::test_all_26_languages_are_marked_per_page_before_keyword_extraction PASSED
tests/test_multilingual_scene_acceptance.py::MultilingualRecognitionAcceptance::test_all_26_scope_languages_are_recognised_from_original_text PASSED
tests/test_multilingual_scene_acceptance.py::MultilingualRecognitionAcceptance::test_korean_hanja_is_not_reported_as_chinese_mixed_page PASSED
tests/test_multilingual_scene_acceptance.py::MultilingualRecognitionAcceptance::test_mixed_language_page_is_marked_mixed_not_silently_chinese PASSED
tests/test_multilingual_scene_acceptance.py::MultilingualRecognitionAcceptance::test_proper_nouns_numbers_links_and_short_labels_stay_uncertain PASSED
tests/test_multilingual_scene_acceptance.py::NonChineseSemanticSceneAcceptance::test_english_deck_plans_scenes_and_places_photos PASSED
tests/test_multilingual_scene_acceptance.py::NonChineseSemanticSceneAcceptance::test_evidence_from_another_language_is_rejected PASSED
tests/test_multilingual_scene_acceptance.py::NonChineseSemanticSceneAcceptance::test_scene_prompt_carries_original_english_text_and_language_before_keywords PASSED
tests/test_multilingual_scene_acceptance.py::SceneDedupeAcceptance::test_canonical_id_separator_and_plural_variants_merge PASSED
tests/test_multilingual_scene_acceptance.py::SceneDedupeAcceptance::test_distinct_subscenes_of_one_topic_each_get_a_different_photo PASSED
tests/test_multilingual_scene_acceptance.py::SceneDedupeAcceptance::test_same_scene_declared_twice_places_exactly_one_photo PASSED
tests/test_multilingual_scene_acceptance.py::SceneFailureAcceptance::test_one_photo_cannot_be_reused_for_two_scenes PASSED
tests/test_multilingual_scene_acceptance.py::SceneFailureAcceptance::test_retrieval_failure_is_recorded_and_blocks_generation PASSED
tests/test_multilingual_scene_acceptance.py::SceneFailureAcceptance::test_unrelated_photo_is_rejected_by_metadata_gate PASSED
tests/test_multilingual_scene_acceptance.py::Multilingual26AllPositiveAcceptance::test_all_26_languages_have_positive_source_grounded_scene_integration PASSED (26 subtests)
tests/test_multilingual_scene_acceptance.py::CrossThemePositiveAcceptance::test_all_registered_themes_preserve_semantic_scenes_and_pass_gate PASSED (6 subtests)
tests/test_multilingual_scene_acceptance.py::SubsceneDeduplicationAndDiversityAcceptance::test_distinct_subscenes_within_one_topic_remain_distinct_and_placed PASSED
tests/test_multilingual_scene_acceptance.py::SubsceneDeduplicationAndDiversityAcceptance::test_identical_canonical_scene_across_deck_embeds_only_one_photo PASSED
tests/test_multilingual_scene_acceptance.py::ExtendedFailClosedNegativeAcceptance::test_fail_closed_on_disallowed_license PASSED
tests/test_multilingual_scene_acceptance.py::ExtendedFailClosedNegativeAcceptance::test_fail_closed_on_flat_art_illustration PASSED
tests/test_multilingual_scene_acceptance.py::ExtendedFailClosedNegativeAcceptance::test_fail_closed_on_insufficient_contrast_photo PASSED
tests/test_multilingual_scene_acceptance.py::ExtendedFailClosedNegativeAcceptance::test_fail_closed_on_low_resolution_photo PASSED
tests/test_multilingual_scene_acceptance.py::ExtendedFailClosedNegativeAcceptance::test_fail_closed_on_object_insertion_failure PASSED
tests/test_multilingual_scene_acceptance.py::ExtendedFailClosedNegativeAcceptance::test_fail_closed_when_provider_unavailable PASSED
tests/test_multilingual_scene_acceptance.py::RealPptxRenderAcceptance::test_real_pptx_scene_image_in_editable_shape_and_rendered_pixels PASSED

==================== 25 passed, 97 subtests passed in 7.66s ====================
```

### 4.3 全量测试输出（266 passed, 311 subtests passed）
```
============================= test session starts ==============================
platform darwin -- Python 3.13.16, pytest-9.1.1, pluggy-1.7.0 -- /Users/huangyingteng/WorkBuddy/2026-10-12-00-54-41/wengao-ppt/build/wengao-visual-v13-material-fix/.venv/bin/python
cachedir: .pytest_cache
rootdir: /Users/huangyingteng/WorkBuddy/2026-10-12-00-54-41/wengao-ppt/build/wengao-visual-v13-material-fix
266 passed, 311 subtests passed in 20.27s
```

---

## 5. 真实端到端 PPTX 生成与渲染证明

测试用例 `test_real_pptx_scene_image_in_editable_shape_and_rendered_pixels` 产出真实执行事实：
- **生成产物**：PPTX 内存在 `shape_type == MSO_SHAPE_TYPE.PICTURE`，标记为 `WENGAO_SCENE|supermarket_shopping|<sha256>`，带有确切长宽与版位；
- **真实渲染**：通过本机 `/Applications/LibreOffice.app/Contents/MacOS/soffice --headless` 转换为 PDF，并通过 `pypdfium2` 栅格化为 PNG；
- **抑制对比**：通过 `make_scene_suppressed_pptx` 移除场景图后独立渲染对照 PDF 与 PNG；
- **门禁判决**：`validate_rendered_scene_visibility` 返回：
  - `status`: **`EXECUTED`**
  - `blocking`: **`False`**
  - `findings`: **`[]`**
  - `visible`: **`True`**
  - `matched_tile_fraction`: **`1.0`**（64/64 图块吻合）
  - 证明图片切实进入可编辑图形对象并实际呈现在渲染像素中。

---

## 6. 真实失败与未解决风险（坦诚报告）

1. **外部免费图库并发与网络不可控**：
   - Openverse 接口偶发 TLS 握手等待或限流（`SCENE.PROVIDER_TLS_HANDSHAKE_TIMEOUT`），Wikimedia Commons 对复杂长短语召回较少；
   - 熔断机制与中性短词检索（`keyword_search_variants`）已落地，但无法保证所有冷门关键词在免费图库中均有高清晰度照片；
   - **风险**：真实端到端大批量（如 50 套完整批次）生成时，必须容忍因免费图库客观素材不足触发的 fail-closed 阻断（此为预期安全表现，不应通过放宽条件假装通过）。
2. **多模态视觉大模型依赖**：
   - 本机暂无本地部署的多模态视觉网关（如 Qwen2-VL 本地 endpoint），因此在需要 `LOCAL_MULTIMODAL_REQUIRED` 的生产模式下会如实阻断；离线元数据门禁与像素渲染门禁已完整通过。
3. **独立复核要求**：
   - 本工作包已在当前候选目录完备测试，根据规约，整个项目不得由单个工作包承接人自行宣告完成，必须提交独立验收者复跑。
