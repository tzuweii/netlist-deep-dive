#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PCBA BOM (.xlsx) 解析器（通用）。

核心用途：BOM 的 refdes 欄展開後即得 `refdes -> Value / Manufacturer_PN` 查表。

⚠️ **標題列位置各檔不同**，所以用「哪一列含有 refdes 欄名」自動偵測，
   **絕不寫死列號或欄號**。

**使用者給的 BOM 就是這塊板的權威**——`netlist 有、BOM 無` 即未貼件 (DNI)。
工具不做變體推理，也不去質疑 BOM 的正確性。

⚠️ 唯一的例外是**文件涵蓋範圍**（不是正確性）：SMT BOM 依定義只列 SMT 件，
   連接器、測試點、鎖孔、手插件本來就不在裡面，它們的缺席不代表沒貼。
   用 `scope` 標註：

     complete  —— 涵蓋全部佈件（**預設**）
     smt_only  —— 只涵蓋 SMT 件；SMT 件缺席仍是 DNI，非 SMT 件缺席則不可判定

   `variant` / `unknown` 保留為 BOM 身分的註記，判讀上等同 `complete`。

⚠️ **重複 refdes 不得靜默覆蓋**。舊版後列直接蓋掉前列，而 DNI 對帳與
   `not_stuffed` 斷言全都建立在 `of()` 上——覆蓋掉的那列可能正是關鍵資訊。
   衝突的 refdes 一律回傳 `AMBIGUOUS`，並讓相關斷言 **FAIL 而不是靜默跳過**。
