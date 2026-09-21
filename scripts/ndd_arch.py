# -*- coding: utf-8 -*-
"""板卡導覽（`<板>_Architecture.md`）——`init` 當下就寫得出來的那一半。

**這份文件在結構上只能是 `[N]`/`[B]`/`[S]` 文件。** `init` 跑完的當下還沒有
任何 datasheet（`MISSING.md` 才剛產生），所以腳位功能、訊號方向、極性、
「這條路恆通」這些 `[D]` 級主張**沒有材料可寫**——想違規也違規不了。

它要回答的是「拿到一塊沒看過的板子，先知道什麼會最快上手」：
重複結構（倍率）、主要 IC、對外介面、電源、訊號分群、未貼件、還缺什麼。

⚠️ **這是第 0 版不是成品。** 之後每次分析把 §8 待查證的項目一條條消掉，
文件就長大一次。腳本永遠寫不出「為什麼這樣設計」，那要人接手。
"""

import collections
import io
import os
import re

import ndd_classify
from ndd_graph import Fabric

# 自動產生的網路名（PADS 改名、OrCAD 流水號）——對讀的人沒有意義，
# 分群時要排掉，否則會蓋掉真正的訊號家族。
_RX_AUTONET = re.compile(r"^[NX]\d{4,}(_|$)", re.I)
# 家族化：把結尾的序號剝掉。`TX_EN_P_0` -> `TX_EN_P`，`ANT_IN10` -> `ANT_IN`。
_RX_TAIL_NUM = re.compile(r"[_]?\d+$")

# 值得單獨列出來的分類（看得到料號就知道要幹嘛的那些）。
_MAJOR = ("ic", "rf", "power_module", "filter", "crystal", "sensor",
          "semiconductor_discrete", "isolator", "optoelectronic",
          "switch", "relay")


def _natkey(s):
    """自然排序。`TX1 < TX2 < … < TX16`——字典序會排成 `TX1, TX10, …, TX9`，
    範圍顯示就會變成「TX1 … TX9」卻說有 16 組，看起來像 bug。"""
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", s)]


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


def repeat_groups(hier, nl):
    """頂層區塊的重複結構。**這是整份文件最有價值的一段。**

    同一塊板上「9 個長得一模一樣的區塊」幾乎一定就是系統架構本身
    （9 個 slot、9 路通道）。簽章用區塊內 footprint 的多重集合——
    refdes 會變、footprint 組成不會。
    """
    top = {}
    for rd, path in hier.part_paths().items():
        if path:
            top.setdefault(path[0], []).append(rd)
    sig = {}
    for blk, rds in top.items():
        c = collections.Counter(nl.parts.get(r, "?") for r in rds)
        sig[blk] = tuple(sorted(c.items()))
    groups = {}
    for blk, s in sig.items():
        groups.setdefault(s, []).append(blk)
    out = []
    for s, blks in groups.items():
        # `s` 是**單一區塊**的 footprint 多重集合，所以這個 sum 就是每組的
        # 零件數——不要再除以組數。
        out.append((sorted(blks, key=_natkey), sum(n for _, n in s)))
    out.sort(key=lambda x: (-len(x[0]), -x[1]))
    return out


def net_families(nl, pwr_rx, min_members=3, top=18):
    """訊號家族——`TX_EN_P_0..8` 收斂成一條 `TX_EN_P ×9`。

    不碰 datasheet 也能看出這塊板有哪幾組匯流排／控制線。
    """
    fam = collections.Counter()
    for n in nl.nets:
        if _is_power(n, pwr_rx) or _RX_AUTONET.match(n):
            continue
        base = _RX_TAIL_NUM.sub("", n)
        # 單字母 base（`H_1`、`P_3`）幾乎一定是鎖孔／基準點一類的東西，
        # 讓它排在最前面會把真正的匯流排蓋掉。
        if base and base != n and len(base.strip("_")) >= 3:
            fam[base] += 1
    return [(k, v) for k, v in fam.most_common(top) if v >= min_members]


def power_nets(nl, pwr_rx, top=14):
    rows = [(n, len(p)) for n, p in nl.nets.items() if _is_power(n, pwr_rx)]
    rows.sort(key=lambda x: -x[1])
    return rows[:top]


def major_parts(nl, bom, cfg, cis, pwr_rx, top=25):
    """依「接了幾條非電源訊號」排序的主要零件，**同料號收斂成一列**。

    §2 已經講完倍率了，這裡再把 `U103`／`U203`／…／`U903` 逐顆列一次只是
    把版面吃掉、把品項種類蓋掉。一個料號一列，附顆數與代表 refdes。
    """
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
    """連接器，**同料號＋同腳數收斂成一列**（理由同 `major_parts`）。"""
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


