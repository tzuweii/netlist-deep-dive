# -*- coding: utf-8 -*-
"""板卡事實表（`<板>_Facts.md`）——`init` 當下就寫得出來的那一半。

**它是寫 `<板>_Architecture.md` 的材料，不是給人讀的導覽。** 導覽由 AI 讀這份
再撰寫（流程見 `references/project-lifecycle.md` §4）。這裡把 AI 自己追會很貴
的東西先算好——階層樹、子電路之間靠哪些訊號相連、跨子電路的控制中樞、
I2C 掛載——AI 只需針對鏈路細節補查。

**這份文件在結構上只能是 `[N]`/`[B]`/`[S]` 文件。** `init` 跑完的當下還沒有
任何 datasheet（`INDEX.md` 才剛產生），所以腳位功能、訊號方向、極性、
「這條路恆通」這些 `[D]` 級主張**沒有材料可寫**——想違規也違規不了。

它提供的是「讀懂這塊板層級」需要的關係：
板子由哪些子電路組成（含重複與成對）、子電路之間靠什麼相連、大顆零件伸進
哪些子電路、共用匯流排、對外介面、電源軌供應誰、未貼件落在哪個子電路。

⚠️ **連線沒有方向。** 「A 驅動 B」要 datasheet，這裡只說「相連」並附網路名。
⚠️ **腳本寫不出「這段是什麼、為什麼這樣設計」**，那是導覽的工作。
"""

import collections
import io
import itertools
import os
import re

import ndd_classify
from ndd_graph import Fabric

# 自動產生的網路名（PADS 改名、OrCAD 流水號）——對讀的人沒有意義，
# 分群時要排掉，否則會蓋掉真正的訊號家族。
_RX_AUTONET = re.compile(r"^[NX]\d{4,}(_|$)", re.I)
# 家族化：把結尾的序號剝掉。`TX_EN_P_0` -> `TX_EN_P`，`ANT_IN10` -> `ANT_IN`。
_RX_TAIL_NUM = re.compile(r"[_]?\d+$")
# I2C：`SCL_P`、`I2C1_SDA`、`SDA_UC2_COMMON`。
_RX_I2C = re.compile(r"(^|[^A-Z0-9])(SCL|SDA)(?=$|[^A-Z])", re.I)

# 值得單獨列出來的分類（看得到料號就知道要幹嘛的那些）。
_MAJOR = ("ic", "rf", "power_module", "filter", "crystal", "sensor",
          "semiconductor_discrete", "isolator", "optoelectronic",
          "switch", "relay")

# 子電路佔全板零件比例超過這個值，就往下拆一層再畫連線圖——
# 否則一個大區塊會把整段 RF 或整片 baseband 吞成一個方塊。
_EXPAND_FRAC = 0.3
# 頂層零件（不在任何子電路）佔這個比例以上，視為沒有階層的板子。
_FLAT_FRAC = 0.7
# 一條網路碰到這麼多個區塊，當成共用線（匯流排、致能）另外列，不拆成兩兩連線。
_WIDE_NET = 4
# 組成相似度到這個值就視為「幾乎相同」（P/R 成對但差幾顆）。
_NEAR = 0.9

# 電源軌表只列這麼多支腳以上的——濾波後的區域供電一條 2、3 支腳，
# 實測一塊板被 `power_net_regex` 收進 367 條，全列等於沒列。
_RAIL_MIN = 10

# 這麼多腳以上的連接器，在連線圖上各自一塊。
_BIG_CONN = 10

ROOT_LABEL = u"（頂層）"


def _natkey(s):
    """自然排序。`TX1 < TX2 < … < TX16`——字典序會排成 `TX1, TX10, …, TX9`，
    範圍顯示就會變成「TX1 … TX9」卻說有 16 組，看起來像 bug。"""
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", s)]


def _pathkey(p):
    return [_natkey(x) for x in p]


def _rng(names):
    """把一串名字縮成「`頭` … `尾`」，兩個以內就全列。"""
    ns = sorted(names, key=_natkey)
    if len(ns) <= 2:
        return u"、".join(u"`%s`" % n for n in ns)
    return u"`%s` … `%s`" % (ns[0], ns[-1])


def _is_power(net, pwr_rx):
    """跟 `Fabric.is_power` 同一套判準，但不必建 Fabric（那會驗 mates）。"""
    if not net:
        return False
    if pwr_rx and pwr_rx.match(net):
        return True
    return (Fabric.cls(net) == "GND"
            and not Fabric._RX_SENSE.search(net.upper()))


def _sig_pins(nl, refdes, pwr_rx):
    """這顆接了幾支**非電源**腳——只用 netlist 就算得出來的重要性指標。"""
    return sum(1 for p in nl.pins(refdes)
               if not _is_power(nl.pin_net(refdes, p), pwr_rx))


def _is_2pin_passive(nl, rd):
    return nl.is_passive(rd) and len(nl.pins(rd)) == 2


def _label(nl, bom, rd):
    """零件的顯示名：BOM 料號；BOM 沒有 = 未貼件，改用 footprint。"""
    pn = bom.pn(rd)
    return pn if pn else u"未貼 %s" % (nl.parts.get(rd) or rd)


def _sample(nets, k=3):
    """代表網路名：有名字的優先，自動編號的不拿來當例子。"""
    named = sorted((n for n in nets if not _RX_AUTONET.match(n)), key=_natkey)
    if named:
        s = u"、".join(u"`%s`" % n for n in named[:k])
        return s + (u" 等" if len(named) > k else u"")
    return u"（皆為無名網路）"


# ----------------------------------------------------------------------
# 階層樹
# ----------------------------------------------------------------------

class Tree(object):
    """`.DSN` 階層，只收 netlist 裡真的有的零件。路徑一律是 tuple，**不切字串**
    （`SPST_P/R_0` 這種名字本身就帶 `/`）。"""

    def __init__(self, hier, nl):
        self.nl = nl
        paths = hier.part_paths() if hier is not None else {}
        self.path = {}
        self.sub = collections.defaultdict(list)      # 子樹內所有零件
        self.direct = collections.defaultdict(list)   # 直接掛在這層的零件
        self.kids = collections.defaultdict(set)
        for rd in nl.parts:
            p = tuple(paths.get(rd) or ())
            self.path[rd] = p
            self.direct[p].append(rd)
            for d in range(len(p) + 1):
                self.sub[p[:d]].append(rd)
                if d:
                    self.kids[p[:d - 1]].add(p[:d])
        self._sig = {}
        # 設計者給每一頁取的名字（parts.csv 方塊列的 value 欄）——
        # `UC1` 這種 instance 名看不出功能，頁名 `Primary_Redundant` 看得出。
        self.page = {}
        if hier is not None:
            for r in hier.blocks():
                v = (r.get("value") or "").strip()
                path = tuple(hier.path_of(r["id"]))
                if v and path and v != path[-1]:
                    self.page[path] = v

    @property
    def n(self):
        return len(self.nl.parts)

    def is_flat(self):
        return len(self.direct[()]) >= _FLAT_FRAC * max(self.n, 1)

    def children(self, p):
        return sorted(self.kids.get(p, ()), key=_pathkey)

    def comp(self, p):
        """子樹的 footprint 多重集合——refdes 會變，組成不會。"""
        if p not in self._sig:
            self._sig[p] = collections.Counter(
                self.nl.parts.get(r, "?") for r in self.sub[p])
        return self._sig[p]

    def sig(self, p):
        return tuple(sorted(self.comp(p).items()))

    def sibling_groups(self, parent):
        """-> (groups, pairs)。groups：組成完全相同的兄弟區塊；
        pairs：剩下的單個裡組成「幾乎相同」的兩兩配對（P/R 差幾顆）。"""
        by = collections.OrderedDict()
        for k in self.children(parent):
            by.setdefault(self.sig(k), []).append(k)
        groups = list(by.values())
        singles = [g[0] for g in groups if len(g) == 1]
        pairs, used = [], set()
        for i, a in enumerate(singles):
            if a in used:
                continue
            best = None
            for b in singles[i + 1:]:
                if b in used:
                    continue
                s = similarity(self.comp(a), self.comp(b))
                if s >= _NEAR and min(len(self.sub[a]), len(self.sub[b])) >= 10:
                    if best is None or s > best[0]:
                        best = (s, b)
            if best:
                pairs.append((a, best[1]))
                used |= {a, best[1]}
        return groups, pairs


def similarity(ca, cb):
    tot = max(sum(ca.values()), sum(cb.values())) or 1
    return float(sum((ca & cb).values())) / tot


def repeat_groups(hier, nl):
    """全板所有層級的重複結構：-> [(父路徑, [區塊路徑...], 每組零件數)]。

    同一塊板上「9 個長得一模一樣的區塊」幾乎一定就是系統架構本身
    （9 個 slot、9 路通道）。**要遞迴往下找**——實測某板的 9 路通道在第三層，
    只比頂層一組都抓不到。重複群組內部只看第一個，不重複展開。
    """
    t = Tree(hier, nl)
    out = []

    def walk(p):
        groups, _pairs = t.sibling_groups(p)
        for g in groups:
            if len(g) >= 2:
                out.append((p, g, len(t.sub[g[0]])))
            walk(g[0])
    walk(())
    out.sort(key=lambda x: (-len(x[1]), -x[2]))
    return out


class KeyPart(object):
    """「值得在導覽裡點名」的零件：分類在 `_MAJOR`、且不是磁珠。

    磁珠被歸成 `filter`（跟 RF 帶通濾波器同類），一塊 RF 板上幾十顆，
    不排掉會把每個子電路的主要零件欄洗成 `BLM31… ×14`。
    """

    _RX_BEAD = re.compile(r"^(ferrite|bead)", re.I)

    def __init__(self, nl, bom, cfg, cis):
        self.nl, self.bom, self.cis = nl, bom, cis
        self.taxo = cfg.get("part_class") or {}
        self.fpov = cfg.get("footprint_class") or {}
        self._memo = {}

    def __call__(self, rd):
        if rd not in self._memo:
            nl = self.nl
            ok = False
            if not (nl.is_passive(rd) or nl.is_mech(rd)
                    or ndd_classify.is_connector(nl, rd)):
                fp = nl.parts.get(rd) or ""
                cat, _src = ndd_classify.classify(
                    rd, self.bom.pn(rd) or "", board=None, overrides=self.taxo,
                    cis=self.cis, footprint=fp, fp_overrides=self.fpov)
                ok = cat in _MAJOR and not self._RX_BEAD.match(fp)
            self._memo[rd] = ok
        return self._memo[rd]


def _key_parts(nl, bom, rds, is_key, k=6):
    """一群零件裡的主要零件，同料號收斂、依顆數排序。"""
    c = collections.Counter(_label(nl, bom, r) for r in rds if is_key(r))
    if not c:
        return u""
    items = sorted(c.items(), key=lambda x: (-x[1], _natkey(x[0])))
    s = u"、".join(u"%s ×%d" % (pn, n) if n > 1 else pn for pn, n in items[:k])
    return s + (u" 等 %d 種" % len(items) if len(items) > k else u"")


def _diff(ca, cb, k=3):
    def fmt(c):
        it = sorted(c.items(), key=lambda x: -x[1])
        return u"、".join(u"`%s` ×%d" % (f, n) for f, n in it[:k])
    a, b = ca - cb, cb - ca
    return fmt(a), fmt(b)


