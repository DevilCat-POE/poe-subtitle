# -*- coding: utf-8 -*-
"""流亡黯道 字幕工坊 — 圖形介面（流亡黯道風格、拖曳檔案、GitHub 自動更新）"""
import codecs
import csv
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox

BASE = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
os.environ["PATH"] = str(BASE) + os.pathsep + os.environ.get("PATH", "")   # ffmpeg.exe 放旁邊也找得到
sys.path.insert(0, str(BASE))

if sys.platform.startswith("win"):           # 高解析度螢幕不模糊（必須在建立視窗前）
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

try:                                          # 拖曳功能（pip install tkinterdnd2）
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _Root, HAS_DND = TkinterDnD.Tk, True
except Exception:
    DND_FILES, _Root, HAS_DND = None, tk.Tk, False

try:
    import poe_assets
except Exception:
    poe_assets = None

import updater  # noqa: E402


def read_version():
    try:
        m = re.search(r'VERSION\s*=\s*"([^"]+)"', (BASE / "poe_subtitle.py").read_text(encoding="utf-8"))
        return m.group(1)
    except Exception:
        return "?"


VERSION = read_version()

# ───────────── 配色（流亡黯道物品稀有度＋暗金） ─────────────
BG = "#0c0a08"
PANEL = "#14110d"
PANEL_HI = "#1c1712"
HEAD = "#1a140d"
EDGE = "#3d3020"
EDGE_IN = "#221b12"
GOLD = "#c9a96b"
GOLD_HI = "#f0d595"
GOLD_DIM = "#6b5636"
TEXT = "#d9cfbf"
DIM = "#837764"
FAINT = "#4d4436"
NORMAL = "#c8c8c8"
MAGIC = "#8888ff"
RARE = "#ffff77"
UNIQUE = "#af6025"
GEM = "#1ba29b"
CURRENCY = "#aa9e82"
RED = "#d23a2a"
UI = "Microsoft JhengHei UI"
SERIF = "Palatino Linotype"

SETTINGS_FILE = BASE / "使用者設定.json"
PERSONAL_CSV = BASE / "個人術語補充.csv"
PERSONAL_PROTECT = BASE / "個人術語保護.txt"
VOCAB_NAME = "火炬和流亡詞彙庫.csv"
CSV_FIELDS = ["遊戲", "類型", "分類", "繁體中文", "簡體中文", "英文", "誤聽寫法", "自動替換", "來源", "備註"]

MODELS = [("最準確（large-v3）", "large-v3", "第一次使用會下載約 3GB，之後不用。有 NVIDIA 顯示卡會快很多。"),
          ("穩定（large-v2）", "large-v2", "吵雜或背景音樂多時，比較不會一直重複同一句。"),
          ("快速（large-v3-turbo）", "large-v3-turbo", "速度約快 3 倍，準確度略低。"),
          ("平衡（medium）", "medium", "電腦較舊、沒有顯示卡時可以試試。"),
          ("最快（small）", "small", "很快，但錯字會明顯變多。")]
LANGS = [("中英混雜（建議）", "auto", "每一段自動判斷中文或英文，英文術語不會被硬翻成中文。"),
         ("只有中文", "zh", "整支影片都是中文時使用。"),
         ("只有英文", "en", "英文訪談、英文實況。")]
GAMES = [("流亡黯道 1 + 2", "poe", "兩代的術語都會用來校正。"),
         ("只有流亡黯道 1", "poe1", "只用一代術語，減少誤改。"),
         ("只有流亡黯道 2", "poe2", "只用二代術語，減少誤改。"),
         ("火炬之光", "torchlight", "火炬之光：無限 的術語。")]
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".ts", ".flv"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg"}
MEDIA_EXT = VIDEO_EXT | AUDIO_EXT | {".srt", ".json"}

TIPS = [
    "字幕比聲音早出現？把「字幕延後」調大一點；太晚就調小。",
    "專有名詞老是被聽錯？加進「個人詞庫」，下次會自動修正。",
    "把 SRT 丟進來只會做錯字校正，不會重新辨識，幾秒就好。",
    "第一次使用會下載約 3GB 的辨識模型，之後就不用再等了。",
    "有 NVIDIA 顯示卡會快很多；沒有也能用，只是比較慢。",
    "完成後記得看「待確認清單」，那些是工具沒把握的地方。",
    "辨識中可以按「停止」，不會留下半成品字幕。",
    "常常被斷開的詞，加進個人詞庫的「斷句保護」就不會再被拆開。",
]


def fmt_time(sec):
    sec = int(max(0, sec))
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02}:{s:02}" if h else f"{m:02}:{s:02}"


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


def open_path(p):
    p = str(p)
    if sys.platform.startswith("win"):
        os.startfile(p)  # noqa
    elif sys.platform == "darwin":
        subprocess.Popen(["open", p])
    else:
        subprocess.Popen(["xdg-open", p])


