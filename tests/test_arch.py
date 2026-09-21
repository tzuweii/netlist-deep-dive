#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""板卡導覽（`<板>_Architecture.md`）。

⚠️ 這份文件的**唯一硬保證**是：它只含 `[N]`/`[B]`/`[S]`，**一條 `[D]` 都沒有**。
   `init` 跑到這一步時 datasheet 還沒到齊，任何腳位功能主張都是憑空捏的。
   所以這裡第一條測試就是「輸出裡不准出現 `[D`」。

⚠️ 第二個容易悄悄壞掉的地方是**倍率**：重複結構那段算錯（例如把每組零件數
   除以組數）不會噴錯，只會印出一個看起來很合理的錯數字。
"""
import io
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
sys.path.insert(0, HERE)

import fixtures                                                # noqa: E402
import ndd_arch as A                                           # noqa: E402
import ndd_pads                                                # noqa: E402
import ndd_hier                                                # noqa: E402


class _Bom(object):
    """最小 BOM 替身：只實作 `ndd_arch` 真的會用到的三個方法。"""

    def __init__(self, pns, scope="complete"):
        self._pns, self.scope = pns, scope

    def pn(self, rd):
        return self._pns.get(rd)

    def of(self, rd):
        return {"pn": self._pns[rd]} if rd in self._pns else None

    def value(self, rd):
        return ""


def _board(tmp, parts, nets, blocks=None, pns=None, scope="complete"):
    asc = fixtures.write_asc(os.path.join(tmp, "b.asc"), parts, nets)
    nl = ndd_pads.Netlist(asc)
    hier = None
    if blocks is not None:
        pc, nc = fixtures.write_hier(os.path.join(tmp, "p.csv"),
                                     os.path.join(tmp, "n.csv"),
                                     parts, nets, blocks=blocks)
        hier = ndd_hier.Hierarchy(pc, nc)
    return nl, _Bom(pns or {}, scope), hier


class ArchTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="ndd_arch_")

    # ---- 最重要的一條：不得出現 [D] ----------------------------------
    def test_never_emits_datasheet_citations(self):
        """`init` 當下沒有 datasheet，所以這份文件**不准出現 `[D 受詞]` 引用**。

        ⚠️ 不能單純斷言 `"[D" not in txt`——文件**應該**在開頭與 §8 講「那些要
        `[D]` 規格書，本文不會有」。要禁的是帶受詞的引用形式（`[D 檔名 p.x]`），
        不是提到這個標記本身。
        """
        parts = {"U1": "IC_A", "R1": "R_0402", "J1": "Conn_X"}
        nets = {"SIG_1": [("U1", "1"), ("R1", "1")],
                "GND": [("U1", "2"), ("R1", "2"), ("J1", "1")]}
        nl, bom, hier = _board(self.tmp, parts, nets, pns={"U1": "IC_A"})
        txt = A.render("b", "B", nl, bom, hier, {}, None)
        self.assertIsNone(re.search(r"\[D[ :][^\]]+\]", txt),
                          u"不該出現帶受詞的 [D ...] 引用")
        # 而且「本文沒有 [D]」這句聲明必須還在——它是這份文件的定位。
        self.assertIn(u"這份文件不會有", txt)

    # ---- 倍率 ---------------------------------------------------------
    def test_repeat_group_counts_parts_per_block_not_divided(self):
        """每組零件數是**單一區塊**的顆數，不是總數除以組數。

        除錯過一次：3 個各 2 顆的區塊被印成「3 組、每組 0 顆」。
        """
        parts = {}
        blocks = {}
        nets = {"GND": []}
        for i in (1, 2, 3):
            for j in (1, 2):
                rd = "U%d%d" % (i, j)
                parts[rd] = "IC_A"
                blocks[rd] = "BLK%d" % i
                nets["GND"].append((rd, "9"))
        nl, bom, hier = _board(self.tmp, parts, nets, blocks=blocks)
        groups = A.repeat_groups(hier, nl)
        self.assertEqual(len(groups), 1)
        blks, n = groups[0]
        self.assertEqual(len(blks), 3)
        self.assertEqual(n, 2)          # 每組 2 顆，不是 6 也不是 0

    def test_repeat_group_range_is_natural_sorted(self):
        """`TX1 … TX16`，不是字典序的 `TX1 … TX9`。"""
        self.assertEqual(A._rng(["TX1", "TX9", "TX10", "TX16"]),
                         u"`TX1` … `TX16`")

    def test_blocks_with_different_composition_are_not_grouped(self):
        """組成不同的區塊不可被當成同一種——簽章是 footprint 多重集合。"""
        parts = {"U11": "IC_A", "U21": "IC_B"}
        blocks = {"U11": "BLK1", "U21": "BLK2"}
        nets = {"GND": [("U11", "9"), ("U21", "9")]}
        nl, bom, hier = _board(self.tmp, parts, nets, blocks=blocks)
        self.assertEqual([len(b) for b, _ in A.repeat_groups(hier, nl)], [1, 1])

    # ---- 未貼件 -------------------------------------------------------
    def test_dni_still_detected_under_smt_only_bom(self):
        """`smt_only` 只讓**連接器／機構件**那類不可判定，一般 SMT 件照判。

        整段跳過會漏掉真結論（實測某板的兩顆 NMOS 就是這樣確認的未貼件）。
        """
        parts = {"U1": "IC_A", "T1": "Trans-NMOS_X", "J1": "Conn_X"}
        nets = {"GND": [("U1", "9"), ("T1", "3"), ("J1", "1")]}
        nl, bom, _h = _board(self.tmp, parts, nets,
                             pns={"U1": "IC_A"}, scope="smt_only")
        shown, total, smt = A.dni_parts(nl, bom, {}, None)
        self.assertTrue(smt)
        rds = [r for r, _fp in shown]
        self.assertIn("T1", rds)        # 一般 SMT 件 -> 判得出來
        self.assertNotIn("J1", rds)     # 連接器 -> 不可判定，排除

    # ---- 主要零件 -----------------------------------------------------
    def test_major_parts_collapse_same_pn(self):
        """同料號收斂成一列並附顆數——否則 ×9 會把整張表吃掉。"""
        parts = {"U%d" % i: "IC_A" for i in range(1, 6)}
        nets = {"SIG_1": [("U%d" % i, "1") for i in range(1, 6)]}
        nl, bom, _h = _board(self.tmp, parts, nets,
                             pns={"U%d" % i: "IC_A" for i in range(1, 6)})
        rows = A.major_parts(nl, bom, {}, None, re.compile(r"^GND$", re.I))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "IC_A")
        self.assertEqual(rows[0][5], 5)          # 顆數

    def test_sig_pins_excludes_power(self):
        parts = {"U1": "IC_A"}
        nets = {"SIG_1": [("U1", "1")], "GND": [("U1", "2")],
                "VDD_3V3": [("U1", "3")]}
        nl, _b, _h = _board(self.tmp, parts, nets)
        pwr = re.compile(r"^VDD", re.I)
        self.assertEqual(A._sig_pins(nl, "U1", pwr), 1)   # GND 與 VDD 都不算

    # ---- 訊號家族 -----------------------------------------------------
    def test_net_families_group_by_trailing_index(self):
        nets = {"GND": []}
        for i in range(9):
            nets["TX_EN_P_%d" % i] = [("U1", str(i + 1))]
        nl, _b, _h = _board(self.tmp, {"U1": "IC_A"}, nets)
        fam = dict(A.net_families(nl, re.compile(r"^GND$", re.I)))
        self.assertEqual(fam.get("TX_EN_P"), 9)

    def test_net_families_skip_autogenerated_names(self):
        """PADS 改名產生的 `N12345678` 對讀的人沒有意義，不可蓋掉真家族。"""
        nets = {"GND": []}
        for i in range(5):
            nets["N1234567%d" % i] = [("U1", str(i + 1))]
        nl, _b, _h = _board(self.tmp, {"U1": "IC_A"}, nets)
        self.assertEqual(A.net_families(nl, re.compile(r"^GND$", re.I)), [])

    # ---- 沒有階層時要降級，不可爆炸 -----------------------------------
    def test_renders_without_hierarchy(self):
        parts = {"U1": "IC_A"}
        nets = {"SIG_1": [("U1", "1")]}
        nl, bom, _h = _board(self.tmp, parts, nets)
        txt = A.render("b", "B", nl, bom, None, {}, None)
        self.assertIn(u"沒有階層資料", txt)

    def test_warns_when_no_power_net_recognised(self):
        """一條電源都沒認出來 = `power_net_regex` 多半沒設，要講出來。"""
        parts = {"U1": "IC_A"}
        nets = {"SIG_1": [("U1", "1")]}
        nl, bom, _h = _board(self.tmp, parts, nets)
        txt = A.render("b", "B", nl, bom, None, {"power_net_regex": r"^NOPE$"},
                       None)
        self.assertIn(u"一條都沒認出來", txt)


if __name__ == "__main__":
    unittest.main()
