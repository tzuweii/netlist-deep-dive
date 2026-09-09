#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""封裝／腳位對應的判定 —— **只用 netlist 與 BOM**，不解析 datasheet 表格。

════════════════════════════════════════════════════════════════════════════
為什麼不從 PDF 推：每家 datasheet 的腳位表排版都不同（腳註標記、跨行儲存格、
文字層把兩個腳號併成一個），要通用地「看懂」表格是無底洞，而且一列錯位就讓
整張表作廢。實測 NXP 一份 16 腳的表就同時踩到三種。

改用這個工具**已經有的**機制：`mate` 的枚舉排名。三個獨立證據來源，全部來自
netlist 與 BOM：

  1. 拓樸一致性 `[N]` —— 模型宣告的電源/接地腳，實際是不是接在電源/地上
  2. 料號後綴     `[B]` —— 訂購碼的封裝後綴（PCA9554B**PW** = TSSOP…）
  3. footprint    `[N]` —— layout 選的實體 footprint 名稱

判別力最強的是第 1 項：候選封裝之間差最多的通常就是電源腳位置。而且只需要
知道**那幾支腳**，不需要整張表。
════════════════════════════════════════════════════════════════════════════

⚠️ **推論出來的一律標 `[?]`。** 自動定案只在「唯一勝出 + 零矛盾 + 至少兩個
   獨立來源一致」時發生，且仍帶 `package:inferred` caveat；否則列出證據並
   要求使用者補充，**不得替使用者假設**。
