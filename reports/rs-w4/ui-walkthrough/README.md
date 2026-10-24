# 工作台界面走查记录（RS-9 验收）

日期：2026-10-24　执行：W4　工具：`scripts/ui_walkthrough.py`（Playwright + 系统 Chrome，headless）
服务：`workbench/server.py --port 8823`（127.0.0.1 回环，NO_PROXY 已按任务书 §2.5 设置）

## 走查结果

| 状态 | 截图 | 验证点 | 结果 |
|---|---|---|---|
| 01 空状态 | 01-empty.png | 引导文案、示例 chips、本机运行面板（6 项真实探测） | ✅ |
| 03 完成态 | 03-done-timeline.png | 8 步时间线（读取/检索/对比/趋势/配图/版式/转场/核对）、每步真实摘要 | ✅ |
| 04 待确认 | 04-conflict-open.png | 冲突卡：两个数并列（你的材料/公开来源）、采用公开来源/保留我的数据 | ✅ |
| 04b 裁决后 | 04b-conflict-resolved.png | 记录选择 → 局部重排 → 卡片转为已处理 | ✅ |
| 05 部分降级 | 05-degraded.png | "有 N 页未配图"说明（来自 images counts，非硬编码） | ✅ |
| 07 来源标签 | 07-sources.png | 已核对来源（引文逐字）+ 已丢弃（原因） | ✅ |
| 08 配图标签 | 08-images.png | 实拍/AI 生成/未配图三类图块 + 许可信息 | ✅ |
| 09 本页检查 | 09-page-check.png | 渲染/配图账本/转场/版式/研究证据逐项状态 | ✅ |
| 09b 缩略条 | 09b-thumb-select.png | 切换页面后大图与检查联动 | ✅ |
| 10 窄屏 | 10-narrow.png | 720px 三栏堆叠，功能不丢 | ✅ |
| 06 操作反馈 | 06-toast-feedback.png | toast（role=status, aria-live=polite） | ✅ |

**JS 错误：0**（走查脚本对 pageerror 设为零容忍）。
机器可读结果：`walkthrough.json`。

## 演示数据说明（如实声明）

走查使用 `scripts/make_demo_job.py` 生成的演示任务（job-demo-walkthrough）：
- 研究用**离线替身**（含 1 个待确认冲突、1 个对比视图、1 条已丢弃）；
- 规划器注入 3 个图片槽以演示 T4 降级与配图标签；
- 事件流/计划/账本/检查全部由真实管线代码落盘。
**示例数据仅用于界面状态验证，不计作真实验收**（任务书：回放与真实分开记账）。

## 复现方式

```bash
# Mac mini 本机（或任意能跑 LibreOffice 的机器）：
bash scripts/init_repo.sh                       # 若尚未建基线
python scripts/make_demo_job.py                 # 生成演示任务（约 2.5 分钟：检查看门狗占 120s）
PYTHONPATH=. python workbench/server.py --port 8823 &
python scripts/ui_walkthrough.py                # 需要 playwright + 系统 Chrome
```

注：本交付机（MacBook Pro）无 LibreOffice，故"逐页渲染"一项在演示任务中如实显示
NOT_EVALUABLE——界面不伪装成功。Mac mini 本机重跑时该项将为真实结果。
