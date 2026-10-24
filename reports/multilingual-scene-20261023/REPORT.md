# 多语言语义场景配图 — 开发与独立验收报告

- 日期：2026-10-23
- 候选项目：`/Volumes/huangyingteng/WorkBuddy/2026-10-12-00-54-41/wengao-ppt/build/wengao-visual-v13-material-fix`
  （原始请求路径 `/Users/huangyingteng/...` 在本机以 SMB 卷 `//…黄应腾的Mac Mini/huangyingteng` 挂载）
- 阶段范围：既有 26 语言清单（`existing-matrix-26-v1`），未扩展，也不声称覆盖全球所有语言
- 状态：**未发布、未宣称完成**

## 0. 结论摘要

| 项目 | 结果 |
| --- | --- |
| 代码改动（4 个模块 + 1 份契约 + 1 份新测试） | 已完成 |
| 离线自动化验收（26 语言、混合、专名/数字/短文本、语义场景、去重、无关图、检索失败） | **通过**：195 passed / 238 subtests |
| 真实产品流程（非中文新样例） | **未通过**：在 `semantic_scenes` 阶段模型调用失败，未生成 PPTX、未做图片检索、无预览 |
| 50 套真实 PPT 开源前门槛 | **0 / 50**（本轮无真实生成产物） |

真实生成未通过的原因是环境阻塞（无本机模型网关、无渲染器），不是离线测试掩盖的“假通过”。离线测试文件显式声明其证据边界，见第 4 节。

## 1. 开工前状态检查

1. 目录存在，**不是 git 仓库**（`fatal: not a git repository`），因此用文件 mtime 判定近期改动。
2. Codex 近期修改（2026-10-23 00:18–00:22，距开工约 20 分钟）：
   - `wengao_ppt/scene_model.py`（00:18）
   - `wengao_ppt/semantic_scenes.py`（00:22）
   - `tests/test_semantic_scenes.py`（00:22）
   - `contracts/semantic_scene_gate_policy.json`（00:21）
3. 处理方式：**未回退任何 Codex 改动**。`scene_model.py`、`test_semantic_scenes.py`、`semantic_scene_gate_policy.json` 本轮零改动；`semantic_scenes.py` 只做 6 处追加式修改（去重规范化 + 审计链 + 计划字段），未删除或重写 Codex 逻辑。
4. 运行环境：本机隔离 venv `/Users/yitenhua/.workbuddy-ai/binaries/python/envs/wengao`（pytest 9.1.1、python-pptx 1.0.2、Pillow 13.0.0、lxml 6.1.3）。项目本身无 venv。
5. 基线：改动前全量 `181 passed, 173 subtests passed`。

### 1.1 存储层异常（必须记录）

SMB 卷在测试期间出现瞬时 I/O 失败：

```
OSError: [Errno 5] Input/output error
  contracts/semantic_scene_gate_policy.json
```

一次全量运行因此有 9 项失败（全部为 `OSError`，非断言失败）。为区分代码问题与存储问题，将项目同源镜像到本地磁盘 `/tmp/wengao-local` 复跑：

- 镜像后逐文件校验 `sha256` 一致（4 个改动文件全部 OK）
- 本地复跑：`195 passed, 238 subtests passed`（无 I/O 错误）
- 稍后在挂载卷原路径复跑：`195 passed, 238 subtests passed in 189.79s`（I/O 恢复正常，结论一致）
- 判定：那 9 项失败为网络卷瞬时抖动，**不是代码回归**；两处运行结论一致

## 2. 改动文件清单

| 文件 | 行数 | SHA-256 |
| --- | --- | --- |
| `wengao_ppt/language_support.py` | 818 | `0b54aaaa1ed27ec1b116a9266625ce656092bd4ed3c03a16ac3b17cfb69466aa` |
| `wengao_ppt/semantic_scenes.py` | 3027 | `a7cd623a5b5994f584a68539a62b08d30361f8c0897ec6aec08b704ef821b915` |
| `wengao_ppt/visual_plan.py` | 345 | `0d780177fdb9906c539df20d6fe98eaa63e084b2419c7762ca72468e352b0100` |
| `tests/test_multilingual_scene_acceptance.py`（新增） | 585 | `74eb36e43fe60ad0949941d610ac55cbe344f8104f3683aac73b8407d656265d` |
| `contracts/language_acceptance_scope_v1.json` | 98 | `63331e2058c35fbb3f21a79c5f52e953fd32ebb400ccd484ac60ccef63d6c598` |

### 2.1 `wengao_ppt/language_support.py`（检测器 v3 → v4）

