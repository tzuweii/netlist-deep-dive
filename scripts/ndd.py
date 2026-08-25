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
    python ndd.py models                      # 列出已查證的腳位模型

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
from ndd_models import describe, load_models, pairs_for            # noqa: E402
from ndd_pads import Netlist, refkey                               # noqa: E402
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
            nl = Netlist(os.path.join(self.dir, b["asc"]))
            bom = Bom(os.path.join(self.dir, b["bom"]), ref_col=b.get("ref_col"),
                      kind=b.get("bom_kind", ""))
            self._cache[key] = (nl, bom)
        return self._cache[key]

    def all_boards(self, which="all"):
        return {k: self.load(k) for k in self.board_keys(which)}

    def fabric(self):
        return Fabric(self.all_boards(), [tuple(m) for m in self.cfg.get("mates", [])],
                      self.models, self.cfg.get("power_net_regex"),
                      self.cfg.get("net_normalize"))

    def label(self, key):
        return self.cfg["boards"][key].get("label", key)


def pn_of(bom, refdes):
    d = bom.of(refdes)
    if d is None:
        return "(未在 BOM refdes 欄中 -> DNI)"
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
                       "bom_kind": "", "ref_col": None}
        print("  %-14s parts %5d / signals %5d" % (key, len(nl.parts), len(nl.nets)))
        print("       -> BOM %-58s refdes 命中率 %.0f%% (次佳 %.0f%%)  %s"
              % (best or "(無)", ratio * 100, second * 100, conf))
    cfg = {
        "project": os.path.basename(os.path.dirname(d)) or "unnamed",
        "boards": boards,
        "mates": [],
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
    print("接著要人工補：mates（連接器對接）、net_normalize、trace.start、"
          "以及每塊板的 bom_kind（是 SMT BOM 還是完整 BOM，影響 DNI 判讀）")


def cmd_pins(args, pj):
    for refdes in args.refdes:
        hits = [b for b in pj.board_keys(args.board) if refdes in pj.load(b)[0].parts]
        if not hits:
            print("!! %s 不在任何 netlist 中" % refdes)
            continue
        for b in hits:
            nl, bom = pj.load(b)
            fp = nl.parts[refdes]
            model, pairs = pairs_for(pj.models, fp, bom.pn(refdes) or "",
                                     len(nl.pins(refdes)))
            print("== %s  [%s]" % (refdes, pj.label(b)))
            print("   footprint : %s" % fp)
            print("   BOM       : %s" % pn_of(bom, refdes))
            if pairs:
                print("   導通模型  : %s（%d 組）" % (model, len(pairs)))
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
                        "value", "class", "stuffed", "net_pin_count"])
            for rd in sorted(nl.parts, key=refkey):
                d = bom.of(rd)
                cls = ("passive" if nl.is_passive(rd)
                       else "mech" if nl.is_mech(rd) else "active")
                pins = nl.pins(rd) or {"": ""}
                for p, net in pins.items():
                    w.writerow([rd, p, net, nl.parts[rd], bom.pn(rd) or "",
                                bom.value(rd) or "", cls, "Y" if d else "N",
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
            far = [(bb, rb) for (ba, ra, bb, rb) in fab.mates if ba == b and ra == conn]
            far += [(ba, ra) for (ba, ra, bb, rb) in fab.mates if bb == b and rb == conn]
            # 第一段：走到中繼（slot）連接器
            mids = fab.trace((b, conn, pin),
                             lambda n: n[0] == b and slot_rx.match(n[1] or ""))
            if not mids:
                rows.append(dict(rail=st.get("rail", ""), signal=net, slot="",
                                 start="%s.%s" % (conn, pin), mid="", far_pin="",
                                 far_net="(未到達中繼連接器)", n_loads=0, loads="",
                                 hops=""))
                continue
            for (mb, mrd, mpin), path in sorted(
                    mids.items(), key=lambda x: (x[0][1], x[0][2])):
                tgt = next(((bb, rb) for (ba, ra, bb, rb) in fab.mates
                            if ba == mb and ra == mrd), None)
                if tgt is None:
                    continue
                tb, trd = tgt
                ends = fab.trace((tb, trd, mpin), fab.is_terminal, max_depth=6)
                fnet = fab.nl[tb].pin_net(trd, mpin)
                loads = sorted("%s.%s" % (rd, p) for _b, rd, p in ends)
                # slot 索引：slot_pattern 若有 capture group 就用它，否則用整個 refdes
                sm = slot_rx.match(mrd)
                slot = sm.group(1) if (sm and sm.groups()) else mrd
                rows.append(dict(
                    rail=st.get("rail", ""), signal=net,
                    slot=slot, start="%s.%s" % (conn, pin),
                    mid="%s.%s" % (mrd, mpin), far_pin="%s.%s" % (trd, mpin),
                    far_net=fnet or "", n_loads=len(loads), loads=" ".join(loads),
                    hops=fab.hop_string(path)))

    if args.signal:
        shown = 0
        for r in rows:
            if r["slot"] not in ("", "1", "101") and shown:
                continue
            print("\n%-22s rail %s  slot %s" % (r["signal"], r["rail"], r["slot"]))
            print("   起點  : %s" % r["start"])
            print("   hops  : %s" % r["hops"])
            print("   -> %s = %s -> %s" % (r["mid"], r["far_pin"], r["far_net"]))
            print("   負載  : %d 個  %s" % (r["n_loads"], r["loads"][:120]))
            shown += 1
        return rows

    out = os.path.join(pj.dir, args.outdir)
    if not os.path.isdir(out):
        os.makedirs(out)
    path = os.path.join(out, "signal_chain.csv")
    cols = ["rail", "signal", "slot", "start", "hops", "mid", "far_pin",
            "far_net", "n_loads", "loads"]
    with io.open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})
    print("寫出 %s  (%d 列)" % (path, len(rows)))
    print("  走到終端腳: %d 列 / 未到達: %d 列"
          % (sum(1 for r in rows if r["n_loads"]),
             sum(1 for r in rows if not r["n_loads"])))
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


