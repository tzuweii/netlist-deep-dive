#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""合成 fixture 產生器。

⚠️ **測試一律使用抽象料號與 refdes**（`BUF_A`、`MUX_A`、`U1`…），不得綁定任何
   真實專案的板名、net 名、refdes 或私有 datasheet。真實專案只能當驗證語料，
   不能變成程式或測試中的特例。見 SPEC.md §12。
"""
import io
import os
import tempfile


# --------------------------------------------------------------------- .asc --
def write_asc(path, parts, nets):
    """parts: {refdes: footprint}；nets: {net: [(refdes, pin), ...]}"""
    out = ["*PADS2000*", "*PART*"]
    for rd, fp in parts.items():
        out.append("%s %s" % (rd, fp))
    out.append("*NET*")
    for net, conns in nets.items():
        out.append("*SIGNAL* %s" % net)
        out.append(" ".join("%s.%s" % (rd, p) for rd, p in conns))
    out.append("*END*")
    with io.open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    return path


# -------------------------------------------------------------------- .xlsx --
def write_bom(path, rows, header=None, sheet="BOM"):
    """rows: list[dict]；預設欄位 Part Reference / Manufacturer_PN / Value。"""
    import openpyxl
    header = header or ["Part Reference", "Manufacturer_PN", "Value"]
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append(header)
    for r in rows:
        ws.append([r.get(h, "") for h in header])
    wb.save(path)
    return path


# ---------------------------------------------------------------- 專案骨架 --
class Project(object):
    """建一個臨時分析資料夾，含 .asc / BOM / ndd.json。"""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="ndd_test_")
        self.cfg = {
            "project": "synthetic",
            "boards": {},
            "mates": [],
            "mate_map": {},
            "part_package": {},
            "endpoints": {},
            "power_net_regex": r"^(?!.*_(EN|PG)$)(GND|.*VDD.*|.*VCC.*)$",
            "net_normalize": [],
            "trace": {"start": [], "slot_pattern": ""},
            "assertions": [],
            "role_rules": [],
            "datasheets": {"dir": "datasheets", "parts": []},
        }

    def board(self, key, parts, nets, bom_rows, bom_scope="complete", label=None):
        asc = "%s.asc" % key
        bom = "%s.xlsx" % key
        write_asc(os.path.join(self.dir, asc), parts, nets)
        write_bom(os.path.join(self.dir, bom), bom_rows)
        self.cfg["boards"][key] = {
            "label": label or key, "asc": asc, "bom": bom,
            "bom_scope": bom_scope, "ref_col": None,
        }
        return self

    def models(self, models):
        import json
        with io.open(os.path.join(self.dir, "models.json"), "w",
                     encoding="utf-8") as fh:
            fh.write(json.dumps(models, indent=2, ensure_ascii=False))
        return self

    def save(self):
        import json
        p = os.path.join(self.dir, "ndd.json")
        with io.open(p, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(self.cfg, indent=2, ensure_ascii=False))
        return p

    def path(self, *parts):
        return os.path.join(self.dir, *parts)


# ---------------------------------------------------------------- 常用零件 --
def buffer_model(mpn="BUF_A", package="PKG24"):
    """單向緩衝器：IN -> OUT，OE 低態致能（strap 可解析）。"""
    return {
        "match": [mpn],
        "kind": "signal_transfer",
        "package": package,
        "verified_against": "synthetic.pdf p.1",
        "pin_roles": {"10": "GND", "20": "PWR"},
        "ordering_suffix": ["PW"],
        "footprint_match": ["TSSOP", "PKG24"],
        "transfer": [{"from": ["2"], "to": ["18"], "direction": "forward",
                      "gate": {"all_of": ["1"]}}],
        "control": {"1": {"type": "enable", "polarity": "low",
                          "mechanism": "strap"}},
    }


def switch_model(mpn="SW_A", package="PKG14"):
    """雙向 FET switch：datasheet 證實雙向。"""
    return {
        "match": [mpn],
        "kind": "signal_transfer",
        "package": package,
        "verified_against": "synthetic.pdf p.2",
        "pin_roles": {"7": "GND", "14": "PWR"},
        "transfer": [{"from": ["2"], "to": ["3"], "direction": "bidirectional",
                      "gate": {"all_of": ["1"]}}],
        "control": {"1": {"type": "enable", "polarity": "high",
                          "mechanism": "strap"}},
    }


def fanout_model(mpn="FAN_A", package="PKG20"):
    """1-to-3 fanout：input -> 每個 output，輸出之間不得互通。"""
    return {
        "match": [mpn],
        "kind": "signal_transfer",
        "package": package,
        "verified_against": "synthetic.pdf p.3",
        "transfer": [{"from": ["1"], "to": ["3", "5", "7"],
                      "direction": "forward"}],
        "control": {},
    }


def mux_model(mpn="MUX_A", package="PKG24"):
    """runtime 選通的 mux：位址腳固定不得使其成為 always。"""
    return {
        "match": [mpn],
        "kind": "signal_transfer",
        "package": package,
        "verified_against": "synthetic.pdf p.4",
        "transfer": [{"from": ["20"], "to": ["1", "3"],
                      "direction": "bidirectional",
                      "gate": {"all_of": ["channel_enabled"]}}],
        "control": {"18": {"type": "address", "mechanism": "strap"},
                    "22": {"type": "address", "mechanism": "strap"}},
        "runtime_conditions": {
            "channel_enabled": {"kind": "runtime_register",
                                "verified_against": "synthetic.pdf p.4"}},
    }


def latch_model(mpn="LATCH_A", package="PKG16"):
    """stateful：SER -> 串接輸出是 transfer；LOAD 只是 control influence。"""
    return {
        "match": [mpn],
        "kind": "stateful",
        "package": package,
        "verified_against": "synthetic.pdf p.5",
        "transfer": [{"from": ["14"], "to": ["9"], "direction": "forward"}],
        "control": {"12": {"type": "trigger", "mechanism": "external"}},
        "control_influence": [{"from": ["12"], "effect": "latch_state_update",
                               "affects": "output_register",
                               "verified_against": "synthetic.pdf p.5"}],
    }


def two_package_variants(mpn="EXP_A"):
    """同一料號的兩個封裝版本，電源腳位置不同 —— 這是拓樸判別力的來源。

    A 版：VSS=8、VDD=16      B 版：VSS=6、VDD=14
    netlist 只要顯示哪支腳接地／接電源，就足以分辨，**完全不需要 datasheet**。
    """
    base = {"match": [mpn], "kind": "signal_transfer",
            "verified_against": "synthetic.pdf p.1"}
    a = dict(base, package="PKGA16", pin_roles={"8": "GND", "16": "PWR"},
             ordering_suffix=["PW"], footprint_match=["TSSOP16"],
             transfer=[{"from": ["1"], "to": ["4"], "direction": "forward"}])
    b = dict(base, package="PKGB16", pin_roles={"6": "GND", "14": "PWR"},
             ordering_suffix=["BS"], footprint_match=["QFN16"],
             transfer=[{"from": ["15"], "to": ["2"], "direction": "forward"}])
    return {"EXP_A_PKGA": a, "EXP_A_PKGB": b}
