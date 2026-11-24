#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
poe_subtitle.py — 流亡黯道 (Path of Exile) 繁體中文字幕工具

輸入可以是：
  * 影音檔  .mp4 .mov .mkv .mp3 .wav .m4a ...  → 辨識 → 詞庫校正 → 切句 → SRT
  * 原文.json（本工具先前輸出的字級時間檔）    → 不重新辨識，直接重跑校正與切句
  * 既有 .srt（例如 Resolve 自動字幕）          → 只做詞庫校正，保留原時間軸

輸出（放在 --out-dir）：
  <名稱> 原文.json        字級時間（重跑用）
  <名稱> 原文.txt         閱讀用
  <名稱> 字幕.srt         最終字幕
  <名稱> 字幕.review.txt  自動替換紀錄＋待人工聽原音確認的項目

詞庫：火炬和流亡詞彙庫.csv（欄位：遊戲,類型,分類,繁體中文,簡體中文,英文,誤聽寫法,自動替換,來源,備註）
  * 誤聽修正／英文誤聽 且「自動替換＝是」 → 直接替換
  * 「自動替換＝否」、疑似詞條（同音／差一字） → 只列入 review，不改字幕
"""
import argparse
import csv
import json
import math
import re
import subprocess
import sys
import tempfile
from pathlib import Path

VERSION = "0.1.32"  # 每次更新請遞增最後一碼，並寫進 更新紀錄.txt

CJK = r"一-鿿"
CJK_RE = re.compile(f"[{CJK}]")
TOKEN_RE = re.compile(f"[{CJK}]|[^{CJK}]+")
STRONG_PUNCT = "。？！?!"
WEAK_PUNCT = "，、；：,;"
ALL_PUNCT = STRONG_PUNCT + WEAK_PUNCT + "….．・「」『』（）()《》〈〉“”\"'‘’—─"
KEEP_END = "？！?!"

GAME_SETS = {
    "poe": {"流亡黯道", "流亡黯道1", "流亡黯道2"},
    "poe1": {"流亡黯道", "流亡黯道1"},
    "poe2": {"流亡黯道", "流亡黯道2"},
    "torchlight": {"火炬之光"},
}


# ───────────────────────── 詞庫 ─────────────────────────
def _boundary(term):
    """純 ASCII 詞要求前後不是英數字，避免 pv 命中 pve 的一部分。"""
    esc = re.escape(term)
    if re.fullmatch(r"[A-Za-z0-9 ]+", term):
        return rf"(?<![A-Za-z0-9]){esc}(?![A-Za-z0-9])"
    return esc


def _read_rows(path):
    """主詞庫 + 同資料夾的 *術語補充*.csv 一起讀取。"""
    extra = sorted(Path(path).parent.glob("*術語補充*.csv"), key=lambda q: (q.name.startswith("個人"), q.name))
    paths = [Path(path)] + extra   # 個人詞庫最後讀，可以覆蓋共用規則
    for p in paths:
        with open(p, encoding="utf-8-sig", newline="") as f:
            yield from csv.DictReader(f)


class Vocab:
    def __init__(self, path, game="poe"):
        allowed = GAME_SETS[game] | {"共用"}
        self.auto = {}          # 小寫誤聽 → (正確, 備註)
        self.flag = []          # (誤聽, 正確, 備註)
        self.terms = {}         # 純中文詞條 → 英文（疑似比對用）
        self.prompt_terms = []  # 給 Whisper 的提示詞（中文）
        self.prompt_en = []     # 給 Whisper 的提示詞（英文專名）
        self.protect = set()    # 斷句時不拆開的詞庫詞
        for r in _read_rows(path):
            if r["遊戲"] not in allowed:
                continue
            typ = r["類型"]
            zh, en = r["繁體中文"].strip(), r["英文"].strip()
            wrong, note = r["誤聽寫法"].strip(), r["備註"].strip()
            if typ in ("誤聽修正", "英文誤聽") and wrong and zh:
                if r["自動替換"].strip() == "是":
                    self.auto[wrong.lower()] = (zh, note)
                else:
                    self.flag.append((wrong, zh, note))
                continue
            if zh and re.fullmatch(f"[{CJK}]{{2,8}}", zh):
                self.protect.add(zh)
            if zh and re.fullmatch(f"[{CJK}]+", zh) and len(zh) >= 3:
                self.terms.setdefault(zh, en)
            if (typ == "人工詞彙" and r["分類"] in ("系統", "通貨", "實況用語")
                    and "人名" not in note and zh):
                self.prompt_terms.append(zh)
            if typ == "人工詞彙" and r["分類"] in ("名稱", "系統", "通貨", "實況用語"):
                cand = re.sub(r"\s*\(.*?\)", "", en or zh).strip()
                if cand and cand.isascii() and cand not in self.prompt_en:
                    self.prompt_en.append(cand)
        self.auto = {k: v for k, v in self.auto.items() if k != v[0]}   # 只差大小寫的保留（用來統一寫法）
        if self.auto:
            keys = sorted(self.auto, key=len, reverse=True)
            self.auto_re = re.compile("|".join(_boundary(k) for k in keys), re.I)
        else:
            self.auto_re = None
        self.flag_re = [(re.compile(_boundary(w), re.I), w, z, n) for w, z, n in self.flag]
        self._build_fuzzy()

    def _build_fuzzy(self):
        # 差一字索引（詞條長度 ≥5）
        self.wild = {}
        for t in self.terms:
            if len(t) >= 5:
                for i in range(len(t)):
                    self.wild.setdefault(t[:i] + "*" + t[i + 1:], []).append(t)
        # 同音索引（需要 pypinyin；沒有就略過）
        self.pinyin = None
        try:
            from pypinyin import lazy_pinyin
            self._py = lazy_pinyin
            idx = {}
            for t in self.terms:
                idx.setdefault(tuple(self._py(list(t))), []).append(t)
            self.pinyin = idx
            self.py_lens = sorted({len(k) for k in idx})
        except Exception:
            self.pinyin = None

    def prompt(self, lang="auto", limit=40):
        """英文／自動模式用純英文提示（中文提示會讓 Whisper 把英文硬翻成中文）。"""
        if str(lang).lower() == "zh":
            seen, out = set(), []
            for t in self.prompt_terms:
                if t not in seen:
                    seen.add(t)
                    out.append(t)
            return "以下是流亡黯道 Path of Exile 的繁體中文遊戲實況。" + "、".join(out[:limit])
        return ("An interview and stream about Path of Exile (PoE) and Path of Exile 2 (PoE 2) at ExileCon. "
                "Names and terms: " + ", ".join(self.prompt_en[:limit]) + ".")


# ───────────────────────── 時間單位 ─────────────────────────
# unit = [text, start, end, prob]
def words_to_units(words):
    units = []
    for w in words:
        text, s, e, p = w["w"], w["s"], w["e"], w.get("p", 1.0)
        toks = TOKEN_RE.findall(text)
        total = sum(len(t) for t in toks) or 1
        pos = 0
        for t in toks:
            a = s + (e - s) * pos / total
            pos += len(t)
            b = s + (e - s) * pos / total
            units.append([t, a, b, p])
    return units


def to_traditional(units, converter):
    if converter is None:
        return
    for u in units:
        c = converter.convert(u[0])
        if len(c) == len(u[0]):
            u[0] = c


def apply_auto(units, vocab, log):
    """在單位層做自動替換，替換處的時間依字數平均分配；同一單位內有多處命中也都會處理。"""
    if vocab.auto_re is None:
        return units
    text = "".join(u[0] for u in units)
    offs = [0]
    for u in units:
        offs.append(offs[-1] + len(u[0]))
    groups = []                       # [起始單位, 結束單位, [(a, b, 正確寫法), ...]]
    for m in vocab.auto_re.finditer(text):
        a, b = m.span()
        i0 = next(i for i in range(len(units)) if offs[i + 1] > a)
        i1 = next(i for i in range(len(units)) if offs[i + 1] >= b)
        right = vocab.auto[m.group(0).lower()][0]
        if m.group(0) == right:          # 已經是正確寫法
            continue
        log.append((units[i0][1], m.group(0), right))
        if groups and i0 <= groups[-1][1]:
            groups[-1][1] = max(groups[-1][1], i1)
            groups[-1][2].append((a, b, right))
        else:
            groups.append([i0, i1, [(a, b, right)]])
    out, last = [], 0
    for i0, i1, ms in groups:
        out.extend(units[last:i0])
        pos, piece = offs[i0], ""
        for a, b, right in ms:
            piece += text[pos:a] + right
            pos = b
        piece += text[pos:offs[i1 + 1]]   # 保留命中範圍之外的字（空格、標點）
        s, e = units[i0][1], units[i1][2]
        p = min(u[3] for u in units[i0:i1 + 1])
        toks = TOKEN_RE.findall(piece)
        total = sum(len(t) for t in toks) or 1
        k = 0
        for t in toks:
            x = s + (e - s) * k / total
            k += len(t)
            out.append([t, x, s + (e - s) * k / total, p])
        last = i1 + 1
    out.extend(units[last:])
    return out


def find_flags(text, timeat, vocab):
    """只標記、不修改：人工確認詞、同音詞條、差一字詞條。"""
    flags = []
    for rx, w, z, n in vocab.flag_re:
        for m in rx.finditer(text):
            flags.append((timeat(m.start()), "待確認詞", f"「{m.group(0)}」可能是「{z}」" + (f"（{n}）" if n else "")))
    seen = set()
    cjk_run = [(m.start(), m.group(0)) for m in re.finditer(f"[{CJK}]+", text)]
    for st, run in cjk_run:
        for L in range(5, 11):
            for i in range(len(run) - L + 1):
                w = run[i:i + L]
                if w in vocab.terms:
                    continue
                for j in range(L):
                    for t in vocab.wild.get(w[:j] + "*" + w[j + 1:], []):
                        key = (st + i, t)
                        if key not in seen:
                            seen.add(key)
                            flags.append((timeat(st + i), "疑似詞條(差一字)", f"「{w}」→「{t}」 {vocab.terms[t]}"))
        if vocab.pinyin:
            py = vocab._py(list(run))          # 每個片段只轉一次拼音，避免大量重複運算拖慢
            for L in vocab.py_lens:
                for i in range(len(run) - L + 1):
                    w = run[i:i + L]
                    if w in vocab.terms:
                        continue
                    for t in vocab.pinyin.get(tuple(py[i:i + L]), []):
                        key = (st + i, t)
                        if key not in seen:
                            seen.add(key)
                            flags.append((timeat(st + i), "疑似詞條(同音)", f"「{w}」→「{t}」 {vocab.terms[t]}"))
    return flags


# ───────────────────────── 切句 ─────────────────────────
def width(s):
    return sum(0.5 if ord(c) < 0x2E80 else 1 for c in s)


def units_to_chars(units):
    chars = []
    for text, s, e, p in units:
        n = len(text) or 1
        for k, ch in enumerate(text):
            chars.append([ch, s + (e - s) * k / n, s + (e - s) * (k + 1) / n, p])
    return chars


def split_clauses(chars, gap):
    """切成句（強標點／長停頓），句內再切成子句（弱標點）。回傳 [句 = [子句 = chars]]。"""
    sents, clauses, cur = [], [], []
    for i, c in enumerate(chars):
        cur.append(c)
        nxt = chars[i + 1] if i + 1 < len(chars) else None
        dt = (nxt[1] - c[2]) if nxt is not None else 0
        pause = dt > gap
        if pause and nxt is not None and (c[0] + nxt[0]) in _WORDS2 and dt < 30:
            pause = False                      # 詞中間的長停頓多為時間戳異常，不切
        prev_ch = chars[i - 1][0] if i else ""
        digits = prev_ch.isdigit() and nxt is not None and nxt[0].isdigit()
        dot_end = c[0] == "." and (nxt is None or nxt[0].isspace()) and not prev_ch.isdigit()   # 英文句號（不含 5.01）
        if c[0] in STRONG_PUNCT or dot_end or pause or nxt is None:
            clauses.append(cur)
            sents.append(clauses)
            clauses, cur = [], []
        elif (c[0] in WEAK_PUNCT and not digits) or dt > gap * 0.5:     # 弱標點或較短停頓 → 子句邊界
            clauses.append(cur)
            cur = []
    return sents


def _wordch(x):
    return x.isascii() and (x.isalnum() or x in "'’-")


def _nocut(a, b):
    """a、b 是相鄰兩字，是否不該在中間切開（英文單字、5.01 這類數字）。"""
    return (_wordch(a) and _wordch(b)) or (a in ".," and b.isdigit()) or (b in ".," and a.isdigit())


def clean(chars):
    """去標點，弱標點變成空格（語氣停頓），保留句尾 ？！；英文縮寫的 ' 與數字小數點保留。"""
    s = ""
    n = len(chars)
    for i, (ch, *_) in enumerate(chars):
        prev = chars[i - 1][0] if i else ""
        nxt = chars[i + 1][0] if i + 1 < n else ""
        if ch in "'’" and prev.isascii() and prev.isalnum() and nxt.isascii() and nxt.isalnum():
            s += "'"
            continue
        if ch in ".," and prev.isdigit() and nxt.isdigit():
            s += ch
            continue
        if ch in WEAK_PUNCT:
            s += " "
        elif ch in STRONG_PUNCT and ch not in KEEP_END:
            s += " "
        elif ch in ALL_PUNCT and ch not in KEEP_END:
            continue
        else:
            s += ch
    return re.sub(r"\s+", " ", s).strip()