def tree_lines(t, bom, is_key):
    """階層樹的 markdown 條列。重複區塊收斂成一行，只展開第一個。"""
    nl = t.nl
    L = []

    def line(depth, head, p_or_rds, extra=u""):
        rds = p_or_rds if isinstance(p_or_rds, list) else (
            t.direct[p_or_rds] if t.kids.get(p_or_rds) else t.sub[p_or_rds])
        kp = _key_parts(nl, bom, rds, is_key)
        tail = (u" — %s" % kp) if kp else u""
        pg = t.page.get(p_or_rds) if isinstance(p_or_rds, tuple) else None
        if pg:
            extra = u"（頁 `%s`）%s" % (pg, extra)
        L.append(u"%s- %s%s%s" % (u"  " * depth, head, extra, tail))

    def walk(p, depth):
        groups, pairs = t.sibling_groups(p)
        partner = dict(pairs)
        second = set(b for _a, b in pairs)
        for g in groups:
            k = g[0]
            has_kids = bool(t.kids.get(k))
            if len(g) >= 2:
                names = [x[-1] for x in g]
                head = u"%s **×%d**，每組 %d 顆" % (_rng(names), len(g),
                                                  len(t.sub[k]))
                note = (u"；以下以 `%s` 為例" % k[-1]) if has_kids else u""
                line(depth, head + note, k)
                if has_kids:
                    walk(k, depth + 1)
                continue
            if k in second:
                continue
            if k in partner:
                b = partner[k]
                da, db = _diff(t.comp(k), t.comp(b))
                diff = []
                if da:
                    diff.append(u"`%s` 多 %s" % (k[-1], da))
                if db:
                    diff.append(u"`%s` 多 %s" % (b[-1], db))
                head = (u"`%s` %d 顆 ／ `%s` %d 顆，**組成幾乎相同**（%s）"
                        % (k[-1], len(t.sub[k]), b[-1], len(t.sub[b]),
                           u"；".join(diff)))
                note = (u"；以下以 `%s` 為例" % k[-1]) if has_kids else u""
                line(depth, head + note, k)
                if has_kids:
                    walk(k, depth + 1)
                continue
            line(depth, u"`%s` %d 顆" % (k[-1], len(t.sub[k])), k,
                 (u"（本層直屬 %d 顆）" % len(t.direct[k])) if has_kids else u"")
            if has_kids:
                walk(k, depth + 1)

    if t.direct[()]:
        line(0, u"%s %d 顆（不在任何子電路）" % (ROOT_LABEL, len(t.direct[()])),
             list(t.direct[()]))
    walk((), 0)
    return L


# ----------------------------------------------------------------------
# 連線圖的單位
# ----------------------------------------------------------------------

class Units(object):
    """連線圖上的方塊。

    從頂層區塊開始；佔全板比例太大的區塊往下拆一層。完全相同且 3 組以上的
    兄弟區塊併成一個方塊——2 組的不併，P/R 成對要看得出誰接誰。

    **不在任何子電路的零件**（頂層，或拆開那層直接掛的）不併成一大塊：
    主要零件與連接器依料號各成一個方塊。實測有板子一半零件在頂層，
    併成「（頂層）」一塊會把 I2C mux、主備切換開關全部藏起來。

    連線的端點只有主要零件與連接器；被動件是黏著劑，不是端點。
    """

    def __init__(self, t, bom, is_key):
        self.t, self.bom, self.is_key = t, bom, is_key
        self.of = {}          # refdes -> unit（完整路徑，或 路徑+(None,) = 本層）
        self.node = {}        # unit -> 畫圖用的節點 key
        nl = t.nl

        def expand(p):
            kids = t.children(p)
            if kids and (p == () or len(t.sub[p]) > _EXPAND_FRAC * t.n):
                u = p + (None,)
                for r in t.direct[p]:
                    self.of[r] = u
                groups, _ = t.sibling_groups(p)
                gmap = {}
                for g in groups:
                    for k in g:
                        gmap[k] = g
                for k in kids:
                    g = gmap.get(k, [k])
                    if len(g) >= 3 and not (t.kids.get(k)
                                            and len(t.sub[k]) > _EXPAND_FRAC * t.n):
                        self._leaf(k, ("G",) + tuple(g))
                    else:
                        expand(k)
            else:
                self._leaf(p, p)
        expand(())
        # 大接頭（`_BIG_CONN` 腳以上）各自一塊——同料號的兩顆通常是主／備
        # 兩個不同介面，併起來就看不出誰接誰；小的（同軸、測試埠）依料號併。
        self._loose = {}
        for r, u in self.of.items():
            if u and u[-1] is None and self.endpoint(r):
                lab = _label(nl, bom, r)
                if ndd_classify.is_connector(nl, r)                         and len(nl.pins(r)) >= _BIG_CONN:
                    lab = u"`%s` %s" % (r, lab)
                self._loose[r] = ("P", u[:-1], lab)

    def _leaf(self, p, node):
        for r in self.t.sub[p]:
            self.of[r] = p
        self.node[p] = node

    def endpoint(self, rd):
        nl = self.t.nl
        return self.is_key(rd) or ndd_classify.is_connector(nl, rd)

    def node_of(self, rd):
        """零件所屬的方塊；不是端點（被動件、測試點…）回 None。"""
        if not self.endpoint(rd):
            return None
        if rd in self._loose:
            return self._loose[rd]
        u = self.of.get(rd)
        return self.node.get(u, u)

    def count(self, key):
        """散件方塊裡有幾顆（顯示 `×n` 用）。"""
        return sum(1 for v in self._loose.values() if v == key)

    def name(self, key):
        if key is None:
            return u""
        if key[0] == "G":
            return u"%s ×%d" % (u" … ".join(
                [u" / ".join(key[1]), u" / ".join(key[-1])]), len(key) - 1)
        if key[0] == "P":
            n = self.count(key)
            base = u"%s ×%d" % (key[2], n) if n > 1 else key[2]
            return (u"%s（%s 本層）" % (base, u" / ".join(key[1]))
                    if key[1] else base)
        return u" / ".join(key) if key else ROOT_LABEL


def _neighbors(nl, rd, net, pwr_rx):
    """這條網路上的其他零件，再穿過一顆 2 腳被動件（串阻、隔直電容、磁珠）。"""
    out = set()
    for r2, _p in nl.nets.get(net, ()):
        if r2 == rd:
            continue
        out.add(r2)
        if _is_2pin_passive(nl, r2):
            for n3 in nl.pins(r2).values():
                if n3 and n3 != net and not _is_power(n3, pwr_rx):
                    for r4, _p4 in nl.nets.get(n3, ()):
                        if r4 not in (r2, rd):
                            out.add(r4)
    return out


def connections(nl, pwr_rx, glue):
    """把「穿過一顆串聯件（串阻、隔直電容、磁珠）還是同一條訊號」的網路
    併成一組。-> [[網路名...], ...]

    不併的話，一條經過串阻的線會被當成兩條連線各算一次。
    """
    parent = {}

    def find(x):
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    for n in nl.nets:
        if not _is_power(n, pwr_rx):
            parent[n] = n
    links = []
    deg = collections.Counter()
    for rd in nl.parts:
        if not glue(rd):
            continue
        ns = [n for n in nl.pins(rd).values() if n in parent]
        if len(ns) == 2 and ns[0] != ns[1]:
            links.append(ns)
            deg[ns[0]] += 1
            deg[ns[1]] += 1
    # 只併「兩端都只有這一顆黏著件」的——那才是單純的串聯元件。
    # 兩端還掛別的被動件（運放回授網路、差動終端）時照鏈併下去，會把 I/Q
    # 這種不同的訊號併成一條。
    for a, b in links:
        if deg[a] == 1 and deg[b] == 1:
            parent[find(a)] = find(b)
    # 連續兩三顆串聯（隔直電容接 0 Ω、兩段串阻）：中間那條網路上**只有**這兩顆
    # 黏著件，照樣是同一條訊號。但兩端若碰到同一顆零件就不併——那是回授網路
    # （同一顆運放的兩支腳），不是串聯。
    via = collections.defaultdict(list)
    for a, b in links:
        via[a].append(b)
        via[b].append(a)
    mem = {}

    def members(root):
        if root not in mem:
            mem[root] = set(r for n in parent if find(n) == root
                            for r, _p in nl.nets[n] if not glue(r))
        return mem[root]
    for m in sorted(via, key=_natkey):
        ends = via[m]
        if len(ends) != 2 or len(nl.nets[m]) != 2:
            continue
        ra, rb, rm = find(ends[0]), find(ends[1]), find(m)
        if len(set((ra, rb, rm))) < 2:
            continue
        if members(ra) & members(rb):
            continue
        u = members(ra) | members(rb) | members(rm)
        parent[ra] = rm
        parent[rb] = rm
        mem[find(m)] = u      # 被併掉的根不會再被 find 回來，舊快取不用清
    groups = collections.defaultdict(list)
    for n in parent:
        groups[find(n)].append(n)
    return list(groups.values())


def block_links(nl, units, pwr_rx):
    """-> (edges, wide)。edges：{(a, b): [連線代表名...]}；wide：跨
    `_WIDE_NET` 個以上方塊的連線（匯流排、共用致能），另外列，不拆成兩兩。"""
    def glue(rd):
        return len(nl.pins(rd)) == 2 and not units.endpoint(rd)             and not nl.is_mech(rd)
    edges = collections.defaultdict(list)
    wide = []
    for ns in connections(nl, pwr_rx, glue):
        nodes = set()
        for n in ns:
            for r, _p in nl.nets[n]:
                nodes.add(units.node_of(r))
        nodes.discard(None)
        if len(nodes) < 2:
            continue
        named = sorted((n for n in ns if not _RX_AUTONET.match(n)),
                       key=_natkey)
        rep = named[0] if named else sorted(ns, key=_natkey)[0]
        if len(nodes) >= _WIDE_NET:
            wide.append((rep, nodes))
            continue
        for a, b in itertools.combinations(sorted(nodes, key=_nodekey), 2):
            edges[(a, b)].append(rep)
    return edges, wide


def _nodekey(k):
    return [_natkey(u"%s" % (x,)) for x in k]


def mermaid(units, edges, limit=40):
    rank = sorted(edges.items(), key=lambda x: -len(x[1]))[:limit]
    used = []
    for (a, b), _ns in rank:
        for x in (a, b):
            if x not in used:
                used.append(x)
    ids = dict((k, "B%d" % i) for i, k in enumerate(used))
    L = [u"```mermaid", u"graph LR"]
    for k in used:
        L.append(u'  %s["%s"]' % (ids[k], units.name(k).replace(u'"', u"'")))
    for (a, b), ns in rank:
        L.append(u"  %s ---|%d| %s" % (ids[a], len(ns), ids[b]))
    L.append(u"```")
    return L, len(edges) - len(rank)


# ----------------------------------------------------------------------
# 其他段落
# ----------------------------------------------------------------------

def hub_parts(nl, bom, pwr_rx, target, group_of, k=8, pool=40):
    """跨出自己方塊最多的幾顆，各伸到哪裡。

    `target(rd)` 決定落點（方塊或零件）；`group_of(rd)` 決定哪些零件算
    「同一種」而只列一次（×9 的重複區塊裡的同一顆，不列九行）。
    依**落到自己以外的腳數**排序——腳多但全在自己區塊裡的（ADC 接 FPGA
    同一區）不是這段要講的。
    """
    cand = []
    for rd in nl.actives():
        if ndd_classify.is_connector(nl, rd) or not bom.pn(rd):
            continue
        cand.append((_sig_pins(nl, rd, pwr_rx), rd))
    cand.sort(key=lambda x: (-x[0], _natkey(x[1])))
    rows = collections.OrderedDict()
    for nsig, rd in cand[:pool]:
        g = group_of(rd)
        if g in rows:
            rows[g][4] += 1
            continue
        own = target(rd)
        reach = collections.Counter()
        for _p, n in nl.pins(rd).items():
            if not n or _is_power(n, pwr_rx):
                continue
            tg = set(target(r) for r in _neighbors(nl, rd, n, pwr_rx))
            tg.discard(None)
            tg.discard(rd)
            for x in tg:
                reach[x] += 1
        ext = sum(v for x, v in reach.items() if x != own)
        rows[g] = [rd, bom.pn(rd), nsig, reach, 1, ext]
    out = [r for r in rows.values() if r[5] > 0]
    out.sort(key=lambda r: (-r[5], -r[2], _natkey(r[0])))
    return out[:k]


