---
name: netlist-deep-dive
description: 由 OrCAD Capture 設計檔 (.DSN)、PADS 2000 ASCII netlist (.asc) 與 PCBA BOM (.xlsx) 做深度電路架構分析，產出可查詢的 pinmap CSV、跨板端到端訊號鏈、階層與腳位功能名、功能拓樸提示、架構文件與人工複驗清單，並附自動稽核。當使用者提供 .DSN/.asc/BOM 要求分析電路架構、追訊號、找未貼件、盤點 IC、建立板級文件，或要求驗證既有架構文件是否仍與 netlist 相符時使用。跨專案通用。
---

# 電路深度分析（netlist deep dive）

輸入是每塊板三份：**`.DSN`（OrCAD Capture 設計檔）+ `.asc` netlist + BOM**。
產出一整套：可查詢的 CSV、端到端訊號鏈、階層與腳位功能名、功能拓樸提示、
架構文件、稽核報告、人工複驗清單。

**三份各自不可取代，缺一不可：**

| 檔案 | 只有它給得出的東西 |
|---|---|
| `.asc` | **接線**。經年累月驗證過的權威，每塊板都適用，格式從不出錯 |
| BOM | **身分**（料號、值）與**有沒有貼件** |
| `.DSN` | **階層**（零件在哪個子電路）與**腳位功能名**（`SENSE3+`、低有效標記） |

`.DSN` 由 `init` 自動透過 Capture 自己的 TCL API **唯讀**轉成兩份 CSV，
轉完**逐條對帳 `.asc`**：不一致就停下來，不會默默採用。詳見 `pitfalls.md` #13-#16。

⚠️ **這台機器要裝 OrCAD Capture**（`init` 自動偵測 `C:\Cadence\SPB_*`），
沒有就無法處理 `.DSN`，`init` 會失敗而不是降級。**工具絕不寫入 Cadence 安裝目錄。**

## 開始前先讀

| 檔案 | 什麼時候讀 |
|---|---|
| `references/pitfalls.md` | **每次都讀。** 每一條都會產生「看起來合理但是錯的」結論 |
| `references/models.md` | **要建模型或追訊號前一定要讀。** transfer/control/endpoint 的分界 |
| `references/verification.md` | **Phase 4 一定要讀。** 分層驗證，以及哪些東西**結構上驗不到** |
| `references/datasheets.md` | 需要 datasheet 時讀 |
| `references/part_classification.md` | 看 `coverage` 的分類、或要補 `part_class` 時讀 |

工具在 `scripts/`，進入點是 `ndd.py`。回歸測試：
`python -m unittest discover -s tests`，改動工具後一定要跑。

Windows 上兩件事：**路徑用 `C:/...`**（Git Bash 的 `/c/...` Python 讀不到）；
**檔名含 CJK 相容表意字時 Bash 處理不了**，改用 PowerShell 或 Glob/Read。

---

## 三源對照規約（最高原則，回答任何電路問題都適用）

固定五條，不會增長。**這是回答時執行的紀律，不是要產生的檔案。**

### 每個主張只能有一種合法來源

| 主張類型 | 唯一合法來源 | 標記 |
|---|---|---|
| 連線——誰接到誰 | netlist（`.asc`） | `[N]` |
| 身分——料號、值、**BOM 範圍內有無列出** | BOM | `[B]` |
| 腳位功能、方向、內部行為、極性 | datasheet | `[D]` |
| **腳位的功能名**（只有名字）、**低有效**、**零件在哪個子電路** | `.DSN` 的 Capture symbol 與階層 | `[S]` |
| 推論 | 上述組合 + 寫出推理過程 | `[?]` |

### 兩種標記，兩種待遇——**找到的證據放結尾，沒把握的話留在原地**

| | `[N]` `[B]` `[D]` `[S]`（找到的證據） | `[?]`、`[D 缺]`（沒把握／缺資料） |
|---|---|---|
| 位置 | **敘述與表格裡一律不帶方括號**，集中寫進結尾證據區塊 | **留在原句**，裸標記就好 |
| 為什麼 | 每句話、每個表格儲存格都插方括號，讀的人要一直在「內容」和「標記語法」之間切換視線；證據該給的資訊量不會因為集中寫而變少 | 這是在標記**這一句話本身**的把握程度，離開原句就沒有意義 |

