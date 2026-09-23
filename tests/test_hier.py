#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`.DSN` 階層（`ndd_hier`）的測試。

這裡的每一條都對應開發期間實際踩過的坑。**跑 tclsh 那一步不在測試範圍**
（需要 Capture，測試環境不該依賴），被替換掉；配對、解析、自我驗證、與
`.asc` 對帳全部走真實程式碼。

⚠️ 最重要的是 `TestCrosscheck`：階層寫錯不會讓任何東西崩潰，只會安靜地
   給出可信但錯誤的答案。對帳是唯一會把這種錯誤變成「當場失敗」的機制，
   它自己失效了就沒有任何東西守著了。
"""
import io
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
sys.path.insert(0, HERE)

import fixtures                                                # noqa: E402
import ndd_hier                                                # noqa: E402
import ndd_pinfn                                               # noqa: E402
from ndd_pads import Netlist                                   # noqa: E402

PARTS = {"U1": "PKG8", "R1": "R_0402", "J1": "Conn"}
NETS = {"SIG_A": [("U1", "1"), ("R1", "1")],
        "SIG_B": [("U1", "2"), ("J1", "3")],
        "GND": [("U1", "8"), ("R1", "2"), ("J1", "1")]}


class _Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="ndd_hier_")
        self.asc = fixtures.write_asc(os.path.join(self.d, "b.asc"), PARTS, NETS)
        self.p, self.n = fixtures.write_hier(
            os.path.join(self.d, "b_parts.csv"),
            os.path.join(self.d, "b_nodes.csv"), PARTS, NETS)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def hier(self):
        return ndd_hier.Hierarchy(self.p, self.n)


class TestParse(_Base):
    def test_counts_and_roles(self):
        h = self.hier()
        self.assertEqual(len(h.real_parts()), 3)
        self.assertEqual(len(h.pins()), 7)
        self.assertEqual([r["base_refdes"] for r in h.real_parts()].count("U1"), 1)

    def test_blocks_and_path_never_split_strings(self):
        """block 名稱可以含 `/`——路徑一律靠 parent_id 上溯，不得切字串。"""
        p, n = fixtures.write_hier(
            os.path.join(self.d, "h_parts.csv"), os.path.join(self.d, "h_nodes.csv"),
            PARTS, NETS, blocks={"U1": "SPST_P/R_8"})
        h = ndd_hier.Hierarchy(p, n)
        blk = h.blocks()[0]
        self.assertEqual(blk["refdes"], "SPST_P/R_8")
        u1 = [r for r in h.real_parts() if r["refdes"] == "U1"][0]
        self.assertEqual(h.path_of(u1["id"]), ["SPST_P/R_8", "U1"])
        self.assertTrue(h.selfcheck()["ok"])

    def test_pin_names_and_active_low(self):
        bs = chr(92)
        p, n = fixtures.write_hier(
            os.path.join(self.d, "p_parts.csv"), os.path.join(self.d, "p_nodes.csv"),
            PARTS, NETS,
            pin_names={("U1", "1"): "SENSE3+",
                       ("U1", "2"): bs.join(["", "P", "W", "R", "D", "N", ""])})
        h = ndd_hier.Hierarchy(p, n)
        self.assertEqual(h.pin_names()[("U1", "1")], "SENSE3+")
        self.assertEqual(h.active_low()[("U1", "2")], "PWRDN")
        # 純數字的腳位名不算功能名
        self.assertNotIn(("R1", "1"), h.pin_names())


class TestSelfcheck(_Base):
    def test_clean(self):
        self.assertTrue(self.hier().selfcheck()["ok"])

    def test_orphan_pin_is_caught(self):
        txt = io.open(self.n, encoding="utf-8").read().replace(",101,U1,", ",999,U1,")
        io.open(self.n, "w", encoding="utf-8", newline="").write(txt)
        sc = self.hier().selfcheck()
        self.assertFalse(sc["ok"])
        self.assertTrue(sc["orphan_pins"])

    def test_depth_inconsistent_with_parent_chain_is_caught(self):
        """API 的 depth 與 parent 鏈是兩個獨立來源，不符代表其中一個壞了。"""
        txt = io.open(self.p, encoding="utf-8").read().replace(
            "101,0,1,U1", "101,0,3,U1")
        io.open(self.p, "w", encoding="utf-8", newline="").write(txt)
        sc = self.hier().selfcheck()
        self.assertFalse(sc["ok"])
        self.assertTrue(sc["depth_mismatch"])


class TestCrosscheck(_Base):
    def test_matching_design_passes(self):
        cc = ndd_hier.crosscheck(self.hier(), Netlist(self.asc))
        self.assertTrue(cc["ok"], cc)
        self.assertEqual(cc["renamed"], [])

    def test_missing_connection_fails(self):
        """少一條接線必須失敗——這正是對帳存在的理由。"""
        txt = io.open(self.n, encoding="utf-8").read().splitlines(True)
        io.open(self.n, "w", encoding="utf-8", newline="").write("".join(txt[:-1]))
        self.assertFalse(ndd_hier.crosscheck(self.hier(), Netlist(self.asc))["ok"])

    def test_extra_part_fails(self):
        io.open(self.p, "a", encoding="utf-8", newline="").write(
            "999,0,1,U99,U99,U99,PKG,,PKG,0,0,U99\n")
        self.assertFalse(ndd_hier.crosscheck(self.hier(), Netlist(self.asc))["ok"])

    def test_pads_renamed_net_is_not_an_error(self):
        """PADS 不收 `*` / `/`，會把整條 net 改名成 `X#####`（實測 T_RADAR_T2）。

        名稱不同但節點集合完全相同 = 改名，不是接錯；要照樣通過並列出對照。
        """
        nets = dict(NETS)
        nets["GPU_SYS_RESET*"] = nets.pop("SIG_A")
        p, n = fixtures.write_hier(
            os.path.join(self.d, "r_parts.csv"), os.path.join(self.d, "r_nodes.csv"),
            PARTS, nets)
        cc = ndd_hier.crosscheck(ndd_hier.Hierarchy(p, n), Netlist(self.asc))
        self.assertTrue(cc["ok"], cc)
        self.assertEqual(cc["renamed"], [("GPU_SYS_RESET*", "SIG_A")])

    def test_rename_with_different_nodes_still_fails(self):
        """改名可以放行，但**接線不同就不行**——別讓改名成為漏網的藉口。"""
        nets = dict(NETS)
        nets["GPU_SYS_RESET*"] = [("U1", "1"), ("J1", "9")]
        del nets["SIG_A"]
        p, n = fixtures.write_hier(
            os.path.join(self.d, "x_parts.csv"), os.path.join(self.d, "x_nodes.csv"),
            PARTS, nets)
        self.assertFalse(ndd_hier.crosscheck(
            ndd_hier.Hierarchy(p, n), Netlist(self.asc))["ok"])


class TestPartPaths(_Base):
    def test_path_excludes_the_part_itself(self):
        p, n = fixtures.write_hier(
            os.path.join(self.d, "b2_parts.csv"), os.path.join(self.d, "b2_nodes.csv"),
            PARTS, NETS, blocks={"U1": "Power/Seq", "R1": "Power/Seq"})
        h = ndd_hier.Hierarchy(p, n)
        self.assertEqual(h.part_paths()["U1"], ["Power/Seq"])
        self.assertEqual(h.block_of("R1"), "Power/Seq")
        self.assertEqual(h.block_of("J1"), "")          # 掛在頂層
        self.assertEqual(h.block_of("NOPE"), "")        # 不存在也不能爆


class TestSymbolImport(unittest.TestCase):
    """symbol 腳位名入庫。**datasheet 原文列不得被動到**——那是人工確認過、
    無法從任何地方重建的資產。"""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="ndd_sym_")
        bs = chr(92)
        self.p, self.n = fixtures.write_hier(
            os.path.join(self.d, "s_parts.csv"), os.path.join(self.d, "s_nodes.csv"),
            PARTS, NETS,
            pin_names={("U1", "1"): "SENSE3+",
                       ("U1", "2"): bs.join(["", "P", "W", "R", "D", "N", ""])})
        self.h = ndd_hier.Hierarchy(self.p, self.n)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _run(self, pn_of, hier=None):
        return ndd_pinfn.import_symbols(
            self.d, [("b", hier or self.h, pn_of, self.n)], verbose=False)

    def rows(self):
        _p, rows, _m = ndd_pinfn.load_cache(self.d)
        return rows

    def test_writes_names_and_active_low(self):
        rep = self._run(lambda rd: {"U1": "PART_A"}.get(rd, ""))
        self.assertEqual(rep["written"], 2)
        got = {r["pin"]: r for r in self.rows()}
        self.assertEqual(got["1"]["pin_name"], "SENSE3+")
        self.assertEqual(got["1"]["active_low"], "")
        # 上劃線在名字裡是逐字元反斜線；存進快取前要還原成可讀名 + 旗標
        self.assertEqual(got["2"]["pin_name"], "PWRDN")
        self.assertEqual(got["2"]["active_low"], "1")
        self.assertEqual(got["2"]["resolved_by"], ndd_pinfn.SYMBOL)
        self.assertEqual(got["2"]["text"], "")      # symbol 沒有原文，不可捏造

    def test_datasheet_rows_survive_and_reimport_is_idempotent(self):
        ndd_pinfn.append_cache(self.d, {
            "part": "PART_A", "pin": "1", "pin_name": "SENSE3+",
            "text": "Current sense input", "source_file": "a.pdf", "page": "7",
            "sha256": "x" * 64, "resolved_by": ndd_pinfn.NOT_APPLICABLE})
        pn = lambda rd: {"U1": "PART_A"}.get(rd, "")
        self._run(pn)
        self._run(pn)                                # 再跑一次不得累積
        rows = self.rows()
        ds = [r for r in rows if r["resolved_by"] == ndd_pinfn.NOT_APPLICABLE]
        sym = [r for r in rows if r["resolved_by"] == ndd_pinfn.SYMBOL]
        self.assertEqual(len(ds), 1)
        self.assertEqual(ds[0]["text"], "Current sense input")
        self.assertEqual(len(sym), 2)

    def test_same_mpn_different_symbol_names_is_a_conflict(self):
        """同料號同腳位卻有兩個名字 = symbol 或 BOM 有一邊錯了。不可合併、
        不可挑一個，一律不寫入並列出來。"""
        p, n = fixtures.write_hier(
            os.path.join(self.d, "c_parts.csv"), os.path.join(self.d, "c_nodes.csv"),
            PARTS, NETS,
            pin_names={("U1", "1"): "SENSE3+", ("R1", "1"): "SENSE3-"})
        rep = self._run(lambda rd: "PART_A" if rd in ("U1", "R1") else "",
                        hier=ndd_hier.Hierarchy(p, n))
        self.assertEqual(rep["written"], 0)
        self.assertEqual(len(rep["conflicts"]), 1)
        self.assertEqual(self.rows(), [])

    def test_different_boards_keep_their_own_symbol_names(self):
        """實測：SN74CBTLV3126 在 interposer 叫 OE1、在 fecu_fm 叫 SEL1。各 .DSN
        各存一份 symbol，跨板名字不同不是錯，兩邊都要留下、各自可查。"""
        p2, n2 = fixtures.write_hier(
            os.path.join(self.d, "b2_parts.csv"), os.path.join(self.d, "b2_nodes.csv"),
            PARTS, NETS, pin_names={("U1", "1"): "SEL1"})
        pn = lambda rd: {"U1": "PART_A"}.get(rd, "")
        rep = ndd_pinfn.import_symbols(
            self.d, [("b", self.h, pn, self.n),
                     ("c", ndd_hier.Hierarchy(p2, n2), pn, n2)], verbose=False)
        self.assertEqual(rep["conflicts"], [])
        rows = self.rows()
        self.assertEqual(ndd_pinfn.symbol_map(rows, "PART_A", "b")["1"]["pin_name"],
                         "SENSE3+")
        self.assertEqual(ndd_pinfn.symbol_map(rows, "PART_A", "c")["1"]["pin_name"],
                         "SEL1")
        self.assertNotIn("1", ndd_pinfn.symbol_map(rows, "PART_A"),
                         "不知道是哪塊板時，各板不一致的腳不可替你挑一個")

    def test_appending_to_an_old_cache_keeps_columns_aligned(self):
        """舊快取沒有 board 欄；直接附加新列會整列錯位。"""
        old_cols = [c for c in ndd_pinfn.COLS if c != "board"]
        with io.open(os.path.join(self.d, ndd_pinfn.CACHE), "w",
                     encoding="utf-8-sig", newline="") as fh:
            fh.write(",".join(old_cols) + "\r\n")
            fh.write("PART_A,1,SENSE3+,,orig,a.pdf,7,x,,not_applicable,,,2026-01-01\r\n")
        ndd_pinfn.append_cache(self.d, {"part": "PART_B", "pin": "2",
                                        "pin_name": "EN", "text": "enable",
                                        "resolved_by": ndd_pinfn.NOT_APPLICABLE})
        got = {r["part"]: r for r in self.rows()}
        self.assertEqual(got["PART_A"]["text"], "orig")
        self.assertEqual(got["PART_B"]["pin_name"], "EN")
        self.assertEqual(got["PART_B"]["resolved_by"], ndd_pinfn.NOT_APPLICABLE)

    def test_refdes_without_bom_mpn_is_skipped(self):
        self.assertEqual(self._run(lambda rd: "")["written"], 0)


class TestConvertGuards(unittest.TestCase):
    def test_locked_design_is_refused_before_running(self):
        """`.DSNlck` 在就代表設計開在 Capture 裡；直接開檔會無限等待。"""
        d = tempfile.mkdtemp(prefix="ndd_lock_")
        try:
            dsn = os.path.join(d, "x.DSN")
            io.open(dsn, "w").write("x")
            io.open(dsn + "lck", "w").write("x")
            with self.assertRaises(ndd_hier.HierError) as cm:
                ndd_hier.convert(dsn, d, tclsh="<unused>")
            self.assertIn("lck", str(cm.exception))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_missing_dsn_is_refused(self):
        with self.assertRaises(ndd_hier.HierError):
            ndd_hier.convert("no_such_file.DSN", ".", tclsh="<unused>")


class TestInitRequiresDsn(unittest.TestCase):
    def test_init_run_stops_when_no_dsn(self):
        """v2 要求三份齊備。少了 .DSN 要明確失敗，不可默默降級。"""
        import test_init
        import ndd
        d = test_init._folder(two_boards=False, with_dsn=False)
        try:
            with self.assertRaises(SystemExit) as cm:
                ndd.main(["init", d, "--run", "--no-datasheets"])
            self.assertIn(".DSN", str(cm.exception))
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
