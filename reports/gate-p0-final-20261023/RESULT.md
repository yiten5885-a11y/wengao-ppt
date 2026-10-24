# P0 最终视觉门禁及身份绑定 · 交付报告（2026-10-23）

- **工作包**：5｜P0 最终视觉门禁及身份绑定
- **执行者**：WorkBuddy 会话（本工作包只改自己文件；`workbench/server.py`、
  `wengao_ppt/*`、`contracts/*`、`scripts/source_corpus_review_gate.py` 属其他
  正在活跃工作的工作包，本轮**零触碰**）
- **候选**：`wengao-visual-v13-material-fix`（8821 未运行未触碰；8823 仅只读
  `/api/meta` 观测；共享 Ollama/代理/CLI 未动；无模型调用、无下载、无外部请求）

---

## 1. 结论（截至本报告时刻）

| 验收项 | 结果 |
| --- | --- |
| 冻结当前导出 `f92232e1…` 全 13 页全范围独立门禁（HG01–HG13 + 原生实播机证） | **PASS**（0 findings / 0 unverified） |
| 图像语义/位置/裁切审计（计划↔内嵌逐项绑定） | **全部一致**（unplanned=0） |
| 历史负例 N01–N08 重跑（真实失败件） | **8/8 PASS**（旧 N01/N04 的 HG06 缺口已闭合） |
| 门禁行为负例 NC01–NC11b（合成夹具，真实渲染路径） | **14/14 PASS** |
| 2026-10-21 第七轮候选 `dc135f46` 重验 | 静态 0 findings；旧 30 条 HG04 alpha 未证项**全部结构性证实**为"无填充+轮廓可证无=不可能遮挡"；HG10 原生实播**如实保留 UNVERIFIED**（该历史件无实播证据，不伪造、不沿用旧 PASS） |
| 单元测试（全套） | 298/298 PASS |
| 验收 manifest（全部哈希磁盘重算） | 绑定自检 **problems: none** |

**按规则声明**：以上是本工作包的真实执行结果，不构成"整个项目完成"；
最终判定须由独立验收者按第 5 条复跑。原生实播录像为 2026-10-22 监督侧
既有证据（本包未新录、未伪造），门禁对其做了逐项机证。

## 2. 基线与改动文件（规则 1、5）

修改前基线 SHA：`reports/gate-p0-final-20261023/BASELINE_SHAS.txt`
（含 `scripts/layout_gate.py=d575ba9c…` 等 5 个文件，改动前哈希可审计回退）。
修改后 SHA：`reports/gate-p0-final-20261023/CHANGED_FILE_SHAS.txt`。

| 文件 | 改动 | 改后 SHA256 |
| --- | --- | --- |
| `scripts/layout_gate.py` | ① HG04 遮挡候选按 HG02 逐字可见性证据**三态处置**（命中/证据化消解入台账/保留未证）；② 新增 **HG13** 逐页结构清单（对象/备注/媒体/动画/转场，从被检件磁盘重算 + 序列化哈希绑定，fail-closed）；③ HG11 身份链增加门禁**自算** build_id（复用 `wengao_ppt.install.build_id` 只读导入）；④ 新增 **HG06b 语义区域侵入**规则 M（正文上侵 KPI 数字区）与 N（结论被吸入并行内容卡家族中无正文的卡）——政策 `negative_regressions` 对 N01/N04 `must_hit HG06` 的缺口由此闭合 | `79357b97…` |
| `scripts/run_negative_regression.py` | N08 输出目录改 `gate-P268-final-rerun-20261023`（历史证据目录原样保留）；mkdir 兼容沙箱误报 | `ab8b7c8d…` |
| `scripts/run_gate_negative_cases.py` | **新增**：NC01–NC11b 门禁行为负例 harness | `a7c415e1…` |
| `scripts/run_p0_final_gate.py` | **新增**：冻结导出全范围验收 runner（含图像语义审计） | `01791cfc…` |
| `scripts/build_p0_manifest.py` | **新增**：验收 manifest 构建（全部哈希磁盘重算 + 互证自检） | `e99e72c6…` |
| `scripts/run_gate_13p.py`、`glyph_gate.py`、`geometry_gate.py` | 未改动（基线 SHA 与改后一致） | `fff25395…`/`c8f3adf8…`/`1f71329e…` |

## 3. 设计要点

