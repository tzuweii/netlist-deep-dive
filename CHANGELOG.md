# 變更紀錄

本專案的版本標籤同時是**可回溯的對比點**：

```bash
git diff v0-baseline v1.0.0 --stat     # 全部差異
git log v0-baseline..v1.0.0            # 逐個 commit
git show v1.0.0                        # 該版的完整說明
```

---

## v1.0.0 — 讓工具在「查不到」時說出查不到

`c567540`　5 個 commit　19 檔　+3760 / −617

這一版的主軸只有一句：**把每個「會通過、但通過是空的」檢查改成會出聲。**

空的綠燈是這個專案自己定義的最高危險類別（「看起來合理但是錯的」），所以多數
改動的效果是**讓輸出變得更保留**，而不是更多。

### 修掉的靜默錯誤

| 位置 | v0 行為 | v1.0.0 |
|---|---|---|
| `ndd_pads.selfcheck()` | 一支腳掛兩條 net 時，pin token 數與 nets 收錄數**都仍相等** → 回報 PASS，第一條 net 從 `pinmap` 靜默消失 | 增比 `pinmap` 收錄數，具名指出 `U1.1 -> NET_A / NET_B` |
| `ndd.py` `cmd_trace` | 中繼連接器的對手查詢只查正向 | 統一為單一雙向 helper |
| `ndd_models` `pairs` | 對稱導通：單向緩衝器可逆走；fanout 走 `B3→1→B7`，**憑空捏造不存在的路徑** | 有向 `transfer`，`direction` 顯式且無預設值 |
| `ndd_graph.is_terminal()` | `return pairs is None` —— 「沒有模型」直接等於「訊號在此結束」，未建模的 bus switch 變成**負載** | endpoint 五態，`unknown_stop` 停在具名位置不進 `loads` |
| `ndd_bom` 重複 refdes | 後列靜默覆蓋前列 | ambiguity sentinel，相關斷言一律 FAIL 並列出衝突列號 |
| `match_model` | 走 dict 順序取第一個子字串命中 | 精確 MPN → 唯一最長 token（含邊界）→ 同分報錯 |

兩個實測過的具體案例：

- **mate 方向**：同一塊板、同一條訊號，`ndd.json` 裡 `["a","JS1","b","JS2"]` 產出
  2 列、`["b","JS2","a","JS1"]` 產出 **0 列**，且無任何警告。而 v0 從未在任何
  文件裡規定過宣告順序。
- **pin 兩 net**：`pin_tokens` 與 `nets` 收錄數在該情境下**都是 2**，只有 `pinmap`
  少一筆。這個檢查的定位是**parser 誤框偵測器**（某行沒被認成 `*SIGNAL*` 標頭，
  底下的腳被歸到前一條 net），**不代表設計短路**——原理圖工具遇到短路會把兩條
  net 合併成一條再匯出。

### v0 最大的結構問題：排名從未進入走圖

`ndd_graph.py:44-47` 只要兩側 pin number 相同就連邊，`rank_mating()` 的結果
**完全沒有被使用**。所以 `mate` 印出「!! 無法唯一判定」之後，`trace` 照樣產出
看起來確定的鏈路——直接違反 `SKILL.md` 自己的硬性規則「不要把兩側樣式相符當成
對接的證據」。

v1.0.0 新增 `mate_map`（已批准的腳位對映 + `evidence`），載入時驗證：

- 映射完整性
- **單射** —— 兩個 A 腳映到同一 B 腳會**靜默合併兩條 net**，而合併後的圖看起來
  完全正常。這是所有 mapping 錯誤裡後果最大的一種
- 雙向往返一致
- pin 存在性

未批准時 `trace` 仍可跑（Phase 0→4 順序上第一天拿不到線束圖），但每條邊帶
`mate:unapproved` 並沿路徑傳到 CSV。**那些是候選路徑，不是已確認的對接結論。**

### 新增的三條分界

| 分界 | 內容 |
|---|---|
| **傳輸 vs 功能影響** | `signal_chain.csv` 只含已驗證的 `signal_transfer` 邊；control／stateful 去 `topology_hint.csv`，**永不作為 BFS 的下一跳**。分類單位是**邊**不是元件——同一顆 IC 可以同時有 transfer 邊、control 腳與狀態行為 |
| **證據品質 vs 閘控狀態** | `confidence`（`confirmed`/`caveated`/`unknown`）與 `gating`（`always`/`conditional`/`unknown`）拆成兩欄。合併成一軸會讓「查證完整的 runtime-gated 路徑」排在「package 未佐證但恆通」之下，與事實相反 |
| **BOM 涵蓋範圍 vs 正確性** | **使用者提供的 BOM 就是這塊板的權威**——缺席即未貼件 (DNI)，不做變體推理。`bom_scope` 標的是**文件涵蓋哪類零件**：`smt_only` 的 BOM 依定義不列連接器/測試點/手插件，那一類的缺席不可判定，但 SMT 件的缺席照樣是 DNI |

