#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""板內隨選追蹤（`trace --board X --from Y`）。

⚠️ 這裡守的是兩件單元測試看不出來的事：

   1. **一條分支停住不得影響其他分支。** 批次 trace 的第一階段做不到這件事
      —— 給了 `stop_fn` 之後 `endpoint_of()` 根本不會被呼叫，沒走到 slot 的
      死節點連 `ends` 都進不去，於是整個起點只吐一列「未到達」。

   2. **跨板節點不得進入 path。** 若邊界標在對面那一側，對面的 mate caveat
      會污染板內結論的 confidence，階層加註還會拿對面的 refdes 去查本板的
      表——`U1`/`J2` 每塊板都有，查到的是**別塊板的**功能名，不是查不到。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
sys.path.insert(0, HERE)

import fixtures                                                # noqa: E402
import ndd                                                     # noqa: E402
import ndd_graph                                               # noqa: E402
import ndd_confidence as C                                     # noqa: E402


def _project(mate_map=None, hier=False, endpoints=None):
    """ecu.J902 -> ecu.J101 <-> fe.J2 -> fe.U1。

    ecu 上另外掛一條**會死在未建模元件上**的分支（J902.3 -> U9），用來驗
    「一條停住不影響另一條」。
    """
    pj = fixtures.Project()
    pj.board("ecu", {"J902": "CONN_P", "J101": "CONN_S", "U9": "PKG8"},
             {"SIG_A": [("J902", "1"), ("J101", "1")],
              "SIG_B": [("J902", "2"), ("J101", "2")],
              "SIG_C": [("J902", "3"), ("U9", "1")]},
             [{"Part Reference": "J902", "Manufacturer_PN": "CONN_P"},
              {"Part Reference": "J101", "Manufacturer_PN": "CONN_S"},
              {"Part Reference": "U9", "Manufacturer_PN": "MYSTERY_IC"}],
             blocks={"U9": "ECU_Misc"} if hier else None,
             pin_names={("U9", "1"): "ECU_SIDE_NAME"} if hier else None)
    pj.board("fe", {"J2": "CONN_S", "U1": "PKG8", "U9": "PKG8"},
             {"FE_A": [("J2", "1"), ("U1", "1")],
              "FE_B": [("J2", "2"), ("U1", "2")],
              "FE_C": [("U9", "1"), ("U1", "3")]},
             [{"Part Reference": "J2", "Manufacturer_PN": "CONN_S"},
              {"Part Reference": "U1", "Manufacturer_PN": "ADC_A"},
              {"Part Reference": "U9", "Manufacturer_PN": "OTHER_IC"}],
             blocks={"U1": "RX_Chain", "U9": "FE_SIDE_BLOCK"} if hier else None,
             # ⚠️ 兩塊板都有 U9，功能名刻意不同：查錯板就會看出來。
             pin_names={("U1", "1"): "AIN0", ("U9", "1"): "FE_SIDE_NAME"}
                       if hier else None)
    pj.cfg["mates"] = [["ecu", "J101", "fe", "J2"]]
    pj.cfg["mate_map"] = mate_map or {}
    if endpoints:
        pj.cfg["endpoints"] = endpoints
    pj.cfg["trace"] = {"start": [], "slot_pattern": ""}
    return pj


def _run(pj, *argv):
    cfg = pj.save()
    return ndd.main(["--config", cfg, "trace"] + list(argv))


def _cross(rows):
    """跨板那一段的列——用來確認 fixture 真的製造出了 mate caveat。"""
    return [r for r in rows if "fe " in r["end"] or r["endpoint_kind"] ==
            ndd_graph.EP_BOUNDARY]


def _rows(pj, *argv):
    """直接拿 cmd_trace 的回傳（stdout 之外還要能被程式檢查）。"""
    cfg = pj.save()
    project = ndd.Project(cfg)
    args = ndd.build_parser().parse_args(["trace"] + list(argv))
    return ndd.cmd_trace(args, project)


class TestPartialChains(unittest.TestCase):
    def test_each_branch_reported_independently(self):
        """一條走到未建模死路、一條走到對接邊界——兩條都要出現。"""
        rows = _rows(_project(), "--board", "ecu", "--from", "J902")
        ends = {r["end"] for r in rows}
        self.assertIn("U9.1", ends, "死在未建模元件上的分支不得消失")
        kinds = {r["endpoint_kind"] for r in rows}
        self.assertIn(ndd_graph.EP_UNCLASSIFIED, kinds)
        self.assertIn(ndd_graph.EP_BOUNDARY, kinds)

    def test_connector_pin_whose_only_edge_is_a_mate_still_appears(self):
        """連接器腳位在 endpoint_of 會提早回 None——不補記就會無聲消失。"""
        rows = _rows(_project(), "--board", "ecu", "--from", "J101")
        self.assertTrue(rows, "J101 至少要有輸出")
        self.assertIn(ndd_graph.EP_BOUNDARY,
                      {r["endpoint_kind"] for r in rows})


