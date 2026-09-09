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
    python ndd.py migrate <分析資料夾>        # 把 v0 的設定升級到 v1

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
import shutil
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
                    "board '%s' 仍使用已改名的 `bom_kind`。請改為 `bom_scope`："
                    "SMT BOM -> smt_only，其餘一律 complete。" % key)
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
# ------------------------------------------------------------------- init --
def _is_connector(nl, refdes):
    fp = (nl.parts.get(refdes) or "").lower()
    return fp.startswith("conn") or bool(re.match(r"^J\d", refdes))


def _scan(d):
    """掃描資料夾並解析。回傳 (netlists, boms, skipped)。"""
    ascs = sorted(f for f in os.listdir(d) if f.lower().endswith(".asc"))
    xlsx = sorted(f for f in os.listdir(d)
                  if f.lower().endswith((".xlsx", ".xlsm")) and not f.startswith("~$"))
    if not ascs:
        raise SystemExit("%s 底下沒有 .asc" % d)
    netlists = [(f, Netlist(os.path.join(d, f))) for f in ascs]
    boms, skipped = {}, []
    for x in xlsx:
        try:
            boms[x] = Bom(os.path.join(d, x))
        except Exception as exc:
            # ⚠️ 解析不了的 BOM 不能只印一行就算了 —— 它會靜默退出配對池，
            #    害某塊板配到錯的 BOM。列進報告要求人工處理。
            skipped.append((x, str(exc)))
    return netlists, boms, skipped


def _board_key(fname, used):
    """board key：可讀、不撞名。舊版硬截 12 字元會讓日期開頭的檔名變 `20260723_fec`。"""
    stem = os.path.splitext(fname)[0]
    # 去掉開頭的日期段與流水號，取有意義的字
    stem = re.sub(r"^\d{6,8}[_\-\s]*", "", stem)
    toks = [t for t in re.split(r"[^A-Za-z0-9]+", stem) if t]
    key = "_".join(toks[:3]).lower()[:20] or "board"
    n, base = 2, key
    while key in used:
        key = "%s%d" % (base, n)
        n += 1
    used.add(key)
    return key


def _pair_boards(netlists, boms):
    """netlist ↔ BOM 配對。回傳每塊板的完整候選排名，不只最佳。"""
    used, rows = set(), []
    for fname, nl in netlists:
        scored = []
        for x, bm in boms.items():
            if not bm.ref:
                continue
            hit = sum(1 for r in bm.ref if r in nl.parts)
            scored.append((hit / float(len(bm.ref)), hit, len(bm.ref), x))
        scored.sort(reverse=True)
        best = scored[0] if scored else (0.0, 0, 0, "")
        second = scored[1][0] if len(scored) > 1 else 0.0
        ok = best[0] >= 0.9 and best[0] - second >= 0.3
        rows.append(dict(key=_board_key(fname, used), asc=fname, nl=nl,
                         bom=best[3], ratio=best[0], second=second,
                         ok=ok, candidates=scored[:4],
                         smt_hint=bool(re.search(r"smt", best[3], re.I))))
    return rows


def _detect_mates(fab, boards):
    """跨板連接器對接自動偵測。回傳 (定案, 歧義, 排除)。

    ⚠️ **不用 footprint 同型分組。** 板對板是公母對接——Samtec 的 `SEAF` 對
       `SEAM`、`TFM` 對 `SFM`，兩側 footprint 名稱本來就不同。用「同型」分組
       會剛好把真正的對接排除掉。改用**腳數**分組，再讓排名去分勝負。

    ⚠️ **兩側都有多個候選 = 實質歧義。** 實測：ECU 的 J902/J903 對 DPU 的
       J2/J1004，四種組合分數**完全相同**（0 矛盾、23 語意），netlist 真的
       分不出來。這種不能猜，要問人。
       只有一側多個（一塊板的連接器對到 N 個同型槽位）則是合理的扇出。
    """
    conns = {}
    for k, (nl, _b) in boards.items():
        for rd in nl.parts:
            if not _is_connector(nl, rd):
                continue
            pins = nl.pins(rd)
            # ⚠️ 板對板對接的判別力來自「腳夠多、名字對得上」。同軸/RF 這種
            #    少腳接頭的對應是**線束決定的**，netlist 判不出來，不要猜。
            if len(pins) < 8:
                continue
            conns.setdefault(len(pins), []).append((k, rd))

    passed, rejected = [], []
    for npin, lst in sorted(conns.items()):
        for i, (ba, ra) in enumerate(lst):
            for bb, rb in lst[i + 1:]:
                if ba == bb:
                    continue                       # 同一塊板上的不算對接
                ok, why = fab._rank_decides(ba, ra, bb, rb)
                row = dict(a="%s.%s" % (ba, ra), b="%s.%s" % (bb, rb),
                           pins=npin, why=why, mate=[ba, ra, bb, rb])
                (passed if ok else rejected).append(row)

    deg = {}
    for r in passed:
        deg[r["a"]] = deg.get(r["a"], 0) + 1
        deg[r["b"]] = deg.get(r["b"], 0) + 1
    accepted, ambiguous = [], []
    for r in passed:
        if deg[r["a"]] > 1 and deg[r["b"]] > 1:
            ambiguous.append(r)
        else:
            accepted.append(r)
    return accepted, ambiguous, rejected


