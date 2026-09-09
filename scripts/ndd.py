#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ndd —— netlist deep dive。PADS netlist + BOM 的深度電路分析工具（跨專案通用）。

    python ndd.py init      <分析資料夾>      # 掃描 .asc/.xlsx，產生 ndd.json 骨架
    python ndd.py pins      U939 A904         # 逐腳列出 net + 料號（自動判斷在哪塊板）
    python ndd.py net       "TX_LOAD_PS_*"    # 網路上有誰（glob 或 /regex/）
    python ndd.py part      SN74CBT           # 依 refdes / footprint / 料號搜尋
    python ndd.py export                      # 產出 pinmap_<board>.csv
    python ndd.py audit                       # 一致性稽核（含 parser 自我驗證）
    python ndd.py mate                        # 連接器對接：枚舉所有對應方式並排名
    python ndd.py trace                       # 端到端訊號鏈 CSV
    python ndd.py trace --signal TX_CLK       # 只追一條，印在終端機
    python ndd.py pinfn LMX2594 8             # 查某腳功能（抽 datasheet 原文並快取）
    python ndd.py datasheets                  # 盤點/下載 datasheet，產生 MISSING.md
    python ndd.py review                      # 產生人工複驗清單 REVIEW.md
    python ndd.py models                      # 列出已查證的元件模型
    python ndd.py manifest                    # 輸入檔完整 SHA-256 + 工具版本
    python ndd.py coverage                    # per-MPN 三源覆蓋狀況

共用選項：--config <ndd.json>（預設沿目前目錄往上找）、--board <key>|all

