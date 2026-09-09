# netlist-deep-dive 實作規格

> 狀態：**待實作**。基準 commit：`eabab9e`。
>
> 本文件定義通用型 skill 的行為契約，不以任何單一產品、板名、refdes、net 名或
> 特定 IC 作為規則前提。真實專案僅可作為驗證語料，不能變成程式中的特例。

---

## 1. 目標與界線

本 skill 的最高原則是三源對照：

| 來源 | 可證明的事 |
|---|---|
| Netlist `[N]` | 板內哪個 `refdes.pin` 接到哪條 net |
| BOM `[B]` | 該 refdes 的實際 MPN、裝配狀態與 BOM 範圍 |
| Datasheet `[D]` | 該 MPN／封裝中 pin 的功能、方向、transfer 與控制條件 |

本工具要產生兩種**不可混用**的結果：

1. **正式 signal trace**：只使用明確宣告、可由 datasheet 查驗的 signal-transfer
   邊；必須保留 mate、package、control 等 caveat 與 confidence。無阻斷 caveat 的列
   可作為架構結論；其餘是可追溯的候選路徑。
2. **functional topology hint**：描述控制、狀態、參數調整與未知邊界；有助於 AI
   理解系統，但**永不得作為 BFS 的下一跳，也不得被敘述為 net 連通**。

### 非目標

- 不要求使用者為全專案所有 IC 建 package map。
- 不建立 BOM 變體推理引擎，不自動展開 refdes 範圍，不從名稱猜測 IC 行為。
- 不把 layout、線束、韌體狀態當成 netlist 可證明的事。
- 不建立累積式「AI 推論資料庫」；快取只保存 datasheet 原文與可複驗事實。

---

## 2. 全域安全規則

1. **沒有證據不能亮綠燈。** 不確定時輸出 caveat、未知端點或待補資料，不得以
   預設值補全。
2. **傳輸邊與功能影響邊分開。** 控制某 IC 不等於訊號穿越該 IC。
3. **方向必須顯式。** 不得把所有 pin pair 預設成雙向。
4. **`always` 必須被正面證明；其餘預設 `conditional` 或 `unknown`。**
5. **封裝只在會改變答案時解析。** 不以已接腳數推定 package。
6. **所有可影響結論的降級資訊都要沿路徑傳到最終輸出。**
7. **每個 commit 都要附合成 regression test。** 測試不可依賴客戶專案檔案、
   私有 datasheet 或特定 refdes。

---

## 3. 檢查責任矩陣

新增的每一種失敗都必須有明確的浮現位置。使用者跑完 `audit` 得到全 PASS，卻仍
有未解事項存在，就等於回到「綠燈不代表查過」。

| 失敗／未解狀態 | 浮現位置 | 效果 |
|---|---|---|
| parser selfcheck 不一致 | `audit` [0] | FAIL，且不得繼續宣稱下游有效 |
| `mate_map` 不完整／非單射／pin 不存在 | `Fabric` 建構時（`mate`／`trace`／`review`） | 直接拒絕，CLI 印乾淨訊息並回傳 exit 2 |
| 未批准 mate | `mate`、`trace`、`signal_chain.csv` | caveat + confidence 降級 |
| model `pin_absent` | `audit` | FAIL |
| model 多重 match／同分 | 載入時 | 直接拒絕載入 |
| package `unresolved_pending_user` | `audit`、`coverage`、`REVIEW.md` | 待辦，非錯誤 |
| package `conflict` | `audit` | FAIL，停走該 model |
| BOM ambiguity | `audit` [3]、相關 assertion | FAIL，列出衝突列號 |
| `bom-scope-insufficient` | `audit` [3]、相關 assertion | 不得 PASS |
| 未宣告 endpoint（`unclassified`） | `coverage`、`REVIEW.md` | 待辦，非錯誤 |

- [ ] 「待辦」與「錯誤」在輸出上必須可區分，且待辦不得讓 `audit` 假性通過。

---

## 4. Commit 1 — 測試框架與 parser／trace 靜默失敗

### 3.1 測試框架

- [ ] 建立 `tests/`、pytest（或 stdlib unittest）入口與合成 `.asc`／`.xlsx`
  fixture builder。
- [ ] 每個後續 commit 新增對應 regression test；CI／本地單一命令可執行。
- [ ] fixture 使用抽象料號與 refdes，例如 `BUF_A`、`MUX_A`、`U1`，不可綁定
  特定專案名稱。