class TestBoardBoundary(unittest.TestCase):
    def test_default_stays_on_board(self):
        rows = _rows(_project(), "--board", "ecu", "--from", "J902")
        for r in rows:
            self.assertNotIn("fe ", r["end"],
                             "預設不得跨板，出現了對面板的落點：%s" % r["end"])
            self.assertNotIn("FE", (r["hops"] or "").upper(),
                             "跨板節點溜進 path：%s" % r["hops"])

    def test_follow_mates_crosses(self):
        rows = _rows(_project(), "--board", "ecu", "--from", "J902",
                     "--follow-mates")
        self.assertTrue(any("fe " in r["end"] for r in rows),
                        "--follow-mates 應該要走到對面板")

    def test_ambiguous_mate_does_not_taint_board_local_confidence(self):
        """對面 mate 排名未定案，板內結論的 confidence 不得被拉低。

        ⚠️ 這正是「邊界標在對面那一側」會踩到的坑：對面節點一旦進了 path，
           `path_caveats` 就會把 `mate:ambiguous` 算進來，於是一個根本沒跨板
           的板內結論被一個它沒依賴的跨板不確定性拉成 unknown。
        """
        clean = _rows(_project(), "--board", "ecu", "--from", "J902")
        orig = ndd_graph.Fabric._rank_decides
        ndd_graph.Fabric._rank_decides = lambda self, *a: (False, "測試強制未定案")
        try:
            amb = _rows(_project(), "--board", "ecu", "--from", "J902")
        finally:
            ndd_graph.Fabric._rank_decides = orig
        self.assertIn("mate:ambiguous",
                      " ".join(r["caveats"] for r in _cross(amb)),
                      "前提檢查：這個 fixture 真的有把對接弄成未定案")
        pick = lambda rs: {(r["end"], r["confidence"]) for r in rs
                           if r["endpoint_kind"] != ndd_graph.EP_BOUNDARY}
        self.assertEqual(pick(clean), pick(amb),
                         "板內落點的 confidence 不該受對面 mate 影響")
        for r in amb:
            if r["endpoint_kind"] != ndd_graph.EP_BOUNDARY:
                self.assertNotIn("mate:", r["caveats"],
                                 "板內落點不該背 mate 的 caveat：%r" % (r,))

    def test_hier_annotation_uses_the_nodes_own_board(self):
        """兩塊板都有 U9：查錯板會貼上對面板的功能名。"""
        rows = _rows(_project(hier=True), "--board", "ecu", "--from", "J902")
        u9 = [r for r in rows if r["end"] == "U9.1"]
        self.assertEqual(len(u9), 1)
        self.assertEqual(u9[0]["pin_name"], "ECU_SIDE_NAME")
        self.assertEqual(u9[0]["block"], "ECU_Misc")


class TestStartSpec(unittest.TestCase):
    def test_refdes_pin_starts_exactly_one_pin(self):
        rows = _rows(_project(), "--board", "ecu", "--from", "J902.3")
        self.assertEqual({r["start"] for r in rows}, {"J902.3"})

    def test_bare_refdes_expands_all_signal_pins(self):
        rows = _rows(_project(), "--board", "ecu", "--from", "J902")
        self.assertEqual({r["start"] for r in rows},
                         {"J902.1", "J902.2", "J902.3"})

    def test_net_start(self):
        rows = _rows(_project(), "--board", "ecu", "--from", "net:SIG_C")
        self.assertTrue(rows)
        self.assertEqual({r["signal"] for r in rows}, {"SIG_C"})

    def test_net_pattern_matching_many_refuses_to_pick(self):
        with self.assertRaises(SystemExit) as cm:
            _rows(_project(), "--board", "ecu", "--from", "net:SIG_*")
        self.assertIn("不替你挑", str(cm.exception))

    def test_unknown_board_and_refdes_fail_loudly(self):
        for spec, needle in ((("--board", "nope", "--from", "J902"), "沒有這塊板"),
                             (("--board", "ecu", "--from", "U404"), "沒有")):
            with self.assertRaises(SystemExit) as cm:
                _rows(_project(), *spec)
            self.assertIn(needle, str(cm.exception))


class TestNoSideEffects(unittest.TestCase):
    def test_adhoc_does_not_write_signal_chain_csv(self):
        pj = _project()
        rc = _run(pj, "--board", "ecu", "--from", "J902")
        self.assertEqual(rc, 0)
        self.assertFalse(
            os.path.exists(os.path.join(pj.dir, "export", "signal_chain.csv")),
            "隨選查詢不得寫檔")