def i2c_buses(nl, pwr_rx, is_key, top=15):
    """SCL 網路上掛了誰（穿過一顆串阻）。匯流排名取 SCL 那條網路。"""
    out = []
    for n in sorted(nl.nets, key=_natkey):
        m = _RX_I2C.search(n)
        if not m or m.group(2).upper() != "SCL" or _is_power(n, pwr_rx):
            continue
        devs = set()
        for r in _neighbors(nl, None, n, pwr_rx):
            if is_key(r):
                devs.add(r)
        if len(devs) < 2:
            continue
        out.append((n, devs))
    out.sort(key=lambda x: -len(x[1]))
    return out[:top]


def major_parts(nl, bom, cfg, cis, pwr_rx, top=25):
    """依「接了幾條非電源訊號」排序的主要零件，**同料號收斂成一列**。
    只在沒有階層的板子上用——有階層時，階層樹已經逐區塊列出主要零件。"""
    taxo = cfg.get("part_class") or {}
    fpov = cfg.get("footprint_class") or {}
    agg = {}
    for rd in nl.actives():
        pn = bom.pn(rd) or ""
        cat, src = ndd_classify.classify(
            rd, pn, board=None, overrides=taxo, cis=cis,
            footprint=nl.parts.get(rd), fp_overrides=fpov)
        if cat not in _MAJOR:
            continue
        key = pn or ("(無料號) " + (nl.parts.get(rd) or rd))
        e = agg.setdefault(key, {"pn": pn, "cat": cat, "src": src,
                                 "rds": [], "sig": 0})
        e["rds"].append(rd)
        e["sig"] = max(e["sig"], _sig_pins(nl, rd, pwr_rx))
    rows = [(e["rds"][0], e["pn"], e["cat"], e["src"], e["sig"], len(e["rds"]))
            for e in agg.values()]
    rows.sort(key=lambda x: (-x[4], -x[5], x[0]))
    return rows[:top]


def connectors(nl, bom):
    """連接器，**同料號＋同腳數收斂成一列**。"""
    agg = {}
    n_total = 0
    for rd in sorted(nl.parts):
        if not ndd_classify.is_connector(nl, rd):
            continue
        n_total += 1
        pn, npin = bom.pn(rd) or "", len(nl.pins(rd))
        agg.setdefault((pn, npin), []).append(rd)
    rows = [(rds, pn, npin) for (pn, npin), rds in agg.items()]
    rows.sort(key=lambda x: (-x[2], -len(x[0])))
    return rows, n_total


# `smt_only` 的 BOM **依定義**不收這幾類，所以它們的缺席不可判定。
# 其餘（一般 SMT 件）在 smt_only 底下**仍然判得出來**——實測 `T902` 這顆
# NMOS 就是這樣確認的未貼件，整段跳過會漏掉真結論。
_NOT_IN_SMT_BOM = ("connector", "mechanical", "test_point", "pcb", "cable",
                   "fiducial")


def dni_parts(nl, bom, cfg, cis, limit=None):
    """netlist 有、BOM 的 `Part Reference` 欄沒有 = 未貼件 (DNI)。"""
    taxo = cfg.get("part_class") or {}
    fpov = cfg.get("footprint_class") or {}
    smt = getattr(bom, "scope", "") == "smt_only"
    out = []
    for rd in sorted(nl.parts):
        if nl.is_mech(rd) or ndd_classify.is_connector(nl, rd):
            continue
        if smt:
            cat, _src = ndd_classify.classify(
                rd, "", board=None, overrides=taxo, cis=cis,
                footprint=nl.parts.get(rd), fp_overrides=fpov)
            if cat in _NOT_IN_SMT_BOM:
                continue
        if bom.of(rd) is None:
            out.append((rd, nl.parts.get(rd, "")))
    return (out[:limit] if limit else out), len(out), smt


def net_families(nl, pwr_rx, min_members=3, top=18):
    """訊號家族——`TX_EN_P_0..8` 收斂成一條 `TX_EN_P ×9`。"""
    fam = collections.Counter()
    for n in nl.nets:
        if _is_power(n, pwr_rx) or _RX_AUTONET.match(n):
            continue
        base = _RX_TAIL_NUM.sub("", n)
        # 單字母 base（`H_1`、`P_3`）幾乎一定是鎖孔／基準點一類的東西。
        if base and base != n and len(base.strip("_")) >= 3:
            fam[base] += 1
    return [(k, v) for k, v in fam.most_common(top) if v >= min_members]


def power_nets(nl, pwr_rx):
    rows = [(n, len(p)) for n, p in nl.nets.items() if _is_power(n, pwr_rx)]
    rows.sort(key=lambda x: (-x[1], _natkey(x[0])))
    return rows


def _mates_of(cfg, key):
    """`mates` 的格式是 `[板A, refdesA, 板B, refdesB]` 四元組。"""
    out = []
    for m in cfg.get("mates") or []:
        if not (isinstance(m, (list, tuple)) and len(m) >= 4):
            continue
        ba, ra, bb, rb = m[0], m[1], m[2], m[3]
        if ba == key:
            out.append((ra, "%s %s" % (bb, rb)))
        elif bb == key:
            out.append((rb, "%s %s" % (ba, ra)))
    return out


def _counted(counter, k=6):
    """`PCA9554 ×2、TLA2024` ——一顆的不寫 ×1。"""
    it = sorted(counter.items(), key=lambda x: (-x[1], _natkey(x[0])))
    s = u"、".join(u"%s ×%d" % (a, b) if b > 1 else a for a, b in it[:k])
    return s + (u" 等 %d 種" % len(it) if len(it) > k else u"")


def _top(counter, k=4, fmt=u"%s %d"):
    it = sorted(counter.items(), key=lambda x: (-x[1], _natkey(u"%s" % x[0])))
    s = u"、".join(fmt % (a, b) for a, b in it[:k])
    return s + (u" 等 %d 處" % len(it) if len(it) > k else u"")


# ----------------------------------------------------------------------
# 逐顆腳位連線（導覽的原料）
# ----------------------------------------------------------------------

# 一條網路上的主要零件超過這麼多顆，就當多點網路（匯流排、共用致能），
# 逐腳表只寫網路名，成員另列在多點網路表。
_P2P_MAX = 3


def _pin_name(hier_pn, rd, pin):
    n = hier_pn.get((rd, pin))
    return (u" %s" % n) if n else u""


def _passive_desc(bom, rd):
    v = bom.value(rd) if hasattr(bom, "value") else ""
    return u"%s %s" % (rd, v or bom.pn(rd) or u"未貼")


# 腳名看起來是供電／接地的——這種腳接到電源軌是本分，§10 不列。
# 其餘接到電源軌或地的腳（`LS`、位址腳、`EN`、模式腳）是**設定**，要列。
_SUPPLY_PIN_RX = re.compile(
    r"^((MGT)?(A|D|P|S|IO)?(VDD|VCC|VSS|VEE|GND|VTT)\w*|\w*_(GND|VSS)\w*"
    r"|P?VIN\d*|EP|EPAD|PAD|VS\d*|V[+-]|THERMAL\w*|N/?C\w*|DNC\w*)$", re.I)
# 只接被動件的腳，往下最多穿過幾顆被動件（分壓、RC、串兩顆電阻）。
_PASSIVE_HOPS = 3


def _passive_chain(nl, bom, net, came, pwr_rx, endpoint, hops, seen):
    """從 `net` 出發穿過被動件，直到碰到主要零件、電源軌或走完 `hops` 顆。
    回傳 `[說明字串]`。"""
    out = []
    for r2, _p2 in nl.nets.get(net, ()):
        if r2 in came or r2 in seen or endpoint(r2) or nl.is_mech(r2):
            continue
        seen.add(r2)
        desc = _passive_desc(bom, r2)
        others = sorted(set(v for v in nl.pins(r2).values() if v and v != net))
        if not others:
            out.append(u"%s → （另一端未接）" % desc)
        for n3 in others:
            if _is_power(n3, pwr_rx):
                out.append(u"%s → `%s`" % (desc, n3))
                continue
            far = [(r4, p4) for r4, p4 in nl.nets.get(n3, ())
                   if r4 != r2 and endpoint(r4)]
            if far:
                out.append(u"%s → %s" % (desc, u"、".join(
                    u"%s.%s" % (r4, p4) for r4, p4 in far)))
                continue
            sub = []
            if hops > 1:
                sub = _passive_chain(nl, bom, n3, came | {r2}, pwr_rx,
                                     endpoint, hops - 1, seen)
            if sub:
                out.extend(u"%s → %s" % (desc, x) for x in sub)
            else:
                out.append(u"%s → （止於 `%s`）" % (desc, n3))
    return out


def pin_far_ends(nl, bom, rd, net, pwr_rx, endpoint):
    """這支腳接到哪些主要零件／連接器的哪支腳。

    穿過**一顆**串聯的 2 腳件（串阻、隔直電容、磁珠、未貼的跳線）——回傳
    `[(遠端 refdes, 遠端腳, 經過的串聯件或 None)]`，以及只接被動件時
    那些被動件的去處（`[(被動件描述, 另一端網路)]`）：終端電阻、上下拉、
    分壓都在這裡，不可講成「懸空」。
    """
    ends, passives = [], []
    for r2, p2 in nl.nets.get(net, ()):
        if r2 == rd:
            continue
        if endpoint(r2):
            ends.append((r2, p2, None))
            continue
        if nl.is_mech(r2):
            continue
        others = [(p3, n3) for p3, n3 in nl.pins(r2).items() if n3 != net]
        if len(nl.pins(r2)) == 2 and others:
            n3 = others[0][1]
            if n3 and not _is_power(n3, pwr_rx):
                far = [(r4, p4) for r4, p4 in nl.nets.get(n3, ())
                       if r4 not in (r2, rd) and endpoint(r4)]
                if far:
                    for r4, p4 in far:
                        ends.append((r4, p4, r2))
    via = set(v for _r, _p, v in ends if v)
    # 測試點是機構件、不算端點，但「這條線有拉測試點」是交接時有用的事實；
    # 不列的話，只接測試點的腳會被寫成「只有這支腳」。
    tps = sorted((r2 for r2, _p in nl.nets.get(net, ())
                  if r2 != rd and nl.is_mech(r2) and r2.upper().startswith("TP")),
                 key=_natkey)
    if not ends:
        passives = _passive_chain(nl, bom, net, {rd}, pwr_rx, endpoint,
                                  _PASSIVE_HOPS, set())
    else:
        # 已經接到零件時，只補上下拉、終端這類直接到電源軌的被動件。
        passives = [x for x in _passive_chain(nl, bom, net, {rd} | via,
                                              pwr_rx, endpoint, 1, set())
                    if u"→ `" in x]
    if tps:
        passives = list(passives) + [u"測試點 %s" % u"、".join(tps)]
    return ends, passives


_RX_SP_TAIL = re.compile(r"\.(Normal|Convert)$")


def source_parts(hier):
    """-> {refdes: symbol 名}，例如 `RF-Mixer_CMD182C4`、`BPF_BFCN-8350plus`。

    symbol 名是設計者在 `.DSN` 選的零件庫名，**前綴就是零件類別**
    （RF-Mixer、BPF、Balun、SWIC…）——讀鏈路時一眼看出每一跳是什麼。
    多 section 零件（`U13-2`、`J4A`）的 section 尾碼剝掉。"""
    out = {}
    if hier is None:
        return out
    for r in hier.real_parts():
        base = r.get("base_refdes") or r["refdes"]
        v = _RX_SP_TAIL.sub("", (r.get("source_part") or "").strip())
        suf = r["refdes"][len(base):] if r["refdes"].startswith(base) else ""
        if suf and v.endswith(suf):
            v = v[:-len(suf)].rstrip("-")
        if v:
            out.setdefault(base, v)
    return out


