---
name: netlist-deep-dive
description: 由 PADS 2000 ASCII netlist (.asc) 與 PCBA BOM (.xlsx) 做深度電路架構分析，產出可查詢的 pinmap CSV、跨板端到端訊號鏈、功能拓樸提示、架構文件與人工複驗清單，並附自動稽核。當使用者提供 .asc/BOM 要求分析電路架構、追訊號、找未貼件、盤點 IC、建立板級文件，或要求驗證既有架構文件是否仍與 netlist 相符時使用。跨專案通用。
---

# 電路深度分析（netlist deep dive）

輸入只需要 **`.asc` netlist + BOM**。產出一整套：可查詢的 CSV、端到端訊號鏈、
功能拓樸提示、架構文件、稽核報告、人工複驗清單。

## 開始前先讀

| 檔案 | 什麼時候讀 |
|---|---|
| `references/pitfalls.md` | **每次都讀。** 每一條都會產生「看起來合理但是錯的」結論 |
| `references/models.md` | **要建模型或追訊號前一定要讀。** transfer/control/endpoint 的分界 |
| `references/verification.md` | **Phase 4 一定要讀。** 三層驗證，以及哪些東西**結構上驗不到** |
| `references/datasheets.md` | 需要 datasheet 時讀 |

工具在 `scripts/`，進入點是 `ndd.py`。**所有指令都要加 `PYTHONIOENCODING=utf-8`**
（cp950 終端機會把中文輸出變亂碼）。

回歸測試：`python -m unittest discover -s tests`。改動工具後一定要跑。

---

## 三源對照規約（最高原則，回答任何電路問題都適用）

固定五條，不會增長。**這是回答時執行的紀律，不是要產生的檔案。**

### 每個主張只能有一種合法來源

| 主張類型 | 唯一合法來源 | 標記 |
|---|---|---|
| 連線——誰接到誰 | netlist | `[N]` |
| 身分——料號、值、**BOM 範圍內有無列出** | BOM | `[B]` |
| 腳位功能、方向、內部行為、極性 | datasheet | `[D 檔名 p.x]` |
| 推論 | 上述組合 + 寫出推理過程 | `[?]` |

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

4. **衝突時**：連線→netlist、料號→BOM、腳位功能→datasheet；**且衝突本身要
   講出來**，不能默默選一個。

5. **不可得就說不可得。** 缺 datasheet → 標 `[D 缺]`；封裝無法定案 → 標
   `unknown_stop(package_unresolved)`。追跡遇到查不到的 IC，**回報「停在
   U6 (<料號>) pin15」而不是繼續猜**——停在具名位置是有用的答案，猜出來的
   完整鏈路是有害的答案。

### 執行機制：可見性

每個電路回答**附一張壓縮證據表**（主張 → 來源），並主動寫出「這次我沒查什麼」。
**沒有標記的主張，依定義就是未查證的**。

工具產出的兩個獨立欄位要照抄，不要自己合併：

| 欄位 | 值 | 回答 |
|---|---|---|
| `confidence` | `confirmed > caveated > unknown` | 證據有多強 |
| `gating` | `always` / `conditional` / `unknown` | 已查證的通斷性質 |

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
| `verified-pins.csv` | **datasheet 原文快取** | `pinfn` 自動累積 |
| `models.json` | **選用加速器**，不是前提 | 同一顆 IC 追第 2 次以上才值得建 |
| `MANIFEST.md` | 輸入檔指紋 | 寫文件時 |

**原始來源是資產，結論是拋棄式的。**

---

## Phase 0 — 收檔案、建專案

1. 每塊板一份 `.asc` + 一份 BOM。缺 BOM 就先問。

```bash
PYTHONIOENCODING=utf-8 python scripts/ndd.py init "C:/path/to/analysis"
```

`init` 用 **refdes 交集**配對 netlist 與 BOM（不是用檔名猜）。命中率 < 90% 或
與次佳差距 < 30% 會標 `!! 需人工確認`。

2. 人工補完 `ndd.json`（`init` 會寫出所有欄位的空殼）：
   - `boards[*].bom_scope` — 預設 `complete`（BOM 即權威）。只有拿到
     **SMT BOM** 時要改成 `smt_only`，那會讓連接器/測試點/手插件的缺席
     標為不可判定，而非未貼件。
   - `mates` — 連接器對接關係
   - `mate_map` — **已批准**的腳位對映（可以先留空，見 Phase 4）
   - `endpoints` — refdes 或料號 → `terminal` / `stateful` / `unknown_stop`
   - `part_package` — **先留空**，只在工具要求時才填（見 Phase 3）
   - `net_normalize`、`trace.start`、`trace.slot_pattern`

3. 用 **AskUserQuestion** 問清楚：這些板子怎麼組成一台？哪些連接器對接？
   有沒有線束？**不要自己猜拓樸。**

## Phase 1 — 盤點與初步理解

```bash
python scripts/ndd.py export      # pinmap_<board>.csv：逐腳事實表
python scripts/ndd.py audit       # 先跑一次
python scripts/ndd.py coverage    # per-MPN 三源覆蓋，看缺口在哪
python scripts/ndd.py part <關鍵字>
```

先看數量結構：某顆料 ×16、×9、×81 這種倍率，通常就是系統架構的直接反映。
**先找出倍率，再解釋它。**

