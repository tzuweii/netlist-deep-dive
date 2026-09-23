#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨板圖：連接器對接驗證 + 端到端訊號追跡 + 功能拓樸提示。

netlist 本身**沒有跨板連線**。要追一條從 A 板走到 C 板的訊號，必須先假設連接器
兩側的腳位如何對應。

════════════════════════════════════════════════════════════════════════════
本模組維護**兩張互不相通的圖**：

  trace graph —— 只含已驗證的 signal_transfer 邊、net 邊與已批准的 mate 邊。
                 這是 `signal_chain.csv` 的來源，可作為架構結論。
  hint  graph —— control influence、stateful 行為、參數控制、未知邊界。
                 **永不得作為 BFS 的下一跳，也不得被敘述為 net 連通。**

  「能控制它」不等於「訊號穿過它」。latch/reset/select 腳會改變元件行為，
  但不構成訊號路徑。兩者都要輸出，只有前者能進正式 trace。
════════════════════════════════════════════════════════════════════════════

⚠️ 三個一定要記住的界線：

  * **netlist 證明「接線意圖」，layout 證明「實體位置」，只有系統行為能證明
    「兩者都對」。** layout 若把連接器鏡像放置，netlist 一個字都不會變。
  * **要分辨對接的物理型式**：兩側同型 → 中間必然有線束，接法只能靠 net 名
    或線束圖；公母直接對接 → 直通。兩者的定案途徑完全不同。
  * **殘存候選要用「會不會壞」排除**：代入後看會不會造成立即而明顯的故障。
    系統若實際會動，該候選就被排除了。