1. **HG04 证据化三态**（修的是"静态判据不可证=永远 UNVERIFIED"的死结，不是放行）：
   与遮挡容器相交的字形若存在 invisible/partial → 命中 `LATER_SHAPE_OCCLUSION`；
   相交字形全部 proven visible（笔画覆盖≥0.70）→ 记入 `occlusion_resolved`
   台账（含逐字覆盖证据）；缺可见性证据 → 保留 UNVERIFIED。NC08（alpha=0.92
   实际遮死→命中）与 NC08b（alpha=0.45 透见→消解且绑定 ≥0.70 覆盖证据）
   双向验证了该路径。
2. **HG06b 规则 M/N** 均以"真实重叠"为判据、不引入新阈值：干净 13 页候选与
   dc135f 重跑均 0 误报（0 findings），N01/N04 精确命中。
3. **HG13 fail-closed**：对象（z 序/几何/填充/裁切/旋转）、备注（内容 SHA）、
   媒体（blob SHA/像素尺寸/版位）、动画（timing 节点计数 + XML SHA）、转场
   （类型 + XML SHA）任一成员不可建立即 UNVERIFIED 阻断。
4. **身份链**：门禁自算 build_id + 报告内逐页全字/无字渲染 SHA、PDF SHA、
   基线 SHA；manifest 构建时全部从磁盘重算并与门禁报告互证。

## 4. 可复现命令（解释器 `/Users/huangyingteng/.workbuddy/binaries/python/envs/default/bin/python`）

```bash
cd /Users/huangyingteng/WorkBuddy/2026-10-12-00-54-41/wengao-ppt/build/wengao-visual-v13-material-fix
export PYTHONDONTWRITEBYTECODE=1
PY=<上述解释器>
# ① 单元测试
$PY -m unittest discover tests                                   # 298/298 OK
# ② 冻结导出全范围验收（PASS；产物在 reports/gate-p0-final-20261023/final-acceptance/）
$PY scripts/run_p0_final_gate.py
# ③ 历史负例 N01–N08（8/8；汇总在 reports/negative-regression-20261021/）
$PY scripts/run_negative_regression.py
# ④ 门禁行为负例 NC01–NC11b（14/14；夹具只写 reports/gate-p0-final-20261023/negative-cases/fixtures/）
$PY scripts/run_gate_negative_cases.py
# ⑤ dc135f 第七轮重验（命令内嵌于本报告 §6；产物 dc135f-recheck/）
# ⑥ 验收 manifest（磁盘重算全部哈希并互证；problems 必须 none）
$PY scripts/build_p0_manifest.py build
```

## 5. 冻结身份与 manifest

- 冻结导出：`wb_loopback_data/jobs/job-4d584240ec05/candidate/fc829139_v25-readable-13p-dedicated.themed.pptx`
  SHA `f92232e1820b60e23d9d9e12a249b689e7d3ce9c9506cff49f41c96b091d300c`
  （运行前后两次哈希核验一致，检查过程零改写）
- 输入/基线：`input_raw/fc829139_v25-readable-13p-dedicated.pptx`
  SHA `0edb7ea298bf20c66f735fa5c8b71613ac7ae653036ad246c6c4420c380b5711`
- PDF：`final-acceptance/render/…themed.pdf` SHA `9e782042b531cf1fcc794999aa3102a0227c7de4ce5ff0f9613a0b1542b95961`
- 逐页渲染：13×（全字+无字）1920px PNG，SHA 逐页记录于
  `ACCEPTANCE_MANIFEST.json` → `renders.full_1920px / notext_1920px`（与门禁
  报告互证一致）
- 原生实播证据：`layout_gate_evidence/f92232e1…/native_playback.json`
  （video `59ae7d6e…` 45.5s/1920×1080/30fps，AVFoundation 全解码，磁盘重开
  身份一致；门禁机证 `machine_checks_pass=true`）
- 图像语义：P1 场景图（Wikimedia 公有领域，provider/来源键记录在案）内嵌
  SHA=计划 SHA、页位一致、对称居中裁切（cover 适配）、裁后等比 ≤2%、画布内；
  其余 10 张内嵌图全部溯源到基线原稿设计图（policy 不变量
  original_media_bytes 保留），计划外新增=0
- 结构清单：13 页对象/备注/媒体/动画/转场逐页清单（含 XML SHA）在门禁报告
  `per_page.*.structure`；manifest 重算互证一致