def two_lines(clauses, maxw):
    """一個 cue 超過一行寬時，拆成兩行，優先在子句邊界、其次在中間。"""
    texts = [clean(c) for c in clauses]
    full = " ".join(t for t in texts if t)
    if width(full) <= maxw:
        return full
    best, bd = None, 1e9
    for k in range(1, len(texts)):
        a, b = " ".join(texts[:k]).strip(), " ".join(texts[k:]).strip()
        if a and b and width(a) <= maxw and width(b) <= maxw and abs(width(a) - width(b)) < bd:
            best, bd = (a, b), abs(width(a) - width(b))
    if best:
        return best[0] + "\n" + best[1]
    flat = full
    mid = len(flat) // 2
    for d in range(0, mid):          # 從中間往外找安全斷點，不切英文字／數字
        for pos in (mid - d, mid + d):
            if 0 < pos < len(flat) and not _nocut(flat[pos - 1], flat[pos]):
                return flat[:pos].strip() + "\n" + flat[pos:].strip()
    return flat


def _safe_cut(cl, k):
    """在 k 附近找不會切斷英文單字的位置。"""
    def ok(i):
        return not _nocut(cl[i - 1][0], cl[i][0])
    for i in range(min(k, len(cl) - 1), 0, -1):
        if ok(i):
            return i
    for i in range(k + 1, len(cl)):
        if ok(i):
            return i
    return k


