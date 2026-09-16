#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""零件分類：這顆是 IC、被動件、連接器，還是機構件？

**為什麼需要這個**：`coverage` 原本把每一顆 active 平等列出，於是屏蔽罩、
螺帽跟 FPGA、ADC 混在同一張表，每一列都掛著「待查 datasheet」。那不是清單，
那是雜訊——真正該先查的東西被埋掉了。

**分類的權威來源不是 refdes 前綴，是料號。** 前綴是各家自己的習慣（實測某
專案用 `ME` 標機構件、`OPEN` 標預留位置），寫死進通用表就是在遷就單一專案，
換一塊板又要補規則。公司的料件資料庫（CIS）本來就有一套綁在料號上的分類
體系，那才是權威——實測 6 塊板 1509 顆 active，光靠料號查表命中 93.6%。

所以順位是：

    1. `ndd.json` 的 `part_class` 覆寫   —— 人講的最大
    2. CIS 快照查表（料號）              —— 事實
    3. refdes 前綴通用表                 —— **推論**，只在前兩者查不到時用
    4. 都沒有 -> `unrecognized`          —— **攤出來等人確認，不猜**

⚠️ 第 2 與第 3 順位的可信度不同，**呼叫端必須把 `source` 一起帶出去**：
   CIS 命中是 `[B]` 等級的事實，前綴推出來的是 `[?]`。混為一談就等於把推論
   洗成事實。

