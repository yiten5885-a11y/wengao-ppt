"""稳稿 PPT · 用户级安装管理入口（安装 / 启动 / 状态 / 升级 / 回退 / 卸载预演）。

设计原则（与主规格一致，不引入第二套安装平台）：
  * **程序与数据分离**：程序在 `<root>/versions/<label>/`，运行数据在 `<root>/data/`
    （CLI 以 `--project-root <root>/data` 运行；工作台以 `--project-root <root>/data` 启动）。
    旧用户数据永远不写进版本目录。
  * **指针驱动**：`<root>/current.json` 记录当前版本；用户不需要 `cd` 到某个版本目录。
  * **先自测再切换**：新版本先完成完整性检查 + 自测（导入、--help、必需资源、一次 precheck 冒烟），
    全部通过才写指针；失败保留旧版本、指针与数据不变。
  * **不删除**：卸载默认只**预演**；`--apply` 也只把程序目录**移入** `quarantine/`，
    用户数据（config/jobs/results）原地保留。

用法：
    python -m wengao_ppt.install install   --package <解压后的包目录> [--label L] [--activate]
    python -m wengao_ppt.install upgrade   --package <包目录> [--label L]
    python -m wengao_ppt.install rollback  [--to L]
    python -m wengao_ppt.install status    [--json]
    python -m wengao_ppt.install versions
    python -m wengao_ppt.install start     [--port 8791]
    python -m wengao_ppt.install cli       -- <wengao_ppt.cli 的参数...>
    python -m wengao_ppt.install uninstall [--apply]

根目录：`--root` 或环境变量 `WENGAO_HOME`，默认 `~/.wengao-ppt`。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCHEMA = "1.0.0"
REQUIRED = (
    "wengao_ppt/__init__.py", "wengao_ppt/cli.py", "wengao_ppt/runner.py",
    "wengao_ppt/source_fidelity.py", "wengao_ppt/page_catalog.py",
    "wengao_ppt/label_extract.py", "workbench/server.py", "workbench/index.html",
    "workbench/make_flow.py", "samples/r5_material_to_ppt.py",
    "contracts/schemas_v1.json", "contracts/error_codes_v1.json",
    "contracts/support_matrix_v1.json", "contracts/upstream_baseline.json",
    "vendor/luobo-ppt/LICENSE", "requirements.txt",
    # 渲染字体配置：缺它时安装实例仍能启动、仍能"渲染成功"，但中文全变方框。
    # 这是发布载荷的必需资源，必须由安装核验与自测共同把关。
    "assets/fontconfig/fonts.conf",
)


# ----------------------------------------------------------------- 基础工具
def sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


#: 安装器自己生成的记录文件不算构建身份的一部分（否则安装前后哈希会变）
MANAGER_FILES = {"INSTALL_RECORD.json", "SELFTEST_FAILED", "BUILD_ID"}
#: 运行期产物（渲染 profile / 字节码）不属于构建身份
RUNTIME_PREFIXES = (".lo-profile/", "__pycache__/")
RUNTIME_SUFFIXES = (".pyc", ".pyo")
#: 数据根 / 运行期状态目录：**不是程序成员**，不得进入构建身份。
#: 监督 2026-10-20 15:12 实证：8823 候选的隔离数据根 wb_loopback_data 位于程序
#: 目录内，server.log 也在程序树内——仅访问 /api/meta 就会追加日志，导致
#: build_id 每次必变、"两次连续只读访问"拿到两个身份。构建身份只聚合**程序**
#: 成员（代码/资源/契约/vendor/样例/工作台 UI）；排除项在此显式列明、可逐条
#: 核对，不用硬填常量掩盖运行期文件混入。
DATA_PREFIXES = ("wb_loopback_data/", "jobs/", "reports/", "workbench_uploads/",
                 "workbench_previews/", "data/", "logs/", "tmp/")
RUNTIME_SUFFIXES = (".pyc", ".pyo", ".log", ".pid", ".tmp", ".lock")


def build_id(prog_dir: Path) -> str:
    """构建身份 = 全量**程序**成员 (相对路径, 内容哈希) 的聚合摘要（与文件名/时间无关）。

    排除：安装器记录、渲染 profile、字节码、数据根与运行期文件（见上方常量）。
    排除项是显式清单而非通配掩盖——核对方式：`find <prog> -type f` 与
    `build_id` 实际计入成员的差集必须只含上述类别的运行期/数据文件。
    """
    h = hashlib.sha256()
    files = sorted(
        p for p in Path(prog_dir).rglob("*")
        if p.is_file() and p.name not in MANAGER_FILES
        and not p.relative_to(prog_dir).as_posix().startswith(RUNTIME_PREFIXES)
        and not p.relative_to(prog_dir).as_posix().startswith(DATA_PREFIXES)
        and not p.name.endswith(RUNTIME_SUFFIXES))
    for p in files:
        rel = p.relative_to(prog_dir).as_posix()
        h.update(f"{rel}\0{p.stat().st_size}\0{sha256(p)}\n".encode())
    return h.hexdigest()[:32]


def root_dir(args=None) -> Path:
    r = getattr(args, "root", None) or os.environ.get("WENGAO_HOME") or "~/.wengao-ppt"
    return Path(r).expanduser().resolve()


def paths(root: Path) -> dict:
    return {"root": root, "versions": root / "versions", "data": root / "data",
            "pointer": root / "current.json", "quarantine": root / "quarantine",
            "log": root / "install.log"}


def log(root: Path, msg: str) -> None:
    p = paths(root)["log"]
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}\n")


def read_pointer(root: Path) -> str | None:
    p = paths(root)["pointer"]
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("current")
    except Exception:  # noqa: BLE001
        return None


def write_pointer(root: Path, label: str) -> None:
    p = paths(root)["pointer"]
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"current": label, "updated_at": time.strftime(
        "%Y-%m-%dT%H:%M:%S%z")}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)          # 原子替换：不会出现"指针指向半个文件"


def current_prog(root: Path) -> Path | None:
    label = read_pointer(root)
    if not label:
        return None
    d = paths(root)["versions"] / label
    return d if (d / "wengao_ppt").is_dir() else None


def env_clean(data_root: Path | None = None) -> dict:
    """子进程环境：不继承 PYTHONPATH；把数据根显式传给产品，避免缓存写进程序目录。"""
    e = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    e["PYTHONDONTWRITEBYTECODE"] = "1"
    e["PYTHONNOUSERSITE"] = "1"
    if data_root is not None:
        e["WENGAO_DATA_ROOT"] = str(data_root)
    return e


# ----------------------------------------------------------------- 完整性 + 自测
def verify_package(pkg: Path) -> dict:
    missing = [r for r in REQUIRED if not (pkg / r).is_file()]
    files = [p for p in pkg.rglob("*") if p.is_file()] if pkg.is_dir() else []
    return {"ok": pkg.is_dir() and not missing, "missing": missing,
            "member_count": len(files),
            "build_id": build_id(pkg) if pkg.is_dir() and files else None}


def _cjk_render_probe(prog: Path, root: Path, tmp_data=None) -> dict:
    """用**本安装包**真实渲染一页纯中文探针，并按字形判据检查。

    这不是"能 import"或"能 precheck"：它要证明这个安装实例能把汉字画出来。
    没有 LibreOffice 时返回 ok=False + SKIPPED 说明（不冒充通过）。
    """
    from tempfile import mkdtemp
    tmp = Path(mkdtemp(prefix="wengao-cjkprobe-"))
    try:
        from wengao_ppt import glyphcheck, render as render_mod
        if not render_mod.real_render_available():
            return {"ok": False, "status": "SKIPPED",
                    "detail": "本机未找到 LibreOffice；中文渲染冒烟未执行（不是通过）。"}
        pptx = glyphcheck.build_probe_pptx(tmp / "cjk_probe.pptx")
        rr = render_mod.render_real(pptx, tmp / "render", data_root=tmp_data or tmp)
        if rr.get("code") != "RENDER.OK":
            return {"ok": False, "status": "FAILED",
                    "detail": f"{rr.get('code')}: {rr.get('detail')}"}
        g = glyphcheck.analyze_pdf(rr["pdf"], tmp / "render" / "png")
        v = glyphcheck.probe_verdict(g)
        return {"ok": bool(v["ok"]), "status": "EXECUTED",
                "detail": f"glyph verdict={g.get('verdict')} "
                          f"page1={v.get('page1_status')} page2={v.get('page2_status')}; "
                          f"fonts={','.join((rr.get('pdf_fonts') or [])[:4])}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "status": "CRASHED",
                "detail": f"{type(e).__name__}: {e}"}
    finally:
        import shutil as _sh
        _sh.rmtree(tmp, ignore_errors=True)


def selftest(prog: Path, root: Path) -> dict:
    """安装自测：导入 + --help + 必需资源 + 一次 precheck 冒烟（数据根用临时目录）。"""
    res: dict = {"checks": []}

    def add(name, ok, detail=""):
        res["checks"].append({"name": name, "ok": bool(ok), "detail": str(detail)[:300]})

    p = subprocess.run([sys.executable, "-c",
                        "import wengao_ppt,sys;print(wengao_ppt.__file__)"],
                       cwd=str(prog), env=env_clean(), capture_output=True, text=True,
                       timeout=180)
    add("import", p.returncode == 0 and str(prog) in (p.stdout or ""), (p.stderr or "")[-200:])
    p = subprocess.run([sys.executable, "-m", "wengao_ppt.cli", "--help"],
                       cwd=str(prog), env=env_clean(), capture_output=True, text=True,
                       timeout=180)
    add("cli --help", p.returncode == 0, (p.stderr or "")[-200:])
    missing = [r for r in REQUIRED if not (prog / r).is_file()]
    add("必需资源在位", not missing, missing)

    # 编译全部 .py：能发现"末尾追加非法代码"这类只在特定路径触发的破损
    import py_compile
    bad = []
    for f in sorted((prog / "wengao_ppt").rglob("*.py")):
        try:
            py_compile.compile(str(f), doraise=True)
        except Exception as e:  # noqa: BLE001
            bad.append(f"{f.name}: {type(e).__name__}")
    add("包内 .py 全部可编译", not bad, bad[:5])

    # 以**模块**方式导入核心模块（-m 会因 SystemExit 提前结束，漏掉尾部运行期错误）
    imp = subprocess.run(
        [sys.executable, "-c",
         "import wengao_ppt, wengao_ppt.cli, wengao_ppt.runner, wengao_ppt.source_fidelity;"
         "import os;print(os.pathsep.join([wengao_ppt.__file__, wengao_ppt.cli.__file__]))"],
        cwd=str(prog), env=env_clean(data_root=paths(root)["data"]),
        capture_output=True, text=True, timeout=180)
    add("核心模块可作为模块导入", imp.returncode == 0, (imp.stderr or "")[-300:])

    # 字体配置：不只是"文件在"，还要能从**本包资源位置**解析出有效配置，
    # 且缓存目录落在数据根（不写程序版本目录）。
    fc = subprocess.run(
        [sys.executable, "-c",
         "import json;from wengao_ppt import fontenv;"
         "e,r=fontenv.build_env();"
         "print(json.dumps({'ok':r.get('ok'),'conf':r.get('effective_conf'),"
         "'sha':r.get('effective_conf_sha256'),'cache':r.get('cache_dir'),"
         "'mode':r.get('mode'),'err':r.get('error'),'policy':r.get('policy')}))"],
        cwd=str(prog), env=env_clean(data_root=paths(root)["data"]),
        capture_output=True, text=True, timeout=180)
    fc_rec = {}
    if fc.returncode == 0:
        try:
            fc_rec = json.loads((fc.stdout or "").strip().splitlines()[-1])
        except Exception:  # noqa: BLE001
            fc_rec = {}
    data_dir = str(paths(root)["data"])
    add("字体配置可解析", bool(fc_rec.get("ok")) and bool(fc_rec.get("conf")),
        fc_rec.get("err") or fc_rec.get("conf") or (fc.stderr or "")[-200:])
    add("字体缓存写在数据根", bool(fc_rec.get("cache", "").startswith(data_dir)),
        fc_rec.get("cache"))

    # 真实中文出字冒烟：只在真实渲染可用时执行；不可用就如实记 SKIPPED（非通过）。
    st = _cjk_render_probe(prog, root, tmp_data=paths(root)["data"])
    add("中文渲染冒烟", st.get("ok"), st.get("detail"))
    add("模块来自本版本目录",
        imp.returncode == 0 and all(str(prog) in x for x in (imp.stdout or "").split(os.pathsep)),
        (imp.stdout or "").strip()[:200])

    tmp = Path(tempfile.mkdtemp(prefix="wengao-selftest-"))
    try:
        sample = tmp / "selftest.pptx"
        gen = subprocess.run(
            [sys.executable, "-c",
             "from pptx import Presentation;from pptx.util import Inches;"
             f"p=Presentation();p.slide_width,p.slide_height=12191695,6858000;"
             "s=p.slides.add_slide(p.slide_layouts[5]);s.shapes.title.text='selftest';"
             f"p.save({str(sample)!r})"],
            cwd=str(prog), env=env_clean(), capture_output=True, text=True, timeout=180)
        add("生成冒烟输入", gen.returncode == 0, (gen.stderr or "")[-200:])
        if gen.returncode == 0:
            q = subprocess.run([sys.executable, "-m", "wengao_ppt.cli",
                                "--project-root", str(tmp / "data"), "precheck",
                                "--input", str(sample), "--mode", "beautify",
                                "--theme", "wengao-basic"],
                               cwd=str(prog), env=env_clean(), capture_output=True,
                               text=True, timeout=600)
            add("precheck 冒烟", q.returncode == 0, (q.stderr or "")[-200:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)     # 只清自测临时目录
    res["ok"] = all(c["ok"] for c in res["checks"])
    return res


# ----------------------------------------------------------------- 安装 / 升级
def _copy_into_versions(pkg: Path, root: Path, label: str) -> Path:
    p = paths(root)
    p["versions"].mkdir(parents=True, exist_ok=True)
    staging = p["versions"] / f".staging-{label}-{int(time.time())}"
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(pkg, staging)
    return staging


def _reject(root: Path, payload: dict, stage: str, code: int = 4) -> int:
    """写入前拒绝的统一出口。

    刻意**不**调用 `log()`：写审计日志本身会在安装根下创建 `install.log`
    （及其父目录），那已经是写入——与"根不匹配就一个字节都不写"冲突。
    拒绝原因直接打印到 stdout，由调用方在隔离环境里自行留档。
    """
    print(json.dumps({"ok": False, "stage": stage, **payload},
                     ensure_ascii=False, indent=2))
    return code


def do_install(pkg: Path, root: Path, label: str | None, activate: bool,
               upgrade: bool, expect_root: str | None = None) -> int:
    p = paths(root)

    # ---- 写入前核对 1：目标根。测试入口显式声明了预期根，实际根必须完全一致。
    # 这一步在任何 mkdir / copytree / write_pointer / write_launcher 之前；
    # 根不匹配就停，不"先写后纠正"。
    if expect_root:
        # 两侧都按同一规则归一（resolve 会展开 /tmp -> /private/tmp 这类符号链接），
        # 否则同一个目录会因前缀不同被误判为"不是预期根"。
        want = Path(str(expect_root)).expanduser().resolve()
        if want != root.resolve():
            return _reject(root, {
                "expect_root": str(want), "resolved_root": str(root),
                "detail": "实际解析到的安装根与声明的预期根不一致，已在写入前拒绝；"
                          "未创建目录、未改指针、未写启动器。请用显式 --root "
                          "指向预期根后重试。",
            }, "root_mismatch")

    # ---- 写入前核对 2：源包身份。包路径必须真实存在且是目录。
    pkg = Path(pkg).expanduser()
    if not pkg.is_absolute():
        pkg = (caller_cwd() / pkg).resolve()
    else:
        pkg = pkg.resolve()
    if not pkg.exists():
        return _reject(root, {"package": str(pkg),
                              "detail": "解析后的包路径不存在。解析基准是调用者的原始"
                                        "cwd（不是启动器 chdir 后的版本目录）。"},
                       "package_missing")
    if not pkg.is_dir():
        return _reject(root, {"package": str(pkg),
                              "detail": "包路径不是目录。"}, "package_not_dir")

    ver = verify_package(pkg)
    if not ver["ok"]:
        print(json.dumps({"ok": False, "stage": "verify", "missing": ver["missing"]},
                         ensure_ascii=False, indent=2))
        log(root, f"install REFUSED verify pkg={pkg} missing={ver['missing']}")
        return 4
    label = label or f"build-{ver['build_id'][:8]}"
    existing = p["versions"] / label
    if existing.exists():
        # 同一构建再次安装（回退后再升级是最常见的路径）：若字节身份一致且该版本
        # 没有失败记录，直接复用并（按需）激活，不重复拷贝，也不报“同名版本已存在”。
        same = (verify_package(existing).get("build_id") == ver["build_id"]
                and not (existing / "SELFTEST_FAILED").is_file())
        if same:
            if activate or not read_pointer(root):
                write_pointer(root, label)
            launcher = write_launcher(root)
            print(json.dumps({"ok": True, "reused": True, "label": label,
                              "build_id": ver["build_id"],
                              "program": str(existing),
                              "activated": read_pointer(root) == label,
                              "current": read_pointer(root),
                              "data_root": str(p["data"]),
                              "entry": launcher["entry"],
                              "double_click": launcher["double_click"],
                              "note": "该构建已在安装根中：复用并切换指针，不重复拷贝。"},
                             ensure_ascii=False, indent=2))
            log(root, f"install REUSED label={label} build={ver['build_id']}")
            return 0
        print(json.dumps({"ok": False, "stage": "label_exists", "label": label,
                          "detail": "同名版本已存在且身份不一致或自测失败；"
                                    "换 --label 或先卸载预演。"},
                         ensure_ascii=False, indent=2))
        return 4

    staging = _copy_into_versions(pkg, root, label)
    st = selftest(staging, root)
    record = {"schema_version": SCHEMA, "label": label, "build_id": ver["build_id"],
              "source_package": str(pkg), "member_count": ver["member_count"],
              # 解析后的实际包根与安装根写进执行记录：事后可核对"装的到底是哪个目录"。
              "resolved_package_root": str(pkg),
              "resolved_install_root": str(root),
              "package_resolved_against_cwd": str(caller_cwd()),
              "expect_root": str(Path(str(expect_root)).expanduser().resolve())
                             if expect_root else None,
              "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "selftest": st, "python": sys.version.split()[0],
              "kind": "upgrade" if upgrade else "install"}
    (staging / "INSTALL_RECORD.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    if not st["ok"]:
        # 自测失败：不切换；留下失败记录供复核（不删，便于诊断）
        (staging / "SELFTEST_FAILED").write_text(
            json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"ok": False, "stage": "selftest", "label": label,
                          "selftest": st, "current": read_pointer(root),
                          "note": "未切换 current；旧版本与数据不变。"},
                         ensure_ascii=False, indent=2))
        log(root, f"install REFUSED selftest label={label}")
        return 3

    final = p["versions"] / label
    os.replace(staging, final)                       # 原子改名：半装目录不会成为 current
    if activate or not read_pointer(root):
        write_pointer(root, label)
    # 稳定入口：与版本无关，只绑定安装根与解释器；current 变更后自动跟随。
    launcher = write_launcher(root)
    print(json.dumps({"ok": True, "label": label, "build_id": ver["build_id"],
                      "program": str(final), "activated": read_pointer(root) == label,
                      "current": read_pointer(root), "data_root": str(p["data"]),
                      "entry": launcher["entry"],
                      "double_click": launcher["double_click"]},
                     ensure_ascii=False, indent=2))
    log(root, f"install OK label={label} build={ver['build_id']} activated={activate}")
    return 0


# ----------------------------------------------------------------- 回退 / 卸载
def installed_versions(root: Path) -> list[dict]:
    out = []
    vdir = paths(root)["versions"]
    if not vdir.is_dir():
        return out
    for d in sorted(vdir.iterdir()):
        if not d.is_dir() or d.name.startswith(".staging-"):
            continue
        rec = {}
        rp = d / "INSTALL_RECORD.json"
        if rp.is_file():
            try:
                rec = json.loads(rp.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                rec = {}
        out.append({"label": d.name, "build_id": rec.get("build_id") or build_id(d),
                    "installed_at": rec.get("installed_at"),
                    "ok": (d / "SELFTEST_FAILED").is_file() is False})
    return out


def do_rollback(root: Path, to: str | None) -> int:
    p = paths(root)
    cur = read_pointer(root)
    vs = [v for v in installed_versions(root) if v["ok"]]
    if not vs:
        print(json.dumps({"ok": False, "stage": "rollback", "detail": "没有可用版本"},
                         ensure_ascii=False, indent=2))
        return 4
    target = to or next((v["label"] for v in vs if v["label"] != cur), None)
    if not target or not (p["versions"] / target).is_dir():
        print(json.dumps({"ok": False, "stage": "rollback", "requested": to,
                          "available": [v["label"] for v in vs]},
                         ensure_ascii=False, indent=2))
        return 4
    prog = p["versions"] / target
    st = selftest(prog, root)
    if not st["ok"]:
        print(json.dumps({"ok": False, "stage": "rollback_selftest", "target": target,
                          "selftest": st, "current": cur,
                          "note": "目标版本自测未通过，未切换。"},
                         ensure_ascii=False, indent=2))
        return 3
    data = p["data"]
    data_files = sum(1 for _ in data.rglob("*") if _.is_file()) if data.is_dir() else 0
    write_pointer(root, target)
    print(json.dumps({"ok": True, "from": cur, "to": target,
                      "build_id": build_id(prog), "program": str(prog),
                      "data_root": str(data), "data_files": data_files,
                      "config_dir_present": (data / "config").is_dir(),
                      "jobs_present": (data / "jobs").is_dir(),
                      "note": "配置/任务在共享数据根，切版本不改动它们；"
                              "config 目录不存在表示本轮测试未创建配置（不是丢失）。",
                      "current": read_pointer(root)},
                     ensure_ascii=False, indent=2))
    log(root, f"rollback {cur} -> {target}")
    return 0


def do_uninstall(root: Path, apply: bool) -> int:
    p = paths(root)
    vs = installed_versions(root)
    data_files = sum(1 for _ in p["data"].rglob("*") if _.is_file()) if p["data"].is_dir() else 0
    plan = {"ok": True, "mode": "apply" if apply else "preview",
            "program_versions": [v["label"] for v in vs],
            "current": read_pointer(root),
            "user_data": {"root": str(p["data"]), "files": data_files,
                          "kept": True},
            "note": "卸载只处理**程序**；config/jobs/results 原地保留。"
                    "未提供 --apply 时仅预演，不改动任何文件。"}
    if not apply:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    if not vs:
        plan["ok"] = False
        plan["detail"] = "没有可移除的程序版本"
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 4
    dest = p["quarantine"] / time.strftime("%Y%m%dT%H%M%S")
    dest.mkdir(parents=True, exist_ok=True)
    moved = []
    for v in vs:
        src = p["versions"] / v["label"]
        shutil.move(str(src), str(dest / v["label"]))   # 移动而非删除：字节仍可复核
        moved.append(v["label"])
    ptr_backup = p["pointer"].with_suffix(".json.removed")
    if p["pointer"].is_file():
        shutil.move(str(p["pointer"]), str(ptr_backup))
    plan.update({"moved_to": str(dest), "moved": moved,
                 "pointer_backup": str(ptr_backup),
                 "user_data_files_after": sum(1 for _ in p["data"].rglob("*")
                                              if _.is_file()) if p["data"].is_dir() else 0})
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    log(root, f"uninstall apply moved={moved} -> {dest}")
    return 0


# ----------------------------------------------------------------- 运行
def do_status(root: Path, as_json: bool) -> int:
    prog = current_prog(root)
    out: dict = {"schema_version": SCHEMA, "root": str(root),
                 "current": read_pointer(root),
                 "data_root": str(paths(root)["data"]),
                 "python": sys.executable,
                 "versions": installed_versions(root)}
    if prog is None:
        out["ok"] = False
        out["detail"] = "未安装（current 为空或版本目录缺失）"
        print(json.dumps(out, ensure_ascii=False, indent=2) if as_json
              else f"未安装：{out['root']}")
        return 4
    p = subprocess.run([sys.executable, "-c", "import wengao_ppt;print(wengao_ppt.__file__)"],
                       cwd=str(prog), env=env_clean(), capture_output=True, text=True,
                       timeout=180)
    out.update({"ok": p.returncode == 0, "program": str(prog),
                "module_file": (p.stdout or "").strip(),
                "build_id": build_id(prog),
                "jobs": len(list((paths(root)["data"] / "jobs").glob("job-*")))
                if (paths(root)["data"] / "jobs").is_dir() else 0})
    if as_json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(f"根目录   : {out['root']}")
        print(f"当前版本 : {out['current']}  (build {out['build_id'][:12]})")
        print(f"程序目录 : {out['program']}")
        print(f"模块     : {out['module_file']}")
        print(f"数据根   : {out['data_root']}  (jobs={out['jobs']})")
        print(f"解释器   : {out['python']}")
        print("可用版本 : " + ", ".join(v["label"] for v in out["versions"]))
    return 0 if out["ok"] else 4


def _port_state(port: int) -> str:
    """返回 free / ours / other。只探测，不结束任何进程。"""
    import socket
    import urllib.request
    s = socket.socket()
    s.settimeout(0.6)
    try:
        in_use = s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()
    if not in_use:
        return "free"
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/bootstrap", timeout=2.0) as r:
            d = json.loads(r.read().decode("utf-8"))
        if "稳稿" in str(d.get("product") or ""):
            return "ours"
    except Exception:  # noqa: BLE001
        return "other"
    return "other"


def do_start(root: Path, port: int) -> int:
    prog = current_prog(root)
    if prog is None:
        print("未安装：先执行 install --package <包目录>", file=sys.stderr)
        return 4
    state = _port_state(port)
    if state == "ours":
        print(json.dumps({
            "started": False, "reused": True, "port": port,
            "url": f"http://127.0.0.1:{port}/",
            "note": "该端口已有稳稿 PPT 工作台在运行：直接使用它，不重复启动，也不结束该进程。",
        }, ensure_ascii=False, indent=2))
        return 0
    if state == "other":
        print(json.dumps({
            "started": False, "port": port, "reason": "PORT_IN_USE_BY_OTHER",
            "note": "该端口被其他程序占用；未终止任何进程。请换 --port（例如 8792）。",
        }, ensure_ascii=False, indent=2))
        return 4
    cmd = [sys.executable, str(prog / "workbench" / "server.py"),
           "--port", str(port), "--project-root", str(paths(root)["data"])]
    print(f"[start] {prog.name} · http://127.0.0.1:{port}/ （Ctrl-C 停止）", flush=True)
    return subprocess.call(cmd, cwd=str(prog), env=env_clean(paths(root)["data"]))


# ----------------------------------------------------------------- 稳定启动入口
BOOT_PY = '''#!/usr/bin/env python3
"""稳稿 PPT 启动引导：定位安装根、current 指针与版本目录，再调用安装入口。