def _is_cjk(ch):
    return "\u4e00" <= ch <= "\u9fff"


def _has_cjk(cl):
    return any(_is_cjk(c[0][:1]) for c in cl if c[0])


_PARTICLES = set("啊吧呢嗎了的說就然後但是所以因為而且或者那")


_WORDS2 = set("所有 所以 因為 為了 為什 但是 然後 如果 可以 可能 已經 還是 就是 這個 那個 一個 一下 一些 一樣 一直 一定 什麼 怎麼 沒有 自己 時候 現在 之後 之前 以後 以前 其實 不過 而且 或者 雖然 當然 應該 需要 比較 非常 真的 一起 開始 結束 裡面 外面 上面 下面 前面 後面 這樣 那樣 這邊 那邊 我們 你們 他們 大家 朋友 玩家 角色 技能 傷害 裝備 地圖 房間 怪物 武器 防禦 攻擊 召喚 範圍 效果 數值 機率 等級 階段 任務 通關 打王 這種 那種 有點 有些 部分 全部 大概 基本 主要 重點 最後 最近 以及 因此 所謂 其他 另外 另一 每個 每次 每一 一次 兩個 三個 四個 五個 十個 第一 第二 第三 不是 不會 不能 不要 不用 不行 不錯 有沒 沒什 會不 能不 好像 好用 好玩 好看 一般 一半 一點 一堆 一波".split())
_WORDS2 |= set("下期 下次 上次 這次 上期 本期 超瓦 瓦爾 蜃景 虛空 夢魘 下午 上午 晚上 早上 中午 昨天 今天 明天 地圖 通貨 輿圖 天賦 神廟 女王 王房 階房 三階 二階 一階 穿越 絲帶 面容 靈魂 核心 迴響 價格 收益 機制 角色 流派 後綴 前綴 詞綴 區域 等級 掉落 實況 影片 觀眾 聊天 留言 更新 改動 測試 策略 賺錢 成本 利潤 場數 機體 房間 寶石 腐敗 閃電 敵對 建築 升階 合成 夢魘 點數 投資 增加 提升 強制 保證 確定 解釋 說明 重要 簡單 容易 便宜 太貴 變成 變得 看到 看起來 覺得 認為 知道 遇到 找到 打到 打死 打得 蓋到".split())
try:
    import json as _json
    _ST = _json.load(open(Path(__file__).with_name("斷句統計.json"), encoding="utf-8"))
    _WORDS2 |= set(_ST.get("nocut", []))
    _CUTP = _ST.get("cut", {})
except Exception:
    _CUTP = {}
_STARTERS = set("我你他她它們這那就但所因然如而還又也")
_NOSTART = set("個的了著過嗎呢吧啊們")