### 3.2 parser selfcheck 補 pinmap 計數

現行 `selfcheck()` 只比對原始 pin token 數與 `nets` 內的 pin 數。同一支
`refdes.pin` 若被寫入兩條 net，`pinmap` 會覆蓋前值，但兩個計數仍可能同時通過。

- [ ] 比對 `sum(len(v) for v in pinmap.values())`。
- [ ] 列出重複的 `refdes.pin` 與涉及的 net。
- [ ] 自檢失敗時 `audit` 必須失敗，不得繼續宣稱下游結論有效。

**驗收：** 一支腳掛兩條 net 的 fixture 必須 FAIL，且指出該腳。

### 3.3 mate 查詢方向對稱

`cmd_trace` 的 mate 查詢必須與設定順序無關。

- [ ] 對接查詢使用單一雙向 helper，不可在不同呼叫點各自實作。
- [ ] 查無對手時保留輸出列，標 `mate:missing`；不得靜默 `continue`。

**驗收：** 對同一 mate 以正向與反向宣告，trace 列數與結果相同。

---

## 5. Commit 2 — confidence、caveat 與已批准 mate map

### 4.1 單一 confidence 軸

- [ ] 在單一模組定義有序 `CONFIDENCE` enum；禁止字串排序。
- [ ] caveat 映射至 confidence 上限；一條路徑取所有邊的最低值。
- [ ] caveat 以 set 累積、輸出前排序，避免重跑產生假 diff。

**confidence 只表達證據品質，不表達閘控狀態。** 兩者是不同的軸：

| 欄位 | 值 | 意義 |
|---|---|---|
| `confidence` | `confirmed > caveated > unknown` | 這條邊的**證據**有多強 |
| `gating` | `always` / `conditional` / `unknown` | 這條邊**已查證**的通斷性質 |

不可合併成一軸。一條 datasheet 查證完整、package 已鎖定的 runtime-gated 邊，證據
品質是 `confirmed`；把它排在「package 未佐證但恆通」之下會與事實相反，並誘使
使用者為了拉高 confidence 而迴避正確標註的 conditional 邊。

實際降級原因一律由 `caveats` 表達，不再另設排序。

### 4.2 mate map

在 `ndd.json` 新增可物化為完整 pin 對映的 `mate_map`。`transform` 可是受限的
轉換描述或顯式 map，但載入後必須先展開成 `{pin_a: pin_b}` 再驗證。

```json
{
  "mate_map": {
    "board_a:J1|board_b:J2": {
      "approved": {"1": "1", "2": "2"},
      "evidence": "harness drawing ..., rev ...",
      "confidence": "confirmed"
    }
  }
}
```

- [ ] 驗證 mapping 完整性、單射、雙向往返、pin 存在性。
- [ ] 列出 B 側未被命中的腳；腳數不等時可合法，但不可隱藏。
- [ ] 有批准 map 時只使用該 map。
- [ ] 無批准 map 時可暫退回同 pin label 對接以保留探索能力，但每條此類邊必須
  帶 `mate:unapproved`，並降級 confidence。

### 4.3 輸出

- [ ] `signal_chain.csv` 新增 `caveats`、`confidence`。
- [ ] 有未批准 mate 時命令列開頭印出警告。
- [ ] 文件明確說明：含 `mate:unapproved` 的列是候選路徑，不是已確認線束／板對板
  對接結論。

**驗收：** 未批准 mate 下游的每一列都帶 `mate:unapproved`；錯誤的非單射 map
必須載入失敗。

---

## 6. Commit 3 — 封裝判定（只用 netlist + BOM）與 pinfn

### 6.1 為什麼不從 datasheet 表格推

每家 datasheet 的腳位表排版都不同。實測一份 NXP 16 腳的表同時踩到三種文字層
問題：腳註標記讓整列比不到、符號與資料被拆成兩行、**兩個腳號被併成一個**
（`VSS 86` 其實是 pin 8 與 pin 6）。

要通用地「看懂」表格是無底洞，而且**一列錯位就讓整張表作廢**——用一張已知有
錯位的表去產生腳位對應，比不做更危險。

### 6.2 改用證據排名（沿用 `mate` 的機制）

- [ ] 三個獨立來源：模型宣告的電源/接地腳實際接法 `[N]`、訂購碼後綴 `[B]`、
  footprint 名稱 `[N]`。
- [ ] 判別力最強的是第一項；**宣告的 GND/PWR 腳在 netlist 上完全沒接 = 矛盾**
  （訊號腳可以 NC，電源腳不會）。
