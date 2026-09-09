# 元件模型：transfer 邊、control、hint

模型檔是 `<專案>/models.json`。**本 skill 不內建任何 seed model**——通用型工具
不應把特定料號當成預設知識，而且預先建模等於把「你對 datasheet 的解讀」凍結成
看不見的永久資產。用到哪顆就自己查證後加哪顆。

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

## Schema

```json
{
  "MODEL_NAME": {
    "match": ["EXACT_MPN"],
    "kind": "signal_transfer",
    "package": "PACKAGE_TABLE_HEADER",
    "package_basis": "exact_table",
    "verified_against": "datasheet.pdf p.7 Table 3（文件編號 rev X）",

    "transfer": [
      {
        "from": ["2"],
        "to": ["18"],
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

### `direction` — 沒有預設值

| 值 | 用在 |
|---|---|
| `forward` | 緩衝器、放大器、fanout、單向轉換 |
| `bidirectional` | **datasheet 證實雙向**的 FET switch、I²C mux 的 SDA、兩腳被動件 |

⚠️ 任一方向當預設，都會靜默把另一半元件模型錯。載入時強制檢查。

`forward` 邊**只能由 `from` 走到 `to`**。逆向查詢要用獨立模式，不能偷偷反向 BFS。

### `package_basis` — 三選一

| 值 | 意義 | 需要 `package`？ |
|---|---|---|
| `exact_table` | 腳位隨封裝改變，本模型對應某一欄 | **是**，且要是欄位標題原文 |
| `not_applicable` | datasheet 只有一份腳位表 | 否 |
| `shared_pinout` | 多封裝，但用到的腳功能一致 | 否 |

⚠️ `package` 要填 datasheet 腳位表的**實際欄位標題**（例如 `HVQFN24`），不是
口語封裝名。這樣才能直接驅動 `pinfn` 的多欄選擇。欄標題會隨改版變動
（`HVQFN24 (SOT616-1)`），比對用子字串。

### `control` — 結構化，不是給人看的字串

| 欄位 | 值 |
|---|---|
| `type` | `enable` / `reset` / `address` / `select` / `mode` / `power` / `clock` / `trigger` / `other` |
| `mechanism` | `strap`（netlist 可解析）/ `runtime` / `external` / `unknown` |
| `polarity` | `high` / `low`，**只有 `enable` 與 `reset` 可以有** |

`address`、多 bit `select`、參數控制沒有 high/low 之分，強迫填只會產生假資訊。

### `gate` 與 `parameter_control` 是不同的東西

- `gate` —— 影響**通不通**
- `parameter_control` —— 影響**通過後的性質**（相位、增益、衰減、頻率）

RF 移相器的訊號**永遠通過**，控制位元改變的是相位。把它標成 `conditional` 會讓
讀者理解成「訊號可能過不去」，那是錯的。

`gate` 必須顯式是 `all_of` 或 `any_of`，不可依 list 順序或預設布林邏輯猜測。

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
「通不通」。把 address 腳放進 `gate` 會在載入時被拒絕——因為固定位址會被誤推成
`always`，而通道選擇其實是 runtime 決定的。

---

## 端點分類（`ndd.json` 的 `endpoints`）

走到一支腳、而該腳沒有可用的 transfer 邊時：

| endpoint_kind | 來源 | 進 `loads`？ |
|---|---|---|
| `terminal(declared)` | 人工宣告 | 是 |
| `stateful(declared)` | 人工宣告 | 是 |
| `unknown_stop(declared)` | 人工宣告「可能穿越但沒建模」 | **否**，停在具名位置 |
| `unknown_stop(model_unusable)` | 有模型但無法使用 | **否**，附原因碼 |
| `unclassified` | **預設** | 是，但帶 caveat |

`unknown_stop(model_unusable)` 的原因碼：`package_unresolved`、`package_conflict`、
`model_ambiguous`、`pin_absent`。「知道它很可能是穿越件、只是無法定案」與「尚未
分類」是不同狀態，不可混為一談。

⚠️ `unclassified` **必須**留在輸出裡。若把未宣告的一律排除，絕大多數真實終端
負載會在被宣告前全部消失，trace 的主要產出就報廢了。**列存在保住可用性，
caveat 保住誠實。**
