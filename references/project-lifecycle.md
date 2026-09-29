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
   就降級跑。三份要是**同一版設計**：netlist 匯出之後線路圖又改過，兩份各自正確
   卻對不上，`init` 會在對帳時停下來。

   `.DSN` 若有子設計目錄，一併放進來。**設計不能開在 Capture 裡**（旁邊會有
   `.DSNlck`），否則轉換會無限等待；工具會先擋下來並要求你關閉。

2. **先看 init 打算怎麼做**（只讀，不寫任何檔案）：

```bash
python scripts/ndd.py init "C:/path/to/analysis" --plan
```

會印出三件事：netlist ↔ BOM 配對（**用 refdes 交集，不用檔名猜**）、連接器對接
候選與判定依據、datasheet 盤點。

3. **只有兩件事要問使用者**，用 `AskUserQuestion` 一次問完：

   - **BOM 配對** —— 只在 `--plan` 標「需你確認」時問，附候選清單
   - **datasheet** —— 下載還是跳過（跳過仍會產生缺件清單）

4. **一路跑完，中途不再停**：

```bash
python scripts/ndd.py init "C:/path/to/analysis" --run     [--bom <key>=<檔名>]... [--accept-pairing] [--accept-mates] [--no-datasheets]
```

`init --run`／`migrate --run` **預設不下載規格書**（要下載加 `--datasheets`），不下載時照樣產生 `datasheets/INDEX.md`。

先把每塊板的 `.DSN` 轉成階層 CSV 並對帳 `.asc`，再依序執行 `pinfn --import-symbols`
→ `export` → `datasheets` → `audit` → `mate` → `trace` → `coverage`
→ `manifest` → `review` → `facts`，**任一步失敗不中止**，結果寫進 `SETUP.md`。

⚠️ **階層那一步是唯一會讓 `init` 直接中止的。** 它排在所有流程之前，因為
`.DSN` 與 `.asc` 對不起來就代表兩份檔案不是同一塊板／同一版，**後面每一個
結論都會建立在錯的基礎上而不會有任何症狀**。看到它停下來，先釐清檔案版本，
不要想辦法繞過。

對帳結果裡的「PADS 改名 N 條」是正常的：`.asc` 不收 `*`、`/` 這類字元，
formatter 會把整條 net 改名成 `X#####`。節點集合完全相同就是改名不是接錯，工具會列出對照表——
**`.DSN` 那邊才有設計者取的原名**，回答時用原名比 `X00697` 有意義得多。

產出：`ndd.json`、`SETUP.md`、`MANIFEST.md`、`REVIEW.md`、`<板>_Facts.md`、`arch_pack/<板>/`、
`datasheets/INDEX.md`、`export/*.csv`、`hier/*.csv`、`verified-pins.csv`。

5. **跑完後我接手，逐板寫 `<板>_Architecture.md`**（見 §4）——給人讀的導覽是
   分析結論，腳本產不出來。

### init 自動決定與不決定的

| 項目 | 自動 | 條件 |
|---|---|---|
| `.DSN` ↔ `.asc` 配對 | ✅ | 用 refdes 交集（≥ 90%），不用檔名猜；配不上就**中止** |
| `.DSN` → 階層 CSV | ✅ | 自動找 `SPB_*`（取版本最高）；找不到 Capture 就**中止** |
| 階層與 `.asc` 對帳 | ✅ | 逐條比 net／節點／零件；**任何不一致都中止**，不是警告 |
| symbol 腳位名入庫 | ✅ | 同料號腳位名不一致時**不寫入**，列出來等人釐清 |
| netlist ↔ BOM 配對 | ✅ | refdes 命中率 ≥ 90% 且領先次佳 ≥ 30%；否則**停下來問** |
| `bom_scope` | ✅ | 檔名含 `SMT` → `smt_only`，否則 `complete` |
| `mates` | ✅ | 腳數 ≥ 8；公母直接對接：實體大小相同、直通語意相符 ≥ 4 且多於矛盾；線束：訊號腳全部靠名稱唯一對上且 ≥ 4 支 |
| `mates`（兩側都有同分候選） | ❌ | **netlist 真的分不出來**，列進 `SETUP.md` 等人決定 |
| `trace.start` | 後援 | 未設時自動用所有對接連接器當起點 |
| `net_normalize` | ❌ | 專案命名習慣，猜不得（削掉有意義的數字會把 `CLK_1`／`CLK_2` 併成同一條）。init 會**列出兩側命名差異樣本**供你寫規則 |
| `endpoints` / `part_package` / `mate_map` | ❌ | 留空，列進 `SETUP.md` 待補 |

⚠️ **`net_normalize` 對對接判定是決定性的。** 實測同一組 40-pin 連接器：沒有
規則時 16 vs 16（判不出來），有規則時 36 vs 32（定案）。填好後重跑 `mate`。

6. 用 **AskUserQuestion** 問清楚：這些板子怎麼組成一台？有沒有線束？
   **不要自己猜拓樸。**

---

## 2. 補 datasheet

```bash
python scripts/ndd.py datasheets              # 盤點 + 自動下載 + 產出 INDEX.md 對照表
python scripts/ndd.py datasheets --pn <料號> --url <你查到的網址>
```