def _name_gap(fab, ba, ra, bb, rb, limit=5):
    """列出直通對應下兩側名字不同的 net 樣本。

    ⚠️ **只報觀察到的差異，不自己發明 `net_normalize` 規則。** 剝錯後綴會讓
       `CLK_1` 與 `CLK_2` 正規化成同一個，排名反而失去鑑別力（pitfalls #8）。
    """
    pa, pb = fab.nl[ba].pins(ra), fab.nl[bb].pins(rb)
    out = []
    for p in sorted(pa):
        na, nb = pa.get(p), pb.get(p)
        if not na or not nb or fab.norm(na) == fab.norm(nb):
            continue
        if fab.cls(na) != fab.cls(nb):
            continue                      # 類別就不同的是矛盾，不是命名差異
        out.append("%s / %s" % (na, nb))
        if len(out) >= limit:
            break
    return out


def _plan(d):
    netlists, boms, skipped = _scan(d)
    rows = _pair_boards(netlists, boms)
    print("=" * 78)
    print("init --plan（只讀，不寫任何檔案）")
    print("=" * 78)
    print("\n[1] netlist ↔ BOM 配對（用 refdes 交集，不用檔名猜）")
    need_ask = []
    for r in rows:
        print("  %-20s parts %5d / signals %5d" % (r["key"], len(r["nl"].parts),
                                                   len(r["nl"].nets)))
        print("       -> %s  命中率 %.0f%%（次佳 %.0f%%）  %s"
              % (r["bom"] or "(無)", r["ratio"] * 100, r["second"] * 100,
                 "OK" if r["ok"] else "!! 需你確認"))
        if not r["ok"]:
            need_ask.append(r)
            for ratio, hit, tot, x in r["candidates"]:
                print("          候選 %5.0f%%  (%d/%d)  %s" % (ratio * 100, hit, tot, x))
        if r["smt_hint"]:
            print("          （檔名含 SMT -> 建議 bom_scope: smt_only）")
    if skipped:
        print("\n  !! 有 %d 份 BOM 解析失敗，**不會參與配對**：" % len(skipped))
        for x, why in skipped:
            print("     %s —— %s" % (x, why))

    boards = {r["key"]: (r["nl"], boms[r["bom"]]) for r in rows if r["bom"] in boms}
    print("\n[2] 連接器對接自動偵測")
    if len(boards) < 2:
        print("  （只有一塊板，無跨板對接）")
        acc, amb, rej = [], [], []
    else:
        fab = Fabric(boards, [], {}, None, [])
        acc, amb, rej = _detect_mates(fab, boards)
        print("  自動定案 %d 組、**需你確認** %d 組、排除 %d 組"
              % (len(acc), len(amb), len(rej)))
        for r in acc[:10]:
            print("     OK  %s <-> %s (%d pin)" % (r["a"], r["b"], r["pins"]))
        for r in amb:
            print("     !!  %s <-> %s (%d pin) —— 兩側都有多個同分候選，"
                  "netlist 分不出來" % (r["a"], r["b"], r["pins"]))
        near = [r for r in rej if "難以區分" in r["why"]]
        for r in rej[:4]:
            print("     --  %s <-> %s (%d pin) —— %s"
                  % (r["a"], r["b"], r["pins"], r["why"][:64]))
        if near:
            print("     ↳ 有 %d 組是「零矛盾但與次佳難以區分」。兩側命名差異樣本："
                  % len(near))
            for g in _name_gap(fab, *near[0]["mate"]):
                print("         %s" % g)
            print("       填好 ndd.json 的 net_normalize 後重跑 `ndd.py mate` "
                  "可能就能定案。")

    print("\n[3] datasheet 盤點")
    ddir = os.path.join(d, "datasheets")
    have = len([f for f in os.listdir(ddir)
                if f.lower().endswith(".pdf")]) if os.path.isdir(ddir) else 0
    pns = set()
    for r in rows:
        bm = boms.get(r["bom"])
        if bm:
            for rd in r["nl"].actives():
                pn = bm.pn(rd)
                if isinstance(pn, str) and pn:
                    pns.add(pn)
    print("  主動料號 %d 種；datasheets/ 現有 PDF %d 份" % (len(pns), have))
    print("  自動下載只對少數原廠站有效，其餘要人工補。")

    print("\n" + "-" * 78)
    print("需要你決定：")
    print("  1. BOM 配對 —— %s"
          % ("全部高信心，無需確認" if not need_ask
             else "%d 塊板需確認：%s" % (len(need_ask),
                                        ", ".join(r["key"] for r in need_ask))))
    print("  2. datasheet —— 要下載還是跳過（跳過仍會產生 MISSING.md 清單）")
    print("\n決定後執行：ndd.py init <資料夾> --run [--bom key=檔名]... "
          "[--no-datasheets]")
    return rows, acc, amb, rej, skipped