### 封裝判定：只用 netlist + BOM

開發過程中先試過「解析 datasheet 腳位表」，用真實 NXP PDF 一測就破功——同一份
16 腳的表同時踩到三種文字層問題：腳註標記讓整列比不到、符號與資料被拆成兩行、
**兩個腳號被併成一個**（`VSS 86` 其實是 pin 8 與 pin 6）。換一家又是另一組，
而且**一列錯位就讓整張表作廢**。

改用這個工具已經有的機制（`mate` 的枚舉排名），三個證據來源全部來自 netlist
與 BOM：

| 來源 | 標記 | 判別力 |
|---|---|---|
| 模型宣告的電源/接地腳實際接法 | `[N]` | **最強**，候選封裝差最多的就是電源腳位置 |
| 訂購碼的封裝後綴 | `[B]` | 強 |
| footprint 名稱 | `[N]` | 中 |

關鍵規則：**宣告的 GND/PWR 腳在 netlist 上完全沒接 = 矛盾**，不是「資料不足」
——訊號腳可以 NC，電源腳不會。

判定結果分五態；`inferred` 的門檻是「零矛盾 + 唯一勝出（margin ≥ 2）+ 至少一個
獨立來源佐證」，**推論一律標 `[?]`** 並帶 `package:inferred` caveat；達不到就
列入待補，**不替使用者假設**。使用者明確宣告優先，但與 netlist 矛盾時仍報
`conflict`——人講的最大，但矛盾要講出來。

`pinfn` 退回本分：抽到**一筆**才自動寫快取（**已證明**無歧義）；抽到多筆就攤開
全部原文、頁碼與腳號欄位，**拒絕替使用者挑也不寫快取**。

### 其他

- 新增 `manifest`（含 `.asc` / BOM / datasheet / **`ndd.json`** 的完整 SHA-256
  與工具版本）與 `coverage`（per-MPN 三源覆蓋）
- **移除內建的 4 個 seed model** —— 通用型工具不應把特定料號當成預設知識，
  預先建模等於把「你對 datasheet 的解讀」凍結成看不見的永久資產
- 新增 `SPEC.md`（行為契約）與 `references/models.md`

### 測試：0 → 53

v0 的 `tests/` 不存在，而 README 的賣點是「把驗證本身也工具化」。

現在有 53 個合成 fixture 回歸測試，全部不依賴客戶專案檔案、私有 datasheet 或
特定 refdes。**測試在開發過程中抓到三個實作錯誤**：

1. `transfer_for` 在封裝未定案時仍回傳 edges —— 等於用未定案的腳位對應產生
   看起來確定的路徑
2. `shared_pinout` 只比 pin label 集合 —— 兩個封裝都是 1–N 時必然通過，
   **等於把要解決的問題當成答案**
3. 宣告的電源腳未接沒有計為矛盾

三個都是「看起來合理但是錯的」那一類，靠讀 code 不容易發現。

### 破壞性變更

| 變更 | 遷移方式 |
|---|---|
| `models.json` 的 `pairs` → `transfer` | **不做靜默轉換**。舊 schema 的對稱性正是要修掉的錯誤，直接轉換會把錯誤帶進新 schema。必須重新核對 datasheet 並補 `direction` |
| `ndd.json` 的 `bom_kind` → `bom_scope` | 給明確錯誤與對應表：SMT BOM → `smt_only`，其餘一律 `complete`（預設） |
| `verified-pins.csv` 舊列 | 一律視為 `unresolved_pending_user`；SHA-256 由 16 字元前綴改為完整值 |
| 內建 seed model | 已移除，需自行在專案 `models.json` 查證後加入 |

輸出格式變動：`signal_chain.csv` 新增 `caveats` / `confidence` / `endpoint_kind`
且 `loads` 欄語意改變；`pinmap_<board>.csv` 新增 `bom_scope`、`stuffed` 新增
`UNKNOWN` / `AMBIGUOUS`；新增 `topology_hint.csv`。

### 從 v0 專案升級

**原始檔（`.asc` / BOM / datasheet）完全不用動。** 要改的只有設定與模型，
而且工具會逐項擋下來並告訴你怎麼改——不會靜默沿用舊值。

依實際出現的順序：

#### 1. `models.json` 的 `pairs`（若有建過模型）

```
!! models.json 載入失敗：
模型 `BUF`：使用了已移除的 `pairs` schema。這是**破壞性遷移**，不做靜默轉換
——舊 schema 的對稱性正是要修掉的錯誤，直接轉換會把錯誤帶進新 schema。
```