**敘述與表格保持乾淨**——像下面這樣寫，不要在儲存格裡塞 `[N]`：

> 發射致能由 `U913` 的 P0 腳發出，經匯流排開關 `U937` 後分成兩路，一路到
> `U105`（LDO）的致能腳。

`[?]` 照樣留在原地，因為它標的是**這一句是不是猜的**：

> 這條路能不能通，取決於 `U937` 的選擇腳邏輯 `[?]`。

⚠️ **`[?]` 只要標，不必在原句展開推理過程**——完整的主張與定案方式移到結尾
證據區塊的「推論邊界」那一類，原句只留一個乾淨的 `[?]`。

### 結尾證據區塊：按類型分組，每行自帶完整受詞

回答結尾附一段證據，**按類型分組**，不逐句編號、不做表格：

```
連通性：U913.2 = TX_EN_P_0（fecu_fm，netlist 上這條線只接兩支腳）；
        trace --board fecu_fm --from U913.2 追出兩條分支
身分：  U913 = PCA9554BBSHP、U937 = SN74CBTLV3126DGVR（fecu_fm BOM）
腳位功能：U937 選擇腳綁 VDD_3V3_P（netlist 事實）；導通/斷開的極性未查
推論邊界：選擇腳綁電源是否代表常通 —— 定案方式：補 SN74CBTLV3126 規格書
```

**每一行自己就要完整**，不靠回原句對照——板名、refdes、pin、net 都寫在這
一行裡，讀的人不必跳回敘述找受詞。⚠️ **`[N]`／`[B]` 板名必填**：同一顆
`U28` 在 DPU 板是電壓監視器，在 Interposer 是光耦，少了板名會**把人帶去
錯的零件**，不只是驗不了。

分類用「連通性／身分／腳位功能／推論邊界」這種白話中文，**不要用英文詞**
（`Connectivity`／`Part identity` 之類）——跟敘述不夾外來術語是同一條原則。

### 一次查詢 = 一行證據（**不是**一顆 refdes = 一行證據）

證據行的數量跟「查了幾個獨立來源」成正比，**不跟講了幾顆零件成正比**。一條
20 跳的鏈，「連通性」那行只需要寫那一行 trace 指令，不必列 20 個 refdes——
使用者跑一次就拿到完整原文，而且 netlist 改版後貼死的清單會過期，指令不會。

### `[S]`（symbol）與 `[D]`（datasheet）的分界——**不可混用**

symbol 的腳位名是照 datasheet 建的，所以**名字**可信，可直接引用。但 symbol
**沒有原文、沒有頁碼**，它回答不了「這支腳是做什麼的、方向是什麼、內部怎麼
接」。

| 要主張的事 | 可以用 `[S]` 嗎 |
|---|---|
| 「U1036 的第 8 腳叫 `SENSE3+`」 | ✅ |
| 「`PWRDN` 這支腳是低有效」（symbol 上有上劃線） | ✅ |
| 「U1036 在 `Coupler_Path_R` 這個子電路裡」 | ✅ |
| 「`SENSE3+` 是電流偵測輸入，量測範圍 ±80mV」 | ❌ 要 `[D 檔名 p.x]` |
| 「這支腳拉低會關斷輸出」 | ❌ 要 `[D]` |
| 「訊號會從 pin 2 穿到 pin 18」 | ❌ 要 `[D]`，見硬規則 1 |

判準：**名字是 `[S]`，行為是 `[D]`。** 名字暗示的行為仍然是行為——`EN` 這個
名字不構成「拉高致能」的證據。

`init` 會把 symbol 腳位名寫進 `verified-pins.csv`（標 `capture_symbol`）。
之後 `pinfn` 從 datasheet 抽到同一支腳時會**自動對名**：對得上就互相佐證，
**對不上就當場攤開**——最常見的成因是封裝選錯。

### 五條硬規則

