# 稳稿 PPT — 场景配图链路修复与独立验收（续轮）

- 日期：2026-10-23
- 候选目录：`/Volumes/huangyingteng/WorkBuddy/2026-10-12-00-54-41/wengao-ppt/build/wengao-visual-v13-material-fix`
  （请求路径 `/Users/huangyingteng/...` 在本机以 SMB 卷挂载）
- 语言范围：现有验收矩阵 26 种，未扩展
- 状态：**未发布，release_ready 保持 false**

## 1. 当前运行任务 job-167a1306f549 的最终真实结果

该任务在我接手时已自然结束（`run_finished_at 2026-10-23T15:46:09+08:00`），不是运行中；未取消、未重启、未让任何代码改动进入这次运行。

| 字段 | 值 |
| --- | --- |
| verdict | **FAILED** |
| stage | `semantic_scenes` |
| fail_reason | **`VISUAL.SCENE_UNRESOLVED`** |
| failure_detail | 场景照片或安全版位未完整解决；阻止文字卡片替代场景图。 |
| model_error | **空**（不是模型失败） |
| 输入 | 13 页 PPTX，`sha256 be8614d943d875c808e2ac08773da5e9cec783512aef7b6a525a75118954065f`，mode=beautify |
| precheck | `upstream_verified=VENDOR_DIR_MISSING`，`real_renderer=available`，`P1_TEXT_LIMITED` |
| 语言 | `detected_source_languages = ["bn","zh"]` |
| 场景 | `planned_scene_count=17`，`planned_photo_count=0` |
| 覆盖审计 | `PASSED`（13 页，missing=0，merged=0） |
| 未解决 | 17/17 全部 `SCENE.NO_LICENSED_PHOTO` |

其中一条未解决记录直接给出了服务侧事实：

```
scene_key: clinic_patient_checkin
reason:    SCENE.NO_LICENSED_PHOTO
detail:    没有找到许可合规、清晰且相关的场景照片；
           最近的服务反馈：图片检索服务的 HTTPS 握手等待超时。
```

结论：本次失败发生在**图片检索层**，模型推理与场景规划都已完成（17 个场景、13 页覆盖审计通过）。原先把 17/17 一律记为“无授权照片”是不准确的。

## 2. 根因诊断（可复核）

要求 B9：先做中性 HTTPS 控制请求，再定位图库请求。

| 探测 | 结果 |
| --- | --- |
| `https://example.com/` | 200，TLS 0.56 s → 网络与 TLS 正常 |
| `https://api.openverse.org/v1/images/?q=cat` | 200，1425 B，`cf-cache-status: HIT` → 图库可达 |
| 同一查询未命中缓存 | 10–25 s，个别 35 s 超时 |
| `https://commons.wikimedia.org/w/api.php` | 200，0.4–1.1 s → 维基 API 很快 |
| 所有域名解析结果 | **全部为 `172.19.0.x`**（代理 fake-IP），`example.com` 亦然 |

由此定位到 5 个独立缺陷（均非模型问题）：

1. **URL 安全门依赖 DNS 解析**：`_url_is_public_https` 要求解析结果为公网 IP。fake-IP 环境下所有主机名都解析到私有地址，于是**每一个候选图都被判为不安全并静默丢弃**，最终表现为“没有授权照片”。
2. **静默丢弃**：URL 不合格与许可不合格两处都是 `continue`，不写失败记录；失败列表为空时统一报 `NO_LICENSED_PHOTO`。
3. **错误归因混淆**：传输失败（TLS 超时/不可达）与“有候选但不合格”共用同一个错误码。
4. **单次检索超时 4 秒**：实测未命中缓存需 5–35 秒，4 秒超时使**未缓存查询永远不可能成功**。
5. **长短语检索命中率为 0**：实测 `shoppers browsing produce supermarket` → 0 条；`supermarket shopping` → 6 条；`cashier scanning items checkout` → 0 条；`checkout cashier` → 2 条。图库是关键词匹配，不是句子匹配。

