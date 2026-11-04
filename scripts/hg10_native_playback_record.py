"""HG10 原生实播与重开核验（2026-11-04）。

对**验收导出原件**做原生放映连续录像（不改动文件字节，SHA 核验才有意义）：
1. 记录导出件 SHA-256（前）；
2. LibreOffice Impress 原生放映（soffice --show，GUI 实播）；
3. `screencapture -V` 连续录像 45s（HG10 要求 30–60s）；期间尝试 AppleScript
   翻页（权限被拒则如实登记"静态放映"，不伪造翻页）；
4. 结束放映（pkill soffice），从磁盘重新打开（headless 重解析全部字节）；
5. 核对 SHA-256（前 == 后），连同录像存证。

产物：reports/hg10-native-playback-20261104/{hg10-native-playback.mov, RECORD.json}
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path("/Users/huangyingteng/WorkBuddy/2026-10-12-00-54-41/wengao-ppt/build/wengao-visual-v13-material-fix")
SRC = REPO / "reports/install-cleanroom-20261104-r10b/first-task/export/cleanroom_first.wengao.pptx"
OUT = REPO / "reports/hg10-native-playback-20261104"
PLAY_FILE = Path("/tmp/hg10-native-play.pptx")
VIDEO = Path("/tmp/hg10-native-playback.mov")
SOFFICE = "/Applications/LibreOffice.app/Contents/MacOS/soffice"

RECORD_SECONDS = 45
RECORD_TARGET = 30  # HG10 下限


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run(cmd, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


log: list[str] = []


def note(msg: str):
    log.append(f"{time.strftime('%H:%M:%S')} {msg}")
    print(msg, flush=True)


OUT.mkdir(parents=True, exist_ok=True)
shutil.copy2(SRC, PLAY_FILE)
sha_before = sha256_file(PLAY_FILE)
note(f"播放对象：{SRC.name}（导出原件副本，字节一致）sha256={sha_before[:16]}…")

# 停掉可能残留的 soffice，保证从头启动
run(["pkill", "-f", "soffice"])
time.sleep(2)

# 原生放映（GUI）
note("启动 LibreOffice Impress 原生放映（soffice --show --norestore）…")
proc = subprocess.Popen([SOFFICE, "--norestore", "--show", str(PLAY_FILE)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# 等 Impress 真正成为前台（放映是全屏的，录到的应只有放映画面——
# 不录启动等待期的桌面，避免把用户屏幕上的其他内容收进证据）。
frontmost_ok = None
started = time.time()
while time.time() - started < 60:
    time.sleep(3)
    r = run(["osascript", "-e",
             'tell application "System Events" to get name of '
             "first application process whose frontmost is true"])
    if r.returncode != 0:
        frontmost_ok = False
        note(f"AppleScript 前台检测被系统拒绝（rc={r.returncode}）："
             "退化为固定等待 35s 后开始录制（启动等待期可能入镜，如实登记）。")
        time.sleep(35)
        break
    front = (r.stdout or "").strip()
    if front == "soffice":
        frontmost_ok = True
        note(f"Impress 已成为前台（{round(time.time() - started)}s），2s 后开始录制。")
        time.sleep(2)
        break
else:
    frontmost_ok = False
    note("60s 内未检测到 Impress 前台（放映可能未启动）：仍录制并如实登记。")

note(f"开始连续录像 {RECORD_SECONDS}s（screencapture -V）…")
cap = subprocess.Popen(["screencapture", "-V", str(RECORD_SECONDS), str(VIDEO)])
# 放映中尝试翻页（→ 键；权限被拒则如实记录静态放映）
advance_ok = None
for i in range(4):
    time.sleep(9)
    if advance_ok is not False:
        r = run(["osascript", "-e",
                 'tell application "System Events" to key code 124'])
        advance_ok = (r.returncode == 0)
        if not advance_ok:
            note(f"AppleScript 翻页被系统拒绝（rc={r.returncode}）："
                 "登记为静态放映，不伪造翻页。")
        else:
            note(f"翻页 {i + 1}")
cap.wait()
note("录像结束。")

note("结束放映并退出 LibreOffice…")
run(["pkill", "-f", "soffice"])
time.sleep(3)

# 从磁盘重新打开（headless 重新解析全部字节——"重开"的机器可核验口径）
note("从磁盘重新打开（headless 转档重解析）…")
reopen = run([SOFFICE, "--headless", "--norestore", "--convert-to", "pdf",
              "--outdir", "/tmp/hg10-reopen", str(PLAY_FILE)], timeout=180)
reopen_ok = (Path("/tmp/hg10-reopen/hg10-native-play.pdf").is_file()
             and reopen.returncode == 0)
note(f"重开解析：{'成功' if reopen_ok else '失败'} rc={reopen.returncode}")

sha_after = sha256_file(PLAY_FILE)
sha_same = (sha_before == sha_after)
note(f"SHA 核对：{'一致' if sha_same else '不一致'}（{sha_after[:16]}…）")

video_ok = VIDEO.is_file() and VIDEO.stat().st_size > 100_000
verdict = ("PASS" if (video_ok and sha_same and reopen_ok) else "FAIL")
if video_ok:
    shutil.copy2(VIDEO, OUT / "hg10-native-playback.mov")
record = {
    "schema": "wengao.hg10.native-playback.v1",
    "gate": "HG10 原生实播与重开核验",
    "date": time.strftime("%Y-%m-%d"),
    "played_file": str(SRC),
    "played_file_note": "验收导出原件（复制到 /tmp 播放，字节一致；原件不动）",
    "sha256_before": sha_before,
    "sha256_after": sha_after,
    "sha256_match": sha_same,
    "recording_seconds": RECORD_SECONDS,
    "recording_meets_30s_floor": RECORD_SECONDS >= RECORD_TARGET,
    "video_file": str(OUT / "hg10-native-playback.mov") if video_ok else None,
    "video_bytes": (OUT / "hg10-native-playback.mov").stat().st_size if video_ok else 0,
    "page_advance": ("applescript_keystroke" if advance_ok else
                     "static（翻页自动化被系统拒绝，如实登记）"),
    "frontmost_detection": ("soffice_verified" if frontmost_ok is True else
                            "unavailable（录制起点见 log）"),
    "reopen_reparse_ok": reopen_ok,
    "player": f"LibreOffice Impress（{Path(SOFFICE).name}）",
    "log": log,
    "verdict": verdict,
    "note": "本机无 PowerPoint/WPS；原生实播以 LibreOffice Impress 执行。"
            "Cua 点击翻页与 PowerPoint 本体的放映另列外部阻断登记。",
}
(OUT / "RECORD.json").write_text(json.dumps(record, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
print(f"verdict: {verdict} -> {OUT / 'RECORD.json'}", flush=True)
sys.exit(0 if verdict == "PASS" else 1)