# ------------------------------------------------------------------ review --
REVIEW_TMPL = u"""# 人工複驗清單

> 由 `ndd.py review` 產生。**這份清單上的每一項，工具都不能替你確認。**
> 專案：{project}

## A. 工具驗得到、且已通過的（不需複驗，列出供追溯）

- parser 自我驗證：{parser}
- 文件斷言：{apass} 條通過{afail}
- 命名規則展開：{role}

## B. 必須人工複驗的

### B1. 腳位模型（最高優先）
追跡結果完全建立在這些模型上；模型錯 → 訊號鏈看起來合理但整張表是錯的。

{models}

- [ ] 上表每一筆的 `verified_against` 都真的翻過那一頁？
- [ ] 有沒有用到**相近型號**的腳位當成同一顆？（例如 -Q1 / 不同封裝腳位不同）

### B2. 連接器對接
{mates}

- [ ] margin ≤ 4 的項目，是否已用 layout 或 continuity 確認？
- [ ] 兩側同型（都是公頭）的對接，是否已取得線束圖？

### B3. netlist 本身答不出來的
- [ ] **netlist ≠ 實體板**：rework／飛線／換料都不在 `.asc` 裡。手上這片的 rework 紀錄查過了嗎？
- [ ] **layout 決定的量**（阻抗、插入損耗、耦合、串音）不能跨版本沿用。
- [ ] **BOM 版本**：拿到的是哪一個 build 變體？是 SMT BOM 還是完整 BOM？
- [ ] **線束**：板間同軸／排線的對應關係不在任何 netlist 裡。
- [ ] **未貼件 (DNI)**：{dni}
- [ ] **懸空（單腳）網路**：{floating}

### B4. 文件裡的因果推論
斷言只驗得到「數值與連線」。凡是「為什麼這樣設計」「這個 RC 造成 N ms 延遲」
「這個拓樸是為了匹配路徑長度」之類的推論，**工具一律驗不到**，需實測或問設計者。

- [ ] 文件中每一句因果推論，都有標記為推論（⚠️）或附上佐證？
"""


def cmd_review(args, pj):
    stats = run_audit(pj.cfg, pj.all_boards("all"), pj.models)
    print("\n" + "=" * 78)
    rep = pj.fabric().verify_mating(verbose=False) if pj.cfg.get("mates") else []
    mates = "\n".join(
        "- `%s <-> %s`：最佳 **%s**（矛盾 %d、語意 %d），次佳 %s（%d）；margin **%d**。%s"
        % (r["a"], r["b"], r["best"], r["best_bad"], r["best_match"],
           r["second"], r["second_match"], r["margin"], r["kind"]) for r in rep
    ) or "- （尚未設定 mates）"
    models = "\n".join(
        "- `%s`：%s" % (k, v["verified_against"]) for k, v in sorted(pj.models.items()))
    txt = REVIEW_TMPL.format(
        project=pj.cfg.get("project", ""),
        parser="全部通過" if not stats["parser_fail"] else "**失敗**: %s" % stats["parser_fail"],
        apass=stats["assert_pass"],
        afail="" if not stats["assert_fail"] else "，**失敗 %d 條：%s**"
              % (len(stats["assert_fail"]), "；".join(stats["assert_fail"][:5])),
        role="0 筆未解釋的不符" if stats["role_bad"] == 0
             else "**%d 筆未解釋的不符**" % stats["role_bad"],
        models=models, mates=mates,
        dni="; ".join("%s: %s" % (k, ", ".join(v) or "無") for k, v in stats["dni"].items()),
        floating="; ".join("%s: %d 條" % (k, len(v)) for k, v in stats["floating"].items()))
    p = os.path.join(pj.dir, "REVIEW.md")
    with io.open(p, "w", encoding="utf-8") as fh:
        fh.write(txt)
    print("寫出 %s" % p)


def cmd_pinfn(args, pj):
    dcfg = pj.cfg.get("datasheets") or {}
    ddir = os.path.join(pj.dir, dcfg.get("dir", "datasheets"))
    if args.list:
        p, rows = ndd_pinfn.load_cache(pj.dir)
        print("原文快取 %s（%d 筆）" % (p, len(rows)))
        for r in rows:
            print("  %-18s pin %-4s %-12s %-8s %s p.%s"
                  % (r["part"], r["pin"], r["pin_name"], r["direction"],
                     r["source_file"], r["page"]))
        return
    if not args.part or args.pin is None:
        raise SystemExit("用法：ndd.py pinfn <料號> <腳位> [--file x.pdf]")
    ndd_pinfn.lookup(pj.dir, ddir, args.part, args.pin, args.file)


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
    p = sub.add_parser("pinfn"); p.add_argument("part", nargs="?"); p.add_argument("pin", nargs="?"); p.add_argument("--file"); p.add_argument("--list", action="store_true"); p.set_defaults(func=cmd_pinfn)
    p = sub.add_parser("models"); p.set_defaults(func=cmd_models)

    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 1
    if getattr(args, "noproj", False):
        args.func(args)
        return 0
    args.func(args, Project(args.config or find_config()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
