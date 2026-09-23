#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""規格書涵蓋多種封裝時，依 `.DSN` symbol 選欄 + netlist 電源地佐證。

腳位表仿 PCA9554（第 1 欄 SO16/TSSOP16、第 2 欄 HVQFN16），symbol 照 HVQFN16 建。
"""
import csv
import io
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import ndd_pinfn                                               # noqa: E402

TABLE = u"""Table 3. Pin description
A0 1 15 address input 0
A1 2 16 address input 1
A2 3 1 address input 2
P0 4 2 port input/output 0
P1 5 3 port input/output 1
P2 6 4 port input/output 2
P3 7 5 port input/output 3
VSS 8 6 supply ground
P4 9 7 port input/output 4
P5 10 8 port input/output 5
P6 11 9 port input/output 6
P7 12 10 port input/output 7
INT 13 11 interrupt output
SCL 14 12 serial clock line
SDA 15 13 serial data line
VDD 16 14 supply voltage
"""
HVQFN = {"1": "A2", "2": "P0", "3": "P1", "4": "P2", "5": "P3", "6": "VSS",
         "7": "P4", "8": "P5", "9": "P6", "10": "P7", "11": "INT", "12": "SCL",
         "13": "SDA", "14": "VDD", "15": "A0", "16": "A1"}
BOARD = {"6": "GND", "14": "PWR", "1": "GND", "15": "GND", "16": "PWR",
         "12": "SIG", "13": "SIG", "2": "SIG"}           # U912 的實際接法


def _fake_pages(_path, _max):
    yield 1, u"PCA9554B; PCA9554C 8-bit I2C-bus I/O port"
    yield 4, TABLE
    yield 6, u"Bit 7 6 5 4 3 2 1 0"


class _Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="ndd_sym_")
        self.pdf = os.path.join(self.d, "PCA9554B_PCA9554C.pdf")
        with open(self.pdf, "wb") as fh:
            fh.write(b"%PDF-1.4 dummy")
        self._orig = ndd_pinfn._pages
        ndd_pinfn._pages = _fake_pages
        ndd_pinfn._PARSED.clear()

    def tearDown(self):
        ndd_pinfn._pages = self._orig
        ndd_pinfn._PARSED.clear()
        shutil.rmtree(self.d, ignore_errors=True)


class TestSelect(_Base):
    def test_symbol_picks_the_package_column_and_power_pins_agree(self):
        hit, why = ndd_pinfn.select_by_symbol(self.pdf, "12", HVQFN, BOARD.get)
        self.assertIsNotNone(hit, why)
        self.assertEqual(hit[1], "SCL")
        self.assertIn("第 2 欄", why)
        self.assertIn("第 14 腳 VDD 接電源", why)

    def test_power_pin_left_unconnected_blocks_the_choice(self):
        board = dict(BOARD)
        board.pop("14")                                     # VDD 沒接
        hit, why = ndd_pinfn.select_by_symbol(self.pdf, "12", HVQFN, board.get)
        self.assertIsNone(hit)
        self.assertIn("沒接", why)

    def test_power_pin_on_the_opposite_rail_blocks_the_choice(self):
        board = dict(BOARD, **{"6": "PWR"})                 # VSS 接到電源
        hit, _why = ndd_pinfn.select_by_symbol(self.pdf, "12", HVQFN, board.get)
        self.assertIsNone(hit)

    def test_no_power_pin_to_verify_means_no_decision(self):
        """symbol 選欄後腳名必然一致；沒有獨立佐證就不定案。"""
        hit, why = ndd_pinfn.select_by_symbol(
            self.pdf, "12", HVQFN, lambda n: "SIG")
        self.assertIsNone(hit)
        self.assertIn("沒有可在 netlist 上驗證", why)

    def test_symbol_disagreeing_with_the_table_is_not_resolved(self):
        wrong = dict(HVQFN, **{"13": "SCL", "12": "SDA"})   # symbol 把 SCL/SDA 畫反
        hit, why = ndd_pinfn.select_by_symbol(self.pdf, "12", wrong, BOARD.get)
        self.assertIsNone(hit)
        self.assertIn("腳名不符", why)

    def test_too_few_symbol_pins_is_not_enough(self):
        hit, _why = ndd_pinfn.select_by_symbol(
            self.pdf, "12", {"12": "SCL", "14": "VDD"}, BOARD.get)
        self.assertIsNone(hit)

    def test_pin_outside_the_table_is_reported_not_guessed(self):
        hit, why = ndd_pinfn.select_by_symbol(self.pdf, "17", HVQFN, BOARD.get)
        self.assertIsNone(hit)
        self.assertIn("第 17 腳", why)


class TestLookup(_Base):
    def _seed_symbol(self):
        with io.open(os.path.join(self.d, ndd_pinfn.CACHE), "w",
                     encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=ndd_pinfn.COLS)
            w.writeheader()
            for pin, name in HVQFN.items():
                w.writerow({"part": "PCA9554BBSHP", "pin": pin, "pin_name": name,
                            "resolved_by": ndd_pinfn.SYMBOL})

    def _rows(self):
        return [r for r in ndd_pinfn.load_cache(self.d)[1]
                if r["resolved_by"] != ndd_pinfn.SYMBOL]

    def test_selected_answer_is_cached_with_its_evidence(self):
        self._seed_symbol()
        out = ndd_pinfn.lookup(self.d, self.d, "PCA9554BBSHP", "12",
                               explicit=os.path.basename(self.pdf),
                               verbose=False, role_of=BOARD.get)
        self.assertEqual([r["pin_name"] for r in out], ["SCL"])
        rows = self._rows()
        self.assertEqual(rows[0]["resolved_by"], ndd_pinfn.SYMBOL_SELECTED)
        self.assertIn("電源地佐證", rows[0]["package"])

    def test_without_netlist_the_tool_still_refuses_to_pick(self):
        self._seed_symbol()
        out = ndd_pinfn.lookup(self.d, self.d, "PCA9554BBSHP", "12",
                               explicit=os.path.basename(self.pdf), verbose=False)
        self.assertEqual(out, [])
        self.assertEqual(self._rows(), [], "未定案不得寫快取")


if __name__ == "__main__":
    unittest.main()