- [ ] `inferred` 門檻：零矛盾 + 唯一勝出（margin ≥ 2）+ 至少一個獨立來源佐證。
- [ ] **推論結果一律標 `[?]`** 並帶 `package:inferred` caveat 沿路徑傳到 CSV。
- [ ] 使用者明確宣告優先，但**與 netlist 矛盾時仍報 `conflict`**——人講的最大，
  矛盾要講出來。
- [ ] 證據不足 → `unresolved_pending_user`，列入待補，**不替使用者假設**。
- [ ] **不得以已接腳數推定封裝**（netlist 只有已接腳，會穩定偏向較小的封裝）。

### 6.3 模型的證據欄位

- [ ] `pin_roles`（`{pin: GND|PWR|SIG}`）—— 建模時順手記下 VSS/VDD，成本趨近
  於零，卻是封裝判定的主要證據。
- [ ] `ordering_suffix`、`footprint_match` —— 選填的佐證來源。
- [ ] `match` 填基礎料號即可；比對允許**訂購碼後綴**（`PCA9554B` 對得上
  `PCA9554BPW`），但多出的部分必須以字母開頭，避免 `LM358` 誤中 `LM3584`。

### 6.4 pinfn 只攤原文，不做封裝判定

- [ ] 抽到**一筆** → `not_applicable`（**已證明**無歧義），寫入快取。
- [ ] 抽到**多筆** → 列出全部原文、頁碼與腳號欄位，**拒絕替使用者挑，也不寫
  快取**；用 `--pick <n>` 指定。
- [ ] 抽不到 → 明說「請人工開 PDF」，**不猜**。
- [ ] 快取加入 `package`、`resolved_by`、完整 datasheet SHA-256。

**驗收：**

- 同料號兩封裝、電源腳位置不同 → 由 netlist 正確判定，且標 `[?]`；
- 無證據時必須待補，不得挑第一個；
- 宣告與 netlist 矛盾必須報 `conflict`；
- 宣告的電源腳未接必須算矛盾；
- 腳數在任何情況下都不得作為封裝的正面證據。

## 7. Commit 4 — 通用的 transfer、control 與 topology 資料模型

### 6.1 分類單位是邊與端點，不是 IC

同一 IC 可以同時有可傳輸資料的邊、控制腳與狀態行為。因此採兩層分類：

| 層級 | 類別 | 是否可進正式 trace |
|---|---|---|
| 邊 | `signal_transfer` | 可以；須通過三源與 package 規則 |
| 邊 | `control_influence` | 不可以；只進 topology hint |
| 端點 | `stateful` | 不可以 |
| 端點 | `terminal` | 不可以 |
| 端點 | `unknown_stop` | 不可以 |
| 端點 | `unclassified` | 不可以；預設、必帶 caveat |

`signal_transfer` 是**功能上的可驗證訊號傳輸**，不等同於歐姆短路；可用於 buffer、
放大器、switch、mux、fanout、RF transfer 等。資料表必須明確支持所宣告的
from/to 關係與方向。

### 6.2 package-aware model selection

`match_model()` 的責任僅是找 MPN 候選；`transfer_for()` 必須在 package 解析後
選擇真正可用的 model。

- [ ] 先精確 MPN，再取唯一最長、具 token 邊界的 match；同分報錯。
- [ ] 若同 MPN 有多個 package-specific model，只能選與 resolved package 相容者。
- [ ] package 未解決且候選 transfer pinout 不一致時，回傳 no transfer，端點為
  `unknown_stop(model_unusable)`（見 6.7）；不得取第一個 model，也不得降為
  `unclassified`——「知道它很可能是穿越件、只是無法定案」與「尚未分類」是不同狀態。
- [ ] model 的 `package_basis` 必須是 `exact_table`、`not_applicable` 或
  `shared_pinout` 之一；前者記錄 datasheet 的實際欄標題。

**pin-existence sanity check**（與 package 解析獨立，package 未鎖定時照樣可跑）：

- [ ] 逐顆比對 model 所有 `transfer.from`／`to`／`gate` 引用的 pin label 是否
  存在於 `nl.pins(refdes)` 的**已觀測腳位**中；不存在即報 `pin_absent`。
- [ ] **名稱必須誠實**：這是 pin-existence check，**不是 package 驗證**。兩個
  package 同為 1–N 且腳位定義不同時它必然通過。它能抓的是打錯、以及照抄了
  不同衍生型號的 model。

