#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回歸測試。

每一條都對應一個**曾經會靜默出錯**的行為（改動的理由見 CHANGELOG.md）。
全部使用合成 fixture，
不依賴任何客戶專案檔案、私有 datasheet 或特定 refdes。
"""
import io
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
sys.path.insert(0, HERE)

import fixtures                                                # noqa: E402
import ndd_confidence as C                                     # noqa: E402
import ndd_package                                             # noqa: E402
import ndd_pinfn                                               # noqa: E402
from ndd_bom import Bom, is_ambiguous                          # noqa: E402
from ndd_graph import (EP_DRIVER, EP_UNCLASSIFIED,             # noqa: E402
                       EP_UNKNOWN_DECLARED, EP_UNKNOWN_UNUSABLE,
                       Fabric, MateMapError)
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

    def test_ranking_decides_or_asks(self):
        """連接器：netlist 連得上即事實 —— 排名定案就不掛 caveat。

        初版的問題不是「沒有批准機制」，是**排名結果從未進入走圖**。
        """
        fab = _fab(self._two_boards())
        status = fab.mate_status[("a", "J1", "b", "J2")]
        self.assertIn(status, ("inferred", "ambiguous"))
        edges = fab.mate.get(("a", "J1", "1"))
        self.assertTrue(edges)
        if status == "inferred":
            self.assertEqual(edges[0][1], [], "定案就不該掛 caveat")
        else:
            self.assertIn("mate:ambiguous", edges[0][1])
        self.assertTrue(fab.mate_evidence[("a", "J1", "b", "J2")],
                        "定案與否都要留下證據字串")

    def test_approved_mate_has_no_caveat(self):
        pj = self._two_boards()
        pj.cfg["mate_map"] = {"a:J1|b:J2": {
            "approved": {"1": "1", "2": "2"}, "evidence": "synthetic",
            "confidence": "confirmed"}}
        fab = _fab(pj)
        self.assertEqual(fab.mate.get(("a", "J1", "1"))[0][1], [])
        self.assertEqual(fab.mate_status[("a", "J1", "b", "J2")], "approved")

    def test_non_injective_map_is_rejected(self):
        """兩個 A 腳映到同一個 B 腳會**靜默合併兩條 net** —— 載入時就要擋。"""
        pj = self._two_boards()
        pj.cfg["mate_map"] = {"a:J1|b:J2": {
            "approved": {"1": "1", "2": "1"}, "evidence": "synthetic",
            "confidence": "confirmed"}}
        with self.assertRaises(MateMapError) as cm:
            _fab(pj)
        self.assertIn("單射", str(cm.exception))

    def test_map_to_nonexistent_pin_is_rejected(self):
        pj = self._two_boards()
        pj.cfg["mate_map"] = {"a:J1|b:J2": {
            "approved": {"1": "1", "2": "99"}, "evidence": "synthetic",
            "confidence": "confirmed"}}
        with self.assertRaises(MateMapError):
            _fab(pj)

    def test_incomplete_map_is_rejected(self):
        """未涵蓋的腳必須明確處理，工具不替使用者假設。"""
        pj = self._two_boards()
        pj.cfg["mate_map"] = {"a:J1|b:J2": {
            "approved": {"1": "1"}, "evidence": "synthetic",
            "confidence": "confirmed"}}
        with self.assertRaises(MateMapError):
            _fab(pj)

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
    """封裝判定**只用 netlist 與 BOM**，不解析 datasheet 表格。"""

    def _board(self, nets, mpn="EXP_A", fp="GENERIC16"):
        pj = fixtures.Project()
        pj.board("a", {"U1": fp}, nets,
                 [{"Part Reference": "U1", "Manufacturer_PN": mpn}],
                 bom_scope="complete")
        return pj

    def _resolve(self, pj, models, declared=None):
        nl = Netlist(pj.path("a.asc"))
        bom = Bom(pj.path("a.xlsx"), scope="complete")
        return ndd_package.resolve(models, nl, bom, "a", "U1",
                                   Fabric.cls, declared)

    def test_topology_alone_picks_the_right_package(self):
        """電源/接地腳實際接在哪 —— 這是最有判別力的一項，且只需要 netlist。"""
        pj = self._board({"GND": [("U1", "8")], "VDD_3V3": [("U1", "16")],
                          "S1": [("U1", "1")], "S2": [("U1", "4")]},
                         mpn="EXP_APW", fp="TSSOP16_BODY")
        res = self._resolve(pj, fixtures.two_package_variants())
        self.assertEqual(res["status"], ndd_package.INFERRED)
        self.assertEqual(res["model"]["package"], "PKGA16")
        self.assertIn("package:inferred", res["caveats"],
                      "推論出來的一定要標記，不能當成事實")

    def test_the_other_package_wins_with_the_other_wiring(self):
        pj = self._board({"GND": [("U1", "6")], "VDD_3V3": [("U1", "14")],
                          "S1": [("U1", "15")], "S2": [("U1", "2")]},
                         mpn="EXP_ABS", fp="QFN16_3X3")
        res = self._resolve(pj, fixtures.two_package_variants())
        self.assertEqual(res["status"], ndd_package.INFERRED)
        self.assertEqual(res["model"]["package"], "PKGB16")

    def test_no_evidence_means_ask_the_user(self):
        """證據不足時**不得替使用者假設** —— 標 [?] 並列入待補。"""
        pj = self._board({"S1": [("U1", "1")], "S2": [("U1", "4")]},
                         mpn="EXP_A", fp="GENERIC16")
        res = self._resolve(pj, fixtures.two_package_variants())
        self.assertEqual(res["status"], ndd_package.UNRESOLVED)
        self.assertIn("package:unresolved", res["caveats"])
        self.assertIn("[?]", ndd_package.describe(res))

    def test_explicit_declaration_beats_inference(self):
        pj = self._board({"GND": [("U1", "8")], "VDD_3V3": [("U1", "16")]},
                         mpn="EXP_A")
        res = self._resolve(pj, fixtures.two_package_variants(), declared="PKGA16")
        self.assertEqual(res["status"], ndd_package.USER_CONFIRMED)
        self.assertEqual(res["caveats"], [])

    def test_declaration_contradicting_netlist_is_reported_not_obeyed(self):
        """人講的最大，但**矛盾要講出來**，不能默默照單全收。"""
        pj = self._board({"GND": [("U1", "6")], "VDD_3V3": [("U1", "14")]},
                         mpn="EXP_A")
        res = self._resolve(pj, fixtures.two_package_variants(), declared="PKGA16")
        self.assertEqual(res["status"], ndd_package.CONFLICT)
        self.assertIn("package:conflict", res["caveats"])

    def test_single_candidate_needs_no_evidence(self):
        pj = self._board({"S1": [("U1", "2")], "GND": [("U1", "10")],
                          "VDD_3V3": [("U1", "20")]}, mpn="BUF_A", fp="PKG24")
        res = self._resolve(pj, {"B": fixtures.buffer_model()})
        self.assertEqual(res["status"], ndd_package.SINGLE_CANDIDATE)
        self.assertEqual(res["caveats"], [])

    def test_unconnected_power_pin_is_a_contradiction(self):
        """訊號腳可以 NC，**電源/接地腳不會**。宣告的 VSS/VDD 沒接就是腳位對不上。

        這條規則是拓樸判別力的主要來源——即使只有一個候選模型也要擋。
        """
        pj = self._board({"S1": [("U1", "2")]}, mpn="BUF_A", fp="PKG24")
        res = self._resolve(pj, {"B": fixtures.buffer_model()})
        self.assertEqual(res["status"], ndd_package.CONFLICT)
        self.assertIn("未接", res["evidence"])

    def test_pin_count_is_never_used_to_infer_package(self):
        """netlist 只有已接腳，用腳數推封裝會穩定偏向較小的封裝。

        這裡只接 2 支腳——若工具用腳數推論，會挑出某個小封裝；正確行為是
        因為證據不足而要求使用者補充。
        """
        pj = self._board({"S1": [("U1", "1")], "S2": [("U1", "4")]},
                         mpn="EXP_A", fp="GENERIC16")
        res = self._resolve(pj, fixtures.two_package_variants())
        self.assertEqual(res["status"], ndd_package.UNRESOLVED)


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
                              "verified_against": "a"}})
        self.assertIn("transfer", str(cm.exception))

    def test_direction_must_be_explicit(self):
        with self.assertRaises(ModelError):
            self._load({"X": {"match": ["X"], "verified_against": "a",
                              "transfer": [{"from": ["1"], "to": ["2"]}]}})

    def test_address_pin_may_not_gate(self):
        """固定位址不得被誤推成 always —— 直接在載入時擋掉。"""
        with self.assertRaises(ModelError) as cm:
            self._load({"X": {"match": ["X"], "verified_against": "a",
                              "control": {"5": {"type": "address",
                                                "mechanism": "strap"}},
                              "transfer": [{"from": ["1"], "to": ["2"],
                                            "direction": "forward",
                                            "gate": {"all_of": ["5"]}}]}})
        self.assertIn("address", str(cm.exception))

    def test_pin_roles_values_are_checked(self):
        with self.assertRaises(ModelError):
            self._load({"X": {"match": ["X"], "verified_against": "a",
                              "transfer": [], "pin_roles": {"8": "GROUND"}}})

    def test_polarity_only_on_enable_like(self):
        with self.assertRaises(ModelError):
            self._load({"X": {"match": ["X"], "verified_against": "a", "transfer": [],
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
        models = {"SHORT": {"match": ["ABC12"], "verified_against": "a", "transfer": []}}
        name, m, _c = select_model(models, "", "ABC1234")
        self.assertIsNone(m, "短 token 不得誤中較長的料號")

    def test_exact_mpn_wins(self):
        models = {
            "A": {"match": ["XY"], "verified_against": "a", "transfer": []},
            "B": {"match": ["XY100"], "verified_against": "a", "transfer": []}}
        name, _m, _c = select_model(models, "", "XY100")
        self.assertEqual(name, "B")

    def test_ambiguous_match_is_reported(self):
        models = {
            "A": {"match": ["ZZ9"], "verified_against": "a", "transfer": []},
            "B": {"match": ["ZZ9"], "verified_against": "a", "transfer": []}}
        _n, m, cav = select_model(models, "", "ZZ9")
        self.assertIsNone(m)
        self.assertIn("model:ambiguous", cav)

    def test_conflicting_declared_package_blocks_transfer(self):
        models = {"A": fixtures.buffer_model()}
        _n, edges, cav = transfer_for(models, "", "BUF_A", 20, "OTHER_PKG")
        self.assertIn("package:conflict", cav)
        self.assertIsNone(edges)

    def test_model_usable_without_any_package_declaration(self):
        models = {"A": fixtures.fanout_model()}
        _n, edges, cav = transfer_for(models, "", "FAN_A", 20, None)
        self.assertEqual(cav, [])
        self.assertTrue(edges)


class TestExampleModels(unittest.TestCase):
    """範例模型必須能通過驗證，且**不得被自動載入**。"""

    def _examples(self):
        p = os.path.join(os.path.dirname(HERE), "references",
                         "example-models.json")
        raw = json.load(io.open(p, encoding="utf-8"))
        return {k: v for k, v in raw.items() if not k.startswith("_")}

    def test_all_examples_validate(self):
        from ndd_models import _validate
        for k, m in self._examples().items():
            _validate(k, dict(m))          # 不得拋例外

    def test_examples_are_not_auto_loaded(self):
        """模型是『某人對 datasheet 的解讀』，不能默默塞進每個專案。"""
        pj = fixtures.Project()
        self.assertEqual(load_models(pj.dir), {},
                         "沒有 models.json 時就該是空的")

    def test_unidirectional_examples_are_forward(self):
        """初版用對稱 pairs，緩衝器與 fanout 會被反向走。"""
        ex = self._examples()
        for name in ("SN74HCS244", "PI49FCT3807"):
            for e in ex[name]["transfer"]:
                self.assertEqual(e["direction"], "forward", name)
        for e in ex["SN74CBTLV3126"]["transfer"]:
            self.assertEqual(e["direction"], "bidirectional",
                             "FET switch 確實雙向")

    def test_mux_edges_are_runtime_gated(self):
        """I2C mux 的通道是軟體選通，不得被推成 always。"""
        m = self._examples()["PCA9547"]
        for e in m["transfer"]:
            self.assertIn("channel_selected", e["gate"]["all_of"])
        self.assertIn("channel_selected", m["runtime_conditions"])

    def test_fanout_outputs_are_not_interconnected(self):
        ex = self._examples()["PI49FCT3807"]
        edges = transfer_edges(ex)
        self.assertEqual(outgoing(edges, "3"), [],
                         "兩個時脈輸出之間不得有路徑")


class TestPinExistence(unittest.TestCase):
    def test_partially_unconnected_pins_are_not_an_error(self):
        """實測真實板子：8 通道緩衝器只用 6 個，未用通道的腳本來就不接。

        netlist 只含已接腳，所以「有任何一支缺」不能當錯誤——那會對每一顆
        有未用通道的元件誤報。
        """
        m = fixtures.buffer_model()
        self.assertEqual(missing_pins(m, ["2", "18"]), [],
                         "部分腳未接是正常設計")
        from ndd_models import unused_pins
        self.assertEqual(unused_pins(m, ["2", "18"]), ["1"])

    def test_model_on_a_completely_different_part_is_detected(self):
        """完全沒有交集 = 模型套錯元件（或腳號格式不相容，如數字套到 BGA）。"""
        m = fixtures.buffer_model()
        self.assertTrue(missing_pins(m, ["A1", "B2", "C3"]))

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
        self.assertEqual(cav, [], "未分類是覆蓋率缺口，不是路徑證據不足")
        import ndd_confidence
        self.assertNotIn("endpoint:unclassified", ndd_confidence.CAVEAT_CEILING,
                         "不得讓它降級 confidence —— 會有 96% 的列一起降級")

    def test_declared_unknown_stop_is_not_a_load(self):
        fab = self._board(endpoints={"ADC_A": "unknown_stop"})
        kind, _r, _c = fab.endpoint_of(("a", "U2", "1"))
        self.assertEqual(kind, EP_UNKNOWN_DECLARED)

    def test_ambiguous_package_is_model_unusable_not_unclassified(self):
        """有模型但封裝無法定案 != 尚未分類。必須是獨立狀態並記錄原因。"""
        pj = fixtures.Project()
        pj.board("a", {"U1": "GENERIC16", "J1": "CONN"},
                 {"S1": [("J1", "1"), ("U1", "1")], "S2": [("U1", "4")]},
                 [{"Part Reference": "U1", "Manufacturer_PN": "EXP_A"},
                  {"Part Reference": "J1", "Manufacturer_PN": "CONN_A"}],
                 bom_scope="complete")
        fab = _fab(pj, fixtures.two_package_variants())
        kind, reason, _c = fab.endpoint_of(("a", "U1", "1"))
        self.assertEqual(kind, EP_UNKNOWN_UNUSABLE)
        self.assertEqual(reason, "package_unresolved")

    def test_topology_evidence_makes_the_model_usable(self):
        """加上電源/接地腳的接法之後，封裝可由 netlist 推定，模型就能用。"""
        pj = fixtures.Project()
        pj.board("a", {"U1": "TSSOP16_BODY", "J1": "CONN"},
                 {"S1": [("J1", "1"), ("U1", "1")], "S2": [("U1", "4")],
                  "GND": [("U1", "8")], "VDD_3V3": [("U1", "16")]},
                 [{"Part Reference": "U1", "Manufacturer_PN": "EXP_APW"},
                  {"Part Reference": "J1", "Manufacturer_PN": "CONN_A"}],
                 bom_scope="complete")
        fab = _fab(pj, fixtures.two_package_variants())
        self.assertIsNone(fab.endpoint_of(("a", "U1", "1")),
                          "封裝推定後應可穿越")
        cav = fab.pkg_res[("a", "U1")]["caveats"]
        self.assertIn("package:inferred", cav, "推論仍要標記")


class TestDriverEndpoint(unittest.TestCase):
    def test_buffer_output_reached_backwards_is_a_driver_not_a_load(self):
        """反向走到緩衝器的輸出腳 = 訊號來源，不是負載。

        對稱模型看不到這件事（會直接穿過去）；有向模型才分得出來。
        把驅動器算進 loads 等於把「誰送出這條訊號」講成「誰在收」。
        """
        pj = fixtures.Project()
        pj.board("a", {"U1": "PKG24", "U2": "PKG8"},
                 {"IN": [("U1", "2")], "OUT": [("U1", "18"), ("U2", "1")],
                  "GND": [("U1", "1"), ("U1", "10")],
                  "VDD_3V3": [("U1", "20")]},
                 [{"Part Reference": "U1", "Manufacturer_PN": "BUF_A"},
                  {"Part Reference": "U2", "Manufacturer_PN": "ADC_A"}],
                 bom_scope="complete")
        fab = _fab(pj, {"B": fixtures.buffer_model()})
        kind, reason, _c = fab.endpoint_of(("a", "U1", "18"))
        self.assertEqual(kind, EP_DRIVER)
        self.assertIn("驅動端", reason)
        import ndd_graph
        self.assertNotIn(EP_DRIVER, ndd_graph.EP_IN_LOADS)

    def test_input_side_still_traverses(self):
        pj = fixtures.Project()
        pj.board("a", {"U1": "PKG24"},
                 {"IN": [("U1", "2")], "OUT": [("U1", "18")],
                  "GND": [("U1", "1"), ("U1", "10")],
                  "VDD_3V3": [("U1", "20")]},
                 [{"Part Reference": "U1", "Manufacturer_PN": "BUF_A"}],
                 bom_scope="complete")
        fab = _fab(pj, {"B": fixtures.buffer_model()})
        self.assertIsNone(fab.endpoint_of(("a", "U1", "2")))


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

    def test_bom_is_authoritative_by_default(self):
        """使用者給的 BOM 就是這塊板的權威 —— 缺席即未貼件，不做變體推理。"""
        for sc in ("complete", "variant", "unknown", "smt_only"):
            b = self._bom([], sc)
            self.assertEqual(b.absent_label(), "未貼件 (DNI)")

    def test_smt_bom_does_not_cover_non_smt_classes(self):
        """唯一的例外是**文件涵蓋範圍**，不是 BOM 的正確性。"""
        smt = self._bom([], "smt_only")
        self.assertTrue(smt.covers_class(is_mech=False, is_connector=False))
        self.assertFalse(smt.covers_class(is_mech=True, is_connector=False))
        self.assertFalse(smt.covers_class(is_mech=False, is_connector=True))
        full = self._bom([], "complete")
        self.assertTrue(full.covers_class(is_mech=True, is_connector=True))

    def test_unknown_scope_is_rejected_value(self):
        with self.assertRaises(ValueError):
            self._bom([], "SMT BOM")        # 舊 bom_kind 字串不得通過


class TestAssertionsUnderAmbiguity(unittest.TestCase):
    def test_smt_part_absent_from_smt_bom_is_provable_dni(self):
        """SMT BOM 本來就該列出所有 SMT 件，所以 SMT 件缺席**是有意義的**。"""
        from ndd_audit import run_assertion
        pj = fixtures.Project()
        nl = Netlist(fixtures.write_asc(pj.path("a.asc"), {"R1": "R_0402"},
                                        {"N1": [("R1", "1")]}))
        bom = Bom(fixtures.write_bom(pj.path("a.xlsx"), []), scope="smt_only")
        ok, _a = run_assertion(
            {"kind": "not_stuffed", "refdes": "R1"}, nl, bom, {})
        self.assertTrue(ok)

    def test_connector_absent_from_smt_bom_proves_nothing(self):
        """連接器/手插件本來就不在 SMT BOM 範圍內，缺席不代表沒貼。"""
        from ndd_audit import run_assertion
        pj = fixtures.Project()
        nl = Netlist(fixtures.write_asc(pj.path("a.asc"), {"J1": "Conn_XYZ"},
                                        {"N1": [("J1", "1")]}))
        bom = Bom(fixtures.write_bom(pj.path("a.xlsx"), []), scope="smt_only")
        ok, actual = run_assertion(
            {"kind": "not_stuffed", "refdes": "J1"}, nl, bom, {})
        self.assertFalse(ok)
        self.assertIn("bom-scope-insufficient", actual)

    def test_variant_bom_still_proves_dni(self):
        """不做變體推理 —— variant 只是 BOM 的身分註記，判讀等同 complete。"""
        from ndd_audit import run_assertion
        pj = fixtures.Project()
        nl = Netlist(fixtures.write_asc(pj.path("a.asc"), {"R1": "R_0402"},
                                        {"N1": [("R1", "1")]}))
        bom = Bom(fixtures.write_bom(pj.path("a.xlsx"), []), scope="variant")
        ok, _a = run_assertion(
            {"kind": "not_stuffed", "refdes": "R1"}, nl, bom, {})
        self.assertTrue(ok)


class TestNetClass(unittest.TestCase):
    """`Fabric.cls` 的地線／電源軌命名慣例。

    曾經只認 `GND` / `*_GND` 與帶 `VDD`/`VCC` 的名字，於是 `AGND`、`PGND`、
    `GND_A`、`3P4V_P`、`6V_C`、`28V_A` 全被判成 SIG。後果有兩層，而且都**不會
    報錯**：`mate` 把電源腳對電源腳算成矛盾，排名因此定不了案；`pin_roles`
    宣告的 VSS/VDD 對不上 netlist，封裝判定變成 conflict。
    """

    def test_ground_aliases(self):
        for n in ("GND", "AGND", "DGND", "PGND", "PGND_R", "GND_A", "GND_B",
                  "GND_EARTH", "28V_GND_PM_2", "GND1",
                  # v1.8.1：尾綴裝飾。T_RADAR_T1 的 48 V 電源入口就是這樣寫的，
                  # 對面那塊板寫 `GLOBAL_GND`，6 腳裡 3 腳被誤計成矛盾。
                  "GLOBAL_GNDL", "GLOBAL_GNDR", "GNDA", "PGND_R2",
                  "GND_EARTH2", "GND_RJ45", "LED_R_GND"):
            self.assertEqual(Fabric.cls(n), "GND", n)

    def test_rail_naming_conventions(self):
        for n, want in (("3P4V_P", "PWR:3P4V"), ("3P3V_C", "PWR:3P3V"),
                        ("6V_C", "PWR:6V"), ("28V_A", "PWR:28V"),
                        ("VDD_3V3_P", "PWR:3V3"), ("MRAM_3V3_DPU", "PWR:3V3"),
                        ("FPGA_MGTAVTT_1V2_DPU", "PWR:1V2")):
            self.assertEqual(Fabric.cls(n), want, n)

    def test_control_signals_beat_rails(self):
        """名字帶電壓不代表是電源軌 —— enable / power-good 必須是 SIG。"""
        for n in ("28V_EN_PM_2", "TX_EN", "TX_PG", "3P3V_PG_DPU", "VDD_3V3_EN"):
            self.assertEqual(Fabric.cls(n), "SIG", n)

    def test_no_substring_false_positives(self):
        """只認整個 token。`TX_PGA_LOAD` 含 `PG`，但它是訊號不是 power-good。"""
        for n in ("TX_PGA_LOAD", "TX_CLK_PS_P", "180DEG_BIT_TX1",
                  "FPGA_FLAG_P", "SERDESA_RTN"):
            self.assertEqual(Fabric.cls(n), "SIG", n)

    def test_negative_rails(self):
        """負電壓軌 —— `\d+V` 吃不到開頭的減號，`FE_-5V_VIN` 會掉成 SIG，
        `FE_VDD_-5V` 則靠 VDD 兜底落到無電壓標的 `PWR`，跨板對不起來。"""
        for n, want in (("FE_VDD_-5V", "PWR:-5V"), ("FE_-5V_VIN", "PWR:-5V"),
                        ("SNS_VDD_-5V_P", "PWR:-5V"), ("-5V_A", "PWR:-5V")):
            self.assertEqual(Fabric.cls(n), want, n)
        # 控制訊號仍然優先 —— 名字帶 -5V 不代表它是 -5 V 軌。
        for n in ("FE_-5V_EN", "-5V_PG", "-5V_PG_B"):
            self.assertEqual(Fabric.cls(n), "SIG", n)

    def test_bare_vdd_without_voltage(self):
        for n in ("A_VDD25", "XO_100MHZ_VDD_DPU", "TCXO_100MHZ_VDD"):
            self.assertEqual(Fabric.cls(n), "PWR", n)

    def test_none_stays_none(self):
        self.assertIsNone(Fabric.cls(None))


class TestMateForm(unittest.TestCase):
    """對接前提：板子都實際接過可以用 —— 只判斷「怎麼接」。

    公母直接對接（兩側系列不同）一律直通；線束（兩側同系列）逐腳比 net 名。
    """

    def _pair(self, fa, fb, nets_a, nets_b):
        """nets_x: {pin: net}。兩塊板各一顆連接器，宣告成一組 mate。"""
        pj = fixtures.Project()
        for key, rd, fp, nets in (("a", "J1", fa, nets_a), ("b", "J2", fb, nets_b)):
            pj.board(key, {rd: fp},
                     {n: [(rd, p)] for p, n in nets.items()},
                     [{"Part Reference": rd, "Manufacturer_PN": fp}])
        pj.cfg["mates"] = [["a", "J1", "b", "J2"]]
        return _fab(pj)

    def test_family_ignores_library_prefix(self):
        self.assertEqual(Fabric.family("Conn_SEAF-20-05p0-L-04-1-A-K-TR"), "SEAF")
        self.assertEqual(Fabric.family("Conn-Header_T2M-115-01-L-D-TH"), "T2M")
        self.assertEqual(Fabric.family("Conn_B04_UEC5-019-1-H-D-RA-2-A"), "UEC5")

    def test_direct_mate_is_straight_despite_contradictions(self):
        """實例 interposer.J3↔DPU.J2001：兩側對同一條軌的電壓命名不同，
        矛盾不得否決直通。"""
        a = {"1": "SIG_A", "2": "SIG_B", "3": "SIG_C", "4": "SIG_D", "5": "3P4V_C"}
        b = {"1": "SIG_A", "2": "SIG_B", "3": "SIG_C", "4": "SIG_D", "5": "3P3V_C"}
        fab = self._pair("Conn_SEAF-1", "Conn_SEAM-1", a, b)
        self.assertEqual(fab.mate_status[("a", "J1", "b", "J2")], "inferred")
        self.assertEqual(fab.mate[("a", "J1", "5")][0][0], ("b", "J2", "5"))

    def test_direct_mate_without_evidence_asks(self):
        """相符的 net 名不夠 —— 確認不了這兩顆是一對。"""
        a = {str(i): "X%d" % i for i in range(1, 9)}
        b = {str(i): "Y%d" % i for i in range(1, 9)}
        fab = self._pair("Conn_SEAF-1", "Conn_SEAM-1", a, b)
        self.assertEqual(fab.mate_status[("a", "J1", "b", "J2")], "ambiguous")

    def test_harness_follows_names_even_when_crossed(self):
        """線束可以任意接：名稱唯一對上就照名稱接，不管同不同號。"""
        a = {"1": "TX", "2": "RX", "3": "GND"}
        b = {"1": "RX", "2": "TX", "3": "GND"}
        fab = self._pair("Conn_T2M-105", "Conn_T2M-105", a, b)
        self.assertEqual(fab.mate_status[("a", "J1", "b", "J2")], "inferred")
        self.assertEqual(fab.mate[("a", "J1", "1")][0][0], ("b", "J2", "2"))
        self.assertEqual(fab.mate[("a", "J1", "2")][0][0], ("b", "J2", "1"))

    def test_harness_with_unmatched_signal_asks(self):
        """TX/RX 各自從自己的角度命名 —— netlist 裡沒有線束接法。"""
        a = {"1": "T1_TX", "2": "T1_RX"}
        b = {"1": "T2_TX", "2": "T2_RX"}
        fab = self._pair("Conn_UEC5-019", "Conn_UEC5-019", a, b)
        self.assertEqual(fab.mate_status[("a", "J1", "b", "J2")], "ambiguous")
        self.assertIn("mate:ambiguous", fab.mate[("a", "J1", "1")][0][1])

    def test_power_only_harness_passes(self):
        """只有電源／地：接法不影響任何訊號。"""
        a = {"1": "VDD_6V_R", "2": "GND"}
        b = {"1": "6V_R", "2": "AGND"}
        fab = self._pair("Conn-Header_T2M-115", "Conn-Header_T2M-115", a, b)
        self.assertEqual(fab.mate_status[("a", "J1", "b", "J2")], "inferred")

    def test_phys_size_ignores_unconnected_pins(self):
        """空腳不在 `pins()` 裡 —— 大小要看最大腳號，不能看已接腳數。"""
        self.assertEqual(Fabric.phys_size(["1", "2", "10"]), 10)
        self.assertEqual(Fabric.phys_size(["A1", "A10", "D3"]), 40)


class TestMateContradiction(unittest.TestCase):
    """`Fabric.contradicts` —— 「不認得名字」不等於「兩塊板對不上」。

    v1.8 的 `rank_mating` 直接比 `cls(a) != cls(b)`，而 `cls` 的 `SIG` 是
    catch-all：真訊號與**任何沒見過的電源／地寫法**都落在那裡。於是每遇到
    一種新的命名慣例就會憑空生出矛盾，而 `_decide_mate` 只要一支矛盾腳就
    擋掉 `inferred`。逐板補正規式追不完，所以改成：雙方都要被正面辨識成
    電源／地，才算矛盾。
    """

    def test_real_contradictions_still_count(self):
        for a, b in (("GND", "PWR:5V"), ("PWR:3V3", "PWR:5V"),
                     ("PWR:48V", "GND"), ("PWR:1V8", "PWR:1V2")):
            self.assertTrue(Fabric.contradicts(a, b), (a, b))
            self.assertTrue(Fabric.contradicts(b, a), (b, a))

    def test_unrecognized_is_not_counter_evidence(self):
        """本版的核心主張。`SIG` 可能只是「我沒見過這種寫法」。"""
        for a, b in (("GND", "SIG"), ("PWR:5V", "SIG"), ("SIG", "SIG"),
                     ("PWR", "SIG")):
            self.assertFalse(Fabric.contradicts(a, b), (a, b))
            self.assertFalse(Fabric.contradicts(b, a), (b, a))

    def test_untagged_pwr_is_weak_evidence(self):
        """無電壓標的 `PWR` 只是「名字帶 VDD/VCC」，不足以推翻帶標的那側。"""
        self.assertFalse(Fabric.contradicts("PWR", "PWR:5V"))
        self.assertFalse(Fabric.contradicts("PWR:5V", "PWR"))

    def test_unconnected_pin_is_not_a_contradiction(self):
        for a in ("GND", "PWR:5V", "SIG", None):
            self.assertFalse(Fabric.contradicts(None, a), a)
            self.assertFalse(Fabric.contradicts(a, None), a)

    def test_split_ground_spelling_no_longer_contradicts(self):
        """端到端：兩塊板對同一支連接器腳，一邊 `GLOBAL_GNDR`、一邊
        `GLOBAL_GND`。⚠️ 這裡刻意用 v1.8 的舊 `cls` 結果（`SIG` vs `GND`）
        當輸入 —— 證明規則本身就擋得住，不依賴正規式有沒有放寬。
        """
        self.assertFalse(Fabric.contradicts("SIG", "GND"))
        # 放寬之後兩邊都認得，而且認成同一類
        self.assertEqual(Fabric.cls("GLOBAL_GNDR"), Fabric.cls("GLOBAL_GND"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
