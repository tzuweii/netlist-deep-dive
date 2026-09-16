#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""零件分類：順位、來源標示、以及「不猜」。

⚠️ 這裡最重要的不是「分類對不對」，是**分類是從哪裡來的**。CIS 查表命中是
   事實，refdes 前綴推出來的是推論——`source` 一旦沒帶出去，推論就會被當成
   事實引用，而且沒有任何症狀。
"""
import io
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
sys.path.insert(0, HERE)

import fixtures                                                # noqa: E402
import ndd                                                     # noqa: E402
import ndd_classify as K                                       # noqa: E402

CIS_CSV = (u'"Number","Manufacturer_PN","classify_str","Part_Type_CIS",'
           u'"Parts_Description"\n'
           u'"1E09010001","ADC_A","1E0901",'
           u'"1E09_Integrated Circuits\\1E0901_Data Acquisition","ADC"\n'
           u'"1M0000001","SHIELD_FRAME_9","1M0001","1M_ME\\1M0001_Shielding",'
           u'"屏蔽罩"\n'
           u'"1E04010001","CONN_S","1E0401",'
           u'"1E04_Connectors, Interconnects\\1E0401_Header","連接器"\n')


def _cis(tmpdir):
    p = os.path.join(tmpdir, "cis.csv")
    with io.open(p, "w", encoding="utf-8") as fh:
        fh.write(CIS_CSV)
    return K.CisIndex(p)


class TestPrefix(unittest.TestCase):
    def test_universal_prefixes(self):
        for rd, cat in (("U12", "ic"), ("Q3", "semiconductor_discrete"),
                        ("D7", "semiconductor_discrete"), ("R101", "passive"),
                        ("C55", "passive"), ("L2", "passive"),
                        ("FB4", "passive"), ("X1", "crystal"),
                        ("Y2", "crystal"), ("J9", "connector"),
                        ("TP33", "test_point"), ("H1", "mechanical"),
                        ("MH2", "mechanical"), ("FM5", "fiducial"),
                        ("F8", "circuit_protection"), ("S1", "switch"),
                        ("K4", "relay"), ("T6", "transformer"),
                        ("W3", "cable")):
            got, src = K.classify(rd, "", "b", {}, None)
            self.assertEqual(got, cat, rd)
            self.assertEqual(src, K.SRC_PREFIX,
                             "前綴推出來的一定要標成推論：%s" % rd)

    def test_company_specific_labels_are_not_hardcoded(self):
        """`ME`/`FUSE`/`Coupler` 這些是某家公司的習慣，寫死就是遷就單一專案。"""
        for rd in ("ME1", "FUSE2", "OPEN3", "Coupler1", "Cal2", "Main1",
                   "GPIO4", "DAC1", "UC2", "AT3", "CS1", "PO2", "CA5",
                   "EXTOUT1", "A7", "M9"):
            got, src = K.classify(rd, "", "b", {}, None)
            self.assertEqual(got, K.UNRECOGNIZED, rd)
            self.assertEqual(src, K.SRC_NONE)

    def test_prefix_of(self):
        self.assertEqual(K.prefix_of("U1036"), "U")
        self.assertEqual(K.prefix_of("ME12"), "ME")
        self.assertEqual(K.prefix_of("OPEN3"), "OPEN")
        self.assertEqual(K.prefix_of(""), "")


class TestCisLookup(unittest.TestCase):
    def setUp(self):
        self.pj = fixtures.Project()
        self.cis = _cis(self.pj.dir)

    def test_hits_by_manufacturer_pn_and_by_internal_number(self):
        for pn in ("ADC_A", "1E09010001"):
            cat, src = K.classify("U1", pn, "b", {}, self.cis)
            self.assertEqual((cat, src), ("ic", K.SRC_CIS), pn)

    def test_cis_beats_prefix(self):
        """屏蔽罩的 refdes 是 `A9`（前綴查不到），CIS 說它是機構件。"""
        cat, src = K.classify("A9", "SHIELD_FRAME_9", "b", {}, self.cis)
        self.assertEqual((cat, src), ("mechanical", K.SRC_CIS))

    def test_case_and_whitespace_only_normalisation(self):
        cat, _ = K.classify("U1", "  adc_a  ", "b", {}, self.cis)
        self.assertEqual(cat, "ic")

    def test_no_fuzzy_matching(self):
        """比不到就是比不到——模糊比對會把「查錯了」偽裝成「查到了」。"""
        cat, src = K.classify("Z1", "ADC_A_VARIANT", "b", {}, self.cis)
        self.assertEqual((cat, src), (K.UNRECOGNIZED, K.SRC_NONE))

    def test_missing_snapshot_degrades_to_prefix(self):
        cis = K.CisIndex(os.path.join(self.pj.dir, "nope.csv"))
        self.assertEqual(len(cis), 0)
        cat, src = K.classify("U1", "ADC_A", "b", {}, cis)
        self.assertEqual((cat, src), ("ic", K.SRC_PREFIX))


class TestOverrides(unittest.TestCase):
    def setUp(self):
        self.pj = fixtures.Project()
        self.cis = _cis(self.pj.dir)

    def test_board_refdes_override_wins_and_is_scoped(self):
        ov = {"b1:U9": "mechanical"}
        self.assertEqual(K.classify("U9", "", "b1", ov, None)[0], "mechanical")
        self.assertEqual(K.classify("U9", "", "b2", ov, None)[0], "ic")

    def test_bare_prefix_override_bulk_classifies(self):
        ov = {"OPEN": "placeholder"}
        for rd in ("OPEN1", "OPEN2", "OPEN37"):
            cat, src = K.classify(rd, "", "b", ov, None)
            self.assertEqual((cat, src), ("placeholder", K.SRC_OVERRIDE))

    def test_override_beats_cis(self):
        ov = {"ADC_A": "placeholder"}
        cat, src = K.classify("U1", "ADC_A", "b", ov, self.cis)
        self.assertEqual((cat, src), ("placeholder", K.SRC_OVERRIDE))


class TestPlaceholderAndRelevance(unittest.TestCase):
    def test_open_style_part_numbers_are_placeholders_not_gaps(self):
        """`OPEN_0402` 是刻意保留的未貼位置，不是缺分類——不能害人去查規格書。"""
        for pn in ("OPEN_0402", "open_0603", "DNP-0402", "NC_0201"):
            cat, _src = K.classify("R1", pn, "b", {}, None)
            self.assertEqual(cat, "placeholder", pn)
        self.assertFalse(K.signal_relevant("placeholder"))

    def test_signal_relevance(self):
        for c in ("ic", "rf", "crystal", "semiconductor_discrete", "filter"):
            self.assertTrue(K.signal_relevant(c), c)
        for c in ("mechanical", "connector", "cable", "test_point", "pcb",
                  "passive", "fiducial"):
            self.assertFalse(K.signal_relevant(c), c)

    def test_unknown_category_defaults_to_relevant(self):
        """分不出來的當「可能重要」。漏查比多列一顆貴得多。"""
        self.assertTrue(K.signal_relevant(K.UNRECOGNIZED))
        self.assertTrue(K.signal_relevant("something_new"))


class TestIsConnectorParity(unittest.TestCase):
    """這段原本在 ndd.py 與 ndd_audit.py 各有一份逐字相同的實作。"""

    def test_matches_the_old_inline_logic(self):
        import re

        def old(nl, refdes):
            fp = (nl.parts.get(refdes) or "").lower()
            return fp.startswith("conn") or bool(re.match(r"^J\d", refdes))

        class NL(object):
            parts = {"J1": "CONN_HDR", "P1": "conn_smt", "U1": "QFN32",
                     "JP2": "JUMPER", "J": "misc"}

        nl = NL()
        for rd in list(nl.parts) + ["ZZ9"]:
            self.assertEqual(K.is_connector(nl, rd), old(nl, rd), rd)

    def test_both_call_sites_delegate(self):
        import ndd_audit
        class NL(object):
            parts = {"P7": "conn_x"}
        nl = NL()
        self.assertTrue(ndd._is_connector(nl, "P7"))
        self.assertTrue(ndd_audit.is_connector(nl, "P7"))


class TestCoverageIntegration(unittest.TestCase):
    def _project(self, part_class=None, with_cis=True):
        pj = fixtures.Project()
        # ⚠️ 屏蔽罩用 `A9`，不是 `H4`——`H` 前綴早就被 Netlist.MECH_RX 濾掉了，
        #    真實專案裡漏網的正是 `A`/`ME` 這種不在那條正則裡的前綴。
        pj.board("b", {"U1": "QFN32", "A9": "SHIELD", "OPEN9": "PAD"},
                 {"SIG": [("U1", "1"), ("U1", "2")],
                  "N2": [("A9", "1"), ("OPEN9", "1")]},
                 [{"Part Reference": "U1", "Manufacturer_PN": "ADC_A"},
                  {"Part Reference": "A9", "Manufacturer_PN": "SHIELD_FRAME_9"},
                  {"Part Reference": "OPEN9", "Manufacturer_PN": "WEIRD_PN"}])
        if with_cis:
            _cis(pj.dir)      # 寫檔；路徑由 cis_snapshot 指
            pj.cfg["cis_snapshot"] = "cis.csv"
        else:
            pj.cfg["cis_snapshot"] = "nope.csv"
        if part_class:
            pj.cfg["part_class"] = part_class
        return pj

    def _run(self, pj):
        project = ndd.Project(pj.save())
        args = ndd.build_parser().parse_args(["coverage"])
        return ndd.cmd_coverage(args, project)

    def test_mechanical_gets_no_datasheet_todo(self):
        agg = self._run(self._project())
        self.assertIn("SHIELD_FRAME_9", agg)
        self.assertNotIn("datasheet", agg["SHIELD_FRAME_9"]["todo"])
        self.assertIn("datasheet", agg["ADC_A"]["todo"],
                      "IC 還是要查 datasheet")

    def test_unrecognized_is_surfaced_not_guessed(self):
        agg = self._run(self._project())
        self.assertEqual(agg["WEIRD_PN"]["cats"], {K.UNRECOGNIZED})
        self.assertEqual(agg["WEIRD_PN"]["srcs"], {K.SRC_NONE})

    def test_part_class_override_resolves_it(self):
        agg = self._run(self._project(part_class={"WEIRD_PN": "placeholder"}))
        self.assertEqual(agg["WEIRD_PN"]["cats"], {"placeholder"})
        self.assertNotIn("datasheet", agg["WEIRD_PN"]["todo"])

    def test_sig_pin_count_is_computed(self):
        agg = self._run(self._project())
        self.assertEqual(agg["ADC_A"]["sig"], 2)

    def test_runs_without_cis_snapshot(self):
        agg = self._run(self._project(with_cis=False))
        self.assertEqual(agg["ADC_A"]["srcs"], {K.SRC_PREFIX},
                         "沒有快照就退回前綴推論，而且要標成推論")


class TestFootprintLayer(unittest.TestCase):
    """footprint 比 refdes 前綴準得多，而且資料本來就在 .asc 裡。"""

    def test_two_segments_beat_one(self):
        self.assertEqual(K.footprint_class("PMIC-DCDC_XYZ"), "power_module")
        self.assertEqual(K.footprint_class("PMIC-LDO_XYZ"), "ic")
        self.assertEqual(K.footprint_class("D-TVS_SMBJ"), "circuit_protection")
        self.assertEqual(K.footprint_class("D-LED_0603"), "optoelectronic")

    def test_first_segment_fallback(self):
        for fp, cat in (("ADC_PARTNO", "ic"), ("Conn_SMP", "connector"),
                        ("OSC_ECS", "crystal"), ("Ferrite_0603", "filter"),
                        ("Open_0402", "placeholder"), ("RF_Mixer", "rf")):
            self.assertEqual(K.footprint_class(fp), cat, fp)

    def test_footprint_beats_refdes_prefix(self):
        """refdes 只說得出「這是 IC」，footprint 說得出「這是濾波器」。"""
        cat, src = K.classify("U7", "", "b", {}, None, "BPF_ABF-8R075G")
        self.assertEqual((cat, src), ("filter", K.SRC_FOOTPRINT))

    def test_cis_beats_footprint(self):
        pj = fixtures.Project()
        cat, src = K.classify("U1", "ADC_A", "b", {}, _cis(pj.dir), "Conn_X")
        self.assertEqual((cat, src), ("ic", K.SRC_CIS))

    def test_footprint_is_marked_as_inference(self):
        """CIS 命中是事實，footprint 是推論——不可混用。"""
        self.assertIn(K.SRC_FOOTPRINT, K.INFERRED_SRC)
        self.assertIn(K.SRC_PREFIX, K.INFERRED_SRC)
        self.assertNotIn(K.SRC_CIS, K.INFERRED_SRC)

    def test_package_names_are_not_rules(self):
        """QFN 是封裝不是功能；`R` 是電阻前綴。那批資料學得出來不代表就能出貨。"""
        for fp in ("QFN-16", "QFN_32", "R-1Kohm", "SOT23", "BGA_676"):
            self.assertIsNone(K.footprint_class(fp), fp)

    def test_project_local_override(self):
        ov = {"PROJ01": "mechanical", "CUST-A": "pcb"}
        self.assertEqual(K.footprint_class("PROJ01_T1_SHIELD", ov),
                         "mechanical")
        self.assertEqual(K.footprint_class("CUST-A-110018", ov), "pcb")

    def test_unknown_footprint_falls_through(self):
        self.assertIsNone(K.footprint_class("Widget_9000"))
        self.assertIsNone(K.footprint_class(""))
        self.assertIsNone(K.footprint_class(None))


class TestPlaceholderTokenBoundary(unittest.TestCase):
    """實測抳到的：`NC` 前綴比對沒要求完整 token。"""

    def test_real_parts_starting_with_nc_are_not_placeholders(self):
        for pn in ("NCR2-123+", "NCS2-23+", "NCP1117", "NC7SZ125"):
            cat, _ = K.classify("U1", pn, "b", {}, None)
            self.assertNotEqual(cat, "placeholder",
                                "%s 是真零件，不是預留位置" % pn)

    def test_real_placeholders_still_match(self):
        for pn in ("OPEN_0402", "NC_0402", "DNP-0402", "DNI 0201", "OPEN"):
            cat, _ = K.classify("R1", pn, "b", {}, None)
            self.assertEqual(cat, "placeholder", pn)

    def test_no_separator_form_is_left_to_the_footprint_rule(self):
        """`OPEN0402` 刻意不比對——放行數字會讓 `NC7SZ125` 又中招。"""
        self.assertNotEqual(K.classify("R1", "OPEN0402", "b", {}, None)[0],
                            "placeholder")
        self.assertEqual(
            K.classify("R1", "OPEN0402", "b", {}, None, "Open_0402")[0],
            "placeholder")


if __name__ == "__main__":
    unittest.main()
