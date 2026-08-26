---
name: netlist-deep-dive
description: 由 PADS 2000 ASCII netlist (.asc) 與 PCBA BOM (.xlsx) 做深度電路架構分析，產出可查詢的 pinmap CSV、跨板端到端訊號鏈、架構文件與人工複驗清單，並附自動稽核。當使用者提供 .asc/BOM 要求分析電路架構、追訊號、找 DNI、盤點 IC、建立板級文件，或要求驗證既有架構文件是否仍與 netlist 相符時使用。跨專案通用。
---

# 電路深度分析（netlist deep dive）

輸入只需要 **`.asc` netlist + BOM**。產出一整套：可查詢的 CSV、端到端訊號鏈、
架構文件、稽核報告、人工複驗清單。

## 開始前先讀

| 檔案 | 什麼時候讀 |
|---|---|
| `references/pitfalls.md` | **每次都讀。** 12 個已經踩過的坑，每個都會產生「看起來合理但是錯的」結論 |
| `references/verification.md` | **Phase 4 一定要讀。** 三層驗證方法，以及哪些東西**結構上驗不到** |
| `references/datasheets.md` | 需要腳位模型或要取得 datasheet 時讀 |

工具在 `scripts/`，進入點是 `ndd.py`。**所有指令都要加 `PYTHONIOENCODING=utf-8`**
（cp950 終端機會把中文輸出變亂碼）。

---

## 三源對照規約（最高原則，回答任何電路問題都適用）

固定五條，不會增長。**這是回答時執行的紀律，不是要產生的檔案。**

### 每個主張只能有一種合法來源

| 主張類型 | 唯一合法來源 | 標記 |
|---|---|---|
| 連線——誰接到誰 | netlist | `[N]` |
| 身分——料號、值、**有無貼件** | BOM | `[B]` |
| 腳位功能、內部行為、極性 | datasheet | `[D 檔名 p.x]` |
| 推論 | 上述組合 + 寫出推理過程 | `[?]` |

### 五條硬規則

1. **腳位功能是 `[D]`——但只適用於「訊號穿過」的元件。** 先分級：

   | 級 | 元件 | 判別依據 | datasheet 用於 |
   |---|---|---|---|
   | A 連接器／結構件 | SMP、SEAF/SEAM、TFML、T2M | **netlist 連上即事實** | 機構、電流額定 |
   | B RF 被動網路 | divider、splitter、combiner、coupler、balun、天線 PCB | **腳數 + 上下游網路即拓樸**（1→2、16→1）；`_P`/`_N` 成對即差動 | 損耗、頻寬、耦合量、相位平衡 |
   | C 純被動 | ferrite、PPTC、RTD、LED | netlist + BOM 值 | 額定、溫漂 |
   | D 主動 IC，訊號終點 | FPGA、ASIC、EEPROM、振盪器、運放 | netlist + BOM | 功能、暫存器、時序、相位雜訊 |
   | **E 主動 IC，訊號穿過** | bus switch、buffer、mux、fanout、SPDT | **一律要 datasheet** | — |

   **只有 E 級在追跡前必須有 `[D]`。** A/B 級把拓樸標 `[N]` 並寫出推理，
   **不要停在 `[D 缺]`**。E 級用 `ndd.py pinfn <料號> <腳位>` 抽原文，成本數十 token。

   ⚠️ 判定「缺」之前要用**全文**複核，不能只比檔名——family datasheet 與型錄類
   文件必然漏判（`OP284FSZ` 在 `OP184_284_484.pdf`；`PS1608GT2` 在
   `n_catalog_partition31_en.pdf`）。誤報「缺」會害人重複採購已持有的規格書。
2. **有無貼件一律 `[B]`。** netlist 有 ≠ 板上有。
3. **`[?]` 必須寫出定案方式。** 沒有定案路徑的推論不准寫進答案。
4. **衝突時**：連線→netlist、料號→BOM、腳位功能→datasheet；**且衝突本身要講出來**，
   不能默默選一個。
5. **不可得就說不可得。** 缺 datasheet → 標 `[D 缺]` 並列入使用者待補清單，
   **不得以推論代替**。追跡遇到查不到的 IC，**回報「停在 U6 (LMK00304SQ) pin15」
   而不是繼續猜**——停在具名位置是有用的答案，猜出來的完整鏈路是有害的答案。

### 執行機制：可見性

每個電路回答**附一張壓縮證據表**（主張 → 來源），並主動寫出「這次我沒查什麼」。
**沒有標記的主張，依定義就是未查證的**，使用者掃一眼就能抓到偷懶。

### 其他仍然適用的原則