def _run_step(name, fn, results):
    """跑一個步驟。**失敗不中止整條流程**，記錄下來寫進 SETUP.md。"""
    print("\n" + "=" * 78)
    print(">>> %s" % name)
    print("=" * 78)
    try:
        out = fn()
        results.append((name, "OK", ""))
        return out
    except SystemExit as exc:
        results.append((name, "跳過", str(exc)))
        print("（跳過：%s）" % exc)
    except Exception as exc:
        results.append((name, "失敗", str(exc)))
        print("!! 失敗：%s" % exc)
    return None


SETUP_TMPL = u"""# 專案建立報告

> 由 `ndd.py init --run` 產生於 {date}。
> **這份檔案記錄 init 做了什麼、你確認了什麼、還缺什麼。**

## 1. 自動決定的

### netlist ↔ BOM 配對
用 **refdes 交集**配對（不用檔名猜 —— 檔名常含產品線代號與日期這種共通 token）。

{pairing}

### 連接器對接
門檻與 `ndd.py mate` 相同：**直通唯一勝出 + 零矛盾 + margin ≥ 2**
（亞軍也乾淨時才要求 margin）。連接器只負責「訊號有沒有連到」，netlist 連得上
即事實，不需要 datasheet。

自動寫入 {n_acc} 組：

{mates_ok}

未能定案 {n_rej} 組（**已列出但沒寫進 ndd.json**）：

{mates_no}

## 2. 你確認過的

{decisions}

## 3. 流程執行結果

{steps}

## 4. 需要你補的

{todo}

---

逐腳查詢用 `ndd.py pins/net/part`，不要靠本文。
"""