### 6.3 有向 transfer schema

以 `transfer` 取代舊 `pairs`。每一邊都必須顯式標方向，沒有預設值。

```json
{
  "match": ["EXACT_MPN"],
  "package_basis": "exact_table",
  "package": "PACKAGE_TABLE_HEADER",
  "kind": "signal_transfer",
  "verified_against": "datasheet.pdf p.7, Table ...",
  "transfer": [
    {
      "from": ["IN"],
      "to": ["OUT"],
      "direction": "forward",
      "gate": {"all_of": []},
      "parameter_control": ["CFG0"],
      "verified_against": "datasheet.pdf p.7"
    }
  ]
}
```

- [ ] `direction` 僅允許 `forward`、`bidirectional`。
- [ ] `forward` 僅能由 `from` 走到 `to`；逆向查詢須有獨立模式，不能偷偷反向 BFS。
- [ ] `bidirectional` 只適用 datasheet 證實雙向 transfer 的邊。
- [ ] model 層可提供預設 `verified_against`，逐邊可 override。
- [ ] `gate` 必須顯式是 `all_of` 或 `any_of`；不可依 list 順序或預設布林邏輯猜測。

### 6.4 實體 control 與 runtime gate

物理控制腳與軟體／暫存器狀態是不同資料型別。

```json
{
  "control": {
    "EN": {"type": "enable", "polarity": "high", "mechanism": "strap"},
    "ADDR0": {"type": "address", "mechanism": "strap"},
    "SEL0": {"type": "select", "mechanism": "runtime"}
  },
  "runtime_conditions": {
    "channel_enabled": {
      "kind": "runtime_register",
      "verified_against": "datasheet.pdf p.12"
    }
  }
}
```

- [ ] `control.type` 的標準值為 `enable`、`reset`、`address`、`select`、`mode`、
  `power`、`clock`、`trigger`、`other`。
- [ ] `polarity` 僅適用有極性的 gate（例如 enable／reset），為選填；address、
  多 bit select、parameter control 不得被強迫填 high/low。
- [ ] `mechanism` 為 `strap`、`runtime`、`external`、`unknown`。
- [ ] `gate.all_of`／`any_of` 的元素可為實體 pin 或 `runtime_conditions` 名稱。
- [ ] runtime register gate 一律至少 `conditional`；不得因 address pin 固定就推成
  `always`。
- [ ] gate 的實體 pin 要由 package-locked pin function 支持；純文字 hint 可不解析
  package，但不得參與 `always` 推導。

### 6.5 gate 與 parameter control

- [ ] `gate` 只描述「訊號能否通過」。
- [ ] `parameter_control` 描述「訊號通過後性質如何改變」，例如相位、增益、
  衰減、頻率或模式；它不得把 transfer 降格為不通。
- [ ] `always` 僅在所有必要 physical gate 已由 netlist 證實接至正確有效狀態時
  產生；runtime、IC 驅動、浮接或未知條件分別產生 `conditional`／`unknown`
  caveat。

### 6.6 control-influence 與 topology hint schema

`control_influence` 是**功能關係**，不可建立 net-to-net 或 pin-to-pin BFS 邊。

```json
{
  "control_influence": [
    {
      "from": ["LATCH"],
      "effect": "latch_state_update",
      "affects": "output_register",
      "verified_against": "datasheet.pdf p.5"
    }
  ]
}
```

- [ ] `topology_hint` 每列至少列出實際 pin/net `[N]`、BOM MPN `[B]`、資料表出處
  `[D]`；缺件明示 `[D 缺]`，不可用名稱補完。
- [ ] hint 可說明「控制某狀態」、「改變某參數」、「停止於未知穿越件」，不可說成
  已確認連通，亦不可被下一跳 BFS 使用。
- [ ] `stateful` 元件可有少數明確 signal-transfer 邊；其餘腳仍可輸出 state／
  control hint，不能因元件整體被標 stateful 而抹掉已驗證 transfer。

### 6.7 endpoint 分類

當進入一個 pin 且沒有可用 transfer 邊時，輸出以下之一：

| endpoint_kind | 意義 | 進 loads |
|---|---|---|
| `terminal(declared)` | 已宣告為終端／負載 | 是 |
| `stateful(declared)` | 已宣告為狀態或轉換端點 | 是 |
| `unknown_stop(declared)` | 已宣告可能穿越但尚無可用 model | 否 |
| `unknown_stop(model_unusable)` | **有 model 但無法使用**（package 未解析／衝突／多重 match） | 否 |
| `unclassified` | 預設，尚未知道是否為終端或穿越件 | 是，且帶 caveat |