def _sp_cat(sp):
    """symbol 名的類別前綴：`RF-Mixer_CMD182C4` -> `RF-Mixer`。"""
    return sp.split("_", 1)[0] if sp and "_" in sp else sp


_RX_DIGITS = re.compile(r"(\d+)")
# 同一顆零件接到同一個對象的腳，這個數以內逐支列出，超過就收成家族。
_EDGE_LIST_MAX = 3
# 同一類（設定腳、只接被動件）逐條列出的上限；其餘只報數量。
_KIND_LIST_MAX = 8


def _template(names):
    """一組名字的共同樣式：各處數字都相同就保留，不同就換成 `#`。

    `FPGA_227_SERDES_RX0_N`、`FPGA_227_SERDES_RX1_N` -> `FPGA_227_SERDES_RX#_N`
    ——bank 號 227 保留，只有真的在變的通道號收掉。結構不同回傳 None。"""
    toks = [_RX_DIGITS.split(n) for n in names]
    if len(set(len(t) for t in toks)) != 1:
        return None
    out = []
    for i, col in enumerate(zip(*toks)):
        if i % 2 == 0:
            if len(set(col)) != 1:
                return None
            out.append(col[0])
        else:
            out.append(col[0] if len(set(col)) == 1 else u"#")
    return u"".join(out)


def name_families(names):
    """-> [(樣式, 個數)]。數字換成 `#` 後結構相同的歸一族，族內再求共同樣式。"""
    by = collections.OrderedDict()
    for n in sorted(set(names), key=_natkey):
        by.setdefault(_RX_DIGITS.sub(u"#", n), []).append(n)
    out = []
    for _k, ns in by.items():
        out.append((ns[0] if len(ns) == 1 else (_template(ns) or ns[0]), len(ns)))
    return out


def _fam_str(names, k=4):
    fs = name_families(names)
    s = u"、".join((u"`%s`" % f) if c == 1 else (u"`%s` ×%d" % (f, c))
                  for f, c in fs[:k])
    return s + (u" 等 %d 族" % len(fs) if len(fs) > k else u"")


def part_edges(nl, bom, hpn, rd, pwr_rx, endpoint):
    """一顆主要零件的所有非電源腳，**依對象收斂**。

    -> dict：
      `to`      {對端 refdes: [(本側腳, 本側腳名, 對端腳, 對端腳名, 網路, 串聯件)]}
      `multi`   [(本側腳名或腳號, 網路)]——主要零件超過 `_P2P_MAX` 顆的網路
      `setting` [說明]——腳名不是電源名、卻接到電源軌或地的腳
      `passive` [(腳, 說明)]——只接被動件（上下拉、終端、分壓），或接到零件之外另有上下拉
      `alone`   [腳名或腳號]——網路上沒有別的零件
    """
    is_conn = ndd_classify.is_connector(nl, rd)
    E = {"to": collections.OrderedDict(), "multi": [], "setting": [],
         "passive": [], "alone": []}
    for p, n in nl.pins(rd).items():
        if not n:
            continue
        nm = hpn.get((rd, p)) or u""
        tag = nm or p
        if _is_power(n, pwr_rx):
            if nm and not is_conn and not _SUPPLY_PIN_RX.match(nm):
                # 腳數少的「電源軌」多半是名字像電源的訊號（`*_SHDN`、
                # `*_FB`）——照樣穿過被動件寫出它接到哪。
                tail = u""
                if Fabric.cls(n) != "GND" and len(nl.nets[n]) <= 8:
                    ch = _passive_chain(nl, bom, n, {rd}, pwr_rx, endpoint,
                                        _PASSIVE_HOPS, set())
                    far = [u"%s.%s" % (r, q) for r, q in nl.nets[n]
                           if r != rd and endpoint(r)]
                    tail = u"；".join(far + ch)
                    tail = (u"（%s）" % tail) if tail else u""
                E["setting"].append(u"%s → `%s`%s" % (tag, n, tail))
            continue
        ends, pas = pin_far_ends(nl, bom, rd, n, pwr_rx, endpoint)
        if len(set(r for r, _p, _v in ends)) > _P2P_MAX:
            E["multi"].append((nm or p, n))
            continue
        for r, q, via in ends:
            E["to"].setdefault(r, []).append(
                (p, nm, q, hpn.get((r, q)) or u"", n, via))
        if pas:
            E["passive"].extend((tag, x) for x in pas)
        elif not ends:
            E["alone"].append(nm or p)
    return E


# 被動件描述裡的去處：`網路`、或「refdes.腳」（只留 refdes）。
_RX_TARGET = re.compile(r"`[^`]+`|\b([A-Z]{1,3}\d+)\.\S+")
_RX_REFDES = re.compile(r"\b[A-Z]{1,3}\d+\b")


def passive_summary(items, k=4):
    """只接被動件的腳收成「接法」：同一串被動件值、接到同一類地方的算一種。

    `R325 100ohm → BANK65_3V3_J25` 與 `R169 100ohm → BANK67_1V8_B20` 都是
    「100 Ω 接到電源軌」——逐條列只是在重複。去處收成三類：電源軌／網路、
    某顆零件（不分腳）、止於。"""
    def key(x):
        y = _RX_TARGET.sub(lambda m: (u"@" + m.group(1)) if m.group(1)
                           else u"`網路`", x)
        # 串聯的被動件 refdes 不同不算不同接法；去處零件（`@J3`）保留。
        return _RX_REFDES.sub(lambda m: m.group(0) if y[m.start() - 1:m.start()]
                              == u"@" else u"*", y)
    by = collections.OrderedDict()
    for tag, x in items:
        by.setdefault(key(x), []).append((tag, x))
    rows = sorted(by.values(), key=lambda v: -len(v))
    out = []
    for v in rows[:k]:
        tag, x = v[0]
        out.append((u"%s：%s" % (tag, x)) if len(v) == 1
                   else (u"×%d 支同接法，例 %s：%s" % (len(v), tag, x)))
    if len(rows) > k:
        out.append(u"另 %d 種接法 %d 支" % (len(rows) - k,
                                        sum(len(v) for v in rows[k:])))
    return u"；".join(out)


def edge_lines(nl, bom, hier, sp, rd, pwr_rx, endpoint, where):
    """一顆主要零件的「邊」：接到誰、幾條、什麼訊號——不逐腳列。"""
    hpn = hier.pin_names() if hier is not None else {}
    E = part_edges(nl, bom, hpn, rd, pwr_rx, endpoint)
    L = [u"- **%s** %s%s" % (rd, _label(nl, bom, rd),
                            (u" 〔`%s`〕" % sp[rd]) if rd in sp else u"")]
    for r in sorted(E["to"], key=_natkey):
        xs = E["to"][r]
        who = u"**%s** %s" % (r, _label(nl, bom, r))
        if _sp_cat(sp.get(r)):
            who += u"〔%s〕" % _sp_cat(sp.get(r))
        if where(r) != where(rd):
            who += u" [%s]" % where(r)
        vias = collections.Counter(_passive_desc(bom, v).split(u" ", 1)[-1]
                                   for _a, _b, _c, _d, _n, v in xs if v)
        via = (u"（經 %s）" % u"、".join(
            (u"%s ×%d" % (k, c)) if c > 1 else k for k, c in vias.most_common(3))
               if vias else u"")
        if len(xs) <= _EDGE_LIST_MAX:
            L.append(u"  - ↔ %s：%s%s" % (who, u"；".join(
                u"%s ↔ %s%s" % (
                    a or p, b or q,
                    u"" if _RX_AUTONET.match(n) else u" `%s`" % n)
                for p, a, q, b, n, _v in xs), via))
            continue
        nets = [n for _p, _a, _q, _b, n, _v in xs if not _RX_AUTONET.match(n)]
        mine = [a or p for p, a, _q, _b, _n, _v in xs]
        theirs = [b or q for _p, _a, q, b, _n, _v in xs]
        L.append(u"  - ↔ %s：%d 條%s" % (who, len(xs), via))
        if nets:
            L.append(u"    - 網路 %s" % _fam_str(nets))
        # 腳名收不成家族（FPGA 的腳名帶 bank 與球號，每支都不同）就不列——
        # 列出來只是前三支腳名，對看架構沒有幫助。
        side = [u"%s %s" % (t, _fam_str(x, 3)) for t, x in
                ((u"本側", mine), (u"對側", theirs))
                if len(name_families(x)) <= 3]
        if side:
            L.append(u"    - %s" % u" ↔ ".join(side))
    if E["multi"]:
        L.append(u"  - 多點網路（見 §12）：%s" % _fam_str([n for _t, n in E["multi"]]))
    xs = E["setting"]
    if xs:
        L.append(u"  - 設定腳 %d 支：%s%s" % (
            len(xs), u"；".join(xs[:_KIND_LIST_MAX]),
            u"；等" if len(xs) > _KIND_LIST_MAX else u""))
    if E["passive"]:
        L.append(u"  - 只接被動件 %d 支：%s" % (
            len(set(t for t, _x in E["passive"])), passive_summary(E["passive"])))
    if E["alone"]:
        L.append(u"  - 未接其他零件 %d 支：%s" % (len(E["alone"]),
                                              _fam_str(E["alone"], 6)))
    return L


def edge_blocks(t, nl, bom, hier, pwr_rx, endpoint, where):
    """-> OrderedDict{子電路: [markdown 行]}，每個子電路裡每顆主要零件的邊。"""
    sp = source_parts(hier)
    byb = collections.OrderedDict()
    for rd in sorted(nl.parts, key=lambda r: (_pathkey(t.path.get(r, ())),
                                              _natkey(r))):
        if endpoint(rd):
            byb.setdefault(where(rd), []).append(rd)
    out = collections.OrderedDict()
    for blk, rds in byb.items():
        L = []
        for rd in sorted(rds, key=_natkey):
            L.extend(edge_lines(nl, bom, hier, sp, rd, pwr_rx, endpoint, where))
        out[blk] = L
    return out


_RX_SRC_PIN = re.compile(r"^(V?OUT\w*|SW\d*|LX\d*|VO\d*|PH\d*)$", re.I)


def rail_members(nl, hpn, n, endpoint):
    """一條電源軌上的主要零件。腳名像輸出（`OUT`、`SW`、`LX`）的寫出腳名——
    那多半是這條軌的來源；其餘只列 refdes，供電腳逐支列對看架構沒有幫助。"""
    by = collections.OrderedDict()
    for r, p in sorted(nl.nets[n], key=lambda x: (_natkey(x[0]), x[1])):
        if endpoint(r):
            nm = hpn.get((r, p)) or u""
            by.setdefault(r, set())
            if _RX_SRC_PIN.match(nm):
                by[r].add(nm)
    return u"、".join((u"**%s.%s**" % (r, u"/".join(sorted(v)))) if v else r
                     for r, v in by.items())


def rail_links(nl, bom, pwr_rx, endpoint):
    """電源軌之間經串聯 2 腳件（磁珠、0 Ω、感測電阻）相連的關係。
    -> {軌: [(串聯件描述, 另一條軌)]}。接到地的（旁路電容）不算。"""
    out = collections.defaultdict(list)
    for rd in sorted(nl.parts, key=_natkey):
        if endpoint(rd) or nl.is_mech(rd) or len(nl.pins(rd)) != 2:
            continue
        a, b = list(nl.pins(rd).values())
        if not (a and b and a != b):
            continue
        if not (_is_power(a, pwr_rx) and _is_power(b, pwr_rx)):
            continue
        # 一端是地、另一端不是 = 旁路電容一類，不是串聯。兩端都是地
        # （`PGND` ↔ `GND` 經磁珠）是分割地的接法，要列。
        if (Fabric.cls(a) == "GND") != (Fabric.cls(b) == "GND"):
            continue
        d = _passive_desc(bom, rd)
        out[a].append((d, b))
        out[b].append((d, a))
    return out


