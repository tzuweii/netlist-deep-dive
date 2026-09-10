#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""稽核引擎：把文件裡的主張變成可執行的斷言，跑一次就知道文件還對不對。

設計取向：**一次性稽核，不是持續維護的回歸測試**。

檢查段（每一種失敗都要有明確的浮現位置——跑完 audit 全 PASS 卻仍有未解
事項存在，就等於回到「綠燈不代表查過」）：
  [0]   parser 自我驗證 —— 最底層，parser 漏讀則上面全部不成立
  [1]   文件斷言
  [1.5] 元件模型 —— pin-existence 與 package 解析狀態
  [2]   命名規則展開   —— ⚠️ 通則會有例外，逐顆展開比對才抓得到
  [3]   netlist vs BOM —— 依 bom_scope 決定能不能說「未貼件」
  [4]   懸空網路

⚠️ **「待辦」與「錯誤」必須可區分，且待辦不得讓 audit 假性通過。**
   跑完 audit 全 PASS 卻仍有未解事項，就等於回到「綠燈不代表查過」。
"""
import re

import ndd_package
from ndd_bom import is_ambiguous
from ndd_graph import Fabric
from ndd_models import i2c_addr, missing_pins
from ndd_pads import refkey


def _expand_pins(spec):
    if isinstance(spec, list):
        return [str(x) for x in spec]
    m = re.match(r"^(\d+)\s*-\s*(\d+)$", str(spec))
    if m:
        return [str(i) for i in range(int(m.group(1)), int(m.group(2)) + 1)]
    return [str(spec)]


def is_connector(nl, refdes):
    fp = (nl.parts.get(refdes) or "").lower()
    return fp.startswith("conn") or bool(re.match(r"^J\d", refdes))


def dni_provable(nl, bom, refdes):
    """這份 BOM 能不能證明這顆 refdes「沒貼」？

    **預設可以** —— 使用者給的 BOM 就是這塊板的權威，缺席即未貼件。

    ⚠️ 唯一的例外是文件涵蓋範圍（不是正確性）：SMT BOM 依定義不列連接器、
       測試點、鎖孔、手插件，它們的缺席不代表沒貼。
    """
    if bom.covers_class(nl.is_mech(refdes), is_connector(nl, refdes)):
        return True, ""
    return False, "非 SMT 件不在 SMT BOM 的涵蓋範圍內"


def run_assertion(a, nl, bom, models):
    """回傳 (ok, 實際值字串)。新增 kind 時務必同步更新 references/pitfalls.md。"""
    k = a["kind"]

    if k == "count_pn":
        got = bom.count_pn(a["pn"])
        return got == a["expect"], got

    if k == "pin_net":
        got = nl.pin_net(a["refdes"], a["pin"])
        return got == a["expect"], got

    if k == "pin_net_seq":
        pins = _expand_pins(a["pins"])
        bad = []
        for idx, p in enumerate(pins, start=int(a.get("start", 1))):
            want = a["expect"].replace("{i}", str(idx)).replace(
                "{i-1}", str(idx - 1))
            got = nl.pin_net(a["refdes"], p)
            if got != want:
                bad.append("pin%s: %s != %s" % (p, got, want))
        return not bad, ("全部相符" if not bad else "; ".join(bad[:3]))

    if k == "bom_value":
        got = bom.value(a["refdes"])
        if is_ambiguous(got):
            return False, "bom:ambiguous %s" % got
        got = got or ""
        return a["contains"].lower() in got.replace(" ", "").lower(), got

    if k == "not_stuffed":
        # ⚠️ 只有 complete BOM 才能證明「沒貼」。SMT BOM／變體／未知範圍的缺件
        #    只代表不在這份 BOM 的範圍內，不是未貼件。
        rds = a["refdes"] if isinstance(a["refdes"], list) else [a["refdes"]]
        blocked = [(r, why) for r, why in
                   ((r, dni_provable(nl, bom, r)[1]) for r in rds) if why]
        if blocked:
            return False, ("bom-scope-insufficient：%s"
                           % "；".join("%s（%s）" % x for x in blocked[:3]))
        amb = [r for r in rds if is_ambiguous(bom.of(r))]
        if amb:
            return False, "bom:ambiguous %s" % amb
        stuffed = [r for r in rds if bom.of(r) is not None]
        return not stuffed, ("皆未貼件" if not stuffed else "這些其實有貼: %s" % stuffed)

    if k == "net_pin_count":
        got = len(nl.net(a["net"]))
        return got == a["expect"], got

    if k == "net_count":
        got = len(nl.find_net(a["pattern"]))
        return got == a["expect"], got

    if k == "single_pin_nets":
        sp = nl.single_pin_nets()
        ok = len(sp) == a["expect"]
        if ok and a.get("all_from"):
            ok = all(a["all_from"].upper() in nl.parts[nl.net(n)[0][0]].upper()
                     for n in sp)
        return ok, "%d 條" % len(sp)

    if k == "i2c_addr":
        pn = bom.pn(a["refdes"])
        pn = "" if is_ambiguous(pn) else (pn or "")
        got, cav = i2c_addr(models, nl, a["refdes"], pn)
        want = int(a["expect"], 16) if isinstance(a["expect"], str) else a["expect"]
        if got is None:
            return False, "無法判定（模型未定義 addr%s）" % (
                "；" + ",".join(cav) if cav else "")
        return got == want, hex(got)

    if k == "net_exists":
        return a["net"] in nl.nets, (a["net"] in nl.nets)

    raise ValueError("未知的 assertion kind: %s" % k)


def role_check(nl, bom, rule, out):
    lo, hi = rule["index"]
    exc = rule.get("known_exceptions", {})
    bad = known = 0
    out.append("--- %s ---" % rule.get("desc", "命名規則"))
    for n in range(lo, hi + 1):
        for tmpl, pn in rule["roles"].items():
            rd = tmpl.replace("{n}", str(n))
            if rd not in nl.parts:
                out.append("  X  %-10s 不存在於 netlist（規則預期 %s）" % (rd, pn))
                bad += 1
                continue
            got = bom.pn(rd)
            if is_ambiguous(got):
                out.append("  X  %-10s BOM 多列衝突（列 %s），無法比對"
                           % (rd, ",".join(str(x) for x in got.rows)))
                bad += 1
                continue
            got = got or ("(不在 BOM -> %s)" % bom.absent_label())
            if got.upper() == pn.upper():
                continue
            if rd in exc:
                out.append("  ~  %-10s 已知例外：%s" % (rd, exc[rd]))
                known += 1
            else:
                out.append("  X  %-10s 規則預期 %-26s 實際 %s" % (rd, pn, got))
                bad += 1
    out.append("  => %s（%d 筆未解釋的不符, %d 筆已知例外）"
               % ("通過" if bad == 0 else "有未解釋的不符", bad, known))
    return bad


def model_check(cfg, boards, models, out):
    """[1.5] 元件模型 —— 封裝判定（只用 netlist/BOM）與 pin-existence。

    ⚠️ pin-existence 是 **sanity check，不是封裝驗證**：兩個封裝同為 1–N 而
       腳位定義不同時它必然通過。封裝由 `ndd_package` 依證據排名判定，推論
       出來的一律標 `[?]`。
    """
    part_pkg = cfg.get("part_package") or {}
    fails, pending, inferred = [], [], []
    for b, (nl, bom) in sorted(boards.items()):
        for rd in sorted(nl.parts, key=refkey):
            pn = bom.pn(rd)
            pn = "" if is_ambiguous(pn) else (pn or "")
            declared = part_pkg.get("%s:%s" % (b, rd)) or part_pkg.get(pn)
            res = ndd_package.resolve(models, nl, bom, b, rd, Fabric.cls,
                                      declared)
            if res["status"] is None:
                continue
            line = "%s.%s (%s) %s" % (b, rd, pn or "?", ndd_package.describe(res))
            if res["status"] == ndd_package.CONFLICT:
                fails.append(line)
                continue
            if res["status"] == ndd_package.UNRESOLVED:
                pending.append(line)
                continue
            if res["status"] == ndd_package.INFERRED:
                inferred.append(line)
            m = res["model"]
            miss = missing_pins(m, nl.pins(rd).keys())
            if miss:
                fails.append("%s.%s 模型 %s 引用的腳 %s 不存在於 netlist"
                             % (b, rd, res["name"], ",".join(miss)))
    if not models:
        out.append("  （尚未定義任何模型 —— 追跡會停在每顆主動件）")
    for f in fails:
        out.append("  FAIL %s" % f)
    for i in inferred:
        out.append("  [?]  %s" % i)
    for p in pending:
        out.append("  待辦 %s" % p)
    if inferred:
        out.append("  ** [?] 標記的封裝是由 netlist/BOM **推論**出來的，不是"
                   "查證過的事實。請人工複核，或填入 ndd.json 的 part_package。**")
    if models and not fails and not pending and not inferred:
        out.append("  全部通過")
    return fails, pending, inferred


def run_audit(cfg, boards, models):
    """boards: {key: (Netlist, Bom)}。回傳供 review 清單使用的統計。"""
    stats = {"assert_pass": 0, "assert_fail": [], "role_bad": 0, "absent": {},
             "floating": {}, "parser_fail": [], "model_fail": [],
             "model_pending": [], "model_inferred": [], "bom_dups": {},
             "bom_ranges": {}, "ok": True}

    print("=" * 78)
    print("netlist / BOM 一致性稽核")
    print("=" * 78)

    print("\n[0] parser 自我驗證（獨立重數原始檔，確認沒有漏讀）")
    for k, (nl, _b) in boards.items():
        r = nl.selfcheck()
        if not r["ok"]:
            stats["parser_fail"].append(k)
        print("  %-4s [%s] parts %d/%d, signals %d/%d, pin token %d/%d, "
              "pinmap %d/%d, 重名 %d, 幽靈 refdes %d, 未歸類行 %d"
              % ("PASS" if r["ok"] else "FAIL", k, r["parts"][0], r["parts"][1],
                 r["nets"][0], r["nets"][1], r["pins"][0], r["pins"][1],
                 r["mapped"][0], r["mapped"][1], r["dup_signal"],
                 len(r["ghost"]), len(r["stray"])))
        if r["dup_pins"]:
            print("         !! 同一支腳出現在多條 net（pinmap 已丟失前值）：")
            for d in r["dup_pins"][:5]:
                print("            %s" % d)
        if r["stray"]:
            print("         未歸類: %s" % r["stray"][:3])
    if stats["parser_fail"]:
        print("  ** parser 自我驗證失敗，下游結論一律不成立。先修 parser。**")

    print("\n[1] 文件斷言逐條驗證")
    for a in cfg.get("assertions", []):
        b = a["board"]
        if b not in boards:
            continue
        nl, bom = boards[b]
        try:
            ok, actual = run_assertion(a, nl, bom, models)
        except Exception as exc:
            ok, actual = False, "檢查時例外: %s" % exc
        print("  %-4s [%s] %s" % ("PASS" if ok else "FAIL", b, a["desc"]))
        if ok:
            stats["assert_pass"] += 1
        else:
            stats["assert_fail"].append(a["desc"])
            print("           實際: %s" % (actual,))
    if not cfg.get("assertions"):
        print("  （尚未定義任何斷言 —— 文件寫到哪，斷言就要補到哪）")

    print("\n[1.5] 元件模型（pin-existence 與 package 解析）")
    out = []
    f, p, inf = model_check(cfg, boards, models, out)
    stats["model_fail"], stats["model_pending"] = f, p
    stats["model_inferred"] = inf
    print("\n".join(out))

    print("\n[2] refdes 命名規則 vs 實際佈件")
    out = []
    for rule in cfg.get("role_rules", []):
        if rule["board"] in boards:
            nl, bom = boards[rule["board"]]
            stats["role_bad"] += role_check(nl, bom, rule, out)
    print("\n".join(out) if out else "  （尚未定義命名規則）")

    print("\n[3] netlist vs BOM 對帳")
    for k, (nl, bom) in boards.items():
        actives = set(nl.actives())
        no_bom = [r for r in nl.parts if bom.of(r) is None]
        act = sorted([r for r in no_bom if r in actives], key=refkey)
        pas = [r for r in no_bom if nl.is_passive(r)]
        mech = [r for r in no_bom if nl.is_mech(r)]
        other = sorted([r for r in no_bom if r not in actives
                        and not nl.is_passive(r) and not nl.is_mech(r)], key=refkey)
        bom_only = sorted([r for r in bom.ref if r not in nl.parts], key=refkey)
        stats["absent"][k] = act
        stats["bom_dups"][k] = bom.dups
        stats["bom_ranges"][k] = bom.suspect_ranges
        print("  [%s] BOM 範圍: %s" % (k, bom.scope))
        print("       netlist 有 / BOM 無: active %d, passive %d, 機構 %d, 其他 %d"
              % (len(act), len(pas), len(mech), len(other)))
        if act:
            print("         -> active 未貼件 (DNI): %s" % ", ".join(act[:30]))
            if bom.scope == "smt_only":
                print("            （SMT BOM：連接器/測試點/手插件不在涵蓋範圍，"
                      "已分開列於下方）")
        if other:
            print("         -> 未分類，請人工判斷: %s" % ", ".join(other[:30]))
        if mech:
            print("         -> 機構/測試點 %d 顆：若 BOM 是 SMT BOM 則屬正常" % len(mech))
        print("       BOM 有 / netlist 無: %d %s"
              % (len(bom_only), ", ".join(bom_only[:15])))
        if bom.dups:
            print("       !! 重複 refdes %d 筆（後列不再靜默覆蓋前列）："
                  % len(bom.dups))
            for rd, rows in sorted(bom.dups.items())[:8]:
                print("          %-9s 列 %s" % (rd, ",".join(str(x) for x in rows)))
        if bom.suspect_ranges:
            print("       !! 疑似 refdes 範圍 %d 筆（預設**不展開**，"
                  "要展開請設 expand_ranges）：%s"
                  % (len(bom.suspect_ranges),
                     ", ".join("%s@列%s" % (c, r)
                               for c, r in bom.suspect_ranges[:6])))

    print("\n[4] 單腳（懸空）網路")
    for k, (nl, _b) in boards.items():
        sp = nl.single_pin_nets()
        stats["floating"][k] = sp
        owners = {}
        for n in sp:
            rd, p = nl.net(n)[0]
            owners.setdefault(nl.parts.get(rd, "?"), []).append("%s.%s" % (rd, p))
        print("  [%s] 共 %d 條" % (k, len(sp)))
        for fp, lst in sorted(owners.items(), key=lambda x: -len(x[1])):
            print("       %-34s %3d 支腳  e.g. %s" % (fp, len(lst), ", ".join(lst[:4])))

    stats["ok"] = not (stats["parser_fail"] or stats["assert_fail"]
                       or stats["model_fail"] or stats["role_bad"]
                       or any(stats["bom_dups"].values()))
    print("\n" + "-" * 78)
    print("稽核結果：%s" % ("PASS" if stats["ok"] else "**FAIL**"))
    if stats["model_pending"]:
        print("待辦 %d 項（不是錯誤，但也**不算通過**）：%s"
              % (len(stats["model_pending"]), "；".join(stats["model_pending"][:3])))
    if stats["model_inferred"]:
        print("[?] %d 項封裝是**推論**的，請人工複核："
              % len(stats["model_inferred"]))
        for x in stats["model_inferred"][:5]:
            print("    %s" % x)
    return stats
