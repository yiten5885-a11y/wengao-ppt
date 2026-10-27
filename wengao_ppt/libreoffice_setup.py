"""wengao_ppt.libreoffice_setup —— LibreOffice 一键安装（降低本机部署门槛）。

背景：`render.real_render` 是 P1 必过检查，依赖本机 LibreOffice/soffice
（见 wengao_ppt/render.py）。要求用户自行去官网下载安装，对"开箱即用"
是个明显的门槛。本模块提供一个显式的、用户点击触发的安装流程：
从官方 documentfoundation.org 下载对应架构的 .dmg，校验官方公布的
SHA-256，挂载后把 LibreOffice.app 复制到 /Applications，再卸载镜像。

纪律：
- 只在用户显式触发安装时才下载/安装，绝不静默自动安装（与 env_probe.py
  "只探测不安装"的既有原则一致，这里的区别只是新增了一条用户主动点击
  才会走到的安装路径）。
- 只信任官方域名 download.documentfoundation.org 及其 Content-Location
  跳转到的镜像；下载完成后必须校验官方公布的 SHA-256，校验失败就拒绝安装。
- 安装目标固定为 /Applications/LibreOffice.app，不覆盖用户已有的同名
  第三方安装（先探测，已安装就直接报告，不重复下载）。
"""
from __future__ import annotations

import hashlib
import json
import platform
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from wengao_ppt import render as render_mod

_DOWNLOAD_ROOT = "https://download.documentfoundation.org/libreoffice/stable/"
_INSTALL_TARGET = Path("/Applications/LibreOffice.app")
# 下载/安装单次预算：官方 dmg 约 250-350MB，取保守上限防止异常重定向到超大文件。
_MAX_DOWNLOAD_BYTES = 600 * 1024 * 1024
_CHUNK = 1 << 20