def multidrop_nets(nl, pwr_rx, endpoint):
    """主要零件超過 `_P2P_MAX` 顆的訊號網路，逐一列成員（含串一顆被動件）。"""
    out = []
    for n in sorted(nl.nets, key=_natkey):
        if _is_power(n, pwr_rx):
            continue
        mem = set()
        for r, p in nl.nets[n]:
            if endpoint(r):
                mem.add((r, p))
        if len(set(r for r, _p in mem)) > _P2P_MAX:
            out.append((n, sorted(mem, key=lambda x: _natkey(x[0]))))
    return out


# ----------------------------------------------------------------------
# 訊號鏈（深層拓樸）
# ----------------------------------------------------------------------

# 接到這麼多顆主要零件以上的，算「中樞」（FPGA、GPIO 擴充、大接頭）。
_HUB_DEG = 6
# 非電源腳這麼多以上的也算中樞，而且**永遠不是串在路上的一節**——小板上 FPGA
# 可能只接兩顆零件，不能因此被當成放大器那樣穿過去。
_HUB_PINS = 24
# 中樞只接過來這麼多條以內，視為控制線（致能、切換），不算訊號路徑的一端。
_CTRL_MAX = 2
# 訊號路徑一段最多幾條：單端 1 條、差動 2 條。超過就是數位匯流排或並列控制
# （數位衰減器的 5 位元控制、PLL 的 SPI、DAC 的資料匯流排），掛在節點旁、
# 不當成鏈的一段——實測 DPU 的 HMC941 因此每一顆都把 RF 鏈截斷。
_SIG_MAX = 2
# 差動對的兩條算一路：`VINA_I_P_DPU1`／`VINA_I_N_DPU1`、`DATACLKINP`／`DATACLKINN`。
# 只在同一段裡 P、N 都在時才併——這塊板的 `_P` 也可能是 Primary。
_RX_PN = re.compile(r"(?<=[A-Z0-9_])(P|N)(?=_|$)")


class Topology(object):
    """零件層級的拓樸：把「只有兩個對象」的零件收成鏈。

    §10 是每顆零件的一跳鄰居；這裡往下追到底——一顆零件（放大器、濾波器、
    衰減器、balun、緩衝器）只接兩個對象時是**串在路上**的一節，鏈穿過它
    繼續走，直到碰到分岔點（開關、混頻器、功分器、FPGA）或連接器才停。
    結果就是設計者畫在方塊圖上的那一條條鏈，只是**沒有方向**。

    - 連接器、大顆零件（`_HUB_PINS` 支訊號腳以上）永遠是鏈的端點。
    - 中樞（接 `_HUB_DEG` 顆以上，或大顆零件）只用 `_CTRL_MAX` 條以內的線接過來時，當成
      控制線掛在節點旁，不算路徑的一端——否則每顆有致能腳的放大器都會被
      FPGA 截斷成分岔點。
    - 超過 `_SIG_MAX` 條的連線（匯流排、並列控制）同樣掛在節點旁：鏈是單端
      或差動訊號走的路。
    - 主要零件超過 `_P2P_MAX` 顆的網路是匯流排，不參與成鏈。
    """

    def __init__(self, nl, bom, sp, pwr_rx, endpoint, hpn=None):
        self.nl, self.bom, self.sp = nl, bom, sp
        self.hpn = hpn or {}
        self.endpoint = endpoint

        def glue(rd):
            return (len(nl.pins(rd)) == 2 and not endpoint(rd)
                    and not nl.is_mech(rd))
        groups = connections(nl, pwr_rx, glue)
        self.nets = groups
        gid = {}
        for i, ns in enumerate(groups):
            for n in ns:
                gid[n] = i
        # 同一組裡的串聯件（串阻、隔直電容、磁珠）。
        self.series = collections.defaultdict(set)
        for rd in nl.parts:
            if not glue(rd):
                continue
            ns = [n for n in nl.pins(rd).values() if n in gid]
            if len(ns) == 2 and ns[0] != ns[1] and gid[ns[0]] == gid[ns[1]]:
                self.series[gid[ns[0]]].add(rd)
        self.members = collections.defaultdict(set)     # 組 -> {(rd, 腳)}
        for i, ns in enumerate(groups):
            for n in ns:
                for r, p in nl.nets[n]:
                    if endpoint(r):
                        self.members[i].add((r, p))
        self.adj = collections.defaultdict(
            lambda: collections.defaultdict(list))      # rd -> 對象 -> [組]
        for g, mem in self.members.items():
            rds = set(r for r, _p in mem)
            if len(rds) < 2 or len(rds) > _P2P_MAX:
                continue
            for a in rds:
                for b in rds:
                    if a != b:
                        self.adj[a][b].append(g)
        self._core = {}
        self._big = dict((r, _sig_pins(nl, r, pwr_rx) >= _HUB_PINS)
                         for r in self.adj)

    def is_conn(self, rd):
        return ndd_classify.is_connector(self.nl, rd)

    def hub(self, rd):
        return self._big.get(rd) or len(self.adj[rd]) >= _HUB_DEG

    def ctrl(self, rd, nb):
        """`nb` 接到 `rd` 的線不算訊號路徑：中樞來的少數控制線，或多條並列的
        匯流排／控制。"""
        if self.is_conn(rd):
            return False
        n = len(self.adj[rd][nb])
        return self.width(rd, nb) > _SIG_MAX or (self.hub(nb) and n <= _CTRL_MAX)

    def width(self, rd, nb):
        """rd 與 nb 之間有幾路訊號：差動對算一路。"""
        names = []
        for g in self.adj[rd][nb]:
            ns = sorted(self.nets[g], key=_natkey)
            names.append(ns[0].upper())
        by = collections.defaultdict(set)
        for n in names:
            for m in _RX_PN.finditer(n):
                by[n[:m.start()] + u"#" + n[m.end():]].add((m.group(1), n))
        paired = set()
        for k, v in by.items():
            if set(x for x, _n in v) == {u"P", u"N"}:
                paired |= set(n for _x, n in v)
        return len(names) - len(paired) // 2

    def core(self, rd):
        if rd not in self._core:
            self._core[rd] = sorted(
                (nb for nb in self.adj[rd] if not self.ctrl(rd, nb)), key=_natkey)
        return self._core[rd]

    def passthru(self, rd):
        return (not self.is_conn(rd) and not self._big.get(rd)
                and len(self.core(rd)) == 2)

    def chains(self):
        """-> [[refdes...]]，每條至少穿過一顆串在路上的零件；正反只留一份。"""
        out, seen = [], set()
        for s in sorted(self.adj, key=_natkey):
            if self.passthru(s):
                continue
            for nb in self.core(s):
                if not self.passthru(nb) or s not in self.core(nb):
                    continue
                path, prev, cur = [s], s, nb
                while self.passthru(cur) and cur not in path:
                    c = self.core(cur)
                    if prev not in c:
                        break
                    path.append(cur)
                    prev, cur = cur, (c[1] if c[0] == prev else c[0])
                if cur in path:         # 繞回自己：環，不是鏈
                    continue
                path.append(cur)
                if len(path) < 3:
                    continue
                k = tuple(path)
                k = min(k, k[::-1])
                if k in seen:
                    continue
                seen.add(k)
                out.append(list(k))
        return out

    # ---- 顯示 --------------------------------------------------------
    def label(self, rd):
        s = _label(self.nl, self.bom, rd)
        cat = _sp_cat(self.sp.get(rd))
        return (u"%s〔%s〕" % (s, cat)) if cat else s

    def _pin(self, rd, pins):
        nm = [self.hpn.get((rd, p)) or p for p in sorted(pins, key=_natkey)]
        return _fam_str(nm, 2) if len(nm) > 1 else nm[0]

    def link(self, a, b):
        """a 與 b 之間：幾條、代表網路、經過的串聯件。"""
        gs = self.adj[a][b]
        names, via = [], []
        for g in gs:
            ns = [n for n in self.nets[g] if not _RX_AUTONET.match(n)]
            if ns:
                names.append(sorted(ns, key=_natkey)[0])
            via += sorted(self.series[g], key=_natkey)
        s = (_fam_str(names, 2) if names else u"")
        if len(gs) > 1:
            s = (u"%d 條 %s" % (len(gs), s)).strip()
        if via:
            # 串聯件寫 refdes＋值：未貼的那顆是誰，交接時一定會被問。
            s += u"（經 %s%s）" % (u"、".join(_passive_desc(self.bom, r)
                                           for r in via[:3]),
                                 u" 等 %d 顆" % len(via) if len(via) > 3 else u"")
        return u" ─%s─ " % (u" %s " % s if s else u"")

    def end_pins(self, rd, nb):
        return set(p for g in self.adj[rd][nb]
                   for r, p in self.members[g] if r == rd)

    def ctrl_note(self, rd):
        hs = sorted((nb for nb in self.adj[rd] if self.ctrl(rd, nb)), key=_natkey)
        if not hs:
            return u""
        return u"［另接控制 %s］" % u"、".join(
            u"%s %d 條" % (h, len(self.adj[rd][h])) for h in hs)

    def line(self, path, where=None):
        """一條鏈的完整寫法（含 refdes、端點腳名、串聯件、控制線、所在子電路）。"""
        s = []
        for i, rd in enumerate(path):
            node = u"**%s** %s" % (rd, self.label(rd))
            if i == 0:
                node = u"**%s**.%s %s" % (rd, self._pin(rd, self.end_pins(rd, path[1])),
                                          self.label(rd))
            elif i == len(path) - 1:
                node = u"**%s**.%s %s" % (rd, self._pin(rd, self.end_pins(rd, path[-2])),
                                          self.label(rd))
            else:
                node += self.ctrl_note(rd)
            s.append(node)
            if i < len(path) - 1:
                s.append(self.link(rd, path[i + 1]))
        tail = u""
        if where is not None:
            locs = []
            for rd in path:
                w = where(rd)
                if w not in locs:
                    locs.append(w)
            tail = u"　@ %s" % u" → ".join(locs)
        return u"".join(s) + tail

    def sig(self, path):
        """同構鏈的簽章：各節料號／類別與條數。"""
        out = []
        for i, rd in enumerate(path):
            out.append(self.label(rd))
            if i < len(path) - 1:
                out.append(len(self.adj[rd][path[i + 1]]))
        return tuple(out)


def chain_families(topo, where):
    """-> [(簽章, [路徑...])]：同構的鏈收成一族（×9 通道只寫一次）。
    正反向統一成簽章較小的那個方向。"""
    fam = collections.OrderedDict()
    for p in topo.chains():
        a, b = topo.sig(p), topo.sig(p[::-1])
        if b < a:
            p, a = p[::-1], b
        fam.setdefault(a, []).append(p)
    rows = list(fam.items())
    rows.sort(key=lambda x: (-len(x[1][0]), -len(x[1]),
                             _natkey(x[1][0][0])))
    return rows


def _loc_template(paths, where):
    """一族鏈所在子電路的共同樣式（`UC1 / CA1`…`CA9` -> `UC1 / CA#`）。"""
    locs = []
    for p in paths:
        ls = []
        for rd in p:
            w = where(rd)
            if w not in ls:
                ls.append(w)
        locs.append(u" → ".join(ls))
    fs = name_families(locs)
    return u"、".join(f for f, _c in fs[:3]) + (u" 等" if len(fs) > 3 else u"")


def chain_lines(topo, fams, where, full=True, start=1):
    """鏈族的 markdown。`full`：每一組都列 refdes（Facts、分塊材料）；
    否則只列第一組與倍率（給 A 看全貌）。"""
    L = []
    for i, (_sig, ps) in enumerate(fams, start):
        head = u"- **C%d**" % i
        if len(ps) > 1:
            head += u" ×%d 組同構" % len(ps)
        head += u"（%d 節）：" % len(ps[0])
        L.append(head + topo.line(ps[0], None))
        L.append(u"  - 所在：%s" % _loc_template(ps, where))
        if len(ps) > 1:
            if full:
                for p in ps[1:]:
                    L.append(u"  - 同構：%s" % u" ─ ".join(p))
            else:
                L.append(u"  - 其餘 %d 組起點 %s" % (len(ps) - 1,
                                                  _rng([p[0] for p in ps[1:]])))
    return L


