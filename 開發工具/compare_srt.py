# -*- coding: utf-8 -*-
"""
比對「工具直出 SRT」與「人工改好 SRT」：
  python 開發工具/compare_srt.py 直出.srt 人工.srt
輸出：每句字數／秒數統計、斷點一致率（P/R/F1）、所有文字改動（依次數排序）。
"""
import collections
import difflib
import re
import sys


def load(p):
    s = open(p, encoding="utf-8-sig").read().replace("\r", "")
    out = []
    for b in s.strip().split("\n\n"):
        lines = b.split("\n")
        if len(lines) >= 3 and "-->" in lines[1]:
            t0, t1 = lines[1].split(" --> ")
            f = lambda t: int(t[:2]) * 3600 + int(t[3:5]) * 60 + int(t[6:8]) + int(t[9:12]) / 1000  # noqa: E731
            out.append((f(t0), f(t1), " ".join(lines[2:]).strip()))
    return out


def n(t):
    return len(re.sub(r"\s", "", t))


def stream(cues):
    s, b = "", set()
    for _, _, t in cues:
        if s:
            b.add(len(s))
        s += re.sub(r"\s", "", t)
    return s, b


def main(raw_p, fix_p):
    R, F = load(raw_p), load(fix_p)
    for name, C in (("直出", R), ("人工", F)):
        L = sorted(n(t) for _, _, t in C)
        D = sorted(b - a for a, b, _ in C)
        print(f"{name}：{len(C)} 句，每句平均 {sum(L) / len(L):.1f} 字（P90 {L[int(len(L) * .9)]}、最長 {L[-1]}），"
              f"平均 {sum(D) / len(D):.2f} 秒（P90 {D[int(len(D) * .9)]:.2f}）")
    rs, rb = stream(R)
    fs, fb = stream(F)
    sm = difflib.SequenceMatcher(None, rs, fs, autojunk=False)
    m = {}
    for a, b, k in sm.get_matching_blocks():
        for j in range(k):
            m[a + j] = b + j
    bm = {m[x] for x in rb if x in m}
    tp = len(bm & fb)
    p, r = tp / max(1, len(bm)), tp / max(1, len(fb))
    print(f"文字相似度 {sm.ratio():.3f}；斷點 P {p:.3f}  R {r:.3f}  F1 {2 * p * r / max(1e-9, p + r):.3f}"
          f"（P 低＝工具切太多，R 低＝工具切太少）")
    reps = collections.Counter()
    for op, a1, a2, b1, b2 in sm.get_opcodes():
        if op != "equal":
            reps[(rs[a1:a2], fs[b1:b2])] += 1
    print(f"\n文字改動 {sum(reps.values())} 處：")
    for (a, b), c in reps.most_common():
        print(f"{c:3d}  [{a}] → [{b}]")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