**不要機械轉換。** 舊 `pairs` 沒有方向資訊，照抄過來只會把「單向元件可雙向走」
這個錯誤帶進新 schema。要重新翻 datasheet 補三件事：

```json
{
  "BUF": {
    "match": ["BUF_A"],
    "kind": "signal_transfer",
    "package": "TSSOP20",
    "verified_against": "buf.pdf p.3 Table 4-1",
    "pin_roles": {"10": "GND", "20": "PWR"},
    "transfer": [{"from": ["2"], "to": ["18"], "direction": "forward",
                  "gate": {"all_of": ["1"]}}],
    "control": {"1": {"type": "enable", "polarity": "low",
                      "mechanism": "strap"}}
  }
}
```

- `direction` —— `forward` 或 `bidirectional`，**沒有預設值**
- `pin_roles` —— VSS/VDD 是哪幾支腳。多封裝料號沒填就無法判別封裝
- `control` —— 由字串改成結構化（`type` / `mechanism` / `polarity`）

沒建過模型的專案可略過這一步（v0 內建的 4 個 seed model 已移除，追跡會停在
主動件並標 `unclassified`）。

#### 2. `ndd.json` 的 `bom_kind` → `bom_scope`

```
board 'a' 仍使用已改名的 `bom_kind`。請改為 `bom_scope`，對應：
SMT BOM -> smt_only、完整 BOM -> complete、未標註 -> unknown。
**不得預設 complete** —— 那會把未知範圍的缺件誤報成真 DNI。
```

每塊板都要改。**只有拿到 SMT BOM 時填 `smt_only`，其餘一律 `complete`**
（預設值）。BOM 是這塊板的權威，工具不做變體推理。

#### 3. 其餘新欄位：不用補

`mate_map`、`part_package`、`endpoints` 缺少時走預設空值，**不報錯**：

- 無 `mate_map` → `trace` 照跑，每列帶 `mate:unapproved`（候選路徑）
- 無 `part_package` → 封裝由 netlist/BOM 證據推論，標 `[?]`
- 無 `endpoints` → 端點預設 `unclassified`，仍進 `loads` 但帶 caveat

想拿到 `confirmed` 等級的結論再逐步補。跑 `ndd.py review` 會列出待補清單。

#### 4. `verified-pins.csv`：自動處理，但要重新確認

舊列一律標為 `unresolved_pending_user`：

```
原文快取（1 筆，其中 1 筆為待重解析的舊 schema）
  ADC_A  pin 1  AIN0  Input  adc.pdf p.4  [-/unresolved_pending_user]
```

原文還在（可以讀），但**不會被當成已解析的快取使用**——舊列缺 `package`
欄，當成有效讀入等於把未鎖定封裝的資料洗成合法覆蓋。需要哪支腳就重跑 `pinfn`。

#### 5. 衍生產物：刪掉重跑

`export/`、`REVIEW.md`、`MANIFEST.md` 都是可重生的。欄位已變動（`signal_chain.csv`
新增 `caveats` / `confidence` / `endpoint_kind`，`loads` 語意改變），直接刪掉重跑：

```bash
rm -rf export REVIEW.md
PYTHONIOENCODING=utf-8 python ndd.py --config <你的>/ndd.json audit
python ndd.py coverage    # 看新的待補缺口
python ndd.py trace
python ndd.py review
```

#### 預期的落差

升級後**結論會變少、標記會變多**，這是預期的：v0 產出的某些鏈路其實建立在
未批准的對接、或未經方向驗證的模型上。`coverage` 與 `REVIEW.md` 會告訴你
差在哪裡。

### 已知限制

**尚未用真實專案驗證。** 以下只跑過合成 fixture：

- BGA 腳位（`A1`、`AN12`）—— `cls()`、`pin_roles`、排序只測過數字腳號
- 大規模（數千 net、上百顆 IC）—— 無效能資料
- 真實 footprint 命名 —— `footprint_match` 的命中率未知
- 多板 + 真線束的 `mate_map`

`MIN_MARGIN = 2` 與「佐證數 ≥ 1」這兩個門檻沒有真實數據校準過。

其他：`references/datasheets.md` 未跟上 `--pick` 流程；`i2c_addr` 斷言未跟上
新 schema。

---

## v0-baseline — 改版前基準

`eabab9e`

PADS netlist + BOM 深度分析的初版：parser 自我驗證、refdes 交集配對 BOM、
datasheet 原文快取與 hash、連接器對接枚舉排名、宣告式斷言引擎、人工複驗清單。

工具 1745 行、文件 633 行、**測試 0 個**、內建 4 個 seed model。

保留為 tag 供對比：`git checkout v0-baseline`。
