#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可信度與 caveat 的**唯一**定義處。

════════════════════════════════════════════════════════════════════════════
兩個獨立的軸，不可合併成一個排序：

  confidence —— 這條邊的**證據**有多強      confirmed > caveated > unknown
  gating     —— 這條邊**已查證**的通斷性質  always > conditional > unknown

  混成一軸會產生反直覺的結果：一條 datasheet 查證完整、package 已鎖定的
  runtime-gated 邊，證據品質其實是 confirmed；若把它排在「package 未佐證但
  恆通」之下，等於鼓勵使用者為了拉高 confidence 而迴避正確標註的 conditional。
════════════════════════════════════════════════════════════════════════════

⚠️ **禁止用字串排序比較這些值**（`conditional` < `unknown` 的字典序毫無意義）。
   一律用 `worst()` / `rank()`。
"""

# --- confidence：證據品質 ---------------------------------------------------
CONFIRMED = "confirmed"
CAVEATED = "caveated"
UNKNOWN = "unknown"
CONFIDENCE_ORDER = [CONFIRMED, CAVEATED, UNKNOWN]

# --- gating：已查證的通斷性質 -----------------------------------------------
ALWAYS = "always"
CONDITIONAL = "conditional"
GATING_ORDER = [ALWAYS, CONDITIONAL, UNKNOWN]

# --- caveat -> confidence 上限 ----------------------------------------------
# 每個 caveat 只能**降低**一條邊的證據品質，不能提高。新增 caveat 時務必在此
# 登錄，否則它不會影響 confidence，等於標了卻沒有效果。
CAVEAT_CEILING = {
    "mate:unapproved":            CAVEATED,
    "mate:missing":               UNKNOWN,
    "package:inferred":           CAVEATED,   # 由 netlist/BOM 推出，非人工確認
    "package:unresolved":         UNKNOWN,
    "package:conflict":           UNKNOWN,
    "model:ambiguous":            UNKNOWN,
    "model:pin_absent":           UNKNOWN,
    "endpoint:unclassified":      CAVEATED,
    "bom:ambiguous":              CAVEATED,
    "bom_scope:insufficient":     CAVEATED,
}


def rank(value, order):
    try:
        return order.index(value)
    except ValueError:
        return len(order) - 1          # 未知值一律視為最差


def worst(values, order):
    """取最差者。空集合視為最好（沒有任何降級因素）。"""
    vals = [v for v in values if v]
    if not vals:
        return order[0]
    return max(vals, key=lambda v: rank(v, order))


def confidence_of(caveats):
    """由 caveat 集合推出這條邊/路徑的證據品質。"""
    return worst([CAVEAT_CEILING.get(c, UNKNOWN) for c in caveats],
                 CONFIDENCE_ORDER)


def worst_confidence(values):
    return worst(values, CONFIDENCE_ORDER)


def worst_gating(values):
    return worst(values, GATING_ORDER)


def render(caveats):
    """輸出前**一定要排序** —— set 迭代順序不定會讓每次重跑產生假 diff，
    而這個工具的核心工作流就是『改版後重跑比對』。"""
    return ";".join(sorted(set(c for c in caveats if c)))


def unknown_caveats(caveats):
    """回傳其中屬於『阻斷級』的 caveat（confidence 直接掉到 unknown）。"""
    return sorted(c for c in caveats
                  if CAVEAT_CEILING.get(c, UNKNOWN) == UNKNOWN)
