#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回歸測試。

每一條都對應 SPEC.md 裡一個**曾經會靜默出錯**的行為。全部使用合成 fixture，
不依賴任何客戶專案檔案、私有 datasheet 或特定 refdes。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
sys.path.insert(0, HERE)

import fixtures                                                # noqa: E402
import ndd_confidence as C                                     # noqa: E402
import ndd_pinfn                                               # noqa: E402
from ndd_bom import Bom, is_ambiguous                          # noqa: E402
from ndd_graph import (EP_UNCLASSIFIED, EP_UNKNOWN_DECLARED,   # noqa: E402
                       EP_UNKNOWN_UNUSABLE, Fabric)
from ndd_models import (ModelError, derive_gating, load_models,  # noqa: E402
                        missing_pins, outgoing, select_model,
                        transfer_edges, transfer_for)
from ndd_pads import Netlist                                   # noqa: E402


def _fab(pj, models=None):
    boards = {}
    for k, b in pj.cfg["boards"].items():
        boards[k] = (Netlist(pj.path(b["asc"])),
                     Bom(pj.path(b["bom"]), scope=b.get("bom_scope", "unknown")))
    return Fabric(boards, pj.cfg["mates"], models or {},
                  pj.cfg["power_net_regex"], pj.cfg["net_normalize"],
                  mate_map=pj.cfg["mate_map"], endpoints=pj.cfg["endpoints"],
                  part_package=pj.cfg["part_package"])


# ===========================================================  Commit 1  ====
class TestParserSelfcheck(unittest.TestCase):
    def test_pin_in_two_nets_must_fail(self):
        """一支腳掛兩條 net：pin_tokens 與 nets 計數**都會**通過，只有 pinmap 少。

        初版在此回報 PASS，且第一條 net 從 pinmap 靜默消失。
        """
        pj = fixtures.Project()
        path = fixtures.write_asc(
            pj.path("b.asc"), {"U1": "PKG8", "U2": "PKG8"},
            {"NET_A": [("U1", "1"), ("U2", "1")],
             "NET_B": [("U1", "1"), ("U2", "2")]})   # U1.1 出現兩次
        nl = Netlist(path)
        r = nl.selfcheck()
        self.assertEqual(r["pins"][0], r["pins"][1], "舊的兩個計數仍然相等")
        self.assertFalse(r["ok"], "必須 FAIL")
        self.assertTrue(any("U1.1" in d for d in r["dup_pins"]))

    def test_clean_netlist_passes(self):
        pj = fixtures.Project()
        path = fixtures.write_asc(pj.path("b.asc"), {"U1": "PKG8"},
                                  {"NET_A": [("U1", "1"), ("U1", "2")]})
        self.assertTrue(Netlist(path).selfcheck()["ok"])


class TestMateDirection(unittest.TestCase):
    def test_partner_lookup_is_symmetric(self):
        """初版只查正向，反向宣告的 mate 會讓整條訊號從 CSV 靜默消失。"""
        pj = fixtures.Project()
        pj.board("a", {"J1": "CONN"}, {"S1": [("J1", "1")]},
                 [{"Part Reference": "J1", "Manufacturer_PN": "CONN_A"}])
        pj.board("b", {"J2": "CONN"}, {"S1": [("J2", "1")]},
                 [{"Part Reference": "J2", "Manufacturer_PN": "CONN_A"}])
        pj.cfg["mates"] = [["a", "J1", "b", "J2"]]
        fab = _fab(pj)
        self.assertEqual(fab.mate_partners("a", "J1"), [("b", "J2")])
        self.assertEqual(fab.mate_partners("b", "J2"), [("a", "J1")],
                         "反向查詢必須同樣找得到")


# ===========================================================  Commit 2  ====
class TestConfidence(unittest.TestCase):
    def test_no_string_ordering(self):
        """`conditional` < `unknown` 的字典序毫無意義，必須用 enum 排序。"""
        self.assertEqual(C.worst_gating([C.ALWAYS, C.CONDITIONAL]), C.CONDITIONAL)
        self.assertEqual(C.worst_gating([C.CONDITIONAL, C.UNKNOWN]), C.UNKNOWN)
        self.assertEqual(C.worst_confidence([C.CONFIRMED, C.CAVEATED]), C.CAVEATED)

    def test_confidence_and_gating_are_separate_axes(self):
        """證據品質與閘控狀態不可混成一軸。"""
        self.assertNotIn(C.CONDITIONAL, C.CONFIDENCE_ORDER)
        self.assertNotIn(C.CAVEATED, C.GATING_ORDER)

    def test_caveats_are_sorted(self):
        a = C.render({"b:x", "a:y"})
        b = C.render({"a:y", "b:x"})
        self.assertEqual(a, b, "輸出順序不定會讓每次重跑產生假 diff")


