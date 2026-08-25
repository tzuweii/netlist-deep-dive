#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""稽核引擎：把文件裡的主張變成可執行的斷言，跑一次就知道文件還對不對。

設計取向：**一次性稽核，不是持續維護的回歸測試**。硬體設計改版頻率通常極低，
跑完把結論寫回文件即可；但每次改版或換專案要能一鍵重跑。

四段檢查：
  [0] parser 自我驗證 —— 最底層，parser 漏讀則上面全部不成立
  [1] 文件斷言       —— 從文件抽出的具體主張，宣告式定義在 ndd.json
  [2] 命名規則展開   —— ⚠️ 通則會有例外，逐顆展開比對才抓得到
  [3] netlist vs BOM —— DNI 對帳
  [4] 懸空網路       —— 單腳網路
"""
import re

from ndd_models import i2c_addr
from ndd_pads import refkey


def _expand_pins(spec):
    if isinstance(spec, list):
        return [str(x) for x in spec]
    m = re.match(r"^(\d+)\s*-\s*(\d+)$", str(spec))
    if m:
        return [str(i) for i in range(int(m.group(1)), int(m.group(2)) + 1)]
    return [str(spec)]


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
        got = bom.value(a["refdes"]) or ""
        return a["contains"].lower() in got.replace(" ", "").lower(), got

    if k == "not_stuffed":
        rds = a["refdes"] if isinstance(a["refdes"], list) else [a["refdes"]]
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
        got = i2c_addr(models, nl, a["refdes"], bom.pn(a["refdes"]) or "")
        want = int(a["expect"], 16) if isinstance(a["expect"], str) else a["expect"]
        return got == want, (hex(got) if got is not None else "無法判定（模型未定義 addr）")

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
            got = bom.pn(rd) or "(不在 BOM -> DNI)"
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


def run_audit(cfg, boards, models):
    """boards: {key: (Netlist, Bom)}。回傳供 review 清單使用的統計。"""
    stats = {"assert_pass": 0, "assert_fail": [], "role_bad": 0, "dni": {},
             "floating": {}, "parser_fail": []}

    print("=" * 78)
    print("netlist / BOM 一致性稽核")
    print("=" * 78)

    print("\n[0] parser 自我驗證（獨立重數原始檔，確認沒有漏讀）")
    for k, (nl, _b) in boards.items():
        r = nl.selfcheck()
        if not r["ok"]:
            stats["parser_fail"].append(k)
        print("  %-4s [%s] parts %d/%d, signals %d/%d, pin token %d/%d, "
              "重名 %d, 幽靈 refdes %d, 未歸類行 %d"
              % ("PASS" if r["ok"] else "FAIL", k, r["parts"][0], r["parts"][1],
                 r["nets"][0], r["nets"][1], r["pins"][0], r["pins"][1],
                 r["dup_signal"], len(r["ghost"]), len(r["stray"])))
        if r["stray"]:
            print("         未歸類: %s" % r["stray"][:3])

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
        stats["dni"][k] = act
        print("  [%s] BOM 類型: %s" % (k, bom.kind))
        print("       netlist 有 / BOM 無: active %d, passive %d, 機構 %d, 其他 %d"
              % (len(act), len(pas), len(mech), len(other)))
        if act:
            print("         -> active 未貼件 (真 DNI): %s" % ", ".join(act[:30]))
        if other:
            print("         -> 未分類，請人工判斷: %s" % ", ".join(other[:30]))
        if mech:
            print("         -> 機構/測試點 %d 顆：若 BOM 是 SMT BOM 則屬正常" % len(mech))
        print("       BOM 有 / netlist 無: %d %s"
              % (len(bom_only), ", ".join(bom_only[:15])))

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
    return stats