def cmd_init(args):
    import datetime
    d = os.path.abspath(args.dir)
    if not args.run:
        _plan(d)
        return

    netlists, boms, skipped = _scan(d)
    rows = _pair_boards(netlists, boms)
    override = dict(kv.split("=", 1) for kv in (args.bom or []))
    for r in rows:
        if r["key"] in override:
            r["bom"], r["ok"], r["forced"] = override[r["key"]], True, True
    unresolved = [r for r in rows if not r["ok"]]
    if unresolved and not args.accept_pairing:
        raise SystemExit(
            "以下板子的 BOM 配對信心不足，**必須先確認**：%s\n"
            "  用 --bom <key>=<檔名> 指定，或確認後加 --accept-pairing。\n"
            "  先跑 `ndd.py init %s --plan` 看候選清單。"
            % (", ".join(r["key"] for r in unresolved), args.dir))

    boards_cfg = {}
    for r in rows:
        boards_cfg[r["key"]] = {
            "label": os.path.splitext(r["asc"])[0], "asc": r["asc"],
            "bom": r["bom"], "bom_scope": "smt_only" if r["smt_hint"] else "complete",
            "sheet": None, "ref_col": None, "expand_ranges": False}

    loaded = {r["key"]: (r["nl"], boms[r["bom"]]) for r in rows if r["bom"] in boms}
    acc, amb, rej = ([], [], [])
    if len(loaded) >= 2:
        acc, amb, rej = _detect_mates(Fabric(loaded, [], {}, None, []), loaded)
    n_auto = len(acc)
    if amb and args.accept_mates:
        for r in amb:
            r["confirmed"] = True
        acc, amb = acc + amb, []

    cfg = {
        "project": os.path.basename(d.rstrip("\\/")) or "unnamed",
        "boards": boards_cfg,
        "mates": [r["mate"] for r in acc],
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
    print("寫出 %s" % p)

    # ---- 一路跑完，中間不再詢問 ----
    pj = Project(p)
    results = []
    def A(**kw):
        base = dict(board="all", outdir="export", limit=40, signal=None,
                    pn=None, url=None, no_download=False)
        base.update(kw)                 # ⚠️ 用 update，不要當關鍵字傳兩次
        return argparse.Namespace(**base)

    _run_step("export —— 逐腳事實表", lambda: cmd_export(A(), pj), results)
    if not args.no_datasheets:
        _run_step("datasheets —— 盤點與下載", lambda: cmd_datasheets(A(), pj), results)
    else:
        _run_step("datasheets —— 只盤點不下載",
                  lambda: cmd_datasheets(A(no_download=True), pj), results)
    stats = _run_step("audit —— 一致性稽核", lambda: run_audit(
        pj.cfg, pj.all_boards("all"), pj.models), results)
    _run_step("mate —— 連接器對接排名", lambda: cmd_mate(A(), pj), results)
    _run_step("trace —— 端到端訊號鏈", lambda: cmd_trace(A(), pj), results)
    _run_step("coverage —— per-MPN 三源覆蓋", lambda: cmd_coverage(A(), pj), results)
    _run_step("manifest —— 輸入檔指紋", lambda: cmd_manifest(A(), pj), results)
    _run_step("review —— 人工複驗清單", lambda: cmd_review(A(), pj), results)

    # ---- SETUP.md ----
    pairing = "\n".join(
        "- `%s` ← `%s`（命中率 %.0f%%，次佳 %.0f%%）%s%s"
        % (r["key"], r["bom"] or "(無)", r["ratio"] * 100, r["second"] * 100,
           "　**你指定的**" if r.get("forced") else "",
           "　bom_scope=smt_only（檔名含 SMT）" if r["smt_hint"] else "")
        for r in rows)
    if skipped:
        pairing += "\n\n**%d 份 BOM 解析失敗，未參與配對：**\n" % len(skipped)
        pairing += "\n".join("- `%s` —— %s" % x for x in skipped)
    todo = []
    if amb:
        todo.append("- [ ] **%d 組對接兩側都有同分候選** —— netlist 分不出來，"
                    "請直接編輯 `ndd.json` 的 `mates` 指定正確組合" % len(amb))
    if rej:
        todo.append("- [ ] **%d 組對接被排除** —— 見上方原因；若確實對接請手動加入"
                    % len(rej))
    if stats:
        if stats.get("model_pending"):
            todo.append("- [ ] **%d 項封裝待指定** —— 見 `ndd.py audit` [1.5] 段"
                        % len(stats["model_pending"]))
        if stats.get("model_inferred"):
            todo.append("- [ ] **%d 項封裝是推論的** —— 請複核" % len(stats["model_inferred"]))
    todo.append("- [ ] **未分類端點** —— 跑 `ndd.py coverage`，把終端負載與穿越件"
                "逐一填進 `ndd.json` 的 `endpoints`")
    todo.append("- [ ] **缺 datasheet 的料號** —— 見 `datasheets/MISSING.md`")
    todo.append("- [ ] **尚未定義任何斷言** —— 文件寫到哪，`assertions` 就要補到哪")
    todo.append("- [ ] **`trace.start` 未設** —— trace 目前從所有對接連接器出發；"
                "要聚焦某條鏈請填入")
    txt = SETUP_TMPL.format(
        date=datetime.date.today().isoformat(),
        pairing=pairing,
        n_acc=len(acc), n_rej=len(rej) + len(amb),
        mates_ok="\n".join("- `%s <-> %s`（%d pin）　%s" % (r["a"], r["b"], r["pins"], r["why"])
                            for r in acc) or "- （無）",
        mates_no="\n".join("- `%s <-> %s`（%d pin）　%s" % (r["a"], r["b"], r["pins"], r["why"])
                            for r in rej) or "- （無）",
        decisions="- BOM 配對：%s\n- datasheet：%s\n- 對接：%s"
                  % ("已由你指定 " + ", ".join(override) if override
                     else ("你確認採用自動配對" if args.accept_pairing
                           else "全部高信心，未需確認"),
                     "跳過下載（仍產生 MISSING.md）" if args.no_datasheets else "已嘗試下載",
                     ("自動定案 %d 組；另 %d 組兩側同分，你確認一併採用"
                      % (n_auto, len(acc) - n_auto)) if len(acc) > n_auto
                     else "自動定案 %d 組" % n_auto),
        steps="\n".join("- %-28s %s%s" % (n, st, "　—— " + why[:60] if why else "")
                         for n, st, why in results),
        todo="\n".join(todo))
    sp = os.path.join(d, "SETUP.md")
    with io.open(sp, "w", encoding="utf-8") as fh:
        fh.write(txt)
    print("\n" + "=" * 78)
    print("寫出 %s" % sp)
    print("init 完成。產生的 .md：SETUP.md / MANIFEST.md / REVIEW.md"
          "%s" % ("" if args.no_datasheets else " / datasheets/MISSING.md"))
    print("可以開始問電路問題了。")


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
                    # BOM 是權威 -> 缺席即未貼件。唯一例外是 SMT BOM 不涵蓋
                    # 連接器/測試點/手插件那一類。
                    from ndd_audit import dni_provable
                    stuffed = "N" if dni_provable(nl, bom, rd)[0] else "UNKNOWN"
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
    fab = pj.fabric()
    if not starts:
        # ⚠️ 舊版在這裡直接中止，讓 init 無法一路跑完。改為**自動用所有參與
        #    對接的連接器當起點** —— 追得到什麼算什麼，總比什麼都不產出好。
        seen = []
        for ba, ra, bb, rb in fab.mates:
            for k, rd in ((ba, ra), (bb, rb)):
                if (k, rd) not in seen:
                    seen.append((k, rd))
        starts = [{"board": k, "conn": rd, "rail": ""} for k, rd in seen]
        if not starts:
            raise SystemExit("trace.start 是空的，且沒有任何 mates 可當起點。"
                             "請填 ndd.json 的 trace.start 或 mates。")
        print("（trace.start 未設 —— 自動用 %d 個對接連接器當起點。"
              "要聚焦某條鏈請填 trace.start。）" % len(starts))
    amb = [k for k, v in fab.mate_status.items() if v == "ambiguous"]
    inf = [k for k, v in fab.mate_status.items() if v == "inferred"]
    if inf:
        print("   %d 組對接由拓樸排名定案 [?]（netlist 連得上即事實）。" % len(inf))
    if amb:
        print("!! %d 組對接**排名無法定案**，下游路徑帶 mate:ambiguous：" % len(amb))
        for ba, ra, bb, rb in amb:
            print("   %s.%s <-> %s.%s —— %s"
                  % (ba, ra, bb, rb, fab.mate_evidence.get((ba, ra, bb, rb), "")))
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


# ----------------------------------------------------------------- migrate --
BOM_KIND_MAP = [(r"SMT", "smt_only")]           # 其餘一律 complete


def cmd_migrate(args):
    """把 v0 建立的專案設定升級到 v1。**只改設定，不動任何原始檔。**

    會做的：
      - `bom_kind` -> `bom_scope`（含 SMT 字樣 -> smt_only，其餘 complete）
      - 補上缺少的空殼欄位（mate_map / part_package / endpoints）
      - 原檔備份成 `ndd.json.v0.bak`

    **不會做的**（刻意）：
      - 不把 models.json 的 `pairs` 自動轉成 `transfer` —— 舊 schema 沒有方向
        資訊，機械轉換只會把「單向元件可雙向走」這個錯誤帶進新 schema。
      - 不清掉舊的 pinfn 快取 —— 原文還有用，只是會被標成待重新確認。
    """
    cfg_path = args.config or find_config(args.dir or os.getcwd())
    d = os.path.dirname(cfg_path)
    with io.open(cfg_path, encoding="utf-8") as fh:
        cfg = json.load(fh)

    changed = []
    for k, b in (cfg.get("boards") or {}).items():
        if "bom_kind" in b and "bom_scope" not in b:
            old = b.pop("bom_kind") or ""
            scope = "complete"
            for pat, val in BOM_KIND_MAP:
                if re.search(pat, old, re.I):
                    scope = val
                    break
            b["bom_scope"] = scope
            changed.append("boards.%s: bom_kind %r -> bom_scope %r"
                           % (k, old[:30], scope))
        for f, default in (("sheet", None), ("ref_col", None),
                           ("expand_ranges", False)):
            if f not in b:
                b[f] = default
                changed.append("boards.%s: 補上 %s" % (k, f))
    for f in ("mate_map", "part_package", "endpoints"):
        if f not in cfg:
            cfg[f] = {}
            changed.append("補上 %s（空殼）" % f)

    if changed:
        bak = cfg_path + ".v0.bak"
        if not os.path.exists(bak):
            shutil.copy2(cfg_path, bak)
        with io.open(cfg_path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(cfg, indent=2, ensure_ascii=False))
        print("已更新 %s（原檔備份為 %s）" % (cfg_path, os.path.basename(bak)))
        for c in changed:
            print("   %s" % c)
    else:
        print("設定已是 v1 格式，無需變更。")

    print("")
    todo = []
    mp = os.path.join(d, "models.json")
    if os.path.exists(mp):
        try:
            raw = json.load(io.open(mp, encoding="utf-8"))
        except Exception as exc:
            raw = {}
            print("!! models.json 讀取失敗：%s" % exc)
        legacy = [k for k, v in raw.items() if isinstance(v, dict) and "pairs" in v]
        if legacy:
            todo.append(
                "models.json 有 %d 個模型仍是舊的 `pairs` schema：%s\n"
                "     **不要機械轉換** —— 舊 schema 沒有方向資訊，照抄會把"
                "「單向元件可雙向走」的錯誤帶進新 schema。\n"
                "     請重翻 datasheet 補 `direction`（forward / bidirectional）、"
                "`pin_roles`（VSS/VDD 腳號）、結構化的 `control`。\n"
                "     schema 見 references/models.md。"
                % (len(legacy), ", ".join(legacy[:6])))
    else:
        todo.append(
            "沒有 models.json。v0 內建的 4 個腳位模型（bus switch / buffer /\n"
            "     clock fanout / I2C mux）**已移除** —— 通用型工具不該把特定料號\n"
            "     當成預設知識。若你的追跡或 i2c_addr 斷言依賴它們，請自行查證後\n"
            "     建立專案的 models.json（見 references/models.md）。")

    _cp, rows, mig = ndd_pinfn.load_cache(d)
    if mig:
        todo.append(
            "verified-pins.csv 有 %d 列是舊格式（缺 package 欄）。原文還在可以讀，\n"
            "     但**不會被當成已解析的快取使用** —— 舊列若被當成有效，等於把\n"
            "     未鎖定封裝的資料洗成合法覆蓋。需要哪支腳就重跑 pinfn。" % mig)

    todo.append(
        "衍生產物請刪掉重跑：`export/`、`REVIEW.md`。欄位已變動\n"
        "     （signal_chain.csv 新增 caveats/confidence/endpoint_kind，\n"
        "     loads 欄不再含驅動端）。")

    print("接下來：")
    for i, t in enumerate(todo, start=1):
        print("  %d. %s" % (i, t))
    print("")
    print("都處理完後跑：ndd.py audit  ->  ndd.py review")
    return changed


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

    p = sub.add_parser("init"); p.add_argument("dir")
    p.add_argument("--plan", action="store_true", help="只讀不寫，印出配對與對接候選")
    p.add_argument("--run", action="store_true", help="寫設定並一路跑完所有流程")
    p.add_argument("--bom", action="append", metavar="KEY=檔名", help="指定某塊板的 BOM")
    p.add_argument("--accept-pairing", action="store_true", help="確認採用自動配對")
    p.add_argument("--accept-mates", action="store_true", help="連同同分的對接候選一併採用")
    p.add_argument("--no-datasheets", action="store_true", help="跳過下載，只產生缺件清單")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init, noproj=True)
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
    p = sub.add_parser("migrate"); p.add_argument("dir", nargs="?")
    p.set_defaults(func=cmd_migrate, noproj=True)

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