def _best_cut(cl, lim):
    """在字寬上限內挑最自然的斷點：優先停頓最久、語言切換（中↔英）、語氣詞之後。"""
    cum, kmax = 0.0, 0
    for i, c in enumerate(cl):
        cum += width(c[0])
        if cum > lim:
            break
        kmax = i + 1
    kmax = max(kmax, 1)
    cum, kmin = 0.0, 1
    for i, c in enumerate(cl):
        cum += width(c[0])
        if cum >= lim * 0.4:
            kmin = i + 1
            break
    best, best_s = None, -1.0
    for i in range(max(kmin, 1), min(kmax, len(cl) - 1) + 1):
        p, q = cl[i - 1], cl[i]
        if _nocut(p[0], q[0]):
            continue
        if (p[0][-1:] + q[0][:1]) in _WORDS2:
            continue                       # 不把常用詞拆開（所有、時候、可以…）
        sc = max(q[1] - p[2], 0.0)
        a, b = p[0][:1], q[0][:1]
        if a.strip() and b.strip() and _is_cjk(a) != _is_cjk(b):
            sc += 1.0
        if a in _PARTICLES:
            sc += 0.15
        sc += 0.5 * _CUTP.get(a + b[:1], 0.0)   # 使用者習慣常在此處斷開
        if b in _STARTERS:
            sc += 0.1
        if b in _NOSTART:
            sc -= 0.2
        sc += i * 1e-4
        if sc > best_s:
            best, best_s = i, sc
    return best if best else _safe_cut(cl, kmax)


# ───────── 學習式斷句（由使用者成品字幕訓練；斷句模型.json）─────────
def _load_model():
    try:
        d = json.load(open(Path(__file__).with_name("斷句模型.json"), encoding="utf-8"))
        return d["b"], d["w"]
    except Exception:
        return None


_MODEL = _load_model()
_PROTECT = set()      # 詞庫與 遊戲術語保護.txt 裡的詞，斷句時不在中間斷開
for _pf in ("遊戲術語保護.txt", "個人術語保護.txt"):   # 個人術語保護.txt：自己加的，自動更新不會覆蓋
    try:
        for _ln in open(Path(__file__).with_name(_pf), encoding="utf-8-sig"):
            _ln = _ln.strip()
            if _ln and not _ln.startswith("#"):
                _PROTECT.add(_ln)
    except Exception:
        pass
_TOKRE = re.compile(r"[A-Za-z0-9'.%+]+|[\u4e00-\u9fff]|[^\sA-Za-z0-9\u4e00-\u9fff]")


def _tnorm(w):
    w = w.lower()
    return "#" if re.fullmatch(r"[0-9.%+]+", w) else w


def _tkind(w):
    return "c" if "\u4e00" <= w[0] <= "\u9fff" else ("d" if w[0].isdigit() else "e")


def _tokenize(chars):
    """回傳 [(正規化字串, 原文, 首字索引, 末字索引)]；略過標點與空白，英數連續合併。"""
    text = "".join(c[0] for c in chars)
    owner = [k for k, c in enumerate(chars) for _ in c[0]]      # 文字位置 → chars 項目索引
    out = []
    for m in _TOKRE.finditer(text):
        w = m.group(0)
        if re.fullmatch(r"[^\w\u4e00-\u9fff%.+]", w):
            continue
        out.append((_tnorm(w), w, owner[m.start()], owner[m.end() - 1]))
    return out


def _cut_logit(T, i):
    g = lambda k: T[k][0] if 0 <= k < len(T) else "^"
    a, b, a2, b2 = g(i - 1), g(i), g(i - 2), g(i + 1)
    b0, w = _MODEL
    x = b0
    for f in ("a=" + a, "b=" + b, "ab=" + a + b, "a2a=" + a2 + a, "bb2=" + b + b2,
              "a2ab=" + a2 + a + b, "abb2=" + a + b + b2, "ka=" + _tkind(a) + _tkind(b)):
        x += w.get(f, 0.0)
    return x


def _dp_segments(chars, cjkw, max_dur):
    """在一整句內，用『斷開機率＋長度偏好（約 10 字）』的動態規劃一次決定所有斷點。回傳 [(char起, char迄)]。"""
    T = _tokenize(chars)
    n = len(T)
    if n == 0:
        return []
    wid = [width(t[1]) for t in T]
    cum = [0.0]
    for x in wid:
        cum.append(cum[-1] + x)
    L1, L0 = [0.0] * (n + 1), [0.0] * (n + 1)
    for i in range(1, n):
        x = _cut_logit(T, i)
        gt = chars[T[i][2]][1] - chars[T[i - 1][3]][2]
        x += min(max(gt, 0.0), 0.6) * 4                                     # 停頓越久越該斷
        if any(chars[k][0] in ALL_PUNCT for k in range(T[i - 1][3] + 1, T[i][2])):
            x += 1.0                                                        # 原文有標點
        if T[i - 1][0] == "#" and _is_cjk(T[i][1][:1]):
            x -= 4.0                                                        # 數字後面的單位不拆
        if (T[i - 1][1][-1:] + T[i][1][:1]) in _WORDS2:
            x -= 3.0
        if _PROTECT:
            hit = False
            for a0 in range(max(0, i - 8), i):
                acc = ""
                for k in range(a0, min(n, a0 + 8)):
                    acc += T[k][1]
                    if k >= i and len(acc) >= 2 and acc in _PROTECT:
                        hit = True
                        break
                if hit:
                    break
            if hit:
                x -= 5.0                                                    # 詞庫裡的詞（凋零、堅毅之心…）不拆                                                        # 常用詞不拆
        pcut = 1.0 / (1.0 + math.exp(-max(min(x, 30), -30)))
        L1[i] = math.log(max(pcut, 1e-6))
        L0[i] = math.log(max(1 - pcut, 1e-6))
    S0 = [0.0] * (n + 1)
    for i in range(1, n):
        S0[i] = S0[i - 1] + L0[i]
    S0[n] = S0[n - 1] if n > 0 else 0.0
    best = [-1e18] * (n + 1)
    bk = [0] * (n + 1)
    best[0] = 0.0
    mu, sig = 10.0, 4.0
    for j in range(1, n + 1):
        for i in range(j - 1, -1, -1):
            ln = cum[j] - cum[i]
            dur = chars[T[j - 1][3]][2] - chars[T[i][2]][1]
            if j - i > 1 and (ln > cjkw or dur > max_dur):
                break
            if best[i] < -1e17:
                continue
            inner = (S0[j - 1] - S0[i]) if j - 1 > i else 0.0
            cutl = L1[j] if j < n else 0.0
            tot = best[i] + inner + cutl - ((ln - mu) / sig) ** 2 / 2
            if ln < 3 and n > 1:
                tot -= 1.5 * (3 - ln)                     # 避免只剩 1～2 字的碎句
            if tot > best[j]:
                best[j], bk[j] = tot, i
    cuts, j = [], n
    while j > 0:
        i = bk[j]
        cuts.append((i, j))
        j = i
    cuts.reverse()
    segs = []
    for k, (i, j) in enumerate(cuts):
        c0 = 0 if k == 0 else T[i][2]
        c1 = len(chars) if k == len(cuts) - 1 else T[j][2]
        segs.append((c0, c1))
    return segs



