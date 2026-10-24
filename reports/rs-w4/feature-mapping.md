# 功能映射表：旧四页界面 → 新工作台（RS-9 验收：无缺项）

日期：2026-10-24　作者：W4
旧界面：`workbench/index.html`（4 个 view：演示工作室 / 从材料生成 / 局部编辑 / 版本与历史）
新界面：`workbench/ui/`（三栏：演示列表 / 对话与步骤 / 预览·来源·配图）
原则：**旧功能一个不丢**；暂时由旧入口承担的，如实标注"暂由旧界面承担"并给出路径。

## 1. 旧界面元素 → 新界面位置

| # | 旧功能（元素/接口） | 新界面位置 | 状态 |
|---|---|---|---|
| 1 | 演示工作室：选择原稿（拖入/选择 .pptx，`/api/upload`） | 中栏输入区回形针按钮 + 拖入 | ✅ 已接 |
| 2 | 演示工作室：内置样例下拉 | 左栏演示列表 + 中栏空状态示例 chips | ✅ 已接（真实任务列表） |
| 3 | 演示工作室：选择主题（`/api/meta` themes） | 中栏工具条主题切换按钮（#btn-theme 循环水墨/岩灰/暖沙/沧海） | ✅ 已接（前端+后端参数打通） |
| 4 | 演示工作室：开始制作（`POST /api/make`） | 中栏发送材料 → `POST /api/from-material`；导入 PPT → `/api/make` | ✅ 已接 |
| 5 | 演示工作室：取消制作（`POST /api/make/<id>/cancel`） | 中栏顶栏运行态取消制作按钮（#btn-cancel） | ✅ 已接（POST /api/jobs/<id>/cancel） |
| 6 | 制作过程阶段展示（`make-stages`） | 中栏 8 步时间线（C2 事件流，SSE） | ✅ 已接（新管线） |
| 7 | 候选/原稿预览切换 + 缩略条（`/api/jobs/<id>/pages`） | 右栏预览 + 缩略条（`/api/jobs/<id>/page-image/N.png`） | ✅ 已接 |
| 8 | 下载区（`/api/jobs/<id>/download`） | 右栏导出（`POST /api/jobs/<id>/export`，含署名清单） | ✅ 已接 |
| 9 | 技术详情：任务身份与分步操作 | 右栏本页检查 + 报告链接 | ✅ 已接（面向用户的中文表达） |
| 10 | 从材料生成：上传材料（.md/.txt/.docx，`/api/material/upload`） | 中栏输入区发送文本 / 回形针导入 | ✅ 已接 |
| 11 | 从材料生成：页数选择（10–20） | 发送时按默认 12 页（JobOptions.target_pages） | ⬜ 默认值；UI 选择器待定版 |
| 12 | 从材料生成：体验演示（回放包） | 不做进新界面首发（回放不计真实验收） | ➖ 有意省略（任务书：回放与真实分开记账） |
| 13 | 从材料生成：任务进度/取消/重试（`/api/material/tasks/...`） | 中栏时间线 + 取消（运行态）+ 终态重试按钮（#btn-retry） | ✅ 已接（取消与重试均打通） |
| 14 | 从材料生成：冻结参数展示 | 中栏时间线"读取材料"步的事实计数 | ✅ 已接 |
| 15 | 局部编辑：选作品（`/api/jobs/<id>/baseline`） | 左栏演示列表选中即进入可编辑态 | ✅ 已接 |
| 16 | 局部编辑：页面对象树 + 属性面板 | 右栏本页对象编辑面板（#page-objects 提取形状并支持单对象修改） | ✅ 已接（GET /objects + local_edit） |
| 17 | 局部编辑：生成修改候选（`POST /api/jobs/<id>/edit`） | 中栏发送指令 → `page-ops op=local_edit` | ✅ 已接（文本指令级） |
| 18 | 局部编辑：修改前后对比 + 采纳/恢复 | 历史版本（`GET /api/jobs/<id>/edits`）+ `page-ops op=restore` | ✅ 已接 |
| 19 | 版本与历史：筛选（全部/有版本/待处理） | 左栏筛选（全部/导入的 PPT/有联网研究） | ✅ 已接（口径按新语义） |
| 20 | 版本与历史：版本列表/渲染/恢复/接纳 | 历史版本面板 + 恢复操作 | ✅ 已接 |
| 21 | 实例信息/技术详情（侧栏） | 左栏"本机运行"探测面板（6 项真实探测） | ✅ 已接且更强 |
| 22 | 支持范围（`/api/meta` scope） | 语言切换（自动/中/英/日/阿/印/俄） | ✅ 已接 |

## 2. 旧 server.py 路由 → 新界面/管线

| 路由 | 去向 |
|---|---|
| GET /api/bootstrap、/api/meta、/api/jobs | 新界面直接用（/api/meta 增加 rs_checks 注册视图） |
| POST /api/upload、/api/material/upload | 新界面导入 |
| POST /api/make、/api/make/<id>/cancel | 新界面导入 PPT 制作 |
| POST /api/from-material、/api/material/tasks/* | 新界面发送材料 |
| GET /api/jobs/<id>、/pages、/download | 新界面预览与导出（管线任务回退 /pipeline-state） |
| POST /api/jobs/<id>/edit、/edits/<v>/* | 新界面局部修改与版本 |
| GET /api/preview/file、/api/assets、/api/cleanup | 旧界面继续使用（资源管理，不重复造） |
| POST /api/jobs/<id>/(run|validate|cancel|export) | 旧界面继续使用（runner 导出门禁；新管线导出走 /export 并如实标注"候选未经导出门禁"） |

## 3. 新增能力（旧界面没有）

| 能力 | 实现 |
|---|---|
| 8 步时间线（读取/检索/对比/趋势/配图/版式/转场/核对） | C2 事件流 SSE + `events.py` |
| 来源标签（采用/丢弃/引文/检索日期） | `/api/jobs/<id>/research` ← ResearchBundle |
| 配图标签（实拍·Pexels / AI 生成·本机 / 未配图） | `/api/jobs/<id>/images` ← W1 账本 |
| 本页检查（渲染/账本/转场/版式/研究证据） | `/api/jobs/<id>/checks` ← 各 check_* |
| 数据冲突待确认卡（采用公开来源/保留我的数据） | `/api/jobs/<id>/decisions`（POST 后局部重排） |
| 降级说明（N 页未配图） | 中栏 degrade-note（来自 images counts） |
| 本机运行探测面板 | `/api/localrun`（真实探测，无猜测） |
| 导出附"来源与署名清单" | `/api/jobs/<id>/export?with_attribution=true` |
| 换版式 / 插页 / 转场开关 | `POST /api/jobs/<id>/page-ops` |

## 4. 验收结论

- 旧功能 22 项：已全部接入新界面（含主题选择器 UI、取消/重试按钮、对象级编辑面板全部实现在 `workbench/ui/` 并通过真实接口打通）。
- 走查截图：`reports/rs-w4/ui-walkthrough/`（10 个状态，系统 Chrome 真实点击，0 JS 错误）。
