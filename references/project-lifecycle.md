# 專案生命週期——建檔、補規格書、建模、寫文件、交付

> **什麼時候讀這份**：要建立新專案、補規格書、建元件模型、寫架構文件、或準備
> 交付。**平常回答電路問題完全用不到這裡的任何一條**，所以它從 `SKILL.md` 搬
> 出來——`SKILL.md` 每次叫用都整份進 context，這裡是需要時才讀。
>
> 這份文件的內容**一個專案大多只會執行一次**。

⚠️ **這台機器要裝 OrCAD Capture**（`init` 自動偵測 `C:\Cadence\SPB_*`），
沒有就無法處理 `.DSN`，`init` 會失敗而不是降級。**工具絕不寫入 Cadence 安裝目錄。**

---

## 1. 建檔（`init`）

> 資料夾裡已有 `ndd.json` = 已建過專案，**不要跑 `init`**（工具會擋）。
> 升級既有專案照 `UPGRADING.md` 做。

1. 每塊板**三份**：`.DSN` + `.asc` + BOM，丟進同一個資料夾。**缺任何一份就先問，
   不要開始。** 三份都是使用者主動提供的——工具不會去找、不會去猜、也不會少一份
   就降級跑。

   `.DSN` 若有子設計目錄，一併放進來。**設計不能開在 Capture 裡**（旁邊會有
   `.DSNlck`），否則轉換會無限等待；工具會先擋下來並要求你關閉。

2. **先看 init 打算怎麼做**（只讀，不寫任何檔案）：

```bash
python scripts/ndd.py init "C:/path/to/analysis" --plan
```

會印出三件事：netlist ↔ BOM 配對（**用 refdes 交集，不用檔名猜**）、連接器對接
候選與排名證據、datasheet 盤點。

3. **只有兩件事要問使用者**，用 `AskUserQuestion` 一次問完：

   - **BOM 配對** —— 只在 `--plan` 標「需你確認」時問，附候選清單
   - **datasheet** —— 下載還是跳過（跳過仍會產生缺件清單）

4. **一路跑完，中途不再停**：

```bash
python scripts/ndd.py init "C:/path/to/analysis" --run     [--bom <key>=<檔名>]... [--accept-pairing] [--accept-mates] [--no-datasheets]
```

先把每塊板的 `.DSN` 轉成階層 CSV 並對帳 `.asc`，再依序執行 `pinfn --import-symbols`
→ `export` → `datasheets` → `audit` → `mate` → `trace` → `coverage`
→ `manifest` → `review`，**任一步失敗不中止**，結果寫進 `SETUP.md`。

⚠️ **階層那一步是唯一會讓 `init` 直接中止的。** 它排在所有流程之前，因為
`.DSN` 與 `.asc` 對不起來就代表兩份檔案不是同一塊板／同一版，**後面每一個
結論都會建立在錯的基礎上而不會有任何症狀**。看到它停下來，先釐清檔案版本，
不要想辦法繞過。

對帳結果裡的「PADS 改名 N 條」是正常的：`.asc` 不收 `*`、`/` 這類字元，
formatter 會把整條 net 改名成 `X#####`。節點集合完全相同就是改名不是接錯，工具會列出對照表——
**`.DSN` 那邊才有設計者取的原名**，回答時用原名比 `X00697` 有意義得多。

產出：`ndd.json`、`SETUP.md`、`MANIFEST.md`、`REVIEW.md`、
`datasheets/MISSING.md`、`export/*.csv`、`hier/*.csv`、`verified-pins.csv`。

5. **跑完後我接手寫架構文件** —— 那是分析結論，腳本產不出來。

### init 自動決定與不決定的

| 項目 | 自動 | 條件 |
|---|---|---|
| `.DSN` ↔ `.asc` 配對 | ✅ | 用 refdes 交集（≥ 90%），不用檔名猜；配不上就**中止** |
| `.DSN` → 階層 CSV | ✅ | 自動找 `SPB_*`（取版本最高）；找不到 Capture 就**中止** |
| 階層與 `.asc` 對帳 | ✅ | 逐條比 net／節點／零件；**任何不一致都中止**，不是警告 |
| symbol 腳位名入庫 | ✅ | 同料號腳位名不一致時**不寫入**，列出來等人釐清 |
| netlist ↔ BOM 配對 | ✅ | refdes 命中率 ≥ 90% 且領先次佳 ≥ 30%；否則**停下來問** |
| `bom_scope` | ✅ | 檔名含 `SMT` → `smt_only`，否則 `complete` |
| `mates` | ✅ | 腳數 ≥ 8、直通唯一勝出、零矛盾、語意相符 ≥ 4、margin ≥ 2 |
| `mates`（兩側都有同分候選） | ❌ | **netlist 真的分不出來**，列進 `SETUP.md` 等人決定 |
| `trace.start` | 後援 | 未設時自動用所有對接連接器當起點 |
| `net_normalize` | ❌ | 專案命名習慣，猜不得（見 `pitfalls.md` #8）。init 會**列出兩側命名差異樣本**供你寫規則 |
| `endpoints` / `part_package` / `mate_map` | ❌ | 留空，列進 `SETUP.md` 待補 |

