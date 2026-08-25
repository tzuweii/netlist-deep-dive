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

## 核心原則（違反其中任一條，產出就不可信）

1. **BOM 是料號的權威。** netlist 的符號名稱、設計文件的零件表都可能落後於換料。
   兩者衝突時一律以 BOM 為準，並把衝突記下來。
2. **腳位模型一定要翻過 datasheet。** 憑記憶或憑相近型號寫腳位，是這套流程唯一
   會產生「整張表都錯但看起來完全合理」的地方。`ndd_models.py` 會拒絕載入沒有
   `verified_against` 的模型——不要繞過它。
3. **通則一定要展開逐顆比對。** 文件為了可讀會寫 `U<n>03`、`A<n>02` 這種通則，
   而通則幾乎一定有例外。用 `role_rules` 展開檢查。
4. **跨板對接要枚舉排名，不能只看「兩側相符」。** 對稱的 GND 分布會讓幾十種錯誤
   對應全部「通過」。
5. **netlist 描述設計，不描述你手上那片板。** rework、飛線、換料都不在裡面。
6. **不確定就標記，不要補完。** 每一句推論標 ⚠️，並寫清楚要怎麼才能定案。

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

## Phase 3 — 建立腳位模型

只有需要**追訊號穿過某顆 IC** 時才要做。對每顆會被穿過的元件（bus switch、
buffer、mux、fanout、串阻）：

1. 開 datasheet 的 Pin Configuration / Pin Functions 那一頁
2. 寫進專案的 `models.json`（格式見 `ndd_models.py` 開頭）
3. **`verified_against` 要寫「檔名 + 頁碼 + 文件編號」**，不是寫「datasheet」

`scripts/ndd_models.py` 已內建 4 個查證過的模型：`SN74CBTLV3126`、
`SN74HCS244`、`PI49FCT3807`、`PCA9547`。**其他型號一律自己查，不要照抄相近型號。**

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

- **不要憑記憶寫腳位。** 沒有 datasheet 就不要建模型，寧可讓追跡停在那裡
  （工具的預設行為就是停住，不要改成用猜的繞過去）。
- **不要用檔名猜配對**（netlist↔BOM、料號↔datasheet）。用 refdes 交集、用內容。
- **不要把「兩側樣式相符」當成對接的證據。** 要枚舉排名。
- **不要在文件裡混用事實與推論。** 推論一律標 ⚠️ 並寫清楚定案方式。
- **檔名含 CJK 相容表意字時**（例如 U+F99C 的「列」），Bash 會處理不了，
  改用 PowerShell 或 Glob/Read 工具，或先複製到 ASCII 路徑。
- **Windows 上 Python 讀不到 Git Bash 的 `/c/...` 路徑**，要用 `C:/...`。
