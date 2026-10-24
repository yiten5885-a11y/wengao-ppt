# RS-7 真实验证节奏 · 运行手册（W4）

日期：2026-10-24　状态：⏳ 待 RS-2/3/4 完成后执行（本机已备好全部脚本与门禁）

任务书 RS-7：1 套 → 5 套 → 20 套，每档 LibreOffice 逐页渲染并人工看图，通过才进下一档。
**回放与真实分别记账；离线 fixture 不计作真实验收。**

## 第 1 档（1 套）：RS-1 同一份稿件，RS-2 完成后重跑

```bash
# Mac mini 本机：
python scripts/rs1_baseline.py --input <13页原稿.pptx>   # 改动前对照（已交付）
# RS-2（W1 分级配图）合入后，同一稿件重跑：
python scripts/rs1_baseline.py --input <同一份.pptx>
```

通过标准（任务书 RS-2 验收）：
- 必须出稿（`metrics.deck_delivered=true`）；
- 报告列出每个场景的级别与理由；
- 人工逐页看图（`reports/rs-w4/rs1-baseline/pages/`），无明显不相关图；
- 出稿率与场景覆盖率**分开记账**，不合并成一个 PASS。

## 第 2 档（5 套）：不同文字、不同结构

- 5 份不同材料：中文新闻稿、英文公司简介、中英混合、仅一句话主题（走完整 RS-3）、
  一份导入 PPT（beautify 路径联跑）。
- 每套：`GeneratePipeline`（generate）/ `BeautifyPipeline`（beautify）跑通 →
  LibreOffice 逐页渲染 → 人工看图 → `reports/rs-w4/rs7-tier2/<job_id>/` 留证。
- 联跑要求（任务书 §4）：**同一份稿件在 generate 与 beautify 两条路径都能出稿**；
  事件流 8 步齐全；降级与阻断按 C8。

## 第 3 档（20 套）：覆盖 RS-6 的 6 种文字

- 中文、英文、日文、阿拉伯文（RTL）、印地文、俄文各至少 2 套，其余补齐到 20。
- **RS-6 用户确认前**：26 语言矩阵与对外语言声明保持不变；本档只做 6 种文字真实链路。
- 未验证语言在对外说明里如实标注"未经真实链路验证"。

## 每套必须留存的证据（门禁从磁盘重算，不信自报）

```
jobs/<job_id>/evidence/
  pipeline/   events.jsonl  run.json  options.json  checks.json
  research/   bundle.json  queries.json  report.md  snapshots/*.txt
  images/     ledger.json  assets/*
  layout/     plan.json  layout_report.json  pages/*.png  render.json
  transition/ transition_report.json
```

## 通过标准汇总

1. 出稿率与场景覆盖率分开统计；
2. 降级项如实记录（T4 场景、omissions、discarded）；
3. 阻断项（错图/无许可/重复/编造数字/溢出/缺转场）必须真的阻断；
4. 每档人工看图记录（页码 + 结论）落盘；
5. 任一档不通过：停下修，不缩小范围抬完成率。
