#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""元件行為模型：**有向 transfer 邊**、實體 control、runtime 條件、功能影響。

════════════════════════════════════════════════════════════════════════════
三條鐵則：

1. **分類的單位是「邊」，不是「元件」。** 同一顆 IC 可以同時有可傳輸的邊、
   控制腳與狀態行為（移位暫存器就是），以元件為單位分類只能分錯。

2. **方向必須顯式。** 舊版的 `pairs` 是對稱的，於是單向緩衝器可以從 output
   逆走回 input，fanout 的兩個 output 可以藉 input 互通——後者是**憑空捏造
   一條不存在的路徑**。`direction` 沒有預設值。

3. **`always` 必須被正面證明。** 手寫 `condition: "always"` 是把未經查證的
   斷言凍結成資產。改為宣告「這條邊由哪些 gate 把關」（事實），通斷性質由
   netlist 實際接法推導（結論）。
════════════════════════════════════════════════════════════════════════════

⚠️ 任何模型都必須有 `verified_against`，且必須是真的翻過那份 datasheet 的那
   一頁。沒有的話 `load_models()` 拒絕載入。
"""
import io
import json
import os
import re

import ndd_confidence as C

# --- 合法值 -----------------------------------------------------------------
DIRECTIONS = ("forward", "bidirectional")
PIN_ROLES = ("GND", "PWR", "SIG")
# ⚠️ `pin_roles` 是這次改版的關鍵欄位：建模的人在 datasheet 上看到 VSS/VDD 是
#    哪幾支腳時順手記下來（成本趨近於零），工具就能**只用 netlist** 驗證
#    「這個封裝的腳位對不對得上這塊板」——不需要解析 datasheet 表格。
CONTROL_TYPES = ("enable", "reset", "address", "select", "mode",
                 "power", "clock", "trigger", "other")
MECHANISMS = ("strap", "runtime", "external", "unknown")
POLARITY_TYPES = ("enable", "reset")       # 只有這兩類可以有 high/low 極性

# 兩腳被動件一律導通（不需要 datasheet），且**確實**是雙向的。
TWO_PIN_FOOTPRINT_PREFIX = ("R_", "L_", "FB_", "Ferrite", "RES", "IND")


class ModelError(Exception):
    pass


def models_with_same_match(_m):
    return ()


# --------------------------------------------------------------- 載入驗證 --
def _fail(name, msg):
    raise ModelError("模型 `%s`：%s" % (name, msg))


def _validate(name, m):
    if "pairs" in m:
        _fail(name,
              "使用了已移除的 `pairs` schema。這是**破壞性遷移**，不做靜默轉換"
              "——舊 schema 的對稱性正是要修掉的錯誤，直接轉換會把錯誤帶進新 "
              "schema。請重新核對 datasheet，改寫成有向的 `transfer`："
              '\n  "transfer": [{"from": ["1"], "to": ["3"], '
              '"direction": "forward"}]')

    # 封裝判定改由 netlist/BOM 證據排名（ndd_package），不再解析 datasheet 表格。
    # `package` 是人填的標籤，工具不去「驗證它對不對」，只驗它與 netlist 一致。
    roles = m.get("pin_roles") or {}
    for pin, role in roles.items():
        if role not in PIN_ROLES:
            _fail(name, "pin_roles['%s'] 必須是 %s 之一（目前 %r）"
                        % (pin, "／".join(PIN_ROLES), role))
    if len(models_with_same_match(m)) and not roles and not m.get("ordering_suffix") \
            and not m.get("footprint_match"):
        pass    # 單一候選時不需要證據；多候選時由 ndd_package 判定並要求補充

    edges = m.get("transfer") or []
    if not m.get("verified_against") and not all(
            e.get("verified_against") for e in edges):
        _fail(name, "缺 `verified_against`（model 層預設或逐邊 override 擇一）。"
                    "腳位模型一律要翻過 datasheet 才能用，請填『檔名 + 頁碼 + "
                    "文件編號』")

    ctrl = m.get("control") or {}
    for pin, spec in ctrl.items():
        if not isinstance(spec, dict):
            _fail(name, "control['%s'] 必須是物件；舊版的字串註記已不再支援"
                        "（字串無法驅動 always/conditional 推導）" % pin)
        if spec.get("type") not in CONTROL_TYPES:
            _fail(name, "control['%s'].type 必須是 %s 之一"
                        % (pin, "／".join(CONTROL_TYPES)))
        if spec.get("mechanism") not in MECHANISMS:
            _fail(name, "control['%s'].mechanism 必須是 %s 之一"
                        % (pin, "／".join(MECHANISMS)))
        if spec.get("polarity") and spec["type"] not in POLARITY_TYPES:
            _fail(name, "control['%s'] 是 %s，不得指定 polarity"
                        "（address／多 bit select／參數控制沒有 high/low 之分）"
                        % (pin, spec["type"]))

    rt = m.get("runtime_conditions") or {}
    for edge in edges:
        d = edge.get("direction")
        if d not in DIRECTIONS:
            _fail(name, "transfer 邊缺少顯式 `direction`（必須是 %s）。"
                        "沒有預設值——任一方向當預設都會靜默把另一半元件模型錯"
                        % "／".join(DIRECTIONS))
        if not edge.get("from") or not edge.get("to"):
            _fail(name, "transfer 邊必須同時有 `from` 與 `to`")
        g = edge.get("gate")
        if g:
            keys = [k for k in ("all_of", "any_of") if k in g]
            if len(keys) != 1:
                _fail(name, "`gate` 必須顯式且只能是 `all_of` 或 `any_of` 其一，"
                            "不可依 list 順序或預設布林邏輯猜測")
            for item in g[keys[0]]:
                if item in rt:
                    continue
                spec = ctrl.get(item)
                if spec is None:
                    _fail(name, "gate 引用了未定義的 `%s`"
                                "（既不是 control pin 也不是 runtime_conditions）"
                                % item)
                if spec["type"] == "address":
                    _fail(name, "gate 不得引用 address 腳（`%s`）。位址決定"
                                "『是哪一顆』，不決定『通不通』；把它放進 gate "
                                "會讓固定位址被誤推成 always" % item)
    return m


def load_models(project_dir=None):
    """載入專案 `models.json` 並強制驗證。

    ⚠️ 本 skill **不內建 seed model**。初版帶的四個 seed 是 `pairs` schema、
       在新規格下無法載入；而通用型 skill 不應把任何特定
       料號當成預設知識。要用就在專案的 `models.json` 自行查證後加入。
    """
    models = {}
    if project_dir:
        p = os.path.join(project_dir, "models.json")
        if os.path.exists(p):
            with io.open(p, encoding="utf-8") as fh:
                raw = json.load(fh)
            for k, v in raw.items():
                models[k] = _validate(k, dict(v))
    return models


# ----------------------------------------------------------------- 選型 --
def _tokens(m):
    return [t for t in m.get("match", []) if t]


def select_model(models, footprint, pn="", package=None):
    """回傳 (name, model, caveats)。

    優先序（三階，不互斥）：① 精確 MPN → ② 唯一最長且具 token 邊界的 match
    → ③ 仍同分才報錯。舊版走 dict 順序取第一個子字串命中，等於讓種子模型與
    專案模型的優先權由**插入順序**決定，而且短 token 會誤中較長的料號。
    """
    pn_u = (pn or "").upper().strip()
    fp_u = (footprint or "").upper()
    hay = "%s|%s" % (fp_u, pn_u)

    exact = [(k, m) for k, m in models.items()
             if any(t.upper() == pn_u for t in _tokens(m)) and pn_u]
    if len(exact) == 1:
        return _package_filter(exact[0][0], exact[0][1], package)
    if len(exact) > 1:
        return None, None, ["model:ambiguous"]

    scored = []
    for k, m in models.items():
        best = 0
        for t in _tokens(m):
            tu = t.upper()
            # token 邊界：命中處前後不得是英數，否則 LM358 會誤中 LM3584
            for mt in re.finditer(re.escape(tu), hay):
                a, b = mt.start(), mt.end()
                pre = hay[a - 1] if a else ""
                post = hay[b] if b < len(hay) else ""
                if not pre.isalnum() and not post.isalnum():
                    best = max(best, len(tu))
        if best:
            scored.append((best, k, m))
    if not scored:
        return None, None, []
    top = max(s[0] for s in scored)
    winners = [(k, m) for n, k, m in scored if n == top]
    if len(winners) > 1:
        return None, None, ["model:ambiguous"]
    return _package_filter(winners[0][0], winners[0][1], package)


def _package_filter(name, m, package):
    """保留給舊呼叫點；封裝判定已移到 `ndd_package.resolve()`。

    這裡只做一件事：若呼叫端明確給了 package 而模型自報的 package 對不上，
    就拒絕——避免把 A 封裝的腳位套到 B 封裝上。
    """
    want = (m.get("package") or "").upper()
    if package and want:
        got = str(package).upper()
        if want not in got and got not in want:
            return None, None, ["package:conflict"]
    return name, m, []


# ------------------------------------------------------------- transfer --
def transfer_edges(m):
    """展開成 [(from_pin, to_pin, direction, edge), ...]。"""
    out = []
    for e in m.get("transfer") or []:
        for a in e["from"]:
            for b in e["to"]:
                out.append((str(a), str(b), e["direction"], e))
    return out


def transfer_for(models, footprint, pn="", npins=0, package=None):
    """回傳 (name, edges, caveats)。edges 為有向：只能由 from 走到 to。

    ⚠️ **帶阻斷級 caveat 時 edges 一律為 None** —— package 未解析／衝突／
       多重 match 的情況下不得生成正式 transfer edge，否則等於用未定案的
       腳位對應去產生看起來確定的路徑。
    """
    name, m, caveats = select_model(models, footprint, pn, package)
    blocking = [c for c in caveats if C.CAVEAT_CEILING.get(c) == C.UNKNOWN]
    if blocking:
        return name, None, caveats
    if m is not None:
        return name, transfer_edges(m), caveats
    if (footprint or "").startswith(TWO_PIN_FOOTPRINT_PREFIX) and npins == 2:
        # 兩腳被動件確實雙向，且不需要 datasheet
        return "2-pin passive", [("1", "2", "bidirectional", {}),
                                 ("2", "1", "bidirectional", {})], []
    return None, None, []


def outgoing(edges, pin):
    """由 `pin` 可以合法走到哪些腳。**forward 邊只能正向走。**"""
    out = []
    for a, b, direction, edge in edges:
        if a == pin:
            out.append((b, edge))
        elif b == pin and direction == "bidirectional":
            out.append((a, edge))
    return out


# --------------------------------------------------- pin-existence check --
def referenced_pins(m):
    """model 引用到的所有實體 pin label（transfer 端點 + gate 的實體腳）。"""
    pins = set()
    for e in m.get("transfer") or []:
        pins.update(str(x) for x in e.get("from", []))
        pins.update(str(x) for x in e.get("to", []))
    rt = m.get("runtime_conditions") or {}
    ctrl = m.get("control") or {}
    for e in m.get("transfer") or []:
        g = e.get("gate") or {}
        for key in ("all_of", "any_of"):
            for item in g.get(key, []):
                if item not in rt and item in ctrl:
                    pins.add(str(item))
    return pins


def missing_pins(m, observed):
    """⚠️ 這是 **pin-existence sanity check，不是 package 驗證**。

    兩個封裝同為 1–N 而腳位定義不同時，它必然通過。它能抓的是打錯、以及照抄
    了不同衍生型號的 model。名稱若叫「封裝驗證」，名字本身就在製造假保證。
    """
    return sorted(referenced_pins(m) - set(observed))


# ------------------------------------------------------------ gating 推導 --
def derive_gating(m, edge, rail_fn):
    """回傳 (gating, notes)。

    **`always` 必須被正面證明**：所有必要的實體 gate 都要由 netlist 證實接在
    正確的有效狀態。runtime／外部驅動／浮接／未知一律降為 conditional 或
    unknown——反過來就會靜默升級確定性。
    """
    g = edge.get("gate") or {}
    key = "all_of" if "all_of" in g else ("any_of" if "any_of" in g else None)
    if not key or not g[key]:
        return C.ALWAYS, []                     # 無 gate = 恆通

    rt = m.get("runtime_conditions") or {}
    ctrl = m.get("control") or {}
    results, notes = [], []
    for item in g[key]:
        if item in rt:
            results.append(C.CONDITIONAL)
            notes.append("%s=runtime_register" % item)
            continue
        spec = ctrl.get(item, {})
        mech = spec.get("mechanism")
        if mech in ("runtime", "external"):
            results.append(C.CONDITIONAL)
            notes.append("%s=%s" % (item, mech))
            continue
        if mech != "strap":
            results.append(C.UNKNOWN)
            notes.append("%s=mechanism_unknown" % item)
            continue
        state = rail_fn(item, spec.get("polarity"))
        results.append(state[0])
        notes.append("%s=%s" % (item, state[1]))

    if key == "all_of":
        return C.worst_gating(results), notes
    # any_of：只要有一條被證實恆通即恆通
    return (C.ALWAYS if C.ALWAYS in results
            else C.worst_gating(results)), notes


def control_influences(m):
    return m.get("control_influence") or []


def i2c_addr(models, nl, refdes, pn="", package=None):
    """由 address 腳的實際接法反推 I2C 位址。

    ⚠️ 位址只回答「是哪一顆」，**不回答「通不通」**——所以它不參與 gating 推導
       （`_validate` 會拒絕把 address 腳放進 gate）。
    """
    _n, m, caveats = select_model(models, nl.parts.get(refdes, ""), pn, package)
    if not m or "addr" not in m:
        return None, caveats
    spec = m["addr"]
    ctrl = m.get("control") or {}
    addr = spec["base"]
    for pin, weight in spec["bits"].items():
        if ctrl.get(pin, {}).get("type") != "address":
            raise ModelError("addr.bits 引用的 `%s` 未宣告為 address 腳" % pin)
        net = (nl.pin_net(refdes, pin) or "").upper()
        if net.startswith(("VDD", "VCC", "+")):
            addr += weight
    return addr, caveats


def describe(models):
    lines = []
    for name, m in sorted(models.items()):
        edges = transfer_edges(m)
        roles = m.get("pin_roles") or {}
        lines.append("%-16s %-14s %2d 條 transfer  package=%s  pin_roles=%s  查證: %s"
                     % (name, m.get("kind", "?"), len(edges),
                        m.get("package", "-"),
                        ",".join("%s:%s" % kv for kv in sorted(roles.items()))
                        or "（無，多封裝時無法判別）",
                        m.get("verified_against", "-")))
        for a, b, d, _e in edges:
            lines.append("        %s %s %s" % (
                a, "->" if d == "forward" else "<->", b))
    return "\n".join(lines) or "（尚未定義任何模型）"
