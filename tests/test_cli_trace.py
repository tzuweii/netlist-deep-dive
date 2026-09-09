#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cmd_trace 的端到端測試：跨板那一跳必須走 caveat 軌與已批准的對映。

⚠️ 這一類 bug 單元測試抓不到 —— `Fabric.mate` 本身是對的，錯在 `cmd_trace`
   自己用 `mate_partners()` 取對手板、然後沿用同一個 pin number 跳過去，
   繞開了 caveat 軌。實測真實專案時才發現：126 列輸出裡沒有任何一列帶
   `mate:unapproved`，而該專案有 16 組未批准對接。

   「標記不傳遞等於沒標」——所以標記必須有端到端測試。
"""
import csv
import io
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
sys.path.insert(0, HERE)

import fixtures                                                # noqa: E402
import ndd                                                     # noqa: E402


def _project(mate_map=None):
    """ecu.J902（起點）-> ecu.J101（slot）<-> fe.J2 -> 負載 U1。"""
    pj = fixtures.Project()
    pj.board("ecu", {"J902": "CONN_P", "J101": "CONN_S"},
             {"SIG_A": [("J902", "1"), ("J101", "1")],
              "SIG_B": [("J902", "2"), ("J101", "2")]},
             [{"Part Reference": "J902", "Manufacturer_PN": "CONN_P"},
              {"Part Reference": "J101", "Manufacturer_PN": "CONN_S"}])
    pj.board("fe", {"J2": "CONN_S", "U1": "PKG8"},
             {"FE_A": [("J2", "1"), ("U1", "1")],
              "FE_B": [("J2", "2"), ("U1", "2")]},
             [{"Part Reference": "J2", "Manufacturer_PN": "CONN_S"},
              {"Part Reference": "U1", "Manufacturer_PN": "ADC_A"}])
    pj.cfg["mates"] = [["ecu", "J101", "fe", "J2"]]
    pj.cfg["mate_map"] = mate_map or {}
    pj.cfg["trace"] = {"start": [{"board": "ecu", "conn": "J902", "rail": "P"}],
                       "slot_pattern": r"^J(\d)01$"}
    return pj


def _run_trace(pj):
    cfg = pj.save()
    rc = ndd.main(["--config", cfg, "trace"])
    assert rc == 0, "trace 應成功"
    p = os.path.join(pj.dir, "export", "signal_chain.csv")
    with io.open(p, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


class TestTraceCaveatPropagation(unittest.TestCase):
    def test_ambiguous_mate_caveat_reaches_the_csv(self):
        """排名決定不了時，caveat 必須傳到最終輸出（標了要有效）。"""
        import ndd_graph
        orig = ndd_graph.Fabric._rank_decides
        ndd_graph.Fabric._rank_decides = lambda self, *a: (False, "測試強制未定案")
        try:
            rows = _run_trace(_project())
            self.assertTrue(rows, "應該有輸出列")
            for r in rows:
                self.assertIn("mate:ambiguous", r["caveats"])
                self.assertEqual(r["confidence"], "unknown")
        finally:
            ndd_graph.Fabric._rank_decides = orig

    def test_decided_mate_carries_no_caveat(self):
        import ndd_graph
        orig = ndd_graph.Fabric._rank_decides
        ndd_graph.Fabric._rank_decides = lambda self, *a: (True, "測試強制定案")
        try:
            for r in _run_trace(_project()):
                self.assertNotIn("mate:", r["caveats"])
        finally:
            ndd_graph.Fabric._rank_decides = orig

    def test_approved_mate_has_no_caveat(self):
        rows = _run_trace(_project({"ecu:J101|fe:J2": {
            "approved": {"1": "1", "2": "2"},
            "evidence": "synthetic", "confidence": "confirmed"}}))
        self.assertTrue(rows)
        for r in rows:
            self.assertNotIn("mate:", r["caveats"])

    def test_approved_non_straight_map_is_actually_used(self):
        """批准的對映若不是直通，`cmd_trace` 必須照批准跳，不能沿用同 pin。"""
        rows = _run_trace(_project({"ecu:J101|fe:J2": {
            "approved": {"1": "2", "2": "1"},
            "evidence": "synthetic", "confidence": "confirmed"}}))
        got = {(r["mid"], r["far_pin"]) for r in rows}
        self.assertIn(("J101.1", "J2.2"), got)
        self.assertIn(("J101.2", "J2.1"), got)
        self.assertNotIn(("J101.1", "J2.1"), got,
                         "沿用同 pin number 等於忽略批准的對映")


if __name__ == "__main__":
    unittest.main(verbosity=2)