自動下載只對少數原廠站有效。流程：自動盤點 → 對 `INDEX.md` 標「缺」的料號用
**WebSearch** 找官方網址 → `--url` 抓下來 → 自製件抓不到是正常的。

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

## 4. 寫架構文件（`<板>_Architecture.md`）

**角度與寫法全部在 `references/architecture-brief.md`**——交接視角、方塊圖為核心、
不用規格書、正文不加 `[ ]` 標記。這一節只講**怎麼派工**。

**材料是 `arch_pack/<板>/`**（`init`／`ndd.py facts` 產生）：`00_skeleton.md`
（組成、子電路之間的連線、介面、電源軌摘要、未貼件）、`00_power.md`（電源軌逐條），
加上每個分塊一份（該塊主要零件接到誰——已依對象收斂、不逐腳——多點網路、
階層 port；超過 40 KB 切成 `_1`／`_2`）。`index.md` 列各檔大小。
主／備與 ×N 的每一份都照列，不合併。

**一行指令跑完三階段，不要派 subagent：**

```bash
python scripts/ndd.py --board <板> arch            # A → B（平行）→ C
python scripts/ndd.py --board <板> arch --stage C  # 只重跑某一階段
```

每次呼叫是**無工具、單回合**的 `claude -p`：brief 當系統提示，材料直接附在訊息裡，
模型直接輸出檔案內容。

| 階段 | 幾次呼叫 | 附上的材料 | 寫 |
|---|---|---|---|
| A 方塊圖 | 1 | `index.md` + `00_skeleton.md` | `arch_pack/<板>/work/A_outline.md` |
| B 分塊 | A 大綱 ```` ```groups ```` 區塊的組數，平行 | `A_outline.md` + 該組分塊檔 | `work/B_<組號>.md` |
| C 組裝 | 1 | `A_outline.md` + 全部 `B_*.md` | `<板>_Architecture.md` |

- **為什麼不用 subagent**：實測 subagent 每次請求固定開銷約 5 萬 token，讀檔、
  寫檔、回報又各佔一回合、每回合整份重送；b0017 六個 subagent 合計處理量遠超過
  材料本身。`claude -p` 無工具的固定開銷不到 1 千，一次請求就是材料送一次＋輸出一次。
- 每階段的 token 寫進 `work/usage.json`，跑完印合計——交付時回報。
- A 的分組區塊漏分、重複或檔名打錯，B 會**停下來**不替它補——重跑 `--stage A`。
- 我自己**不讀**分塊檔與 B 的產出，讀了就把整份材料搬進主 session。

C 完成後我補兩件事：

- 跑 `python scripts/ndd.py manifest`，在文件開頭引用 `MANIFEST.md`（輸入檔與
  `ndd.json` 的 SHA-256），不手打版本號
- 選用：文件裡可機械驗證的主張（「U20 接到 J3 共 55 條」）補進 `ndd.json` 的
  `assertions`，跑 `audit`；**首次 FAIL 就是文件的錯**，改文件不改斷言

---

## 5. 交付複驗

```bash
python scripts/ndd.py review      # 產出 REVIEW.md（含 coverage 指引）
```

**這一步不可省略。** 交付時要明確告訴使用者：

> 工具驗得到的部分已驗過並列在 A 段；**B 段每一項都需要你人工確認**。
> 工具定不了的對接（`mate:ambiguous`）是佔位不是結論；`unclassified` 端點
> 是**還沒分類**，不是「已確認為負載」。

---

## 產出物政策（完整對照）

| 東西 | 定位 | 何時產生 |
|---|---|---|
| **回答本身** | **主要交付物** | 每次 |
| `<板>_Facts.md` | **板卡事實表**——人查閱用，只含 `[N]`/`[B]`/`[S]` | `init` 自動產生；`ndd.py facts` 重跑 |
| `arch_pack/<板>/` | **撰寫材料**——Facts 切成骨架與分塊；`work/` 是各階段中間產物 | 隨 `facts` 產生；可重生 |
| `<板>_Architecture.md` | **板卡導覽**——以方塊圖為核心的交接文件 | `init` 後跑 `ndd.py arch`（§4） |
| 其他 md 文件 | 只有使用者明確要求時 | 明確要求 |
| pinmap / signal_chain CSV | **可重生的衍生物** | 需要時重跑，過期就丟 |
| `topology_hint.csv` | 功能說明，**不是連通** | 隨 trace 產生 |
| `verified-pins.csv` | **datasheet 原文快取** + symbol 腳位名 | `pinfn` 自動累積；symbol 列由 `init` 整批重建 |
| `hier/*_parts.csv` / `hier/*_nodes.csv` | **可重生的衍生物**（`.DSN` 轉出） | `init` 產生；`.DSN` 更新就重跑 |
| `models.json` | **選用加速器**，不是前提 | 同一顆 IC 追第 2 次以上才值得建 |
| `export/cis_parts.csv` | **選用加速器**（料件分類快照），不是前提 | 使用者自行從 CIS 唯讀匯出（方式見 `docs/設計說明.md`）；入 `MANIFEST.md` |
| `MANIFEST.md` | 輸入檔指紋 | 寫文件時 |

**原始來源是資產，結論是拋棄式的。**