`coverage` 的 `unclassified` 欄是**預設值不是結論**——未宣告的穿越件會落在那裡。

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
python ndd.py pinfn --board <板> --refdes U939 15   # 由工具鎖定三源
python ndd.py pinfn --list
```

`--refdes` 模式會自己從 BOM 取料號、從 netlist 取已接腳位與 footprint，
**不必靠你記得傳對料號**。

**先查快取，未命中才抽取；抽到的原文自動寫進 `verified-pins.csv`。**

> **快取原文，永不快取解讀。**
> 快取的是逐字內容 + 出處（檔名／頁碼／SHA-256／封裝欄），不是「pin1 與 pin3
> 內部連通」這種推出來的結論。原文快取 5 秒就能核對；推論快取會把錯誤凍結成
> 永久資產。datasheet 換版時 SHA-256 不符會自動失效重抽。

### 封裝判定只用 netlist + BOM

**工具不解析 datasheet 的腳位表。** 每家排版都不同，通用地「看懂」是無底洞，
而且一列錯位整張表就作廢。改用三個證據來源排名（同 `mate` 的機制）：

| 來源 | 標記 |
|---|---|
| **模型宣告的電源/接地腳，實際接在哪** | `[N]` |
| 訂購碼的封裝後綴（`…PW` / `…BS`） | `[B]` |
| layout 的 footprint 名稱 | `[N]` |

`ndd.py audit` 的 [1.5] 段會逐顆列出判定與證據：

```
[?]  a.U1 (EXP_APW) [?] 推論 PKGA16（margin 4）：
     拓樸[N] 16:PWR✓ 8:GND✓；料號後綴[B] ✓；footprint[N] ✓
待辦 a.U3 (EXP_A)  [?] 待補：候選 PKGA16／PKGB16；證據不足
```

**推論出來的一律標 `[?]`**，並帶 `package:inferred` caveat 沿路徑傳到 CSV。
門檻是「零矛盾 + 唯一勝出 + 至少一個獨立來源佐證」，達不到就列入待補，
**不替你假設**。要定案就填 `ndd.json` 的 `part_package`。

⚠️ 即使你明確宣告，若與 netlist 矛盾仍會 FAIL——**人講的最大，但矛盾要講出來**。

### pinfn 抽到多筆時不替你挑

```
!! pin 14 抽到 2 筆（這份 datasheet 可能涵蓋多種封裝）。拒絕替你挑，也不寫快取。
   [1] p.4  SCL   腳號欄位 14,12   serial clock line
   [2] p.4  VDD   腳號欄位 16,14   supply voltage
   -> --pick <n> [--package <標籤>]
```

腳號欄位直接攤在眼前，你 5 秒就能對照 audit 的封裝判定選定。**抽到一筆才是
已證明無歧義**，才會自動寫入快取。

### 元件模型（選用，非前提）

`models.json` 只在**同一顆 IC 被追第二次以上**時才值得建。schema 與規則見
`references/models.md`。要點：

- `direction` 必須顯式（`forward` / `bidirectional`），沒有預設值
- `pin_roles` 記下 VSS/VDD 是哪幾支腳（成本趨近於零，卻是封裝判定的主要證據）
- `gate`（通不通）與 `parameter_control`（通過後的性質）要分開
- `always` 由 netlist 推導，不可手寫
- **本 skill 不內建 seed model** —— 預先建模是把你的解讀凍結成永久資產

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

這不是放寬標準，而是把本文件早就寫下的原則落實成程式：**殘存候選要用「會不會
壞」排除；實際出貨的板子是接著線在跑的，對應若錯 netlist 根本對不上，早就會被
發現**。只有排名真的分不出來時才需要 layout／線束圖／實測。

**殘存候選要用「會不會壞」排除**：代入後看它會不會造成立即而明顯的故障
（SDA/SCL 對調 → I2C 全滅）。系統若實際會動，該候選就被排除了。這通常比找
線束圖快，而且是**唯一能同時涵蓋 layout 正確性的證據**。

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
> 帶 `mate:unapproved` 的路徑是候選，不是結論；`unclassified` 端點是預設值，
> 不是「已確認為負載」。

---

## 硬性規則

- **不要憑記憶寫腳位。** 一律 `pinfn` 查原文並標頁碼。
- **不要把控制關係當成訊號路徑。** latch/reset/select 進 hint，不進 trace。
- **不要讓單向元件雙向走。** `direction` 必須來自 datasheet。
- **不要用腳數推封裝。** netlist 只有已接腳。封裝由電源腳接法等證據排名判定。
- **推論出來的封裝一律標 `[?]`。** 它不是查證過的事實。
- **BOM 缺席就是未貼件**，除非該類零件不在該 BOM 的涵蓋範圍內（SMT BOM 的連接器/機構件）。
- **不要把 `mate:unapproved` 的路徑講成已確認對接。**
- **不要預先大量建模。** 要預先投資就投資在補 datasheet。
- **不要為了「看起來完整」而省略證據表。** 沒標記的主張等於自承未查證。
- **不要用檔名猜配對**（netlist↔BOM、料號↔datasheet）。用內容。
- **不要在文件裡混用事實與推論。** 推論一律標 ⚠️ 並寫清楚定案方式。
- **檔名含 CJK 相容表意字時**，Bash 會處理不了，改用 PowerShell 或 Glob/Read。
- **Windows 上 Python 讀不到 Git Bash 的 `/c/...` 路徑**，要用 `C:/...`。
