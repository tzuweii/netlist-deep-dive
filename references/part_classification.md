# 零件分類（這顆到底要不要查 datasheet）

`coverage` 會把每種料號分類，決定要不要查規格書。分類由工具處理（順位、料件
資料庫對照、footprint 規則見 `docs/設計說明.md`）；讀結果時看標記：

| 標記 | 意思 | 回答時 |
|---|---|---|
| `(宣告)` | 人在 `ndd.json` 指定 | 照用 |
| 無標記 | 公司料件資料庫（CIS 快照）查到 | 事實 |
| `[?]` | 由 footprint、料號樣式或 refdes 開頭推測 | 推論，標 `[?]` |
| `!!` | 認不出來 | 列給使用者確認，**不猜** |

`placeholder`：料號以 `OPEN`／`NC`／`DNP`／`DNI`／`NOSTUFF` 開頭的，是**刻意保留
的未貼位置**，不需要規格書，也不要叫人去找。

## 未辨識的多半是自製件——請人工補，工具不猜

剩下認不出來的多半是公司自製／專案專屬的東西（屏蔽罩、自訂 footprint 的熱敏
電阻、客戶代號開頭的天線），沒有通用命名可循，**猜出來的分類比空白更糟**。
`coverage` 表尾會列出「分類未辨識，需你確認」，在 `ndd.json` 補：

```json
"part_class": {
  "OPEN": "placeholder",
  "ME": "mechanical",
  "b0017_dpu_v1:A12": "mechanical"
}
```

key 可以是 `<board>:<refdes>`（單一實例）、料號、或裸前綴（整批適用）。
footprint 樣式則用另一個欄位：

```json
"footprint_class": {
  "PROJ01": "mechanical",
  "R-1Kohm": "sensor"
}
```

**隨用隨長，不是必填清單**——跟 `part_package`、`endpoints` 同一個慣例。

料件資料庫快照是**選用加速器**：沒有就退回 footprint 推論，不報錯。要設定時，
匯出方式見 `docs/設計說明.md`。