## 6. dc135f（第七轮）重验明细

产物：`reports/gate-p0-final-20261023/dc135f-recheck/`（13 页全字+无字渲染 +
gate-report.json）。复现命令：

```bash
$PY - <<'EOF'
import sys, json; sys.path.insert(0,'.'); sys.path.insert(0,'scripts')
import layout_gate as lg; from pathlib import Path
OUT=Path("reports/gate-p0-final-20261023/dc135f-recheck"); OUT.mkdir(parents=True,exist_ok=True)
POLICY=json.loads(Path("/Users/huangyingteng/Documents/Codex/2026-10-20/n-2/outputs/稳稿_严格产品验收/排版硬门禁_20261021/排版硬门禁.json").read_text(encoding="utf-8"))
PPTX=Path("reports/gate-13p-20261021/v14-readable-13p-dedicated.pptx")
BASE=Path("reports/p0-native-glyph-20261020/evidence/exact_v12.pptx")
pdf=lg.render_pdf(PPTX,OUT/"render"); pf=lg.render_pngs(pdf,OUT/"render","full")
nt=OUT/"notext"; nt.mkdir(parents=True,exist_ok=True)
ntp=lg.strip_text_pptx(PPTX,nt/"dc135f-notext.pptx"); ntpdf=lg.render_pdf(ntp,nt)
pn=lg.render_pngs(ntpdf,nt,"notext")
rep=lg.check(PPTX,pdf,pages=list(range(1,14)),baseline=BASE,expect_pptx_sha=lg.sha256(PPTX),policy=POLICY,png_full=pf,png_notext=pn,static_only=True,source_fidelity_verified=False)
(OUT/"gate-report.json").write_text(json.dumps(rep,ensure_ascii=False,indent=1),encoding="utf-8")
print(rep["verdict"], rep["counts"], len(rep["unverified"]))
EOF
```

- 静态全 13 页：0 findings（与旧报告一致，无新增缺陷、无新规则误报）
- 旧 30 条 `OCCLUSION_ALPHA_UNRESOLVED`：逐条对被检件重算 → 30/30 为
  `fill=none + 轮廓可证无（drawingml_hardcoded_no_fill_no_style_or_placeholder）`
  → 结构上不可能遮挡，当前门禁正确排除（旧报告为旧版门禁误分类）
- 旧 2 条 HG10（原生实播缺失/未跑）：**如实保留 UNVERIFIED**——dc135f 历史
  件没有实播证据，本包不伪造录像、不沿用任何旧 PASS

## 7. 真实失败与未解决风险（规则 5，全部如实）

1. **候选程序树并行漂移**：本轮期间 build_id 三次只读观测各不相同
   （`e7d492e5` → `0ab37fc5` / `63fca1eb` → `cea9ea4a`），说明其他工作包正在
   活跃修改程序成员。冻结导出件字节身份稳定且经 manifest 互证；但"同一
   build SHA"在程序树层面以 manifest 记录的漂移观测为准，需独立验收者在
   树静止时刻复算确认。
2. **dc135f HG10**：该历史件无原生实播证据，UNVERIFIED 保持（阻断性状态，
   不得解除）。若监督要求补录，须对该历史件真实放映，本包不代办。
3. **门禁 PASS 的独立性边界**：本包产出为"独立实现+实跑"的结果，按用户规则
   仍须**独立验收者复跑**；本包不自行宣告整个项目完成。
4. **HG06b 规则 N 的适用面**：以"并行内容卡家族"为锚，若出现非平行布局的
   同类缺陷可能漏报（当前政策清单内 8 个负例与 14 个合成负例未覆盖该形态；
   如监督提出新形态，按政策修订流程追加）。
5. **8821 全程未运行**（本周期连接拒绝）：未触碰、未重启、未探针。

## 8. 交付物清单

`reports/gate-p0-final-20261023/`：
`ACCEPTANCE_MANIFEST.json`、`BASELINE_SHAS.txt`、`CHANGED_FILE_SHAS.txt`、
`final-acceptance/`（gate-report.json、image-semantic-audit.json、26 张逐页
渲染、无字副本）、`dc135f-recheck/`（同构）、`negative-cases/`（NC-results.json
+ 12 个受控夹具及其真实渲染）、`negative-regression-rerun/`（新旧汇总存档）、
本报告 `RESULT.md`。