⚠️ CJK 輸出在 cp950 終端機會亂碼，前面加 PYTHONIOENCODING=utf-8。
⚠️ Windows 上請用 C:/... 形式路徑；Git Bash 的 /c/... Python 讀不到。
"""
from __future__ import print_function

import argparse
import csv
import io
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ndd_audit import run_audit                                    # noqa: E402
from ndd_bom import Bom                                            # noqa: E402
from ndd_graph import Fabric                                       # noqa: E402
from ndd_bom import is_ambiguous                                   # noqa: E402
import ndd_package                                                  # noqa: E402
from ndd_models import (ModelError, describe, load_models,          # noqa: E402
                        missing_pins, transfer_for)
from ndd_pads import Netlist, refkey                               # noqa: E402
import ndd_confidence as C                                         # noqa: E402
import ndd_graph                                                   # noqa: E402
import ndd_pinfn                                                   # noqa: E402

CONFIG_NAME = "ndd.json"

# datasheet 直連樣板。⚠️ 只有這幾家實測可用；Microchip 擋 curl (403)、
# 代理商站 (Mouser/Digikey) 會回 bot-check HTML 而不是 PDF。
URL_TEMPLATES = [
    ("TI", "https://www.ti.com/lit/ds/symlink/{slug}.pdf"),
    ("NXP", "https://www.nxp.com/docs/en/data-sheet/{pn}.pdf"),
]


# --------------------------------------------------------------------- 設定 --
def find_config(start=None):
    d = os.path.abspath(start or os.getcwd())
    while True:
        p = os.path.join(d, CONFIG_NAME)
        if os.path.exists(p):
            return p
        nd = os.path.dirname(d)
        if nd == d:
            raise SystemExit(
                "找不到 %s。先跑 `python ndd.py init <分析資料夾>`。" % CONFIG_NAME)
        d = nd


class Project(object):
    def __init__(self, cfg_path):
        self.path = cfg_path
        self.dir = os.path.dirname(cfg_path)
        with io.open(cfg_path, encoding="utf-8") as fh:
            self.cfg = json.load(fh)
        self.models = load_models(self.dir)
        self._cache = {}

    def board_keys(self, which="all"):
        return list(self.cfg["boards"]) if which == "all" else [which]

    def load(self, key):
        if key not in self._cache:
            b = self.cfg["boards"][key]
            if "bom_kind" in b and "bom_scope" not in b:
                raise SystemExit(
                    "board '%s' 仍使用已改名的 `bom_kind`。請改為 `bom_scope`，"
                    "對應：SMT BOM -> smt_only、完整 BOM -> complete、"
                    "未標註 -> unknown。**不得預設 complete**——那會把未知範圍"
                    "的缺件誤報成真 DNI。" % key)
            nl = Netlist(os.path.join(self.dir, b["asc"]))
            bom = Bom(os.path.join(self.dir, b["bom"]), sheet=b.get("sheet"),
                      ref_col=b.get("ref_col"),
                      scope=b.get("bom_scope", "unknown"),
                      expand_ranges=bool(b.get("expand_ranges")))
            self._cache[key] = (nl, bom)
        return self._cache[key]

    def all_boards(self, which="all"):
        return {k: self.load(k) for k in self.board_keys(which)}

    def fabric(self):
        return Fabric(self.all_boards(),
                      [tuple(m) for m in self.cfg.get("mates", [])],
                      self.models, self.cfg.get("power_net_regex"),
                      self.cfg.get("net_normalize"),
                      mate_map=self.cfg.get("mate_map"),
                      endpoints=self.cfg.get("endpoints"),
                      part_package=self.cfg.get("part_package"))

    def label(self, key):
        return self.cfg["boards"][key].get("label", key)


def pn_of(bom, refdes):
    d = bom.of(refdes)
    if is_ambiguous(d):
        return "!! BOM 多列衝突（列 %s）" % ",".join(str(x) for x in d.rows)
    if d is None:
        return "(未在 BOM refdes 欄中 -> %s)" % bom.absent_label()
    pn, val = bom.pn(refdes) or "", bom.value(refdes) or ""
    return "%s | %s" % (pn, val) if val and val != pn else (pn or val)


# ------------------------------------------------------------------- 子命令 --
def cmd_init(args):
    d = os.path.abspath(args.dir)
    ascs = sorted(f for f in os.listdir(d) if f.lower().endswith(".asc"))
    xlsx = sorted(f for f in os.listdir(d)
                  if f.lower().endswith((".xlsx", ".xlsm")) and not f.startswith("~$"))
    if not ascs:
        raise SystemExit("%s 底下沒有 .asc" % d)
    # ⚠️ 不要用檔名猜 netlist 與 BOM 的配對——檔名常含共通 token（產品線代號、
    #    日期），猜錯了不會有任何跡象。改用 **refdes 交集**：BOM 的 refdes 應該
    #    幾乎全部出現在對應的 netlist 裡。
    boms = {}
    for x in xlsx:
        try:
            boms[x] = Bom(os.path.join(d, x))
        except Exception as exc:
            print("  (略過 %s：%s)" % (x, exc))

    boards = {}
    low_conf = []
    for a in ascs:
        nl = Netlist(os.path.join(d, a))
        key = re.sub(r"[^A-Za-z0-9]+", "_", os.path.splitext(a)[0]).strip("_").lower()[:12]
        scored = []
        for x, b in boms.items():
            if not b.ref:
                continue
            hit = sum(1 for r in b.ref if r in nl.parts)
            scored.append((hit / float(len(b.ref)), hit, len(b.ref), x))
        scored.sort(reverse=True)
        best, ratio = "", 0.0
        if scored:
            ratio, hit, tot, best = scored[0]
        second = scored[1][0] if len(scored) > 1 else 0.0
        conf = "OK" if ratio >= 0.9 and ratio - second >= 0.3 else "!! 需人工確認"
        if conf != "OK":
            low_conf.append(key)
        boards[key] = {"label": os.path.splitext(a)[0], "asc": a, "bom": best,
                       "bom_scope": "unknown", "sheet": None, "ref_col": None,
                       "expand_ranges": False}
        print("  %-14s parts %5d / signals %5d" % (key, len(nl.parts), len(nl.nets)))
        print("       -> BOM %-58s refdes 命中率 %.0f%% (次佳 %.0f%%)  %s"
              % (best or "(無)", ratio * 100, second * 100, conf))
    cfg = {
        "project": os.path.basename(os.path.dirname(d)) or "unnamed",
        "boards": boards,
        "mates": [],
        # ⚠️ 未出現在骨架裡的欄位，使用者不會知道它存在 —— 一律寫出空殼。
        "mate_map": {},
        "part_package": {},
        "endpoints": {},
        "power_net_regex": r"^(?!.*_(EN|PG)$)(GND|.*VDD.*|.*VCC.*|.*_\d+V\d+.*)$",
        "net_normalize": [],
        "trace": {"start": [], "slot_pattern": ""},
        "assertions": [],
        "role_rules": [],
        "datasheets": {"dir": "datasheets", "parts": []},
    }
    p = os.path.join(d, CONFIG_NAME)
    if os.path.exists(p) and not args.force:
        raise SystemExit("%s 已存在，要覆蓋請加 --force" % p)
    with io.open(p, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(cfg, indent=2, ensure_ascii=False))
    print("\n寫出 %s" % p)
    if low_conf:
        print("\n⚠️ 這幾塊板的 BOM 配對信心不足，**請人工確認 ndd.json 的 bom 欄**：%s"
              % ", ".join(low_conf))
    print("接著要人工補：")
    print("  boards[*].bom_scope  complete / smt_only / variant / unknown")
    print("                       **只有 complete 才能把缺件稱為 DNI**")
    print("  mates / mate_map     對接關係；mate_map 是已批准的腳位對映，")
    print("                       未批准時 trace 仍可跑，但每列會帶 mate:unapproved")
    print("  part_package         只在 datasheet 多封裝欄且會改變答案時才需要")
    print("  endpoints            refdes 或 MPN -> terminal / stateful / unknown_stop")
    print("  net_normalize / trace.start")


def cmd_pins(args, pj):
    for refdes in args.refdes:
        hits = [b for b in pj.board_keys(args.board) if refdes in pj.load(b)[0].parts]
        if not hits:
            print("!! %s 不在任何 netlist 中" % refdes)
            continue
        for b in hits:
            nl, bom = pj.load(b)
            fp = nl.parts[refdes]
            pn = bom.pn(refdes)
            pn = "" if is_ambiguous(pn) else (pn or "")
            pkg = ((pj.cfg.get("part_package") or {}).get("%s:%s" % (b, refdes))
                   or (pj.cfg.get("part_package") or {}).get(pn))
            model, edges, cav = transfer_for(pj.models, fp, pn,
                                             len(nl.pins(refdes)), pkg)
            print("== %s  [%s]" % (refdes, pj.label(b)))
            print("   footprint : %s" % fp)
            print("   BOM       : %s" % pn_of(bom, refdes))
            if edges:
                print("   transfer  : %s（%d 條有向邊）" % (model, len(edges)))
                for a_, b_, d_, _e in edges:
                    print("               %s %s %s"
                          % (a_, "->" if d_ == "forward" else "<->", b_))
            res = ndd_package.resolve(pj.models, nl, bom, b, refdes,
                                      ndd_graph.Fabric.cls, pkg)
            if res["status"]:
                print("   封裝判定  : %s" % ndd_package.describe(res))
            if cav:
                print("   caveats   : %s" % C.render(cav))
            pins = nl.pins(refdes)
            w = max([len(p) for p in pins] or [1])
            for p, net in pins.items():
                mark = "   <-- 單腳懸空" if len(nl.net(net)) == 1 else ""
                print("   pin %-*s  %s%s" % (w, p, net, mark))
            print()


def cmd_net(args, pj):
    for pattern in args.pattern:
        for b in pj.board_keys(args.board):
            nl, bom = pj.load(b)
            names = nl.find_net(pattern)
            if not names:
                continue
            print("== [%s] %r -> %d 條網路" % (pj.label(b), pattern, len(names)))
            for n in names[:args.limit]:
                conns = sorted(nl.net(n), key=lambda x: (refkey(x[0]), x[1]))
                print("   %s  (%d pins)" % (n, len(conns)))
                if args.verbose or len(names) == 1:
                    for rd, p in conns:
                        print("        %-9s pin %-4s %-30s %s"
                              % (rd, p, nl.parts.get(rd, "?"), pn_of(bom, rd)))
            if len(names) > args.limit:
                print("   ... 另有 %d 條未列出" % (len(names) - args.limit))
            print()


def cmd_part(args, pj):
    for pattern in args.pattern:
        for b in pj.board_keys(args.board):
            nl, bom = pj.load(b)
            hits = set(nl.find_part(pattern))
            up = pattern.upper()
            for rd in nl.parts:
                if up in ((bom.pn(rd) or "") + "|" + (bom.value(rd) or "")).upper():
                    hits.add(rd)
            if not hits:
                continue
            hits = sorted(hits, key=refkey)
            print("== [%s] %r -> %d 顆" % (pj.label(b), pattern, len(hits)))
            for rd in hits[:args.limit]:
                print("   %-9s %-34s %s" % (rd, nl.parts.get(rd, "?"), pn_of(bom, rd)))
            if len(hits) > args.limit:
                print("   ... 另有 %d 顆" % (len(hits) - args.limit))
            print()


def cmd_export(args, pj):
    out = os.path.join(pj.dir, args.outdir)
    if not os.path.isdir(out):
        os.makedirs(out)
    for b in pj.board_keys(args.board):
        nl, bom = pj.load(b)
        path = os.path.join(out, "pinmap_%s.csv" % b)
        with io.open(path, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["refdes", "pin", "net", "footprint", "part_number",
                        "value", "class", "stuffed", "bom_scope",
                        "net_pin_count"])
            for rd in sorted(nl.parts, key=refkey):
                d = bom.of(rd)
                if is_ambiguous(d):
                    stuffed = "AMBIGUOUS"
                elif d:
                    stuffed = "Y"
                else:
                    # ⚠️ 「BOM 沒有」不等於「板上沒有」——要看 BOM 範圍
                    stuffed = "N" if bom.scope_supports_dni() else "UNKNOWN"
                pn = bom.pn(rd)
                val = bom.value(rd)
                cls = ("passive" if nl.is_passive(rd)
                       else "mech" if nl.is_mech(rd) else "active")
                pins = nl.pins(rd) or {"": ""}
                for p, net in pins.items():
                    w.writerow([rd, p, net, nl.parts[rd],
                                "" if is_ambiguous(pn) else (pn or ""),
                                "" if is_ambiguous(val) else (val or ""),
                                cls, stuffed, bom.scope,
                                len(nl.net(net)) if net else ""])
        print("寫出 %s  (%d parts / %d signals)" % (path, len(nl.parts), len(nl.nets)))


def cmd_mate(args, pj):
    if not pj.cfg.get("mates"):
        raise SystemExit("ndd.json 的 mates 是空的。先填入 [板A, 連接器A, 板B, 連接器B]。")
    print("連接器對接：枚舉所有對應方式並排名")
    print("（矛盾腳數 = 兩側類別打架；語意相符 = net 名稱正規化後相同）\n")
    rep = pj.fabric().verify_mating()
    weak = [r for r in rep if not r["ok"] or r["margin"] <= 4]
    if weak:
        print("\n⚠️ 以下對接的證據偏弱，務必人工複驗（layout 或 continuity）：")
        for r in weak:
            print("   %s <-> %s  margin %d  %s"
                  % (r["a"], r["b"], r["margin"], r["kind"]))
    return rep


def cmd_audit(args, pj):
    return run_audit(pj.cfg, pj.all_boards(args.board), pj.models)


def cmd_trace(args, pj):
    tcfg = pj.cfg.get("trace") or {}
    starts = tcfg.get("start") or []
    if not starts:
        raise SystemExit("ndd.json 的 trace.start 是空的，例如 "
                         '[{"board":"ecu","conn":"J902","rail":"P"}]')
    fab = pj.fabric()
    unapproved = [k for k, v in fab.mate_status.items() if v != "approved"]
    if unapproved:
        print("!! 有 %d 組對接尚未批准腳位對映（ndd.json 的 mate_map）。" % len(unapproved))
        print("   trace 仍會跑，但這些路徑帶 mate:unapproved —— 它們是**候選路徑**，")
        print("   不是已確認的線束／板對板對接結論。")
        print("")
    slot_rx = re.compile(tcfg.get("slot_pattern") or "$^")
    rows = []
    for st in starts:
        b, conn = st["board"], st["conn"]
        nl = fab.nl[b]
        for pin, net in nl.pins(conn).items():
            if not net or fab.is_power(net):
                continue
            if args.signal and args.signal.upper() not in net.upper():
                continue
            base = dict(rail=st.get("rail", ""), signal=net,
                        start="%s.%s" % (conn, pin))
            # 第一段：走到中繼（slot）連接器
            mids = fab.trace((b, conn, pin),
                             stop_fn=lambda n: n[0] == b and slot_rx.match(n[1] or ""))
            if not mids:
                rows.append(dict(base, slot="", mid="", far_pin="",
                                 far_net="(未到達中繼連接器)", n_loads=0,
                                 loads="", stops="", hops="",
                                 endpoint_kind="", gating="",
                                 caveats="", confidence=C.UNKNOWN))
                continue
            for (mb, mrd, mpin), (path, _ep) in sorted(
                    mids.items(), key=lambda x: (x[0][1], x[0][2])):
                sm = slot_rx.match(mrd)
                slot = sm.group(1) if (sm and sm.groups()) else mrd
                # ⚠️ 這一跳**就是**跨板對接本身。必須走 fab.mate（它才帶
                #    已批准的對映與 mate caveat），不能只用 mate_partners 取
                #    對手板然後沿用同一個 pin number —— 那等於：
                #      (a) 忽略 mate_map 的批准對映（非直通時直接算錯）
                #      (b) mate:unapproved 永遠不會出現在輸出裡（標了等於沒標）
                crossings = fab.mate.get((mb, mrd, mpin), [])
                if not crossings:
                    # ⚠️ 舊版在這裡 `continue`，整條訊號從 CSV 靜默消失。
                    rows.append(dict(
                        base, slot=slot, mid="%s.%s" % (mrd, mpin), far_pin="",
                        far_net="(mate 未宣告或該腳不在對映中)",
                        n_loads=0, loads="", stops="",
                        hops=fab.hop_string(path), endpoint_kind="",
                        gating=fab.path_gating(path),
                        caveats=C.render(fab.path_caveats(path, ["mate:missing"])),
                        confidence=C.confidence_of(
                            fab.path_caveats(path, ["mate:missing"]))))
                    continue
                for (tb, trd, tpin), mcav in crossings:
                    ends = fab.trace((tb, trd, tpin), max_depth=6)
                    fnet = fab.nl[tb].pin_net(trd, tpin)
                    loads, stops, gat, kinds = [], [], [], set()
                    cav = set(mcav)          # 跨板那一跳的 caveat 要進來
                    for (eb, erd, ep_), (epath, ep) in sorted(ends.items()):
                        kind, reason, ecav = ep
                        tag = "%s.%s" % (erd, ep_)
                        if kind in ndd_graph.EP_IN_LOADS:
                            loads.append(tag)
                        else:
                            stops.append("%s(%s)" % (tag, reason or kind))
                        kinds.add(kind)
                        cav |= fab.path_caveats(epath, ecav)
                        gat.append(fab.path_gating(epath))
                    cav |= fab.path_caveats(path)
                    gat.append(fab.path_gating(path))
                    rows.append(dict(
                        base, slot=slot, mid="%s.%s" % (mrd, mpin),
                        far_pin="%s.%s" % (trd, tpin), far_net=fnet or "",
                        n_loads=len(loads), loads=" ".join(sorted(loads)),
                        stops=" ".join(sorted(stops)),
                        endpoint_kind=";".join(sorted(kinds)),
                        gating=C.worst_gating(gat),
                        caveats=C.render(cav), confidence=C.confidence_of(cav),
                        hops=fab.hop_string(path)))

    if args.signal:
        shown = 0
        for r in rows:
            if r["slot"] not in ("", "1", "101") and shown:
                continue
            print("")
            print("%-22s rail %s  slot %s" % (r["signal"], r["rail"], r["slot"]))
            print("   起點  : %s" % r["start"])
            print("   hops  : %s" % r["hops"])
            print("   -> %s = %s -> %s" % (r["mid"], r["far_pin"], r["far_net"]))
            print("   負載  : %d 個  %s" % (r["n_loads"], r["loads"][:120]))
            if r["stops"]:
                print("   停點  : %s" % r["stops"][:120])
            print("   端點  : %s | gating %s | confidence %s"
                  % (r["endpoint_kind"], r["gating"], r["confidence"]))
            if r["caveats"]:
                print("   caveat: %s" % r["caveats"])
            shown += 1
        return rows

    out = os.path.join(pj.dir, args.outdir)
    if not os.path.isdir(out):
        os.makedirs(out)
    path = os.path.join(out, "signal_chain.csv")
    cols = ["rail", "signal", "slot", "start", "hops", "mid", "far_pin",
            "far_net", "n_loads", "loads", "stops", "endpoint_kind",
            "gating", "caveats", "confidence"]
    with io.open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})
    print("寫出 %s  (%d 列)" % (path, len(rows)))
    print("  走到終端腳: %d 列 / 未到達: %d 列"
          % (sum(1 for r in rows if r["n_loads"]),
             sum(1 for r in rows if not r["n_loads"])))
    for lvl in (C.CONFIRMED, C.CAVEATED, C.UNKNOWN):
        n = sum(1 for r in rows if r["confidence"] == lvl)
        if n:
            print("  confidence %-10s %d 列" % (lvl, n))

    # ---- hint graph：另一份輸出，**不可被 BFS 使用** ----
    hints = fab.collect_hints()
    hp = os.path.join(out, "topology_hint.csv")
    hcols = ["board", "refdes", "pin", "net", "mpn", "relation", "effect", "source"]
    with io.open(hp, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=hcols)
        w.writeheader()
        for r in hints:
            w.writerow({c: r.get(c, "") for c in hcols})
    print("寫出 %s  (%d 列；功能說明，**不是連通**)" % (hp, len(hints)))
    return rows


# --------------------------------------------------------------- datasheets --
def _slug(pn):
    s = re.split(r"[,/ ]", pn)[0]
    s = re.sub(r"(RGT|RGR|DGVR|PWR|QPWRQ1|IRUGT|NA|BS|T-E|TR\d*|-Q1|-TR\d*)$", "", s)
    return s.lower()


def _looks_like_pdf(path):
    try:
        with open(path, "rb") as fh:
            return fh.read(5) == b"%PDF-" and os.path.getsize(path) > 20000
    except OSError:
        return False


def _fetch(url, dest):
    """⚠️ HTTP 200 不代表拿到 PDF——擋機器人的站會回一頁 HTML。一定要驗魔術位元。"""
    try:
        subprocess.check_call(
            ["curl", "-sSL", "--max-time", "40", "-o", dest, url],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except Exception:
        return False
    if _looks_like_pdf(dest):
        return True
    if os.path.exists(dest):
        os.remove(dest)
    return False


def cmd_datasheets(args, pj):
    dcfg = pj.cfg.get("datasheets") or {}
    ddir = os.path.join(pj.dir, dcfg.get("dir", "datasheets"))
    if not os.path.isdir(ddir):
        os.makedirs(ddir)
    have = {f.lower(): f for f in os.listdir(ddir)}

    if args.url:
        if not args.pn:
            raise SystemExit("--url 要搭配 --pn")
        dest = os.path.join(ddir, "%s.pdf" % _slug(args.pn))
        ok = _fetch(args.url, dest)
        print("%s  %s -> %s" % ("OK  " if ok else "失敗", args.pn, dest))
        if not ok:
            print("   （200 也可能是 bot-check HTML，本工具會驗 %PDF 魔術位元後才留檔）")
        return

    parts = dcfg.get("parts")
    if not parts:                       # 沒指定就從 BOM 自動盤點主動元件
        parts = []
        for k in pj.board_keys(args.board):
            nl, bom = pj.load(k)
            for rd in nl.actives():
                pn = bom.pn(rd)
                if pn and pn not in parts:
                    parts.append(pn)
    print("需要的料號 %d 筆，datasheet 目錄：%s\n" % (len(parts), ddir))

    missing = []
    for pn in sorted(parts):
        slug = _slug(pn)
        hit = next((v for k, v in have.items()
                    if slug and slug in k.replace("-", "").replace("_", "")), None)
        if hit:
            print("  已有   %-34s %s" % (pn, hit))
            continue
        got = None
        if not args.no_download:
            base = pn.split(",")[0]
            tried = []
            for vendor, tmpl in URL_TEMPLATES:
                for token in dict.fromkeys([slug, slug.upper(), base, base.upper()]):
                    url = tmpl.format(slug=token.lower(), pn=token)
                    if url in tried:
                        continue
                    tried.append(url)
                    dest = os.path.join(ddir, "%s.pdf" % slug)
                    if _fetch(url, dest):
                        got = "%s  %s" % (vendor, url)
                        break
                if got:
                    break
        if got:
            print("  下載   %-34s %s" % (pn, got))
        else:
            print("  缺     %-34s" % pn)
            missing.append(pn)

    mp = os.path.join(ddir, "MISSING.md")
    with io.open(mp, "w", encoding="utf-8") as fh:
        fh.write("# 缺少的 datasheet\n\n")
        fh.write("自動下載只對少數原廠站有效（實測：TI、NXP 可；"
                 "Microchip 回 403；代理商站回 bot-check HTML 而非 PDF）。\n\n")
        fh.write("**以下請自行下載後放進 `%s/`**，"
                 "檔名建議用料號小寫：\n\n" % dcfg.get("dir", "datasheets"))
        for pn in missing:
            fh.write("- [ ] `%s`\n" % pn)
        fh.write("\n> 補齊之後，凡是要寫進 `models.json` 的腳位模型，"
                 "都必須翻過對應的 datasheet 並填上 `verified_against`"
                 "（檔名 + 頁碼 + 文件編號）。\n")
    print("\n缺 %d 筆，已寫出 %s" % (len(missing), mp))


# ---------------------------------------------------------------- manifest --
def cmd_manifest(args, pj):
    """輸入檔的完整 SHA-256 + 工具版本。

    ⚠️ `ndd.json` 也要入帳 —— 它含 mates / mate_map / net_normalize /
       power_net_regex / assertions / part_package / endpoints，每一項都直接
       改變結論。工具版本同理：工具邏輯本身會改變結論。
    """
    import datetime
    items = []

    def add(kind, path):
        if os.path.exists(path):
            items.append((kind, os.path.relpath(path, pj.dir),
                          ndd_pinfn.sha256(path)))

    for k in pj.board_keys("all"):
        b = pj.cfg["boards"][k]
        add("netlist", os.path.join(pj.dir, b["asc"]))
        add("bom", os.path.join(pj.dir, b["bom"]))
    add("config", pj.path)
    add("models", os.path.join(pj.dir, "models.json"))
    ddir = os.path.join(pj.dir,
                        (pj.cfg.get("datasheets") or {}).get("dir", "datasheets"))
    if os.path.isdir(ddir):
        for f in sorted(os.listdir(ddir)):
            if f.lower().endswith(".pdf"):
                add("datasheet", os.path.join(ddir, f))

    try:
        rev = subprocess.check_output(
            ["git", "-C", os.path.dirname(os.path.abspath(__file__)),
             "rev-parse", "--short", "HEAD"],
            stderr=subprocess.PIPE).decode().strip()
    except Exception:
        rev = "(not a git work tree)"

    lines = ["# 輸入 manifest", "",
             "產生時間：%s" % datetime.date.today().isoformat(),
             "工具版本：%s" % rev, ""]
    for k in pj.board_keys("all"):
        b = pj.cfg["boards"][k]
        lines.append("- board `%s`：bom_scope=%s, sheet=%s"
                     % (k, b.get("bom_scope", "unknown"),
                        b.get("sheet") or "(第一個)"))
    lines += ["", "| 種類 | 檔案 | SHA-256 |", "|---|---|---|"]
    for kind, rel, sha in items:
        lines.append("| %s | `%s` | `%s` |" % (kind, rel, sha))
    txt = "\n".join(lines) + "\n"
    # ⚠️ manifest 產物本身不入帳，避免 hash 自我指涉
    out = os.path.join(pj.dir, "MANIFEST.md")
    with io.open(out, "w", encoding="utf-8") as fh:
        fh.write(txt)
    print(txt)
    print("寫出 %s（共 %d 個輸入檔；本檔自身不入帳）" % (out, len(items)))
    return items


# ---------------------------------------------------------------- coverage --
def _ds_present(pn, have):
    key = re.sub(r"[^a-z0-9]", "", (pn or "").split(",")[0].lower())[:6]
    return bool(key) and any(key in h for h in have)


def cmd_coverage(args, pj):
    """per-MPN 的三源覆蓋狀況。**不做無人維護的 per-pin 矩陣。**"""
    _cp, cache, _mig = ndd_pinfn.load_cache(pj.dir)
    locked = {}
    for r in cache:
        if r.get("resolved_by") in ndd_pinfn.RESOLVED_OK:
            k = r["part"].upper()
            locked[k] = locked.get(k, 0) + 1
    ddir = os.path.join(pj.dir,
                        (pj.cfg.get("datasheets") or {}).get("dir", "datasheets"))
    have = [re.sub(r"[^a-z0-9]", "", f.lower())
            for f in os.listdir(ddir)] if os.path.isdir(ddir) else []
    eps = pj.cfg.get("endpoints") or {}
    part_pkg = pj.cfg.get("part_package") or {}

    agg = {}
    for k in pj.board_keys(args.board):
        nl, bom = pj.load(k)
        for rd in nl.actives():
            pn = bom.pn(rd)
            amb = is_ambiguous(pn)
            pn = "" if amb else (pn or "")
            key = pn or "(無 MPN)"
            e = agg.setdefault(key, {"n": 0, "bom": "無", "scope": set(),
                                     "state": set(), "todo": set()})
            e["n"] += 1
            e["scope"].add(bom.scope)   # 同一料號可能跨多塊板，scope 不同
            if amb:
                e["bom"] = "ambiguity"
                e["todo"].add("BOM 衝突")
            elif bom.of(rd) is not None:
                e["bom"] = "有"
            else:
                e["todo"].add("BOM 缺(scope=%s)" % bom.scope)
            pkg = part_pkg.get("%s:%s" % (k, rd)) or part_pkg.get(pn)
            res = ndd_package.resolve(pj.models, nl, bom, k, rd,
                                      ndd_graph.Fabric.cls, pkg)
            m, cav = res["model"], res["caveats"]
            declared = eps.get("%s:%s" % (k, rd)) or eps.get(pn)
            if m is not None and res["status"] == ndd_package.INFERRED:
                e["state"].add("modelled[?]")
                e["todo"].add("複核推論封裝")
            elif m is not None and not cav:
                e["state"].add("modelled")
            elif "package:unresolved" in cav:
                e["state"].add("package 未定")
                e["todo"].add("package")
            elif cav:
                e["state"].add("model_unusable")
                e["todo"].add("model")
            elif declared:
                e["state"].add(declared)
            else:
                # ⚠️ 未宣告的穿越件會落在這裡。這是**預設值，不是結論**。
                e["state"].add("unclassified")
                e["todo"].add("endpoint 分類")
            if not _ds_present(pn, have):
                e["todo"].add("datasheet")

    hdr = ("%-28s %4s %-10s %-9s %-4s %-6s %-22s %s"
           % ("MPN", "顆", "BOM", "scope", "DS", "pinfn", "狀態", "待處理"))
    print(hdr)
    print("-" * 120)
    for pn in sorted(agg):
        e = agg[pn]
        print("%-28s %4d %-10s %-9s %-4s %-6d %-22s %s"
              % (pn[:28], e["n"], e["bom"], ",".join(sorted(e["scope"])),
                 "有" if _ds_present(pn, have) else "無",
                 locked.get(pn.upper(), 0),
                 ",".join(sorted(e["state"]))[:22],
                 ", ".join(sorted(e["todo"])) or "-"))
    print("")
    print("注：pinfn 欄只計 **package-locked** 的快取列；未鎖定的不算覆蓋")
    print("    （否則等於把未解決的歧義洗成綠格）。")
    print("    `unclassified` 是預設值，不是結論 —— 未宣告的穿越件會落在這裡。")
    return agg


# ------------------------------------------------------------------ review --
REVIEW_TMPL = u"""# 人工複驗清單

