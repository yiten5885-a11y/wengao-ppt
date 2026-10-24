# W3 联网研究模块最终交付总结报告 (Final Report)

**交付窗口**：第 3 开发窗口（W3 联网研究）  
**对应工作包**：RS-3 联网检索与对比，以及 RS-6 多语言检索  
**交付日期**：2026-10-24  
**公共契约版本**：`rs-contracts-v1`

---

## 1. 做了什么（交付物清单）

我们严格在 **W3 独占范围** 内完成了全部既定研发任务，无任何越界修改，未触碰任何受保护文件：

### A. 核心模块代码 (`wengao_ppt/research/`)
1. **类型契约 (`types.py`)**：严格实现公共契约 C4 定义的 `rs.research_bundle.v1` 与 `rs.verify_report.v1` 全套数据模型。
2. **安全网络层 (`netsafe_compat.py`)**：
   - 严禁触碰 8821 端口；
   - 环回请求 (127.0.0.1:8890, 11434) 强制绕过环境变量代理 (`ProxyHandler({})`)；
   - 严格遵循 fake-IP 环境兼容规范（不依赖 `socket.gethostbyname` 误判私网地址）。
3. **模型交互 (`llm.py`)**：本地 Ollama Qwen 模型调用封装，支持结构化 JSON 提示词与兼容资源锁机制。
4. **研究问题规划 (`plan.py`)**：自动生成横向对比与纵向趋势检索词；严格保证外发请求仅含中性实体词/指标词，绝不泄露用户原文段落；抽取用户自带事实 (`USER_MATERIAL`)。
5. **检索后端适配器 (`search/`)**：
   - `SearXNGBackend`：默认连接 `http://127.0.0.1:8890`；
   - `DirectSearchBackend`：公网高可用后备检索源；
   - `MockSearchBackend`：确定性单元测试适配器；
   - `ResilientSearchBackend`：主备双轨弹性路由，保证检索链路永不崩溃。
6. **快照抓取与落盘 (`fetch.py`)**：页面 HTML 安全下载、清洗提取正文纯文本，保存快照到 `evidence/research/snapshots/<fact_id>.txt` 并计算 SHA-256。
7. **事实抽取与分级 (`extract.py`)**：结构化抽取关键数字事实与连续 `quote_span`，进行权威来源分级（A 级官方/财报、B 级主流财经媒体、排除内容农场与垃圾源）。
8. **逐字核验与可比性 (`ground.py`)**：
   - 落地核验纪律：`quote_span` 必须在快照中连续逐字出现，且数值和单位与之匹配（否则以 `RESEARCH.QUOTE_NOT_IN_SNAPSHOT` / `RESEARCH.UNIT_OR_PERIOD_MISMATCH` 阻断丢弃）；
   - 可比性检查：横纵对比两端同单位、同口径，不可比则不进同图并记录 `omissions`。
9. **亮点纯规则计算 (`highlight.py`)**：纯数学与算法计算 `max_gap`、`streak`、`inflection`，差值与百分点由代码算好存入 `computed`，坚决不让模型凭空编造数字。
10. **视图组装与冲突并列 (`compare.py`)**：
    - 组装 `comparison`、`trend`、`insight` 三类视图；
    - 用户事实与网络事实冲突时并列呈现，标“待确认”，生成 `conflicts` 记录，**绝对不覆盖用户材料原值**；
    - 自动生成权威引用来源页脚 `Footnote`。
11. **独立重算校验器 (`verify.py`)**：`verify_bundle(job_dir) -> VerifyReport`，只读磁盘重算快照哈希、逐字出现与可比性，不信 bundle 自报字段，供门禁脚本无偏差复核。
12. **总编排入口 (`runner.py`, `__init__.py`)**：`run_research(material_text, options, job_dir, emit)`，完整驱动 stage 事件流（`research` / `compare` / `trend`），关闭或失败时优雅降级并记录 `omissions`，永远不抛阻断异常。

### B. 本地 SearXNG 部署与工具 (`tools/searxng/`)
1. `searxng_service.py`：基于 Python 实现的 SearXNG 本地微服务，绑定 `127.0.0.1:8890`，对外提供完全兼容 SearXNG 的 `/search?q=...&format=json` 接口，解决本机无 Docker 的环境问题。
2. `start_searxng.sh`：一键检测与后台启动脚本。
3. `test_stability.py`：包含 32 条真实中英文行业检索查询的稳定性评测脚本。