class TestMateApproval(unittest.TestCase):
    def _two_boards(self):
        pj = fixtures.Project()
        pj.board("a", {"J1": "CONN"},
                 {"S1": [("J1", "1")], "S2": [("J1", "2")]},
                 [{"Part Reference": "J1", "Manufacturer_PN": "CONN_A"}])
        pj.board("b", {"J2": "CONN"},
                 {"S1": [("J2", "1")], "S2": [("J2", "2")]},
                 [{"Part Reference": "J2", "Manufacturer_PN": "CONN_A"}])
        pj.cfg["mates"] = [["a", "J1", "b", "J2"]]
        return pj

    def test_unapproved_mate_carries_caveat(self):
        """初版直接以同 pin number 連邊，排名結果從未進入走圖。"""
        fab = _fab(self._two_boards())
        edges = fab.mate.get(("a", "J1", "1"))
        self.assertTrue(edges)
        self.assertIn("mate:unapproved", edges[0][1])
        self.assertEqual(fab.mate_status[("a", "J1", "b", "J2")], "unapproved")

    def test_approved_mate_has_no_caveat(self):
        pj = self._two_boards()
        pj.cfg["mate_map"] = {"a:J1|b:J2": {
            "approved": {"1": "1", "2": "2"}, "evidence": "synthetic",
            "confidence": "confirmed"}}
        fab = _fab(pj)
        self.assertEqual(fab.mate.get(("a", "J1", "1"))[0][1], [])
        self.assertEqual(fab.mate_status[("a", "J1", "b", "J2")], "approved")

    def test_approved_map_is_used_not_same_pin(self):
        """批准的對映若不是直通，走圖必須照批准走。"""
        pj = self._two_boards()
        pj.cfg["mate_map"] = {"a:J1|b:J2": {
            "approved": {"1": "2", "2": "1"}, "evidence": "synthetic",
            "confidence": "confirmed"}}
        fab = _fab(pj)
        self.assertEqual(fab.mate[("a", "J1", "1")][0][0], ("b", "J2", "2"))