沒有 CIS 快照的機器仍然能跑（退回前綴表）——快照是**選用加速器，不是前提**，
與 `models.json` 的定位一致。
"""
from __future__ import print_function

import csv
import io
import os
import re

# 分類 -> 是否與訊號完整性有關（決定要不要排進「待查 datasheet」）
SIGNAL_RELEVANT = {
    "ic": True,
    "semiconductor_discrete": True,
    "crystal": True,
    "filter": True,
    "rf": True,
    "sensor": True,
    "switch": True,
    "relay": True,
    "transformer": True,
    "isolator": True,
    "optoelectronic": True,
    "power_module": True,
    "circuit_protection": True,
    "passive": False,
    "connector": False,
    "cable": False,
    "test_point": False,
    "mechanical": False,
    "fiducial": False,
    "pcb": False,
    "battery": False,
    "dev_board": False,
    "instrument": False,
    "placeholder": False,      # 刻意保留的未貼位置（OPEN_xxxx）
}

# CIS 大類代碼 -> 分類。代碼是公司自己的，穩定；名稱只拿來給人看。
CIS_CATEGORY = {
    "1E01": "cable", "3E01": "cable", "1E22": "cable",
    "1E02": "passive", "1E14": "passive", "1E08": "passive",
    "1E12": "passive",
    "1E03": "circuit_protection",
    "1E04": "connector",
    "1E05": "crystal",
    "1E06": "semiconductor_discrete",
    "1E07": "filter",
    "1E09": "ic", "1EA0": "ic", "3EA0": "ic",
    "1E10": "isolator",
    "1E11": "optoelectronic",
    "1E13": "relay",
    "1E15": "rf",
    "1E16": "sensor",
    "1E17": "mechanical",
    "1E18": "switch",
    "1E20": "instrument",
    "1E21": "power_module",
    "1E23": "dev_board",
    "1E24": "mechanical",          # Non Soldered Parts
    "1E25": "battery",
    "1EA1": "pcb",
    "1M": "mechanical",            # 1M_ME
}

# refdes 前綴後援表。**只收真正跨公司通用的**——`ME`、`FUSE`、`OPEN`、
# `Coupler` 這類是某家公司的習慣，寫進來就是遷就單一專案，一律留給
# `part_class` 用一行整批歸類。
PREFIX_CATEGORY = {
    "U": "ic",
    "Q": "semiconductor_discrete", "D": "semiconductor_discrete",
    "R": "passive", "C": "passive", "L": "passive", "FB": "passive",
    "X": "crystal", "Y": "crystal",
    "J": "connector",
    "TP": "test_point",
    "H": "mechanical", "MH": "mechanical",
    "FM": "fiducial",
    "F": "circuit_protection",
    "S": "switch",
    "K": "relay",
    "T": "transformer",
    "W": "cable",
}

UNRECOGNIZED = "unrecognized"
# 名字長這樣的料號＝刻意保留的未貼位置，不是缺分類。
_PLACEHOLDER_RX = re.compile(r"^(OPEN|NC|DNP|DNI|NOSTUFF)[-_ ]?", re.I)

SRC_OVERRIDE, SRC_CIS, SRC_PREFIX, SRC_NONE = "override", "cis", "prefix", "none"


def prefix_of(refdes):
    """refdes 的字母前綴。`U1036` -> `U`、`ME12` -> `ME`、`OPEN3` -> `OPEN`。"""
    m = re.match(r"^([A-Za-z_]+)", (refdes or "").strip())
    return m.group(1).upper() if m else ""


def _norm(s):
    return (s or "").strip().upper()


class CisIndex(object):
    """CIS 快照（料號 -> 分類）。檔案不在就是空的，呼叫端不必分兩套路。"""

    def __init__(self, path=None):
        self.path = path
        self.by_pn = {}
        self.rows = 0
        if not path or not os.path.exists(path):
            return
        with io.open(path, encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                self.rows += 1
                # 兩種料號都要收：BOM 上寫的是原廠料號還是公司內部編號不一定。
                for col in ("Manufacturer_PN", "Number"):
                    k = _norm(r.get(col))
                    if k:
                        self.by_pn.setdefault(k, r)

    def __len__(self):
        return len(self.by_pn)

    def lookup(self, pn):
        """回傳 (分類, 大類代碼, 細類路徑) 或 (None, "", "")。

        ⚠️ **只做去空白＋轉大寫，不做模糊比對。** 比不到就是比不到，交給下一
           順位——模糊比對會把「查錯了」偽裝成「查到了」，而且沒有症狀。
        """
        r = self.by_pn.get(_norm(pn))
        if not r:
            return None, "", ""
        path = r.get("Part_Type_CIS") or ""
        top = path.split("\\")[0]
        code = top.split("_")[0] if top else ""
        cat = CIS_CATEGORY.get(code)
        if cat is None:
            return None, code, path
        return cat, code, path


def classify(refdes, pn, board=None, overrides=None, cis=None):
    """回傳 (分類, 來源)。來源決定這筆能不能當事實引用。"""
    ov = overrides or {}
    for key in ("%s:%s" % (board, refdes), pn, prefix_of(refdes)):
        if key and key in ov:
            return ov[key], SRC_OVERRIDE
    if pn and _PLACEHOLDER_RX.match(pn.strip()):
        return "placeholder", SRC_CIS
    if cis is not None and pn:
        cat, _code, _path = cis.lookup(pn)
        if cat:
            return cat, SRC_CIS
    cat = PREFIX_CATEGORY.get(prefix_of(refdes))
    if cat:
        return cat, SRC_PREFIX
    return UNRECOGNIZED, SRC_NONE


def signal_relevant(category):
    """分不出來的一律當「可能重要」。漏查比誤列一顆貴得多。"""
    return SIGNAL_RELEVANT.get(category, True)


def is_connector(nl, refdes):
    """連接器判定。

    原本這段在 `ndd.py` 與 `ndd_audit.py` 各寫了一份**逐字相同**的實作，
    改一邊忘另一邊只是時間問題，所以收斂到這裡。
    footprint 的判斷要留著——`P` 前綴沒收進通用表（跟「電位計」撞名），
    靠 footprint 兜底才不會漏。
    """
    fp = (nl.parts.get(refdes) or "").lower()
    return fp.startswith("conn") or bool(re.match(r"^J\d", refdes or ""))
