# -*- coding: utf-8 -*-
"""主／備（P／R）這類成對子電路：腳本逐顆對應，只把**差異**交給撰寫者。

撰寫材料原本把 P、R 兩份都照列，B 要自己逐腳比對兩套才找得到不對稱——那是
機械工作，而且兩份材料幾乎一樣長。這裡先把 R 的每一顆對應到 P 的某一顆，再逐腳
比對接法：R 側只送「對應表 + 接法不同的腳 + 只在一側的零件 + 料號／貼件不同」。

對應方法（不看 refdes，refdes 兩套編號不同）：
1. 每顆零件的起始標籤 = footprint、料號（未貼另標）、各腳接到電源／地／訊號。
2. 反覆細化：標籤 += 各訊號網路上鄰居的標籤（不分腳——兩擲對調的開關，鄰居
   集合相同，仍對得起來；對調本身在第 3 步逐腳比對時才報出來）。
3. 由細到粗：每一輪標籤兩側各剩一顆的直接對上（只有一處差異時，細標籤不同、
   粗標籤仍對得上）。
4. 其餘從已對上的鄰居往外長：同起始標籤（找不到就同料號＋footprint）的候選裡，
   逐腳相同的對端**嚴格最多**（同分再比差異少）、且不到一半腳不同的才配，反覆到沒有進展。只有一側
   多一顆未貼電阻、或開關兩擲對調時，照樣對得上，差異在逐腳比對時報出來；
   同值電容這種對稱群不會因為「剩下剛好一顆」而錯配。最後一輪才放寬平手。

⚠️ 對不上的零件照列在「只在一側」，**不猜**。
"""

import collections
import re

import ndd_classify

_ROUNDS = 4
# 像電源軌的網路：這麼多顆以上、且大半是 2 腳被動件（旁路電容）。`power_net_regex`
# 沒收到的軌（`6V_R`、`AGND`）照樣當軌處理——否則上面幾十顆電容彼此都是鄰居，
# 一處不一致就整片對不上（實測某 interposer 因此 106 顆報成「只在一側」）。
_RAIL_MIN = 12
_RAIL_PASSIVE = 0.6
# 這麼多顆以上的網路一律當軌：不管上面是什麼，幾十顆彼此都是鄰居，對配對沒有
# 鑑別力（實測 `3P4V_R` 57 顆、被動件只佔一半，也是一條軌）。
_RAIL_BIG = 24