# ===========================================================  Commit 3  ====
class TestPackageResolution(unittest.TestCase):
    # legal 是 {封裝欄: {pin: 腳位功能}}。兩個封裝的**功能不同**——這正是常態，
    # 也是 package 解析要解決的問題；只比 pin label 集合等於把問題當成答案。
    LEGAL = {"PKGA24": {str(i): "FN_A%d" % i for i in range(1, 25)},
             "PKGB20": {str(i): "FN_B%d" % i for i in range(1, 21)}}

    def test_larger_package_partially_connected_must_not_pick_small(self):
        """**核心回歸**：24 腳件只接 20 支腳，不得被判成 20 腳封裝。

        `pins()` 只含已接腳，系統性低估；用腳數判定會穩定偏向較小的封裝。
        """
        observed = [str(i) for i in range(1, 21)]     # 20 支已接腳
        pkg, by, _c, _n = ndd_pinfn.resolve_package(self.LEGAL, observed)
        self.assertNotEqual(pkg, "PKGB20")
        self.assertEqual(by, ndd_pinfn.UNRESOLVED)

    def test_observed_fits_only_one_column(self):
        observed = ["24", "1"]                        # 只有 24 腳封裝容得下
        pkg, by, corrob, _n = ndd_pinfn.resolve_package(self.LEGAL, observed)
        self.assertEqual(pkg, "PKGA24")
        self.assertEqual(by, ndd_pinfn.AUTO_UNIQUE)
        self.assertEqual(corrob, "pin_set")

    def test_single_column_is_not_applicable(self):
        pkg, by, _c, _n = ndd_pinfn.resolve_package({"ONLY24": {"1", "2"}}, ["1"])
        self.assertEqual(by, ndd_pinfn.NOT_APPLICABLE)
        self.assertEqual(pkg, "ONLY24")

    def test_extraction_failure_must_not_fall_back_to_pin_count(self):
        pkg, by, _c, note = ndd_pinfn.resolve_package(None, ["1", "2", "3"])
        self.assertEqual(by, ndd_pinfn.UNRESOLVED)
        self.assertEqual(pkg, "")
        self.assertIn("腳數", note)

    def test_declared_package_not_in_table_is_conflict(self):
        _p, by, _c, _n = ndd_pinfn.resolve_package(self.LEGAL, ["1"], "PKGZ99")
        self.assertEqual(by, ndd_pinfn.CONFLICT)

    def test_shared_pinout_requires_matching_pin_functions(self):
        """候選封裝在**實際用到的腳上功能一致** -> 封裝不改變答案，不必問。"""
        legal = {"P1": {"1": "IN", "2": "OUT", "9": "NC"},
                 "P2": {"1": "IN", "2": "OUT", "8": "NC"}}
        _p, by, corrob, _n = ndd_pinfn.resolve_package(legal, ["1", "2"])
        self.assertEqual(by, ndd_pinfn.SHARED_PINOUT)
        self.assertEqual(corrob, "pin_function")

    def test_same_labels_different_functions_is_not_shared(self):
        """label 集合相同 != 腳位相同。這是最容易誤判成『不必問』的情況。"""
        legal = {"P1": {"1": "IN", "2": "OUT"},
                 "P2": {"1": "OUT", "2": "IN"}}
        _p, by, _c, _n = ndd_pinfn.resolve_package(legal, ["1", "2"])
        self.assertEqual(by, ndd_pinfn.UNRESOLVED)


class TestCacheMigration(unittest.TestCase):
    def test_legacy_rows_are_marked_unresolved(self):
        """舊列缺 package，被當成已解析讀入等於把歧義洗成合法資料。"""
        import csv
        import io as _io
        pj = fixtures.Project()
        with _io.open(os.path.join(pj.dir, ndd_pinfn.CACHE), "w",
                      encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=ndd_pinfn.LEGACY_COLS)
            w.writeheader()
            w.writerow({"part": "X", "pin": "1", "pin_name": "A",
                        "direction": "I", "text": "t", "source_file": "x.pdf",
                        "page": "1", "sha256": "0123456789abcdef",
                        "verified_on": "2026-01-01"})
        _p, rows, migrated = ndd_pinfn.load_cache(pj.dir)
        self.assertEqual(migrated, 1)
        self.assertEqual(rows[0]["resolved_by"], ndd_pinfn.UNRESOLVED)
        self.assertNotIn(rows[0]["resolved_by"], ndd_pinfn.RESOLVED_OK)


# ===========================================================  Commit 4  ====
class TestModelSchema(unittest.TestCase):
    def _load(self, models):
        pj = fixtures.Project().models(models)
        return load_models(pj.dir)

    def test_legacy_pairs_schema_is_rejected(self):
        with self.assertRaises(ModelError) as cm:
            self._load({"X": {"match": ["X"], "pairs": [["1", "2"]],
                              "verified_against": "a", "package_basis": "not_applicable"}})
        self.assertIn("transfer", str(cm.exception))

    def test_direction_must_be_explicit(self):
        with self.assertRaises(ModelError):
            self._load({"X": {"match": ["X"], "package_basis": "not_applicable",
                              "verified_against": "a",
                              "transfer": [{"from": ["1"], "to": ["2"]}]}})

    def test_address_pin_may_not_gate(self):
        """固定位址不得被誤推成 always —— 直接在載入時擋掉。"""
        with self.assertRaises(ModelError) as cm:
            self._load({"X": {"match": ["X"], "package_basis": "not_applicable",
                              "verified_against": "a",
                              "control": {"5": {"type": "address",
                                                "mechanism": "strap"}},
                              "transfer": [{"from": ["1"], "to": ["2"],
                                            "direction": "forward",
                                            "gate": {"all_of": ["5"]}}]}})
        self.assertIn("address", str(cm.exception))

    def test_exact_table_requires_package(self):
        with self.assertRaises(ModelError):
            self._load({"X": {"match": ["X"], "package_basis": "exact_table",
                              "verified_against": "a", "transfer": []}})

    def test_polarity_only_on_enable_like(self):
        with self.assertRaises(ModelError):
            self._load({"X": {"match": ["X"], "package_basis": "not_applicable",
                              "verified_against": "a", "transfer": [],
                              "control": {"5": {"type": "address",
                                                "polarity": "high",
                                                "mechanism": "strap"}}}})