> 由 `ndd.py review` 產生。**這份清單上的每一項，工具都不能替你確認。**
> 專案：{project}

## 0. 三源覆蓋

{coverage}

## A. 工具驗得到、且已通過的（不需複驗，列出供追溯）

- parser 自我驗證：{parser}
- 文件斷言：{apass} 條通過{afail}
- 元件模型：{model}
- 命名規則展開：{role}

## B. 必須人工複驗的

### B1. 元件 transfer 模型（最高優先）
追跡結果完全建立在這些模型上；模型錯 → 訊號鏈看起來合理但整張表是錯的。

```
{models}
```

- [ ] 每條 transfer 邊的 `direction` 都真的翻過 datasheet？**單向元件不得雙向走。**
- [ ] 每條邊的 `verified_against` 都真的翻過那一頁？
- [ ] `pin_roles` 的 VSS/VDD 腳號是否真的翻過 datasheet？（封裝判定全靠它）
- [ ] 有沒有用到**相近型號**的腳位當成同一顆？

### B2. 連接器對接
{mates}

- [ ] margin ≤ 4 的項目，是否已用 layout 或 continuity 確認？
- [ ] 兩側同型（都是公頭）的對接，是否已取得線束圖？
- [ ] **帶 `mate:unapproved` 的路徑是候選路徑，不是結論。** 是否已填 `mate_map`？