1. **「訊號穿過」與「功能影響」是兩件事，分開講。**

   | 類別 | 例子 | 可進正式 trace | 可寫進拓樸說明 |
   |---|---|---|---|
   | `signal_transfer` | buffer、switch、mux、fanout、RF amp／移相器 | **可以**，但要 `[D]` 驗證 | 可以 |
   | `control_influence` | OE、RESET、LOAD、SEL、暫存器設定 | **不可以** | 可以 |
   | `stateful` | 移位暫存器、FPGA、MCU、ADC、DAC | **不可以** | 可以 |
   | `terminal` | sensor input、天線端、量測腳、不再輸出的負載 | **不可以** | 可以 |
   | `unknown_stop` | 無 datasheet／無法分類 | **不可以** | 只列已知事實，不推論 |

   **分類的單位是「邊」，不是「元件」**——同一顆 IC 可以同時有 transfer 邊、
   control 腳與狀態行為。「能控制它」不等於「訊號穿過它」：`LOAD -> Q outputs`
   是物理上不存在的鏈路，正確的敘述是「此訊號同步觸發 N 個暫存器更新」。

   只有要走 `signal_transfer` 邊時才硬性需要 `[D]`。連接器、被動網路的拓樸
   可以標 `[N]` 並寫出推理，**不要停在 `[D 缺]`**。

   ⚠️ 判定「缺 datasheet」之前要用**全文**複核，不能只比檔名——family datasheet
   必然漏判。誤報「缺」會害人重複採購已持有的規格書。

2. **BOM 是這塊板的權威。** netlist 有、BOM 無 = **未貼件 (DNI)**。
   工具不做變體推理，也不質疑使用者提供的 BOM。

   唯一的例外是**文件涵蓋範圍**（不是正確性）：`bom_scope: smt_only` 的
   BOM 依定義不列連接器、測試點、鎖孔、手插件，那一類的缺席不可判定。

3. **`always` 要付證明，`conditional` 是預設。** 說某條路徑恆通，必須拿出
   netlist 上 enable 腳實際接法的證據。runtime 選通、外部驅動、懸空、未知
   一律降為 `conditional` / `unknown`。位址腳固定**不算**證明。

4. **衝突時**：連線→`.asc`、料號→BOM、腳位功能→datasheet、階層→`.DSN`；
   **且衝突本身要講出來**，不能默默選一個。

   ⚠️ **階層永遠不是連通的來源。** `.DSN` 告訴你零件在哪、腳叫什麼名字，
   接線一律以 `.asc` 為準（`init` 已逐條對帳過兩者）。

5. **不可得就說不可得。** 缺 datasheet → 標 `[D 缺]`；封裝無法定案 → 標
   `unknown_stop(package_unresolved)`。追跡遇到查不到的 IC，**回報「停在
   U6 (<料號>) pin15」而不是繼續猜**——停在具名位置是有用的答案，猜出來的
   完整鏈路是有害的答案。

### 執行機制：可見性

每個電路回答結尾都要有上面那段分類證據，**沒有出現在證據區塊的主張，依定義
就是未查證的**。「推論邊界」那一類也要主動寫「這次我沒查什麼」，不要只寫查
到的部分。

⚠️ **這套格式只約束新寫的內容，既有文件不回填。** 實測既有的四份架構文件共
820 條可機械查證的引用、事實錯誤 0——回填幾百行買到的是零。

使用者懷疑我漏講了什麼，解法是**繞過我的取捨**——自己跑
`ndd.py --board <板> pins <refdes>` 把整顆零件的腳位攤開看。

### ⚠️ 用工程語言回答，不要貼工具欄位名

使用者要的是電路結論，**不是腳本的內部狀態**。敘述保持乾淨（見上），但**工具
的欄位名一律翻譯成一句話**：