- 主语言判定改为**跨脚本语义单元占比**：汉字/假名/谚文/泰文/泰米尔/泰卢固按字计 1 单元，拉丁按 3.5 字符折 1 单元，天城文/孟加拉 2.5，阿拉伯/希伯来/西里尔/希腊 3。解决“中文页夹英文产品词被判成英文页”。
- 修正韩文汉字误判：文本含谚文时，Han 字符不再当作中文证据（原先 `고객이 商品을…` 被判 `mul[ko,zh]`）。
- 剥离 URL / 邮箱 / 主机标签后再做词法打分：原先 `https://example.com/a/b?c=1` 因 `com` + `a` 被判为葡萄牙语。
- 13 种拉丁字母语言补充日常实词（商超/结算/票据/医疗/增长等），并加入**有界屈折匹配**：≥6 字母词干允许词内匹配（`anazungumza` ← `zungumza`），≥4 字母词干允许≤4 字符后缀（`shoppers` ← `shop`、`tüketiciler` ← `tüketici`）。
- 新增字段：`mixed`、`mixed_languages`、`script_shares`、`script_unit_shares`、`lexical_evidence`、`uncertain`。
- `language_support_record()` 新增 `mixed_language_pages`、`uncertain_language_pages`、`pending_language_handling`。

### 2.2 `wengao_ppt/semantic_scenes.py`

1. 新增 `_normalize_canonical_scene_token()`：统一大小写、`-`/`.`/空格等分隔符、重复下划线、简单英文复数（`consumer.checkouts` → `consumer_checkout`）。
2. `_identity_signature()` 改用该规范化；**不改写中心名词**，因此 `consumer_checkout` 与 `consumer_receipt` 仍为两个场景。
3. `canonical_scene_id` 入口改为“先规范化再校验”：接受分隔符/复数变体，纯非英文或规范化后为空的键仍抛 `SCENE.CANONICAL_SCENE_ID_INVALID`（原先带连字符的合法场景键会让整轮规划直接失败）。
4. `_normalize_semantics()` 新增可选 `dedupe_audit` 出参，记录每条 `kept` / `merged` 决策。
5. `build_plan()` 把 `dedupe_audit` 写入计划，便于独立复核“合并了什么、保留了什么”。

### 2.3 `wengao_ppt/visual_plan.py`

- 新增 `_language_state()`：`detected` / `mixed` / `uncertain`。
- 每页计划新增 `language_state`、`mixed_languages`；语言不确定页的原因文案改为“已记录为待处理，不套用中文固定词表”。
- 计划新增 `language_pending`：`uncertain_pages`、`mixed_pages`、`handling`。

## 3. 规则说明

### 3.1 语言识别必须先于关键词与场景规划

固定顺序（已由契约与测试双重锁定）：

```
extract_original_page_text
→ detect_source_language_and_script_offline
→ apply_language_scoped_legacy_keyword_matcher_only_after_detection（仅 zh）
→ plan_source_grounded_scene_keywords_in_original_language
→ generate_neutral_english_photo_search_queries
→ retrieve_and_insert_one_photo_per_unique_scene
→ run_scene_and_layout_gates
```

- 原文与原文证据全程本地保留；只有中性英文 `visual_query` 允许外发。
- 非中文页、`und` 页、混合页一律不套用中文固定标签库。
- **已知设计取舍（待评审确认）**：当页面为“中文为主 + 英文实词”（`primary=zh, mixed=true`）时，旧中文标签库仍会运行（`ZH_ONLY_APPLIED`），理由是中文原文确实存在、匹配只作用于中文标签；同时该页被标记 `mixed` 并进入开放词表规划器。若评审要求“mixed 一律旁路中文词库”，只需把 `visual_plan.py` 的判定条件从 `primary == "zh"` 收紧为 `primary == "zh" and state == "detected"`，本轮未擅自改变该行为。

### 3.2 场景规划（语言无关的同一语义流程）

- 模型按原文识别 `subject` / `action` / `setting` / `environment` / `action_state`，并输出 `lighting` / `composition_camera` / `style` / `quality` 作为生成描述字段。
- 检索描述规范化为英文 `photo_search_terms{subject, action, setting}` → 权威 `visual_query`；最多 2 个英文同义备选，仅扩大召回、不放宽准入。
- 中文、英文或其他语言走同一提示与同一校验；备选查询若不能保留原锚点即被拒绝。
- 原文证据必须逐字连续出现在对应页原文中，否则 `SCENE.EVIDENCE_NOT_GROUNDED`（跨语言“翻译证据”会被直接拒绝，本轮已加测试）。

### 3.3 场景级去重与多样性

| 规则 | 实现 |
| --- | --- |
| 同一具体场景全稿最多一张 | 规范化 `canonical_scene_id` 作为唯一身份，重复定义合并为一个 scene |
| 同主题不同子场景可分别配图 | 规范化不改写中心名词；商超/结算/票据各自独立身份 |
| 图片身份不得重复 | 全稿 `sha256` 唯一 + 感知哈希距离 ≤4 视为近重复并阻断 |
| 去重可审计 | 计划写入 `dedupe_audit`（kept/merged + 签名） |
| 门禁二次独立校验 | `validate_output` 用同一 `_identity_signature` 重新判重，防止手工改计划绕过 |