- [ ] `unknown_stop` 必須顯示具名 refdes.pin，不得混入 `loads`。
- [ ] `unknown_stop(model_unusable)` 必須記錄原因碼：`package_unresolved`、
  `package_conflict`、`model_ambiguous`、`pin_absent`。
- [ ] `unclassified` 必須保留在 CSV；否則未宣告的大量真實終端會使結果失去用途。
- [ ] endpoint 宣告可為 MPN 預設加 board/refdes override，並寫入 coverage。

### 6.8 輸出護欄

- [ ] `signal_chain.csv` 僅含 `signal_transfer` 邊與端點；欄位至少有
  `caveats`、`confidence`、`endpoint_kind`。
- [ ] 含 `mate:unapproved`、package 未佐證或 `conditional` 的列必須可見，且不得
  在文件中稱為無條件已確認路徑。
- [ ] `topology_hint` 為另一份輸出，檔名 `topology_hint.csv`，僅含 control
  influence、stateful 行為、parameter control、未知邊界與來源。欄位至少有
  `board`、`refdes`、`pin`、`net`、`mpn`、`relation`、`effect`、`source`。
- [ ] 在程式介面上將 trace graph 與 hint graph 分成不同資料結構，禁止 hint graph
  傳給 BFS。

**Commit 4 必收測試：**

- 單向 buffer 不得從 output 反走到 input；
- fanout 任兩個 output 不得藉 input 互通；
- 資料表證實的雙向 switch 仍可雙向走；
- stateful 元件的 latch／clock 影響只能出現在 hint，不能到其 Q outputs；
- runtime-register gate 必為 `conditional`，固定 address 不得改為 `always`；
- parameter control 不得把本來恆通的 RF／analog transfer 標成不通；
- package 不同且 transfer pinout 不同時不得選 model；shared pinout 時不得詢問
  package；
- hint 不得改變正式 trace 的可達端點集合。

---

## 8. Commit 5 — BOM ambiguity 與 BOM 範圍誠實性

### 7.1 重複 refdes

- [ ] BOM 解析器記錄 `dups: refdes -> row numbers`，不可後列靜默覆蓋前列。
- [ ] `of()` 遇衝突回傳 ambiguity sentinel。
- [ ] `pn_of`、export stuffed 欄、DNI audit、role check、`count_pn`、
  `not_stuffed` 遇 ambiguity 必須明確 FAIL 或輸出衝突列號。

### 7.2 refdes 範圍

- [ ] 偵測疑似 `R1-R5` 範圍並警告。
- [ ] 僅在 `expand_ranges: true` 時展開；預設不得猜測。

### 7.3 變體／不完整 BOM

不實作完整 variant engine，但必須避免把未知 BOM 範圍誤稱 DNI。

- [ ] board 設定新增 `bom_scope: complete | smt_only | variant | unknown`。
- [ ] `complete` 才可將 netlist 有、BOM 無的合格電子件標為候選 DNI。
- [ ] `smt_only`、`variant`、`unknown` 的缺件標 `bom-absent:<scope>`，不是「真 DNI」。
- [ ] `not_stuffed` assertion 只有在 scope 足以支持時才可 PASS；否則輸出
  `bom-scope-insufficient`。

**驗收：** 重複 refdes、範圍 refdes、variant BOM 缺件三種 fixture 都不可得到
靜默 PASS 或「真 DNI」。

---

## 9. Commit 6 — manifest 與可重現性

新增 `ndd.py manifest`。

- [ ] 輸出所有 `.asc`、BOM、使用到的 datasheet、`ndd.json` 的完整 SHA-256。
- [ ] 輸出 BOM sheet／scope、工具 git revision（或版本）、日期。
- [ ] 若 manifest 產物在專案目錄內，hash 時排除自身。
- [ ] 文件產物可引用 manifest ID，而非手打來源版本。

**驗收：** 任一輸入檔內容改變後 manifest 必須改變；manifest 自身變動不得影響
自己的 hash。

---

## 10. Commit 7 — coverage 與 REVIEW

新增 `ndd.py coverage`，以 **per-MPN** 呈現，不做無人維護的 per-pin 矩陣。