class TestDirectionality(unittest.TestCase):
    def test_forward_buffer_cannot_be_walked_backwards(self):
        """初版 `pairs` 對稱，可從 output 逆走回 input。"""
        m = fixtures.buffer_model()
        edges = transfer_edges(m)
        self.assertEqual([p for p, _e in outgoing(edges, "2")], ["18"])
        self.assertEqual(outgoing(edges, "18"), [], "不得逆向穿越單向緩衝器")

    def test_fanout_outputs_are_not_interconnected(self):
        """初版可走 B1 -> input -> B2，**憑空捏造一條不存在的路徑**。"""
        edges = transfer_edges(fixtures.fanout_model())
        self.assertEqual(outgoing(edges, "3"), [], "fanout 輸出不得反走回輸入")
        self.assertEqual(sorted(p for p, _e in outgoing(edges, "1")),
                         ["3", "5", "7"])

    def test_bidirectional_switch_still_walks_both_ways(self):
        edges = transfer_edges(fixtures.switch_model())
        self.assertEqual([p for p, _e in outgoing(edges, "2")], ["3"])
        self.assertEqual([p for p, _e in outgoing(edges, "3")], ["2"])


class TestGating(unittest.TestCase):
    def test_runtime_register_is_conditional_despite_fixed_address(self):
        """位址腳固定**不得**讓 runtime 選通的邊變成 always。"""
        m = fixtures.mux_model()
        edge = m["transfer"][0]
        g, notes = derive_gating(m, edge, lambda p, pol: (C.ALWAYS, "tied"))
        self.assertEqual(g, C.CONDITIONAL)
        self.assertTrue(any("runtime_register" in n for n in notes))

    def test_strapped_enable_at_correct_polarity_is_always(self):
        m = fixtures.buffer_model()
        g, _n = derive_gating(m, m["transfer"][0],
                             lambda p, pol: (C.ALWAYS, "tied_gnd"))
        self.assertEqual(g, C.ALWAYS)

    def test_floating_enable_is_unknown(self):
        m = fixtures.buffer_model()
        g, _n = derive_gating(m, m["transfer"][0],
                             lambda p, pol: (C.UNKNOWN, "floating"))
        self.assertEqual(g, C.UNKNOWN)

    def test_no_gate_is_always(self):
        m = fixtures.fanout_model()
        g, _n = derive_gating(m, m["transfer"][0], lambda p, pol: (C.UNKNOWN, "x"))
        self.assertEqual(g, C.ALWAYS)


class TestModelSelection(unittest.TestCase):
    def test_token_boundary_prevents_false_match(self):
        models = {"SHORT": {"match": ["ABC12"], "package_basis": "not_applicable",
                            "verified_against": "a", "transfer": []}}
        name, m, _c = select_model(models, "", "ABC1234")
        self.assertIsNone(m, "短 token 不得誤中較長的料號")

    def test_exact_mpn_wins(self):
        models = {
            "A": {"match": ["XY"], "package_basis": "not_applicable",
                  "verified_against": "a", "transfer": []},
            "B": {"match": ["XY100"], "package_basis": "not_applicable",
                  "verified_against": "a", "transfer": []}}
        name, _m, _c = select_model(models, "", "XY100")
        self.assertEqual(name, "B")

    def test_ambiguous_match_is_reported(self):
        models = {
            "A": {"match": ["ZZ9"], "package_basis": "not_applicable",
                  "verified_against": "a", "transfer": []},
            "B": {"match": ["ZZ9"], "package_basis": "not_applicable",
                  "verified_against": "a", "transfer": []}}
        _n, m, cav = select_model(models, "", "ZZ9")
        self.assertIsNone(m)
        self.assertIn("model:ambiguous", cav)

    def test_unresolved_package_blocks_transfer(self):
        """package 未解析且 basis 是 exact_table -> 不得生成正式 transfer edge。"""
        models = {"A": fixtures.buffer_model()}
        _n, edges, cav = transfer_for(models, "", "BUF_A", 20, None)
        self.assertIn("package:unresolved", cav)
        self.assertIsNone(edges)

    def test_shared_pinout_model_needs_no_package(self):
        models = {"A": fixtures.fanout_model()}     # package_basis=not_applicable
        _n, edges, cav = transfer_for(models, "", "FAN_A", 20, None)
        self.assertEqual(cav, [])
        self.assertTrue(edges)


