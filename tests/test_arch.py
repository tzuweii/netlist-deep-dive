#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""板卡事實表（`<板>_Facts.md`）。

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
        _parent, blks, n = groups[0]
        self.assertEqual(len(blks), 3)
        self.assertEqual(n, 2)          # 每組 2 顆，不是 6 也不是 0

    def test_repeat_groups_found_below_top_level(self):
        """重複結構要**遞迴往下找**。實測 9 路通道在第三層，只比頂層一組都抓不到。"""
        parts, blocks, nets = {}, {}, {"GND": []}
        for i in (1, 2, 3):
            rd = "U%d" % i
            parts[rd] = "IC_A"
            blocks[rd] = ("UC1", "Common", "CA%d" % i)
            nets["GND"].append((rd, "9"))
        parts["U9"] = "IC_B"
        blocks["U9"] = ("UC1", "Main")
        nets["GND"].append(("U9", "9"))
        nl, _bom, hier = _board(self.tmp, parts, nets, blocks=blocks)
        groups = A.repeat_groups(hier, nl)
        self.assertEqual([(p, len(b)) for p, b, _n in groups],
                         [(("UC1", "Common"), 3)])

    def test_near_identical_siblings_are_paired(self):
        """主／備常差幾顆（實測差兩顆 0402）——要並列出來並講出差在哪。"""
        parts, blocks, nets = {}, {}, {"GND": []}
        for side in ("P", "R"):
            for j in range(12):
                rd = "U%s%d" % (side, j)
                parts[rd] = "IC_A"
                blocks[rd] = "Main_%s" % side
                nets["GND"].append((rd, "9"))
        parts["RX1"] = "R_0402"
        blocks["RX1"] = "Main_R"
        nets["GND"].append(("RX1", "1"))
        nl, bom, hier = _board(self.tmp, parts, nets, blocks=blocks)
        txt = A.render("b", "B", nl, bom, hier, {}, None)
        self.assertIn(u"組成幾乎相同", txt)
        self.assertIn(u"`Main_R` 多 `R_0402` ×1", txt)

    # ---- 子電路之間的連線 ---------------------------------------------
    def _two_blocks(self, extra_parts=None, extra_nets=None, extra_blocks=None):
        parts = {"U1": "IC_A", "U2": "IC_B", "R1": "R_0402"}
        blocks = {"U1": "BLK_A", "U2": "BLK_B", "R1": "BLK_A"}
        nets = {"S_A": [("U1", "1"), ("R1", "1")],
                "S_B": [("R1", "2"), ("U2", "1")],
                "GND": [("U1", "9"), ("U2", "9")]}
        parts.update(extra_parts or {})
        nets.update(extra_nets or {})
        blocks.update(extra_blocks or {})
        pns = dict((r, fp) for r, fp in parts.items() if r.startswith("U"))
        return _board(self.tmp, parts, nets, blocks=blocks, pns=pns)

    def _links(self, nl, bom, hier):
        t = A.Tree(hier, nl)
        u = A.Units(t, bom, A.KeyPart(nl, bom, {}, None))
        return A.block_links(nl, u, re.compile(r"^GND$"))

    def test_series_resistor_counts_as_one_connection(self):
        """U1 -R1- U2 是**一條**訊號，不是 S_A、S_B 兩條。"""
        nl, bom, hier = self._two_blocks()
        edges, wide = self._links(nl, bom, hier)
        self.assertEqual(list(edges.values()), [["S_A"]])
        self.assertEqual(wide, [])

    def test_power_nets_are_not_links(self):
        nl, bom, hier = self._two_blocks()
        edges, _w = self._links(nl, bom, hier)
        self.assertNotIn("GND", [n for ns in edges.values() for n in ns])

    def test_passive_chain_does_not_merge_different_signals(self):
        """兩條線各經串阻、串阻另一端又被回授電阻接在一起（運放電路）時，
        不可把兩條併成一條——實測 I/Q 因此被算成一條。"""
        parts = {"U1": "IC_A", "U2": "IC_B", "R1": "R_0402", "R2": "R_0402",
                 "R3": "R_0402"}
        blocks = {"U1": "BLK_A", "U2": "BLK_B", "R1": "BLK_A", "R2": "BLK_A",
                  "R3": "BLK_A"}
        nets = {"I": [("U1", "1"), ("U2", "1"), ("R1", "1")],
                "Q": [("U1", "2"), ("U2", "2"), ("R2", "1")],
                "X": [("R1", "2"), ("R3", "1")],
                "Y": [("R2", "2"), ("R3", "2")],
                "GND": [("U1", "9")]}
        nl, bom, hier = _board(self.tmp, parts, nets, blocks=blocks,
                               pns={"U1": "IC_A", "U2": "IC_B"})
        edges, _w = self._links(nl, bom, hier)
        self.assertEqual(sorted(n for ns in edges.values() for n in ns),
                         ["I", "Q"])

    def test_two_series_parts_in_a_row_are_one_connection(self):
        """隔直電容再接 0 Ω 到 ADC：中間那條網路只有兩顆串聯件，仍是一條訊號。"""
        parts = {"U1": "IC_A", "U2": "IC_B", "C1": "C_0402", "R1": "R_0402"}
        blocks = {"U1": "BLK_A", "U2": "BLK_B", "C1": "BLK_A", "R1": "BLK_A"}
        nets = {"OUT": [("U1", "1"), ("C1", "1")],
                "MID": [("C1", "2"), ("R1", "1")],
                "AIN": [("R1", "2"), ("U2", "1")],
                "GND": [("U1", "9"), ("U2", "9")]}
        nl, bom, hier = _board(self.tmp, parts, nets, blocks=blocks,
                               pns={"U1": "IC_A", "U2": "IC_B"})
        edges, _w = self._links(nl, bom, hier)
        self.assertEqual(len(edges), 1)
        self.assertEqual(len(list(edges.values())[0]), 1)

    def test_loose_parts_become_one_block_per_part_number(self):
        """不在子電路的零件依料號各成一塊，不併成「（頂層）」一大塊。"""
        nl, bom, hier = self._two_blocks(
            extra_parts={"U7": "IC_MUX"},
            extra_nets={"S_C": [("U7", "1"), ("U2", "2")]})
        edges, _w = self._links(nl, bom, hier)
        names = [k[2] for pair in edges for k in pair if k[0] == "P"]
        self.assertIn("IC_MUX", names)

    def test_net_touching_many_blocks_is_listed_as_shared(self):
        parts, blocks, nets = {}, {}, {"BUS": [], "GND": []}
        for i in range(1, 5):
            rd = "U%d" % i
            parts[rd] = "IC_%d" % i
            blocks[rd] = "B%d" % i
            nets["BUS"].append((rd, "1"))
        nl, bom, hier = _board(self.tmp, parts, nets, blocks=blocks,
                               pns=dict((r, fp) for r, fp in parts.items()))
        edges, wide = self._links(nl, bom, hier)
        self.assertEqual(edges, {})
        self.assertEqual([n for n, _nodes in wide], ["BUS"])

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
        self.assertEqual(A.repeat_groups(hier, nl), [])

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

    # ---- §10 依對象收斂 -----------------------------------------------
    def test_template_keeps_constant_digits(self):
        """會變的數字才換成 `#`——bank 號 227 在每條上都一樣，要留著。"""
        self.assertEqual(A._template(["FPGA_227_RX0_N", "FPGA_227_RX1_N"]),
                         "FPGA_227_RX#_N")
        self.assertEqual(A.name_families(["D0", "D1", "D2", "CLK"]),
                         [("CLK", 1), ("D#", 3)])

    def _bus(self, n):
        parts = {"U1": "IC_A", "U2": "IC_B"}
        nets = {"GND": [("U1", "99"), ("U2", "99")]}
        for i in range(n):
            nets["D_%d" % i] = [("U1", str(i + 1)), ("U2", str(i + 1))]
        return _board(self.tmp, parts, nets,
                      pns={"U1": "IC_A", "U2": "IC_B"})

    def test_many_pins_to_one_part_collapse_to_one_edge(self):
        """同一對零件之間 16 條線是**一條邊**，不是 16 行。"""
        nl, bom, _h = self._bus(16)
        L = A.edge_lines(nl, bom, None, {}, "U1", re.compile(r"^GND$"),
                         lambda r: r.startswith("U"), lambda r: u"X")
        txt = u"\n".join(L)
        self.assertIn(u"16 條", txt)
        self.assertIn(u"`D_#` ×16", txt)
        self.assertLess(len(L), 5)

    def test_few_pins_are_listed_individually(self):
        nl, bom, _h = self._bus(2)
        L = A.edge_lines(nl, bom, None, {}, "U1", re.compile(r"^GND$"),
                         lambda r: r.startswith("U"), lambda r: u"X")
        self.assertIn(u"`D_0`", u"\n".join(L))
        self.assertIn(u"`D_1`", u"\n".join(L))

    def test_passive_summary_groups_same_wiring(self):
        """串阻 refdes 不同、去處網路不同，接法相同就算一種；去處零件不同要分開。"""
        s = A.passive_summary([
            ("1", u"R1 R_100ohm → `BANK65_3V3_J25`"),
            ("2", u"R2 R_100ohm → `BANK67_1V8_B20`"),
            ("3", u"R3 R_1K → J3.D14"),
            ("4", u"R4 R_1K → J4.D14")])
        self.assertIn(u"×2 支同接法", s)
        self.assertIn(u"J3.D14", s)
        self.assertIn(u"J4.D14", s)

    # ---- 撰寫材料切包 -------------------------------------------------
    def _rf(self, n=3):
        """n 路相同通道：接頭 -C- 放大器 - 濾波器 - SPDT，SPDT 另兩腳接接頭與偵測器
        再到接頭；放大器致能與開關控制各一條線回 FPGA（中樞）。

        n=1 時 FPGA 只接兩顆零件——它仍不可被當成串在路上的一節。"""
        parts, blocks, nets, pns = {}, {}, {"GND": []}, {}
        parts["U9"], pns["U9"], blocks["U9"] = "FPGA_BGA", "FPGA_X", "CTRL"
        for k in range(30):             # FPGA 一定有一大堆 I/O
            nets["IO_%d" % k] = [("U9", "IO%d" % k)]
        for i in range(1, n + 1):
            j, c, a, f, sw, ja, jb, dt = ("J%d0" % i, "C%d0" % i, "U%d1" % i,
                                          "U%d2" % i, "U%d3" % i, "J%d1" % i,
                                          "J%d2" % i, "U%d4" % i)
            for rd, fp, pn in ((j, "SMA_CONN", "SMA"), (c, "C_0402", None),
                               (a, "AMP_QFN", "AMP_X"), (f, "BPF_SMD", "BPF_X"),
                               (sw, "SW_QFN", "SPDT_X"), (ja, "SMA_CONN", "SMA"),
                               (jb, "SMA_CONN", "SMA"), (dt, "DET_SOT", "DET_X")):
                parts[rd], blocks[rd] = fp, "CH%d" % i
                if pn:
                    pns[rd] = pn
            nets["RF_IN_%d" % i] = [(j, "1"), (c, "1")]
            nets["N%05d" % (100 + i)] = [(c, "2"), (a, "1")]
            nets["AMP_OUT_%d" % i] = [(a, "2"), (f, "1")]
            nets["BPF_OUT_%d" % i] = [(f, "2"), (sw, "1")]
            nets["SW_A_%d" % i] = [(sw, "2"), (ja, "1")]
            nets["SW_B_%d" % i] = [(sw, "3"), (dt, "1")]
            nets["DET_%d" % i] = [(dt, "2"), (jb, "1")]
            nets["AMP_EN_%d" % i] = [(a, "3"), ("U9", "E%d" % i)]
            nets["SW_CTL_%d" % i] = [(sw, "4"), ("U9", "S%d" % i)]
            for rd in (j, a, f, sw, ja, jb, dt):
                nets["GND"].append((rd, "9"))
        return _board(self.tmp, parts, nets, blocks=blocks, pns=pns)

    def _topo(self, nl, bom, hier):
        is_key = A.KeyPart(nl, bom, {}, None)
        ep = lambda r: is_key(r) or A.ndd_classify.is_connector(nl, r)
        return A.Topology(nl, bom, A.source_parts(hier), re.compile(r"^GND$"), ep)

    def test_chain_runs_through_two_port_parts_to_branch_point(self):
        """接頭 → 放大器 → 濾波器 → 開關：放大器、濾波器只接兩個對象，是串在
        路上的一節；開關接三個，是分岔點，鏈停在那。隔直電容寫在兩節之間。"""
        nl, bom, hier = self._rf(n=1)
        chains = sorted(self._topo(nl, bom, hier).chains())
        self.assertIn(["J10", "U11", "U12", "U13"], chains)
        self.assertIn(["J12", "U14", "U13"], chains)
        self.assertEqual(len(chains), 2)

    def test_control_line_from_hub_does_not_cut_the_chain(self):
        """放大器的致能接回 FPGA，不可讓它變成三個對象的分岔點——每顆有致能腳的
        放大器都會被截斷。控制線改寫在節點旁。"""
        nl, bom, hier = self._rf(n=3)
        tp = self._topo(nl, bom, hier)
        self.assertTrue(tp.passthru("U11"))
        self.assertFalse(tp.passthru("U13"))      # 開關真的有三個 RF 對象
        line = tp.line(["J10", "U11", "U12", "U13"])
        self.assertIn(u"另接控制 U9 1 條", line)

    def test_connector_is_always_a_chain_end(self):
        """接頭只接兩個對象也不可被穿過——它是板子的邊界。"""
        nl, bom, hier = self._rf(n=1)
        tp = self._topo(nl, bom, hier)
        for c in tp.chains():
            self.assertNotIn(u"J", u"".join(r[0] for r in c[1:-1]))

    def test_identical_chains_collapse_into_one_family(self):
        """×3 通道的同一條鏈是一族三組，不是三條；每組 refdes 都保留。"""
        nl, bom, hier = self._rf(n=3)
        txt = A.render("b", "B", nl, bom, hier, {"power_net_regex": "^GND$"}, None)
        sec = txt[txt.index(u"## 11."):txt.index(u"## 12.")]
        self.assertIn(u"×3 組同構", sec)
        self.assertIn(u"J30 ─ U31 ─ U32 ─ U33", sec)
        self.assertIn(u"CH#", sec)
        self.assertEqual(sec.count(u"- **C"), 2)

    def test_parallel_control_does_not_cut_the_chain(self):
        """數位衰減器的 5 位元控制接到 GPIO 擴充（不是中樞）——5 條並列不是訊號
        路徑，衰減器仍是串在路上的一節，擴充器也不可被串進鏈裡。"""
        parts = {"J1": "SMA_CONN", "J2": "SMA_CONN", "U1": "ATT_QFN",
                 "U2": "GPIO_QFN"}
        pns = {"J1": "SMA", "J2": "SMA", "U1": "ATT_X", "U2": "GPIO_X"}
        nets = {"RF_A": [("J1", "1"), ("U1", "RF1")],
                "RF_B": [("U1", "RF2"), ("J2", "1")],
                "GND": [("J1", "9"), ("J2", "9"), ("U1", "9"), ("U2", "9")]}
        for k in range(5):
            nets["ATT_P%d" % k] = [("U1", "P%d" % k), ("U2", "IO%d" % k)]
        nl, bom, hier = _board(self.tmp, parts, nets, pns=pns)
        tp = self._topo(nl, bom, hier)
        self.assertEqual(tp.chains(), [["J1", "U1", "J2"]])
        self.assertIn(u"U2 5 條", tp.line(["J1", "U1", "J2"]))

    def test_differential_pairs_count_as_one_path(self):
        """雙通道 VGA：I、Q 各一對差動進、各一對差動出到 ADC。差動對算一路，
        所以它有三個對象（兩顆 balun、ADC），是分岔點——不可把 I 與 Q 串成
        同一條鏈。"""
        parts = {"U3": "BALUN_SMD", "U4": "BALUN_SMD", "U1": "VGA_QFN",
                 "U2": "ADC_BGA"}
        pns = {"U3": "BALUN_X", "U4": "BALUN_X", "U1": "VGA_X", "U2": "ADC_X"}
        nets = {"GND": [(r, "99") for r in parts]}
        for ch, m in (("I", "U3"), ("Q", "U4")):
            for pol in ("P", "N"):
                nets["VIN_%s_%s" % (ch, pol)] = [(m, pol), ("U1", "IN%s%s" % (ch, pol))]
                nets["VOUT_%s_%s" % (ch, pol)] = [("U1", "OUT%s%s" % (ch, pol)),
                                                  ("U2", "AIN%s%s" % (ch, pol))]
        nl, bom, hier = _board(self.tmp, parts, nets, pns=pns)
        tp = self._topo(nl, bom, hier)
        self.assertEqual(tp.width("U1", "U3"), 1)
        self.assertEqual(tp.width("U1", "U2"), 2)
        self.assertFalse(tp.passthru("U1"))

    def test_primary_suffix_alone_is_not_a_pair(self):
        """`_P` 在這類設計裡也是 Primary；沒有對應的 `_N` 就不併。"""
        parts = {"U1": "IC_A", "U2": "IC_B"}
        nets = {"SW_CA0_P": [("U1", "1"), ("U2", "1")],
                "SW_CA1_P": [("U1", "2"), ("U2", "2")],
                "GND": [("U1", "9"), ("U2", "9")]}
        nl, bom, hier = _board(self.tmp, parts, nets,
                               pns={"U1": "IC_A", "U2": "IC_B"})
        self.assertEqual(self._topo(nl, bom, hier).width("U1", "U2"), 2)

    def test_material_is_board_level_only(self):
        """撰寫材料 = 全板部分（§1–§9）＋鏈族摘要；逐顆零件的邊（§10）不給，
        待查證清單（§15）也不給。階層樹的主要零件帶 symbol 類別。"""
        nl, bom, hier = self._rf(n=3)
        out = {}
        A.render("b", "B", nl, bom, hier, {"power_net_regex": "^GND$"}, None,
                 out=out)
        txt = A.material(out, "B")
        self.assertIn(u"## 2. 板子的組成", txt)
        self.assertIn(u"訊號鏈（Facts §11 摘要）", txt)
        self.assertIn(u"×3 組同構", txt)
        self.assertIn(u"其餘 2 組", txt)            # 摘要：不逐組列
        self.assertNotIn(u"## 10.", txt)
        self.assertNotIn(u"這份文件還不知道什麼", txt)

    def test_compact_facts_has_no_interpretation(self):
        """精簡版 Facts 留在資料夾給人與 LLM 查，必須全是事實：不可有「通常」
        「常見於主／備」「控制中樞」這類推論，也不含逐顆零件接到誰（§10）。"""
        nl, bom, hier = self._rf(n=3)
        out = {}
        A.render("b", "B", nl, bom, hier, {"power_net_regex": "^GND$"}, None,
                 out=out)
        txt = A.compact(out, "b", "B")
        self.assertIn(u"## 2.", txt)
        self.assertIn(u"## 這份文件還不知道什麼", txt)
        self.assertNotIn(u"## 10.", txt)
        self.assertIsNone(re.search(u"通常|常見|中樞|多半|推測|應是|可能是", txt))

    def test_compact_facts_marks_rule_derived_sections(self):
        """依規則算出的節（連線數、跨區零件、SCL、電源軌）要標「推算」，檔頭要說
        只當線索、引用前用 ndd.py 查證；直接讀取的節（組成、未貼件）不標。"""
        nl, bom, hier = self._rf(n=3)
        out = {}
        A.render("b", "B", nl, bom, hier, {"power_net_regex": "^GND$"}, None,
                 out=out)
        txt = A.compact(out, "b", "B")
        for h in (u"## 3. ", u"## 4. ", u"## 5. ", u"## 7. "):
            line = [l for l in txt.splitlines() if l.startswith(h)][0]
            self.assertIn(A._EST, line)
        for h in (u"## 2. ", u"## 8. "):
            line = [l for l in txt.splitlines() if l.startswith(h)][0]
            self.assertNotIn(A._EST, line)
        self.assertIn(u"只當找方向的線索", txt)
        self.assertIn(u"ndd.py", txt.split(u"## 1.")[0])

    def test_bus_nets_do_not_form_chains(self):
        """匯流排上的零件不可被當成兩兩串接。"""
        parts, nets, pns = {}, {"BUS": [], "GND": []}, {}
        for i in range(1, 6):
            rd = "U%d" % i
            parts[rd] = pns[rd] = "IC_%d" % i
            nets["BUS"].append((rd, "1"))
        nl, bom, hier = _board(self.tmp, parts, nets, pns=pns)
        self.assertEqual(self._topo(nl, bom, hier).chains(), [])

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
