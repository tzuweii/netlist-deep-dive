# Phase 0 — 收檔案、建專案（`init` 一次跑完）

> **什麼時候讀這份**：使用者給了新的一組 `.DSN` + `.asc` + BOM，而且資料夾裡
> **還沒有** `ndd.json`。已經有 `ndd.json` 就是建過了，不要跑 `init`（工具會擋），
> 要升級照 `UPGRADING.md`。
>
> 這一段從 `SKILL.md` 搬出來，因為它**一個專案只會用一次**，而 `SKILL.md`
> 每次叫用都整份進 context。平常回答電路問題用不到這裡的任何一條。

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