- 不依赖调用者 cwd（可从任意目录运行，包括含空格/中文的目录）；
- 不依赖 PYTHONPATH；只把当前版本目录放进 sys.path；
- 版本每次运行都从 current.json 重新解析，升级/回退后自动跟随；
- chdir 到版本目录**之前**把调用者的原始 cwd 记进 WENGAO_CALLER_CWD，
  供 install 解析 `--package` 等用户相对路径（否则 "." 会被解释成版本目录）。
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
cfg_path = ROOT / "launcher.json"
if not cfg_path.is_file():
    sys.stderr.write("启动入口未初始化：缺少 launcher.json（重新执行 install 或 launcher 子命令）\\n")
    raise SystemExit(4)
cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
ROOT = Path(cfg.get("root") or ROOT).expanduser().resolve()
ptr = ROOT / "current.json"
if not ptr.is_file():
    sys.stderr.write("当前没有已安装版本（current.json 不存在）：先执行 install --package <包目录> --activate\\n")
    raise SystemExit(4)
label = (json.loads(ptr.read_text(encoding="utf-8")) or {}).get("current")
prog = ROOT / "versions" / str(label)
if not label or not (prog / "wengao_ppt").is_dir():
    sys.stderr.write("current 指针指向的版本目录不可用：%s\\n" % prog)
    raise SystemExit(4)
