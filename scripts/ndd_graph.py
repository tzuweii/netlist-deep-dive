#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨板圖：連接器對接驗證 + 端到端訊號追跡。

netlist 本身**沒有跨板連線**。要追一條從 A 板走到 C 板的訊號，必須先假設連接器
兩側的腳位如何對應。本模組做兩件事：

1. `verify_mating()` —— **枚舉所有合理的對應方式並排名**，不是只檢查「兩側樣式
   相符」。後者是陷阱：對稱的 GND 分布會讓幾十種錯誤對應全部「通過」。
2. `trace()` —— 把三（或 N）塊板接成一張圖走 BFS，輸出完整中繼元件。

════════════════════════════════════════════════════════════════════════════
⚠️ 三個一定要記住的界線：

  * **netlist 證明「接線意圖」，layout 證明「實體位置」，只有系統行為能證明
    「兩者都對」。** netlist 不含 footprint 方位，所以 layout 若把連接器鏡像
    放置，netlist 一個字都不會變，所有名稱照樣完美對上——netlist 原理上偵測
    不到這種錯誤。排名結果再漂亮，都只是「設計意圖」的證據。
  * **要分辨對接的物理型式**：兩側若都是公頭（terminal），中間必然有線束，
    腳位對應由線束決定；若是公母直接板對板，就沒有線束問題，只剩 footprint
    方位，答案在 layout 檔裡。這兩種的定案途徑完全不同。
  * **殘存的候選要用「會不會壞」來排除**：把每個殘存對應代入，看它會不會造成
    立即而明顯的故障（例如 SDA/SCL 對調 → I2C 全滅）。若系統實際會動，該候選
    就被排除了。這通常比找線束圖快。
