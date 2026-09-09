#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`init` 的端到端測試。

設計目標：**下完 init 就能開始問電路問題**。所以測的是「一路跑完、該產的
檔案都在」，而不是個別步驟的正確性（那些有各自的測試）。

只有兩件事該停下來問人：BOM 配對有疑慮、要不要下載 datasheet。其餘一律跑完。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
sys.path.insert(0, HERE)

import fixtures                                                # noqa: E402
import ndd                                                     # noqa: E402


def _folder(two_boards=True, ambiguous_bom=False):
    """建一個只有 .asc 與 .xlsx 的資料夾（模擬使用者剛丟檔案進來）。"""
    d = tempfile.mkdtemp(prefix="ndd_init_")
    # a 板：起點連接器 + 12 腳對接連接器
    fixtures.write_asc(
        os.path.join(d, "board_a.asc"),
        {"JX": "Conn_HDR", "U1": "PKG8"},
        {"SIG_%d" % i: [("JX", str(i)), ("U1", str(min(i, 8)))] for i in range(1, 9)})
    rows_a = [{"Part Reference": "JX", "Manufacturer_PN": "CONN_X"},
              {"Part Reference": "U1", "Manufacturer_PN": "ADC_A"}]
    fixtures.write_bom(os.path.join(d, "bom_a.xlsx"), rows_a)
    if two_boards:
        fixtures.write_asc(
            os.path.join(d, "board_b.asc"),
            {"JY": "Conn_RCPT", "U9": "PKG8"},
            {"SIG_%d" % i: [("JY", str(i)), ("U9", str(min(i, 8)))]
             for i in range(1, 9)})
        rows_b = [{"Part Reference": "JY", "Manufacturer_PN": "CONN_Y"},
                  {"Part Reference": "U9", "Manufacturer_PN": "DAC_A"}]
        if ambiguous_bom:
            # 讓 b 板的 BOM 也含 a 板的 refdes -> 兩份 BOM 都像是 a 板的
            rows_b += rows_a
        fixtures.write_bom(os.path.join(d, "bom_b.xlsx"), rows_b)
    return d


class TestInitPlan(unittest.TestCase):
    def test_plan_writes_nothing(self):
        d = _folder()
        before = sorted(os.listdir(d))
        self.assertEqual(ndd.main(["init", d, "--plan"]), 0)
        self.assertEqual(sorted(os.listdir(d)), before,
                         "--plan 不得寫出任何檔案")


