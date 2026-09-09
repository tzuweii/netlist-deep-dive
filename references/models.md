# 元件模型：transfer 邊、control、封裝判定

模型檔是 `<專案>/models.json`。

`references/example-models.json` 有四顆可直接複製的範例（bus switch、buffer、
clock fanout、I²C mux），但**不會自動載入**：

```bash
python ndd.py models --examples        # 看有哪些
python ndd.py models --add PCA9547     # 複製進專案
```

手動複製是刻意的：模型是「某人對 datasheet 的解讀」，自動塞進每個專案等於讓你
在不知情下用別人的解讀去追訊號——錯了會產生看起來完全合理、但整張表是錯的
訊號鏈。複製之後它就是**你的宣告**，`audit` 與 `REVIEW.md` 才會把它列進你要
負責複核的清單。

## 哪顆值得建模 —— 用數的，不要靠記憶

```bash
ndd.py blockers      # 訊號鏈停在哪些料號上、各擋住幾條
```

依「擋住的鏈路數」排序，並算出每顆還有幾支非電源訊號腳（數字大 = 訊號很可能
還會繼續走）。**只用 netlist，不需要 datasheet，也不需要跨 session 記憶。**

只是終端負載的，填 `ndd.json` 的 `endpoints` 就好——**不需要建模**。

---

## 為什麼分類的單位是「邊」而不是「元件」

同一顆 IC 可以同時具備三種性質。以一顆帶輸出鎖存的移位暫存器為例：

| 性質 | 例子 | 能進正式 trace？ |
|---|---|---|
| signal_transfer | 串接輸入 → 串接輸出 | 可以 |
| control_influence | LOAD／RCLK 觸發輸出暫存器更新 | **不可以** |
| stateful | 移位暫存器本身 | **不可以** |

以元件為單位分類的話，這顆只能被歸成一種，怎麼歸都是錯的。

> **「能控制它」不等於「訊號穿過它」。** latch/reset/select 腳會改變元件行為，
> 但不構成訊號路徑。兩者都要輸出，只有前者能進 `signal_chain.csv`。

---

## 封裝判定：只用 netlist 與 BOM

**工具不解析 datasheet 的腳位表。** 每家排版都不同（腳註標記、跨行儲存格、
文字層把兩個腳號併成一個），要通用地「看懂」是無底洞，而且一列錯位就讓整張表
作廢——實測 NXP 一份 16 腳的表就同時踩到三種。

改用這個工具**已經有的**機制：`mate` 的枚舉排名。三個獨立證據來源：

| 來源 | 標記 | 說明 |
|---|---|---|
| **拓樸一致性** | `[N]` | 模型宣告的電源/接地腳，實際是不是接在電源/地上 |
| 料號後綴 | `[B]` | 訂購碼的封裝碼（`…PW` / `…BS`） |
| footprint 名稱 | `[N]` | layout 選的實體 footprint |

判別力最強的是第一項：候選封裝之間差最多的通常就是電源腳位置，而 netlist 直接
就知道哪支腳接地、哪支腳接電源。**只需要知道那幾支腳，不需要整張表。**

### 判定結果

| status | 意義 | 可用？ |
|---|---|---|
| `user_confirmed` | `ndd.json` 的 `part_package` 明確宣告 | 可以 |
| `single_candidate` | 該料號只有一個模型，無從選錯 | 可以 |
| `inferred` | 證據排名唯一勝出 | 可以，但**一律標 `[?]`** 並帶 `package:inferred` |
| `unresolved_pending_user` | 證據不足 | **不可以**，列入待補 |
| `conflict` | 宣告或候選與 netlist 矛盾 | **不可以**，`audit` FAIL |

`inferred` 的門檻：**零矛盾 + 唯一勝出（margin ≥ 2）+ 至少一個獨立來源佐證**。
達不到就問人，**不得替使用者假設**。

⚠️ 即使使用者明確宣告，若與 netlist 矛盾仍會報 `conflict`——**人講的最大，但
矛盾要講出來**，不能默默照單全收。

### `pin_roles` 是關鍵欄位

```json
"pin_roles": {"8": "GND", "16": "PWR"}
```

建模的人在 datasheet 上看到 VSS/VDD 是哪幾支腳時順手記下來（成本趨近於零），
工具就能**只用 netlist** 驗證這個封裝對不對得上這塊板。合法值：`GND` / `PWR`
/ `SIG`。

⚠️ **宣告的 GND/PWR 腳在 netlist 上完全沒接 = 矛盾**，不是「資料不足」。
訊號腳可以 NC，電源腳不會。這條規則是拓樸判別力的主要來源。

---

## Schema

