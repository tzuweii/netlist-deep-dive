#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""零件分類：這顆是 IC、被動件、連接器，還是機構件？

**為什麼需要這個**：`coverage` 原本把每一顆 active 平等列出，於是屏蔽罩、
螺帽跟 FPGA、ADC 混在同一張表，每一列都掛著「待查 datasheet」。那不是清單，
那是雜訊——真正該先查的東西被埋掉了。

**分類的權威來源是料號，不是 refdes 前綴。** 公司的料件資料庫（CIS）本來就
有一套綁在料號上的分類體系——實測 6 塊板 1509 顆 active，料號查表命中 93.6%。

但 CIS 快照是內部資料，不能隨 skill 散布。所以主力其實是 **footprint**：它
就在 `.asc` 裡，不需要密碼、不會過期，而且比 refdes 前綴精確得多——refdes
只說得出「這是一顆 IC」，footprint 說得出「這是 ADC／濾波器／連接器」。

實測（同樣六塊板）：**沒有 CIS 快照時，footprint 分類涵蓋 96.9%，未辨識只剩
1.7%**（沒有 footprint 這層是 9.5%）；有快照時兩者合計未辨識 0。

順位：

    1. `ndd.json` 的 `part_class` 覆寫   —— 人講的最大
    2. CIS 快照查表（料號）              —— **事實**
    3. footprint 樣式                    —— 推論
    4. 料號樣式（OPEN/DNP/NC…）          —— 推論
    5. refdes 前綴通用表                 —— 推論
    6. 都沒有 -> `unrecognized`          —— **攤出來等人確認，不猜**

⚠️ 第 2 與第 3-5 的可信度不同，**呼叫端必須把 `source` 一起帶出去**（用
   `INFERRED_SRC` 判斷）：CIS 命中是 `[B]` 等級的事實，樣式推出來的是 `[?]`。
   混為一談就等於把推論洗成事實。

CIS 快照是**選用加速器，不是前提**，與 `models.json` 的定位一致。
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

# footprint 樣式 -> 分類。**比 refdes 前綴準得多**：refdes 只說得出「這是一顆
# IC」，footprint 說得出「這是 ADC／濾波器／連接器」。
#
# 這張表是**從真實專案的 footprint 逐條比對 CIS 分類學出來的**（1161 顆，零
# 衝突），不是從 EDA 詞彙表挑的——準確度來自那次對照。字面剛好都是通用功能
# 詞，所以能出貨而不透露任何料號、客戶或頻段，那是另一件事。
#
# ⚠️ **刻意排除的**，即使它們在那批資料上「學得出來」：
#    - `QFN` -> rf：QFN 是**封裝**不是功能，那批板子的 QFN 剛好都是 RF 件而已。
#    - `R` -> sensor：來自 `R-1Kohm`（熱敏電阻），但 `R` 是電阻的通用前綴，
#      套到別塊板會大規模誤判。
#    - footprint 名稱嵌了料號的（`ADC-<原廠料號>` 這種寫法）、含客戶代號、
#      專案代號或工作頻段字樣的——出貨的規則表不該透露這些，改走專案本地的
#      `footprint_class`／`part_class`。
#
# ⚠️ 這是**推論**，不是事實。footprint 是繪圖慣例，選錯 footprint 就分類錯，
#    而且沒有症狀。要定案仍須查 CIS 或 datasheet。
FOOTPRINT_CATEGORY = {
    # 前兩段優先，找不到才退回首段
    "PMIC-DCDC": "power_module",
    "PMIC-Buck": "ic", "PMIC-LDO": "ic", "PMIC-MON": "ic", "PMIC-REF": "ic",
    "PMIC-REG": "ic", "PMIC-SUP": "ic", "PMIC-SW": "ic",
    "D-TVS": "circuit_protection", "D-LED": "optoelectronic",
    "D-Rectifier": "semiconductor_discrete",
    "D-Schottky": "semiconductor_discrete",
    # 首段
    "ADC": "ic", "DAC": "ic", "Buff": "ic", "Controller": "ic", "FPGA": "ic",
    "IO": "ic", "Logic": "ic", "MUX": "ic", "Memory": "ic", "PLL": "ic",
    "RealTimeClock": "ic", "TRX": "ic", "SWIC": "ic",
    "RF": "rf", "CPL": "rf", "Amplifier": "rf", "Balun": "rf",
    "BPF": "filter", "LPF": "filter", "CMC": "filter", "EMI": "filter",
    "Ferrite": "filter",
    "Conn": "connector", "Header": "connector",
    "OSC": "crystal", "Sensor": "sensor", "SW": "switch",
    "OPTOISO": "isolator", "BAT": "battery", "Fuse": "circuit_protection",
    "Trans": "semiconductor_discrete", "Nut": "mechanical",
    "Open": "placeholder",
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
#
# ⚠️ **一定要比對完整 token。** 原本寫成 `^(OPEN|NC|...)[-_ ]?` 少了結尾條件，
#    於是 `NCR2-123+`（MiniCircuits 變壓器）、`NCP1117`（ON Semi LDO）、
#    `NC7SZ125`（ON 邏輯閘）這種真零件被當成「預留未貼位置」，安靜地從
#    「待查 datasheet」清單消失。
#
#    ⚠️ 分隔符後面**不可以放行數字**：那會讓 `NC7SZ125` 又中招。代價是
#    `OPEN0402`（沒有分隔符）比不到——可以接受，那種寫法由 footprint 的
#    `Open` 規則涵蓋，而且真實資料寫的是 `OPEN_0402`。
_PLACEHOLDER_RX = re.compile(r"^(OPEN|NC|DNP|DNI|NOSTUFF)([-_ ]|$)", re.I)

SRC_OVERRIDE, SRC_CIS = "override", "cis"
SRC_FOOTPRINT, SRC_PREFIX, SRC_NONE = "footprint", "prefix", "none"
# 從名字樣式推出來的（footprint／料號樣式／refdes 前綴）都是**推論**，
# 只有 CIS 查表與人工宣告算事實。呼叫端要靠這個分辨，不可混用。
INFERRED_SRC = (SRC_FOOTPRINT, SRC_PREFIX)


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


def footprint_class(fp, overrides=None):
    """footprint -> 分類。前兩段優先（`PMIC-LDO`），找不到才退回首段。"""
    segs = [s for s in re.split(r"[_\-]", (fp or "").strip()) if s]
    if not segs:
        return None
    ov = overrides or {}
    for key in ("-".join(segs[:2]), segs[0]):
        if key in ov:
            return ov[key]
        if key in FOOTPRINT_CATEGORY:
            return FOOTPRINT_CATEGORY[key]
    return None


def classify(refdes, pn, board=None, overrides=None, cis=None,
             footprint=None, fp_overrides=None):
    """回傳 (分類, 來源)。**來源決定這筆能不能當事實引用。**

    順位：人工宣告 > CIS 查表（事實）> footprint 樣式 > 料號樣式 >
          refdes 前綴（以上三者皆為推論）> 未辨識（不猜）。
    """
    ov = overrides or {}
    for key in ("%s:%s" % (board, refdes), pn, prefix_of(refdes)):
        if key and key in ov:
            return ov[key], SRC_OVERRIDE
    if cis is not None and pn:
        cat, _code, _path = cis.lookup(pn)
        if cat:
            return cat, SRC_CIS
    cat = footprint_class(footprint, fp_overrides)
    if cat:
        return cat, SRC_FOOTPRINT
    if pn and _PLACEHOLDER_RX.match(pn.strip()):
        return "placeholder", SRC_FOOTPRINT
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