- **通則一定要展開逐顆比對。** 文件寫 `U<n>03`、`A<n>02` 這種通則，而通則幾乎
  一定有例外（`role_rules` 展開檢查）。
- **跨板對接要枚舉排名，不能只看「兩側相符」。** 對稱的 GND 分布會讓幾十種錯誤
  對應全部「通過」。
- **netlist 描述設計，不描述手上那片板。** rework、飛線、換料都不在裡面。

### 產出物政策（避免累積會腐化的東西）

| 東西 | 定位 | 何時產生 |
|---|---|---|
| **回答本身** | **主要交付物** | 每次 |
| md 文件 | 只有使用者明確要求時 | 明確要求 |
| pinmap CSV | **可重生的衍生物**，不是文件 | 需要時重跑，過期就丟 |
| `verified-pins.csv` | **datasheet 原文快取**（見下） | `pinfn` 自動累積 |
| 腳位模型 `models.json` | **選用加速器**，不是前提 | 同一顆 IC 追第 2 次以上才值得建 |
| assertions | 附屬於**已存在的**文件 | 沒有文件就不需要 |

**原始來源是資產，結論是拋棄式的。** 囤積結論才是會腐化的東西。

---

## Phase 0 — 收檔案、建專案

1. 確認拿到的東西：每塊板一份 `.asc` + 一份 BOM。缺 BOM 就先問，**不要只靠
   netlist 做分析**——料號、DNI、被動元件值全部來自 BOM。
2. 把檔案放進一個分析資料夾（建議 `<專案>/analysis/`），然後：

```bash
PYTHONIOENCODING=utf-8 python scripts/ndd.py init "C:/path/to/analysis"
```

`init` 會用 **refdes 交集**配對 netlist 與 BOM（不是用檔名猜——檔名常含共通
token，猜錯不會有任何跡象）。命中率 < 90% 或與次佳差距 < 30% 會標 `!! 需人工確認`。

3. 人工補完 `ndd.json`：
   - `boards[*].bom_kind` — **是 SMT BOM 還是完整 BOM？**這直接影響 DNI 判讀
     （SMT BOM 本來就不含連接器、測試點、鎖孔）
   - `mates` — 連接器對接關係，`[板A, 連接器A, 板B, 連接器B]`
   - `net_normalize` — 兩側命名習慣的差異（見 `pitfalls.md` #8）
   - `trace.start` / `trace.slot_pattern`

4. 用 **AskUserQuestion** 問清楚：這些板子怎麼組成一台？哪些連接器對接？
   有沒有線束？**不要自己猜拓樸。**

## Phase 1 — 盤點與初步理解

```bash
python scripts/ndd.py export      # pinmap_<board>.csv：逐腳事實表
python scripts/ndd.py audit       # 先跑一次，看 DNI 與懸空網路
python scripts/ndd.py part <關鍵字>
```

先看數量結構：某顆料 ×16、×9、×81 這種倍率，通常就是系統架構的直接反映
（例如 9 slot × 16 channel）。**先找出倍率，再解釋它。**

## Phase 2 — 取得 datasheet

```bash
python scripts/ndd.py datasheets              # 盤點 + 自動下載 + 產出 MISSING.md
python scripts/ndd.py datasheets --pn <料號> --url <你查到的網址>
```

自動下載只對少數原廠站有效（實測 TI、NXP 可；Microchip 403；代理商站回
bot-check HTML）。**流程**：先跑自動盤點 → 對 `MISSING.md` 裡的料號用
**WebSearch** 找官方 datasheet 網址 → 用 `--url` 抓下來 → 自製件／連接器／
機構件抓不到是正常的，留給使用者補。

細節與陷阱見 `references/datasheets.md`。

## Phase 3 — 查證腳位功能（規則 1 的執行方式）

```bash
python ndd.py pinfn LMX2594 8
#   lmx2594.pdf p.7:  8  OSCinP  Input  Reference input clock (+). ... Requires AC-coupling capacitor.
python ndd.py pinfn --list        # 看已快取了哪些
```

**先查快取，未命中才抽取；抽到的原文自動寫進 `verified-pins.csv`。**

快取的欄位是 `料號, 腳位, 腳名, 方向, 原文摘句, 檔名, 頁碼, datasheet SHA256, 日期`。

> **快取原文，永不快取解讀。**
> 快取的是 datasheet 的逐字內容 + 出處，不是「pin1 與 pin3 內部連通」這種
> 我推出來的拓樸結論。原文快取你 5 秒就能核對；推論快取會把錯誤凍結成永久
> 資產，而且沒人看得出來。datasheet 換版時 SHA256 不符會自動失效重抽。