def _topo_overview(topo, fams, where):
    """給 A 看全貌的拓樸：鏈族只寫第一組與倍率，加交會點表。"""
    L = chain_lines(topo, fams, where, full=False)
    jn = junctions(topo, fams)
    if jn:
        L += [u"", u"### 鏈的交會點", u"",
              u"| 零件 | 顆數 | 每顆接幾條鏈 | 例 |", u"|---|---|---|---|"]
        L += [u"| %s | %d | %.1f | `%s` |" % (lab, n, avg, rd)
              for lab, n, avg, rd in jn]
    return L


def junctions(topo, fams, k=25):
    """鏈的交會點：一顆零件是幾條鏈的端點——開關、合成器、混頻器通常在這。
    同料號收斂。-> [(標籤, 顆數, 每顆平均鏈數, 例 refdes)]"""
    cnt = collections.Counter()
    for _s, ps in fams:
        for p in ps:
            cnt[p[0]] += 1
            cnt[p[-1]] += 1
    by = collections.OrderedDict()
    for rd, c in sorted(cnt.items(), key=lambda x: (-x[1], _natkey(x[0]))):
        if c < 2:
            continue
        lab = topo.label(rd)
        e = by.setdefault(lab, [0, 0, rd])
        e[0] += 1
        e[1] += c
    rows = [(lab, n, float(tot) / n, rd) for lab, (n, tot, rd) in by.items()]
    rows.sort(key=lambda x: (-x[2] * x[1], _natkey(x[0])))
    return rows[:k]


# ----------------------------------------------------------------------
# 組裝
# ----------------------------------------------------------------------