### 3.4 放置与失败记录

- 场景图经 `apply_plan` 真实插入 PPTX，图片名标记 `WENGAO_SCENE|<scene_key>|<sha256>`，并写入许可署名到备注与 `descr`。
- 计划冻结 `rect`、`area_ratio`（≥0.12）、`text_overlap_ratio`（≤0.28）、`placement_mode`、`fit_mode`；门禁按实际嵌入字节复算面积、矩形、裁切几何、页号、感知哈希。
- 失败/待处理显式落盘：
  - 检索失败 → `SCENE.NO_LICENSED_PHOTO`（附每次查询的失败码）
  - 无安全版位 → `SCENE.NO_SAFE_PHOTO_AREA`
  - 元数据不相关 → `SCENE.IMAGE_CONTEXT_UNVERIFIED` / `SCENE.IMAGE_RELEVANCE_UNVERIFIED`
  - 复用/近重复 → `SCENE.PHOTO_REUSED_OR_UNVERIFIED` / `SCENE.NEAR_DUPLICATE_PHOTO`
  - 语言不确定 → `visual_plan.language_pending.uncertain_pages`
  - 任一未解决场景存在时 `apply_plan` 抛 `SCENE.UNRESOLVED_SCENES`，不允许出稿
- 不存在“文字卡/占位图冒充配图成功”的路径（测试对计划序列化文本断言不含 `text_card` / `placeholder_image`）。

## 4. 测试命令与逐项结果

运行环境（本地同源镜像与挂载卷原路径各跑一次，结果一致）：

```bash
PY=/Users/yitenhua/.workbuddy-ai/binaries/python/envs/wengao/bin/python
cd /tmp/wengao-local            # 或挂载卷原路径
$PY -m pytest tests -q -p no:cacheprovider
# → 195 passed, 238 subtests passed in 207.18s（本地镜像）
# → 195 passed, 238 subtests passed in 189.79s（挂载卷原路径）
```

新增/相关用例逐项：

| 要求 | 测试用例 | 结果 |
| --- | --- | --- |
| 26 语言逐一识别 | `test_all_26_scope_languages_are_recognised_from_original_text`（26 子测试） | PASS |
| 26 语言逐页标记 + 中文词库仅限中文 | `test_all_26_languages_are_marked_per_page_before_keyword_extraction`（26 子测试） | PASS |
| 混合语言识别与路由 | `test_mixed_language_page_is_marked_mixed_not_silently_chinese` | PASS |
| 韩文汉字不误判 | `test_korean_hanja_is_not_reported_as_chinese_mixed_page` | PASS |
| 专名/数字/链接/短文本 → 待处理 | `test_proper_nouns_numbers_links_and_short_labels_stay_uncertain`（5 子测试） | PASS |
| 非中文语义场景规划 + 实际插入 + 门禁 | `test_english_deck_plans_scenes_and_places_photos` | PASS |
| 跨语言证据拒绝 | `test_evidence_from_another_language_is_rejected` | PASS |
| 原文与语言先于关键词进入提示 | `test_scene_prompt_carries_original_english_text_and_language_before_keywords` | PASS |
| 同一场景声明两次只放一张 | `test_same_scene_declared_twice_places_exactly_one_photo` | PASS |
| 分隔符/复数变体合并 | `test_canonical_id_separator_and_plural_variants_merge` | PASS |
| 同主题不同场景各自配图 | `test_distinct_subscenes_of_one_topic_each_get_a_different_photo` | PASS |
| 一张图不得用于两个场景 | `test_one_photo_cannot_be_reused_for_two_scenes` | PASS |
| 无关图片拒绝 | `test_unrelated_photo_is_rejected_by_metadata_gate` | PASS |
| 检索失败不假通过 | `test_retrieval_failure_is_recorded_and_blocks_generation` | PASS |
| 既有语言矩阵契约 | `tests/test_multilingual_matrix.py`（16 项 / 全 26 语言子测试） | PASS |

**离线与真实的区分**：上述用例全部为离线证据。语义规划结果由测试注入（`semantic_resolution=`），照片由固定 fixture provider 提供。文件头已声明：这些测试证明路由、去重、放置与门禁行为，**不证明真实模型质量与真实图库检索**。

## 5. 真实样例结果（非中文新样例）

样例：`retail_clinic_en.pptx`，6 页英文（零售篮子迁移 / 自助结算 / 小票存证 / 冷链压力 / 门诊候诊 / 下一步），
`sha256 = 6665b85513ebb6927fdb755bf40e600e3647091bd946abf6352722f3ed79f7ea`