def build_cues(chars, maxw=30, max_dur=6.5, gap=0.3, lines=1, cjkw=15, cjk_dur=3.0, offset=0.0):
    cues = []

    def lim_of(cl):
        return (cjkw if _has_cjk(cl) else maxw) * lines

    for sent in split_clauses(chars, gap):
        sent = [c for c in sent if c]
        flat_all = [c for cl in sent for c in cl]
        if _MODEL and _has_cjk(flat_all):
            for c0, c1 in _dp_segments(flat_all, cjkw, min(max_dur, cjk_dur)):
                seg = flat_all[c0:c1]
                text = clean(seg).strip()
                real = [c for c in seg if c[0].strip() and c[0] not in ALL_PUNCT]
                if text and real:
                    cues.append({"start": real[0][1], "end": real[-1][2], "text": text,
                                 "conf": min(c[3] for c in real)})
            continue
        # 先把過長子句在最自然的位置切開
        flat = []
        for cl in sent:
            while width(clean(cl)) > lim_of(cl):
                k = _best_cut(cl, lim_of(cl))
                if k <= 0 or k >= len(cl):
                    break
                flat.append(cl[:k])
                cl = cl[k:]
            if cl:
                flat.append(cl)
        group = []

        def flush():
            if not group:
                return
            allc = [c for cl in group for c in cl]
            text = two_lines(group, maxw) if lines > 1 else " ".join(clean(x) for x in group).strip()
            if text.strip():
                cues.append({"start": allc[0][1], "end": allc[-1][2], "text": text,
                             "conf": min(c[3] for c in allc if c[0].strip() and c[0] not in ALL_PUNCT) if any(c[0] not in ALL_PUNCT for c in allc) else 1.0})
            group.clear()

        for cl in flat:
            trial = group + [cl]
            allc = [c for x in trial for c in x]
            too_wide = width(" ".join(clean(x) for x in trial)) > lim_of(allc)
            if group and _has_cjk([c for x in group for c in x]) != _has_cjk(cl) \
                    and width(" ".join(clean(x) for x in group)) >= 8 and width(clean(cl)) >= 8:
                too_wide = True                      # 中英切換且兩邊都不短：另起一句
            too_long = allc[-1][2] - allc[0][1] > (min(max_dur, cjk_dur) if _has_cjk(allc) else max_dur)
            if group and (too_wide or too_long) and not ((group[-1][-1][0] + cl[0][0]) in _WORDS2):
                flush()                              # 不在詞中間換句
            group.append(cl)
        flush()
    if offset:                                  # 整體延後（Whisper 時間戳通常偏早）
        for c in cues:
            c["start"] += offset
            c["end"] += offset
    # 時間微調：末尾留一點、避免重疊、太短的延長
    for i, c in enumerate(cues):
        nxt = cues[i + 1]["start"] if i + 1 < len(cues) else 1e9
        c["end"] = min(c["end"] + 0.15, nxt - 0.02)
        if c["end"] - c["start"] < 0.9:
            c["end"] = min(c["start"] + 0.9, nxt - 0.02)
        if c["end"] <= c["start"]:
            c["end"] = c["start"] + 0.3
    return cues