════════════════════════════════════════════════════════════════════════════
"""
import itertools
import re
from collections import deque

from ndd_models import pairs_for, match_model


class Fabric(object):
    def __init__(self, boards, mates, models, power_rx, net_normalize=None):
        """boards: {key: (Netlist, Bom)}；mates: [(ba,ra,bb,rb), ...]"""
        self.nl = {k: v[0] for k, v in boards.items()}
        self.bom = {k: v[1] for k, v in boards.items()}
        self.models = models
        self.power_rx = re.compile(power_rx, re.I) if power_rx else None
        self.norm_rules = [(re.compile(a), b) for a, b in (net_normalize or [])]
        self.mates = mates
        self.mate = {}
        for ba, ra, bb, rb in mates:
            for p in self.nl[ba].pins(ra):
                if p in self.nl[bb].pins(rb):
                    self.mate.setdefault((ba, ra, p), []).append((bb, rb, p))
                    self.mate.setdefault((bb, rb, p), []).append((ba, ra, p))

    # ---- 工具 -------------------------------------------------------------
    def is_power(self, net):
        return bool(net and self.power_rx and self.power_rx.match(net))

    def norm(self, n):
        if n is None:
            return None
        for rx, rep in self.norm_rules:
            n = rx.sub(rep, n)
        return n

    @staticmethod
    def cls(n):
        """腳位類別 —— 跨板唯一可靠的不變量。net 名稱兩側可能不同，但一支腳
        是 GND / 某條電源 / 訊號，不會因為換一塊板就改變。"""
        if n is None:
            return None
        u = n.upper()
        if u == "GND" or u.endswith("_GND"):
            return "GND"
        if u.endswith(("_EN", "_PG", "_PGOOD")):        # 名字帶 VDD 但其實是控制訊號
            return "SIG"
        m = re.search(r"(\d+V\d+|\d+V\b)", u)
        if m and ("VDD" in u or "VCC" in u or u.startswith(("+", "V"))):
            return "PWR:" + m.group(1)
        if "VDD" in u or "VCC" in u:
            return "PWR"
        return "SIG"

    def _pn(self, board, refdes):
        b = self.bom.get(board)
        return (b.pn(refdes) or "") if b else ""

    # ---- 對接驗證 ---------------------------------------------------------
    def _candidates(self, pins):
        """依腳位命名方式產生所有合理的對應候選。"""
        if all(p.isdigit() for p in pins):
            n = len(pins)
            half = n // 2
            return [
                ("直通 n->n", lambda p: p),
                ("換排(奇偶互換)",
                 lambda p: str(int(p) + 1 if int(p) % 2 else int(p) - 1)),
                ("整體反轉", lambda p: str(n + 1 - int(p))),
                ("同排反轉", lambda p: str(half + 1 - int(p) if int(p) <= half
                                           else 3 * half + 1 - int(p))),
            ]
        rows = sorted({p[0] for p in pins})
        ncol = max(int(p[1:]) for p in pins)
        out = []
        for perm in itertools.permutations(rows):
            for rev in (False, True):
                def fn(p, perm=perm, rev=rev):
                    r, i = p[0], int(p[1:])
                    return "%s%d" % (perm[rows.index(r)],
                                     ncol + 1 - i if rev else i)
                out.append(("%s->%s %s" % ("".join(rows), "".join(perm),
                                           "反轉" if rev else "正向"), fn))
        return out

    def rank_mating(self, ba, ra, bb, rb):
        pa, pb = self.nl[ba].pins(ra), self.nl[bb].pins(rb)
        res = []
        for label, fn in self._candidates(list(pa)):
            bad = match = 0
            for p in pa:
                try:
                    q = fn(p)
                except Exception:
                    continue
                ca, cb = self.cls(pa.get(p)), self.cls(pb.get(q))
                if ca is not None and cb is not None and ca != cb:
                    bad += 1
                if self.norm(pa.get(p)) == self.norm(pb.get(q)):
                    match += 1
            res.append((bad, -match, label))
        res.sort()
        return res, len(pa)

    def verify_mating(self, verbose=True):
        report = []
        for ba, ra, bb, rb in self.mates:
            rank, n = self.rank_mating(ba, ra, bb, rb)
            (b1, m1, l1), (b2, m2, l2) = rank[0], rank[1]
            straight = l1.startswith("直通") or re.match(r"^(\w+)->\1 正向$", l1)
            ok = bool(straight) and b1 == 0 and (b2 > 0 or -m1 > -m2)
            row = dict(a="%s.%s" % (ba, ra), b="%s.%s" % (bb, rb), pins=n,
                       best=l1, best_bad=b1, best_match=-m1,
                       second=l2, second_bad=b2, second_match=-m2,
                       margin=(-m1) - (-m2), ok=ok,
                       kind=self._mate_kind(ba, ra, bb, rb))
            report.append(row)
            if verbose:
                print("  %s <-> %s  (%d pin, %s)" % (row["a"], row["b"], n, row["kind"]))
                print("        最佳 %-24s 矛盾 %d 腳, 語意相符 %d"
                      % (l1, b1, -m1))
                print("        次佳 %-24s 矛盾 %d 腳, 語意相符 %d   -> %s"
                      % (l2, b2, -m2,
                         "直通唯一勝出（margin %d）" % row["margin"] if ok
                         else "!! 無法唯一判定，需 layout 或實測"))
        return report

    def _mate_kind(self, ba, ra, bb, rb):
        """判斷是『直接板對板』還是『中間有線束』——兩者定案途徑不同。"""
        fa = self.nl[ba].parts.get(ra, "")
        fb = self.nl[bb].parts.get(rb, "")
        base = lambda s: re.sub(r"^Conn_", "", s).split("-")[0].upper()
        if base(fa) and base(fa) == base(fb):
            return "兩側同型 -> 很可能中間有線束，腳位對應由線束決定"
        return "公母直接對接 -> 無線束，只剩 footprint 方位問題（查 layout）"

    # ---- 走圖 -------------------------------------------------------------
    def neighbours(self, node):
        board, rd, pin = node
        nl = self.nl[board]
        out = []
        net = nl.pin_net(rd, pin)
        if net and not self.is_power(net):
            for rd2, p2 in nl.net(net):
                if (rd2, p2) != (rd, pin):
                    out.append(((board, rd2, p2), "net:%s" % net))
        name, pairs = pairs_for(self.models, nl.parts.get(rd, ""),
                                self._pn(board, rd), len(nl.pins(rd)))
        if pairs:
            for a, b in pairs:
                if pin == a:
                    out.append(((board, rd, b), "thru:%s|%s %s->%s" % (rd, name, a, b)))
                elif pin == b:
                    out.append(((board, rd, a), "thru:%s|%s %s->%s" % (rd, name, b, a)))
        for m in self.mate.get((board, rd, pin), []):
            out.append((m, "mate"))
        return out

    def trace(self, start, stop_fn, max_depth=16):
        seen = {start: None}
        q = deque([(start, 0)])
        ends = {}
        while q:
            node, d = q.popleft()
            if d >= max_depth:
                continue
            for nxt, why in self.neighbours(node):
                if nxt in seen:
                    continue
                seen[nxt] = (node, why)
                if stop_fn(nxt):
                    ends[nxt] = self._path(seen, nxt)
                else:
                    q.append((nxt, d + 1))
        return ends

    @staticmethod
    def _path(seen, node):
        out = []
        while seen.get(node):
            prev, why = seen[node]
            out.append((prev, why, node))
            node = prev
        return list(reversed(out))

    def hop_string(self, path):
        steps = []
        for _prev, why, node in path:
            b, rd, pin = node
            if why.startswith("thru:"):
                body = why.split(":", 1)[1]
                rdname, rest = body.split("|", 1)
                # 模型名稱可能含空格（例如 "2-pin passive"），所以從右邊切
                model, pins = rest.rsplit(" ", 1)
                steps.append("%s %s(%s) %s" % (b.upper(), rdname, model, pins))
            elif why == "mate":
                steps.append(">> %s %s.%s" % (b.upper(), rd, pin))
        return " | ".join(steps)

    def is_terminal(self, node):
        b, rd, _p = node
        nl = self.nl[b]
        if nl.is_mech(rd) or rd.startswith("J"):
            return False
        fp = nl.parts.get(rd, "")
        _n, pairs = pairs_for(self.models, fp, self._pn(b, rd), len(nl.pins(rd)))
        return pairs is None