def render(key, label, nl, bom, hier, cfg, cis, missing_pn=None, out=None):
    """產生一份板卡事實表的 markdown 文字。

    `out` 給一個 dict 時，另外填入切包（`pack`）要用的結構：各節起始行、
    各子電路的邊、多點網路、階層 port。"""
    pwr_rx = re.compile(cfg.get("power_net_regex") or r"^(GND|VCC|VDD)", re.I)
    L = []
    w = L.append
    marks = {}
    ports = []

    def _sec(k):
        marks[k] = len(L)
    t = Tree(hier, nl)
    flat = hier is None or t.is_flat()
    is_key = KeyPart(nl, bom, cfg, cis)
    units = None if flat else Units(t, bom, is_key)

    def where(rd):
        """零件所在的子電路（完整路徑）。"""
        p = t.path.get(rd) or ()
        return u" / ".join(p) if p else ROOT_LABEL

    w(u"# %s — 板卡事實表" % label)
    w(u"")
    w(u"> **這是 `init` 自動產生的事實表**，只含**不需要規格書就能斷言的事實**"
      u"（`[N]` netlist／`[B]` BOM／`[S]` 階層）。")
    w(u"> 它是撰寫 `%s_Architecture.md`（給人讀的板卡導覽）的材料。" % key)
    w(u"> 腳位功能、訊號方向、極性、某條路通不通——那些要 `[D]` 規格書，"
      u"**這份文件不會有**，見最後一節。連線一律**沒有方向**。")
    w(u"> 逐腳查詢請用 `ndd.py --board %s pins/net/part`，不要靠本文。" % key)
    w(u"")

    # --- 1 規模 ---------------------------------------------------------
    conns, n_conn = connectors(nl, bom)
    _sec(1)
    w(u"## 1. 規模")
    w(u"")
    w(u"| | |")
    w(u"|---|---|")
    w(u"| 零件 | %d 顆（其中 active %d 顆） |" % (len(nl.parts), len(nl.actives())))
    w(u"| 網路 | %d 條 |" % len(nl.nets))
    w(u"| 連接器 | %d 顆（%d 種）|" % (n_conn, len(conns)))
    w(u"")

    # --- 2 組成 ---------------------------------------------------------
    w(u"## 2. 板子的組成 `[S]`（先看這段）")
    w(u"")
    if hier is None:
        w(u"（沒有階層資料——`.DSN` 未轉檔，這段無法產生）")
    elif not t.kids.get(()):
        w(u"這塊板的原理圖**沒有子電路**，全部零件都在頂層。§3 改用零件之間的連線。")
    else:
        w(u"`.DSN` 的子電路，由外而內。**重複的收斂成一行**（倍率通常就是系統架構），"
          u"**組成幾乎相同的兩兩並列**（常見於主／備）。每行後面是該處的主動件 `[B]`。")
        if flat:
            w(u"")
            w(u"⚠️ %d 顆裡有 %d 顆在頂層、不在任何子電路——階層只切了一小部分，"
              u"§3 改用零件之間的連線。" % (t.n, len(t.direct[()])))
        w(u"")
        L.extend(tree_lines(t, bom, is_key))
        w(u"")
        w(u"⚠️ 子電路代表**設計者怎麼切分電路**，不代表訊號怎麼走。")
    w(u"")

    # --- 3 連線 ---------------------------------------------------------
    if not flat:
        edges, wide = block_links(nl, units, pwr_rx)
        w(u"## 3. 子電路之間怎麼連 `[N]`")
        w(u"")
        w(u"兩個方塊之間有幾條**非電源**訊號相連（穿過串阻、隔直電容、磁珠仍算"
          u"同一條）。端點只算主要零件與連接器。3 組以上完全相同的子電路併成一個"
          u"方塊；佔全板 %d%% 以上的子電路拆開一層畫，不在子電路裡的零件依料號"
          u"各成一塊。" % int(_EXPAND_FRAC * 100))
        w(u"")
        if not edges:
            w(u"（子電路之間沒有直接相連的訊號網路）")
        else:
            ml, hidden = mermaid(units, edges)
            L.extend(ml)
            if hidden:
                w(u"")
                w(u"（圖上只畫條數最多的 40 條連線，另有 %d 條見下表）" % hidden)
            w(u"")
            w(u"| 方塊 | 方塊 | 訊號數 | 代表網路 |")
            w(u"|---|---|---|---|")
            for (a, b), ns in sorted(edges.items(),
                                     key=lambda x: (-len(x[1]), _nodekey(x[0][0]))):
                w(u"| %s | %s | %d | %s |" % (units.name(a), units.name(b),
                                              len(ns), _sample(ns)))
        w(u"")
        if wide:
            w(u"**跨 %d 個以上方塊的共用網路**（匯流排、共用致能一類）：" % _WIDE_NET)
            w(u"")
            w(u"| 網路 | 碰到幾個方塊 | 方塊 |")
            w(u"|---|---|---|")
            wide.sort(key=lambda x: (-len(x[1]), _natkey(x[0])))
            for n, nodes in wide:
                names = sorted((units.name(x) for x in nodes), key=_natkey)
                w(u"| `%s` | %d | %s |" % (n, len(nodes), u"、".join(names[:5])
                                           + (u" 等" if len(names) > 5 else u"")))
            w(u"")

        hubs = hub_parts(nl, bom, pwr_rx, units.node_of,
                         lambda r: (bom.pn(r), units.node_of(r)), k=8)
        w(u"## 4. 跨子電路的零件 `[N]`")
        w(u"")
        if not hubs:
            w(u"（訊號腳最多的零件都只接在自己的子電路裡）")
        else:
            w(u"訊號腳伸出自己方塊最多的幾顆，每支訊號腳（穿過一顆串阻／電容也算）"
              u"落在哪些方塊。這通常就是控制中樞。")
            w(u"")
            w(u"| 零件 | 料號 `[B]` | 所在 `[S]` | 訊號腳 | 落在（腳數） |")
            w(u"|---|---|---|---|---|")
            for rd, pn, nsig, reach, cnt, _ext in hubs:
                named = collections.Counter(
                    dict((units.name(k2), v) for k2, v in reach.items()))
                who = (u"`%s` 等 %d 顆" % (rd, cnt)) if cnt > 1 else u"`%s`" % rd
                w(u"| %s | %s | %s | %d | %s |" % (who, pn, where(rd), nsig,
                                                  _top(named, 5)))
        w(u"")
    else:
        w(u"## 3. 主要零件之間怎麼連 `[N]`")
        w(u"")
        rows = major_parts(nl, bom, cfg, cis, pwr_rx)
        hub_pn = set(pn for _rd, pn, _c, _s, _n, _cnt in rows[:15] if pn)
        cset = set(r for rds, _pn, _np in conns for r in rds)

        def tgt(r):
            if r in cset:
                return u"`%s` %s" % (r, bom.pn(r) or u"")
            pn = bom.pn(r)
            return pn if pn in hub_pn else None
        hubs = hub_parts(nl, bom, pwr_rx, tgt, bom.pn, k=12)
        if not hubs:
            w(u"（無）")
        else:
            w(u"訊號腳最多的幾種料號，各自（穿過一顆串阻／電容也算）接到哪些主要零件"
              u"與連接器；同料號只列一行。")
            w(u"")
            w(u"| 零件 | 料號 `[B]` | 訊號腳 | 接到（網路數） |")
            w(u"|---|---|---|---|")
            for rd, pn, nsig, reach, cnt, _ext in hubs:
                who = (u"`%s` 等 %d 顆" % (rd, cnt)) if cnt > 1 else u"`%s`" % rd
                w(u"| %s | %s | %d | %s |" % (who, pn, nsig,
                                             _top(reach, 5) or u"—"))
        w(u"")
        w(u"## 4. 主要零件 `[B]`")
        w(u"")
        if not rows:
            w(u"（無）")
        else:
            w(u"| 料號 `[B]` | 幾顆 | 分類 | 非電源訊號腳 `[N]` | 代表 refdes |")
            w(u"|---|---|---|---|---|")
            for rd, pn, cat, src, n, cnt in rows:
                mark = u"" if src == ndd_classify.SRC_CIS else u" `[?]`"
                w(u"| %s | %d | %s%s | %d | `%s` |"
                  % (pn or u"未貼 %s" % nl.parts.get(rd, ""), cnt, cat, mark,
                     n, rd))
            w(u"")
            w(u"⚠️ 分類標 `[?]` 的是從 footprint／refdes 前綴**推**出來的。")
        w(u"")

    # --- 5 I2C ----------------------------------------------------------
    buses = i2c_buses(nl, pwr_rx, is_key)
    w(u"## 5. I2C 匯流排 `[N]`")
    w(u"")
    if not buses:
        w(u"（沒有名稱帶 `SCL` 且掛兩顆以上主動件的網路）")
    else:
        w(u"依網路名認出的 SCL（穿過一顆串阻也算），上面掛了哪些主動件。")
        w(u"")
        w(u"| SCL 網路 | 幾顆 | 掛載 `[B]` |")
        w(u"|---|---|---|")
        for n, devs in buses:
            c = collections.Counter(_label(nl, bom, r) for r in devs)
            w(u"| `%s` | %d | %s |" % (n, len(devs), _counted(c, 6)))
    w(u"")

    # --- 6 對外介面 -----------------------------------------------------
    w(u"## 6. 對外介面")
    w(u"")
    mate_of = dict(_mates_of(cfg, key))
    if not conns:
        w(u"（無連接器）")
    else:
        w(u"| 料號 `[B]` | 幾顆 | 腳數 `[N]` | refdes | 所在 `[S]` | 已定案的對接 |")
        w(u"|---|---|---|---|---|---|")
        for rds, pn, npin in conns:
            locs = collections.Counter(where(r) for r in rds)
            loc = u"、".join(sorted(locs, key=_natkey)[:3]) + (
                u" 等" if len(locs) > 3 else u"")
            mt = u"、".join(u"`%s`→%s" % (r, mate_of[r])
                            for r in rds if r in mate_of) or u"—"
            w(u"| %s | %d | %d | %s | %s | %s |"
              % (pn or u"—", len(rds), npin, _rng(rds), loc, mt))
        w(u"")
        w(u"「已定案的對接」只列板對板接頭寫進 `ndd.json` `mates` 的；空白**不代表"
          u"沒對接**，跑 `ndd.py mate` 看排名與證據。同軸、線纜埠接到哪裡"
          u"**不在任何一份來源裡**。")
    w(u"")

    # --- 7 電源 ---------------------------------------------------------
    w(u"## 7. 電源軌 `[N]`")
    w(u"")
    pw = power_nets(nl, pwr_rx)
    if not pw:
        w(u"⚠️ **一條都沒認出來**——`power_net_regex` 可能還沒設。"
          u"追跡會因此穿過每顆 2-pin 被動件走遍全板。")
    else:
        w(u"| 網路 | 腳數 | 供應到 `[S]` |")
        w(u"|---|---|---|")
        big = [(n, c) for n, c in pw if c >= _RAIL_MIN][:60]
        for n, c in big:
            locs = collections.Counter(where(r) for r, _p in nl.nets[n])
            w(u"| `%s` | %d | %s |" % (n, c, _top(locs, 3) if not flat
                                       or len(locs) > 1 else u"—"))
        if len(pw) > len(big):
            w(u"")
            w(u"（只列 %d 支腳以上的；另有 %d 條較小的軌，多半是濾波後的區域供電、"
              u"或名字像電源的訊號）" % (_RAIL_MIN, len(pw) - len(big)))
        w(u"")
        w(u"⚠️ 這是 `power_net_regex` **目前判得出來的**。漏設一條的症狀是追跡落點"
          u"爆量；多收一條訊號的症狀是**路徑無聲消失**。")
    w(u"")

    # --- 8 未貼件 -------------------------------------------------------
    w(u"## 8. 未貼件 `[B]`")
    w(u"")
    dni, total, smt = dni_parts(nl, bom, cfg, cis)
    if smt:
        w(u"⚠️ 這份 BOM 是 `smt_only`，**依定義不列連接器／測試點／鎖孔／手插件**，"
          u"那幾類的缺席**不可判定**，已排除。")
        w(u"")
    if not total:
        w(u"（無）")
    else:
        w(u"netlist 有、BOM 的 `Part Reference` 欄沒有 = **未貼件 (DNI)**，共 %d 顆。"
          u"依子電路分組，主要零件逐顆列出——**整組沒貼**通常比單顆重要：" % total)
        w(u"")
        byb = collections.OrderedDict()
        for rd, fp in dni:
            byb.setdefault(where(rd), []).append((rd, fp))
        rows = []
        for b, items in byb.items():
            act = [(r, fp) for r, fp in items if is_key(r)]
            rows.append((b, act, len(items) - len(act)))
        rows.sort(key=lambda x: (-len(x[1]), -x[2], _natkey(x[0])))
        w(u"| 子電路 | 主要零件 | 其他（被動件、磁珠…） |")
        w(u"|---|---|---|")
        for b, act, npas in rows:
            a = u"、".join(u"`%s`（%s）" % (r, fp) for r, fp in
                          sorted(act, key=lambda x: _natkey(x[0]))[:8])
            if len(act) > 8:
                a += u" 等 %d 顆" % len(act)
            w(u"| %s | %s | %s |" % (b, a or u"—", npas or u"—"))
    w(u"")

    # --- 9 訊號家族 -----------------------------------------------------
    fams = net_families(nl, pwr_rx, top=12)
    w(u"## 9. 訊號家族 `[N]`")
    w(u"")
    if not fams:
        w(u"（沒有偵測到帶序號的訊號家族）")
    else:
        w(u"序號結尾的網路收斂成一條看：")
        w(u"")
        w(u"| 家族 | 條數 |")
        w(u"|---|---|")
        for k, v in fams:
            w(u"| `%s_*` | %d |" % (k, v))
    w(u"")

    # --- 10 零件之間的邊 ------------------------------------------------
    endpoint = (lambda r: is_key(r) or ndd_classify.is_connector(nl, r))
    hpn = hier.pin_names() if hier is not None else {}
    _sec(10)
    w(u"## 10. 逐顆零件接到誰 `[N]` `[S]`")
    w(u"")
    w(u"每個子電路裡的每顆主要零件（含連接器），**依對象收斂**：接到哪顆主要零件、"
      u"幾條、什麼網路。零件後的〔 〕是 `.DSN` 的 symbol 名，對端後的〔 〕是它的"
      u"類別前綴（`RF-Mixer`、`BPF`、`Balun`…）`[S]`；對端在別的子電路時以 `[ ]` "
      u"標出。穿過**一顆**串聯件（串阻、隔直電容、磁珠）照樣算接到。%d 條以內逐支"
      u"列出，超過就收成家族：數字相同的保留，會變的換成 `#`。只接被動件的腳往下"
      u"穿過最多 %d 顆被動件（終端電阻、上下拉、分壓——**不是懸空**）。腳名不是"
      u"電源名、卻接到電源軌或地的腳是「設定腳」（位址、致能綁死）。"
      u"**沒有方向**。逐腳明細用 `ndd.py --board %s pins <refdes>`。"
      % (_EDGE_LIST_MAX, _PASSIVE_HOPS, key))
    w(u"")
    eb = edge_blocks(t, nl, bom, hier, pwr_rx, endpoint, where)
    for blk, lines in eb.items():
        w(u"### %s" % blk)
        w(u"")
        L.extend(lines)
        w(u"")

    # --- 11 訊號鏈 ------------------------------------------------------
    _sec(11)
    topo = Topology(nl, bom, source_parts(hier), pwr_rx, endpoint, hpn)
    fams = chain_families(topo, where)
    w(u"## 11. 訊號鏈：往下追到底的拓樸 `[N]` `[S]`")
    w(u"")
    w(u"§10 是每顆零件的一跳鄰居；這裡把它們**串起來**。只接兩個對象的主要零件"
      u"（放大器、濾波器、衰減器、balun、緩衝器一類）是串在路上的一節，鏈穿過它"
      u"繼續走，直到碰到分岔點（接三個以上對象的零件）或連接器才停——這就是方塊圖"
      u"上的一條條鏈。串聯件（串阻、隔直電容、磁珠）寫在兩節之間的括號裡。"
      u"鏈是單端或差動訊號走的路：接 %d 顆以上零件的中樞只用 %d 條以內的線接過來"
      u"（致能、切換），或兩顆之間超過 %d 條並列（數位控制、資料匯流排），都當"
      u"控制寫在節點後的［ ］裡，不截斷鏈。匯流排（一條網路上超過 %d 顆主要零件）"
      u"不參與成鏈。"
      u"**同構的鏈收成一族**，第一組寫全、其餘列 refdes。**沒有方向**——"
      u"從哪端寫起只是排序。" % (_HUB_DEG, _CTRL_MAX, _SIG_MAX, _P2P_MAX))
    w(u"")
    chains_by = []
    if not fams:
        w(u"（沒有穿過任何串接零件的鏈）")
    else:
        L.extend(chain_lines(topo, fams, where, full=True))
        for i, (_sg, ps) in enumerate(fams, 1):
            chains_by.append((i, [(set(p), topo.line(p, where), u" ─ ".join(p))
                                  for p in ps]))
        w(u"")
        jn = junctions(topo, fams)
        if jn:
            w(u"**鏈的交會點**（一顆零件是幾條鏈的端點；同料號收斂）——分岔、"
              u"合成、切換多半在這裡：")
            w(u"")
            w(u"| 零件 | 顆數 | 每顆接幾條鏈 | 例 |")
            w(u"|---|---|---|---|")
            for lab, n, avg, rd in jn:
                w(u"| %s | %d | %.1f | `%s` |" % (lab, n, avg, rd))
    w(u"")

    # --- 12 多點網路 ----------------------------------------------------
    _sec(12)
    w(u"## 12. 多點網路 `[N]`")
    w(u"")
    w(u"一條網路上的主要零件超過 %d 顆（匯流排、共用致能、共用時脈）。" % _P2P_MAX)
    w(u"")
    multi = []
    for n, mem in multidrop_nets(nl, pwr_rx, endpoint):
        line = u"- `%s`（%d 顆）：%s" % (n, len(set(r for r, _p in mem)), u"、".join(
            u"%s.%s%s（%s）" % (r, p, _pin_name(hpn, r, p), _label(nl, bom, r))
            for r, p in mem))
        multi.append((set(r for r, _p in mem), line))
        w(line)
    w(u"")

    # --- 13 電源軌 ------------------------------------------------------
    _sec(13)
    w(u"## 13. 電源軌上的主要零件 `[N]` `[S]`")
    w(u"")
    w(u"每條電源軌（`GND` 類除外）接到哪些主要零件。腳名是 `OUT`／`VOUT`／`SW` 一類"
      u"的粗體寫出腳名，通常就是這條軌的來源 `[?]`。軌與軌之間經串聯件"
      u"（磁珠、0 Ω、感測電阻）相連的，列在該軌下方——穩壓器輸出經磁珠分出去的子軌"
      u"靠這個接回來源。")
    w(u"")
    links = rail_links(nl, bom, pwr_rx, endpoint)
    for n, _c in power_nets(nl, pwr_rx):
        lk = links.get(n, [])
        if Fabric.cls(n) == "GND":
            # 地只列分割地的串聯件（`PGND` ↔ `GND`），不列上千支腳。
            if lk:
                w(u"- `%s`（地）" % n)
                for d, other in lk:
                    w(u"  - 經 %s ↔ `%s`" % (d, other))
            continue
        mem = rail_members(nl, hpn, n, endpoint)
        if not mem and not lk:
            continue
        w(u"- `%s`：%s" % (n, mem or u"—"))
        for d, other in lk:
            w(u"  - 經 %s ↔ `%s`" % (d, other))
    w(u"")
    w(u"### 連接器的電源／地腳")
    w(u"")
    for rds, _pn, _np in conns:
        for r in rds:
            by = collections.OrderedDict()
            for p, n in nl.pins(r).items():
                if n and _is_power(n, pwr_rx):
                    by.setdefault(n, []).append(p)
            if by:
                w(u"- **%s**：%s" % (r, u"；".join(
                    u"`%s` ×%d（%s）" % (n, len(ps), _rng(ps))
                    for n, ps in sorted(by.items(), key=lambda x: -len(x[1])))))
    w(u"")

    # --- 14 階層 port ---------------------------------------------------
    _sec(14)
    w(u"## 14. 子電路的階層 port `[S]`")
    w(u"")
    w(u"設計者在 `.DSN` 每個子電路方塊上畫的對外接點（port 名 → 實際接到的網路）。"
      u"這是設計者自己定義的方塊邊界。")
    w(u"")
    if hier is not None:
        byp = collections.OrderedDict()
        for r in sorted(hier.ports(), key=lambda r: (
                _pathkey(hier.path_of(r["owner_id"])), _natkey(r["pin_name"]))):
            byp.setdefault(u" / ".join(hier.path_of(r["owner_id"])), []).append(
                (r["pin_name"], r["net"]))
        for b, ps in byp.items():
            line = u"- **%s**：%s" % (b, u"、".join(
                (u"`%s`" % a) if a == n else (u"`%s`→`%s`" % (a, n))
                for a, n in ps))
            ports.append((b, line))
            w(line)
    else:
        w(u"（沒有階層資料）")
    w(u"")

    # --- 15 待查證 ------------------------------------------------------
    _sec(15)
    w(u"## 15. 這份文件還不知道什麼")
    w(u"")
    w(u"**以下每一項都需要規格書或人工判斷，`init` 產不出來。**")
    w(u"")
    w(u"- [ ] **訊號方向與功能** —— §3、§11 只說「相連」。誰驅動誰、哪支是致能，"
      u"要 `[D]`；鏈路用 `ndd.py trace --board %s --from <起點>` 追" % key)
    w(u"- [ ] **重複與成對代表什麼** —— 階層只說「設計者這樣切」，"
      u"沒說那是 N 個 slot、N 路通道還是主／備")
    w(u"- [ ] **為什麼這樣設計** —— 腳本永遠寫不出來，要人接手")
    if missing_pn:
        w(u"- [ ] **缺 %d 份規格書** —— 見 `datasheets/INDEX.md`" % missing_pn)
    w(u"")
    if out is not None:
        out.update(lines=L, marks=marks, blocks=eb, multi=multi, ports=ports,
                   chains=chains_by, tree=t, nl=nl, bom=bom, hpn=hpn,
                   pwr_rx=pwr_rx, label=topo.label,
                   topo=_topo_overview(topo, fams, where),
                   path=t.path, where=where, n_parts=len(nl.parts))
    return u"\n".join(L) + u"\n"