⚠️ **`net_normalize` 對對接判定是決定性的。** 實測同一組 40-pin 連接器：沒有
規則時 16 vs 16（判不出來），有規則時 36 vs 32（定案）。填好後重跑 `mate`。

6. 用 **AskUserQuestion** 問清楚：這些板子怎麼組成一台？有沒有線束？
   **不要自己猜拓樸。**

---

## 2. 補 datasheet

```bash
python scripts/ndd.py datasheets              # 盤點 + 自動下載 + 產出 MISSING.md
python scripts/ndd.py datasheets --pn <料號> --url <你查到的網址>
```

自動下載只對少數原廠站有效。流程：自動盤點 → 對 `MISSING.md` 裡的料號用
**WebSearch** 找官方網址 → `--url` 抓下來 → 自製件／連接器抓不到是正常的。

---

## 3. 建元件模型（選用，非前提）

**不要靠記憶判斷哪顆值得建模——用數的：**

```bash
python ndd.py blockers      # 訊號鏈停在哪些料號上、各擋住幾條
```

輸出的「其他訊號腳」= 該顆除了訊號停住的那支腳外，還有幾支接在**非電源**網路
上。數字大代表訊號很可能還會繼續走——這是**只用 netlist** 就能算的穿越件跡象。

**只是終端負載的，填 `ndd.json` 的 `endpoints` 就好**，不需要建模也不需要
datasheet。

```bash
python ndd.py models --examples        # 看有哪些範例
python ndd.py models --add PCA9547     # 複製進專案的 models.json
```

範例**不會自動載入，要手動複製**——模型是「某人對 datasheet 的解讀」，複製這個
動作讓它變成**你的宣告**，`audit` / `REVIEW.md` 才會把它列進你要複核的清單。
範例的 `pin_roles` 多半留空，那要翻 datasheet 才能填，**不要憑印象**。

schema、`direction` 沒有預設值、`gate` 與 `parameter_control` 的分界、`always`
必須由 netlist 推導 —— 全部見 `references/models.md`。

---

## 4. 寫架構文件

**`init` 已經產生 `<板>_Architecture.md` 的第 0 版**——那份只含不需要規格書就
能斷言的事實（重複結構、主要零件、對外介面、電源、訊號家族、未貼件），並在
§8 列出它還不知道什麼。**這一階段是把 §8 一條條消掉**，不是從零開始寫。

**md 解釋「為什麼」，CSV 回答「是什麼」，稽核確認兩者一致。** 不要把逐腳資料
抄進 md。第 0 版刻意不寫任何 `[D]` 級主張（那時 datasheet 還沒到齊），
**加深的內容要自己帶出處**。

文件開頭必備：

```bash
python scripts/ndd.py manifest    # 產生 MANIFEST.md
```

- 引用 manifest（含 `.asc` / BOM / datasheet / **`ndd.json`** 的完整 SHA-256
  與工具版本）而不是手打版本號——`ndd.json` 裡的 `mate_map`、`part_package`、
  `net_normalize` 每一項都會改變結論
- 一句「逐腳查詢請用 CSV / `ndd.py`，不要靠本文」
- 書寫慣例：**每個 refdes 後面一律附料號**

每寫一條可機械驗證的主張，就在 `ndd.json` 的 `assertions` 補一條。
寫完跑 `audit`，**首次執行的 FAIL 就是文件的錯**，改文件而不是改斷言。

---

## 5. 交付複驗

```bash
python scripts/ndd.py review      # 產出 REVIEW.md（含 coverage 指引）
```

**這一步不可省略。** 交付時要明確告訴使用者：

> 工具驗得到的部分已驗過並列在 A 段；**B 段每一項都需要你人工確認**。
> 排名無法定案的對接（`mate:ambiguous`）是候選不是結論；`unclassified` 端點
> 是**還沒分類**，不是「已確認為負載」。

---

## 產出物政策（完整對照）

| 東西 | 定位 | 何時產生 |
|---|---|---|
| **回答本身** | **主要交付物** | 每次 |
| `<板>_Architecture.md` | **板卡導覽**——`init` 寫第 0 版，之後由人加深 | `init` 自動產生 |
| 其他 md 文件 | 只有使用者明確要求時 | 明確要求 |
| pinmap / signal_chain CSV | **可重生的衍生物** | 需要時重跑，過期就丟 |
| `topology_hint.csv` | 功能說明，**不是連通** | 隨 trace 產生 |
| `verified-pins.csv` | **datasheet 原文快取** + symbol 腳位名 | `pinfn` 自動累積；symbol 列由 `init` 整批重建 |
| `hier/*_parts.csv` / `hier/*_nodes.csv` | **可重生的衍生物**（`.DSN` 轉出） | `init` 產生；`.DSN` 更新就重跑 |
| `models.json` | **選用加速器**，不是前提 | 同一顆 IC 追第 2 次以上才值得建 |
| `export/cis_parts.csv` | **選用加速器**（料件分類快照），不是前提 | 使用者自行從 CIS 唯讀匯出；入 `MANIFEST.md` |
| `MANIFEST.md` | 輸入檔指紋 | 寫文件時 |

**原始來源是資產，結論是拋棄式的。**