| 阶段 | 结果 |
| --- | --- |
| `precheck` | `verdict=QUEUED`，`job_id=job-2ba9c9bb22c3`，`support_profile=P1_TEXT_LIMITED`，`upstream_verified=VENDOR_DIR_MISSING`，`real_renderer=MISSING` |
| `run` | **FAILED**，`stage=semantic_scenes`，`fail_reason=MODEL.HTTP_ERROR` |
| 请求模型 | `qwen3.8:27b-wb65536`（`WENGAO_SCENE_MODEL` 未设置，取默认值） |
| 端点发现 | `127.0.0.1:18888/health`、`127.0.0.1:11434/api/tags` 均无响应 → 回退 `http://127.0.0.1:18888` |
| 经环境代理的错误 | `{"code":"MODEL.HTTP_ERROR","provider":"ollama-local","status_code":502,"detail":"upstream connect failed: Connection refused (os error 61)"}` |
| 绕过环境代理的错误 | `{"gate":"MODEL.TRANSPORT_ERROR","detail":"本机语义模型连接失败（ConnectionRefusedError）。"}` |
| 图片检索 | 未发生（模型阶段即失败） |
| 插入后 PPTX | 无产物，无身份可记录 |
| 预览 | 不可用（无 LibreOffice；`real_renderer=MISSING`） |

阻塞点（两项独立）：

1. 本机 `127.0.0.1:18888` 与 `127.0.0.1:11434` 均无模型网关进程（`curl --noproxy '*'` 返回连接拒绝）。
2. 环境自带 `HTTP_PROXY=http://127.0.0.1:52719` 会把 loopback 请求也代理出去，把“连接拒绝”包装成 HTTP 502，容易误读为“服务返回错误”。

按约束：未重启 8821/8823 或任何共享服务，未修改代理/Ollama 配置，未下载模型，未购买额度，未调用 Claude 或任何付费模型。上述“绕过代理”仅为单次诊断子进程的环境变量隔离，未改动任何共享配置。

## 6. 生成文件哈希

| 产物 | SHA-256 |
| --- | --- |
| 输入样例 `retail_clinic_en.pptx` | `6665b85513ebb6927fdb755bf40e600e3647091bd946abf6352722f3ed79f7ea` |
| 生成 PPTX | **无（本轮未生成）** |
| 场景计划 JSON | **无（模型阶段失败，未产出计划）** |

## 7. 尚未通过的验收项

1. **真实模型语义规划（26 语言 × 5 层）**：0/26 有真实调用证据。矩阵中仅 `zh`/`en` 为 `FULL_SUPPORT`，其余仍 `PARTIAL_SUPPORT` / `PENDING_VERIFICATION`。
2. **真实图片检索与插入**：0 套。检索、许可元数据、视觉复核、实际放置均未在真实链路跑通。
3. **26 语言排版层（RTL / CJK / 复杂文字字体回退）**：未验收。
4. **预览与真实渲染**：无 LibreOffice，`render.real_render` 无法评估。
5. **50 套真实 PPT 开源前门槛**：0/50。
6. **跨语言场景等价判定**：当前依赖模型对同一画面输出同一 `canonical_scene_id`；规范化只能吸收大小写/分隔符/复数差异，**不能**在没有模型的情况下把中文“推车选购”和英文“browsing produce”判为同一场景。这是已知能力边界，需由真实模型或向量化等价判定补齐。
7. **混合语言页是否旁路中文旧词库**：见 3.1 的设计取舍，待评审确认。

## 8. 复现命令

```bash
PY=/Users/yitenhua/.workbuddy-ai/binaries/python/envs/wengao/bin/python
PROJ="/Volumes/huangyingteng/WorkBuddy/2026-10-12-00-54-41/wengao-ppt/build/wengao-visual-v13-material-fix"

# 离线全量
cd "$PROJ" && $PY -m pytest tests -q -p no:cacheprovider

# 仅本轮新增验收
cd "$PROJ" && $PY -m pytest tests/test_multilingual_scene_acceptance.py -v -p no:cacheprovider

# 真实流程（需本机模型网关与 LibreOffice，本轮不可用）
cd "$PROJ"
$PY -m wengao_ppt.cli --project-root /tmp/wengao-real-20261023/data \
    precheck --input /tmp/wengao-real-20261023/retail_clinic_en.pptx
$PY -m wengao_ppt.cli --project-root /tmp/wengao-real-20261023/data \
    run --job-id <job_id> --timeout 240
```

## 9. 声明

本轮未发布、未宣称项目完成，也未把模型列表、API 连通性或离线 fixture 计为真实生成通过。开源前仍要求至少 50 套真实 PPT 生成验收通过。
