# -*- coding: utf-8 -*-
"""
用 YouTube CC（使用者親手做的字幕）重新訓練斷句模型。

用法：
  python 開發工具/train_cc.py devilcat_cc.json            訓練＋評估（不覆寫）
  python 開發工具/train_cc.py devilcat_cc.json --write    評估後寫入 斷句模型.json

CC 檔格式（由瀏覽器從 YouTube 轉錄稿收集）：
  {影片ID: {"t": 標題, "len": 秒數, "rows": ["0:12\t字幕文字", ...]}}
"""
import json
import math
import random
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
import poe_subtitle as ps  # noqa: E402

CHAR_SEC = 0.16          # 平均每字約 0.16 秒（剪映成品統計：10 字 1.6 秒）


def parse_ts(s):
    p = [int(x) for x in s.split(":")]
    return p[0] * 3600 + p[1] * 60 + p[2] if len(p) == 3 else p[0] * 60 + p[1]


def load_clean(path, verbose=True):
    d = json.load(open(path, encoding="utf-8"))
    vids = {}
    for vid, v in d.items():
        rows = []
        for r in v["rows"]:
            if "\t" not in r:
                continue
            ts, tx = r.split("\t", 1)
            tx = tx.strip()
            if not tx or not re.match(r"^\d+(:\d+){1,2}$", ts):
                continue
            rows.append((parse_ts(ts), tx))
        if not rows:
            continue
        ls = sorted(len(re.sub(r"\s", "", t)) for _, t in rows)
        med = ls[len(ls) // 2]
        dens = len(rows) / max(1, v["len"])
        ok = med <= 12 and dens >= 0.25
        if verbose and not ok:
            print(f"  略過 {vid}（中位數 {med} 字、每秒 {dens:.2f} 行）{v['t'][:24]}")
        if ok:
            vids[vid] = rows
    return vids


def stretches(rows, max_cues=8):
    """把時間上連續的字幕行串成一段（模擬同一口氣），回傳 [[行文字, ...], ...]。"""
    out, cur = [], []
    for k, (t, tx) in enumerate(rows):
        cur.append(tx)
        nxt = rows[k + 1][0] if k + 1 < len(rows) else None
        n = len(re.sub(r"\s", "", tx))
        cont = nxt is not None and (nxt - t) <= CHAR_SEC * n + 1.0
        if not cont or len(cur) >= max_cues:
            if len(cur) >= 2:
                out.append(cur)
            cur = []
    return out


def to_tokens(lines):
    """回傳 (tokens, 真正斷點集合)；斷點以 token 索引表示（在 token i 之前斷開）。"""
    chars, t = [], 0.0
    bounds_char = []
    for ln in lines:
        bounds_char.append(len(chars))
        for ch in ln:
            chars.append([ch, t, t + CHAR_SEC, 1.0])
            t += CHAR_SEC
    T = ps._tokenize(chars)
    first_tok = {}
    for i, tk in enumerate(T):
        first_tok.setdefault(tk[2], i)
    cuts = set()
    starts = [tk[2] for tk in T]
    for b in bounds_char[1:]:
        # 斷點 = 第一個 起始字 >= b 的 token
        for i, s in enumerate(starts):
            if s >= b:
                if i > 0:
                    cuts.add(i)
                break
    return chars, T, cuts


def feats(T, i):
    g = lambda k: T[k][0] if 0 <= k < len(T) else "^"  # noqa: E731
    a, b, a2, b2 = g(i - 1), g(i), g(i - 2), g(i + 1)
    return {"a=" + a: 1, "b=" + b: 1, "ab=" + a + b: 1, "a2a=" + a2 + a: 1, "bb2=" + b + b2: 1,
            "a2ab=" + a2 + a + b: 1, "abb2=" + a + b + b2: 1, "ka=" + ps._tkind(a) + ps._tkind(b): 1}


def build_xy(vids):
    X, y = [], []
    for rows in vids.values():
        for st in stretches(rows):
            _, T, cuts = to_tokens(st)
            for i in range(1, len(T)):
                X.append(feats(T, i))
                y.append(1 if i in cuts else 0)
    return X, y


def train(X, y, C=1.0):
    from sklearn.feature_extraction import DictVectorizer
    from sklearn.linear_model import LogisticRegression
    dv = DictVectorizer()
    Xm = dv.fit_transform(X)
    lr = LogisticRegression(C=C, max_iter=3000, solver="lbfgs")
    lr.fit(Xm, y)
    w = {f: round(float(c), 2) for f, c in zip(dv.get_feature_names_out(), lr.coef_[0]) if abs(c) >= 0.1}
    return round(float(lr.intercept_[0]), 3), w


def evaluate(vids, model, cjkw=15):
    """用工具實際的動態規劃斷句，比較預測斷點和使用者真實斷點（只看連續說話、沒有停頓提示的情況）。"""
    old = ps._MODEL
    ps._MODEL = model
    tp = fp = fn = 0
    lens = []
    try:
        for rows in vids.values():
            for st in stretches(rows):
                chars, T, cuts = to_tokens(st)
                segs = ps._dp_segments(chars, cjkw, 99.0)
                pred = set()
                starts = [tk[2] for tk in T]
                for c0, _ in segs[1:]:
                    for i, s in enumerate(starts):
                        if s >= c0:
                            pred.add(i)
                            break
                for c0, c1 in segs:
                    lens.append(len(re.sub(r"\s", "", "".join(c[0] for c in chars[c0:c1]))))
                tp += len(pred & cuts)
                fp += len(pred - cuts)
                fn += len(cuts - pred)
    finally:
        ps._MODEL = old
    p = tp / max(1, tp + fp)
    r = tp / max(1, tp + fn)
    f = 2 * p * r / max(1e-9, p + r)
    lens.sort()
    return {"P": round(p, 3), "R": round(r, 3), "F1": round(f, 3),
            "平均字數": round(sum(lens) / max(1, len(lens)), 1), "P90字數": lens[int(len(lens) * 0.9)] if lens else 0}


def main():
    path = sys.argv[1]
    vids = load_clean(path)
    keys = sorted(vids)
    random.Random(7).shuffle(keys)
    ntest = max(5, len(keys) // 6)
    test = {k: vids[k] for k in keys[:ntest]}
    trainv = {k: vids[k] for k in keys[ntest:]}
    print(f"可用影片 {len(vids)} 支：訓練 {len(trainv)}、測試 {len(test)}")
    X, y = build_xy(trainv)
    print(f"訓練樣本 {len(y)}（斷點 {sum(y)}）")
    b, w = train(X, y)
    new = (b, w)
    res_old = evaluate(test, ps._MODEL)
    res_new = evaluate(test, new)
    blend_w = dict(ps._MODEL[1])
    for k, v in w.items():
        blend_w[k] = round(blend_w.get(k, 0.0) * 0.4 + v * 0.6, 2)
    for k in ps._MODEL[1]:
        if k not in w:
            blend_w[k] = round(ps._MODEL[1][k] * 0.4, 2)
    blend = (round(ps._MODEL[0] * 0.4 + b * 0.6, 3), {k: v for k, v in blend_w.items() if abs(v) >= 0.1})
    res_blend = evaluate(test, blend)
    print("舊模型（剪映 13 部）:", res_old)
    print("新模型（YouTube CC）:", res_new)
    print("混合 40/60       :", res_blend)
    if "--write" in sys.argv:
        best = max([("old", res_old, ps._MODEL), ("new", res_new, new), ("blend", res_blend, blend)],
                   key=lambda x: x[1]["F1"])
        if best[0] == "old":
            print("舊模型最好，不覆寫。")
            return
        # 用全部影片重訓再輸出
        if best[0] == "new":
            Xa, ya = build_xy(vids)
            b, w = train(Xa, ya)
            out = (b, w)
        else:
            Xa, ya = build_xy(vids)
            b2, w2 = train(Xa, ya)
            bw = {}
            for k in set(ps._MODEL[1]) | set(w2):
                v = ps._MODEL[1].get(k, 0.0) * 0.4 + w2.get(k, 0.0) * 0.6
                if abs(v) >= 0.1:
                    bw[k] = round(v, 2)
            out = (round(ps._MODEL[0] * 0.4 + b2 * 0.6, 3), bw)
        json.dump({"說明": f"由使用者成品字幕訓練的『此處斷開機率』模型（logistic）；特徵＝前後字組合；"
                          f"資料：剪映成品 13 部＋YouTube CC {len(vids)} 支（{best[0]}）",
                   "b": out[0], "w": out[1]},
                  open(BASE / "斷句模型.json", "w", encoding="utf-8"), ensure_ascii=False)
        print(f"已寫入 斷句模型.json（{best[0]}，{len(out[1])} 個特徵）")


if __name__ == "__main__":
    main()