sys.path.insert(0, str(prog))
# 必须在 chdir **之前**记住调用者的原始 cwd：chdir 之后按 cwd 解释
# `--package .` 会把它解析成当前版本目录（"把当前版本再装一遍"），
# 而不是用户真正指定的包目录。install.main() 用它还原相对路径的解析基准。
CALLER_CWD = os.getcwd()
os.chdir(prog)
os.environ["WENGAO_HOME"] = str(ROOT)
os.environ["WENGAO_CALLER_CWD"] = CALLER_CWD
import wengao_ppt  # noqa: E402
if not str(Path(wengao_ppt.__file__).resolve()).startswith(str(prog)):
    sys.stderr.write("模块解析失败：加载到 %s，不是当前版本 %s\\n"
                     % (wengao_ppt.__file__, prog))
    raise SystemExit(4)
from wengao_ppt.install import main as install_main  # noqa: E402
raise SystemExit(install_main(sys.argv[1:]))
'''

SH_LAUNCHER = '''#!/bin/sh
# 稳稿 PPT 稳定入口：在任意目录执行本脚本即可（无需 cd、无需设置 PYTHONPATH）。
# 用法示例：
#   ./wengao status
#   ./wengao start --port 8792
#   ./wengao cli -- precheck --input /绝对路径/稿件.pptx --mode beautify --theme wengao-business
set -e
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PY=$(sed -n 's/.*"python": *"\\([^"]*\\)".*/\\1/p' "$ROOT/launcher.json" | head -1)
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
  echo "启动入口找不到安装时记录的解释器：请重新执行 install 或 launcher 子命令。" >&2
  exit 4