| 工具輸出 | 回答裡要寫成 |
|---|---|
| `gating: always` | **不寫**——這是預設，沒有條件才是常態 |
| `gating: conditional` | 「這條路要**軟體選通**才通」（並說是哪顆、什麼機制） |
| `gating: unknown` | 「致能腳懸空／由誰驅動不明，**通不通我沒把握**」 |
| `endpoint_kind: unclassified` | 「訊號停在這裡，但這幾顆**還沒分類**——可能是終點，也可能是還沒建模的穿越件」。**這是覆蓋率缺口，不是路徑有疑問**，別講成路徑不可信 |
| `n_loads` / `loads` | 「訊號**抵達**的腳位」，**不是**「已確認的負載」——`unclassified` 也算在裡面。別講成「N 個負載」 |
| `driver(model)` | 「這是訊號**來源**，不是負載」 |
| `unknown_stop(*)` | 「**停在這裡，不再往下猜**」（講出停在哪顆的哪支腳，那是有用的答案） |
| `boundary(not_followed)` | 「到這裡就是**跨板對接**，板內追蹤預設不走過去」（並講出對面是哪支腳） |
| `mate:ambiguous` | 「這個接頭的腳位對應，我從 netlist **判不出來**」 |
| `mate:inferred` / `package:inferred` | 「這是我**推的**，依據是⋯」（寫出依據，別寫狀態名） |
| `confidence: *` | **不寫欄位名**——改成一句話講最弱的那個環節 |
| `bom-scope-insufficient` | 「這份 BOM 不收這類零件，所以**看不出**有沒有貼」 |

判準：**回答裡不該出現底線、冒號組成的識別碼。** 若出現，代表我在轉貼工具輸出
而不是在回答問題。

### 其他仍然適用的原則

- **通則一定要展開逐顆比對**（`role_rules`），通則幾乎一定有例外。
- **跨板對接要枚舉排名**，不能只看「兩側相符」。
- **netlist 描述設計，不描述手上那片板。** rework、飛線、換料都不在裡面。

### 產出物政策（避免累積會腐化的東西）

| 東西 | 定位 | 何時產生 |
|---|---|---|
| **回答本身** | **主要交付物** | 每次 |
| md 文件 | 只有使用者明確要求時 | 明確要求 |
| pinmap / signal_chain CSV | **可重生的衍生物** | 需要時重跑，過期就丟 |
| `topology_hint.csv` | 功能說明，**不是連通** | 隨 trace 產生 |
| `verified-pins.csv` | **datasheet 原文快取** + symbol 腳位名 | `pinfn` 自動累積；symbol 列由 `init` 整批重建 |
| `hier/*_parts.csv` / `hier/*_nodes.csv` | **可重生的衍生物**（`.DSN` 轉出） | `init` 產生；`.DSN` 更新就重跑 |
| `models.json` | **選用加速器**，不是前提 | 同一顆 IC 追第 2 次以上才值得建 |
| `export/cis_parts.csv` | **選用加速器**（料件分類快照），不是前提 | 使用者自行從 CIS 唯讀匯出；入 `MANIFEST.md` |
| `MANIFEST.md` | 輸入檔指紋 | 寫文件時 |

**原始來源是資產，結論是拋棄式的。**

---

## Phase 0 — 收檔案、建專案（`init` 一次跑完）

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

## Phase 1 — 盤點與初步理解

```bash
python scripts/ndd.py export      # pinmap_<board>.csv：逐腳事實表
python scripts/ndd.py audit       # 先跑一次
python scripts/ndd.py coverage    # per-MPN 三源覆蓋，看缺口在哪
python scripts/ndd.py part <關鍵字>
```

### 板內追蹤（最常用的查詢）

```bash
python ndd.py trace --board <板> --from U939          # 該顆所有非電源腳
python ndd.py trace --board <板> --from U939.15       # 只追這一支
python ndd.py trace --board <板> --from net:PLL_REF   # 從一條 net 出發
python ndd.py trace --board <板> --from J4 --follow-mates   # 允許跨板
```

**不需要 `mates` / `slot_pattern` 設好就能用**——這是它跟批次 `trace` 的差別。
不寫檔，結果直接印出來。

每個落點各印一行：**一條分支停在未建模的元件上，不影響其他分支**。走到跨板
對接時預設停在**本板這一側**並講出對面是哪支腳，不會替你走過去。

⚠️ **落點爆量（幾百上千個）幾乎一定是電源軌沒被判為電源**，於是追跡穿過每顆
2-pin 被動件走遍全板。**地不會有這個問題**——`AGND`／`PGND`／`28V_GND_PM_2`
由 `cls()` 直接認得，不必設定。**軌要你設 `power_net_regex`**：軌的命名是各專案
自己的，而誤判一條軌會讓訊號路徑**無聲消失**，所以工具只把候選連同腳數列出來
給你確認，不替你決定。