class TestPinExistence(unittest.TestCase):
    def test_missing_pin_detected(self):
        m = fixtures.buffer_model()
        self.assertEqual(missing_pins(m, ["2", "18"]), ["1"])

    def test_same_pin_count_different_pinout_still_passes(self):
        """**必須誠實命名**：這是 pin-existence check，不是 package 驗證。"""
        m = fixtures.buffer_model()
        self.assertEqual(missing_pins(m, ["1", "2", "18"]), [])


class TestEndpoints(unittest.TestCase):
    def _board(self, endpoints=None, models=None, part_package=None):
        pj = fixtures.Project()
        pj.board("a", {"U1": "PKG24", "U2": "PKG8", "J1": "CONN"},
                 {"S1": [("J1", "1"), ("U1", "2")],
                  "S2": [("U1", "18"), ("U2", "1")],
                  "GND": [("U1", "1")]},
                 [{"Part Reference": "U1", "Manufacturer_PN": "BUF_A"},
                  {"Part Reference": "U2", "Manufacturer_PN": "ADC_A"},
                  {"Part Reference": "J1", "Manufacturer_PN": "CONN_A"}],
                 bom_scope="complete")
        pj.cfg["endpoints"] = endpoints or {}
        pj.cfg["part_package"] = part_package or {}
        return _fab(pj, models or {})

    def test_undeclared_endpoint_defaults_to_unclassified_and_stays_in_output(self):
        """初版把「沒有模型」直接當終端負載；現在仍進 loads 但必須帶 caveat。"""
        fab = self._board()
        kind, _reason, cav = fab.endpoint_of(("a", "U2", "1"))
        self.assertEqual(kind, EP_UNCLASSIFIED)
        self.assertIn("endpoint:unclassified", cav)

    def test_declared_unknown_stop_is_not_a_load(self):
        fab = self._board(endpoints={"ADC_A": "unknown_stop"})
        kind, _r, _c = fab.endpoint_of(("a", "U2", "1"))
        self.assertEqual(kind, EP_UNKNOWN_DECLARED)

    def test_model_with_unresolved_package_is_model_unusable(self):
        """有 model 但無法使用 != 尚未分類。必須是獨立狀態並記錄原因。"""
        fab = self._board(models={"B": fixtures.buffer_model()})
        kind, reason, _c = fab.endpoint_of(("a", "U1", "2"))
        self.assertEqual(kind, EP_UNKNOWN_UNUSABLE)
        self.assertEqual(reason, "package_unresolved")

    def test_resolved_package_allows_transfer(self):
        fab = self._board(models={"B": fixtures.buffer_model()},
                          part_package={"BUF_A": "PKG24"})
        self.assertIsNone(fab.endpoint_of(("a", "U1", "2")),
                          "package 鎖定後應可穿越")


class TestHintGraphSeparation(unittest.TestCase):
    def test_control_influence_is_not_a_traversable_edge(self):
        """latch/clock 影響只能出現在 hint，不得成為 BFS 的下一跳。"""
        pj = fixtures.Project()
        pj.board("a", {"U1": "PKG16"},
                 {"LOAD": [("U1", "12")], "SER": [("U1", "14")],
                  "QH": [("U1", "9")]},
                 [{"Part Reference": "U1", "Manufacturer_PN": "LATCH_A"}],
                 bom_scope="complete")
        pj.cfg["part_package"] = {"LATCH_A": "PKG16"}
        fab = _fab(pj, {"L": fixtures.latch_model()})
        nxt = [n for n, _w, _c, _g in fab.neighbours(("a", "U1", "12"))]
        self.assertNotIn(("a", "U1", "9"), nxt,
                         "control 腳不得連到輸出腳")
        hints = fab.collect_hints()
        self.assertTrue(any(h["relation"] == "control_influence" for h in hints))

    def test_stateful_part_keeps_its_verified_transfer_edge(self):
        pj = fixtures.Project()
        pj.board("a", {"U1": "PKG16"},
                 {"SER": [("U1", "14")], "QH": [("U1", "9")]},
                 [{"Part Reference": "U1", "Manufacturer_PN": "LATCH_A"}],
                 bom_scope="complete")
        pj.cfg["part_package"] = {"LATCH_A": "PKG16"}
        fab = _fab(pj, {"L": fixtures.latch_model()})
        nxt = [n for n, _w, _c, _g in fab.neighbours(("a", "U1", "14"))]
        self.assertIn(("a", "U1", "9"), nxt,
                      "元件整體是 stateful 不得抹掉已驗證的 transfer 邊")