class LibreOfficeSetupError(RuntimeError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _mac_arch() -> str:
    machine = platform.machine().lower()
    return "aarch64" if machine in ("arm64", "aarch64") else "x86_64"


def status() -> dict:
    """探测当前是否已安装；已安装就不建议也不允许重新下载安装。"""
    soffice, src = render_mod.find_soffice()
    if soffice:
        return {"installed": True, "soffice_path": soffice, "source": src,
                "version": render_mod.soffice_version(soffice)}
    return {"installed": False, "soffice_path": None, "source": None, "version": None,
            "install_target_occupied": _INSTALL_TARGET.exists()}


def _open_with_retry(opener, req: urllib.request.Request, *, timeout: int,
                     retries: int = 2):
    """官方站点/镜像偶发 TLS 握手超时；按既有探测代码的重试节奏重试几次。"""
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return opener.open(req, timeout=timeout)
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            if attempt < retries:
                time.sleep(1.0)
    raise LibreOfficeSetupError(
        "LO_SETUP.NETWORK_UNAVAILABLE",
        f"无法连接官方下载站点（{type(last).__name__}），请检查网络后重试。") from last


def _latest_stable_version(opener) -> str:
    with _open_with_retry(opener, urllib.request.Request(_DOWNLOAD_ROOT, method="GET"),
                          timeout=15) as r:
        html = r.read().decode("utf-8", "replace")
    versions = re.findall(r'href="(\d+\.\d+\.\d+)/"', html)
    if not versions:
        raise LibreOfficeSetupError("LO_SETUP.VERSION_LIST_UNAVAILABLE",
                                    "无法从官方站点取得可用版本列表。")
    def key(v: str) -> tuple[int, ...]:
        return tuple(int(p) for p in v.split("."))
    return sorted(versions, key=key)[-1]


def _dmg_info(opener, version: str, arch: str) -> dict:
    base = f"{_DOWNLOAD_ROOT}{version}/mac/{arch}/"
    filename = f"LibreOffice_{version}_MacOS_{arch}.dmg"
    meta_url = base + filename + ".meta4"
    with _open_with_retry(opener, urllib.request.Request(meta_url, method="GET"),
                          timeout=15) as r:
        meta = r.read().decode("utf-8", "replace")
    m = re.search(r'<hash type="sha-256">([0-9a-fA-F]{64})</hash>', meta)
    if not m:
        raise LibreOfficeSetupError("LO_SETUP.CHECKSUM_UNAVAILABLE",
                                    "官方未公布该版本的 SHA-256 校验值，拒绝安装。")
    return {"url": base + filename, "filename": filename, "sha256": m.group(1).lower(),
            "version": version}


def plan_install() -> dict:
    """只探测、不下载：返回这次安装会拉取的版本/文件名/体积/校验值，供用户确认。"""
    opener = urllib.request.build_opener()
    arch = _mac_arch()
    version = _latest_stable_version(opener)
    info = _dmg_info(opener, version, arch)
    req = urllib.request.Request(info["url"], method="HEAD")
    with _open_with_retry(opener, req, timeout=15) as r:
        size = int(r.headers.get("Content-Length") or 0)
        final_url = r.geturl()
    info.update(arch=arch, size_bytes=size, resolved_url=final_url)
    return info


def install(progress: Optional[Callable[[str, dict], None]] = None) -> dict:
    """执行下载 → 校验 → 挂载 → 复制 → 卸载。仅限用户显式触发。"""
    def report(stage: str, **detail):
        if progress:
            progress(stage, detail)

    existing = status()
    if existing["installed"]:
        report("skipped_already_installed", **existing)
        return {"ok": True, "already_installed": True, **existing}

    plan = plan_install()
    if plan["size_bytes"] <= 0 or plan["size_bytes"] > _MAX_DOWNLOAD_BYTES:
        raise LibreOfficeSetupError("LO_SETUP.SIZE_UNEXPECTED",
                                    f"安装包体积异常（{plan['size_bytes']} 字节），拒绝下载。")
    report("plan", **plan)

    opener = urllib.request.build_opener()
    tmp_dir = Path(tempfile.mkdtemp(prefix="wengao_lo_setup_"))
    dmg_path = tmp_dir / plan["filename"]
    try:
        report("downloading", url=plan["resolved_url"], size_bytes=plan["size_bytes"])
        hasher = hashlib.sha256()
        downloaded = 0
        req = urllib.request.Request(plan["resolved_url"], method="GET")
        with _open_with_retry(opener, req, timeout=30) as resp, open(dmg_path, "wb") as fh:
            while True:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                downloaded += len(chunk)
                if downloaded > _MAX_DOWNLOAD_BYTES:
                    raise LibreOfficeSetupError("LO_SETUP.DOWNLOAD_TOO_LARGE",
                                                "下载内容超过预期体积上限，已中止。")
                hasher.update(chunk)
                fh.write(chunk)
                report("download_progress", downloaded=downloaded, total=plan["size_bytes"])

        actual_sha256 = hasher.hexdigest()
        if actual_sha256 != plan["sha256"]:
            raise LibreOfficeSetupError(
                "LO_SETUP.CHECKSUM_MISMATCH",
                f"下载文件校验失败（期望 {plan['sha256']}，实际 {actual_sha256}），已拒绝安装。")
        report("checksum_ok", sha256=actual_sha256)

        mount_point = tmp_dir / "mount"
        mount_point.mkdir(parents=True, exist_ok=True)
        report("mounting")
        mount = subprocess.run(
            ["hdiutil", "attach", "-nobrowse", "-noautoopen", "-mountpoint",
             str(mount_point), str(dmg_path)],
            capture_output=True, text=True, timeout=120)
        if mount.returncode != 0:
            raise LibreOfficeSetupError("LO_SETUP.MOUNT_FAILED",
                                        f"挂载安装镜像失败：{mount.stderr.strip()[:300]}")
        try:
            app_src = mount_point / "LibreOffice.app"
            if not app_src.is_dir():
                raise LibreOfficeSetupError("LO_SETUP.APP_NOT_FOUND",
                                            "安装镜像内没有找到 LibreOffice.app。")
            report("copying", target=str(_INSTALL_TARGET))
            if _INSTALL_TARGET.exists():
                raise LibreOfficeSetupError("LO_SETUP.TARGET_EXISTS",
                                            f"{_INSTALL_TARGET} 已存在，拒绝覆盖。")
            copy = subprocess.run(
                ["ditto", str(app_src), str(_INSTALL_TARGET)],
                capture_output=True, text=True, timeout=180)
            if copy.returncode != 0:
                raise LibreOfficeSetupError("LO_SETUP.COPY_FAILED",
                                            f"复制安装包失败：{copy.stderr.strip()[:300]}")
        finally:
            subprocess.run(["hdiutil", "detach", str(mount_point), "-quiet"],
                           capture_output=True, text=True, timeout=60)

        report("verifying")
        soffice, src = render_mod.find_soffice()
        if not soffice:
            raise LibreOfficeSetupError("LO_SETUP.POST_INSTALL_PROBE_FAILED",
                                        "安装后仍未探测到可用的 soffice 可执行文件。")
        result = {"ok": True, "already_installed": False, "installed": True,
                  "soffice_path": soffice, "source": src,
                  "version": render_mod.soffice_version(soffice)}
        report("done", **result)
        return result
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
