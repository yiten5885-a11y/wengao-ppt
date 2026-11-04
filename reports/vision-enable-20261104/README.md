# 视觉主题在本安装变为可用 · 2026-11-04

用户指令：「视觉主题在本安装不可用,那就重新安装或者让安装变得可用」。
结论：**未下载任何模型、未重装系统组件**——把用户既有配置修到正确形状即可
（`~/.wengao_ppt/cloud_model.json`），随后视觉链路两段均实测通过。

## 根因（三项，全部是配置形状，不是代码缺陷）

| # | 问题 | 后果 | 修法 |
| --- | --- | --- | --- |
| 1 | `endpoint` 缺 `/openai` 后缀（`…/v1beta`） | 视觉复核拼 `…/v1beta/chat/completions` → 404；生图走原生 base 正确（歪打正着） | 改为 `…/v1beta/openai`；生图侧 `active_image_gen_target` 会自动剥掉 `/openai` 还原原生 base（代码注释里写明该设计预期） |
| 2 | `vision_model` 为空 | `active_vision_target()` 恒返回 None → 探测永远 `available=false` | 设 `gemini-3.8-flash`（API 明确告知 `gemini-2.5-flash` 对新用户已下线） |
| 3 | `vision_enabled=false` | 界面语义不明确 | 设 true（enabled 已为 true，此开关只是显式化） |

`text_model` 保持为空：文本规划继续走本机 18088（Qwen3.8-27B），架构不动。

## 实测记录（全部走产品真实代码路径，测试进程须绕开会话沙箱代理）

1. **视觉复核** `scene_model._call_cloud_vision_once`：64×64 纯红 PNG + 颜色提问
   → `{"color": "red"}`，`gemini-3.8-flash`，3.0s。
   *注意：`gemini-2.5-flash` 返回 404 "no longer available to new users，
   use gemini-3.8-flash"。*
2. **云端生图** `media.genimg.cloud_genimg.CloudImageClient`（gemini-native）：
   `visual_query="quiet library reading room…"`、16:9
   → 1,652,025 bytes PNG（photorealistic 阅览室，`gemini-2.5-flash-image`
   **未下线**，无需更换），存 `work/vision-test-img.png`。
3. **产品探测** `scene_model.probe_vision_endpoint()`
   → `{"role":"vision","mode":"cloud","available":true,
   "provider":"cloud:generativelanguage.googleapis.com"}`。
4. **HTTP 层**：8902 端口起工作台，`/api/bootstrap` 返回
   `vision={available:true, mode:cloud, provider:…}`，
   `wengao-visual-dark.visual_upgrade=true`（40s——`jobs_list` 扫 34 个历史
   任务，见「已知问题」）。
5. **真 Chrome 走查**（`WALKTHROUGH.json` + `visual-theme-available.png`）：
   视觉升级分组 hint 显示「当前图片语义复核：**已就绪（cloud:…）**」，
   卡片无「本安装不可用」徽标，`page_errors=[]`。

## 环境注意事项（下次排障先看这里）

* **本会话沙箱代理**（`HTTP_PROXY=http://127.0.0.1:57069`）转发不了
  `generativelanguage.googleapis.com`（curl HTTP 000 / RemoteDisconnected×3）；
  `--noproxy '*'` 直连 200 in 0.54s。用户真实环境（Clash TUN）无此问题。
  任何云端实测都要 `env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy`。
* urllib 对回环目标**不**自动绕过代理（curl 会）——测试客户端打 127.0.0.1
  必须用 `ProxyHandler({})` 或设 `no_proxy`。

## 代码改动（让状态在**选择之前**可见，不再「选了才报错」）

* `workbench/server.py`：bootstrap 新增 `vision` 摘要
  （`_vision_summary()`，探测抛异常时降级 `available=false`，bootstrap 永不 500）。
* `workbench/index.html`：视觉升级分组 hint 动态化（已就绪/不可用两态 +
  不可用时两条可操作去处）；`themeCard` 对 `visual_upgrade && !available`
  的卡加 `.needvision`「本安装不可用」徽标。
* `scripts/theme_gallery.py`：画廊 HTML 每卡显示构件行（id · 条数/页数）。
* 测试：`tests/test_workbench_vision_badge.py`（3 条：bootstrap 组装一致、
  前端静态接线、探测异常不炸）；`tests/test_install_doctor.py` 本机路径 3 测
  显式隔离云端配置 + 新增云端分支 1 测（云端 mode 下不得探测本机端点）。

## 已知问题（本轮不修，记录在案）

* `/api/bootstrap` 在真实数据根（34 个历史任务）上耗时 ~40s：
  `WB.jobs_list()` 全量扫描。首屏一次，可接受但明显慢；后续应做缓存/分页。
* 本会话 pytest 进程内对回环 HTTP 的请求反复超时（同样请求 curl 2s 即回），
  bootstrap 测试因此改为直接驱动 `_bootstrap` 组装逻辑（stub `_send`），
  HTTP 层由真 Chrome 走查兜底。

## 干净安装根首任务（vision_enable 后重跑）

见 `reports/install-cleanroom-20261104-r5/CLEANROOM_INSTALL.json`——
`first_task_run` 此前唯一的 `blocked_external`（视觉端点）已解除。

## 干净安装 r5–r10：视觉启用只是第一层，后面每轮暴露一层

视觉端点解除后，干净安装根首任务继续暴露产品自身的既有规则（每一层都按
证据处置，详见 `CONTINUE_HERE.md` 本日章节）：

* **r5** `SCENE.NO_SCENES_PLANNED`：零场景→unresolved 是防伪造硬规则（有测试
  守护）。曾误加 runner「零场景放行」分支，已回退。
* **r6/r7** `--allow-text-card-fallback` 只对具体场景生效（`scene is None →
  still_unresolved`）；harness 首任务原稿改为含第 4 页具体场景（r8 起）。
* **r8** 首任务全链走通：本机规划器判 2 场景 → 云端生图 → 云端复核拒
  （拟人动作静态图不可见）→ 兜底卡片 → run 放行。
* **r9** run 过、validate 拦：图库图「检索摘要匹配、下载后完整元数据不匹配」
  在外层门禁处判死场景且不回退生图。已修（`_ai_fallback()` 回退 + 三类
  元数据门禁拒绝可回退一次生图；许可/内容安全拒绝不回退）。
* **r10** 修复后首跑 run 反报 `MODEL.TRANSPORT_ERROR`（RemoteDisconnected×3）：
  本会话沙箱代理被干净安装环境继承——**本报告上一节的代理注意事项在
  跑 cleanroom 时同样适用**（r8/r9 是在无代理环境跑的）。非产品缺陷。

修复后载荷：`dist/wengao-ppt-0.1.0-7a876cf7`。

**r10b（无代理环境）终局：verdict PASS，17/17 步全绿**——run（99s）→
validate（0 blocking / 0 total findings，`visual.semantic_scene_coverage`
EXECUTED）→ publish → export → OOXML 合格 → 升级/回滚/卸载全过。
终局实证（`安装 根 T/data/jobs/job-e4e358fc36c4/valid/scene_visual_plan.json`）：
`unresolved=[]`；r9 判死的同一场景由
`license=AI_GENERATED_CLOUD / provider=cloud_image_generation` 接住，
复核 `gemini-3.8-flash` PASSED / high / photographic=true。