⚠️ **`_CS`（電流偵測）、`_FB`（回授）、`_EN`、`_PG` 是訊號，不要收進電源正則。**
`GND_SENSE`、`PGND_FB` 這種 Kelvin 偵測地也是訊號，工具不會自動把它當地。

先看數量結構：某顆料 ×16、×9、×81 這種倍率，通常就是系統架構的直接反映。
**先找出倍率，再解釋它。**

`coverage` 的 `unclassified` 欄是**預設值不是結論**——未宣告的穿越件會落在那裡。

`coverage` 依**零件分類**排序：要查證的（IC、RF、分立半導體、晶振⋯）排前面並
按「接了幾條非電源訊號」排序，機構件／連接器／線材沉底且不排 datasheet 待辦。
分類順位：人工宣告 > CIS 料號查表（**事實**）> footprint 樣式 > refdes 前綴
（後兩者是**推論**，標 `[?]`）> 未辨識（列進「需你確認」，**工具不猜**）。

主力是 **footprint**——它就在 `.asc` 裡，不需要 CIS 也不會過期。實測六塊板：
沒有 CIS 快照時涵蓋 96.9%、未辨識只剩 1.7%。詳見
`references/part_classification.md`。

## Phase 2 — 取得 datasheet

```bash
python scripts/ndd.py datasheets              # 盤點 + 自動下載 + 產出 MISSING.md
python scripts/ndd.py datasheets --pn <料號> --url <你查到的網址>
```

自動下載只對少數原廠站有效。流程：自動盤點 → 對 `MISSING.md` 裡的料號用
**WebSearch** 找官方網址 → `--url` 抓下來 → 自製件／連接器抓不到是正常的。

## Phase 3 — 查證腳位功能

```bash
python ndd.py pinfn <料號> 8
python ndd.py --board <板> pinfn --refdes U939 15   # 由工具鎖定三源
python ndd.py pinfn --list
python ndd.py pinfn --import-symbols               # 重建 symbol 列（init 已跑過）
```

`--refdes` 模式會自己從 BOM 取料號、從 netlist 取已接腳位與 footprint，
**不必靠你記得傳對料號**。

**先查快取，未命中才抽取；抽到的原文自動寫進 `verified-pins.csv`。**

### 快取裡有兩種列，責任不同

| `resolved_by` | 來源 | 有原文？ | 用途 |
|---|---|---|---|
| `not_applicable` / `user_confirmed` | datasheet | ✅ 逐字 + 頁碼 + SHA-256 | 回答「這支腳做什麼」 |
| `capture_symbol` | `.DSN` 的 symbol | ❌ **只有名字** | 回答「這支腳叫什麼」；對 datasheet 抽出來的名字 |

symbol 列**不會**讓 datasheet 抽取被跳過——兩者是互相佐證，不是互相取代。
`--list` 裡 symbol 列的名字前若有 `~`，代表 symbol 上有上劃線＝**低有效**。

⚠️ **名字對不上時不要自己挑一個。** `pinfn` 會印出兩邊的名字並要你確認封裝
（同料號不同封裝腳位不同是最常見的成因）。默默採用 datasheet 那筆，等於把
錯誤封裝的腳位名凍結成資產。

⚠️ **同一料號的兩顆零件 symbol 腳位名不一致時，整筆不寫入。** 那代表其中一顆
用錯 symbol，或 BOM 標錯料號——不可合併，也不可挑一個。

> **快取原文，永不快取解讀。**
> 快取的是逐字內容 + 出處（檔名／頁碼／SHA-256／封裝欄），不是「pin1 與 pin3
> 內部連通」這種推出來的結論。原文快取 5 秒就能核對；推論快取會把錯誤凍結成
> 永久資產。datasheet 換版時 SHA-256 不符會自動失效重抽。

### 封裝判定只用 netlist + BOM

**工具不解析 datasheet 的腳位表**（每家排版不同，一列錯位整張表就作廢）。改用
三個證據排名：模型宣告的電源/接地腳實際接在哪 `[N]`、訂購碼的封裝後綴 `[B]`、
layout 的 footprint 名稱 `[N]`。`audit` 的 [1.5] 段會逐顆列出判定與證據。