抽取器已處理三種腳位表排版（腳號在前／腳名在前／腳名+多封裝欄），並過濾目錄頁
的假命中。⚠️ **多封裝欄時它會列出全部並警告，不會替你選**——`PCA9547` 的 pin 18
在 SO24 是 `SC6`、在 HVQFN24 是 `A2`，選錯就全錯。先從 netlist 腳數與 BOM 料號
後綴判斷封裝。

抽不到（掃描影像、表格特殊）時，**人工開 PDF 確認**，不要略過。

### 腳位模型（選用，非前提）

`models.json` 只在**同一顆 IC 被追第二次以上**時才值得建——建的時候是把已經查過
的那次順手記下來，不是預先猜哪些會用到。已內建 4 個查證過的：`SN74CBTLV3126`、
`SN74HCS244`、`PI49FCT3807`、`PCA9547`。

⚠️ **不要為了「先理解全盤電路」而預先大量建模。** 實測一個 3 板系統有 89 種主動
料號，其中 74 種是終端負載（FPGA、感測器、LDO…）根本沒有「穿越」可言；建了
83% 是空轉。而且模型是**你對 datasheet 的解讀**，大量預建等於把可能的錯誤凍結成
看不見的永久資產。**要預先投資，投資在補齊 datasheet，不是建模型。**

## Phase 4 — 驗證（先讀 `references/verification.md`）

```bash
python scripts/ndd.py mate        # 連接器對接：枚舉所有對應方式並排名
python scripts/ndd.py trace       # 端到端訊號鏈
python scripts/ndd.py audit       # 完整稽核（含 parser 自我驗證）
```

**`mate` 的判讀**：只有「直通唯一勝出且 margin 夠大」才算站得住。
margin ≤ 4 一定要在文件裡標明證據薄弱。工具也會自動判斷這個對接是
「兩側同型 → 中間有線束」還是「公母直接對接 → 只剩 footprint 方位問題」，
兩者的定案途徑完全不同。

**殘存候選要用「會不會壞」排除**：把每個殘存對應代入，看它會不會造成立即而
明顯的故障（例如 SDA/SCL 對調 → I2C 全滅）。若系統實際會動，該候選就被排除了。
這通常比找線束圖快，而且是**唯一能同時涵蓋 layout 正確性的證據**。

## Phase 5 — 寫文件

文件的分工要講清楚：**md 解釋「為什麼」，CSV 回答「是什麼」，稽核確認兩者一致。**
不要把逐腳資料抄進 md——那是 CSV 的工作，抄進去只會過時且產生通則的例外。

文件開頭必備：

- 來源檔清單 + **SHA256 前綴 + 日期**（改版時才知道文件過期了）
- 一句「逐腳查詢請用 CSV / `ndd.py`，不要靠本文」
- 書寫慣例：**每個 refdes 後面一律附料號**（`U103 (LMZ30606RKGT)`）

每寫一條可機械驗證的主張，就在 `ndd.json` 的 `assertions` 補一條。
寫完後跑 `audit`，**首次執行的 FAIL 就是文件的錯**，改文件而不是改斷言；
改完再把斷言改成驗證「更正後的事實」。

## Phase 6 — 人工複驗清單

```bash
python scripts/ndd.py review      # 產出 REVIEW.md
```

**這一步不可省略。** `REVIEW.md` 列出所有工具驗不到、必須人工確認的項目：
腳位模型、對接證據薄弱處、rework、線束、BOM 變體、因果推論。

交付時要明確告訴使用者：

> 工具驗得到的部分（數值、連線、數量、命名規則）已經驗過並列在 A 段；
> **B 段每一項都需要你人工確認**，工具無法代勞。

---

## 硬性規則

- **不要憑記憶寫腳位。** 一律 `pinfn` 查 datasheet 原文並標頁碼。沒有 datasheet
  就標 `[D 缺]`、停在具名位置，不要猜著補完。
- **不要預先大量建模。** 見 Phase 3 末。要預先投資就投資在補 datasheet。
- **不要為了「看起來完整」而省略證據表。** 沒標記的主張等於自承未查證。
- **不要用檔名猜配對**（netlist↔BOM、料號↔datasheet）。用 refdes 交集、用內容。
- **不要把「兩側樣式相符」當成對接的證據。** 要枚舉排名。
- **不要在文件裡混用事實與推論。** 推論一律標 ⚠️ 並寫清楚定案方式。
- **檔名含 CJK 相容表意字時**（例如 U+F99C 的「列」），Bash 會處理不了，
  改用 PowerShell 或 Glob/Read 工具，或先複製到 ASCII 路徑。
- **Windows 上 Python 讀不到 Git Bash 的 `/c/...` 路徑**，要用 `C:/...`。