### C. 错误码登记 (`contracts/errors/research.json`)
登记 `RESEARCH.*` 命名空间下的全部错误码：`QUOTE_NOT_IN_SNAPSHOT`, `SOURCE_MISSING`, `UNIT_OR_PERIOD_MISMATCH`, `INCOMPARABLE_DEFINITION`, `NO_RELIABLE_SOURCE`, `SEARCH_BACKEND_ERROR`, `FETCH_FAILED`, `FACT_CONFLICT`, `OMISSION_RECORDED`。

### D. 固定样例包 (`fixtures/research/`)
供 W2 版式窗口无阻塞并行使用的 4 份标准固定样例（均标 `fixture: true` 并附带真实形态快照文本与 SHA-256）：
- `bundle_sample_zh.json` (中文汽车行业横纵对比与洞察)
- `bundle_sample_en.json` (英文特斯拉与比亚迪交付量横向对比)
- `bundle_sample_empty.json` (未检索到外部数据时的合法空 bundle 与 omissions)
- `bundle_sample_conflict.json` (用户材料自述与外部权威机构统计冲突并列呈现)
- `fixtures/research/snapshots/*.txt` (对应 8 个快照文件)

### E. 自动化测试套件 (`tests/research/`)
编写 17 个自动化测试，全面覆盖：
- 契约格式与 Fixture 快照校验；
- 亮点引擎数学规则精度；
- 规划隐私与中性词生成；
- 六项红队用例（伪造数字/引文被拒、快照篡改被拒、来源缺失被拒、不可比被拒、冲突并列呈现、检索空结果降级不崩）；
- verify_bundle 独立磁盘重算与防篡改验证。

---

## 2. 验收证据与测试结果

1. **单元测试与红队测试**：
   - 运行：`pytest tests/research -v`
   - 结果：**17 passed in 40.10s (100% 通过)**。
2. **全量回归测试**：
   - 运行：`pytest tests/ -q`
   - 结果：**348 passed, 311 subtests passed (原 331 测试零回归，新增 17 测试全部通过)**。
3. **SearXNG 稳定性基准测试** (`reports/rs-w3/SEARXNG_STABILITY.md`)：
   - 32 条密集真实查询（18 条中文 + 14 条英文）；
   - 成功率：**100% (32/32)**；
   - 平均响应延迟：`0.71s`，p50：`0.46s`，p90：`0.99s`。
4. **隐私合规审计** (`reports/rs-w3/PRIVACY_AUDIT.md`)：
   - 实际发出的全部检索词经审计核验，均为中性实体与财务/行业指标短语，零原文长句泄露。
5. **三类真实输入端到端真实验收** (`reports/rs-w3/REAL_RUNS.md`)：
   - 中文新闻稿：耗时 38s，核验 11 项事实，产出 2 个视图，`verify_bundle` **PASS**；
   - 英文公司简介：耗时 33s，核验 1 项事实，产出 1 个视图，`verify_bundle` **PASS**；
   - 一句话主题：耗时 40s，核验 10 项事实，产出 2 个视图，`verify_bundle` **PASS**。

---

## 3. 未完成与风险评估

1. **外部网站反爬风险**：个别网站（如 Tesla 官网部分路径、百度百科等）对爬虫返回 403 阻断。当前架构严格实现了优雅降级机制，遇到无法抓取站点会自动记录并切换下一条候选，若所有候选均受阻则触发 `RESEARCH.NO_RELIABLE_SOURCE` 生成 `omissions`，保证出稿永不中断。
2. **本地大模型推理耗时**：本地 Mac mini 上 27B Qwen 连续执行抽取可能耗时 15-20 秒，目前已优化 prompt 长度截断并配备规则抽取双重兜底，兼顾了准确率与响应速度。

---

## 4. 需要其他窗口配合的事项

1. **W2（版式与成稿）**：
   - 直接读取并使用 `fixtures/research/` 提供的 4 份标准 JSON 与快照进行对比页、趋势页、洞察页的排版渲染；
   - 对比视图中的差值、增长率等关键数据已由 W3 在 `views[].highlight.computed` 中计算完毕，W2 直接取用即可，无需另行计算；
   - 当遇到 `conflicts` 时，按照 `bundle_sample_conflict.json` 样例将两组数据并列展示并标注“待确认”。
2. **W4（工作台与集成）**：
   - 在编排管线中接入 `from wengao_ppt.research import run_research, verify_bundle`；
   - 在主门禁中注册 `verify_bundle`；
   - 在前端界面中展示 `evidence/research/queries.json` 供用户审计外发查询词，展示 `evidence/research/report.md` 作为研究报告。