class Settings(dict):
    DEFAULTS = {"game": "poe", "model": "large-v3", "lang": "auto", "offset": 0.2, "cjk_width": 16,
                "vocab": "", "auto_update": True, "skip_version": "", "geometry": ""}

    def __init__(self):
        super().__init__(self.DEFAULTS)
        try:
            self.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
        if self.get("settings_version", 0) < 1:      # v0.1.34：每句上限預設 15→16（沒自己改過的才跟著改）
            if self.get("cjk_width") == 15:
                self["cjk_width"] = 16
            self["settings_version"] = 1

    def save(self):
        try:
            SETTINGS_FILE.write_text(json.dumps(self, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass


# ═══════════════════════════════ 主視窗 ═══════════════════════════════
class App(_Root):
    def __init__(self):
        super().__init__()
        self.title(f"流亡黯道 字幕工坊  v{VERSION}")
        self.configure(bg=BG)
        self.s = max(1.0, self.winfo_fpixels("1i") / 96.0)   # 介面縮放（125%、150% 螢幕）
        self.cfg = Settings()
        self.q = queue.Queue()
        self.proc = None
        self.tip_i = 0
        self.phase = 0.0
        self.hints = {}
        self.st = dict(file=None, kind="", size=0, dur=None, out_dir="", busy=False, stage="", detail="",
                       frac=0.0, total=None, t0=None, pos0=0.0, result=None, error=None, cancelled=False,
                       dragging=False, update=None, upd_state="", upd_text="", upd_frac=0.0, banner=True,
                       out_path="")
        if not self.cfg.get("vocab") or not Path(self.cfg["vocab"]).exists():
            self.cfg["vocab"] = str(BASE / VOCAB_NAME) if (BASE / VOCAB_NAME).exists() else ""

        self._fonts()
        self._icons()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(int(1000 * self.s), int(sw * 0.94)), min(int(900 * self.s), int(sh * 0.9))
        geo = self.cfg.get("geometry") or ""
        m = re.match(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$", geo)
        if not (m and 0 <= int(m.group(3)) < sw - 100 and 0 <= int(m.group(4)) < sh - 100):
            geo = f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 2 - 20)}"
        self.geometry(geo)
        self.minsize(min(int(900 * self.s), int(sw * 0.9)), min(int(620 * self.s), int(sh * 0.85)))

        self.cv = tk.Canvas(self, bg=BG, highlightthickness=0, bd=0)
        self.cv.pack(fill="both", expand=True)
        self._make_log()
        self.after(10, self._dark_titlebar)

        if HAS_DND:
            try:
                self.cv.drop_target_register(DND_FILES)
                self.cv.dnd_bind("<<Drop>>", self.on_drop)
                self.cv.dnd_bind("<<DropEnter>>", lambda e: self._drag(True, e))
                self.cv.dnd_bind("<<DropLeave>>", lambda e: self._drag(False, e))
                self.log.drop_target_register(DND_FILES)
                self.log.dnd_bind("<<Drop>>", self.on_drop)
            except Exception:
                pass

        self.cv.bind("<Configure>", self._on_resize)
        self.bind("<Control-o>", lambda e: self.pick_media())
        self.bind("<Return>", lambda e: self.start())
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(120, self.pump)
        self.after(8000, self._next_tip)
        self.after(60, self._animate)
        if self.cfg.get("auto_update", True):
            self.after(1500, lambda: self.check_update(silent=True))

        self.say("── 歡迎使用流亡黯道字幕工坊 ──\n", "head")
        self.say("1. 把影片、音訊或 SRT 拖進上方框框（或點框框選檔）\n"
                 "2. 確認右邊的設定，按「開始產生字幕」\n"
                 "3. 完成後會產生：字幕.srt（匯入剪映／PR／達文西）、字幕.review.txt（需要你聽原音確認的地方）\n", "dim")
        if not HAS_DND:
            self.say("（拖曳功能尚未安裝：重新雙擊「一鍵安裝並啟動.bat」即可，現在先用點選的方式選檔）\n", "warn")

    # ───────────── 基礎 ─────────────
    def _fonts(self):
        F = lambda fam, size, w="normal", sl="roman": tkfont.Font(self, family=fam, size=size, weight=w, slant=sl)  # noqa
        self.f = {"title": F(UI, 20, "bold"), "sub": F(SERIF, 10, "normal", "italic"), "h": F(UI, 11, "bold"),
                  "b": F(UI, 10, "bold"), "n": F(UI, 10), "s": F(UI, 9), "xs": F(UI, 8),
                  "big": F(UI, 13, "bold"), "item": F(UI, 12, "bold"), "pct": F(SERIF, 24, "bold"),
                  "go": F(UI, 14, "bold"), "log": F(UI, 10)}

    def _icons(self):
        self.img = {}
        if not poe_assets:
            return
        for k in ("ICON64", "ICON128", "STONE", "WATERMARK"):
            try:
                self.img[k] = tk.PhotoImage(master=self, data=getattr(poe_assets, k))
            except Exception:
                pass
        if "ICON128" in self.img:
            try:
                self.iconphoto(True, self.img["ICON128"])
            except Exception:
                pass
        self.img["HEADICON"] = self.img.get("ICON128" if self.s >= 1.75 else "ICON64")

    def _dark_titlebar(self):
        if not sys.platform.startswith("win"):
            return
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            v = ctypes.c_int(1)
            for attr in (20, 19):
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), 4) == 0:
                    break
            col = ctypes.c_int(0x00080A0C)        # 標題列顏色（Windows 11）
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(col), 4)
            col = ctypes.c_int(0x006BA9C9)        # 標題文字金色
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(col), 4)
        except Exception:
            pass

    # 畫圖小工具：以「設計單位」作畫，最後統一乘上縮放倍率
    def _k(self, tags, kw):
        if "width" in kw:
            kw["width"] = max(1, kw["width"] * self.s)
        t = tags if isinstance(tags, (tuple, list)) else (tags,)
        return ("fresh",) + tuple(t)

    def rect(self, x0, y0, x1, y1, tags="ui", **kw):
        return self.cv.create_rectangle(x0, y0, x1, y1, tags=self._k(tags, kw), **kw)

    def oval(self, x0, y0, x1, y1, tags="ui", **kw):
        return self.cv.create_oval(x0, y0, x1, y1, tags=self._k(tags, kw), **kw)

    def line(self, *pts, tags="ui", **kw):
        return self.cv.create_line(*pts, tags=self._k(tags, kw), **kw)

    def poly(self, pts, tags="ui", **kw):
        return self.cv.create_polygon(*pts, tags=self._k(tags, kw), **kw)

    def arc(self, x0, y0, x1, y1, tags="ui", **kw):
        return self.cv.create_arc(x0, y0, x1, y1, tags=self._k(tags, kw), **kw)

    def text(self, x, y, s, tags="ui", font="n", **kw):
        return self.cv.create_text(x, y, text=s, font=self.f[font], tags=self._k(tags, kw), **kw)

    def commit(self):
        self.cv.scale("fresh", 0, 0, self.s, self.s)
        self.cv.dtag("fresh", "fresh")

    def tw(self, s, font="n"):
        return self.f[font].measure(s) / self.s

    def fit(self, s, width, font="n", middle=False):
        if self.tw(s, font) <= width:
            return s
        lo, hi = 1, len(s)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            cand = (s[:mid // 2] + "…" + s[-(mid - mid // 2):]) if middle else s[:mid] + "…"
            lo, hi = (mid, hi) if self.tw(cand, font) <= width else (lo, mid - 1)
        return (s[:lo // 2] + "…" + s[-(lo - lo // 2):]) if middle else s[:lo] + "…"

    def diamond(self, x, y, r=4, fill=GOLD, outline="#3a2c16", tags="ui"):
        return self.poly([x, y - r, x + r, y, x, y + r, x - r, y], tags=tags, fill=fill, outline=outline)

    def divider(self, x0, x1, y, color=GOLD_DIM, tags="ui"):
        cx = (x0 + x1) / 2
        self.line(x0, y, cx - 10, y, fill=color, tags=tags)
        self.line(cx + 10, y, x1, y, fill=color, tags=tags)
        self.diamond(cx, y, 4, fill=color, outline=color, tags=tags)

    def panel(self, x0, y0, x1, y1, title=None, color=GOLD, tags="ui"):
        self.rect(x0, y0, x1, y1, tags=tags, fill=PANEL, outline=EDGE)
        self.rect(x0 + 3, y0 + 3, x1 - 3, y1 - 3, tags=tags, outline=EDGE_IN)
        if title:
            self.rect(x0 + 4, y0 + 4, x1 - 4, y0 + 34, tags=tags, fill=HEAD, outline="")
            self.line(x0 + 4, y0 + 34, x1 - 4, y0 + 34, tags=tags, fill=EDGE)
            cx, w = (x0 + x1) / 2, self.tw(title, "h")
            self.text(cx, y0 + 19, title, tags=tags, font="h", fill=color)
            for sgn in (-1, 1):
                a = cx + sgn * (w / 2 + 12)
                b = cx + sgn * (w / 2 + 70)
                self.line(a, y0 + 19, b, y0 + 19, tags=tags, fill=GOLD_DIM)
                self.diamond(a, y0 + 19, 3, fill=color, outline=color, tags=tags)
        for x, y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
            self.diamond(x, y, 5, tags=tags)

    def button(self, key, x0, y0, x1, y1, label, cmd, kind="normal", enabled=True, font=None, hint=None):
        tag = "btn_" + key
        pal = {"primary": ("#4b1a12", "#6a2618", GOLD, GOLD_HI, "#f3dfb3"),
               "danger": ("#3a0d0d", "#561414", "#a2463a", "#e0735f", "#f0c8c0"),
               "normal": ("#1d1812", "#2a2219", "#5c4a2e", GOLD, TEXT),
               "ghost": (PANEL, PANEL_HI, EDGE, GOLD_DIM, TEXT)}[kind]
        fill, hfill, edge, hedge, fg = pal
        if not enabled:
            fill, hfill, edge, hedge, fg = "#15120f", "#15120f", "#2c251b", "#2c251b", FAINT
        self.rect(x0, y0, x1, y1, tags=("ui", tag, tag + "_r"), fill=fill, outline=edge, width=1)
        if kind == "primary":
            self.rect(x0 + 3, y0 + 3, x1 - 3, y1 - 3, tags=("ui", tag), outline="#2c0f09" if enabled else "#1c1712")
            self.line(x0 + 4, y0 + 4, x1 - 4, y0 + 4, tags=("ui", tag), fill="#8a3a22" if enabled else "#1c1712")
            for x in (x0, x1):
                self.diamond(x, (y0 + y1) / 2, 5, fill=GOLD if enabled else FAINT, tags=("ui", tag))
        self.text((x0 + x1) / 2, (y0 + y1) / 2 + 1, label, tags=("ui", tag), font=font or "n", fill=fg)
        cv = self.cv

        def enter(e):
            if enabled:
                cv.itemconfigure(tag + "_r", fill=hfill, outline=hedge)
                cv.configure(cursor="hand2")
            if hint:
                self.show_tip(e, *hint)

        def leave(e):
            cv.itemconfigure(tag + "_r", fill=fill, outline=edge)
            cv.configure(cursor="")
            self.hide_tip()

        cv.tag_bind(tag, "<Enter>", enter)
        cv.tag_bind(tag, "<Leave>", leave)
        cv.tag_bind(tag, "<Motion>", (lambda e: self.move_tip(e)) if hint else (lambda e: None))
        cv.tag_bind(tag, "<ButtonRelease-1>", (lambda e: (self.hide_tip(), cmd(e))) if enabled else (lambda e: None))

    def link(self, key, x, y, label, cmd, color=DIM, hover=GOLD_HI, anchor="w", font="s", hint=None):
        tag = "lnk_" + key
        self.text(x, y, label, tags=("ui", tag), font=font, fill=color, anchor=anchor)
        cv = self.cv

        def enter(e):
            cv.itemconfigure(tag, fill=hover)
            cv.configure(cursor="hand2")
            if hint:
                self.show_tip(e, *hint)

        def leave(e):
            cv.itemconfigure(tag, fill=color)
            cv.configure(cursor="")
            self.hide_tip()

        cv.tag_bind(tag, "<Enter>", enter)
        cv.tag_bind(tag, "<Leave>", leave)
        cv.tag_bind(tag, "<Motion>", (lambda e: self.move_tip(e)) if hint else (lambda e: None))
        cv.tag_bind(tag, "<ButtonRelease-1>", lambda e: cmd(e))

    # ───────────── 物品說明框風格的提示 ─────────────
    def show_tip(self, e, title, body, color=GOLD):
        self._tip = (title, body, color)
        self.move_tip(e)

    def move_tip(self, e):
        if not getattr(self, "_tip", None):
            return
        self.cv.delete("tip")
        title, body, color = self._tip
        x, y = e.x / self.s + 16, e.y / self.s + 18
        lines = body.split("\n")
        w = max([self.tw(title, "b")] + [self.tw(t, "s") for t in lines]) + 28
        h = 34 + 18 * len(lines) + 10
        W, H = self.cv.winfo_width() / self.s, self.cv.winfo_height() / self.s
        if x + w > W - 6:
            x = max(6, e.x / self.s - w - 10)
        if y + h > H - 6:
            y = max(6, e.y / self.s - h - 10)
        self.rect(x, y, x + w, y + h, tags="tip", fill="#0a0806", outline=GOLD_DIM)
        self.rect(x + 1, y + 1, x + w - 1, y + 27, tags="tip", fill="#1f170d", outline="")
        self.text(x + w / 2, y + 14, title, tags="tip", font="b", fill=color)
        self.divider(x + 10, x + w - 10, y + 31, tags="tip")
        for i, t in enumerate(lines):
            self.text(x + w / 2, y + 44 + 18 * i, t.lstrip("+"), tags="tip", font="s", fill=MAGIC if t.startswith("+") else TEXT)
        self.commit()
        self.cv.tag_raise("tip")

    def hide_tip(self, *_):
        self._tip = None
        self.cv.delete("tip")

    # ───────────── 版面 ─────────────
    def _on_resize(self, e=None):
        self._draw_bg()
        self.render()

    def _draw_bg(self):
        cv = self.cv
        cv.delete("bg")
        W, H = cv.winfo_width(), cv.winfo_height()
        st = self.img.get("STONE")
        if st:
            tw, th = st.width(), st.height()
            for x in range(0, W, tw):
                for y in range(0, H, th):
                    cv.create_image(x, y, image=st, anchor="nw", tags="bg")
        wm = self.img.get("WATERMARK")
        if wm:
            cv.create_image(W * 0.5, H * 0.56, image=wm, anchor="center", tags="bg")
        cv.tag_lower("bg")

    def render(self):
        cv = self.cv
        W, H = cv.winfo_width() / self.s, cv.winfo_height() / self.s
        if W < 100 or H < 100:
            return
        cv.delete("ui")
        self.hide_tip()
        st, P = self.st, 22
        self._draw_header(W)
        top = 96
        if st["update"] and st["banner"] or st["upd_state"] in ("busy", "error", "done"):
            self._draw_banner(P, top, W - P)
            top += 52
        split = max(520, W * 0.6)
        lx0, lx1, rx0, rx1 = P, split - 8, split + 8, W - P
        compact = H < 800                       # 小螢幕或 150% 縮放：紀錄區移到右邊
        avail = H - top - 34
        fileh = 162 if avail > 560 else 134
        btnh = 54 if avail > 560 else 46
        rowh = 38 if not compact else max(30, min(38, (avail - fileh - 14 - 48 - 14 - btnh) / 6))
        y = top
        self._draw_file_card(lx0, y, lx1, y + fileh)
        y += fileh + 14
        rows_h = 34 + 6 * rowh + 14
        self._draw_settings(lx0, y, lx1, y + rows_h, rowh)
        y += rows_h + 14
        if st["busy"]:
            self.button("go", lx0, y, lx1, y + btnh, "■  停止辨識", self.cancel, kind="danger", font="go")
        else:
            self.button("go", lx0, y, lx1, y + btnh, "開始產生字幕", self.start, kind="primary", font="go",
                        enabled=bool(st["file"]))
        bottom = y + btnh
        if compact:
            ph = 352
            self._draw_progress(rx0, top, rx1, top + ph, compact=True)
            lgx0, lgy0, lgx1, lgy1 = rx0, top + ph + 14, rx1, H - 34
        else:
            self._draw_progress(rx0, top, rx1, bottom)
            lgx0, lgy0, lgx1, lgy1 = lx0, bottom + 16, rx1, H - 34
        self.panel(lgx0, lgy0, lgx1, lgy1, "進度紀錄")
        self.link("clearlog", lgx1 - 14, lgy0 + 19, "清除", lambda e: self.log.delete("1.0", "end"), anchor="e")
        self.link("copylog", lgx1 - 52, lgy0 + 19, "複製全部", self.copy_log, anchor="e")
        cv.coords(self.log_win, (lgx0 + 12) * self.s, (lgy0 + 42) * self.s)
        cv.itemconfigure(self.log_win, width=max(10, (lgx1 - lgx0 - 24) * self.s),
                         height=max(10, (lgy1 - lgy0 - 52) * self.s))
        self._draw_footer(P, H - 17, W - P)
        self.commit()
        cv.tag_raise(self.log_win)

    def _draw_header(self, W):
        P = 22
        ic = self.img.get("HEADICON")
        if ic:
            self.cv.create_image((P + 32) * self.s, 48 * self.s, image=ic, tags="ui")
            tx = P + 74
        else:
            tx = P
        self.text(tx, 38, "流亡黯道  字幕工坊", font="title", fill=GOLD, anchor="w")
        self.text(tx + 2, 68, "Path of Exile  ·  繁體中文字幕產生器", font="sub", fill=DIM, anchor="w")
        st = self.st
        x = W - P
        self.text(x, 36, f"v{VERSION}", font="b", fill=CURRENCY, anchor="e")
        if st["update"]:
            lbl, col = f"◆ 有新版 v{st['update']['version']}", UNIQUE
            cmd = lambda e: (st.update(banner=True), self.render())  # noqa
        elif st["upd_state"] == "checking":
            lbl, col, cmd = "檢查更新中…", DIM, (lambda e: None)
        elif st["upd_state"] == "latest":
            lbl, col, cmd = "✓ 已是最新版", GEM, (lambda e: self.check_update())
        elif st["upd_state"] == "offline":
            lbl, col, cmd = "無法連線檢查更新", FAINT, (lambda e: self.check_update())
        else:
            lbl, col, cmd = "檢查更新", DIM, (lambda e: self.check_update())
        self.link("upd", x, 62, lbl, cmd, color=col, anchor="e",
                  hint=("自動更新", "從 GitHub 下載最新版\n你的個人詞庫、設定、字幕檔不會被覆蓋"))
        self.divider(P, W - P, 88)

    def _draw_banner(self, x0, y0, x1):
        st = self.st
        y1 = y0 + 42
        self.rect(x0, y0, x1, y1, fill="#1d130a", outline=UNIQUE)
        self.diamond(x0, (y0 + y1) / 2, 5, fill=UNIQUE, outline=UNIQUE)
        self.diamond(x1, (y0 + y1) / 2, 5, fill=UNIQUE, outline=UNIQUE)
        cy = (y0 + y1) / 2
        if st["upd_state"] == "busy":
            self.text(x0 + 18, cy, f"正在更新…　{st['upd_text']}", font="b", fill=GOLD_HI, anchor="w")
            bx0, bx1 = x1 - 300, x1 - 18
            self.rect(bx0, cy - 6, bx1, cy + 6, fill="#0a0806", outline=GOLD_DIM)
            self.rect(bx0 + 1, cy - 5, bx0 + 1 + (bx1 - bx0 - 2) * st["upd_frac"], cy + 5, fill=UNIQUE, outline="")
            return
        if st["upd_state"] == "error":
            self.text(x0 + 18, cy, self.fit("更新失敗：" + st["upd_text"], x1 - x0 - 330), font="b", fill=RED, anchor="w")
            self.button("upd_web", x1 - 300, cy - 14, x1 - 150, cy + 14, "到 GitHub 手動下載",
                        lambda e: webbrowser.open(updater.REPO_URL), font="s")
            self.button("upd_x", x1 - 140, cy - 14, x1 - 18, cy + 14, "關閉",
                        lambda e: (st.update(upd_state="", banner=False), self.render()), kind="ghost", font="s")
            return
        if st["upd_state"] == "done":
            self.text(x0 + 18, cy, st["upd_text"], font="b", fill=GEM, anchor="w")
            return
        info = st["update"]
        notes = (info.get("notes") or "").strip().splitlines()
        head = f"◆ 新版本 v{info['version']} 已推出"
        self.text(x0 + 18, cy, head, font="b", fill=UNIQUE, anchor="w")
        if notes:
            nx = x0 + 30 + self.tw(head, "b")
            self.text(nx, cy, self.fit(notes[0], x1 - nx - 330, "s"), font="s", fill=TEXT, anchor="w")
        self.button("upd_go", x1 - 312, cy - 15, x1 - 190, cy + 15, "立即更新", self.do_update, kind="primary", font="b",
                    hint=(f"更新到 v{info['version']}", "\n".join(notes[:8]) or "（沒有說明）"))
        self.button("upd_skip", x1 - 180, cy - 15, x1 - 92, cy + 15, "略過此版", self.skip_update, kind="ghost", font="s")
        self.button("upd_later", x1 - 84, cy - 15, x1 - 18, cy + 15, "稍後", lambda e: (st.update(banner=False), self.render()),
                    kind="ghost", font="s")

    def _draw_file_card(self, x0, y0, x1, y1):
        st = self.st
        if not st["file"]:
            self.panel(x0, y0, x1, y1, "① 選擇影片")
            zx0, zy0, zx1, zy1 = x0 + 16, y0 + 46, x1 - 16, y1 - 14
            col = GOLD_HI if st["dragging"] else GOLD_DIM
            self.rect(zx0, zy0, zx1, zy1, tags=("ui", "drop", "drop_r"), fill=PANEL_HI if st["dragging"] else PANEL,
                      outline=col, dash=(6, 4), width=1)
            cx, cy = (zx0 + zx1) / 2, (zy0 + zy1) / 2
            msg = "放開滑鼠，就是這個檔案！" if st["dragging"] else (
                "把影片、音訊或 SRT 拖曳到這裡" if HAS_DND else "點這裡選擇影片、音訊或 SRT")
            self.text(cx, cy - 14, msg, tags=("ui", "drop"), font="big", fill=GOLD if not st["dragging"] else GOLD_HI)
            self.text(cx, cy + 14, ("或點這裡選擇檔案　·　" if HAS_DND else "") + "MP4 · MKV · MOV · MP3 · WAV · M4A · SRT",
                      tags=("ui", "drop"), font="s", fill=DIM)
            self.cv.tag_bind("drop", "<Enter>", lambda e: (self.cv.itemconfigure("drop_r", outline=GOLD), self.cv.configure(cursor="hand2")))
            self.cv.tag_bind("drop", "<Leave>", lambda e: (self.cv.itemconfigure("drop_r", outline=col), self.cv.configure(cursor="")))
            self.cv.tag_bind("drop", "<ButtonRelease-1>", lambda e: self.pick_media())
            return
        # 已選檔：用「物品說明框」呈現
        kind = st["kind"]
        rarity, base = {"video": (UNIQUE, "影片"), "audio": (RARE, "音訊"), "srt": (MAGIC, "既有字幕（只做校正）"),
                        "json": (NORMAL, "原文 JSON（重跑斷句）")}.get(kind, (NORMAL, "檔案"))
        self.panel(x0, y0, x1, y1)
        self.rect(x0 + 4, y0 + 4, x1 - 4, y0 + 52, fill="#1f150b" if kind == "video" else HEAD, outline="")
        name = Path(st["file"]).name
        self.text((x0 + x1) / 2, y0 + 20, self.fit(name, x1 - x0 - 60, "item", middle=True), font="item", fill=rarity)
        ext = Path(st["file"]).suffix.upper().lstrip(".")
        self.text((x0 + x1) / 2, y0 + 41, f"{base} · {ext}", font="s", fill=rarity)
        self.divider(x0 + 30, x1 - 30, y0 + 60, color=rarity if kind != "video" else GOLD_DIM)
        props = []
        if st["dur"]:
            props.append(("長度", fmt_time(st["dur"])))
        props.append(("大小", fmt_size(st["size"])))
        ytxt = y0 + 80
        s = "　　".join(f"{k}：{v}" for k, v in props)
        self.text((x0 + x1) / 2, ytxt, s, font="n", fill=TEXT)
        out = st["out_dir"]
        self.text(x0 + 24, ytxt + 28, "字幕存到：", font="s", fill=DIM, anchor="w")
        self.text(x0 + 24 + self.tw("字幕存到：", "s"), ytxt + 28, self.fit(out, x1 - x0 - 160, "s", middle=True),
                  font="s", fill=CURRENCY, anchor="w")
        self.link("outdir", x1 - 22, ytxt + 28, "變更", lambda e: self.pick_out(), color=GOLD, anchor="e")
        if kind == "srt":
            note = "SRT 只會校正錯字、整理標點，保留原本的時間軸與斷句"
        elif kind == "json":
            note = "不重新辨識，直接用新的詞庫與斷句重做一次字幕"
        else:
            note = "會先辨識語音，再用詞庫校正術語、自動斷句"
        if y1 - y0 >= 150:
            self.text((x0 + x1) / 2, y1 - 22, note, font="xs", fill=FAINT if kind in ("video", "audio") else MAGIC)
        if not st["busy"]:
            self.link("change", x1 - 14, y0 + 20, "更換 ✕", lambda e: self.clear_file(), color=DIM, anchor="e", font="xs")

    def _draw_settings(self, x0, y0, x1, y1, rowh=38):
        self.panel(x0, y0, x1, y1, "② 字幕設定")
        c = self.cfg
        label_x, val_x0, val_x1 = x0 + 22, x0 + 150, x1 - 18
        rows = [
            ("game", "遊戲", self._name(GAMES, c["game"]), ("遊戲", "決定用哪些遊戲的術語來修正錯字")),
            ("model", "辨識模型", self._name(MODELS, c["model"]), ("辨識模型", self._hint(MODELS, c["model"]))),
            ("lang", "說話語言", self._name(LANGS, c["lang"]), ("說話語言", self._hint(LANGS, c["lang"]))),
            ("offset", "字幕延後", f"{c['offset']:+.2f} 秒", ("字幕延後",
                "語音辨識的時間通常比聲音早 0.1～0.3 秒\n字幕太早出現 → 調大　太晚 → 調小\n+預設 +0.20 秒")),
            ("cjk", "每句最多字數", f"{int(c['cjk_width'])} 字", ("每句最多字數",
                "中文字幕一句最多幾個字（單行）\n依親手字幕統計：平均 10 字、九成在 15 字內\n+預設 16 字")),
            ("vocab", "詞庫", Path(c["vocab"]).name if c["vocab"] else "（未選擇）", ("詞庫",
                "遊戲術語、常見錯字對照表\n同資料夾的「…術語補充.csv」也會一起讀取\n+你的個人詞庫也會自動套用")),
        ]
        dis = self.st["busy"]
        for i, (key, lab, val, hint) in enumerate(rows):
            y = y0 + 34 + 12 + i * rowh + (rowh - 38) / 2
            cy = y + 14
            self.link("lab_" + key, label_x, cy, lab, lambda e: None, color=TEXT, hover=GOLD_HI, font="n", hint=hint)
            if key in ("offset", "cjk"):
                bx = val_x0
                self.button(key + "_m", bx, y, bx + 32, y + 28, "−", lambda e, k=key: self.step(k, -1), enabled=not dis, font="b")
                self.rect(bx + 38, y, bx + 148, y + 28, fill="#0f0d0a", outline=EDGE)
                self.text(bx + 93, cy, val, font="b", fill=MAGIC)
                self.button(key + "_p", bx + 154, y, bx + 186, y + 28, "+", lambda e, k=key: self.step(k, +1), enabled=not dis, font="b")
                if dis:
                    pass
                elif key == "offset":
                    self.link("off_reset", bx + 200, cy, "還原預設", lambda e: self.set_cfg(offset=0.2), font="xs")
                else:
                    self.link("cjk_reset", bx + 200, cy, "還原預設", lambda e: self.set_cfg(cjk_width=16), font="xs")
            elif key == "vocab":
                self.button("sel_vocab", val_x0, y, val_x1 - 120, y + 28,
                            self.fit(val, val_x1 - 120 - val_x0 - 20, "n"), lambda e: self.pick_vocab(), enabled=not dis)
                self.button("personal", val_x1 - 112, y, val_x1, y + 28, "個人詞庫", lambda e: self.edit_personal(),
                            hint=("個人詞庫", "加入你自己常被聽錯的詞、你的名字、台詞\n自動更新不會覆蓋"))
            else:
                self.button("sel_" + key, val_x0, y, val_x1, y + 28, "", lambda e, k=key: self.popup(e, k), enabled=not dis)
                self.text(val_x0 + 12, cy, val, tags=("ui", "btn_sel_" + key), font="n", fill=TEXT if not dis else FAINT, anchor="w")
                self.text(val_x1 - 14, cy, "▼", tags=("ui", "btn_sel_" + key), font="xs", fill=GOLD if not dis else FAINT, anchor="e")

    def _orb(self, cx, cy, r, frac, palette, tags):
        back, front, glow = palette
        self.oval(cx - r - 9, cy - r - 9, cx + r + 9, cy + r + 9, tags=tags, fill="#0d0a06", outline="#3d2f19", width=1)
        self.oval(cx - r - 5, cy - r - 5, cx + r + 5, cy + r + 5, tags=tags, fill="", outline=GOLD_DIM, width=4)
        self.oval(cx - r, cy - r, cx + r, cy + r, tags=tags, fill="#07070b", outline="#000000")
        frac = max(0.0, min(1.0, frac))
        if frac > 0.002:
            for layer, (col, amp, sp, ph) in enumerate(((back, 4.0, 1.3, 1.7), (front, 3.0, 1.0, 0.0))):
                lvl = cy + r - 2 * r * frac + (2 if layer == 0 else 0)
                pts = []
                for k in range(96):
                    a = 2 * math.pi * k / 96
                    x, y = cx + r * math.cos(a), cy + r * math.sin(a)
                    dx = x - cx
                    bot = cy + math.sqrt(max(0.0, r * r - dx * dx))
                    wave = lvl + amp * math.sin(dx / r * 3.2 + self.phase * sp + ph) * (1 if frac < 0.995 else 0)
                    pts += [x, min(max(y, wave), bot)]
                self.poly(pts, tags=tags, fill=col, outline="")
        self.arc(cx - r + 10, cy - r + 8, cx + r - 10, cy + r - 12, tags=tags, start=100, extent=70, style="arc",
                 outline=glow, width=3)
        self.arc(cx - r + 4, cy - r + 4, cx + r - 4, cy + r - 4, tags=tags, start=200, extent=140, style="arc",
                 outline="#000000", width=4)
        for k in range(4):
            a = math.pi / 2 * k
            self.diamond(cx + math.cos(a) * (r + 7), cy + math.sin(a) * (r + 7), 6, tags=tags)

    def _draw_progress(self, x0, y0, x1, y1, compact=False):
        st = self.st
        self.panel(x0, y0, x1, y1, "③ 進度")
        cx = (x0 + x1) / 2
        r = 56 if compact else max(50, min(84, (x1 - x0) / 2 - 40, (y1 - y0 - 300) / 2))
        cy = y0 + 50 + r + 6
        self._orb_geo = (cx, cy, r)
        self._draw_orb()
        y = cy + r + 32
        tw = x1 - x0 - 30
        if st["error"]:
            self.text(cx, y, "發生錯誤", font="big", fill=RED)
            self.text(cx, y + 22, st["error"], font="s", fill=TEXT, width=tw, justify="center", anchor="n")
            self.button("errlog", cx - 70, y1 - 44, cx + 70, y1 - 14, "複製錯誤訊息", self.copy_log, font="s")
            return
        if st["result"]:
            n, fixed, todo = st["result"]
            self.text(cx, y, "字幕完成", font="big", fill=UNIQUE)
            self.divider(x0 + 40, x1 - 40, y + 19, color=GOLD_DIM)
            mods = [(f"產生 {n} 句字幕", MAGIC), (f"自動修正 {fixed} 處錯字", MAGIC),
                    (f"待你確認 {todo} 項" if todo else "沒有需要確認的地方", RARE if todo else GEM)]
            for i, (t, col) in enumerate(mods):
                self.text(cx, y + 36 + i * 20, t, font="n", fill=col)
            by = y1 - 44
            w = (x1 - x0 - 44) / 2
            self.button("openout", x0 + 16, by, x0 + 16 + w, by + 30, "開啟資料夾", lambda e: self.open_out(), font="b")
            self.button("openrev", x1 - 16 - w, by, x1 - 16, by + 30, "看待確認清單", lambda e: self.open_review(),
                        enabled=bool(st["out_path"]), font="b",
                        hint=("待確認清單", "工具沒把握的詞、疑似重複或漏字的地方\n建議對照影片聽一下原音"))
            return
        if st["busy"]:
            self.text(cx, y, st["stage"] or "處理中…", font="b", fill=GOLD)
            self.text(cx, y + 22, st["detail"], font="s", fill=DIM, width=tw, justify="center", anchor="n")
        else:
            self.text(cx, y, "準備就緒" if st["file"] else "等待檔案", font="big", fill=GOLD if st["file"] else DIM)
            self.text(cx, y + 24, "按下「開始產生字幕」" if st["file"] else "先把影片拖進左邊的框框", font="s", fill=DIM)
        if compact:
            return
        # 流程清單：目前進行到哪一步
        if st["kind"] in ("srt",):
            steps = ["讀取字幕", "詞庫校正錯字", "整理與去重", "輸出字幕"]
        elif st["kind"] == "json":
            steps = ["讀取原文", "詞庫校正錯字", "自動斷句", "輸出字幕"]
        else:
            steps = ["辨識語音", "詞庫校正錯字", "自動斷句", "輸出字幕"]
        cur = -1
        if st["busy"]:
            cur = 0 if (st["kind"] in ("video", "audio") and st["frac"] < 0.97) else 1
        sy = y + 74
        if y1 - 20 - sy < 4 * 26:
            return
        self.divider(x0 + 40, x1 - 40, sy - 16, color=EDGE)
        lx = cx - 70
        for i, name in enumerate(steps):
            yy = sy + i * 26
            if cur > i:
                col, mark = GEM, "✓"
            elif cur == i:
                col, mark = GOLD_HI, "◆"
            else:
                col, mark = FAINT, "◇"
            self.text(lx, yy, mark, font="n", fill=col, anchor="center")
            self.text(lx + 16, yy, name, font="n", fill=col if cur >= i else DIM if not st["busy"] else FAINT, anchor="w")

    def _draw_orb(self):
        if not hasattr(self, "_orb_geo"):
            return
        st = self.st
        self.cv.delete("orb")
        cx, cy, r = self._orb_geo
        if st["error"]:
            pal, frac = ("#4a0b0b", "#9c1c1c", "#e46a5a"), 1.0
        elif st["result"]:
            pal, frac = ("#6b4a14", "#b8862c", "#ffe2a0"), 1.0
        elif st["busy"]:
            pal, frac = ("#14246b", "#2d52c8", "#8fb0ff"), st["frac"]
        else:
            pal, frac = ("#14246b", "#2d52c8", "#8fb0ff"), 0.0
        self._orb(cx, cy, r, frac, pal, ("ui", "orb"))
        if st["busy"] or st["result"] or st["error"]:
            pct = "!" if st["error"] else ("✓" if st["result"] else (f"{int(st['frac'] * 100)}%" if st["total"] else "…"))
            self.text(cx + 1, cy + 2, pct, tags=("ui", "orb"), font="pct", fill="#000000")
            self.text(cx, cy, pct, tags=("ui", "orb"), font="pct", fill="#f5ecd8")

    def _draw_footer(self, x0, y, x1):
        self.text(x0, y, "提示　" + TIPS[self.tip_i % len(TIPS)], tags=("ui", "tiptext"), font="s", fill=CURRENCY, anchor="w")
        x = x1
        items = [("issue", "回報問題", lambda e: webbrowser.open(updater.ISSUES_URL)),
                 ("gh", "GitHub", lambda e: webbrowser.open(updater.REPO_URL)),
                 ("log", "更新紀錄", lambda e: open_path(BASE / "更新紀錄.txt")),
                 ("au", "自動更新：" + ("開" if self.cfg.get("auto_update", True) else "關"), self.toggle_auto)]
        for i, (key, lab, cmd) in enumerate(items):
            if i:
                self.text(x - 9, y, "·", font="s", fill=FAINT)
                x -= 18
            self.link("f_" + key, x, y, lab, cmd, anchor="e")
            x -= self.tw(lab, "s")

    # ───────────── 記錄區 ─────────────
    def _make_log(self):
        self.log = tk.Text(self, bg="#0a0907", fg=TEXT, insertbackground=GOLD, relief="flat", bd=0, wrap="word",
                           font=self.f["log"], highlightthickness=0, padx=8, pady=6, spacing1=1, spacing3=1,
                           selectbackground="#4a3a22", selectforeground="#ffffff", cursor="arrow")
        for tag, col in (("head", GOLD), ("dim", DIM), ("speech", NORMAL), ("time", FAINT), ("warn", RARE),
                         ("err", RED), ("done", UNIQUE), ("magic", MAGIC), ("gem", GEM)):
            self.log.tag_configure(tag, foreground=col)
        self.log.bind("<Key>", lambda e: None if (e.state & 4 and e.keysym.lower() in ("c", "a")) else "break")
        self.log_win = self.cv.create_window(0, 0, window=self.log, anchor="nw", tags="logwin")

    def say(self, s, tag=None):
        s = s.replace("\r\n", "\n")
        parts = s.split("\r")
        for i, p in enumerate(parts):
            if i > 0:                         # \r：覆蓋目前這一行（下載進度）
                self.log.delete("end-1c linestart", "end-1c")
            if not p:
                continue
            for seg in re.split(r"(?<=\n)", p):
                if not seg:
                    continue
                t = tag or self._classify(seg)
                m = re.match(r"^(\[\d+:\d+(?::\d+)?\])(.*)$", seg, re.S) if t == "speech" else None
                if m:
                    self.log.insert("end", m.group(1), "time")
                    self.log.insert("end", m.group(2), "speech")
                else:
                    self.log.insert("end", seg, t)
        if int(self.log.index("end-1c").split(".")[0]) > 6000:
            self.log.delete("1.0", "1500.0")
        self.log.see("end")

    @staticmethod
    def _classify(line):
        if re.match(r"^\[\d+:\d+", line):
            return "warn" if "重複" in line else "speech"
        if re.search(r"錯誤|失敗|Error|Traceback|找不到|缺少", line):
            return "err"
        if re.search(r"提醒|改用 CPU|顯示卡加速無法", line):
            return "warn"
        if line.startswith("完成") or line.startswith("輸出資料夾"):
            return "done"
        if line.startswith("──"):
            return "head"
        return "dim"

    def copy_log(self, *_):
        self.clipboard_clear()
        self.clipboard_append(self.log.get("1.0", "end-1c"))
        self.say("（已複製進度紀錄，可以直接貼給別人看）\n", "gem")

    # ───────────── 選檔／拖曳 ─────────────
    def _drag(self, on, e=None):
        self.st["dragging"] = on
        self.render()
        return getattr(e, "action", None)

    def on_drop(self, e):
        self.st["dragging"] = False
        try:
            for p in self.tk.splitlist(e.data):
                self.route_path(p)
        except Exception as ex:
            self.say(f"拖曳失敗：{ex}\n", "err")
        self.render()
        return e.action

    def route_path(self, p):
        path = Path(p)
        if self.st["busy"]:
            self.say("（正在產生字幕，完成後再放新檔案）\n", "warn")
        elif path.is_dir():
            if self.st["file"]:
                self.st["out_dir"] = str(path)
        elif path.suffix.lower() == ".csv":
            self.set_cfg(vocab=str(path))
        elif path.suffix.lower() in MEDIA_EXT:
            self.set_file(path)
        else:
            self.say(f"（不支援的檔案類型：{path.name}）\n", "warn")

    def set_file(self, path):
        ext = path.suffix.lower()
        kind = "video" if ext in VIDEO_EXT else "audio" if ext in AUDIO_EXT else ext.lstrip(".")
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        self.st.update(file=str(path), kind=kind, size=size, dur=None, result=None, error=None, out_path="",
                       out_dir=str(path.parent / "字幕輸出"))
        self.render()
        if kind in ("video", "audio"):
            threading.Thread(target=self._probe, args=(str(path),), daemon=True).start()

    def _probe(self, path):
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                                 capture_output=True, text=True, timeout=20, creationflags=flags).stdout
            self.q.put(("dur", path, float(out.strip())))
        except Exception:
            pass

    def clear_file(self):
        self.st.update(file=None, kind="", result=None, error=None)
        self.render()

    def pick_media(self):
        if self.st["busy"]:
            return
        p = filedialog.askopenfilename(title="選擇影片或音訊", filetypes=[
            ("影音檔", "*.mp4 *.mov *.mkv *.avi *.webm *.ts *.flv *.mp3 *.wav *.m4a *.flac *.aac *.ogg"),
            ("既有字幕 SRT（只做校正）", "*.srt"), ("原文 JSON", "*.json"), ("所有檔案", "*.*")])
        if p:
            self.route_path(p)

    def pick_vocab(self):
        p = filedialog.askopenfilename(title="選擇詞庫 CSV", initialdir=str(BASE), filetypes=[("CSV", "*.csv"), ("所有檔案", "*.*")])
        if p:
            self.set_cfg(vocab=p)

    def pick_out(self):
        p = filedialog.askdirectory(title="選擇字幕存放資料夾", initialdir=self.st["out_dir"] or str(BASE))
        if p:
            self.st["out_dir"] = p
            self.render()

    def open_out(self):
        d = Path(self.st["out_dir"])
        if d.exists():
            open_path(d)

    def open_review(self):
        if self.st["out_path"] and Path(self.st["out_path"]).exists():
            open_path(self.st["out_path"])

    # ───────────── 設定 ─────────────
    @staticmethod
    def _name(table, val):
        return next((n for n, v, _ in table if v == val), table[0][0])

    @staticmethod
    def _hint(table, val):
        return next((h for n, v, h in table if v == val), "")

    def set_cfg(self, **kw):
        self.cfg.update(kw)
        self.cfg.save()
        self.render()

    def step(self, key, d):
        if key == "offset":
            v = round(min(1.5, max(-1.0, self.cfg["offset"] + 0.05 * d)), 2)
            self.set_cfg(offset=v)
        else:
            self.set_cfg(cjk_width=int(min(24, max(8, self.cfg["cjk_width"] + d))))

    def popup(self, e, key):
        table, ck = {"game": (GAMES, "game"), "model": (MODELS, "model"), "lang": (LANGS, "lang")}[key]
        m = tk.Menu(self, tearoff=0, bg=PANEL_HI, fg=TEXT, activebackground="#4a3a22", activeforeground="#ffffff",
                    font=self.f["n"], bd=1, relief="solid")
        for name, val, hint in table:
            mark = "✓  " if self.cfg[ck] == val else "　 "
            m.add_command(label=f"{mark}{name}　　{hint}", command=lambda v=val: self.set_cfg(**{ck: v}))
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def toggle_auto(self, *_):
        self.set_cfg(auto_update=not self.cfg.get("auto_update", True))

    def _next_tip(self):
        self.tip_i += 1
        try:
            self.cv.itemconfigure("tiptext", text="提示　" + TIPS[self.tip_i % len(TIPS)])
        except Exception:
            pass
        self.after(9000, self._next_tip)

    def _animate(self):
        if self.st["busy"] or self.st["result"] or self.st["error"]:
            self.phase += 0.18
            self._draw_orb()
            self.commit()
            self.cv.tag_raise("tip")
        self.after(70 if self.st["busy"] else 140, self._animate)

    # ───────────── 個人詞庫編輯 ─────────────
    def edit_personal(self):
        rows, extra = [], []
        try:
            with open(PERSONAL_CSV, encoding="utf-8-sig", newline="") as f:
                for r in csv.DictReader(f):
                    if r.get("類型") in ("誤聽修正", "英文誤聽") and r.get("誤聽寫法") and r.get("繁體中文"):
                        rows.append(r)
                    else:
                        extra.append(r)
        except FileNotFoundError:
            pass
        orig = {}
        for r in rows:                         # 同一條規則出現多次時，以最後一條為準
            k = (r["誤聽寫法"].strip(), r["繁體中文"].strip())
            orig.pop(k, None)
            orig[k] = r
        lines = [("?" if r.get("自動替換", "").strip() != "是" else "") + f"{k[0]}={k[1]}" for k, r in orig.items()]
        try:
            prot = PERSONAL_PROTECT.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            prot = ""

        top = tk.Toplevel(self, bg=BG)
        top.title("個人詞庫")
        top.transient(self)
        w, h = int(820 * self.s), int(560 * self.s)
        top.geometry(f"{w}x{h}+{self.winfo_rootx() + 60}+{self.winfo_rooty() + 60}")
        hdr = tk.Label(top, text="個人詞庫　·　只存在你的電腦，自動更新不會覆蓋", bg=BG, fg=GOLD, font=self.f["h"], anchor="w")
        hdr.pack(fill="x", padx=16, pady=(14, 4))
        body = tk.Frame(top, bg=BG)
        body.pack(fill="both", expand=True, padx=16)
        boxes = []
        for col, (title, help_, content) in enumerate((
                ("錯字自動修正", "一行一條：錯誤寫法=正確寫法\n前面加 ? 代表只列進待確認清單、不自動改\n例：我是萌萌=我是惡魔貓　　?紅毛=惡魔貓", "\n".join(lines)),
                ("斷句保護", "一行一個詞，斷句時不會從中間切開\n例：能量護盾、混沌傷害\n# 開頭的行是註解", prot))):
            fr = tk.Frame(body, bg=PANEL, highlightthickness=1, highlightbackground=EDGE)
            fr.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 8, 0), pady=4)
            body.columnconfigure(col, weight=3 if col == 0 else 2)
            tk.Label(fr, text=title, bg=HEAD, fg=GOLD, font=self.f["b"]).pack(fill="x")
            tk.Label(fr, text=help_, bg=PANEL, fg=DIM, font=self.f["s"], justify="left", anchor="w").pack(fill="x", padx=10, pady=6)
            t = tk.Text(fr, bg="#0a0907", fg=TEXT, insertbackground=GOLD, relief="flat", font=self.f["n"], undo=True,
                        selectbackground="#4a3a22", padx=8, pady=6)
            t.pack(fill="both", expand=True, padx=6, pady=(0, 6))
            t.insert("1.0", content)
            boxes.append(t)
        body.rowconfigure(0, weight=1)
        bar = tk.Frame(top, bg=BG)
        bar.pack(fill="x", padx=16, pady=12)
        status = tk.Label(bar, text="", bg=BG, fg=DIM, font=self.f["s"])
        status.pack(side="left")

        def save():
            out, bad = [], []
            for ln in boxes[0].get("1.0", "end").splitlines():
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                auto = not ln.startswith("?")
                ln = ln.lstrip("?？").strip()
                ln = ln.replace("＝", "=")
                if "=" not in ln:
                    bad.append(ln)
                    continue
                wrong, right = (x.strip() for x in ln.split("=", 1))
                if not wrong or not right:
                    bad.append(ln)
                    continue
                r = dict(orig.get((wrong, right)) or {})
                if not r:
                    r = {k: "" for k in CSV_FIELDS}
                    r.update(遊戲="共用", 來源="個人詞庫")
                r.update(類型="英文誤聽" if wrong.isascii() else "誤聽修正", 繁體中文=right, 誤聽寫法=wrong,
                         自動替換="是" if auto else "否")
                out.append(r)
            if bad:
                status.configure(text="這幾行看不懂（需要「錯誤=正確」）：" + "、".join(bad[:3]), fg=RED)
                return
            with open(PERSONAL_CSV, "w", encoding="utf-8-sig", newline="") as f:
                wr = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
                wr.writeheader()
                wr.writerows(extra + out)
            PERSONAL_PROTECT.write_text(boxes[1].get("1.0", "end").strip() + "\n", encoding="utf-8")
            self.say(f"個人詞庫已儲存：{len(out)} 條修正規則，下次產生字幕時生效。\n", "gem")
            top.destroy()

        for txt, cmd, bg in (("取消", top.destroy, "#1d1812"), ("儲存", save, "#4b1a12")):
            tk.Button(bar, text=txt, command=cmd, bg=bg, fg="#f3dfb3", activebackground="#6a2618", activeforeground="#fff",
                      relief="flat", bd=0, padx=22, pady=6, font=self.f["b"], cursor="hand2",
                      highlightthickness=1, highlightbackground=GOLD).pack(side="right", padx=(8, 0))
        tk.Button(bar, text="用記事本／Excel 開啟 CSV", command=lambda: (save(), open_path(PERSONAL_CSV)), bg="#1d1812",
                  fg=DIM, relief="flat", bd=0, padx=10, pady=6, font=self.f["s"], cursor="hand2").pack(side="right", padx=8)

    # ───────────── 執行 ─────────────
    def start(self, *_):
        st = self.st
        if st["busy"] or not st["file"]:
            return
        media = Path(st["file"])
        if not media.exists():
            return messagebox.showwarning("找不到檔案", "這個檔案已經不在原本的位置，請重新選擇。")
        vocab = self.cfg.get("vocab", "")
        if not vocab or not Path(vocab).exists():
            return messagebox.showwarning("缺少詞庫", f"找不到詞庫檔，請在「詞庫」欄重新選擇 {VOCAB_NAME}。")
        out = st["out_dir"] or str(media.parent / "字幕輸出")
        py = Path(sys.executable)
        if py.name.lower() == "pythonw.exe" and py.with_name("python.exe").exists():
            py = py.with_name("python.exe")
        c = self.cfg
        argv = [str(py), "-u", str(BASE / "poe_subtitle.py"), str(media), "--vocab", vocab, "--out-dir", out,
                "--game", c["game"], "--model", c["model"], "--lang", c["lang"],
                "--offset", f"{c['offset']:.2f}", "--cjk-width", str(int(c["cjk_width"]))]
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        try:
            self.proc = subprocess.Popen(argv, cwd=str(BASE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, env=env,
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as ex:
            return messagebox.showerror("無法啟動", str(ex))
        st.update(busy=True, result=None, error=None, cancelled=False, frac=0.0, total=None, t0=None, pos0=0.0,
                  out_dir=out, out_path="", started=time.time(),
                  stage="準備中…" if st["kind"] in ("video", "audio") else "校正中…",
                  detail="轉換音訊、載入辨識模型（第一次使用會下載約 3GB）" if st["kind"] in ("video", "audio") else "")
        self.say(f"\n── 開始處理：{media.name} ──\n", "head")
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        self.render()

    def _reader(self, proc):
        dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        fd = proc.stdout.fileno()
        while True:
            try:
                b = os.read(fd, 4096)
            except OSError:
                break
            if not b:
                break
            self.q.put(("out", dec.decode(b)))
        self.q.put(("exit", proc.wait()))

    def cancel(self, *_):
        if self.proc and self.proc.poll() is None:
            self.st["cancelled"] = True
            try:
                self.proc.terminate()
            except Exception:
                pass

    def _parse(self, chunk):
        st = self.st
        for ln in chunk.replace("\r", "\n").split("\n"):
            m = re.search(r"音訊長度 .*?（(\d+) 秒）", ln)
            if m:
                st.update(total=float(m.group(1)), stage="載入辨識模型…",
                          detail="第一次使用會下載模型（約 3GB），請耐心等候")
                continue
            m = re.match(r"^\[(?:(\d+):)?(\d+):(\d+)\]", ln)
            if m and st["total"]:
                pos = int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
                now = time.time()
                if st["t0"] is None:
                    st["t0"], st["pos0"] = now, pos
                st["frac"] = min(0.97, 0.03 + 0.94 * pos / st["total"])
                st["stage"] = "辨識語音中"
                det = f"已辨識 {fmt_time(pos)} / {fmt_time(st['total'])}"
                el, done = now - st["t0"], pos - st["pos0"]
                if el > 8 and done > 5:
                    det += f"　·　剩約 {fmt_time((st['total'] - pos) * el / done)}"
                st["detail"] = det
                continue
            m = re.search(r"完成：(\d+) 句字幕，自動替換 (\d+) 處，待確認 (\d+) 項", ln)
            if m:
                st["result"] = tuple(int(x) for x in m.groups())
                continue
            m = re.search(r"輸出資料夾：(.+)$", ln)
            if m:
                st["out_dir"] = m.group(1).strip()
                stem = Path(st["file"]).stem.replace(" 原文", "")
                st["out_path"] = str(Path(st["out_dir"]) / f"{stem} 字幕.review.txt")
            if re.search(r"找不到|缺少|Error|錯誤", ln):
                st["last_err"] = ln.strip()

    def _finish(self, code):
        st = self.st
        st["busy"] = False
        self.proc = None
        el = time.time() - st.get("started", time.time())
        if st["cancelled"]:
            st.update(result=None, error=None, frac=0.0)
            self.say("── 已停止 ──\n", "warn")
        elif code == 0 and st["result"]:
            st["frac"] = 1.0
            self.say(f"── 完成，共花了 {fmt_time(el)} ──\n", "done")
            self.bell()
        else:
            msg = st.get("last_err") or f"程式結束代碼 {code}"
            if "ffmpeg" in msg:
                msg += "\n請重新執行「一鍵安裝並啟動.bat」安裝 ffmpeg"
            elif "faster-whisper" in msg or "faster_whisper" in msg:
                msg += "\n請重新執行「一鍵安裝並啟動.bat」安裝辨識套件"
            st["error"] = msg
            self.say("── 失敗，請把上面的紀錄複製給開發者 ──\n", "err")
        st["last_err"] = ""
        self.render()

    def pump(self):
        changed = False
        out = []
        try:
            for _ in range(3000):
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "out":
                    out.append(item[1])
                elif kind == "exit":
                    if out:
                        s = "".join(out)
                        self.say(s)
                        self._parse(s)
                        out = []
                    self._finish(item[1])
                elif kind == "dur":
                    if item[1] == self.st["file"]:
                        self.st["dur"] = item[2]
                        changed = True
                elif kind == "upd":
                    self._on_update_event(*item[1:])
                    changed = True
        except queue.Empty:
            pass
        if out:
            s = "".join(out)
            self.say(s)
            self._parse(s)
            changed = True
        if changed:
            self.render()
        self.after(120, self.pump)

    # ───────────── 自動更新 ─────────────
    def check_update(self, silent=False):
        if self.st["upd_state"] in ("checking", "busy"):
            return
        self.st["upd_state"] = "checking"
        self.render()

        def work():
            try:
                self.q.put(("upd", "checked", updater.check(), silent))
            except Exception as ex:
                self.q.put(("upd", "offline", str(ex), silent))

        threading.Thread(target=work, daemon=True).start()

    def _on_update_event(self, kind, data, extra=None):
        st = self.st
        if kind == "checked":
            if data["newer"] and (data["version"] != self.cfg.get("skip_version") or not extra):
                st.update(update=data, upd_state="", banner=True)
            else:
                st.update(update=None, upd_state="latest")
        elif kind == "offline":
            st["upd_state"] = "offline"
        elif kind == "progress":
            st.update(upd_state="busy", upd_frac=data, upd_text=extra)
        elif kind == "applied":
            res = data
            st.update(upd_state="done", upd_text=f"已更新到 v{res['version']}（{len(res['changed'])} 個檔案），重新啟動中…")
            self.render()
            self.after(1200, lambda: self._restart(res["reqs_changed"]))
        elif kind == "failed":
            st.update(upd_state="error", upd_text=data)

    def do_update(self, *_):
        if self.st["busy"]:
            return messagebox.showinfo("稍等一下", "正在產生字幕，完成後再更新。")
        info = self.st["update"]
        self.st.update(upd_state="busy", upd_frac=0.0, upd_text="準備中")
        self.render()

        def work():
            try:
                res = updater.apply_update(info, progress=lambda f, t: self.q.put(("upd", "progress", f, t)))
                self.q.put(("upd", "applied", res))
            except Exception as ex:
                self.q.put(("upd", "failed", str(ex)))

        threading.Thread(target=work, daemon=True).start()

    def skip_update(self, *_):
        self.set_cfg(skip_version=self.st["update"]["version"])
        self.st.update(update=None, upd_state="latest")
        self.render()

    def _restart(self, reqs_changed):
        self._save_geometry()
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if reqs_changed else 0
        try:
            if reqs_changed and (BASE / "一鍵安裝並啟動.bat").exists():
                subprocess.Popen(["cmd", "/c", str(BASE / "一鍵安裝並啟動.bat")], cwd=str(BASE), creationflags=flags)
            else:
                subprocess.Popen([sys.executable, str(BASE / "poe_subtitle_gui.py")], cwd=str(BASE))
        except Exception as ex:
            messagebox.showinfo("請手動重新開啟", f"更新完成，請重新開啟字幕工具。\n（{ex}）")
        self.destroy()

    # ───────────── 關閉 ─────────────
    def _save_geometry(self):
        try:
            if self.state() == "normal":
                self.cfg["geometry"] = self.geometry()
            self.cfg.save()
        except Exception:
            pass

    def on_close(self):
        if self.st["busy"]:
            if not messagebox.askyesno("還在產生字幕", "字幕還沒做完，確定要關閉嗎？"):
                return
            self.cancel()
        self._save_geometry()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