class TestFabricStayOn(unittest.TestCase):
    """直接打 Fabric.trace —— stop_fn 做不到的那件事。"""

    def test_boundary_and_sibling_dead_end_coexist(self):
        pj = _project()
        project = ndd.Project(pj.save())
        fab = project.fabric()
        ends = fab.trace(("ecu", "J902", "1"), stay_on="ecu")
        ends.update(fab.trace(("ecu", "J902", "3"), stay_on="ecu"))
        kinds = {ep[0] for _p, ep in ends.values() if ep}
        self.assertIn(ndd_graph.EP_BOUNDARY, kinds)
        self.assertIn(ndd_graph.EP_UNCLASSIFIED, kinds)
        for node in ends:
            self.assertEqual(node[0], "ecu",
                             "stay_on 模式下不得有對面板的節點：%r" % (node,))

    def test_stay_on_none_is_unchanged(self):
        pj = _project()
        project = ndd.Project(pj.save())
        fab = project.fabric()
        a = fab.trace(("ecu", "J902", "1"))
        b = fab.trace(("ecu", "J902", "1"), stay_on=None)
        self.assertEqual(sorted(a), sorted(b))
        self.assertTrue(any(n[0] == "fe" for n in a),
                        "沒給 stay_on 時本來就會跨板")


class TestGroundIsSkippedWithoutConfig(unittest.TestCase):
    """地線不需要使用者設定就要跳過；電源軌反而不可以自作主張。

    實測的坑：`cls()` 從 v1.8.0 就認得 `AGND`/`PGND`，但 `is_power()`
    只看使用者的 `power_net_regex`，兩者沒接起來——於是一塊板 748 條
    net 裡只判出 2 條是電源，`AGND`（1052 支腳）被當成訊號，追跡穿過
    每顆 2-pin 被動件走遍全板：一條 2-pin 訊號網路追出 1642 個落點。
    """

    def _fab(self, regex=None):
        pj = fixtures.Project()
        pj.board("b", {"U1": "PKG8", "R1": "0402", "U2": "PKG8"},
                 {"AGND": [("U1", "2"), ("R1", "2"), ("U2", "2")],
                  "SIG": [("U1", "1"), ("R1", "1")],
                  "V_SENSE_CS": [("U2", "1"), ("R1", "1")]},
                 [{"Part Reference": "U1", "Manufacturer_PN": "A"},
                  {"Part Reference": "R1", "Manufacturer_PN": "R"},
                  {"Part Reference": "U2", "Manufacturer_PN": "B"}])
        if regex is not None:
            pj.cfg["power_net_regex"] = regex
        return ndd.Project(pj.save()).fabric()

    def test_agnd_pgnd_are_power_without_any_config(self):
        fab = self._fab(regex=r"^GND$")
        for n in ("GND", "AGND", "PGND", "28V_GND_PM_2", "GND_A", "DGND"):
            self.assertTrue(fab.is_power(n), n)

    def test_rails_still_need_the_users_regex(self):
        """軌的命名是專案自己的，工具不替使用者主張。"""
        fab = self._fab(regex=r"^GND$")
        for n in ("6V_R", "3P4V_P", "28V_A"):
            self.assertFalse(fab.is_power(n), n)
        fab2 = self._fab(regex=r"^(GND|\d+V_.*|\dP\dV_.*)$")
        self.assertTrue(fab2.is_power("6V_R"))

    def test_sense_and_feedback_nets_are_not_swallowed(self):
        """`cls()` 會把 `_CS`/`_FB` 判成 PWR——不可以整個接過來，
        否則它們的路徑會被安靜地砍掉。"""
        fab = self._fab(regex=r"^GND$")
        for n in ("6V_CS_PM_2", "3P4V_FB_PM_2", "3P4V_EN_PM_2"):
            self.assertFalse(fab.is_power(n), n)

    def test_kelvin_sense_grounds_are_not_auto_classified_as_power(self):
        """`cls()` 是先判地再判控制訊號，所以 `GND_SENSE` 會被它回成 GND。

        那些是 Kelvin 偵測回授一類的**訊號**，power 板上是正常設計。
        自動判錯的代價是路徑無聲消失，所以自動判地時要先排掉它們。
        """
        fab = self._fab(regex=r"^GND$")
        for n in ("GND_SENSE", "PGND_FB", "GND_EN", "AGND_CS", "GND_DET",
                  "GND_MON1"):
            self.assertEqual(ndd_graph.Fabric.cls(n), "GND",
                             "前提：cls 本來就會把它當地：%s" % n)
            self.assertFalse(fab.is_power(n),
                             "偵測/回授地不得被自動當成電源：%s" % n)

    def test_plain_grounds_still_auto_classified(self):
        fab = self._fab(regex=r"^GND$")
        for n in ("AGND", "PGND", "DGND", "GND_A", "GNDL", "28V_GND_PM_2"):
            self.assertTrue(fab.is_power(n), n)

    def test_user_regex_can_still_override_a_sense_ground(self):
        """人講的最大：擋的只是**自動**判定。"""
        fab = self._fab(regex=r"^(GND|GND_SENSE)$")
        self.assertTrue(fab.is_power("GND_SENSE"))

    def test_ground_does_not_become_a_traversal_path(self):
        fab = self._fab(regex=r"^GND$")
        ends = fab.trace(("b", "U1", "1"), stay_on="b")
        self.assertNotIn(("b", "U2", "2"), ends,
                         "訊號不得經由 AGND 走到別顆 IC")


if __name__ == "__main__":
    unittest.main()