"""
import re

import ndd_confidence as C

# 判定結果
USER_CONFIRMED = "user_confirmed"      # ndd.json 明確宣告
INFERRED = "inferred"                  # 由 netlist/BOM 證據推出，仍標 [?]
SINGLE_CANDIDATE = "single_candidate"   # 該料號只有一個模型，無從選錯
UNRESOLVED = "unresolved_pending_user"  # 證據不足，要問人
CONFLICT = "conflict"                   # 宣告與 netlist 矛盾

RESOLVED_OK = (USER_CONFIRMED, SINGLE_CANDIDATE)
# INFERRED 可用，但會掛 caveat 並列進 REVIEW —— 它是推論不是事實。

ROLE_VALUES = ("GND", "PWR", "SIG")
MIN_MARGIN = 2                          # 比照 mate：margin 太小就是證據薄弱


def role_of_net(cls_fn, net):
    """把 net 的類別壓成三種角色。`cls_fn` 用 Fabric.cls，跨模組共用同一套。"""
    c = cls_fn(net)
    if c is None:
        return None
    if c == "GND":
        return "GND"
    if c.startswith("PWR"):
        return "PWR"
    return "SIG"


def score_topology(model, nl, refdes, cls_fn):
    """模型宣告的 `pin_roles` 對上 netlist 實況。回傳 (矛盾數, 相符數, 明細)。

    這是**最有判別力**的一項：兩個封裝的 VSS/VDD 幾乎不會落在同一支腳，
    而 netlist 直接就知道哪支腳接地、哪支腳接電源。
    """
    roles = model.get("pin_roles") or {}
    bad = good = 0
    detail = []
    for pin, want in sorted(roles.items()):
        net = nl.pin_net(refdes, str(pin))
        got = role_of_net(cls_fn, net)
        if got is None:
            # ⚠️ 訊號腳可以 NC，**電源/接地腳不會**。宣告的 VSS/VDD 在 netlist
            #    上完全沒接，代表這個封裝的腳位對不上這塊板——這是矛盾，不是
            #    「資料不足」。這條規則正是拓樸判別力的主要來源。
            if want in ("GND", "PWR"):
                bad += 1
                detail.append("%s:預期%s但未接✗" % (pin, want))
            else:
                detail.append("%s:未接(%s?)" % (pin, want))
            continue
        if got == want:
            good += 1
            detail.append("%s:%s✓" % (pin, want))
        else:
            bad += 1
            detail.append("%s:預期%s實際%s✗" % (pin, want, got))
    return bad, good, detail


def score_suffix(model, pn):
    """訂購碼後綴。datasheet 的腳位圖下方通常就印著各封裝的訂購碼。"""
    sufs = model.get("ordering_suffix") or []
    if not sufs or not pn:
        return None
    up = pn.upper().replace("-", "").split(",")[0]
    for s in sufs:
        if up.endswith(s.upper()):
            return True
    return False


def score_footprint(model, footprint):
    """layout 選的實體 footprint 名稱。命中即為強證據，沒命中不算反證
    （footprint 命名習慣因專案而異）。"""
    pats = model.get("footprint_match") or []
    if not pats or not footprint:
        return None
    up = footprint.upper()
    for p in pats:
        if re.search(p.upper(), up):
            return True
    return False


def rank(candidates, nl, bom, refdes, cls_fn):
    """對同一料號的多個封裝候選排名。

    candidates: [(name, model), ...]
    回傳 [(矛盾, -相符, -佐證數, name, model, 證據字串), ...]，已排序。
    """
    fp = nl.parts.get(refdes, "")
    pn = bom.pn(refdes)
    pn = "" if not isinstance(pn, str) else pn
    rows = []
    for name, m in candidates:
        bad, good, detail = score_topology(m, nl, refdes, cls_fn)
        suf = score_suffix(m, pn)
        fpm = score_footprint(m, fp)
        corrob = sum(1 for x in (suf, fpm) if x is True)
        ev = []
        if detail:
            ev.append("拓樸[N] %s" % " ".join(detail[:6]))
        if suf is not None:
            ev.append("料號後綴[B] %s" % ("✓" if suf else "✗"))
        if fpm is not None:
            ev.append("footprint[N] %s" % ("✓" if fpm else "✗"))
        rows.append((bad, -good, -corrob, name, m, "；".join(ev) or "（無證據）"))
    rows.sort(key=lambda r: (r[0], r[1], r[2], r[3]))
    return rows


def resolve(models, nl, bom, board, refdes, cls_fn, declared=None):
    """判定該 refdes 該用哪個模型。

    回傳 dict：`status` / `name` / `model` / `caveats` / `evidence` / `margin`。

    優先序：
      ① `ndd.json` 明確宣告        -> user_confirmed（人講的最大）
      ② 只有一個候選模型           -> single_candidate（無從選錯）
      ③ 排名唯一勝出且證據足夠     -> inferred（**仍標 [?]**）
      ④ 其餘                       -> unresolved_pending_user（問人）
    """
    pn = bom.pn(refdes)
    pn = "" if not isinstance(pn, str) else pn
    fp = nl.parts.get(refdes, "")
    cands = _candidates_for(models, fp, pn)
    if not cands:
        return dict(status=None, name=None, model=None, caveats=[],
                    evidence="", margin=0)

    if declared:
        hit = [(n, m) for n, m in cands
               if _pkg_matches(m.get("package"), declared)]
        if not hit:
            return dict(status=CONFLICT, name=None, model=None,
                        caveats=["package:conflict"], margin=0,
                        evidence="宣告的 %s 沒有對應的模型（候選：%s）"
                                 % (declared, "／".join(
                                     m.get("package") or n for n, m in cands)))
        name, m = hit[0]
        bad, _g, detail = score_topology(m, nl, refdes, cls_fn)
        if bad:
            # 人講的最大，但**矛盾要講出來**，不能默默照單全收
            return dict(status=CONFLICT, name=name, model=m, margin=0,
                        caveats=["package:conflict"],
                        evidence="宣告 %s，但 netlist 有 %d 支腳矛盾：%s"
                                 % (declared, bad, " ".join(detail[:6])))
        return dict(status=USER_CONFIRMED, name=name, model=m, margin=0,
                    caveats=[], evidence="ndd.json 宣告 %s" % declared)

    if len(cands) == 1:
        name, m = cands[0]
        bad, _g, detail = score_topology(m, nl, refdes, cls_fn)
        if bad:
            return dict(status=CONFLICT, name=name, model=m, margin=0,
                        caveats=["package:conflict"],
                        evidence="唯一候選但 netlist 有 %d 支腳矛盾：%s"
                                 % (bad, " ".join(detail[:6])))
        return dict(status=SINGLE_CANDIDATE, name=name, model=m, margin=0,
                    caveats=[], evidence="該料號只有一個模型" +
                    ("；拓樸相符 %s" % " ".join(detail[:6]) if detail else ""))

    rows = rank(cands, nl, bom, refdes, cls_fn)
    best, second = rows[0], rows[1]
    margin = (best[1] * -1) - (second[1] * -1) + (second[0] - best[0])
    corrob = -best[2]
    ok = (best[0] == 0 and                      # 零矛盾
          (second[0] > 0 or margin >= MIN_MARGIN) and   # 唯一勝出
          corrob >= 1)                          # 至少一個獨立來源佐證
    if ok:
        return dict(status=INFERRED, name=best[3], model=best[4],
                    caveats=["package:inferred"], margin=margin,
                    evidence=best[5])
    return dict(status=UNRESOLVED, name=None, model=None, margin=margin,
                caveats=["package:unresolved"],
                evidence="候選 %s；最佳 %s（%s）" % (
                    "／".join(r[3] for r in rows), best[3], best[5]))


def _pkg_matches(want, declared):
    if not want or not declared:
        return False
    a, b = str(want).upper(), str(declared).upper()
    return a in b or b in a


def _mpn_matches(token, mpn):
    """料號比對：允許**訂購碼後綴**，但不允許不同料號互撞。

    `PCA9554B` 應該對上 `PCA9554BPW`（`PW` 是封裝/包裝碼），
    但 `LM358` **不可**對上 `LM3584`（`4` 是另一顆料）。

    判準：多出來的部分必須以**字母**開頭。訂購碼後綴一律是字母段，
    而料號的延伸幾乎都是數字。
    """
    t, m = token.upper(), (mpn or "").upper()
    if not t or not m:
        return False
    if t == m:
        return True
    if m.startswith(t):
        rest = m[len(t):].lstrip("-_")
        return bool(rest) and rest[0].isalpha()
    return False


def _candidates_for(models, footprint, pn):
    """同一料號可能有多個封裝版本的模型；全部收進來讓證據去分勝負。"""
    pn_u = (pn or "").upper().strip().split(",")[0]
    out = [(k, m) for k, m in models.items()
           if any(_mpn_matches(t, pn_u) for t in m.get("match", []))]
    if out:
        return sorted(out)
    # 退而求其次：比對 footprint 字串（layout 命名有時直接寫料號）
    fp = (footprint or "").upper()
    for k, m in sorted(models.items()):
        for t in m.get("match", []):
            tu = t.upper()
            i = fp.find(tu)
            if i >= 0:
                pre = fp[i - 1] if i else ""
                post = fp[i + len(tu)] if i + len(tu) < len(fp) else ""
                if not pre.isalnum() and not post.isalnum():
                    out.append((k, m))
                    break
    return sorted(out)


def describe(res):
    """給 audit / REVIEW 用的一行說明。推論一律帶 [?]。"""
    if res["status"] is None:
        return "（無模型）"
    tag = {USER_CONFIRMED: "[B/N 已宣告]", SINGLE_CANDIDATE: "[N]",
           INFERRED: "[?] 推論", UNRESOLVED: "[?] 待補", CONFLICT: "!! 矛盾"}
    return "%s %s%s：%s" % (tag.get(res["status"], "?"), res["name"] or "-",
                           ("（margin %d）" % res["margin"]) if res["margin"] else "",
                           res["evidence"])