def _natkey(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


class Matcher(object):
    def __init__(self, nl, bom, is_power, gnd):
        self.nl, self.bom = nl, bom
        self.is_power, self.gnd = is_power, gnd
        self._lab = None
        self._rail = {}

    def rail(self, n):
        """電源軌（regex 認得的，或看起來像軌的大網路）。"""
        if n not in self._rail:
            mem = self.nl.nets.get(n, ())
            two = sum(1 for r, _p in mem
                      if self.nl.is_passive(r) and len(self.nl.pins(r)) == 2)
            self._rail[n] = bool(self.is_power(n) or len(mem) >= _RAIL_BIG or (
                len(mem) >= _RAIL_MIN and two >= _RAIL_PASSIVE * len(mem)))
        return self._rail[n]

    def netmap(self, m, rs):
        """已對上的零件逐腳投票：本套的軌 -> 參照套的哪一條軌。"""
        vote = collections.defaultdict(collections.Counter)
        for r, p in m.items():
            if r not in rs:
                continue
            for k, nr in self.nl.pins(r).items():
                np_ = self.nl.pin_net(p, k)
                if nr and np_ and self.rail(nr) and not self.gnd(nr):
                    vote[nr][np_] += 1
        return dict((n, c.most_common(1)[0][0]) for n, c in vote.items())

    def _base(self, rd):
        nl = self.nl
        pn = self.bom.pn(rd) or u"DNI"
        pins = []
        for p, n in sorted(nl.pins(rd).items()):
            if not n:
                k = u"-"
            elif self.gnd(n):
                k = u"G"
            elif self.rail(n):
                k = u"V"
            elif len(nl.nets[n]) == 1:
                k = u"0"
            else:
                k = u"S"
            pins.append(p + k)
        return u"%s|%s|%s" % (nl.parts.get(rd), pn, u",".join(pins))

    def labels(self):
        """全板零件的最終標籤（整板一起算，外部鄰居才有一致的標籤）。"""
        if self._lab is not None:
            return self._lab
        nl = self.nl
        lab = dict((rd, hash(self._base(rd))) for rd in nl.parts)
        labs = [lab]
        nbr = collections.defaultdict(list)
        for n, mem in nl.nets.items():
            if self.rail(n) or len(mem) < 2:
                continue
            rds = [r for r, _p in mem]
            for r in set(rds):
                nbr[r].extend(x for x in rds if x != r)
        for _ in range(_ROUNDS):
            lab = dict((rd, hash((lab[rd], tuple(sorted(lab[x] for x in nbr[rd])))))
                       for rd in nl.parts)
            labs.append(lab)
        self._lab = labs
        return labs

    def pin_view(self, rd, m=None, hide=(), nmap=None):
        """-> {腳: frozenset(對端)}；對端是 (refdes, 腳)，經 `m` 換成 P 側名字；
        電源腳換成「電源」「地」。

        `hide`：另一套的零件。兩套共用一條網路（共用輸入軌、共用 I2C）時，
        每一套都會在那條網上「看到」另一套的零件——那不是自己的結構，比對時
        要拿掉，否則共用網路上每一顆都會被報成不同。"""
        m = m or {}
        out = {}
        for p, n in self.nl.pins(rd).items():
            if not n:
                out[p] = frozenset([u"(未接)"])
            elif self.gnd(n):
                out[p] = frozenset([u"GND"])
            elif self.rail(n):
                # 軌以「哪一條」比，不以上面掛了誰比。`nmap` 是 False 時只說「接軌」
                # （配對用——軌的對應要等零件配好才學得到）；是 dict 時本套的軌換成
                # 參照套的名字，還沒對應到的標 `?`（最後比差異用）。
                if nmap is False:
                    out[p] = frozenset([u"PWR"])
                elif nmap is None:
                    out[p] = frozenset([(u"軌", n)])
                else:
                    out[p] = frozenset([(u"軌", nmap.get(n, u"?" + n))])
            else:
                # 測試點不比：兩側各自編號、又不參與電路，只會變成雜訊。
                out[p] = frozenset((m.get(r, r), q) for r, q in self.nl.nets[n]
                                   if r != rd and not self.nl.is_mech(r)
                                   and r not in hide)
        return out

    def ndiff(self, p, r, m, ps=(), rs=()):
        a = self.pin_view(p, hide=rs, nmap=False)
        b = self.pin_view(r, m, hide=ps, nmap=False)
        return sum(1 for k in set(a) | set(b) if a.get(k) != b.get(k))

    def match(self, ps, rs, anchor=None):
        """P 側零件、R 側零件 -> {R: P}。`anchor` 是已知的全板對應（外部鄰居用）。"""
        labs = self.labels()
        m = dict(anchor or {})
        ps, rs = set(ps), set(rs)
        # 1. 由細到粗：每一輪標籤兩側各剩一顆的直接配。只有一處差異（多一顆
        #    未貼電阻）時細標籤會不同，退到粗標籤仍對得上。
        for lab in reversed(labs):
            taken = set(m.values())
            byp = collections.defaultdict(list)
            byr = collections.defaultdict(list)
            for x in ps:
                if x not in taken:
                    byp[lab[x]].append(x)
            for x in rs:
                if x not in m:
                    byr[lab[x]].append(x)
            for k, rr in byr.items():
                pp = byp.get(k) or []
                if len(pp) == 1 and len(rr) == 1:
                    m[rr[0]] = pp[0]
        # 2. 從已對上的鄰居往外長：候選是同起始標籤（或同料號＋footprint）還沒
        #    配的 P 零件，逐腳差異**明顯最少**（嚴格少於第二名）才配。對稱的
        #    一群（同值電容）這樣才不會亂配——錯配會讓後面整片都報假差異。
        pview = {}

        def pv(x):
            if x not in pview:
                pview[x] = self.pin_view(x, hide=rs, nmap=False)
            return pview[x]

        def nd(a, b):
            return sum(1 for k in set(a) | set(b) if a.get(k) != b.get(k))

        def mask(v, drop):
            return dict((k, frozenset(e for e in xs
                                      if not (isinstance(e, tuple) and e[0] != u"軌"
                                              and drop(e[0]))))
                        for k, xs in v.items())

        def wrong_rail(x, r, nmap):
            """已知對應的軌上，候選接的是不同一條——旁路電容分得出 3.4 V 與 6 V。"""
            n = 0
            for k, nr in self.nl.pins(r).items():
                np_ = self.nl.pin_net(x, k)
                if nr in nmap and np_ and nmap[nr] != np_:
                    n += 1
            return n

        def agree(a, b):
            """逐腳相同的對端有幾個——正面證據；還沒對上的鄰居不加不減。"""
            return sum(len(a[k] & b[k]) for k in a if k in b)

        key0 = lambda x: labs[0][x]
        key1 = lambda x: (self.nl.parts.get(x), self.bom.pn(x))
        for strict in (True, True, True, False):
            changed = True
            while changed:
                changed = False
                taken = set(m.values())
                nmap = self.netmap(m, rs)
                left0 = collections.defaultdict(list)
                left1 = collections.defaultdict(list)
                for x in ps:
                    if x not in taken:
                        left0[key0(x)].append(x)
                        left1[key1(x)].append(x)
                for r in sorted((x for x in rs if x not in m), key=_natkey):
                    cand = [x for x in (left0.get(key0(r)) or []) if x not in taken] \
                        or [x for x in (left1.get(key1(r)) or []) if x not in taken]
                    if not cand:
                        continue
                    # 還沒對上的鄰居兩側都先拿掉——上下拉電阻成對時，彼此都還沒配，
                    # 不該因此算成差異。
                    b = mask(self.pin_view(r, m, hide=ps, nmap=False),
                             lambda e: e in rs and e not in m)
                    sc = []
                    for x in cand:
                        a = mask(pv(x), lambda e: e in ps and e not in taken)
                        sc.append((-agree(a, b), nd(a, b), wrong_rail(x, r, nmap),
                                   _natkey(x), x))
                    sc.sort()
                    best = sc[0]
                    npin = max(len(self.nl.pins(r)), 1)
                    if best[1] * 2 >= npin and best[1] > 0:
                        continue
                    if strict and len(sc) > 1 and sc[1][:3] == best[:3]:
                        continue
                    m[r] = best[4]
                    taken.add(best[4])
                    changed = True
        return m


def _pin_label(hpn, rd, p):
    n = hpn.get((rd, p))
    return (u"%s(%s)" % (p, n)) if n and n != p else p


def _ends(hpn, s):
    out = []
    for x in sorted(s, key=lambda y: _natkey(u"%s" % (y,))):
        if isinstance(x, tuple) and x[0] == u"軌":
            out.append(u"軌 `%s`" % x[1].lstrip(u"?"))
        elif isinstance(x, tuple):
            out.append(u"%s.%s" % (x[0], _pin_label(hpn, x[0], x[1])))
        else:
            out.append(x)
    return u"、".join(out) or u"（無）"


def diff_lines(mt, hpn, ps, rs, m, label, where, names=(u"P", u"R")):
    """本套（`rs`）對參照套（`ps`）的差異 markdown。`m`：{本套: 參照套}。
    `names`：(參照套名, 本套名)，寫進每一行——成對不一定是主／備（兩個相同的
    電源模組、兩個通道也會成對），用子電路名才不會誤導。"""
    nl, bom = mt.nl, mt.bom
    P, R = names
    inv = dict((v, k) for k, v in m.items())
    L = []
    mine = [r for r in sorted(rs, key=_natkey) if r in m and m[r] in ps]
    # 測試點、鎖孔這類機構件不列：比對時已經不看它們，列出來只是兩套各自編號的雜訊。
    only_r = [r for r in sorted(rs, key=_natkey)
              if (r not in m or m[r] not in ps) and not nl.is_mech(r)]
    only_p = [p for p in sorted(ps, key=_natkey)
              if (p not in inv or inv[p] not in rs) and not nl.is_mech(p)]

    L.append(u"## 位號對應（%s → %s）" % (R, P))
    L.append(u"")
    L.append(u"、".join(u"%s→%s" % (r, m[r]) for r in mine if not nl.is_passive(r))
             or u"（無主動件）")
    L.append(u"")
    L.append(u"被動件 %d 顆也已對應，接法相同的不列。" %
             sum(1 for r in mine if nl.is_passive(r)))
    L.append(u"")

    L.append(u"## 接法不同的腳")
    L.append(u"")
    L.append(u"對端一律換成 `%s` 的位號（還沒對應到的保留原名），方便直接對照它的材料；"
             u"兩套共用的網路上，另一套的零件不列。" % P)
    L.append(u"")
    # 同一個差異出現在很多顆上（例如整條 I2C 在 P 側另接 U2031、R 側另接 U2034），
    # 只寫一次，不逐顆重複。
    rows, common = [], collections.OrderedDict()
    nmap = mt.netmap(m, rs)
    for r in mine:
        p = m[r]
        a, b = mt.pin_view(p, hide=rs), mt.pin_view(r, m, hide=ps, nmap=nmap)
        bad = [k for k in sorted(set(a) | set(b), key=_natkey) if a.get(k) != b.get(k)]
        for k in bad:
            pa, pb = a.get(k, frozenset()), b.get(k, frozenset())
            rows.append((r, p, k, pa, pb))
            if pa & pb:
                common.setdefault((pa - pb, pb - pa), []).append((r, p, k))
    rep = set(key for key, v in common.items() if len(v) >= 3)
    by = collections.OrderedDict()
    for r, p, k, pa, pb in rows:
        if pa & pb and (pa - pb, pb - pa) in rep:
            continue
        by.setdefault((r, p), []).append((k, pa, pb))
    for (r, p), xs in by.items():
        L.append(u"- **%s**（%s：%s）%s" % (r, P, p, label(r)))
        for k, pa, pb in xs:
            if pa & pb:
                L.append(u"  - %s：%s 另接 %s；%s 另接 %s（其餘相同）" % (
                    _pin_label(hpn, p, k), P, _ends(hpn, pa - pb), R, _ends(hpn, pb - pa)))
            else:
                L.append(u"  - %s：%s 接 %s；%s 接 %s" % (
                    _pin_label(hpn, p, k), P, _ends(hpn, pa), R, _ends(hpn, pb)))
    for (oa, ob) in [k for k in common if k in rep]:
        xs = common[(oa, ob)]
        L.append(u"- **%d 支腳同一差異**：%s 另接 %s；%s 另接 %s（其餘相同）——%s" % (
            len(xs), P, _ends(hpn, oa), R, _ends(hpn, ob),
            u"、".join(u"%s.%s" % (r, _pin_label(hpn, r, k)) for r, _p, k in xs)))
    if not rows:
        L.append(u"（無——對應到的零件逐腳接法相同）")
    L.append(u"")

    L.append(u"## 料號或貼件不同")
    L.append(u"")
    n = 0
    for r in mine:
        a, b = bom.pn(m[r]), bom.pn(r)
        if a != b:
            n += 1
            L.append(u"- %s（%s：%s）：%s %s；%s %s" % (r, P, m[r], P, a or u"未貼",
                                                    R, b or u"未貼"))
    if not n:
        L.append(u"（無）")
    L.append(u"")

    L.append(u"## 只在一側（對不上的零件）")
    L.append(u"")
    for side, xs in ((u"只在 " + R, only_r), (u"只在 " + P, only_p)):
        if not xs:
            continue
        for x in xs:
            pins = u"；".join(u"%s→%s" % (_pin_label(hpn, x, q), nn or u"未接")
                             for q, nn in sorted(nl.pins(x).items(), key=lambda y: _natkey(y[0])))
            L.append(u"- %s：**%s** %s（%s）— %s" % (side, x, label(x), where(x), pins))
    if not only_r and not only_p:
        L.append(u"（無）")
    L.append(u"")
    return L


def pairs(tree, grp):
    """成對的子電路（兩個兄弟、組成相同或幾乎相同），且各自就是一個分塊。
    -> [(P 分塊名, R 分塊名, P 路徑, R 路徑)]，P 取自然排序在前的那個。"""
    out = []

    def walk(p):
        groups, near = tree.sibling_groups(p)
        cand = [tuple(g) for g in groups if len(g) == 2] + list(near)
        for a, b in cand:
            a, b = sorted((a, b), key=lambda x: [_natkey(y) for y in x])
            ga, gb = grp(a), grp(b)
            if ga == u" / ".join(a) and gb == u" / ".join(b):
                out.append((ga, gb, a, b))
        for k in tree.children(p):
            walk(k)
    walk(())
    return out
