#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""元件「內部導通模型」庫 —— 追跡訊號鏈時，走到一顆 IC 要知道從哪支腳出去。

════════════════════════════════════════════════════════════════════════════
⚠️ 本檔的鐵則：**任何模型都必須有 `verified_against`，且必須是真的翻過那份
   datasheet 的那一頁。憑記憶寫腳位是這整套工具最容易出錯、也最難察覺的地方**
   ——模型錯了，追出來的訊號鏈會看起來完全合理，但整張表是錯的。
   沒有 `verified_against` 的模型，`load_models()` 會拒絕載入。
════════════════════════════════════════════════════════════════════════════

模型欄位：
    match             : list[str]  比對 footprint 或 BOM 料號的子字串（大小寫不拘）
    kind              : "switch" | "buffer" | "mux" | "fanout" | "passthru"
    pairs             : list[[pin_a, pin_b]]  雙向導通的腳對
    control           : {pin: 名稱}  OE / 選通腳，僅供人閱讀，不參與走圖
    note              : str        走圖語意上的但書（例如 mux 是「軟體可打通」）
    verified_against  : str        datasheet 檔名 + 頁碼 + 文件編號
    verified_on       : str        查證日期
"""
import io
import json
import os

# ---------------------------------------------------------------------------
# 種子模型：以下三筆都是逐頁翻過 datasheet 確認的，可直接沿用。
# 換專案時若用到別的料號，**必須自己查證後再加**，不要照抄相近型號。
# ---------------------------------------------------------------------------
SEED_MODELS = {
    "SN74CBTLV3126": {
        "match": ["SN74CBTLV3126"],
        "kind": "switch",
        "pairs": [["2", "3"], ["5", "6"], ["8", "9"], ["11", "12"]],
        "control": {"1": "1OE", "4": "2OE", "10": "3OE", "13": "4OE"},
        "note": "FET bus switch，OE 高態導通。OE 拉低時 S/D 高阻抗，"
                "且內部無輸入緩衝器，因此未使用通道的 S/D 懸空不會造成貫穿電流。",
        "verified_against": "sn74cbtlv3126.pdf p.3 Table 4-1（TI SCDS038N）",
        "verified_on": "2026-08-25",
    },
    "SN74HCS244": {
        "match": ["SN74HCS244"],
        "kind": "buffer",
        "pairs": [["2", "18"], ["4", "16"], ["6", "14"], ["8", "12"],
                  ["11", "9"], ["13", "7"], ["15", "5"], ["17", "3"]],
        "control": {"1": "1OE (低態致能)", "19": "2OE (低態致能)"},
        "note": "八路緩衝器，兩個 bank 各四路。OE 為**低態**致能，接 GND = 永遠致能。",
        "verified_against": "sn74hcs244-q1.pdf p.3 Pin Functions（TI SCLS821C）",
        "verified_on": "2026-08-25",
    },
    "PI49FCT3807": {
        "match": ["PI49FCT3807"],
        "kind": "fanout",
        # A (輸入) = pin1；B0..B9 (輸出) = 3,5,7,9,11,12,14,16,18,19
        "pairs": [["1", p] for p in ["3", "5", "7", "9", "11",
                                     "12", "14", "16", "18", "19"]],
        "control": {},
        "note": "1-to-10 clock driver，無致能腳。⚠️ 輸出腳號不連續（11 之後跳到 12），"
                "不要用等差級數推。",
        "verified_against": "pi49fct3807.pdf p.2 Pin Description（Diodes DS43192 Rev 2-2）",
        "verified_on": "2026-08-25",
    },
    "PCA9547": {
        "match": ["PCA9547"],
        "kind": "mux",
        # 上游 SCL=19 / SDA=20 對各通道；HVQFN24 腳位
        "pairs": ([["20", p] for p in ["1", "3", "5", "7", "10", "12", "14", "16"]]
                  + [["19", p] for p in ["2", "4", "6", "8", "11", "13", "15", "17"]]),
        "control": {"18": "A2", "22": "A0", "23": "A1", "24": "RESET (低態)"},
        "addr": {"base": 112, "bits": {"18": 4, "23": 2, "22": 1}},   # 0x70
        "note": "8 通道 I2C mux。走圖時把 8 個通道都視為可通，所以追出來的是"
                "『軟體有可能打通的路徑』，不是同一瞬間的實際連線。"
                "⚠️ A0/A1/A2 在部分符號裡沒有命名，會被畫成一般電源/接地腳，"
                "看起來就像位址無從查起——實際上查 datasheet 腳位表就有。",
        "verified_against": "PCA9547.pdf p.5 Table 3 HVQFN24 欄（NXP Rev.4）",
        "verified_on": "2026-08-25",
    },
}

# 兩腳被動件一律導通（不需要 datasheet）
TWO_PIN_FOOTPRINT_PREFIX = ("R_", "L_", "FB_", "Ferrite", "RES", "IND")


class ModelError(Exception):
    pass


def load_models(project_dir=None):
    """載入種子模型 + 專案自訂 `models.json`，並強制檢查 verified_against。"""
    models = {k: dict(v) for k, v in SEED_MODELS.items()}
    if project_dir:
        p = os.path.join(project_dir, "models.json")
        if os.path.exists(p):
            with io.open(p, encoding="utf-8") as fh:
                for k, v in json.load(fh).items():
                    models[k] = v
    bad = [k for k, v in models.items() if not v.get("verified_against")]
    if bad:
        raise ModelError(
            "以下模型沒有 `verified_against`，拒絕載入：%s\n"
            "腳位模型一律要翻過 datasheet 才能用。請補上『檔名 + 頁碼 + 文件編號』，"
            "或先用 `ndd.py datasheets` 把 datasheet 抓下來再查。" % ", ".join(bad))
    return models


def match_model(models, footprint, pn=""):
    hay = ("%s|%s" % (footprint or "", pn or "")).upper()
    for name, m in models.items():
        for token in m["match"]:
            if token.upper() in hay:
                return name, m
    return None, None


def pairs_for(models, footprint, pn="", npins=0):
    name, m = match_model(models, footprint, pn)
    if m:
        return name, [tuple(x) for x in m["pairs"]]
    if (footprint or "").startswith(TWO_PIN_FOOTPRINT_PREFIX) and npins == 2:
        return "2-pin passive", [("1", "2")]
    return None, None


def i2c_addr(models, nl, refdes, pn=""):
    """依模型的 addr 定義，由位址腳實際接 VDD/GND 反推 I2C 位址。"""
    name, m = match_model(models, nl.parts.get(refdes, ""), pn)
    if not m or "addr" not in m:
        return None
    spec = m["addr"]
    addr = spec["base"]
    for pin, weight in spec["bits"].items():
        net = (nl.pin_net(refdes, pin) or "").upper()
        if net.startswith(("VDD", "VCC", "+")):
            addr += weight
    return addr


def describe(models):
    lines = []
    for name, m in sorted(models.items()):
        lines.append("%-16s %-9s %2d 組導通  查證: %s"
                     % (name, m["kind"], len(m["pairs"]), m["verified_against"]))
    return "\n".join(lines)


if __name__ == "__main__":
    print(describe(load_models()))
