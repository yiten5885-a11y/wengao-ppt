"""渲染字体环境的**唯一**构造点。

为什么要有这个模块
------------------
事故 `incident-20261017-f88b`：发布包漏带 `assets/fontconfig/fonts.conf`，
`render._fontconfig_env()` 在安装布局里解析不到配置 → 返回空环境 →
LibreOffice 看不到任何中文字体 → 全部中文画成方框，退出码仍是 0。
同时缓存目录写进了程序版本目录，`XDG_CACHE_HOME` 落到只读安装目录。

本模块负责：
1. 从**包自身资源位置**解析字体配置（不依赖 cwd、不依赖作者桌面、不依赖原工程）；
2. 按当前 OS 生成平台字体目录（标准系统目录由适配器给出，个人目录用运行时 HOME）；
3. 把有效配置与缓存写到**数据根**（不写程序版本目录）；
4. 给出可复核的环境指纹：配置内容哈希、平台目录、缓存/profile 位置、
   渲染器版本；实际解析到的字体身份由 PDF 侧另行提取（见 `glyphcheck`）。

边界：只影响本次渲染子进程的环境变量；不改 /etc、不改用户 fontconfig、
不复制、不下载任何字体文件。
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

#: 包内必需资源（相对程序目录）。发布清单与安装核验都引用这一处，
#: 避免"打包脚本加了、安装核验没加"这种两套清单漂移。
REQUIRED_FILES = ("assets/fontconfig/fonts.conf",)

#: 配置里插入平台目录的标记。模板本身保持平台中立，
#: 具体目录在运行时按当前 OS 写入有效配置。
PLATFORM_MARKER = "<!--WENGAO_PLATFORM_FONT_DIRS-->"

#: 生成的有效配置版本。改变生成逻辑时必须递增，使缓存指纹随之变化。
ENV_POLICY_VERSION = "fontenv/2"

#: 测试/诊断用：显式指定配置（A/B 复验的"旧行为"分支）。
OVERRIDE_ENV = "WENGAO_FONTCONFIG_FILE"
#: 测试/诊断用：强制关闭配置注入，复现"没有配置"的事故条件。
DISABLE_ENV = "WENGAO_FONTCONFIG_DISABLE"


def package_root() -> Path:
    """程序目录（含 `wengao_ppt/`、`assets/`、`contracts/` 的那一层）。

    安装布局是 `~/.wengao-ppt-xxx/versions/build-xxxx/`；开发布局是工程根。
    两者都以本文件所在包目录的父目录为准，不读 cwd。
    """
    return Path(__file__).resolve().parents[1]


def base_conf_path() -> Path:
    return package_root() / "assets" / "fontconfig" / "fonts.conf"


def missing_resources(prog: Path | None = None) -> list[str]:
    """包内必需的字体资源是否齐全。缺失时调用方必须阻断，不得静默降级。"""
    root = Path(prog) if prog is not None else package_root()
    return [r for r in REQUIRED_FILES if not (root / r).is_file()]


def data_root(explicit: str | Path | None = None) -> Path:
    """数据根：显式参数 → 环境变量 → 用户缓存目录（开发态兜底）。"""
    if explicit:
        return Path(explicit)
    env = os.environ.get("WENGAO_DATA_ROOT")
    if env:
        return Path(env)
    return Path.home() / ".cache" / "wengao-ppt"


def platform_font_dirs() -> list[str]:
    """当前 OS 的标准字体目录（平台适配器）。

    只列**标准**目录；个人目录由 fontconfig 的 `~` 展开处理，
    不写死任何用户名或作者路径。
    """
    home = str(Path.home())
    if sys.platform == "darwin":
        return ["/System/Library/Fonts", "/System/Library/Fonts/Supplemental",
                "/Library/Fonts", f"{home}/Library/Fonts",
                "/Applications/LibreOffice.app/Contents/Resources/fonts/truetype"]
    if os.name == "nt":
        win = os.environ.get("WINDIR") or r"C:\Windows"
        local = os.environ.get("LOCALAPPDATA") or f"{home}\\AppData\\Local"
        return [f"{win}\\Fonts", f"{local}\\Microsoft\\Windows\\Fonts"]
    return ["/usr/share/fonts", "/usr/local/share/fonts", f"{home}/.fonts",
            f"{home}/.local/share/fonts"]


def packaged_font_dir() -> Path | None:
    """随包分发的字体目录（`<package_root>/fonts`，W2 放入的 Noto 系 OFL 字体）。

    产品声明"只用可再分发字体"，渲染环境就必须能解析到这些字体，
    否则声明只是空话。目录不存在时返回 None（不写死、不报错）。
    """
    d = package_root() / "fonts"
    return d if d.is_dir() else None


def _conf_dirs_block() -> str:
    """平台标准目录 + 包内分发字体目录。

    包内目录放在**最后**：系统里已有同名字体时以系统为准，
    包内字体只作兜底（保证换一台没装 Noto 的机器也能出字形）。
    """
    dirs = list(platform_font_dirs())
    packaged = packaged_font_dir()
    if packaged is not None:
        dirs.append(str(packaged))
    return "\n".join(f"  <dir>{d}</dir>" for d in dirs)


def effective_conf_text(base_text: str) -> str:
    """把平台目录写进模板。模板缺标记时原样返回（并由此在指纹里可辨识）。"""
    if PLATFORM_MARKER in base_text:
        return base_text.replace(PLATFORM_MARKER, _conf_dirs_block())
    return base_text


def build_env(root: str | Path | None = None) -> tuple[dict, dict]:
    """构造渲染子进程环境，返回 `(env, record)`。

    record 是证据而不是装饰：`ok=False` 表示**包内必需资源缺失**，
    调用方必须把本次渲染判为不可信（新错误码 RENDER.FONTCONFIG_RESOURCE_MISSING），
    不能因为 LibreOffice 仍能退出 0 就当渲染通过。
    """
    root_p = data_root(root)
    rec: dict = {
        "policy": ENV_POLICY_VERSION, "ok": True, "mode": "packaged",
        "package_root": str(package_root()),
        "base_conf": str(base_conf_path()),
        "base_conf_sha256": None, "effective_conf": None,
        "effective_conf_sha256": None, "cache_dir": None, "data_root": str(root_p),
        "platform_dirs": platform_font_dirs(), "platform": sys.platform,
        "override": None, "disabled": False, "error": None,
    }
    env = dict(os.environ)
    if os.environ.get(DISABLE_ENV):
        rec.update({"ok": False, "mode": "disabled-by-test",
                    "error": f"{DISABLE_ENV} 已设置：本次渲染没有字体配置（事故条件复现）"})
        for k in ("FONTCONFIG_FILE", "FONTCONFIG_PATH", "XDG_CACHE_HOME"):
            env.pop(k, None)
        return env, rec

    override = os.environ.get(OVERRIDE_ENV)
    if override:
        conf = Path(override)
        rec.update({"mode": "override", "override": str(conf)})
        if not conf.is_file():
            rec.update({"ok": False, "error": f"{OVERRIDE_ENV} 指向的配置不存在：{conf}"})
            return env, rec
        rec["effective_conf"] = str(conf)
        rec["effective_conf_sha256"] = sha256_text(conf.read_bytes())
        env["FONTCONFIG_FILE"] = str(conf)
        cache = root_p / ".fontconfig" / "cache-override"
        cache.mkdir(parents=True, exist_ok=True)
        env["XDG_CACHE_HOME"] = str(cache)
        rec["cache_dir"] = str(cache)
        return env, rec

    base = base_conf_path()
    if not base.is_file():
        rec.update({"ok": False, "mode": "missing",
                    "error": "发布包缺少 assets/fontconfig/fonts.conf；"
                             "渲染无法保证中文字形，按资源缺失拒绝。"})
        return env, rec

    base_bytes = base.read_bytes()
    rec["base_conf_sha256"] = sha256_text(base_bytes)
    text = effective_conf_text(base_bytes.decode("utf-8"))
    out_dir = root_p / ".fontconfig"
    out_dir.mkdir(parents=True, exist_ok=True)
    conf = out_dir / "fonts.conf"
    conf.write_text(text, encoding="utf-8")
    cache = out_dir / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    env["FONTCONFIG_FILE"] = str(conf)
    # XDG_CACHE_HOME 必须指向数据根：写进程序版本目录会污染构建身份。
    env["XDG_CACHE_HOME"] = str(cache)
    rec.update({"effective_conf": str(conf),
                "effective_conf_sha256": sha256_text(text.encode("utf-8")),
                "cache_dir": str(cache), "platform_marker_used": PLATFORM_MARKER in
                base_bytes.decode("utf-8", "replace")})
    return env, rec


def sha256_text(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def fingerprint(record: dict, renderer: dict | None = None,
                resolved_fonts: list[str] | None = None) -> dict:
    """缓存与证据用的环境指纹。

    只包含**影响渲染结果**的身份：有效配置内容、平台目录、缓存/profile 位置、
    渲染器版本，以及本次 PDF 里实际解析到的字体名（真实解析结果，不是猜测）。
    不含时间戳、随机目录名等易变字段——否则缓存永远失效。
    """
    fp = {
        "policy": record.get("policy"),
        "mode": record.get("mode"),
        "ok": bool(record.get("ok")),
        "effective_conf_sha256": record.get("effective_conf_sha256"),
        "base_conf_sha256": record.get("base_conf_sha256"),
        "platform": record.get("platform"),
        "platform_dirs": sorted(record.get("platform_dirs") or []),
        "renderer": (renderer or {}).get("version"),
        "resolved_fonts": sorted(set(resolved_fonts or [])),
    }
    blob = repr(sorted(fp.items())).encode("utf-8")
    fp["digest"] = hashlib.sha256(blob).hexdigest()
    return fp
