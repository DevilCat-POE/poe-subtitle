# -*- coding: utf-8 -*-
"""
updater.py — 從 GitHub 自動更新字幕工具

流程：
  1. 讀取 GitHub 上的 version.json，比較版號
  2. 下載該版本的 zip（找不到 tag 就下載 main 分支）
  3. 舊檔備份到 _backup\v舊版號\，再覆蓋新檔
  4. 個人檔案（個人詞庫、設定、API 金鑰、輸出字幕、.venv…）永遠不會被覆蓋

也可以單獨執行（GUI 壞掉時的備援）：
  python updater.py           檢查並更新
  python updater.py --check   只檢查
"""
import fnmatch
import hashlib
import io
import json
import shutil
import ssl
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

REPO = "DevilCat-POE/poe-subtitle"
BRANCH = "main"
BASE = Path(__file__).resolve().parent
UA = {"User-Agent": "poe-subtitle-updater"}

VERSION_URL = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/version.json"
ZIP_TAG_URL = "https://codeload.github.com/" + REPO + "/zip/refs/tags/v{ver}"
ZIP_MAIN_URL = f"https://codeload.github.com/{REPO}/zip/refs/heads/{BRANCH}"
REPO_URL = f"https://github.com/{REPO}"
ISSUES_URL = f"https://github.com/{REPO}/issues"

# 永遠不覆蓋、不刪除的檔案（使用者自己的東西）
PROTECTED = [
    "個人*", "使用者設定.json", "subtitle_ai_config.json", "*.srt", "*.review.txt",
    "交接文件*.md", "ffmpeg.exe", "ffprobe.exe", "desktop.ini",
    ".venv/*", "_backup/*", "字幕/*", "字幕輸出/*", "__pycache__/*", ".git/*",
]


def parse_ver(v):
    out = []
    for p in str(v).strip().lstrip("vV").split("."):
        num = "".join(ch for ch in p if ch.isdigit())
        out.append(int(num) if num else 0)
    return tuple(out)


def local_version():
    try:
        import poe_subtitle
        return poe_subtitle.VERSION
    except Exception:
        pass
    try:
        return json.loads((BASE / "version.json").read_text(encoding="utf-8"))["version"]
    except Exception:
        return "0.0.0"


def _ctx():
    try:
        return ssl.create_default_context()
    except Exception:
        return None


def _get(url, timeout=10, progress=None):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as r:
        total = int(r.headers.get("Content-Length") or 0)
        buf, got = io.BytesIO(), 0
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            buf.write(chunk)
            got += len(chunk)
            if progress:
                progress(got, total)
        return buf.getvalue()


def is_protected(rel):
    rel = rel.replace("\\", "/")
    name = rel.rsplit("/", 1)[-1]
    for pat in PROTECTED:
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(name, pat):
            return True
    return False


def check(timeout=8):
    """回傳 {"version", "notes", "newer": bool, "local"}；連不上網路就丟出例外。"""
    data = json.loads(_get(f"{VERSION_URL}?t={int(time.time())}", timeout=timeout).decode("utf-8-sig"))
    loc = local_version()
    data["local"] = loc
    data["newer"] = parse_ver(data.get("version", "0")) > parse_ver(loc)
    return data


def _sha(b):
    return hashlib.sha1(b).hexdigest()


def apply_update(info, progress=None, log=print):
    """
    下載並套用更新。progress(fraction 0~1, 說明文字)。
    回傳 {"changed": [...], "removed": [...], "backup": 路徑, "reqs_changed": bool}
    """
    ver = info["version"]
    say = progress or (lambda f, t: None)
    say(0.02, "下載新版中…")

    def dl_prog(got, total):
        say(0.05 + 0.6 * (got / total if total else 0.5), f"下載中 {got / 1048576:.1f} MB")

    try:
        blob = _get(ZIP_TAG_URL.format(ver=ver), timeout=60, progress=dl_prog)
    except Exception:
        blob = _get(ZIP_MAIN_URL, timeout=60, progress=dl_prog)

    say(0.7, "檢查檔案…")
    zf = zipfile.ZipFile(io.BytesIO(blob))
    names = [n for n in zf.namelist() if not n.endswith("/")]
    if not names:
        raise RuntimeError("下載的更新檔是空的")
    root = names[0].split("/", 1)[0] + "/"
    files = {}
    for n in names:
        rel = n[len(root):] if n.startswith(root) else n
        if not rel or rel.startswith(".github/") or rel in (".gitignore", ".gitattributes"):
            continue
        if is_protected(rel):
            continue
        files[rel] = zf.read(n)
    if "poe_subtitle.py" not in files or "poe_subtitle_gui.py" not in files:
        raise RuntimeError("更新檔不完整，已取消（你的檔案沒有變動）")

    # 新程式先編譯一次，有語法錯誤就不套用
    for rel, data in files.items():
        if rel.endswith(".py"):
            compile(data.decode("utf-8-sig"), rel, "exec")

    backup = BASE / "_backup" / f"v{local_version()}_{time.strftime('%Y%m%d-%H%M%S')}"
    changed, removed = [], []
    old_req = (BASE / "requirements.txt").read_bytes() if (BASE / "requirements.txt").exists() else b""

    todo = [(rel, data) for rel, data in files.items()
            if not ((BASE / rel).exists() and _sha((BASE / rel).read_bytes()) == _sha(data))]
    for i, (rel, data) in enumerate(todo):
        dst = BASE / rel
        say(0.72 + 0.25 * i / max(1, len(todo)), f"更新 {rel}")
        if dst.exists():
            (backup / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, backup / rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".new")
        tmp.write_bytes(data)
        tmp.replace(dst)
        changed.append(rel)

    # 官方要求移除的舊檔：移到備份資料夾，不直接刪除
    try:
        meta = json.loads(files.get("version.json", b"{}").decode("utf-8-sig"))
    except Exception:
        meta = {}
    for rel in meta.get("obsolete", []):
        p = BASE / rel
        if p.is_file() and not is_protected(rel):
            (backup / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(backup / rel))
            removed.append(rel)

    reqs_changed = files.get("requirements.txt", old_req) != old_req
    say(1.0, "更新完成")
    return {"changed": changed, "removed": removed, "backup": str(backup) if (changed or removed) else "",
            "reqs_changed": reqs_changed, "version": ver}


def _cli():
    print(f"字幕工具更新程式　目前版本 v{local_version()}")
    try:
        info = check()
    except Exception as e:
        print(f"無法連線到 GitHub：{e}")
        return 1
    if not info["newer"]:
        print(f"已經是最新版（GitHub 上是 v{info['version']}）。")
        return 0
    print(f"發現新版 v{info['version']}\n{info.get('notes', '')}\n")
    if "--check" in sys.argv:
        return 0
    res = apply_update(info, progress=lambda f, t: print(f"  {int(f * 100):3d}%  {t}"))
    print(f"\n已更新 {len(res['changed'])} 個檔案。舊檔備份在：{res['backup'] or '（無）'}")
    if res["reqs_changed"]:
        print("這次更新需要安裝新套件，請重新雙擊「一鍵安裝並啟動.bat」。")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