另有 1 个真实假通过：

6. **插画被当作场景照片接受**：真实下载到的 `Doctor Patient (NIH BioArt 130)` 是线描插画，因元数据无负面词而通过风格门。

## 3. 本轮改动

| 文件 | 变更 |
| --- | --- |
| `wengao_ppt/scene_images.py` | 6 处修复（见下） |
| `tests/test_scene_provider_transport.py`（新增） | 13 项回归测试 |
| `tests/test_semantic_scenes.py` | 夹具 `png_bytes()` 加入确定性颗粒；1 处错误码断言改为更精确的码 |
| `tests/test_scene_provider_network_failures.py` | 夹具改为带颗粒的图片；新增 `import random` |

未改动：`scene_model.py`、`semantic_scenes.py`、`contracts/semantic_scene_gate_policy.json`（保留 Codex 2.20.0 规则与最近修改）。

### 3.1 `scene_images.py` 修复明细

1. `_url_is_public_https()` 改为**按主机形状判定**：仍拒绝非 HTTPS、含凭据、`localhost`/`.local`/`.internal`/`.lan`/`.intranet`、裸单标签主机、以及**字面**私有/回环/链路本地/组播/保留 IP；不再要求 DNS 解析为公网。新增 `_host_resolution_report()` 仅作诊断输出。
2. URL 不合格改为记录 `SCENE.PHOTO_URL_NOT_PUBLIC_HTTPS`。
3. 许可不合格改为记录 `SCENE.LICENSE_NOT_ALLOWED`（维基以 CC BY-SA 为主，这一条能解释“有候选但不可用”）。
4. 终态错误分类：存在传输类失败 → 抛出该传输码（`PROVIDER_TLS_HANDSHAKE_TIMEOUT` / `PROVIDER_UNAVAILABLE` / `SEARCH_BUDGET_EXHAUSTED` …）且详情取自该传输行；全部为资格类失败 → `SCENE.PROVIDER_ROWS_INELIGIBLE` 并列出逐项计数；无任何候选 → `SCENE.NO_LICENSED_PHOTO`。
5. 超时按服务区分（实测依据）：Openverse 20 s/45 s，Wikimedia 4 s/12 s，Pexels 15 s/30 s。
6. 新增 `keyword_search_variants()`：由场景锚点生成 2–3 词短查询（只使用锚点中已有的词），**前置**于既有同义词变体，仅扩大召回；元数据门仍用权威三锚点全量查询校验，不放松准入。
7. 新增 `photographic_asset_check()`：按 5-bit 色数与前八色占比识别平涂插画，命中则记 `SCENE.PHOTO_NOT_PHOTOGRAPHIC`。

### 3.2 阈值标定（真实样本）

| 样本 | 5bit 色数 | 前八色占比 | 判定 |
| --- | --- | --- | --- |
| `Doctor Patient (NIH BioArt 130)` 线描插画 | 440 | 0.795 | 拒绝 |
| Commons 实拍 `Shopping Cart Supermarket.jpg` | 2682 | 0.202 | 通过 |
| 阈值 | < 1200 且 > 0.55 | | 判为平涂 |

## 4. 真实图库证据（非 fixture）

真实下载已打通，但**不能算场景图验收通过**：

| 场景 | 结果 |
| --- | --- |
| `doctor consulting patient clinic`（首次） | 下载成功：Wikimedia Commons，`Public domain`，`sha256 4e4405ab02f0…`，1920×1164，4.19 s —— 目视为**线描插画**，加入拦截后该类素材被拒 |
| 同上（加入拦截后复跑） | 接受 `Doctor Spurzheim in his consulting room…`，CC BY 4.0，`sha256 06e260e55c87…`，1704×1089，9.8 s —— 历史版画扫描，元数据命中三锚点，但对现代商务稿件语义偏弱 |
| `shoppers browsing produce supermarket` | `PROVIDER_UNAVAILABLE`（53 s 预算耗尽） |
| `worker assembling products factory` | `PROVIDER_UNAVAILABLE`（53 s） |
| `cashier scanning items checkout` | `PROVIDER_ROWS_INELIGIBLE`（34 条候选，全部 `IMAGE_CONTEXT_UNVERIFIED`） |