fi
exec "$PY" "$ROOT/bin/wengao_boot.py" "$@"
'''

CMD_LAUNCHER = '''#!/bin/sh
# 双击启动（macOS）：等价于 `<安装根>/bin/wengao start`。
# 若端口已被本产品实例占用，会直接显示其地址而不重复启动。
ROOT=$(cd "$(dirname "$0")" && pwd)
PORT="${WENGAO_PORT:-8791}"
exec "$ROOT/bin/wengao" start --port "$PORT"
'''


def write_launcher(root: Path) -> dict:
    """在安装根写入与版本无关的启动入口（shell 脚本 + 双击入口 + 引导）。

    入口只记录**安装根**与**安装时的解释器**；当前版本每次运行从 current.json 解析，
    因此升级/回退后自动跟随，不写死某个版本目录。
    """
    p = paths(root)
    b = root / "bin"
    b.mkdir(parents=True, exist_ok=True)
    (b / "wengao_boot.py").write_text(BOOT_PY, encoding="utf-8")
    sh = b / "wengao"
    sh.write_text(SH_LAUNCHER, encoding="utf-8")
    sh.chmod(0o755)
    cmd = root / "启动稳稿PPT.command"
    cmd.write_text(CMD_LAUNCHER, encoding="utf-8")
    cmd.chmod(0o755)
    rec = {"schema_version": "1.0.0", "root": str(root),
           "python": sys.executable, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "entry": str(sh), "double_click": str(cmd),
           "note": "入口与版本无关；当前版本由 <root>/current.json 决定。"}
    (root / "launcher.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    log(root, f"launcher written entry={sh} python={sys.executable}")
    return rec


def do_launcher(root: Path) -> int:
    rec = write_launcher(root)
    print(json.dumps({"ok": True, **rec}, ensure_ascii=False, indent=2))
    return 0


#: 这些选项的值是路径：用户按自己的 cwd 传相对路径时，要先转成绝对路径，
#: 否则在程序目录里执行会解析到错误位置（旧版会在版本目录下找输入）。
PATH_OPTS = ("--input", "--outdir", "--asset", "--material-source",
             "--material-ledger", "--material-plan", "--cancel-file")


def _absolutize(argv: list[str]) -> list[str]:
    cwd = Path.cwd()
    out: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in PATH_OPTS and i + 1 < len(argv):
            v = argv[i + 1]
            if not Path(v).is_absolute():
                v = str((cwd / v).resolve())
            out += [a, v]
            i += 2
            continue
        hit = next((o for o in PATH_OPTS if a.startswith(o + "=")), None)
        if hit:
            v = a.split("=", 1)[1]
            if not Path(v).is_absolute():
                v = str((cwd / v).resolve())
            out.append(f"{hit}={v}")
            i += 1
            continue
        out.append(a)
        i += 1
    return out


def do_cli(root: Path, argv: list[str]) -> int:
    prog = current_prog(root)
    if prog is None:
        print("未安装：先执行 install --package <包目录>", file=sys.stderr)
        return 4
    cmd = [sys.executable, "-m", "wengao_ppt.cli",
           "--project-root", str(paths(root)["data"]), *_absolutize(argv)]
    return subprocess.call(cmd, cwd=str(prog), env=env_clean(paths(root)["data"]))


# ----------------------------------------------------------------- CLI
def caller_cwd() -> Path:
    """**调用者的原始 cwd**，而不是启动器 chdir 之后的 cwd。

    为什么必须区分：稳定入口 `bin/wengao_boot.py` 为了让模块从版本目录导入，
    会在调用 `main()` 之前 `os.chdir(版本目录)`。若此后再按 `Path.cwd()` 解释
    `--package .`，用户给的包路径就会被解析成**当前已安装版本目录**，
    于是 `install --package .` 变成"把当前版本再装一遍"，而不是装用户指的包。
    启动器因此把原始 cwd 通过 `WENGAO_CALLER_CWD` 传下来；直接以模块方式运行
    时它就是进程 cwd，行为不变。
    """
    env_cwd = os.environ.get("WENGAO_CALLER_CWD")
    if env_cwd:
        try:
            return Path(env_cwd).expanduser().resolve()
        except OSError:
            pass
    return Path.cwd()


def resolve_user_path(raw: str) -> Path:
    """把用户给的路径按**调用者 cwd** 解析成绝对路径（相对路径不因 chdir 而漂移）。"""
    p = Path(str(raw)).expanduser()
    if p.is_absolute():
        return p.resolve()
    return (caller_cwd() / p).resolve()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m wengao_ppt.install",
        description="稳稿 PPT 用户级安装管理（安装/启动/状态/升级/回退/卸载预演）")
    ap.add_argument("--root", default=None, help="安装根（默认 $WENGAO_HOME 或 ~/.wengao-ppt）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("install", help="安装一个包目录")
    p.add_argument("--package", required=True)
    p.add_argument("--label", default=None)
    p.add_argument("--activate", action="store_true", help="安装后立即切换 current")
    p.add_argument("--expect-root", default=None,
                   help="测试/自动化入口用：声明预期安装根。实际解析根与它不一致时"
                        "在任何写入（建目录/改指针/写启动器/装依赖）之前拒绝。"
                        "普通用户安装不必传。")

    p = sub.add_parser("upgrade", help="升级到新包（自测通过才切换）")
    p.add_argument("--package", required=True)
    p.add_argument("--label", default=None)
    p.add_argument("--expect-root", default=None,
                   help="测试/自动化入口用：声明预期安装根，不一致则写入前拒绝。")

    p = sub.add_parser("rollback", help="回退到指定/上一个版本")
    p.add_argument("--to", default=None)

    p = sub.add_parser("status", help="查看当前安装状态")
    p.add_argument("--json", action="store_true")

    sub.add_parser("versions", help="列出版本")

    p = sub.add_parser("start", help="用当前版本启动工作台（仅回环）")
    p.add_argument("--port", type=int, default=8791)

    sub.add_parser("launcher", help="（重新）写入稳定启动入口与双击入口")

    p = sub.add_parser("cli", help="用当前版本执行产品 CLI（透传参数）")
    p.add_argument("rest", nargs=argparse.REMAINDER)

    p = sub.add_parser("uninstall", help="卸载预演；--apply 仅移动程序，保留数据")
    p.add_argument("--apply", action="store_true")

    a = ap.parse_args(argv)
    root = root_dir(a)

    # 写入前核对 0：声明了预期根就必须在**建任何目录之前**核对。
    # 早前这里无条件 `paths(root)["data"].mkdir(...)`，导致"根不匹配"的拒绝
    # 仍然会在目标根下留下 data/ —— 拒绝却在目标根写了一个字节，语义不对。
    expect_root = getattr(a, "expect_root", None)
    if expect_root and a.cmd in ("install", "upgrade"):
        want = Path(str(expect_root)).expanduser().resolve()
        if want != root.resolve():
            return _reject(root, {
                "expect_root": str(want), "resolved_root": str(root),
                "detail": "实际解析到的安装根与声明的预期根不一致，已在任何写入之前"
                          "拒绝；未创建目录、未改指针、未写启动器。请用显式 --root "
                          "指向预期根后重试。",
            }, "root_mismatch")

    paths(root)["data"].mkdir(parents=True, exist_ok=True)

    if a.cmd == "install":
        return do_install(resolve_user_path(a.package), root, a.label,
                          a.activate, upgrade=False, expect_root=expect_root)
    if a.cmd == "upgrade":
        return do_install(resolve_user_path(a.package), root, a.label,
                          True, upgrade=True, expect_root=expect_root)
    if a.cmd == "rollback":
        return do_rollback(root, a.to)
    if a.cmd == "status":
        return do_status(root, a.json)
    if a.cmd == "versions":
        print(json.dumps({"root": str(root), "current": read_pointer(root),
                          "versions": installed_versions(root)},
                         ensure_ascii=False, indent=2))
        return 0
    if a.cmd == "start":
        return do_start(root, a.port)
    if a.cmd == "launcher":
        return do_launcher(root)
    if a.cmd == "cli":
        rest = list(a.rest)
        if rest and rest[0] == "--":
            rest = rest[1:]
        return do_cli(root, rest)
    if a.cmd == "uninstall":
        return do_uninstall(root, a.apply)
    return 2


if __name__ == "__main__":
    sys.exit(main())