| 欄位 | 值 |
|---|---|
| BOM | 有／無／ambiguity |
| BOM scope | complete／smt_only／variant／unknown |
| Datasheet | 有／無 |
| package-locked pinfn | 筆數 |
| transfer／endpoint 狀態 | modelled／declared-unmodelled／stateful／terminal／unclassified |
| 待處理事項 | package、datasheet、mate、model 或 BOM scope |

- [ ] 未鎖定 package 的 pinfn 不得計入已覆蓋。
- [ ] 名稱模式只能作為「可能需 review」提示，不得自動把元件分類成 transfer。
- [ ] coverage 放在 `REVIEW.md` 開頭，但不取代既有人工複驗清單。
- [ ] `REVIEW.md` 的腳位模型段需配合逐邊 schema 重寫：列出每條 transfer 邊的
  `direction`、`package_basis`、`verified_against`，而非既有的整顆 model 一行。

**驗收：** 未鎖定 package 的 pinfn 快取列不得使該 MPN 顯示為已覆蓋；
`unclassified` 端點必須出現在待處理事項。

---

## 11. 文件與相容性

- [ ] `SKILL.md` 同步三源規則、package 惰性政策、transfer/hint 分離、BOM scope。
- [ ] `references/verification.md` 補 parser pinmap、mate、方向與 package 的驗證界線。
- [ ] `references/pitfalls.md` 補「已接腳數不等於 package 腳數」、「無模型不等於
  終端」、「control 不等於 transfer」、「反向穿越」四類錯誤。
- [ ] `references/example-ndd.json` 使用抽象元件與 board 名，示範 mate_map、
  part_package、transfer、runtime gate、control influence、endpoint、bom_scope。
- [ ] `README.md` 明載：正式 trace 是已驗證 transfer；hint 是功能說明，不是連通。
- [ ] `pairs -> transfer` 為破壞性 schema 遷移，提供清楚錯誤訊息與 migration note；
  不做靜默相容轉換。

### 設定檔遷移（`ndd.json`）

- [ ] `cmd_init` 必須寫出所有新欄位的空骨架：`mate_map`、`part_package`、
  `bom_scope`、endpoint 宣告。未出現在骨架裡的欄位，使用者不會知道它存在。
- [ ] `bom_kind` → `bom_scope` 是欄位改名。載入舊檔時給明確錯誤訊息與對應表
  （`SMT BOM` → `smt_only`、完整 BOM → `complete`、未標註 → `unknown`），
  **不得靜默沿用舊欄位，也不得預設為 `complete`**（預設 complete 會把未知範圍
  的缺件誤報成真 DNI）。

### 快取遷移（`verified-pins.csv`）

舊列同時缺 `package`／`resolved_by`／`corroborated_by`，且 SHA-256 由 16 字元
前綴改為完整值。

- [ ] 舊列一律視為 `resolved_by: unresolved_pending_user`，**不得當成已解析**；
  否則等於把未鎖定 package 的舊快取洗成合法資料。
- [ ] 前綴 SHA 與完整 SHA 不可直接字串比對；遷移時明確標記為待重抽。
- [ ] 快取是長期累積的資產，遷移規則錯一次會長期污染，必須有 regression test。

---

## 12. 真實專案驗證的使用方式

真實專案可用於確認上述通用 fixture 沒漏掉現場情況，例如多封裝 pin table、
runtime-controlled mux、stateful control、RF parameter control、variant BOM 與複雜
跨板 connector。

但它們必須遵守：

- 不把真實 MPN、net、refdes、板名寫入 parser、graph、分類器或預設規則；
- 專案專屬 transfer model 放在該專案的 `models.json`，不是 skill 的通用 seed；
- 若保留可重用 seed model，必須 exact-MPN + package-aware 啟用，並有公開可取得的
  datasheet 與獨立合成 regression fixture；
- 真實專案數量、coverage 比例與個別結果只可出現在驗證報告，不可成為規格門檻。

### 既有 seed model 的處置

初版內建四個 seed model（bus switch、buffer、clock fanout、I²C mux）。它們是
`pairs` schema 且無 `package_basis`，在本規格下無法載入。

- [ ] 逐一決定：遷移為 `transfer` schema（補 `direction`、`gate`、
  `package_basis`）或移除。
- [ ] 保留者必須符合 §11 的 seed 門檻，且各配一份**合成** regression fixture，
  不得以真實專案檔案作為測試依據。
- [ ] 遷移時必須重新核對 datasheet，**不得從舊 `pairs` 推導方向**——舊 schema
  的對稱性正是要修掉的錯誤，直接轉換會把錯誤帶進新 schema。