# ───────────────────────── 輸出 ─────────────────────────
def fmt_srt(t):
    ms = int(round(max(t, 0) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def fmt_mmss(t):
    t = int(t)
    if t >= 3600:
        return f"{t // 3600}:{t % 3600 // 60:02}:{t % 60:02}"
    return f"{t // 60:02}:{t % 60:02}"


def write_srt(cues, path):
    with open(path, "w", encoding="utf-8", newline="\r\n") as f:
        for i, c in enumerate(cues, 1):
            f.write(f"{i}\n{fmt_srt(c['start'])} --> {fmt_srt(c['end'])}\n{c['text']}\n\n")


_KEEP = ("YouTube", "ExileCon", "PoEDB", "DreamcoreFrank", "PoE")


def _fix_join(t):
    """修正兩句黏在一起（如 nightCouple、channelI）。"""
    keep = {}
    for i, k in enumerate(_KEEP):
        t = t.replace(k, f"\x00{i}\x00")
    t = re.sub(r"(?<=[a-z])(?=[A-Z][a-z'])", " ", t)
    t = re.sub(r"(?<=[a-z])(?=I(?:\b|'))", " ", t)
    for i, k in enumerate(_KEEP):
        t = t.replace(f"\x00{i}\x00", k)
    t = re.sub(r"(?<=\d) (?=[CDQcdq](?![A-Za-z]))", "", t)          # 2 C → 2C
    t = re.sub(r"(都|你|誰){2,}", r"\1", t)
    t = re.sub(r"([\u4e00-\u9fff]) ([\u4e00-\u9fff])", lambda m: m.group(1) + m.group(2) if (m.group(1) + m.group(2)) in _WORDS2 else m.group(0), t)   # 詞中間的空格拿掉                              # 都都都→都、你你→你
    return t


def parse_srt(path):
    raw = Path(path).read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    out = []
    for block in re.split(r"\n\s*\n", raw.strip()):
        lines = block.split("\n")
        k = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if k is None:
            continue
        a, b = [x.strip() for x in lines[k].split("-->")]

        def sec(x):
            h, m, rest = x.replace(".", ",").split(":")
            s, ms = rest.split(",")
            return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000
        out.append({"start": sec(a), "end": sec(b), "text": "\n".join(lines[k + 1:])})
    return out


def _norm(t):
    return re.sub(r"[\W_]+", "", t.lower())


_REPEAT_RE = re.compile(r"(.{2,24}?)(?:[\s，,。.、!?！？-]*\1){2,}")


def _collapse_text(text):
    return _REPEAT_RE.sub(r"\1", text.replace("\n", " "))


def wrap_plain(text, maxw):
    return two_lines([[[c, 0, 0, 1.0] for c in text]], maxw)


def _dedupe_stream(cues, maxw):
    """把所有字幕串成一條文字流，找出「同一段話連續重複 3 次以上」，只保留第 1 次。"""
    notes, chars, owner = [], [], []
    for ci, c in enumerate(cues):
        for pos, ch in enumerate(c["text"]):
            if re.match(r"[\W_]", ch):
                continue
            chars.append(ch.lower()[0])
            owner.append((ci, pos))
    text = "".join(chars)
    gone = {}
    for m in re.finditer(r"(.{2,24}?)\1{2,}", text):
        u, (a, b) = m.group(1), m.span()
        times = (b - a) // len(u)
        if len(u) * times < 9:                    # 太短（如「哈哈哈」）視為正常
            continue
        end, k = b, 0
        while end + k < len(text) and k < len(u) - 1 and text[end + k] == u[k]:
            k += 1                                # 連同結尾殘缺的半句一起刪
        for g in range(a + len(u), end + k):
            ci, pos = owner[g]
            gone.setdefault(ci, set()).add(pos)
        notes.append((cues[owner[a][0]]["start"], "重複已合併", f"「{u[:20]}」連續重複 {times} 次，只保留 1 次，請聽原音"))
    out = []
    for ci, c in enumerate(cues):
        dels = gone.get(ci)
        if not dels:
            out.append(c)
            continue
        total = sum(1 for ch in c["text"] if not re.match(r"[\W_]", ch)) or 1
        kept = total - len(dels)
        if kept <= 0:
            continue
        c = dict(c)
        t = "".join(ch for pos, ch in enumerate(c["text"]) if pos not in dels)
        t = re.sub(r"\s+", " ", t.replace("\n", " ")).strip(" ，,。.、")
        dur = c["end"] - c["start"]
        if sum(dels) / len(dels) >= len(c["text"]) / 2:
            c["end"] = c["start"] + dur * kept / total
        else:
            c["start"] = c["end"] - dur * kept / total
        c["text"] = wrap_plain(t, maxw)
        out.append(c)
    return out, notes


def dedupe_cues(cues, maxw):
    """移除 Whisper 常見的重複幻聽：同一段話連續重複、或連續 3 句以上相同。"""
    out, notes = _dedupe_stream(cues, maxw)
    res, i = [], 0
    while i < len(out):
        j = i
        key = _norm(out[i]["text"])
        while key and j + 1 < len(out) and _norm(out[j + 1]["text"]) == key:
            j += 1
        if j - i + 1 >= 3:
            notes.append((out[i]["start"], "疑似幻聽",
                          f"「{out[i]['text'].replace(chr(10), ' ')[:30]}」連續 {j - i + 1} 句相同，只保留第 1 句，請聽原音"))
            res.append(out[i])
        else:
            res.extend(out[i:j + 1])
        i = j + 1
    return res, notes


def check_cues(cues, maxw, max_dur):
    issues = []
    for i, c in enumerate(cues):
        dur = c["end"] - c["start"]
        if dur > max_dur:
            issues.append((c["start"], "過長", f"第 {i + 1} 句 {dur:.1f} 秒"))
        for line in c["text"].split("\n"):
            if width(line) > maxw + 0.01:
                issues.append((c["start"], "過寬", f"第 {i + 1} 句「{line}」"))
        if c.get("conf", 1.0) < 0.45:
            issues.append((c["start"], "低信心", f"第 {i + 1} 句「{c['text'].replace(chr(10), ' ')}」辨識信心低，請聽原音"))
    for i in range(1, len(cues)):
        gap = cues[i]["start"] - cues[i - 1]["end"]
        if gap > 15:
            issues.append((cues[i - 1]["end"], "無字幕空檔",
                           f"{fmt_mmss(cues[i - 1]['end'])}–{fmt_mmss(cues[i]['start'])} 約 {gap:.0f} 秒沒有字幕，請確認是否真的沒人說話"))
    streak = 1
    for i in range(1, len(cues)):
        streak = streak + 1 if cues[i]["text"] == cues[i - 1]["text"] else 1
        if streak == 3:
            issues.append((cues[i]["start"], "疑似幻聽", f"「{cues[i]['text']}」連續重複 3 次以上"))
    return issues


def write_review(path, auto_log, flags, issues, title):
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write(f"# {title} 校正報告\n\n")
        f.write(f"## 一、已自動替換（{len(auto_log)} 處）\n")
        for t, w, r in sorted(auto_log):
            f.write(f"[{fmt_mmss(t)}] {w} → {r}\n")
        rest = sorted(flags + issues)
        f.write(f"\n## 二、請聽原音確認（{len(rest)} 項）\n")
        for t, kind, msg in rest:
            f.write(f"[{fmt_mmss(t)}] {kind}：{msg}\n")


# ───────────────────────── 辨識 ─────────────────────────
def extract_audio(src, dst):
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-vn", "-ac", "1",
           "-ar", "16000", "-c:a", "pcm_s16le", str(dst)]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # 打包成無視窗程式時不閃黑框
    try:
        subprocess.run(cmd, check=True, creationflags=flags)
    except FileNotFoundError:
        sys.exit("找不到 ffmpeg，請先安裝並加入 PATH。")


def _add_cuda_dll_dirs():
    """pip 版 nvidia-cublas / nvidia-cudnn 的 DLL 位置加入搜尋路徑（Windows）。"""
    import os
    for base in map(Path, sys.path):
        nv = base / "nvidia"
        if nv.is_dir():
            for d in nv.glob("*/bin"):
                os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
                if hasattr(os, "add_dll_directory"):
                    try:
                        os.add_dll_directory(str(d))
                    except OSError:
                        pass


SR = 16000


def _is_degenerate(text):
    t = _norm(text)
    if len(t) >= 9 and re.search(r"(.{2,24}?)\1{2,}", t):
        return True
    if len(t) >= 40:
        import zlib
        raw = t.encode("utf-8")
        return len(raw) / max(1, len(zlib.compress(raw))) > 3.0
    return False


def _has_run(segs, n=3):
    keys = [_norm(x["t"]) for x in segs]
    for i in range(len(keys) - n + 1):
        if keys[i] and all(keys[i + k] == keys[i] for k in range(n)):
            return True
    return False


def _supported(fn, kw):
    import inspect
    try:
        params = inspect.signature(fn).parameters
        if any(p.kind == p.VAR_KEYWORD for p in params.values()):
            return kw
        return {k: v for k, v in kw.items() if k in params}
    except (TypeError, ValueError):
        return kw


def _transcribe_pass(model, audio, args, prompt, lang, strict=False, offset=0.0, verbose=True):
    kw = dict(language=lang, task="transcribe", beam_size=args.beam, best_of=5, word_timestamps=True,
              vad_filter=not strict,
              vad_parameters={"threshold": 0.4, "min_silence_duration_ms": 700, "speech_pad_ms": 500, "max_speech_duration_s": 30},
              initial_prompt=None if strict else (prompt or None),
              condition_on_previous_text=False,
              temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
              compression_ratio_threshold=2.4, log_prob_threshold=-1.0, no_speech_threshold=0.6,
              repetition_penalty=1.3 if strict else 1.05, hallucination_silence_threshold=2.0)
    if strict:
        kw["no_repeat_ngram_size"] = 4
    if lang is None:
        kw["multilingual"] = True          # 中英混雜：每一段各自判斷語言，不把英文硬翻成中文
    segs, _ = model.transcribe(audio, **_supported(model.transcribe, kw))
    out = []
    for seg in segs:
        ws = [{"w": w.word, "s": round(w.start + offset, 3), "e": round(w.end + offset, 3),
               "p": round(w.probability, 3)} for w in (seg.words or [])]
        if not ws:
            ws = [{"w": seg.text, "s": round(seg.start + offset, 3), "e": round(seg.end + offset, 3), "p": 0.5}]
        out.append({"s": seg.start + offset, "e": seg.end + offset, "t": seg.text.strip(), "words": ws})
        if verbose:
            print(f"[{fmt_mmss(seg.start + offset)}] {seg.text.strip()}", flush=True)
    return out


def _truncate(seg):
    """片段內部重複時，只保留第一次出現的部分。"""
    keep = _collapse_text(seg["t"])
    target, acc, words = len(_norm(keep)), 0, []
    for w in seg["words"]:
        if acc >= target:
            break
        words.append(w)
        acc += len(_norm(w["w"]))
    return {"s": seg["s"], "e": words[-1]["e"] if words else seg["e"], "t": keep, "words": words or seg["words"][:1]}


def _run_asr(audio, args, prompt, device, compute):
    from faster_whisper import WhisperModel
    model = WhisperModel(args.model, device=device, compute_type=compute)
    lang = None if str(args.lang).lower() in ("auto", "", "none") else args.lang
    segs = _transcribe_pass(model, audio, args, prompt, lang)
    dur, final, notes, i = len(audio) / SR, [], [], 0
    while i < len(segs):
        key = _norm(segs[i]["t"])
        j = i
        while key and j + 1 < len(segs) and _norm(segs[j + 1]["t"]) == key:
            j += 1
        if not (j - i + 1 >= 3 or _is_degenerate(segs[i]["t"])):
            final.extend(segs[i:j + 1])
            i = j + 1
            continue
        s0, e0 = segs[i]["s"], segs[j]["e"]
        print(f"[{fmt_mmss(s0)}] 偵測到重複內容，重新辨識這一段…", flush=True)
        a, b = max(0.0, s0 - 1.0), min(dur, e0 + 1.0)
        retry = _transcribe_pass(model, audio[int(a * SR):int(b * SR)], args, prompt, lang,
                                 strict=True, offset=a, verbose=False)
        retry = [r for r in retry if s0 - 0.5 <= (r["s"] + r["e"]) / 2 <= e0 + 0.5]
        if retry and not any(_is_degenerate(r["t"]) for r in retry) and not _has_run(retry):
            final.extend(retry)
            notes.append((s0, "重複已重新辨識", f"{fmt_mmss(s0)}–{fmt_mmss(e0)} 原本重複「{segs[i]['t'][:20]}」，已用嚴格設定重辨，請聽原音確認"))
        else:
            first = _truncate(segs[i]) if _is_degenerate(segs[i]["t"]) else segs[i]
            final.append(first)
            notes.append((s0, "疑似幻聽", f"{fmt_mmss(s0)}–{fmt_mmss(e0)} 重辨後仍重複「{first['t'][:20]}」，只保留一句，請聽原音（可能是雜音或多人說話）"))
        i = j + 1
    words, lines = [], []
    for sg in final:
        lines.append(f"[{fmt_mmss(sg['s'])}] {sg['t']}")
        words.extend(sg["words"])
    return words, "\n".join(lines), notes


def transcribe(media, args, prompt):
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        sys.exit("缺少 faster-whisper：pip install faster-whisper")
    _add_cuda_dll_dirs()
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "a.wav"
        extract_audio(media, wav)
        # 自己讀取音訊，避開 faster-whisper 內部 PyAV 版本不合的問題
        import wave
        import numpy as np
        with wave.open(str(wav), "rb") as wf:
            audio = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
        print(f"音訊長度 {fmt_mmss(len(audio) / SR)}（{len(audio) / SR:.0f} 秒）", flush=True)
        try:
            return _run_asr(audio, args, prompt, args.device, args.compute)
        except (RuntimeError, ValueError, OSError) as e:
            msg = str(e).lower()
            if args.device != "cpu" and any(k in msg for k in ("cublas", "cudnn", "cuda", "dll", "library")):
                print(f"\n顯示卡加速無法使用（{e}）\n改用 CPU 重新辨識，速度會慢很多，請耐心等候…\n", flush=True)
                return _run_asr(audio, args, prompt, "cpu", "int8")
            raise


# ───────────────────────── 主流程 ─────────────────────────
def get_converter():
    try:
        from opencc import OpenCC
        return OpenCC("s2tw")
    except Exception:
        print("提醒：未安裝 opencc，不做簡轉繁（pip install opencc-python-reimplemented）。", file=sys.stderr)
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="流亡黯道 繁體中文字幕工具")
    ap.add_argument("input", help="mp4/mp3/wav 等影音檔，或 原文.json，或 .srt")
    ap.add_argument("--vocab", default="火炬和流亡詞彙庫.csv", help="詞庫 CSV 路徑")
    ap.add_argument("--game", choices=list(GAME_SETS), default="poe", help="poe=1+2（預設）/poe1/poe2/torchlight")
    ap.add_argument("--out-dir", default="output")
    ap.add_argument("--model", default="large-v3")
    ap.add_argument("--lang", default="auto", help="auto=中英混雜自動判斷（預設）/zh/en")
    ap.add_argument("--device", default="auto")
    ap.add_argument("--compute", default="auto")
    ap.add_argument("--beam", type=int, default=5)
    ap.add_argument("--prompt", default=None, help="自訂提示詞；預設用詞庫產生短提示")
    ap.add_argument("--no-prompt", action="store_true", help="不給提示詞（提示詞可能造成幻聽）")
    ap.add_argument("--max-width", type=int, default=30, help="每行字寬上限（中文字=1，英數=0.5；預設 30＝約 60 個英文字母）")
    ap.add_argument("--max-dur", type=float, default=6.5, help="單句字幕秒數上限")
    ap.add_argument("--gap", type=float, default=0.3, help="超過幾秒停頓就切句")
    ap.add_argument('--version', action='version', version='字幕工具 v' + VERSION)
    ap.add_argument("--offset", type=float, default=0.2, help="字幕整體延後秒數（預設 0.2；字幕偏早就調大、偏晚就調小或負數）")
    ap.add_argument("--cjk-width", type=int, default=15, help="含中文的句子每行字數上限（預設 14，依你的剪映習慣：平均約 10 字）")
    ap.add_argument("--lines", type=int, default=1, choices=[1, 2], help="每句字幕行數（預設 1，與剪映相同）")
    args = ap.parse_args(argv)
    print('字幕工具 v' + VERSION)

    src = Path(args.input)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = src.stem.replace(" 原文", "")
    vocab = Vocab(args.vocab, args.game)
    _PROTECT.update(vocab.protect)
    _PROTECT.update(v[0] for v in vocab.auto.values() if re.fullmatch(f"[{CJK}]{{2,8}}", v[0]))
    conv = get_converter()
    auto_log, flags = [], []
    ext = src.suffix.lower()

    if ext == ".srt":                                   # 只校正既有 SRT
        cues = parse_srt(src)
        for c in cues:
            u = words_to_units([{"w": _fix_join(c["text"].replace("\n", " ")), "s": c["start"], "e": c["end"], "p": 1.0}])
            to_traditional(u, conv)
            u = apply_auto(u, vocab, auto_log)
            c["text"] = wrap_plain("".join(x[0] for x in u), args.max_width) if args.lines > 1 else " ".join("".join(x[0] for x in u).split())
            flags += find_flags(c["text"], lambda i, c=c: c["start"], vocab)
        cues, dnotes = dedupe_cues(cues, args.max_width if args.lines > 1 else 1000)
        issues = check_cues(cues, args.max_width if args.lines > 1 else 1000, args.max_dur) + dnotes
    else:
        notes = []
        if ext == ".json":
            words = json.loads(src.read_text(encoding="utf-8"))
        else:
            prompt = None if args.no_prompt else (args.prompt or vocab.prompt(args.lang))
            words, txt, notes = transcribe(src, args, prompt)
            (out / f"{stem} 原文.json").write_text(json.dumps(words, ensure_ascii=False), encoding="utf-8")
            (out / f"{stem} 原文.txt").write_text(txt, encoding="utf-8-sig")
        units = words_to_units(words)
        to_traditional(units, conv)
        units = apply_auto(units, vocab, auto_log)
        chars = units_to_chars(units)
        text = "".join(c[0] for c in chars)
        flags = find_flags(text, lambda i: chars[min(i, len(chars) - 1)][1], vocab)
        cues = build_cues(chars, args.max_width, args.max_dur, args.gap, args.lines, args.cjk_width, offset=args.offset)
        for c in cues:
            c["text"] = _fix_join(c["text"])      # 黏字、2 C→2C、都都都→都、詞中空格
        cues, dnotes = dedupe_cues(cues, args.max_width if args.lines > 1 else 1000)
        issues = check_cues(cues, max(args.max_width, args.cjk_width) * args.lines, args.max_dur) + dnotes + notes

    write_srt(cues, out / f"{stem} 字幕.srt")
    write_review(out / f"{stem} 字幕.review.txt", auto_log, flags, issues, stem)
    print(f"完成：{len(cues)} 句字幕，自動替換 {len(auto_log)} 處，待確認 {len(flags) + len(issues)} 項。")
    print(f"輸出資料夾：{out.resolve()}")


if __name__ == "__main__":
    main()
