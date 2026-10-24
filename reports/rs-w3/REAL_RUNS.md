# W3 联网研究真实检索验收报告 (Real Runs)

- **验收时间**: `2026-10-24T05:20:36+0800`
- **验收标准**: 真实发起网络检索、抓取页面快照、抽取事实、逐字核验与磁盘独立重算（不使用预设 fixture）
- **回放与真实分账**: 本报告所有用例均为本机真实网络/大模型端到端调用，不计入离线 fixture。

## 用例汇总

| 用例编号 | 用例名称 | 耗时(s) | 独立校验 | 实际外发查询 | 核实事实数 | 产出视图数 | 降级省略数 |
|---|---|---|---|---|---|---|---|
| `real_run_01_zh_press` | 中文新闻稿：新能源汽车行业发展 | 38.21 | **PASS** | 2 | 11 | 2 | 1 |
| `real_run_02_en_profile` | 英文公司简介：Tesla EV and Clean Energy | 33.67 | **PASS** | 2 | 1 | 1 | 2 |
| `real_run_03_one_sentence` | 仅一句话主题：中国商业航天市场 | 40.39 | **PASS** | 2 | 10 | 2 | 1 |

## 逐用例详情与证据追溯

### 中文新闻稿：新能源汽车行业发展 (`real_run_01_zh_press`)
- **证据目录**: `reports/rs-w3/runs/real_run_01_zh_press/evidence/research/bundle.json`
- **报告路径**: `reports/rs-w3/runs/real_run_01_zh_press/evidence/research/report.md`
- **生成视图**:
  - v01 (trend): HTTP HTTP 历史趋势
  - v02 (insight): HTTP HTTP 关键洞察
- **降级省略说明 (Omissions)**:
  - 横向对比视图：行业竞品/均值基准 [RESEARCH.NO_RELIABLE_SOURCE]

### 英文公司简介：Tesla EV and Clean Energy (`real_run_02_en_profile`)
- **证据目录**: `reports/rs-w3/runs/real_run_02_en_profile/evidence/research/bundle.json`
- **报告路径**: `reports/rs-w3/runs/real_run_02_en_profile/evidence/research/report.md`
- **生成视图**:
  - v01 (insight): currentCol currentCol 关键洞察
- **降级省略说明 (Omissions)**:
  - 横向对比视图：行业竞品/均值基准 [RESEARCH.NO_RELIABLE_SOURCE]
  - 纵向趋势视图：多期历史数据 [RESEARCH.NO_RELIABLE_SOURCE]

### 仅一句话主题：中国商业航天市场 (`real_run_03_one_sentence`)
- **证据目录**: `reports/rs-w3/runs/real_run_03_one_sentence/evidence/research/bundle.json`
- **报告路径**: `reports/rs-w3/runs/real_run_03_one_sentence/evidence/research/report.md`
- **生成视图**:
  - v01 (trend): 习近平向202 习近平向202 历史趋势
  - v02 (insight): 习近平向202 习近平向202 关键洞察
- **降级省略说明 (Omissions)**:
  - 横向对比视图：行业竞品/均值基准 [RESEARCH.NO_RELIABLE_SOURCE]

## 验收结论

1. **端到端跑通**：三类典型输入均完整走通【规划 -> 检索 -> 抓取 -> 抽取 -> 逐字核验 -> 对比视图组装 -> 证据落盘】全流水线。
2. **宁缺勿编落地**：当检索结果未能满足 A/B 级权威标准或口径不齐时，系统如实生成 `omissions`，未发生任何大模型补写或编造数字。
3. **磁盘独立重算闭环**：`verify_bundle` 在只读磁盘重算快照哈希与连续逐字匹配后，所有合法外部事实均 100% 验证通过。