class TestInitRun(unittest.TestCase):
    def test_run_produces_everything_needed_to_start_asking(self):
        """一路跑完，該產的檔案都在——中途不再問任何問題。"""
        d = _folder()
        self.assertEqual(
            ndd.main(["init", d, "--run", "--no-datasheets", "--accept-mates"]), 0)
        for f in ("ndd.json", "SETUP.md", "MANIFEST.md", "REVIEW.md"):
            self.assertTrue(os.path.exists(os.path.join(d, f)), "缺 %s" % f)
        self.assertTrue(os.path.isdir(os.path.join(d, "export")))
        self.assertTrue(os.path.exists(
            os.path.join(d, "datasheets", "MISSING.md")),
            "跳過下載時仍要產生缺件清單")

    def test_config_has_every_field_even_when_empty(self):
        """未出現在骨架裡的欄位，使用者不會知道它存在。"""
        d = _folder()
        ndd.main(["init", d, "--run", "--no-datasheets"])
        cfg = json.load(io.open(os.path.join(d, "ndd.json"), encoding="utf-8"))
        for k in ("boards", "mates", "mate_map", "part_package", "endpoints",
                  "power_net_regex", "net_normalize", "trace", "assertions",
                  "role_rules", "datasheets"):
            self.assertIn(k, cfg)
        b = list(cfg["boards"].values())[0]
        for k in ("label", "asc", "bom", "bom_scope", "sheet", "ref_col",
                  "expand_ranges"):
            self.assertIn(k, b)

    def test_setup_md_records_what_is_still_missing(self):
        d = _folder()
        ndd.main(["init", d, "--run", "--no-datasheets"])
        txt = io.open(os.path.join(d, "SETUP.md"), encoding="utf-8").read()
        for section in ("自動決定的", "你確認過的", "流程執行結果", "需要你補的"):
            self.assertIn(section, txt)
        self.assertIn("- [ ]", txt, "待補事項要是可打勾的清單")

    def test_low_confidence_pairing_stops_and_asks(self):
        """BOM 配對有疑慮時**必須**停下來問，不能自己選。"""
        d = _folder(ambiguous_bom=True)
        with self.assertRaises(SystemExit) as cm:
            ndd.main(["init", d, "--run", "--no-datasheets"])
        self.assertIn("配對信心不足", str(cm.exception))
        self.assertFalse(os.path.exists(os.path.join(d, "ndd.json")),
                         "未確認前不得寫出設定")

    def test_explicit_bom_override_unblocks(self):
        d = _folder(ambiguous_bom=True)
        keys = []
        # 先用 --plan 取得 board key
        ndd.main(["init", d, "--plan"])
        cfgless = [f for f in os.listdir(d) if f.endswith(".asc")]
        self.assertTrue(cfgless)
        rc = ndd.main(["init", d, "--run", "--no-datasheets",
                       "--accept-pairing"])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(os.path.join(d, "ndd.json")))

    def test_existing_config_is_not_overwritten_without_force(self):
        d = _folder()
        ndd.main(["init", d, "--run", "--no-datasheets"])
        with self.assertRaises(SystemExit):
            ndd.main(["init", d, "--run", "--no-datasheets"])

    def test_single_board_project_still_completes(self):
        """只有一塊板時沒有對接可偵測，但流程仍要跑完。"""
        d = _folder(two_boards=False)
        self.assertEqual(ndd.main(["init", d, "--run", "--no-datasheets"]), 0)
        self.assertTrue(os.path.exists(os.path.join(d, "SETUP.md")))


class TestMigrate(unittest.TestCase):
    """v0 專案設定升級。**只改設定，不動原始檔。**"""

    def _v0_project(self):
        d = _folder(two_boards=False)
        ndd.main(["init", d, "--run", "--no-datasheets"])
        p = os.path.join(d, "ndd.json")
        cfg = json.load(io.open(p, encoding="utf-8"))
        for b in cfg["boards"].values():          # 退回 v0 格式
            b["bom_kind"] = "SMT BOM（不含手插件）"
            for f in ("bom_scope", "sheet", "expand_ranges"):
                b.pop(f, None)
        for f in ("mate_map", "part_package", "endpoints"):
            cfg.pop(f, None)
        io.open(p, "w", encoding="utf-8").write(
            json.dumps(cfg, ensure_ascii=False, indent=2))
        return d, p

    def test_v0_config_is_rejected_before_migrating(self):
        d, p = self._v0_project()
        with self.assertRaises(SystemExit) as cm:
            ndd.main(["--config", p, "audit"])
        self.assertIn("bom_kind", str(cm.exception))

    def test_migrate_maps_scope_and_backs_up(self):
        d, p = self._v0_project()
        self.assertEqual(ndd.main(["migrate", d]), 0)
        cfg = json.load(io.open(p, encoding="utf-8"))
        b = list(cfg["boards"].values())[0]
        self.assertEqual(b["bom_scope"], "smt_only", "含 SMT 字樣要對到 smt_only")
        self.assertNotIn("bom_kind", b)
        for f in ("mate_map", "part_package", "endpoints"):
            self.assertIn(f, cfg)
        self.assertTrue(os.path.exists(p + ".v0.bak"), "原檔要備份")

    def test_migrate_is_idempotent(self):
        d, p = self._v0_project()
        ndd.main(["migrate", d])
        before = io.open(p, encoding="utf-8").read()
        ndd.main(["migrate", d])
        self.assertEqual(io.open(p, encoding="utf-8").read(), before)

    def test_audit_runs_after_migrate(self):
        d, p = self._v0_project()
        ndd.main(["migrate", d])
        self.assertEqual(ndd.main(["--config", p, "audit"]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