PACK_DIR = "arch_pack"


def pack_groups(path, n_parts):
    """子電路路徑 -> 分塊名。頂層子電路一塊；佔全板 `_EXPAND_FRAC` 以上的拆一層。

    和 §3 方塊圖同一條拆法，分塊包才對得上圖上的方塊。"""
    size = collections.Counter(p[0] for p in path.values() if p)
    big = set(k for k, c in size.items() if c >= _EXPAND_FRAC * n_parts)

    def grp(p):
        if not p:
            return ROOT_LABEL
        return u" / ".join(p[:2]) if p[0] in big and len(p) > 1 else p[0]
    return grp


def _slug(s):
    return re.sub(r"[^0-9A-Za-z_-]+", "_", s).strip("_") or "root"


# 每份包的上限：撰寫 agent 一次 Read 讀得完（實測 60 KB 以上的包會被分段讀，
# 每多一段就多一輪、整份 context 重送一次）。
PACK_MAX = 40 * 1024


def _units(lines):
    """§10 一個子電路的行 -> [零件的行]，以 `- **` 開頭切。"""
    out = []
    for ln in lines:
        if ln.startswith(u"- **") or not out:
            out.append([])
        out[-1].append(ln)
    return out


def _chunk(head, items, cap):
    """items = [(小節標題或 None, [行])]，依序裝箱，每箱不超過 `cap` 位元組
    （單一項目本身超過就獨佔一箱）。換箱時重複目前的小節標題。"""
    size = lambda ls: sum(len(x.encode("utf-8")) + 1 for x in ls)
    boxes, cur, cur_title = [], list(head), None
    for title, ls in items:
        add = ([u"", title, u""] if title and title != cur_title else []) + ls
        if len(cur) > len(head) and size(cur) + size(add) > cap:
            boxes.append(cur)
            cur = list(head) + ([u"", title, u""] if title else [])
            add = ls
        cur.extend(add)
        cur_title = title or cur_title
    boxes.append(cur)
    return boxes


def pack(out, header, cap=PACK_MAX):
    """-> [(檔名, 文字)]：骨架包、拓樸包、電源包，加上每個分塊一份（超過 `cap` 再切）。

    骨架包 = §1–§9——組成、子電路之間的連線、介面。拓樸包 = §11 的鏈族（每族
    只寫一組與倍率）與交會點——A 重建全板架構用。Facts 的開頭說明與 §15 講的是
    規格書與待查證，不是撰寫材料，不放進來。電源包 = §13。
    分塊包 = 碰到該分塊的 §11 鏈（每組都列）+ 各子電路的 §10 邊 + 碰到它的
    §12 多點網路 + 它的 §14 port——B 逐區驗證拓樸用。
    ⚠️ 主／備、×N 的每一份都照列，不合併：P/R 是這塊板的事實，要寫出來。"""
    L, m = out["lines"], out["marks"]
    skel = ([u"# %s — 總覽" % header, u"",
             u"> 全板總覽：組成、子電路之間的連線、介面、電源軌摘要、未貼件。"
             u"電源軌逐條見 `00_power.md`，各分塊的零件連線見同目錄其他檔。"
             u"連線一律沒有方向。", u""] + L[m[1]:m[10]])
    files = [("00_skeleton.md", u"\n".join(skel) + u"\n")]
    tp = [u"# %s — 拓樸骨架" % header, u"",
          u"> Facts §11 的鏈族：只接兩個對象的零件串成一條鏈，直到分岔點或連接器。"
          u"每族只寫第一組與倍率，逐組 refdes 在各分塊檔。沒有方向。", u""]
    for j, box in enumerate(_chunk(tp[:4], [(None, [x]) for x in out["topo"]], cap)):
        files.append(((u"00_topology.md" if j == 0 else u"00_topology_%d.md" % (j + 1)),
                      u"\n".join(box) + u"\n"))
    pw = [u"# %s — 電源軌" % header, u""] + L[m[13]:m[14]]
    for j, box in enumerate(_chunk(pw[:2], [(None, [x]) for x in pw[2:]], cap)):
        files.append(((u"00_power.md" if j == 0 else u"00_power_%d.md" % (j + 1)),
                      u"\n".join(box) + u"\n"))
    grp = pack_groups(out["path"], out["n_parts"])
    diffs = pair_diffs(out, grp)
    # 子電路路徑字串 -> 分塊；用零件反查，因為 §10 標題就是 where(rd)。
    blk_grp = {}
    part_grp = {}
    for rd, p in out["path"].items():
        blk_grp[out["where"](rd)] = grp(p)
        part_grp[rd] = grp(p)
    order = collections.OrderedDict()
    for blk in out["blocks"]:
        order.setdefault(blk_grp.get(blk, ROOT_LABEL), []).append(blk)
    for i, (g, blks) in enumerate(order.items(), 1):
        if g in diffs:
            ref, lines = diffs[g]
            head = [u"# %s — 分塊：%s（與 %s 的差異）" % (header, g, ref), u"",
                    u"> 腳本已把本塊每一顆零件對應到 `%s` 的某一顆（依料號、footprint、"
                    u"鄰居結構，不看 refdes），逐腳比對接法。**這裡只列不同處**："
                    u"未列出的零件與接法都和 `%s` 相同，讀那份材料再用下面的位號"
                    u"對應換成本塊的 refdes。" % (ref, ref), u""]
            items = [(None, [x]) for x in lines]
            pts = [line for b, line in out["ports"]
                   if b == g or b.startswith(g + u" / ")]
            items += [(u"## 階層 port", [x]) for x in pts]
            boxes = _chunk(head, items, cap)
            for j, box in enumerate(boxes):
                fn = (u"%02d_%s.md" % (i, _slug(g))) if len(boxes) == 1 else \
                    (u"%02d_%s_%d.md" % (i, _slug(g), j + 1))
                files.append((fn, u"\n".join(box) + u"\n"))
            continue
        head = [u"# %s — 分塊：%s" % (header, g), u"",
                u"> 碰到本塊的訊號鏈（每組都列）、本塊各子電路的主要零件接到誰、"
                u"碰到本塊的多點網路、本塊的階層 port。全板總覽見 `00_skeleton.md`、"
                u"`00_topology.md`。"]
        items = []
        for cid, inst in out.get("chains") or ():
            mine = [x for x in inst
                    if any(part_grp.get(r, ROOT_LABEL) == g for r in x[0])]
            if not mine:
                continue
            ls = [u"- **C%d**（全板 %d 組同構，本塊 %d 組）：%s"
                  % (cid, len(inst), len(mine), mine[0][1])]
            ls += [u"  - 同構：%s" % x[2] for x in mine[1:]]
            items.append((u"## 訊號鏈", ls))
        for b in blks:
            items += [(u"### %s" % b, u) for u in _units(out["blocks"][b])]
        mine = [line for rds, line in out["multi"]
                if any(part_grp.get(r, ROOT_LABEL) == g for r in rds)]
        items += [(u"## 多點網路", [x]) for x in mine]
        pts = [line for b, line in out["ports"]
               if b == g or b.startswith(g + u" / ")]
        items += [(u"## 階層 port", [x]) for x in pts]
        boxes = _chunk(head, items, cap)
        for j, box in enumerate(boxes):
            fn = (u"%02d_%s.md" % (i, _slug(g))) if len(boxes) == 1 else                 (u"%02d_%s_%d.md" % (i, _slug(g), j + 1))
            files.append((fn, u"\n".join(box) + u"\n"))
    return files


def pair_diffs(out, grp):
    """-> {R 分塊名: (P 分塊名, [差異行])}。沒有成對子電路或沒有結構時回空。"""
    t = out.get("tree")
    if t is None or not t.kids.get(()):
        return {}
    import ndd_pair
    nl, bom, pwr_rx = out["nl"], out["bom"], out["pwr_rx"]
    prs = ndd_pair.pairs(t, grp)
    if not prs:
        return {}
    mt = ndd_pair.Matcher(nl, bom, lambda n: _is_power(n, pwr_rx),
                          lambda n: Fabric.cls(n) == "GND")
    m = {}
    # 大的先配（DPU／DPU1 的 FPGA 先對上，UC 那側的外部鄰居才換得了名字）。
    prs.sort(key=lambda x: -len(t.sub[x[2]]))
    # 兩輪：第一輪各對自己配，第二輪帶著全部的對應再配一次——DPU 的隔直電容
    # 另一端在 SW，SW 那對要先配好，電容才分得出誰是誰。
    for _ in range(2):
        for _gp, _gr, a, b in prs:
            m.update(mt.match(t.sub[a], t.sub[b], anchor=m))
    res = {}
    for gp, gr, a, b in prs:
        res[gr] = (gp, ndd_pair.diff_lines(mt, out["hpn"], set(t.sub[a]),
                                           set(t.sub[b]), m, out["label"],
                                           out["where"]))
    return res


def write_pack(pj, key, out, label, echo=print):
    """寫出 `arch_pack/<板>/`：骨架包、分塊包與 `index.md`（各檔大小）。"""
    d = os.path.join(pj.dir, PACK_DIR, key)
    if os.path.isdir(d):
        for f in os.listdir(d):
            if f.endswith(".md"):
                os.remove(os.path.join(d, f))
    else:
        os.makedirs(d)
    files = pack(out, label)
    idx = [u"# %s — 撰寫材料" % label, u"",
           u"| 檔案 | KB |", u"|---|---|"]
    for fn, txt in files:
        with io.open(os.path.join(d, fn), "w", encoding="utf-8") as fh:
            fh.write(txt)
        idx.append(u"| `%s` | %.1f |" % (fn, len(txt.encode("utf-8")) / 1024.0))
    with io.open(os.path.join(d, "index.md"), "w", encoding="utf-8") as fh:
        fh.write(u"\n".join(idx) + u"\n")
    echo("寫出 %s（%d 份）" % (d, len(files)))
    return d


def write(pj, key, missing_pn=None, echo=print, with_pack=False):
    """產生並寫出 `<板>_Facts.md`；`with_pack` 時另寫 `arch_pack/<板>/` 撰寫材料
    （只有 `ndd.py arch` 要，寫完 Architecture 就刪）。回傳路徑。"""
    nl, bom = pj.load(key)
    hier = pj.hier(key)
    label = (pj.cfg["boards"][key].get("label") or key)
    cis = None
    try:
        import ndd  # 只為了共用 CIS 快照索引；失敗就降級成不查表
        cis = ndd._cis_index(pj)
    except Exception:
        pass
    out = {}
    txt = render(key, label, nl, bom, hier, pj.cfg, cis, missing_pn, out=out)
    p = os.path.join(pj.dir, "%s_Facts.md" % key)
    with io.open(p, "w", encoding="utf-8") as fh:
        fh.write(txt)
    echo("寫出 %s" % p)
    if with_pack:
        write_pack(pj, key, out, label, echo)
    return p