```json
{
  "MODEL_NAME": {
    "match": ["BASE_MPN"],
    "kind": "signal_transfer",
    "package": "自己填的封裝標籤",
    "verified_against": "datasheet.pdf p.7 Table 3（文件編號 rev X）",

    "pin_roles": {"8": "GND", "16": "PWR"},
    "ordering_suffix": ["PW"],
    "footprint_match": ["TSSOP16"],

    "transfer": [
      {
        "from": ["2"], "to": ["18"],
        "direction": "forward",
        "gate": {"all_of": ["1"]},
        "parameter_control": [],
        "verified_against": "datasheet.pdf p.7"
      }
    ],

    "control": {
      "1": {"type": "enable", "polarity": "low", "mechanism": "strap"}
    },

    "runtime_conditions": {
      "channel_enabled": {"kind": "runtime_register",
                          "verified_against": "datasheet.pdf p.12"}
    },

    "control_influence": [
      {"from": ["12"], "effect": "latch_state_update",
       "affects": "output_register", "verified_against": "datasheet.pdf p.5"}
    ]
  }
}
```

`match` 填**基礎料號**即可——比對允許訂購碼後綴（`PCA9554B` 對得上
`PCA9554BPW`），但多出來的部分必須以**字母**開頭，所以 `LM358` 不會誤中
`LM3584`。

同一料號有多個封裝版本時，就建多個模型（各自 `package` / `pin_roles` /
`ordering_suffix` / `footprint_match` 不同），讓證據去分勝負。

### `direction` — 沒有預設值

| 值 | 用在 |
|---|---|
| `forward` | 緩衝器、放大器、fanout、單向轉換 |
| `bidirectional` | **datasheet 證實雙向**的 FET switch、I²C mux 的 SDA、兩腳被動件 |

⚠️ 任一方向當預設，都會靜默把另一半元件模型錯。`forward` 邊**只能由 `from`
走到 `to`**。

### `control` — 結構化，不是給人看的字串

| 欄位 | 值 |
|---|---|
| `type` | `enable` / `reset` / `address` / `select` / `mode` / `power` / `clock` / `trigger` / `other` |
| `mechanism` | `strap`（netlist 可解析）/ `runtime` / `external` / `unknown` |
| `polarity` | `high` / `low`，**只有 `enable` 與 `reset` 可以有** |

### `gate` 與 `parameter_control` 是不同的東西

- `gate` —— 影響**通不通**
- `parameter_control` —— 影響**通過後的性質**（相位、增益、衰減、頻率）

RF 移相器的訊號**永遠通過**，控制位元改變的是相位。標成 `conditional` 會讓
讀者理解成「訊號可能過不去」，那是錯的。

---

## `always` 必須被正面證明

手寫 `"condition": "always"` 是把未經查證的斷言凍結成資產。改為宣告「這條邊由
哪些 gate 把關」（事實），通斷性質由 netlist 實際接法推導（結論）：

| netlist 實際狀況 | 推出的 gating |
|---|---|
| `gate` 為空 | `always` |
| 所有 `enable` 皆 `strap` 且接到符合極性的硬 rail | `always` |
| 任一 gate 是 `runtime` 或 `external` | `conditional`（**與 strap 狀態無關**） |
| `enable` 接到另一顆 IC 的輸出 | `conditional`，並記錄驅動者 |
| `enable` 落在單腳網路（懸空） | `unknown` + 獨立警告 |
| `enable` 接到**相反**極性的 rail | `conditional`（`tied_inactive`） |

⚠️ **`type: address` 完全不參與 gating 推導。** 位址決定「是哪一顆」，不決定
「通不通」。把 address 腳放進 `gate` 會在載入時被拒絕。

---

## 端點分類（`ndd.json` 的 `endpoints`）

走到一支腳、而該腳沒有可用的 transfer 邊時：

| endpoint_kind | 來源 | 進 `loads`？ |
|---|---|---|
| `terminal(declared)` | 人工宣告 | 是 |
| `stateful(declared)` | 人工宣告 | 是 |
| `driver(model)` | 反向走到 transfer 邊的輸出端 | **否**，那是訊號來源不是負載 |
| `unknown_stop(declared)` | 人工宣告「可能穿越但沒建模」 | **否**，停在具名位置 |
| `unknown_stop(model_unusable)` | 有模型但無法使用 | **否**，附原因碼 |
| `unclassified` | **預設** | 是 |

原因碼：`package_unresolved`、`package_conflict`、`model_ambiguous`、`pin_absent`。

⚠️ `unclassified` **必須**留在輸出裡。若把未宣告的一律排除，絕大多數真實終端
負載會在被宣告前全部消失，trace 的主要產出就報廢了。

⚠️ 但它**不掛 caveat、不降級 `confidence`**。路徑本身由 netlist 完整支持；
未分類的是「訊號到終點之後會不會繼續走」，那是**覆蓋率缺口**不是路徑證據不足。
實測一塊真實板子有 96% 的列都是 unclassified——讓它降級等於把「所有主張看起來
一樣確定」倒過來變成「所有主張看起來一樣不確定」，一樣沒有資訊。

狀態由 `endpoint_kind` 欄呈現，待辦由 `coverage` 與 `REVIEW.md` 列出。