"""
import re
from collections import deque

import ndd_confidence as C
import ndd_package
from ndd_models import (control_influences, derive_gating, missing_pins,
                        outgoing, transfer_edges)

# endpoint 分類 —— 「沒有模型」**不等於**「訊號在此結束」。
EP_TERMINAL = "terminal(declared)"
EP_STATEFUL = "stateful(declared)"
EP_UNKNOWN_DECLARED = "unknown_stop(declared)"
EP_UNKNOWN_UNUSABLE = "unknown_stop(model_unusable)"
EP_UNCLASSIFIED = "unclassified"
# ⚠️ 驅動端：走到某顆的 transfer 輸出腳、且無法再往前走。這是訊號的**來源**
#    不是負載。有向模型才分得出來——對稱模型會直接穿過去，看不到這件事。
EP_DRIVER = "driver(model)"
# ⚠️ 板內追蹤的跨板邊界：**本板這一側**的腳位，對面腳位只當字串證據帶著。
#    對面的節點不進 path——否則對面的 mate caveats 會污染板內結論的
#    confidence，階層加註也會拿對面的 refdes 去查本板的表而查到**錯的**。
EP_BOUNDARY = "boundary(not_followed)"
# 進 loads 的端點種類：unknown_stop 是「停在具名位置」、driver 是來源，
# boundary 是「還沒走」，都不算。
EP_IN_LOADS = (EP_TERMINAL, EP_STATEFUL, EP_UNCLASSIFIED)

class MateMapError(Exception):
    """已批准的對接對映本身有問題 —— 載入時就要擋，不能等到走圖。"""


# 對接的前提：板子都已在實體世界接過、可以用。所以工具不判斷「有沒有接」，
# 只判斷「怎麼接」：
#   公母直接對接 -> 一律直通（不考慮 footprint 畫錯）；證據只用來確認
#                   「這兩顆真的是一對」。
#   線束（兩側同型）-> 逐腳比 net 名；每支訊號腳都唯一對上才算數。
# 直通至少要有這麼多支腳的 net 名兩側相符 —— 這是**正面證據**的下限。
# 沒有它，「零矛盾」在資訊量不足的小連接器上會無條件通過。
MATE_MIN_SEMANTIC = 4

_BLOCKING = {"package:unresolved": "package_unresolved",
             "package:conflict": "package_conflict",
             "model:ambiguous": "model_ambiguous",
             "model:pin_absent": "pin_absent"}


class Fabric(object):
    def __init__(self, boards, mates, models, power_rx, net_normalize=None,
                 mate_map=None, endpoints=None, part_package=None):
        """boards: {key: (Netlist, Bom)}；mates: [(ba,ra,bb,rb), ...]"""
        self.nl = {k: v[0] for k, v in boards.items()}
        self.bom = {k: v[1] for k, v in boards.items()}
        self.models = models
        self.power_rx = re.compile(power_rx, re.I) if power_rx else None
        self.norm_rules = [(re.compile(a), b) for a, b in (net_normalize or [])]
        self.mates = [tuple(m) for m in mates]
        self.mate_map = mate_map or {}
        self.endpoints = endpoints or {}
        self.part_package = part_package or {}
        self.hints = []                 # hint graph：只輸出，不走
        self.pkg_res = {}               # (board, refdes) -> 封裝判定與證據
        self._model_cache = {}
        self._build_mates()

    # ---- 對接邊 -----------------------------------------------------------
    @staticmethod
    def mate_key(ba, ra, bb, rb):
        return "%s:%s|%s:%s" % (ba, ra, bb, rb)

    def _approved(self, ba, ra, bb, rb):
        """回傳 (map, 反向?) 或 (None, False)。兩種宣告順序都要找得到。"""
        m = self.mate_map.get(self.mate_key(ba, ra, bb, rb))
        if m:
            return m, False
        m = self.mate_map.get(self.mate_key(bb, rb, ba, ra))
        if m:
            return m, True
        return None, False

    @staticmethod
    def _validate_map(key, amap, pa, pb):
        """已批准的對映必須先被驗證，否則「批准」只是把錯誤升級成結論。

        ⚠️ **單射檢查最重要**：兩個 A 腳映到同一個 B 腳會**靜默合併兩條 net**，
           而合併後的圖看起來完全正常。這是所有 mapping 錯誤裡後果最大的一種。
        """
        errs = []
        missing_a = [p for p in amap if p not in pa]
        missing_b = [q for q in amap.values() if q not in pb]
        if missing_a:
            errs.append("A 側不存在的 pin：%s" % ", ".join(sorted(missing_a)[:8]))
        if missing_b:
            errs.append("B 側不存在的 pin：%s" % ", ".join(sorted(missing_b)[:8]))
        seen = {}
        dup = []
        for p, q in amap.items():
            if q in seen:
                dup.append("%s 與 %s 都映到 %s" % (seen[q], p, q))
            seen[q] = p
        if dup:
            errs.append("非單射（會靜默合併 net）：%s" % "；".join(dup[:5]))
        uncovered_a = [p for p in pa if p not in amap]
        if uncovered_a:
            errs.append("A 側未涵蓋的 pin：%s" % ", ".join(sorted(uncovered_a)[:8]))
        if errs:
            raise MateMapError(
                "mate_map `%s` 驗證失敗：\n  - %s\n"
                "  （未涵蓋的腳若確實不對接，請在 approved 裡明確省略並於 "
                "evidence 說明；工具不會替你假設。）" % (key, "\n  - ".join(errs)))
        uncovered_b = [q for q in pb if q not in set(amap.values())]
        return uncovered_b

    def _build_mates(self):
        self.mate = {}
        self.mate_status = {}
        self.mate_uncovered = {}
        self.mate_evidence = {}
        for ba, ra, bb, rb in self.mates:
            spec, rev = self._approved(ba, ra, bb, rb)
            pa, pb = self.nl[ba].pins(ra), self.nl[bb].pins(rb)
            if spec:
                amap = {str(k): str(v) for k, v in spec["approved"].items()}
                if rev:
                    amap = {v: k for k, v in amap.items()}
                key = self.mate_key(ba, ra, bb, rb)
                self.mate_uncovered[key] = self._validate_map(key, amap, pa, pb)
                pairs = [(p, q) for p, q in amap.items()]
                cav = []
                self.mate_status[(ba, ra, bb, rb)] = "approved"
                self.mate_evidence[(ba, ra, bb, rb)] = spec.get("evidence", "")
            else:
                # 連接器只負責「訊號有沒有連到」——netlist 連得上就是事實，
                # 不需要 datasheet。板子都在實體世界接過，所以這裡讓工具
                # **自己定案**（規則見 `_mate_decision`）：
                #   定案        -> inferred，不掛 caveat
                #   定不了      -> mate:ambiguous，要問人
                ok, why = self._decide_mate(ba, ra, bb, rb)
                pairs = self._mate_decision(ba, ra, bb, rb)["pairs"]
                if ok:
                    cav = []
                    self.mate_status[(ba, ra, bb, rb)] = "inferred"
                else:
                    cav = ["mate:ambiguous"]
                    self.mate_status[(ba, ra, bb, rb)] = "ambiguous"
                self.mate_evidence[(ba, ra, bb, rb)] = why
            for p, q in pairs:
                if p in pa and q in pb:
                    self.mate.setdefault((ba, ra, p), []).append(((bb, rb, q), cav))
                    self.mate.setdefault((bb, rb, q), []).append(((ba, ra, p), cav))

    def _decide_mate(self, ba, ra, bb, rb):
        """能不能自己定案。回傳 (可定案?, 證據字串)。"""
        d = self._mate_decision(ba, ra, bb, rb)
        return d["ok"], d["why"]

    # 零件庫名稱前綴：`Conn_`、`Conn-`，以及跟在後面的分類字（`Header_`、
    # `DSUB_`、`B04_`）。剝掉之後第一個 `-` 之前就是系列名（SEAF、T2M、UEC5）。
    _RX_FAMILY_PREFIX = re.compile(r"^Conn[-_](?:[A-Za-z0-9]+_)?", re.I)

    @staticmethod
    def family(footprint):
        return Fabric._RX_FAMILY_PREFIX.sub("", footprint or "").split("-")[0].upper()

    def mate_form(self, ba, ra, bb, rb):
        """`direct`（公母直接對接）或 `harness`（兩側同型，中間必有線束）。

        ⚠️ 不判斷誰公誰母——那不影響結論。兩側同系列（T2M 對 T2M、UEC5 對
           UEC5）插不起來，中間一定有線；不同系列（SEAF 對 SEAM）就是直接對插。
        """
        fa = self.family(self.nl[ba].parts.get(ra, ""))
        fb = self.family(self.nl[bb].parts.get(rb, ""))
        return "harness" if fa and fa == fb else "direct"

    @staticmethod
    def phys_size(pins):
        """連接器的實體大小估計，用來找可能對插的另一顆。

        ⚠️ **不可用已接腳數**：`pins()` 只含已接的腳，兩顆對插的連接器空腳
           數不同時，已接腳數就不同，真正的一對會被分到不同組。改用最大腳號
           （字母排：排數 × 最大欄號）；只有最末端的腳剛好是空腳時才會低估。
        """
        pins = list(pins)
        if not pins:
            return 0
        if all(p.isdigit() for p in pins):
            return max(int(p) for p in pins)
        if all(re.match(r"^[A-Za-z]\d+$", p) for p in pins):
            # 排數從 A 算到最後一排，不數出現過幾排 —— 整排空腳的排不在 pins 裡
            rows = ord(max(p[0].upper() for p in pins)) - ord("A") + 1
            return rows * max(int(p[1:]) for p in pins)
        return len(pins)

    def _mate_decision(self, ba, ra, bb, rb):
        """對接怎麼接、有沒有把握。回傳 dict：

        `ok` / `why` / `form` / `pairs`（採用的腳位對應）/ `match` / `bad` /
        `signals`（線束：對上的訊號腳數）。
        """
        key = (ba, ra, bb, rb)
        cache = self.__dict__.setdefault("_decision_cache", {})
        if key not in cache:
            pa, pb = self.nl[ba].pins(ra), self.nl[bb].pins(rb)
            form = self.mate_form(ba, ra, bb, rb)
            if form == "direct":
                cache[key] = self._decide_direct(pa, pb)
            else:
                cache[key] = self._decide_harness(pa, pb)
        return cache[key]

    def _decide_direct(self, pa, pb):
        """公母直接對接：接法一律直通。證據只回答「這兩顆真的是一對嗎」。

        ⚠️ 矛盾腳不否決：已確認過的真實案例（interposer.J3↔DPU.J2001 的
           `3P4V_C` 對 `3P3V_C_VDD_X`）就是兩側對同一條軌的電壓命名不同。
           填錯配對時則是**幾乎沒有相符、到處矛盾**（J3↔J2002：相符 0、
           矛盾 36），所以判準是「相符夠多，且多於矛盾」。
        """
        pairs = [(p, p) for p in pa if p in pb]
        bad = sum(1 for p, q in pairs
                  if Fabric.contradicts(self.cls(pa[p]), self.cls(pb[q])))
        match = sum(1 for p, q in pairs if self.norm(pa[p]) == self.norm(pb[q]))
        why = "公母直接對接 -> 直通（語意相符 %d、矛盾 %d）" % (match, bad)
        ok = True
        if match < MATE_MIN_SEMANTIC:
            # ⚠️ 「零矛盾」在小型連接器上是**空過的檢查**：沒有相符的 net 名時
            #    任何配對都零矛盾。沒有正面證據就不能確認這兩顆是一對。
            ok = False
            why += ("；**正面證據不足**（語意相符 %d < %d）—— 兩側命名不同"
                    "（補 net_normalize）或兩顆根本不是一對" % (match, MATE_MIN_SEMANTIC))
        elif bad >= match:
            ok = False
            why += "；矛盾不少於相符 —— 這兩顆很可能不是一對，請確認配對"
        return dict(ok=ok, why=why, form="direct", pairs=pairs,
                    match=match, bad=bad, signals=0)

    def _decide_harness(self, pa, pb):
        """線束：接法由線決定，netlist 裡沒有。只能逐腳比 net 名。

        每支**訊號腳**（非電源／地）都要在對面找到唯一同名的腳，兩個方向都要。
        電源／地不必對上——追訊號時本來就跳過。兩側都只有電源／地時，
        接法不影響任何訊號，直接放行。

        定不了時退回直通對應並由呼叫端掛 `mate:ambiguous`（它只是佔位，
        真正的接法要使用者用 `mate_map` 提供）。
        """
        def signals(pins):
            return {p: self.norm(n) for p, n in pins.items()
                    if n and not self.is_rail(self.cls(n)) and not self.is_power(n)}
        sa, sb = signals(pa), signals(pb)
        inv_a, inv_b = {}, {}
        for p, n in sa.items():
            inv_a.setdefault(n, []).append(p)
        for q, n in sb.items():
            inv_b.setdefault(n, []).append(q)
        mapping, miss = {}, []
        for p, n in sorted(sa.items()):
            if len(inv_a[n]) == 1 and len(inv_b.get(n, [])) == 1:
                mapping[p] = inv_b[n][0]
            else:
                miss.append(p)
        miss_b = [q for q in sorted(sb) if q not in set(mapping.values())]
        straight = [(p, p) for p in pa if p in pb]
        base = dict(form="harness", match=len(mapping), bad=0,
                    signals=len(mapping))
        if not sa and not sb:
            return dict(base, ok=True, pairs=straight,
                        why="線束，兩側只有電源／地 -> 接法不影響訊號追蹤")
        if miss or miss_b:
            sample = ", ".join(["A.%s=%s" % (p, pa[p]) for p in miss[:3]]
                               + ["B.%s=%s" % (q, pb[q]) for q in miss_b[:3]])
            return dict(base, ok=False, pairs=straight,
                        why=("線束，訊號腳 %d 支靠名稱唯一對上、%d 支對不上"
                             "（例：%s）—— 線束接法不在 netlist 裡，請提供 mate_map"
                             % (len(mapping), len(miss) + len(miss_b), sample)))
        same = sum(1 for p, q in mapping.items() if p == q)
        return dict(base, ok=True, pairs=sorted(mapping.items()),
                    why=("線束，%d 支訊號腳全部靠名稱唯一對上（其中同號 %d）"
                         % (len(mapping), same)))

    def mate_partners(self, board, refdes):
        """對接對手查詢 —— **唯一**的實作。

        曾經有兩處各自實作、其中一處只查正向，導致以相反順序宣告的 mate 讓整條
        訊號從 CSV 靜默消失。任何呼叫點都必須用這個 helper。
        """
        out = []
        for ba, ra, bb, rb in self.mates:
            if (ba, ra) == (board, refdes):
                out.append((bb, rb))
            elif (bb, rb) == (board, refdes):
                out.append((ba, ra))
        return out

    # ---- 工具 -------------------------------------------------------------
    def is_power(self, net):
        """這條 net 要不要在追跡時跳過。

        兩個來源，**故意只取其中一半的 `cls()`**：

        - 使用者的 `power_net_regex` —— 電源軌歸這裡管。軌的命名是各專案
          自己的（`6V_R`、`3P4V_P`、`28V_A`），而**誤判一條軌的代價是訊號
          路徑無聲消失**，所以那個判斷留給人。
        - `cls(net) == "GND"` —— 地歸這裡管。「`AGND`／`PGND`／`28V_GND_PM_2`
          是地」是 EDA 通用慣例不是專案私有規則，`cls` 從 v1.8.0 就認得了，
          而地永遠不是訊號路徑，判它不需要任何專案知識。

        ⚠️ **不可以直接用 `cls(net) != "SIG"`。** `cls` 會把 `6V_CS_PM_2`
           （電流偵測）、`3P4V_FB_PM_2`（回授）判成 `PWR:*`——那些其實是
           訊號腳，整個接過去等於把它們的路徑安靜地砍掉。`cls` 的
           `_RX_CTRL` 只擋 `EN`/`PG`，沒擋 `CS`/`FB`。

        ⚠️ **`cls` 是先判地、再判控制訊號**，所以 `GND_SENSE`、`PGND_FB`、
           `GND_EN` 會被它回成 `GND`。那些是 Kelvin 偵測回授一類的**訊號**，
           在 power 板上是正常設計。自動判地時要先把它們排掉——自動判錯的
           代價是路徑無聲消失，而使用者正則仍可覆蓋回來（人講的最大）。
        """
        if not net:
            return False
        if self.power_rx and self.power_rx.match(net):
            return True
        return self.cls(net) == "GND" and not self._RX_SENSE.search(net.upper())

    def norm(self, n):
        if n is None:
            return None
        for rx, rep in self.norm_rules:
            n = rx.sub(rep, n)
        return n

    # 地線與電源軌的命名慣例 —— 這是 **EDA 通用慣例**，不是某個專案的私有規則，
    # 所以寫死在這裡而不是丟給設定檔。⚠️ 只認「整個 token」，不做子字串比對：
    # `TX_PGA_LOAD` 不可以因為含有 `PG` 就被當成 power-good。
    _RX_GND = re.compile(r"(^|_)[A-Z]*GND[A-Z0-9]*(_|$)")
    _RX_CTRL = re.compile(r"(^|_)(EN|PG|PGOOD|PWRGD|POK)(_|$)")
    # 名字裡帶這些 token 的，**不自動當成地**（`GND_SENSE`、`PGND_FB` 是
    # Kelvin 偵測回授一類的訊號，在 power 板上是正常設計）。只擋自動判定，
    # 使用者的 power_net_regex 仍然可以把它判成電源。
    _RX_SENSE = re.compile(r"(^|_)(SENSE|SNS|FB|CS|EN|PG|PGOOD|PWRGD|POK|"
                           r"DET|ALERT|MON)(_|\d|$)")
    _RX_RAIL = re.compile(r"(^|_)(-?\d+P\d+V|-?\d+V\d+|-?\d+V)(_|$)")

    @staticmethod
    def cls(n):
        """腳位類別 —— 跨板唯一可靠的不變量。

        認得的地線寫法：token 的核心是 `GND`，前後都可以有裝飾 ——
        `GND` / `AGND` / `PGND` / `GND_A` / `GNDL` / `GND_EARTH2` /
        `28V_GND_PM_2`。認得的電源軌寫法：帶 `VDD`/`VCC` 的，以及
        `3P3V_P` / `1P8V` / `28V_A` / `-5V_A` / `MRAM_3V3_DPU` 這類
        「（負號）數字 + V」慣例。

        ⚠️ 控制訊號優先於電源軌：`28V_EN_PM_2` 是 enable，不是 28 V 軌；
           `FE_-5V_EN` 是 enable，不是 -5 V 軌。

        ⚠️ **回傳 `SIG` 的意思是「不認得」，不是「確定是訊號」。**
           它是 catch-all：真的訊號、以及任何沒見過的電源／地寫法，
           都落在這裡。任何拿 `SIG` 當**反證**的地方都是 bug ——
           要判斷兩支腳矛不矛盾請用 `contradicts()`，不要直接比 `cls()`。
        """
        if n is None:
            return None
        u = n.upper()
        if u == "GND" or Fabric._RX_GND.search(u):
            return "GND"
        # 名字帶電壓或 VDD，但其實是 enable / power-good 之類的控制訊號
        if u.endswith(("_EN", "_PG", "_PGOOD")) or Fabric._RX_CTRL.search(u):
            return "SIG"
        m = Fabric._RX_RAIL.search(u)
        if m:
            return "PWR:" + m.group(2)
        if "VDD" in u or "VCC" in u:
            return "PWR"
        return "SIG"

    @staticmethod
    def is_rail(c):
        """這個類別是不是「正面辨識出來的電源／地」。

        `SIG` 不算 —— 它是 `cls` 的 catch-all，代表「不認得」而不是
        「確定是訊號」。
        """
        return c == "GND" or (c or "").startswith("PWR")

    @staticmethod
    def contradicts(ca, cb):
        """兩支腳的類別算不算矛盾。

        ⚠️ 判準是**雙方都要有正面證據**。認不得的名字會落到 `SIG`，把它
           當反證等於宣稱「我沒見過這種寫法 == 這兩塊板對不上」—— v1.8
           修過一次（`AGND` 被判成 SIG，害 5 組對接卡在 ambiguous），但
           逐板補正規式追不完：實測 T_RADAR 兩塊板，`GLOBAL_GNDL/R` 的
           尾綴字母又漏一次，48 V 電源入口 6 腳裡 3 腳被誤計成矛盾。

           所以這裡改成：**不認得就不計分**，把判定責任交還給
           `MATE_MIN_SEMANTIC` 那條正面證據下限。最壞結果是證據不足、
           停在 `ambiguous` 要人確認 —— 而不是憑空生出矛盾，看起來像
           佈局真的對不上。
        """
        if not (Fabric.is_rail(ca) and Fabric.is_rail(cb)):
            return False
        if ca == cb:
            return False
        # 無電壓標的 `PWR`（只靠名字帶 VDD/VCC 認出來的）是弱證據，
        # 不足以推翻帶電壓標的那一側。
        if ca.startswith("PWR") and cb.startswith("PWR") and "PWR" in (ca, cb):
            return False
        return True

    def _pn(self, board, refdes):
        b = self.bom.get(board)
        if b is None:
            return ""
        pn = b.pn(refdes)
        return pn or ""

    def package_of(self, board, refdes):
        """惰性 package 解析：只查已宣告的例外表，不做任何推定。

        ⚠️ **不得以已接腳數推定 package** —— `pins()` 只含已接腳，系統性低估，
           會穩定偏向較小的封裝。
        """
        key = "%s:%s" % (board, refdes)
        if key in self.part_package:
            return self.part_package[key]
        return self.part_package.get(self._pn(board, refdes))

    # ---- 對接驗證 ---------------------------------------------------------
    def verify_mating(self, verbose=True):
        report = []
        for ba, ra, bb, rb in self.mates:
            d = self._mate_decision(ba, ra, bb, rb)
            n = len(self.nl[ba].pins(ra))
            status = self.mate_status.get((ba, ra, bb, rb), "unapproved")
            row = dict(a="%s.%s" % (ba, ra), b="%s.%s" % (bb, rb), pins=n,
                       form=d["form"], match=d["match"], bad=d["bad"],
                       ok=d["ok"], decision=d["why"], status=status,
                       evidence=self.mate_evidence.get((ba, ra, bb, rb), ""),
                       kind=self._mate_kind(ba, ra, bb, rb))
            report.append(row)
            if verbose:
                print("  %s <-> %s  (%d pin, %s)" % (row["a"], row["b"], n, row["kind"]))
                print("        判定 %s%s" % ("" if d["ok"] else "!! ", d["why"]))
                note = {"approved": "已由 mate_map 批准",
                        "inferred": "工具定案 [?]（板子實際接過可以用；"
                                    "netlist 連得上即事實）",
                        "ambiguous": "**工具定不了** -> 下游帶 mate:ambiguous，"
                                     "請確認配對或提供 mate_map"}
                print("        定案狀態 %s —— %s" % (status, note.get(status, "")))
                ev = self.mate_evidence.get((ba, ra, bb, rb))
                if status == "approved" and ev:
                    print("        證據 %s" % ev)
        return report

    def _mate_kind(self, ba, ra, bb, rb):
        if self.mate_form(ba, ra, bb, rb) == "harness":
            return "兩側同型 -> 中間有線束，腳位對應由線束決定"
        return "公母直接對接 -> 直通"

    # ---- 模型解析 ---------------------------------------------------------
    def _model_at(self, board, refdes):
        """回傳 (name, model, edges, caveats)。

        封裝判定**只用 netlist 與 BOM**（`ndd_package.resolve`）：模型宣告的
        電源／接地腳實際接在哪、訂購碼後綴、footprint 名稱。推論出來的會帶
        `package:inferred`，可以用但要標 `[?]`；證據不足則不給 edges。
        """
        key = (board, refdes)
        if key in self._model_cache:
            return self._model_cache[key]
        nl, bom = self.nl[board], self.bom[board]
        declared = self.package_of(board, refdes)
        res = ndd_package.resolve(self.models, nl, bom, board, refdes,
                                  self.cls, declared)
        self.pkg_res[key] = res
        m = res["model"]
        cav = list(res["caveats"])
        edges = None
        if m is not None:
            miss = missing_pins(m, nl.pins(refdes).keys())
            if miss:
                cav.append("model:pin_absent")
            elif not [c for c in cav if c in _BLOCKING]:
                edges = transfer_edges(m)
        out = (res["name"], m, edges, cav)
        self._model_cache[key] = out
        return out

    def _passive_edges(self, board, rd):
        """兩腳被動件確實雙向導通，且不需要 datasheet。"""
        from ndd_models import TWO_PIN_FOOTPRINT_PREFIX
        nl = self.nl[board]
        fp = nl.parts.get(rd, "")
        if fp.startswith(TWO_PIN_FOOTPRINT_PREFIX) and len(nl.pins(rd)) == 2:
            return [("1", "2", "bidirectional", {}),
                    ("2", "1", "bidirectional", {})]
        return None

    def _rail_state(self, board, refdes, pin, polarity):
        """gate 的實體腳實際接到什麼 —— `always` 的唯一正面證據來源。"""
        nl = self.nl[board]
        net = nl.pin_net(refdes, pin)
        if not net:
            return C.UNKNOWN, "unconnected"
        if len(nl.net(net)) == 1:
            return C.UNKNOWN, "floating"        # 懸空致能腳本身就是設計問題
        cls = self.cls(net)
        if polarity == "low" and cls == "GND":
            return C.ALWAYS, "tied_gnd"
        if polarity == "high" and cls and cls.startswith("PWR"):
            return C.ALWAYS, "tied_pwr"
        if cls == "GND" or (cls and cls.startswith("PWR")):
            return C.CONDITIONAL, "tied_inactive"
        return C.CONDITIONAL, "driven_by:%s" % net

    # ---- 走圖 -------------------------------------------------------------
    def neighbours(self, node):
        """回傳 [(next_node, why, caveats, gating), ...]。**只含 trace graph 的邊。**"""
        board, rd, pin = node
        nl = self.nl[board]
        out = []

        net = nl.pin_net(rd, pin)
        if net and not self.is_power(net):
            for rd2, p2 in nl.net(net):
                if (rd2, p2) != (rd, pin):
                    out.append(((board, rd2, p2), "net:%s" % net, [], C.ALWAYS))

        name, m, edges, cav = self._model_at(board, rd)
        if edges is None and m is None and not cav:
            edges, name = self._passive_edges(board, rd), "2-pin passive"
        if edges:
            rail = lambda p, pol: self._rail_state(board, rd, p, pol)
            for nxt_pin, edge in outgoing(edges, pin):
                gating, notes = (derive_gating(m, edge, rail) if m
                                 else (C.ALWAYS, []))
                why = "thru:%s|%s %s->%s" % (rd, name, pin, nxt_pin)
                if notes:
                    why += " [%s]" % ",".join(notes)
                out.append(((board, rd, nxt_pin), why, list(cav), gating))

        for nxt, mcav in self.mate.get((board, rd, pin), []):
            out.append((nxt, "mate", list(mcav), C.ALWAYS))
        return out

    def endpoint_of(self, node):
        """沒有可用 transfer 邊時，這支腳算哪一種端點。

        回傳 (endpoint_kind, reason, caveats)；可繼續穿越時回傳 None。

        ⚠️ 舊版是 `return pairs is None`——「沒有模型」直接等於「訊號在此結束」，
           於是未建模的穿越件被寫進 loads 當**負載**。這與「停在具名位置」相反。
        """
        board, rd, pin = node
        nl = self.nl[board]
        if nl.is_mech(rd) or self.mate_partners(board, rd):
            return None                          # 連接器：由 mate 邊繼續
        name, m, edges, cav = self._model_at(board, rd)
        blocking = [c for c in cav if c in _BLOCKING]
        if blocking:
            return (EP_UNKNOWN_UNUSABLE,
                    ",".join(_BLOCKING[c] for c in sorted(blocking)), list(cav))
        if edges is None and m is None and not cav:
            edges = self._passive_edges(board, rd)
        if edges and outgoing(edges, pin):
            return None
        if edges and any(pin == b for _a, b, _d, _e in edges):
            # 走到 transfer 邊的**輸出**端且無法再前進 = 這裡是訊號來源。
            # 把它算進 loads 會把驅動器講成負載。
            return EP_DRIVER, "反向走到驅動端輸出腳", []
        declared = (self.endpoints.get("%s:%s" % (board, rd))
                    or self.endpoints.get(self._pn(board, rd)))
        if declared == "terminal":
            return EP_TERMINAL, "", []
        if declared == "stateful":
            return EP_STATEFUL, "", []
        if declared == "unknown_stop":
            return EP_UNKNOWN_DECLARED, "declared", []
        # ⚠️ 預設 unclassified**必須**保留在輸出裡。若把未宣告的一律當
        #    unknown_stop 排除，絕大多數真實終端負載會在被宣告前全部消失，
        #    trace 的主要產出就報廢了。
        #
        # 但它**不掛 caveat**：這是覆蓋率缺口，不是路徑證據不足。狀態由
        # `endpoint_kind` 欄呈現，待辦由 coverage / REVIEW.md 列出。
        return EP_UNCLASSIFIED, "undeclared", []

    def trace(self, start, stop_fn=None, max_depth=16, stay_on=None):
        """BFS。caveats 與 gating 沿路徑累積 —— 標記不傳遞等於沒標。

        `stop_fn` 給定時用它判斷終點（例如「走到中繼連接器就停」）；否則用
        endpoint 分類。回傳 {node: (path, endpoint or None)}。

        `stay_on` 給板名時＝**板內追蹤**：走到別塊板的邊**在產生的當下就擋**，
        邊界記在本板這一側的節點上（`EP_BOUNDARY`），對面腳位只寫進 reason
        字串。對面的節點不進 `seen`、不進 path，所以對面的 caveats/gating
        不會污染板內結論，階層加註也不會拿對面的 refdes 去查本板的表。
        """
        seen = {start: None}
        q = deque([(start, 0)])
        ends = {}
        while q:
            node, d = q.popleft()
            if d >= max_depth:
                continue
            for nxt, why, cav, gating in self.neighbours(node):
                if stay_on is not None and nxt[0] != stay_on:
                    # ⚠️ 一定要顯式記這個 leaf。連接器腳位在 endpoint_of 會因
                    #    mate_partners 為真而提早回傳 None，只擋邊不補記的話，
                    #    這支腳會從輸出裡**無聲消失**。
                    # 邊界那一列**自己**要背這條 mate 的 caveat——它宣稱了
                    # 對面是哪支腳，對映不確定時這個宣稱就不確定。其他板內
                    # 落點不受影響：那條 mate 邊從沒進過它們的 path。
                    ends.setdefault(node, (self._path(seen, node),
                                           (EP_BOUNDARY,
                                            "-> %s %s.%s" % nxt, list(cav))))
                    continue
                if nxt in seen:
                    continue
                seen[nxt] = (node, why, cav, gating)
                if stop_fn is not None:
                    if stop_fn(nxt):
                        ends[nxt] = (self._path(seen, nxt), None)
                    else:
                        q.append((nxt, d + 1))
                    continue
                ep = self.endpoint_of(nxt)
                if ep is not None:
                    ends[nxt] = (self._path(seen, nxt), ep)
                    if ep[0] in (EP_UNKNOWN_UNUSABLE, EP_UNKNOWN_DECLARED):
                        continue                 # 停在具名位置，不再往下猜
                else:
                    q.append((nxt, d + 1))
        return ends

    @staticmethod
    def _path(seen, node):
        out = []
        while seen.get(node):
            prev, why, cav, gating = seen[node]
            out.append((prev, why, node, cav, gating))
            node = prev
        return list(reversed(out))

    @staticmethod
    def path_caveats(path, extra=()):
        cav = set(extra)
        for _p, _w, _n, c, _g in path:
            cav.update(c)
        return cav

    @staticmethod
    def path_gating(path):
        return C.worst_gating([g for _p, _w, _n, _c, g in path])

    def hop_string(self, path):
        steps = []
        for _prev, why, node, _cav, _g in path:
            b, rd, pin = node
            if why.startswith("thru:"):
                body = why.split(":", 1)[1]
                rdname, rest = body.split("|", 1)
                steps.append("%s %s(%s)" % (b.upper(), rdname, rest))
            elif why == "mate":
                steps.append(">> %s %s.%s" % (b.upper(), rd, pin))
        return " | ".join(steps)

    # ---- hint graph（**不可被 BFS 使用**）---------------------------------
    def collect_hints(self, board=None):
        """列舉功能影響關係。這些是**功能說明，不是連通**。"""
        rows = []
        keys = [board] if board else list(self.nl)
        for b in keys:
            nl = self.nl[b]
            for rd in nl.parts:
                _n, m, _e, _c = self._model_at(b, rd)
                if not m:
                    continue
                mpn = self._pn(b, rd)
                for inf in control_influences(m):
                    for p in inf.get("from", []):
                        rows.append(dict(
                            board=b, refdes=rd, pin=str(p),
                            net=nl.pin_net(rd, str(p)) or "",
                            mpn=mpn, relation="control_influence",
                            effect="%s -> %s" % (inf.get("effect", ""),
                                                 inf.get("affects", "")),
                            source=inf.get("verified_against")
                                   or m.get("verified_against", "")))
                for e in m.get("transfer") or []:
                    for p in e.get("parameter_control", []):
                        rows.append(dict(
                            board=b, refdes=rd, pin=str(p),
                            net=nl.pin_net(rd, str(p)) or "",
                            mpn=mpn, relation="parameter_control",
                            effect="改變訊號性質，不改變是否導通",
                            source=e.get("verified_against")
                                   or m.get("verified_against", "")))
        self.hints = rows
        return rows