### B3. netlist 本身答不出來的
- [ ] **netlist ≠ 實體板**：rework／飛線／換料都不在 `.asc` 裡。
- [ ] **layout 決定的量**（阻抗、插入損耗、耦合、串音）不能跨版本沿用。
- [ ] **BOM 範圍**：{scope}
- [ ] **線束**：板間同軸／排線的對應關係不在任何 netlist 裡。
- [ ] **BOM 缺件**：{absent}
- [ ] **懸空（單腳）網路**：{floating}

### B4. 封裝判定
**`[?]` 標記的封裝是由 netlist/BOM 推論出來的，不是查證過的事實。**

{inferred}

- [ ] 上列每一項是否已人工複核？確認後填入 `ndd.json` 的 `part_package`。

待補（證據不足，工具拒絕推論）：

{pending}

### B5. 未分類端點
`unclassified` 是預設值，不是結論。未宣告的穿越件會被當成負載列出。

- [ ] 跑 `ndd.py coverage`，把 `unclassified` 逐一歸類到 `endpoints`。

### B6. 文件裡的因果推論
斷言只驗得到「數值與連線」。凡是「為什麼這樣設計」之類的推論，**工具一律驗不到**。

- [ ] 文件中每一句因果推論，都有標記為推論（⚠️）或附上佐證？
"""


def cmd_review(args, pj):
    stats = run_audit(pj.cfg, pj.all_boards("all"), pj.models)
    print("")
    print("=" * 78)
    rep = pj.fabric().verify_mating(verbose=False) if pj.cfg.get("mates") else []
    mates = "\n".join(
        "- `%s <-> %s`：最佳 **%s**（矛盾 %d、語意 %d），次佳 %s（%d）；"
        "margin **%d**；批准狀態 **%s**。%s"
        % (r["a"], r["b"], r["best"], r["best_bad"], r["best_match"],
           r["second"], r["second_match"], r["margin"], r["status"], r["kind"])
        for r in rep) or "- （尚未設定 mates）"
    txt = REVIEW_TMPL.format(
        project=pj.cfg.get("project", ""),
        coverage="跑 `ndd.py coverage` 取得 per-MPN 覆蓋表。",
        parser=("全部通過" if not stats["parser_fail"]
                else "**失敗**: %s" % stats["parser_fail"]),
        apass=stats["assert_pass"],
        afail=("" if not stats["assert_fail"] else "，**失敗 %d 條：%s**"
               % (len(stats["assert_fail"]), "；".join(stats["assert_fail"][:5]))),
        model=("全部通過" if not stats["model_fail"] else "**失敗 %d 項：%s**"
               % (len(stats["model_fail"]), "；".join(stats["model_fail"][:3]))),
        role=("0 筆未解釋的不符" if stats["role_bad"] == 0
              else "**%d 筆未解釋的不符**" % stats["role_bad"]),
        models=describe(pj.models), mates=mates,
        scope="; ".join("%s: %s" % (k, pj.load(k)[1].scope)
                        for k in pj.board_keys("all")),
        absent="; ".join("%s: %s" % (k, ", ".join(v) or "無")
                         for k, v in stats["absent"].items()),
        floating="; ".join("%s: %d 條" % (k, len(v))
                           for k, v in stats["floating"].items()),
        pending=("\n".join("- [ ] %s" % x for x in stats["model_pending"])
                 or "- （無）"),
        inferred=("\n".join("- [ ] %s" % x
                            for x in stats.get("model_inferred", []))
                  or "- （無推論項目）"))
    p = os.path.join(pj.dir, "REVIEW.md")
    with io.open(p, "w", encoding="utf-8") as fh:
        fh.write(txt)
    print("寫出 %s" % p)


def cmd_pinfn(args, pj):
    dcfg = pj.cfg.get("datasheets") or {}
    ddir = os.path.join(pj.dir, dcfg.get("dir", "datasheets"))
    if args.list:
        _p, rows, mig = ndd_pinfn.load_cache(pj.dir)
        print("原文快取（%d 筆，其中 %d 筆為待重解析的舊 schema）" % (len(rows), mig))
        for r in rows:
            print("  %-18s pin %-4s %-12s %-8s %s p.%s  [%s/%s]"
                  % (r["part"], r["pin"], r["pin_name"], r["direction"],
                     r["source_file"], r["page"], r.get("package") or "-",
                     r.get("resolved_by") or "-"))
        return
    part, observed = args.part, None
    if args.refdes and args.pin is None and args.part is not None:
        args.pin, part = args.part, None      # `pinfn --refdes U1 15` 的 15 是腳位
    if args.refdes:
        if not args.board or args.board == "all":
            raise SystemExit("--refdes 要搭配 --board <key>")
        nl, bom = pj.load(args.board)
        if args.refdes not in nl.parts:
            raise SystemExit("%s 不在 %s 的 netlist 中" % (args.refdes, args.board))
        pn = bom.pn(args.refdes)
        if is_ambiguous(pn):
            raise SystemExit("%s 在 BOM 有多列衝突（列 %s），先解決 BOM"
                             % (args.refdes, ",".join(str(x) for x in pn.rows)))
        if not pn:
            raise SystemExit("%s 不在 BOM 中，無法取得 MPN" % args.refdes)
        part = pn
        observed = list(nl.pins(args.refdes).keys())
        print("鎖定三源：[N] %s.%s（%d 支已接腳）  [B] %s  [D] 待查"
              % (args.board, args.refdes, len(observed), pn))
    if not part or args.pin is None:
        raise SystemExit("用法：ndd.py pinfn <料號> <腳位>  或  "
                         "ndd.py pinfn --board <key> --refdes <refdes> <腳位>")
    declared = args.package
    if not declared:
        pp = pj.cfg.get("part_package") or {}
        if args.refdes:
            declared = pp.get("%s:%s" % (args.board, args.refdes))
        declared = declared or pp.get(part)
    ndd_pinfn.lookup(pj.dir, ddir, part, args.pin, args.file,
                     package=declared or "", pick=args.pick)


def cmd_models(args, pj):
    print(describe(pj.models))


# --------------------------------------------------------------------- main --
def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--board", default="all")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--outdir", default="export")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("init"); p.add_argument("dir"); p.add_argument("--force", action="store_true"); p.set_defaults(func=cmd_init, noproj=True)
    p = sub.add_parser("pins"); p.add_argument("refdes", nargs="+"); p.set_defaults(func=cmd_pins)
    p = sub.add_parser("net"); p.add_argument("pattern", nargs="+"); p.add_argument("-v", "--verbose", action="store_true"); p.set_defaults(func=cmd_net)
    p = sub.add_parser("part"); p.add_argument("pattern", nargs="+"); p.set_defaults(func=cmd_part)
    p = sub.add_parser("export"); p.set_defaults(func=cmd_export)
    p = sub.add_parser("audit"); p.set_defaults(func=cmd_audit)
    p = sub.add_parser("mate"); p.set_defaults(func=cmd_mate)
    p = sub.add_parser("trace"); p.add_argument("--signal"); p.set_defaults(func=cmd_trace)
    p = sub.add_parser("datasheets"); p.add_argument("--pn"); p.add_argument("--url"); p.add_argument("--no-download", action="store_true"); p.set_defaults(func=cmd_datasheets)
    p = sub.add_parser("review"); p.set_defaults(func=cmd_review)
    p = sub.add_parser("pinfn"); p.add_argument("part", nargs="?"); p.add_argument("pin", nargs="?"); p.add_argument("--file"); p.add_argument("--refdes"); p.add_argument("--package"); p.add_argument("--pick", type=int); p.add_argument("--list", action="store_true"); p.set_defaults(func=cmd_pinfn)
    p = sub.add_parser("models"); p.set_defaults(func=cmd_models)
    p = sub.add_parser("manifest"); p.set_defaults(func=cmd_manifest)
    p = sub.add_parser("coverage"); p.set_defaults(func=cmd_coverage)

    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 1
    if getattr(args, "noproj", False):
        args.func(args)
        return 0
    # 遷移類與設定類錯誤要給乾淨、可行動的訊息，不要丟 traceback 給使用者。
    # ⚠️ 要包住整個子命令，不能只包 Project() —— MateMapError 是在 fabric()
    #    才拋出的，只包建構子會讓它以 traceback 逸出。
    try:
        pj = Project(args.config or find_config())
        args.func(args, pj)
    except ModelError as exc:
        print("!! models.json 載入失敗：")
        print(exc)
        return 2
    except ndd_graph.MateMapError as exc:
        print("!! mate_map 驗證失敗（已批准的對映本身有問題，先修它）：")
        print(exc)
        return 2
    except ValueError as exc:
        print("!! 設定或 BOM 載入失敗：")
        print(exc)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