可复核证据文件：`reports/multilingual-scene-20261023/evidence/real_photo_download.json`。

结论：**机制可用，但可用素材供给不足**。免费图库对“三锚点同时命中”的严格元数据门支持很差；Openverse 命中缓存才快，未命中即 5–35 秒。这决定了 50 套真实出图在当前供给下不可能稳定达标。

## 5. 测试

```bash
PY=/Users/yitenhua/.workbuddy-ai/binaries/python/envs/wengao/bin/python
cd /tmp/wengao-local          # 与挂载卷逐字节一致的本地镜像
$PY -m pytest tests -q -p no:cacheprovider
```

结果见第 6 节。新增用例覆盖：URL 门 15 例矩阵、fake-IP 不丢候选、短查询只使用锚点词、传输失败不误报、许可拒绝被记录、平涂插画被拒、真实照片被接受、超时与实测延迟匹配。

## 6. 测试结果

| 范围 | 命令 | 结果 |
| --- | --- | --- |
| 全量 | `pytest tests -q` | **208 passed, 260 subtests passed**（4m44s） |
| 场景 provider 相关三组 | `pytest tests/test_semantic_scenes.py tests/test_scene_provider_network_failures.py tests/test_scene_provider_transport.py -q` | **101 passed, 101 subtests**（3m39s） |
| 新增回归 | `pytest tests/test_scene_provider_transport.py -q` | **13 passed, 25 subtests** |

基线对比：本轮开始前为 195 passed / 238 subtests；本轮 +13 项测试、+22 子测试，**零回归**。

改动文件 SHA-256：

| 文件 | SHA-256 |
| --- | --- |
| `wengao_ppt/scene_images.py` | `072bfbfca718b72cee652f6e805ad588784564a9f1954fdab37441b3a0e8483b` |
| `tests/test_scene_provider_transport.py` | `c539c0634efd7c65a63c9580a5ef15575ea4d8e8ca637e62026440a4532bcb7e` |
| `tests/test_semantic_scenes.py` | `e2a7aa1d740dc7beeacae92a707d9d0f3085e69f021a3d440e6fcc395202642e` |
| `tests/test_scene_provider_network_failures.py` | `7a3f8d23ae637da93cce15e50e11717ccd4fef1e3a7064565b9d242810a6f6c3` |

构建身份：本地镜像计算为 `698e06c57cef917e52d76bc5d3405ecc`，与改动前的 `73cd8292102ede04b15c4fa9264da66b` 不同（源码已变）。**权威值需在候选机原路径上重算**（SMB 卷上计算超时未完成）。按 F1，任何 50 套批次都必须在该新身份冻结后重新开始。

## 7. 仍未通过 / 未实施

1. **26 种语言 × 真实工作台上传→推理→出图→渲染→导出**：0/26。本机无法访问 8823（SMB 挂载的另一台机器上的回环服务），也无本机模型。
2. **工作台 8823 端到端**：不可达（此前 `curl 127.0.0.1:8823` 经环境代理返回 502）。
3. **50 套真实生成**：0/50。
4. **排版硬门禁 12 项与 P2/P5/P10 缺陷**：未开始（本轮聚焦场景配图链路）。
5. **原生播放门禁、alpha/透明对象、安装与许可、文档与消毒**：未开始。
6. **构建身份**：本轮改动使 build_id 变化，`73cd8292102ede04b15c4fa9264da66b` 已不再对应当前源码；50 套批次必须在**新的冻结身份**下重新开始（本轮未冻结、未起批）。
7. **插画/版画识别**：色统计只能识别平涂矢量图；灰度版画扫描会通过。需要本机视觉复核（当前不可用）才能判定“是否为现代纪实照片”。
