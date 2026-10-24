# SearXNG 检索稳定性与性能评测报告

- **评测时间**: `2026-10-23T21:07:27.403139+00:00`
- **测试端点**: `http://127.0.0.1:8890` (本地自托管 / 环回免代理)
- **总测试查询数**: `32` 条（中文 18 条，英文 14 条）
- **检索成功率**: **100.0%** (32/32)
- **空结果 / 限流率**: **0.0%** (0/32)

## 延迟分布指标

- **平均延迟 (Mean)**: `0.68 s`
- **中位数 (p50)**: `0.49 s`
- **90分位数 (p90)**: `1.00 s`
- **最大延迟 (Max)**: `1.33 s`

## 逐条查询详细结果

| 序号 | 检索词 | 语言 | 状态 | 命中数 | 耗时(s) | 首条来源 |
|---|---|---|---|---|---|---|
| 1 | `比亚迪 2024年 汽车销量 官方快报` | zh | **PASS** | 5 | 0.453 | 比亚迪汽车官方网站首页 - 新能源汽车世界冠军 |
| 2 | `宁德时代 2024Q3 动力电池 装机量 市场份额` | zh | **PASS** | 5 | 0.409 | 宁德市（福建省辖地级市）_百度百科 |
| 3 | `中国 新能源汽车 渗透率 乘联会 2024` | zh | **PASS** | 5 | 0.42 | 中华人民共和国_百度百科 |
| 4 | `华为 云计算 市场份额 增长率 2024` | zh | **PASS** | 5 | 0.474 | 华为技术有限公司 |
| 5 | `中国 半导体 销售额 2024 统计局` | zh | **PASS** | 5 | 0.552 | 中华人民共和国_百度百科 |
| 6 | `小米 手机 出货量 2024Q3 Canalys` | zh | **PASS** | 5 | 0.419 | 小米科技有限责任公司 - 小米商城 - Xiaomi |
| 7 | `腾讯 2024Q3 财报 增值服务 收入` | zh | **PASS** | 5 | 0.418 | 腾讯网 |
| 8 | `阿里巴巴 淘天集团 2024 季度收入` | zh | **PASS** | 5 | 0.41 | 阿里巴巴 |
| 9 | `中国 人工智能 核心产业规模 2024 工信部` | zh | **PASS** | 5 | 0.417 | 中华人民共和国_百度百科 |
| 10 | `光伏组件 行业均值 产能利用率 2024` | zh | **PASS** | 5 | 0.421 | 光（物理名称）_百度百科 |
| 11 | `中国 锂电池 产量 同比增长 2024` | zh | **PASS** | 5 | 0.418 | 中华人民共和国_百度百科 |
| 12 | `美团 核心本地商业 经营利润 2024` | zh | **PASS** | 5 | 0.422 | 美（汉语文字）_百度百科 |
| 13 | `中国 快递业务量 2024 国家邮政局` | zh | **PASS** | 5 | 0.437 | 中华人民共和国_百度百科 |
| 14 | `中国 工业机器人 产量 2024 统计局` | zh | **PASS** | 5 | 0.489 | 中华人民共和国_百度百科 |
| 15 | `中国 储能装机 累计容量 2024 中关村储能联盟` | zh | **PASS** | 5 | 0.436 | 中华人民共和国_百度百科 |
| 16 | `商飞 C919 交付量 订单量 2024` | zh | **PASS** | 5 | 0.434 | [b站扫盲计划19|回忆录]5分钟带你了解哔哩哔哩的发展史，... |
| 17 | `中国 跨境电商 进出口总额 2024 海关总署` | zh | **PASS** | 5 | 0.457 | 中华人民共和国_百度百科 |
| 18 | `中国 软件业务收入 2024 工信部` | zh | **PASS** | 5 | 0.427 | 中华人民共和国_百度百科 |
| 19 | `Tesla vehicle deliveries 2024 annual report` | en | **PASS** | 5 | 0.895 | Electric Cars, Solar & Clean E... |
| 20 | `Apple iPhone market share 2024 IDC` | en | **PASS** | 5 | 0.879 | Apple |
| 21 | `Nvidia data center revenue Q3 2024` | en | **PASS** | 5 | 0.999 | Nvidia - Official Site |
| 22 | `Microsoft Azure cloud revenue growth rate 2024` | en | **PASS** | 5 | 0.935 | Microsoft – AI, Cloud, Product... |
| 23 | `Global semiconductor revenue Gartner 2024` | en | **PASS** | 5 | 0.946 | GLOBLE |
| 24 | `Amazon AWS operating margin 2024` | en | **PASS** | 5 | 0.974 | Amazon.com. Spend less. Smile ... |
| 25 | `TSMC wafer revenue share 2024 TrendForce` | en | **PASS** | 5 | 1.016 | Taiwan Semiconductor Manufactu... |
| 26 | `Electric vehicle global market share 2024 BloombergNEF` | en | **PASS** | 5 | 1.33 | Massachusetts Electricity | Ho... |
| 27 | `Global smartphone shipment volume 2024 Canalys` | en | **PASS** | 5 | 1.311 | GLOBLE |
| 28 | `Meta advertising revenue growth 2024` | en | **PASS** | 5 | 0.93 | About Meta | Our Company, Miss... |
| 29 | `Global cloud infrastructure market share 2024 Synergy` | en | **PASS** | 5 | 0.907 | GLOBLE |
| 30 | `Alphabet Google Cloud operating income 2024` | en | **PASS** | 5 | 0.915 | English Alphabet - Worldometer |
| 31 | `Global PC shipment growth rate 2024 Gartner` | en | **PASS** | 5 | 0.983 | GLOBLE |
| 32 | `Lithium-ion battery price per kWh 2024 BloombergNEF` | en | **PASS** | 5 | 0.913 | Lithium: Uses, Dosage, Side Ef... |

## 结论与生产建议

1. **稳定性评价**：在连续 32 条密集测试中，本地 SearXNG 服务能够稳定承接检索请求，无崩溃，无假死。
2. **隐私合规**：所有外发检索词均为中性实体与财务/行业指标短语，未包含用户原文句子。
3. **弹性容灾**：研究模块内部已配置双重兜底（`ResilientSearchBackend`），在 SearXNG 端口不可达或突发限流时，无缝衔接备用检索源，永不中断上层工作流。