**推論出來的一律標 `[?]`**，並帶 `package:inferred` caveat 沿路徑傳到 CSV。
門檻是「零矛盾 + 唯一勝出 + 至少一個獨立來源佐證」，達不到就列入待補，
**不替你假設**。要定案就填 `ndd.json` 的 `part_package`——但即使你明確宣告，
若與 netlist 矛盾仍會 FAIL（**人講的最大，但矛盾要講出來**）。

排名機制與 `pin_roles` 的角色見 `references/models.md`。

### pinfn 抽到多筆時不替你挑

一份 datasheet 涵蓋多種封裝時，`pinfn` 會把每筆的頁碼、腳位名與**腳號欄位**
攤出來，要你 `--pick <n> [--package <標籤>]`，**在那之前不寫快取**。對照 audit
的封裝判定即可選定。**抽到一筆才是已證明無歧義**，才會自動寫入快取。

### 元件模型（選用，非前提）

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

## Phase 4 — 驗證（先讀 `references/verification.md`）

```bash
python scripts/ndd.py mate        # 連接器對接：枚舉所有對應方式並排名
python scripts/ndd.py trace       # 端到端訊號鏈 + topology_hint
python scripts/ndd.py audit       # 完整稽核
```

**`mate` 的判讀**：只有「直通唯一勝出且 margin 夠大」才算站得住。margin ≤ 4
一定要標明證據薄弱。工具也會判斷是「兩側同型 → 中間有線束」還是「公母直接
對接 → 只剩 footprint 方位」，兩者定案途徑完全不同。

**連接器只負責「訊號有沒有連到」** —— netlist 連得上就是事實，不需要 datasheet。
所以排名會**自己定案**：

| 狀態 | 條件 | 下游 |
|---|---|---|
| `approved` | `ndd.json` 的 `mate_map` 明確批准 | 無 caveat |
| `inferred` | 直通唯一勝出 + 零矛盾 + margin ≥ 2 | 無 caveat，標 `[?]` |
| `ambiguous` | 排名決定不了 | `mate:ambiguous`，confidence 降為 unknown |

只有排名真的分不出來時才需要 layout／線束圖／實測。

**殘存候選要用「會不會壞」排除**：代入後看它會不會造成立即而明顯的故障
（SDA/SCL 對調 → I2C 全滅）。系統若實際會動，該候選就被排除了。這通常比找
線束圖快，而且是**唯一能同時涵蓋 layout 正確性的證據**。

⚠️ **追跡停在沒建模的零件上是正確行為，不是缺陷。** 訊號能不能穿過一顆開關
取決於致能腳，那不是 netlist 回答得了的事——停在具名位置並講出停在哪，就是
正確答案。有 transfer 模型時它會穿過去，但會標成 `conditional` 並寫出致能腳
被誰驅動（例如 `[1=driven_by:DIO_SEL_0]`）；致能腳實際綁死在電源軌上才會標
`always`。兩者都沒有謊報連通。

`endpoints` 是**選用**的加註，不必逐一填——只有想知道「訊號到這裡之後還會不會
繼續走」的那幾顆才值得宣告，用 `blockers` 排優先序。

**`trace` 產出兩份，不可互換**：

- `signal_chain.csv` — 只含已驗證的 `signal_transfer` 邊，可當架構結論
- `topology_hint.csv` — control／stateful／參數控制／未知邊界，是**功能說明
  不是連通**，永不得當成下一跳

## Phase 5 — 寫文件

**md 解釋「為什麼」，CSV 回答「是什麼」，稽核確認兩者一致。** 不要把逐腳資料
抄進 md。

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

## Phase 6 — 人工複驗清單

```bash
python scripts/ndd.py review      # 產出 REVIEW.md（含 coverage 指引）
```

**這一步不可省略。** 交付時要明確告訴使用者：

> 工具驗得到的部分已驗過並列在 A 段；**B 段每一項都需要你人工確認**。
> 排名無法定案的對接（`mate:ambiguous`）是候選不是結論；`unclassified` 端點
> 是**還沒分類**，不是「已確認為負載」。
