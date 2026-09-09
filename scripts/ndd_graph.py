#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨板圖：連接器對接驗證 + 端到端訊號追跡 + 功能拓樸提示。

netlist 本身**沒有跨板連線**。要追一條從 A 板走到 C 板的訊號，必須先假設連接器
兩側的腳位如何對應。

════════════════════════════════════════════════════════════════════════════
本模組維護**兩張互不相通的圖**：

  trace graph —— 只含已驗證的 signal_transfer 邊、net 邊與已批准的 mate 邊。
                 這是 `signal_chain.csv` 的來源，可作為架構結論。
  hint  graph —— control influence、stateful 行為、參數控制、未知邊界。
                 **永不得作為 BFS 的下一跳，也不得被敘述為 net 連通。**

  「能控制它」不等於「訊號穿過它」。latch/reset/select 腳會改變元件行為，
  但不構成訊號路徑。兩者都要輸出，只有前者能進正式 trace。
════════════════════════════════════════════════════════════════════════════

⚠️ 三個一定要記住的界線：

  * **netlist 證明「接線意圖」，layout 證明「實體位置」，只有系統行為能證明
    「兩者都對」。** layout 若把連接器鏡像放置，netlist 一個字都不會變。
  * **要分辨對接的物理型式**：兩側同型 → 中間必然有線束；公母直接對接 →
    只剩 footprint 方位。兩者的定案途徑完全不同。
  * **殘存候選要用「會不會壞」排除**：代入後看會不會造成立即而明顯的故障。
    系統若實際會動，該候選就被排除了。