"""
import io
import os
import re

try:
    import openpyxl
except ImportError:                                  # pragma: no cover
    openpyxl = None

REF_COL_CANDIDATES = [
    "part reference", "reference", "references", "designator", "designators",
    "refdes", "ref des", "part references", "location",
]
PN_COL_CANDIDATES = ["manufacturer_pn", "manufacturer pn", "mfg pn", "mpn",
                     "manufacturer part number", "part number", "name"]
VAL_COL_CANDIDATES = ["value", "comment", "description"]

SCOPES = ("complete", "smt_only", "variant", "unknown")

_SPLIT_RX = re.compile(r"[,\s;]+")
# 疑似範圍：R1-R5 / R1-5。⚠️ **只警告不展開** —— `J1-1` 也可能是合法 refdes，
# 自動展開是猜測，違反「不確定就標記，不要補完」。
_RANGE_RX = re.compile(r"^([A-Za-z]+)(\d+)\s*-\s*([A-Za-z]*)(\d+)$")


class _Ambiguous(object):
    """同一 refdes 在 BOM 出現多列且內容衝突。**不是資料，是待解事項。**"""

    def __init__(self, refdes=None, rows=None):
        self.refdes = refdes
        self.rows = rows or []

    def __repr__(self):
        return "<AMBIGUOUS %s @ rows %s>" % (
            self.refdes, ",".join(str(r) for r in self.rows))

    def __bool__(self):
        return False                 # 不可被當成「有找到」

    __nonzero__ = __bool__


AMBIGUOUS = _Ambiguous()


def is_ambiguous(x):
    return isinstance(x, _Ambiguous)


class Bom(object):
    def __init__(self, path, sheet=None, ref_col=None, scope="complete",
                 expand_ranges=False):
        if openpyxl is None:
            raise RuntimeError("需要 openpyxl：pip install openpyxl")
        if scope not in SCOPES:
            raise ValueError(
                "bom_scope 必須是 %s 之一（目前 %r）。舊欄位 `bom_kind` 已改名，"
                "對應：SMT BOM -> smt_only，其餘一律 complete。"
                % ("／".join(SCOPES), scope))
        self.path = path
        self.name = os.path.basename(path)
        self.scope = scope
        self.expand_ranges = expand_ranges
        self.rows = []
        self.ref = {}
        self.dups = {}               # refdes -> [列號, ...]
        self.suspect_ranges = []     # (cell, 列號)
        self.header_row = None
        self.header = []
        self.ref_col_name = None
        self.sheet_name = None
        self._parse(sheet, ref_col)

    def _parse(self, sheet, ref_col):
        wb = openpyxl.load_workbook(self.path, data_only=True, read_only=True)
        ws = wb[sheet] if sheet else wb[wb.sheetnames[0]]
        self.sheet_name = ws.title
        wanted = [ref_col.lower()] if ref_col else REF_COL_CANDIDATES
        header = None
        idx = None
        for r, row in enumerate(ws.iter_rows(values_only=True), start=1):
            vals = ["" if v is None else str(v).strip() for v in row]
            if header is None:
                low = [v.lower() for v in vals]
                hit = next((w for w in wanted if w in low), None)
                if hit:
                    header, idx = vals, low.index(hit)
                    self.header_row, self.ref_col_name = r, vals[idx]
                continue
            if not any(vals):
                continue
            d = {h: v for h, v in zip(header, vals) if h}
            d["_row"] = r
            self.rows.append(d)
            cell = vals[idx] if idx < len(vals) else ""
            for rd in self.expand(cell, r):
                if rd in self.ref and self.ref[rd].get("_row") != r:
                    self.dups.setdefault(rd, [self.ref[rd]["_row"]]).append(r)
                else:
                    self.ref[rd] = d
        wb.close()
        if header is None:
            raise ValueError(
                "在 %s 找不到 refdes 欄（試過 %s）。用 --ref-col 指定欄名。"
                % (self.name, ", ".join(wanted)))
        self.header = header

    def expand(self, cell, row=None):
        if not cell:
            return []
        out = []
        for t in _SPLIT_RX.split(cell.strip()):
            if not t:
                continue
            m = _RANGE_RX.match(t)
            if m and (not m.group(3) or m.group(3) == m.group(1)):
                self.suspect_ranges.append((t, row))
                if self.expand_ranges:
                    lo, hi = int(m.group(2)), int(m.group(4))
                    if lo <= hi:
                        out.extend("%s%d" % (m.group(1), i)
                                   for i in range(lo, hi + 1))
                        continue
            out.append(t)
        return out

    def _pick(self, d, cands):
        low = {k.lower(): k for k in d if isinstance(k, str)}
        for c in cands:
            if c in low and d[low[c]]:
                return d[low[c]]
        return ""

    def of(self, refdes):
        if refdes in self.dups:
            return _Ambiguous(refdes, self.dups[refdes])
        return self.ref.get(refdes)

    def pn(self, refdes):
        d = self.of(refdes)
        if is_ambiguous(d):
            return d
        return self._pick(d, PN_COL_CANDIDATES) if d else None

    def value(self, refdes):
        d = self.of(refdes)
        if is_ambiguous(d):
            return d
        return self._pick(d, VAL_COL_CANDIDATES) if d else None

    def _pn_str(self, refdes):
        p = self.pn(refdes)
        return "" if (p is None or is_ambiguous(p)) else p

    def count_pn(self, pn):
        pn = pn.upper()
        return sum(1 for r in self.ref if self._pn_str(r).upper() == pn)

    def refdes_of_pn(self, pn):
        pn = pn.upper()
        return sorted(r for r in self.ref if self._pn_str(r).upper() == pn)

    def absent_label(self):
        """netlist 有、BOM 無 = 未貼件。BOM 是這塊板的權威。"""
        return "未貼件 (DNI)"

    def covers_class(self, is_mech, is_connector):
        """這份 BOM 的涵蓋範圍包不包含這一類零件。

        ⚠️ 這問的是**文件涵蓋範圍**，不是 BOM 的正確性。SMT BOM 完全正確，
           只是依定義不列非 SMT 件。
        """
        if self.scope != "smt_only":
            return True
        return not (is_mech or is_connector)

    def __repr__(self):
        return ("<Bom %s [%s] sheet=%s: %d items / %d refdes"
                " (標題列 %d, refdes 欄 '%s', 重複 %d, 疑似範圍 %d)>"
                % (self.name, self.scope, self.sheet_name, len(self.rows),
                   len(self.ref), self.header_row, self.ref_col_name,
                   len(self.dups), len(self.suspect_ranges)))
