#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""料號 -> 規格書對照：只有一套比對，INDEX.md 的「缺」才可信。"""
import io
import os
import re
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import ndd                                                     # noqa: E402
import ndd_pinfn                                               # noqa: E402


def _dir(*names):
    d = tempfile.mkdtemp(prefix="ndd_ds_")
    for n in names:
        with open(os.path.join(d, n), "wb") as fh:
            fh.write(b"%PDF-1.4 dummy")
    return d


class TestLocate(unittest.TestCase):
    def setUp(self):
        self.d = _dir("PCA9547.pdf", "PCA9554B_PCA9554C.pdf", "tps7a4701rgw.pdf")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _via(self, pn, explicit=None):
        p, via = ndd_pinfn.locate(self.d, pn, explicit)
        return (os.path.basename(p) if p else None), via

    def test_full_part_number_in_filename_is_confirmed(self):
        f, via = self._via("TPS7A4701RGW")
        self.assertEqual(f, "tps7a4701rgw.pdf")
        self.assertIn(via, ndd_pinfn.CONFIRMED_VIA)

    def test_filename_equal_to_base_part_number_is_confirmed(self):
        """pca9547.pdf 對 PCA9547PW,118：檔名就是料號去掉訂購碼後綴。"""
        f, via = self._via("PCA9547PW,118")
        self.assertEqual(f, "PCA9547.pdf")
        self.assertIn(via, ndd_pinfn.CONFIRMED_VIA)

    def test_family_datasheet_is_found_but_needs_a_look(self):
        """系列規格書要找得到（舊的 MISSING.md 會判缺），但不可直接算已確認。"""
        f, via = self._via("PCA9554BBSHP")
        self.assertEqual(f, "PCA9554B_PCA9554C.pdf")
        self.assertNotIn(via, ndd_pinfn.CONFIRMED_VIA)

    def test_sharing_only_leading_digits_is_not_a_match(self):
        """實測：PCA9547（I²C mux）曾因共用 `PCA95` 被配到 PCA9554（GPIO 擴充器）。"""
        os.remove(os.path.join(self.d, "PCA9547.pdf"))
        f, _via = self._via("PCA9547BS,118")
        self.assertIsNone(f)

    def test_model_number_strips_ordering_code(self):
        for pn, model in (("PCA9547BS,118", "pca9547"), ("AD5667RBCPZ-R2", "ad5667"),
                          ("XC7Z100-1FFG900I", "xc7z100"),
                          ("74LVC2G34GV,125", "74lvc2g34")):
            self.assertEqual(ndd_pinfn.model_of(pn), model)

    def test_user_mapping_wins_and_is_confirmed(self):
        f, via = self._via("PCA9554BBSHP", "PCA9554B_PCA9554C.pdf")
        self.assertEqual((f, via), ("PCA9554B_PCA9554C.pdf", ndd_pinfn.VIA_USER))

    def test_user_mapping_to_absent_file_is_missing_not_guessed(self):
        f, _via = self._via("PCA9554BBSHP", "nope.pdf")
        self.assertIsNone(f)

    def test_unrelated_part_is_missing(self):
        f, via = self._via("XC7Z100-1FFG900I")
        self.assertIsNone(f)
        self.assertTrue(via.startswith("缺"))


class TestIndex(unittest.TestCase):
    def setUp(self):
        self.d = _dir("PCA9547.pdf", "PCA9554B_PCA9554C.pdf")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _rows(self):
        rows = []
        for pn in ("PCA9554BBSHP", "PCA9547PW,118", "XC7Z100-1FFG900I"):
            p, via = ndd_pinfn.locate(self.d, pn)
            st = ("缺" if p is None else
                  "已確認" if via in ndd_pinfn.CONFIRMED_VIA else "待確認")
            rows.append((pn, {"refs": {"a": 1}}, p, via, st))
        return rows

    def test_index_lists_found_and_missing_and_replaces_old_list(self):
        with io.open(os.path.join(self.d, "MISSING.md"), "w", encoding="utf-8") as fh:
            fh.write(u"- [ ] `PCA9554BBSHP`\n")
        ip, n = ndd._write_ds_index(self.d, "datasheets", self._rows(), 5)
        self.assertEqual((n["已確認"], n["待確認"], n["缺"]), (1, 1, 1))
        self.assertFalse(os.path.exists(os.path.join(self.d, "MISSING.md")),
                         "舊清單只比檔名，留著會和對照表互相矛盾")
        txt = io.open(ip, encoding="utf-8").read()
        self.assertIn("PCA9554B_PCA9554C.pdf", txt)
        self.assertEqual(re.search(r"<!-- missing: (\d+) -->", txt).group(1), "1")

    def test_index_goes_stale_when_a_datasheet_is_added(self):
        ndd._write_ds_index(self.d, "datasheets", self._rows(), 0)
        self.assertFalse(ndd_pinfn.index_stale(self.d))
        old = time.time() - 60
        os.utime(os.path.join(self.d, ndd_pinfn.INDEX_NAME), (old, old))
        with open(os.path.join(self.d, "xc7z100.pdf"), "wb") as fh:
            fh.write(b"%PDF-1.4 dummy")
        self.assertTrue(ndd_pinfn.index_stale(self.d))


if __name__ == "__main__":
    unittest.main()