def dni_parts(nl, bom, cfg, cis, limit=20):
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
    return out[:limit], len(out), smt


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


def render(key, label, nl, bom, hier, cfg, cis, missing_pn=None):
    """產生一份板卡導覽的 markdown 文字。"""
    pwr_rx = re.compile(cfg.get("power_net_regex") or r"^(GND|VCC|VDD)", re.I)
    L = []
    w = L.append

    actives = nl.actives()
    w(u"# %s — 板卡導覽" % label)
    w(u"")
    w(u"> **這是 `init` 自動產生的第 0 版**，只含**不需要規格書就能斷言的事實**"
      u"（`[N]` netlist／`[B]` BOM／`[S]` 階層）。")
    w(u"> 腳位功能、訊號方向、極性、某條路通不通——那些要 `[D]` 規格書，"
      u"**這份文件不會有**，見 §8。")
    w(u"> 逐腳查詢請用 `ndd.py --board %s pins/net/part`，不要靠本文。" % key)
    w(u"")

    # --- 1 規模 ---------------------------------------------------------
    w(u"## 1. 規模")
    w(u"")
    w(u"| | |")
    w(u"|---|---|")
    w(u"| 零件 | %d 顆（其中 active %d 顆） |" % (len(nl.parts), len(actives)))
    w(u"| 網路 | %d 條 |" % len(nl.nets))
    conns, n_conn = connectors(nl, bom)
    w(u"| 連接器 | %d 顆（%d 種）|" % (n_conn, len(conns)))
    w(u"")

    # --- 2 重複結構 -----------------------------------------------------
    w(u"## 2. 重複結構（先看這段）")
    w(u"")
    if hier is None:
        w(u"（沒有階層資料——`.DSN` 未轉檔，這段無法產生）")
    else:
        groups = [g for g in repeat_groups(hier, nl) if len(g[0]) >= 2]
        if not groups:
            w(u"沒有偵測到重複的頂層區塊 `[S]`。")
        else:
            w(u"**倍率就是系統架構的直接反映**——先找出倍率，再解釋它。")
            w(u"")
            w(u"| 區塊 | 幾組 | 每組零件數 |")
            w(u"|---|---|---|")
            for blks, n in groups[:10]:
                w(u"| %s | **%d** | %d |" % (_rng(blks), len(blks), n))
            w(u"")
            w(u"⚠️ 以上是 `.DSN` 的階層 `[S]`，代表**設計者怎麼切分電路**，"
              u"不代表訊號怎麼走——連通一律以 `.asc` 為準。")
    w(u"")

    # --- 3 主要零件 -----------------------------------------------------
    w(u"## 3. 主要零件（依接了幾條非電源訊號排序）")
    w(u"")
    rows = major_parts(nl, bom, cfg, cis, pwr_rx)
    if not rows:
        w(u"（無）")
    else:
        w(u"**同料號收斂成一列**（§2 已經講完倍率了）。")
        w(u"")
        w(u"| 料號 `[B]` | 幾顆 | 分類 | 非電源訊號腳 `[N]` | 代表 refdes | 階層位置 `[S]` |")
        w(u"|---|---|---|---|---|---|")
        for rd, pn, cat, src, n, cnt in rows:
            mark = u"" if src == ndd_classify.SRC_CIS else u" `[?]`"
            blk = hier.block_of(rd) if hier else ""
            w(u"| %s | %d | %s%s | %d | `%s` | %s |"
              % (pn or u"**未貼件**", cnt, cat, mark, n, rd, blk or u"頂層"))
        w(u"")
        w(u"⚠️ 分類標 `[?]` 的是從 footprint／refdes 前綴**推**出來的，不是查表得到的事實。")
        w(u"「非電源訊號腳」取同料號中的最大值，是**只用 netlist 就算得出來的**"
          u"重要性指標——數字大代表訊號很可能還會繼續走。")
    w(u"")

    # --- 4 對外介面 -----------------------------------------------------
    w(u"## 4. 對外介面")
    w(u"")
    mate_of = dict(_mates_of(cfg, key))
    if not conns:
        w(u"（無連接器）")
    else:
        w(u"| 料號 `[B]` | 幾顆 | 腳數 `[N]` | refdes | 已定案的對接 |")
        w(u"|---|---|---|---|---|")
        for rds, pn, npin in conns:
            rng = _rng(rds)
            mt = "、".join("`%s`→%s" % (r, mate_of[r])
                           for r in rds if r in mate_of) or u"—"
            w(u"| %s | %d | %d | %s | %s |"
              % (pn or u"—", len(rds), npin, rng, mt))
        w(u"")
        w(u"「已定案的對接」空白**不代表沒對接**——只代表 `ndd.json` 的 `mates` "
          u"還沒寫進去。跑 `ndd.py mate` 看排名與證據。")
    w(u"")

    # --- 5 電源與地 -----------------------------------------------------
    w(u"## 5. 電源與地 `[N]`")
    w(u"")
    pw = power_nets(nl, pwr_rx)
    if not pw:
        w(u"⚠️ **一條都沒認出來**——`power_net_regex` 可能還沒設。"
          u"追跡會因此穿過每顆 2-pin 被動件走遍全板。")
    else:
        w(u"| 網路 | 腳數 |")
        w(u"|---|---|")
        for n, c in pw:
            w(u"| `%s` | %d |" % (n, c))
        w(u"")
        w(u"⚠️ 這是**目前判得出來的**。軌的命名各專案自己的，漏設一條的症狀是"
          u"追跡落點爆量；多收一條訊號的症狀是**路徑無聲消失**。")
    w(u"")

    # --- 6 訊號家族 -----------------------------------------------------
    w(u"## 6. 訊號家族 `[N]`")
    w(u"")
    fams = net_families(nl, pwr_rx)
    if not fams:
        w(u"（沒有偵測到帶序號的訊號家族）")
    else:
        w(u"序號結尾的網路收斂成一條看，這通常就是這塊板的匯流排與控制線：")
        w(u"")
        w(u"| 家族 | 條數 |")
        w(u"|---|---|")
        for k, v in fams:
            w(u"| `%s_*` | %d |" % (k, v))
    w(u"")

    # --- 7 未貼件 -------------------------------------------------------
    w(u"## 7. 未貼件 `[B]`")
    w(u"")
    shown, total, smt = dni_parts(nl, bom, cfg, cis)
    if smt:
        w(u"⚠️ 這份 BOM 是 `smt_only`，**依定義不列連接器／測試點／鎖孔／手插件**，"
          u"那幾類的缺席**不可判定**，已排除。以下只含一般 SMT 件。")
        w(u"")
    if not total:
        w(u"（無）")
    else:
        w(u"netlist 有、BOM 的 `Part Reference` 欄沒有 = **未貼件 (DNI)**，共 %d 顆："
          % total)
        w(u"")
        for rd, fp in shown:
            w(u"- `%s`（%s）" % (rd, fp))
        if total > len(shown):
            w(u"- …另有 %d 顆" % (total - len(shown)))
    w(u"")

    # --- 8 待查證 -------------------------------------------------------
    w(u"## 8. 這份文件還不知道什麼")
    w(u"")
    w(u"**以下每一項都需要規格書或人工判斷，`init` 產不出來。**"
      u"之後每次分析消掉一條，這一節就縮短一次。")
    w(u"")
    w(u"- [ ] **任何腳位的功能、方向、極性** —— 全部要 `[D]`，本文一條都沒有")
    w(u"- [ ] **訊號實際怎麼走** —— 本文只講有什麼，沒講接到哪。"
      u"用 `ndd.py trace --board %s --from <起點>` 查" % key)
    w(u"- [ ] **為什麼這樣設計** —— 腳本永遠寫不出來，要人接手")
    if missing_pn:
        w(u"- [ ] **缺 %d 份規格書** —— 見 `datasheets/MISSING.md`" % missing_pn)
    w(u"- [ ] **§2 的重複結構代表什麼** —— 階層只告訴你「設計者這樣切」，"
      u"沒告訴你那是 9 個 slot 還是 9 路備援")
    w(u"")
    return u"\n".join(L) + u"\n"


def write(pj, key, missing_pn=None, echo=print):
    """產生並寫出 `<板>_Architecture.md`。回傳路徑。"""
    nl, bom = pj.load(key)
    hier = pj.hier(key)
    label = (pj.cfg["boards"][key].get("label") or key)
    cis = None
    try:
        import ndd  # 只為了共用 CIS 快照索引；失敗就降級成不查表
        cis = ndd._cis_index(pj)
    except Exception:
        pass
    txt = render(key, label, nl, bom, hier, pj.cfg, cis, missing_pn)
    p = os.path.join(pj.dir, "%s_Architecture.md" % key)
    with io.open(p, "w", encoding="utf-8") as fh:
        fh.write(txt)
    echo("寫出 %s" % p)
    return p
