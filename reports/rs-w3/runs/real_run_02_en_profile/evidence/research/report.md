# 稳稿 PPT：联网研究与数据事实报告 (Research Report)

- **Bundle ID**: `rb-1cca6086a7a0`
- **创建时间**: `2026-10-23T21:19:22.420932+00:00`
- **材料语言**: `en`
- **研究功能状态**: `已启用`

---

## 1. 实际发出的外发检索词清单 (Queries)

> [!NOTE] 隐私保护声明：根据公共契约，所有外发查询仅包含中性实体词与行业/财务指标词，严禁外发用户原文段落。

| 序号 | 检索词 | 语言 | 检索后端 | 发送时间 |
|---|---|---|---|---|
| 1 | `Tesla Inc 行业均值 市场规模` | zh | searxng | 2026-10-23T21:19:22.421256+00:00 |
| 2 | `Tesla Inc market share industry benchmark` | en | searxng | 2026-10-23T21:19:22.421256+00:00 |

## 2. 采纳的事实清单 (Accepted Facts)

| Fact ID | 来源类型 | 主体 | 指标 | 数值与单位 | 周期 | 来源机构 | 等级 | 快照 SHA-256 |
|---|---|---|---|---|---|---|---|---|
| `f001` | `WEB_RESEARCH` | currentCol | currentCol | **6.218 ** | 2024 | 纯电动车、太阳能和清洁能源 | 特斯拉中国 | C | `da1f92352cf2` |

## 3. 对比与洞察视图 (Views)

### 视图 `v01`: currentCol currentCol 关键洞察 (insight)
- **指标**: currentCol () | **周期**: 2024
- **亮点发现 (max_gap)**: currentCol 核心指标 currentCol 达到 6.218
  - 规则名: `INSIGHT_KEY_METRIC_V1`
  - 计算细节: `{"value": 6.218, "unit": ""}`
- **页脚出处**: 数据来源: 纯电动车、太阳能和清洁能源 | 特斯拉中国 (2024)

## 4. 丢弃的未达标候选 (Discarded)

| 错误码 | 详细原因 | 来源 URL |
|---|---|---|
| `RESEARCH.FETCH_FAILED` | 无法抓取页面或有效正文过短: Electric Cars, Solar & Clean Energy | Tesla | https://www.tesla.com/ |
| `RESEARCH.FETCH_FAILED` | 无法抓取页面或有效正文过短: Find Us | Tesla | https://www.tesla.com/findus/location/store/desmoines |

## 5. 降级省略项 (Omissions)

| 拟建视图/对比需求 | 错误码 / 理由 |
|---|---|
| 横向对比视图：行业竞品/均值基准 | `RESEARCH.NO_RELIABLE_SOURCE` |
| 纵向趋势视图：多期历史数据 | `RESEARCH.NO_RELIABLE_SOURCE` |