# ===========================================================  Commit 5  ====
class TestBomAmbiguity(unittest.TestCase):
    def _bom(self, rows, scope="complete"):
        pj = fixtures.Project()
        p = fixtures.write_bom(pj.path("b.xlsx"), rows)
        return Bom(p, scope=scope)

    def test_duplicate_refdes_is_ambiguous_not_last_wins(self):
        b = self._bom([{"Part Reference": "R1", "Manufacturer_PN": "AAA"},
                       {"Part Reference": "R1", "Manufacturer_PN": "BBB"}])
        self.assertIn("R1", b.dups)
        self.assertTrue(is_ambiguous(b.of("R1")))
        self.assertTrue(is_ambiguous(b.pn("R1")))

    def test_range_is_detected_but_not_expanded(self):
        b = self._bom([{"Part Reference": "R1-R5", "Manufacturer_PN": "AAA"}])
        self.assertTrue(b.suspect_ranges)
        self.assertIsNone(b.of("R3"), "預設不得自動展開（那是猜測）")

    def test_range_expands_only_when_opted_in(self):
        pj = fixtures.Project()
        p = fixtures.write_bom(pj.path("b.xlsx"),
                               [{"Part Reference": "R1-R3",
                                 "Manufacturer_PN": "AAA"}])
        b = Bom(p, scope="complete", expand_ranges=True)
        self.assertIsNotNone(b.of("R2"))

    def test_scope_gates_dni_claim(self):
        self.assertTrue(self._bom([], "complete").scope_supports_dni())
        for sc in ("smt_only", "variant", "unknown"):
            b = self._bom([], sc)
            self.assertFalse(b.scope_supports_dni())
            self.assertIn("bom-absent", b.absent_label())

    def test_unknown_scope_is_rejected_value(self):
        with self.assertRaises(ValueError):
            self._bom([], "SMT BOM")        # 舊 bom_kind 字串不得通過


class TestAssertionsUnderAmbiguity(unittest.TestCase):
    def test_not_stuffed_fails_when_scope_insufficient(self):
        from ndd_audit import run_assertion
        pj = fixtures.Project()
        nl = Netlist(fixtures.write_asc(pj.path("a.asc"), {"R1": "R_0402"},
                                        {"N1": [("R1", "1")]}))
        bom = Bom(fixtures.write_bom(pj.path("a.xlsx"), []), scope="smt_only")
        ok, actual = run_assertion(
            {"kind": "not_stuffed", "refdes": "R1"}, nl, bom, {})
        self.assertFalse(ok)
        self.assertIn("bom-scope-insufficient", actual)

    def test_not_stuffed_fails_on_ambiguity(self):
        from ndd_audit import run_assertion
        pj = fixtures.Project()
        nl = Netlist(fixtures.write_asc(pj.path("a.asc"), {"R1": "R_0402"},
                                        {"N1": [("R1", "1")]}))
        bom = Bom(fixtures.write_bom(
            pj.path("a.xlsx"),
            [{"Part Reference": "R1", "Manufacturer_PN": "A"},
             {"Part Reference": "R1", "Manufacturer_PN": "B"}]), scope="complete")
        ok, actual = run_assertion(
            {"kind": "not_stuffed", "refdes": "R1"}, nl, bom, {})
        self.assertFalse(ok, "ambiguity 必須 FAIL，不得靜默跳過")
        self.assertIn("ambiguous", str(actual))


if __name__ == "__main__":
    unittest.main(verbosity=2)