"""
import itertools
import re
from collections import deque

import ndd_confidence as C
import ndd_package
from ndd_models import (control_influences, derive_gating, missing_pins,
                        outgoing, transfer_edges)

# endpoint 分類 —— 「沒有模型」**不等於**「訊號在此結束」。
EP_TERMINAL = "terminal(declared)"
EP_STATEFUL = "stateful(declared)"
EP_UNKNOWN_DECLARED = "unknown_stop(declared)"
EP_UNKNOWN_UNUSABLE = "unknown_stop(model_unusable)"
EP_UNCLASSIFIED = "unclassified"
# 進 loads 的端點種類：unknown_stop 不算，因為它是「停在具名位置」不是負載。
EP_IN_LOADS = (EP_TERMINAL, EP_STATEFUL, EP_UNCLASSIFIED)

class MateMapError(Exception):
    """已批准的對接對映本身有問題 —— 載入時就要擋，不能等到走圖。"""


_BLOCKING = {"package:unresolved": "package_unresolved",
             "package:conflict": "package_conflict",
             "model:ambiguous": "model_ambiguous",
             "model:pin_absent": "pin_absent"}


class Fabric(object):
    def __init__(self, boards, mates, models, power_rx, net_normalize=None,
                 mate_map=None, endpoints=None, part_package=None):
        """boards: {key: (Netlist, Bom)}；mates: [(ba,ra,bb,rb), ...]"""
        self.nl = {k: v[0] for k, v in boards.items()}
        self.bom = {k: v[1] for k, v in boards.items()}
        self.models = models
        self.power_rx = re.compile(power_rx, re.I) if power_rx else None
        self.norm_rules = [(re.compile(a), b) for a, b in (net_normalize or [])]
        self.mates = [tuple(m) for m in mates]
        self.mate_map = mate_map or {}
        self.endpoints = endpoints or {}
        self.part_package = part_package or {}
        self.hints = []                 # hint graph：只輸出，不走
        self.pkg_res = {}               # (board, refdes) -> 封裝判定與證據
        self._model_cache = {}
        self._build_mates()

    # ---- 對接邊 -----------------------------------------------------------
    @staticmethod
    def mate_key(ba, ra, bb, rb):
        return "%s:%s|%s:%s" % (ba, ra, bb, rb)

    def _approved(self, ba, ra, bb, rb):
        """回傳 (map, 反向?) 或 (None, False)。兩種宣告順序都要找得到。"""
        m = self.mate_map.get(self.mate_key(ba, ra, bb, rb))
        if m:
            return m, False
        m = self.mate_map.get(self.mate_key(bb, rb, ba, ra))
        if m:
            return m, True
        return None, False

    @staticmethod
    def _validate_map(key, amap, pa, pb):
        """已批准的對映必須先被驗證，否則「批准」只是把錯誤升級成結論。

        ⚠️ **單射檢查最重要**：兩個 A 腳映到同一個 B 腳會**靜默合併兩條 net**，
           而合併後的圖看起來完全正常。這是所有 mapping 錯誤裡後果最大的一種。
        """
        errs = []
        missing_a = [p for p in amap if p not in pa]
        missing_b = [q for q in amap.values() if q not in pb]
        if missing_a:
            errs.append("A 側不存在的 pin：%s" % ", ".join(sorted(missing_a)[:8]))
        if missing_b:
            errs.append("B 側不存在的 pin：%s" % ", ".join(sorted(missing_b)[:8]))
        seen = {}
        dup = []
        for p, q in amap.items():
            if q in seen:
                dup.append("%s 與 %s 都映到 %s" % (seen[q], p, q))
            seen[q] = p
        if dup:
            errs.append("非單射（會靜默合併 net）：%s" % "；".join(dup[:5]))
        uncovered_a = [p for p in pa if p not in amap]
        if uncovered_a:
            errs.append("A 側未涵蓋的 pin：%s" % ", ".join(sorted(uncovered_a)[:8]))
        if errs:
            raise MateMapError(
                "mate_map `%s` 驗證失敗：\n  - %s\n"
                "  （未涵蓋的腳若確實不對接，請在 approved 裡明確省略並於 "
                "evidence 說明；工具不會替你假設。）" % (key, "\n  - ".join(errs)))
        uncovered_b = [q for q in pb if q not in set(amap.values())]
        return uncovered_b

    def _build_mates(self):
        self.mate = {}
        self.mate_status = {}
        self.mate_uncovered = {}
        for ba, ra, bb, rb in self.mates:
            spec, rev = self._approved(ba, ra, bb, rb)
            pa, pb = self.nl[ba].pins(ra), self.nl[bb].pins(rb)
            if spec:
                amap = {str(k): str(v) for k, v in spec["approved"].items()}
                if rev:
                    amap = {v: k for k, v in amap.items()}
                key = self.mate_key(ba, ra, bb, rb)
                self.mate_uncovered[key] = self._validate_map(key, amap, pa, pb)
                pairs = [(p, q) for p, q in amap.items()]
                cav = []
                self.mate_status[(ba, ra, bb, rb)] = "approved"
            else:
                # ⚠️ 無批准 map 時暫退回「同 pin label 對接」以保留探索能力，
                #    但**每一條這種邊都必須帶 caveat**：排名結果尚未進入定案，
                #    這是候選路徑，不是已確認的線束／板對板結論。
                pairs = [(p, p) for p in pa if p in pb]
                cav = ["mate:unapproved"]
                self.mate_status[(ba, ra, bb, rb)] = "unapproved"
            for p, q in pairs:
                if p in pa and q in pb:
                    self.mate.setdefault((ba, ra, p), []).append(((bb, rb, q), cav))
                    self.mate.setdefault((bb, rb, q), []).append(((ba, ra, p), cav))

    def mate_partners(self, board, refdes):
        """對接對手查詢 —— **唯一**的實作。

        曾經有兩處各自實作、其中一處只查正向，導致以相反順序宣告的 mate 讓整條
        訊號從 CSV 靜默消失。任何呼叫點都必須用這個 helper。
        """
        out = []
        for ba, ra, bb, rb in self.mates:
            if (ba, ra) == (board, refdes):
                out.append((bb, rb))
            elif (bb, rb) == (board, refdes):
                out.append((ba, ra))
        return out

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
        """腳位類別 —— 跨板唯一可靠的不變量。"""
        if n is None:
            return None
        u = n.upper()
        if u == "GND" or u.endswith("_GND"):
            return "GND"
        if u.endswith(("_EN", "_PG", "_PGOOD")):    # 名字帶 VDD 但其實是控制訊號
            return "SIG"
        m = re.search(r"(\d+V\d+|\d+V\b)", u)
        if m and ("VDD" in u or "VCC" in u or u.startswith(("+", "V"))):
            return "PWR:" + m.group(1)
        if "VDD" in u or "VCC" in u:
            return "PWR"
        return "SIG"

    def _pn(self, board, refdes):
        b = self.bom.get(board)
        if b is None:
            return ""
        pn = b.pn(refdes)
        return pn or ""

    def package_of(self, board, refdes):
        """惰性 package 解析：只查已宣告的例外表，不做任何推定。

        ⚠️ **不得以已接腳數推定 package** —— `pins()` 只含已接腳，系統性低估，
           會穩定偏向較小的封裝。
        """
        key = "%s:%s" % (board, refdes)
        if key in self.part_package:
            return self.part_package[key]
        return self.part_package.get(self._pn(board, refdes))

    # ---- 對接驗證 ---------------------------------------------------------
    def _candidates(self, pins):
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
            status = self.mate_status.get((ba, ra, bb, rb), "unapproved")
            row = dict(a="%s.%s" % (ba, ra), b="%s.%s" % (bb, rb), pins=n,
                       best=l1, best_bad=b1, best_match=-m1,
                       second=l2, second_bad=b2, second_match=-m2,
                       margin=(-m1) - (-m2), ok=ok, status=status,
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
                print("        批准狀態 %s%s"
                      % (status,
                         "" if status == "approved"
                         else " -> 下游每一列都會帶 mate:unapproved（候選路徑，"
                              "不是已確認對接）"))
        return report

    def _mate_kind(self, ba, ra, bb, rb):
        fa = self.nl[ba].parts.get(ra, "")
        fb = self.nl[bb].parts.get(rb, "")
        base = lambda s: re.sub(r"^Conn_", "", s).split("-")[0].upper()
        if base(fa) and base(fa) == base(fb):
            return "兩側同型 -> 很可能中間有線束，腳位對應由線束決定"
        return "公母直接對接 -> 無線束，只剩 footprint 方位問題（查 layout）"

    # ---- 模型解析 ---------------------------------------------------------
    def _model_at(self, board, refdes):
        """回傳 (name, model, edges, caveats)。

        封裝判定**只用 netlist 與 BOM**（`ndd_package.resolve`）：模型宣告的
        電源／接地腳實際接在哪、訂購碼後綴、footprint 名稱。推論出來的會帶
        `package:inferred`，可以用但要標 `[?]`；證據不足則不給 edges。
        """
        key = (board, refdes)
        if key in self._model_cache:
            return self._model_cache[key]
        nl, bom = self.nl[board], self.bom[board]
        declared = self.package_of(board, refdes)
        res = ndd_package.resolve(self.models, nl, bom, board, refdes,
                                  self.cls, declared)
        self.pkg_res[key] = res
        m = res["model"]
        cav = list(res["caveats"])
        edges = None
        if m is not None:
            miss = missing_pins(m, nl.pins(refdes).keys())
            if miss:
                cav.append("model:pin_absent")
            elif not [c for c in cav if c in _BLOCKING]:
                edges = transfer_edges(m)
        out = (res["name"], m, edges, cav)
        self._model_cache[key] = out
        return out

    def _passive_edges(self, board, rd):
        """兩腳被動件確實雙向導通，且不需要 datasheet。"""
        from ndd_models import TWO_PIN_FOOTPRINT_PREFIX
        nl = self.nl[board]
        fp = nl.parts.get(rd, "")
        if fp.startswith(TWO_PIN_FOOTPRINT_PREFIX) and len(nl.pins(rd)) == 2:
            return [("1", "2", "bidirectional", {}),
                    ("2", "1", "bidirectional", {})]
        return None

    def _rail_state(self, board, refdes, pin, polarity):
        """gate 的實體腳實際接到什麼 —— `always` 的唯一正面證據來源。"""
        nl = self.nl[board]
        net = nl.pin_net(refdes, pin)
        if not net:
            return C.UNKNOWN, "unconnected"
        if len(nl.net(net)) == 1:
            return C.UNKNOWN, "floating"        # 懸空致能腳本身就是設計問題
        cls = self.cls(net)
        if polarity == "low" and cls == "GND":
            return C.ALWAYS, "tied_gnd"
        if polarity == "high" and cls and cls.startswith("PWR"):
            return C.ALWAYS, "tied_pwr"
        if cls == "GND" or (cls and cls.startswith("PWR")):
            return C.CONDITIONAL, "tied_inactive"
        return C.CONDITIONAL, "driven_by:%s" % net

    # ---- 走圖 -------------------------------------------------------------
    def neighbours(self, node):
        """回傳 [(next_node, why, caveats, gating), ...]。**只含 trace graph 的邊。**"""
        board, rd, pin = node
        nl = self.nl[board]
        out = []

        net = nl.pin_net(rd, pin)
        if net and not self.is_power(net):
            for rd2, p2 in nl.net(net):
                if (rd2, p2) != (rd, pin):
                    out.append(((board, rd2, p2), "net:%s" % net, [], C.ALWAYS))

        name, m, edges, cav = self._model_at(board, rd)
        if edges is None and m is None and not cav:
            edges, name = self._passive_edges(board, rd), "2-pin passive"
        if edges:
            rail = lambda p, pol: self._rail_state(board, rd, p, pol)
            for nxt_pin, edge in outgoing(edges, pin):
                gating, notes = (derive_gating(m, edge, rail) if m
                                 else (C.ALWAYS, []))
                why = "thru:%s|%s %s->%s" % (rd, name, pin, nxt_pin)
                if notes:
                    why += " [%s]" % ",".join(notes)
                out.append(((board, rd, nxt_pin), why, list(cav), gating))

        for nxt, mcav in self.mate.get((board, rd, pin), []):
            out.append((nxt, "mate", list(mcav), C.ALWAYS))
        return out

    def endpoint_of(self, node):
        """沒有可用 transfer 邊時，這支腳算哪一種端點。

        回傳 (endpoint_kind, reason, caveats)；可繼續穿越時回傳 None。

        ⚠️ 舊版是 `return pairs is None`——「沒有模型」直接等於「訊號在此結束」，
           於是未建模的穿越件被寫進 loads 當**負載**。這與「停在具名位置」相反。
        """
        board, rd, pin = node
        nl = self.nl[board]
        if nl.is_mech(rd) or self.mate_partners(board, rd):
            return None                          # 連接器：由 mate 邊繼續
        name, m, edges, cav = self._model_at(board, rd)
        blocking = [c for c in cav if c in _BLOCKING]
        if blocking:
            return (EP_UNKNOWN_UNUSABLE,
                    ",".join(_BLOCKING[c] for c in sorted(blocking)), list(cav))
        if edges is None and m is None and not cav:
            edges = self._passive_edges(board, rd)
        if edges and outgoing(edges, pin):
            return None
        declared = (self.endpoints.get("%s:%s" % (board, rd))
                    or self.endpoints.get(self._pn(board, rd)))
        if declared == "terminal":
            return EP_TERMINAL, "", []
        if declared == "stateful":
            return EP_STATEFUL, "", []
        if declared == "unknown_stop":
            return EP_UNKNOWN_DECLARED, "declared", []
        # ⚠️ 預設 unclassified**必須**保留在輸出裡。若把未宣告的一律當
        #    unknown_stop 排除，絕大多數真實終端負載會在被宣告前全部消失，
        #    trace 的主要產出就報廢了。列存在保住可用性，caveat 保住誠實。
        return EP_UNCLASSIFIED, "undeclared", ["endpoint:unclassified"]

    def trace(self, start, stop_fn=None, max_depth=16):
        """BFS。caveats 與 gating 沿路徑累積 —— 標記不傳遞等於沒標。

        `stop_fn` 給定時用它判斷終點（例如「走到中繼連接器就停」）；否則用
        endpoint 分類。回傳 {node: (path, endpoint or None)}。
        """
        seen = {start: None}
        q = deque([(start, 0)])
        ends = {}
        while q:
            node, d = q.popleft()
            if d >= max_depth:
                continue
            for nxt, why, cav, gating in self.neighbours(node):
                if nxt in seen:
                    continue
                seen[nxt] = (node, why, cav, gating)
                if stop_fn is not None:
                    if stop_fn(nxt):
                        ends[nxt] = (self._path(seen, nxt), None)
                    else:
                        q.append((nxt, d + 1))
                    continue
                ep = self.endpoint_of(nxt)
                if ep is not None:
                    ends[nxt] = (self._path(seen, nxt), ep)
                    if ep[0] in (EP_UNKNOWN_UNUSABLE, EP_UNKNOWN_DECLARED):
                        continue                 # 停在具名位置，不再往下猜
                else:
                    q.append((nxt, d + 1))
        return ends

    @staticmethod
    def _path(seen, node):
        out = []
        while seen.get(node):
            prev, why, cav, gating = seen[node]
            out.append((prev, why, node, cav, gating))
            node = prev
        return list(reversed(out))

    @staticmethod
    def path_caveats(path, extra=()):
        cav = set(extra)
        for _p, _w, _n, c, _g in path:
            cav.update(c)
        return cav

    @staticmethod
    def path_gating(path):
        return C.worst_gating([g for _p, _w, _n, _c, g in path])

    def hop_string(self, path):
        steps = []
        for _prev, why, node, _cav, _g in path:
            b, rd, pin = node
            if why.startswith("thru:"):
                body = why.split(":", 1)[1]
                rdname, rest = body.split("|", 1)
                steps.append("%s %s(%s)" % (b.upper(), rdname, rest))
            elif why == "mate":
                steps.append(">> %s %s.%s" % (b.upper(), rd, pin))
        return " | ".join(steps)

    # ---- hint graph（**不可被 BFS 使用**）---------------------------------
    def collect_hints(self, board=None):
        """列舉功能影響關係。這些是**功能說明，不是連通**。"""
        rows = []
        keys = [board] if board else list(self.nl)
        for b in keys:
            nl = self.nl[b]
            for rd in nl.parts:
                _n, m, _e, _c = self._model_at(b, rd)
                if not m:
                    continue
                mpn = self._pn(b, rd)
                for inf in control_influences(m):
                    for p in inf.get("from", []):
                        rows.append(dict(
                            board=b, refdes=rd, pin=str(p),
                            net=nl.pin_net(rd, str(p)) or "",
                            mpn=mpn, relation="control_influence",
                            effect="%s -> %s" % (inf.get("effect", ""),
                                                 inf.get("affects", "")),
                            source=inf.get("verified_against")
                                   or m.get("verified_against", "")))
                for e in m.get("transfer") or []:
                    for p in e.get("parameter_control", []):
                        rows.append(dict(
                            board=b, refdes=rd, pin=str(p),
                            net=nl.pin_net(rd, str(p)) or "",
                            mpn=mpn, relation="parameter_control",
                            effect="改變訊號性質，不改變是否導通",
                            source=e.get("verified_against")
                                   or m.get("verified_against", "")))
        self.hints = rows
        return rows
