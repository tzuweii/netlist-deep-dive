# 零件分類（這顆到底要不要查 datasheet）

`coverage` 原本把每一顆 active 平等列出，於是屏蔽罩、螺帽跟 FPGA、ADC 混在
同一張表，每一列都掛著「待查 datasheet」。那不是清單，那是雜訊——真正該先查
的東西被埋掉了。

## 分類的權威來源是**料號**，不是 refdes 前綴

前綴是各家自己的習慣。實測某專案用 `ME` 標機構件、`OPEN` 標預留位置、
`Coupler` 標耦合器——這些寫死進工具的通用表，就是在遷就單一專案，換一塊板又
要補規則，而且補的人不會知道漏了什麼。

公司的料件資料庫（OrCAD CIS）本來就有一套**綁在料號上**的分類體系。實測 6
塊板 1509 顆 active：光靠料號查表命中 **93.6%**，查不到的只有 2 個料號。

## 順位（第一個命中就採用）

| 順位 | 來源 | 標示 | 這是事實還是推論 |
|---|---|---|---|
| 1 | `ndd.json` 的 `part_class` | `(宣告)` | 人講的最大 |
| 2 | CIS 快照查表（料號） | 無標示 | **事實** |
| 3 | refdes 前綴通用表 | `[?]` | **推論** |
| 4 | 都沒命中 | `!!` | 未辨識，**攤出來等人確認，不猜** |

⚠️ 第 2 與第 3 順位可信度不同，**`source` 一定要跟著分類一起帶出去**。CIS
命中是 `[B]` 等級的事實，前綴推出來的是 `[?]`。混為一談等於把推論洗成事實，
而且沒有任何症狀。

## CIS 快照怎麼來

**唯讀 SELECT，密碼不經過 AI**，在自己的 PowerShell 跑：

```powershell
$srv='<CIS 伺服器 IP>'; $db='CIS'; $user='<帳號>'
$pw = Read-Host "密碼" -AsSecureString
$pw.MakeReadOnly()
$cred = New-Object System.Data.SqlClient.SqlCredential($user,$pw)
$cn = New-Object System.Data.SqlClient.SqlConnection("Server=$srv;Database=$db")
$cn.Credential = $cred
$cn.Open()
$cmd = $cn.CreateCommand()
$cmd.CommandText = @'
SELECT Number, Manufacturer_PN, classify_str, Part_Type_CIS, Parts_Description
FROM CIS_ALL
WHERE (Manufacturer_PN IS NOT NULL AND Manufacturer_PN <> '')
   OR (Number IS NOT NULL AND Number <> '')
'@
$dt = New-Object System.Data.DataTable
(New-Object System.Data.SqlClient.SqlDataAdapter($cmd)).Fill($dt) | Out-Null
$cn.Close()
$dt | Export-Csv '<專案>\export\cis_parts.csv' -NoTypeInformation -Encoding UTF8
```

整段只有一個 `SELECT`，沒有 `INSERT`/`UPDATE`/`DELETE`/`ALTER`/`DROP`，
`SqlDataAdapter` 只呼叫 `Fill()`（讀）沒有 `Update()`（寫）。

路徑用 `ndd.json` 的 `cis_snapshot` 指定（預設 `export/cis_parts.csv`）。
**檔案不在就整套降級成只用前綴**，不報錯——快照是**選用加速器，不是前提**，
與 `models.json` 的定位一致。快照的 SHA-256 要進 `MANIFEST.md`：它會改變結論。

## CIS 大類 -> 分類

| 代碼 | CIS 名稱 | 分類 | 要查 datasheet |
|---|---|---|---|
| `1E09` `1EA0` `3EA0` | Integrated Circuits / IC (TFT) | `ic` | ✅ |
| `1E15` | RF, IF and RFID | `rf` | ✅ |
| `1E06` | Discrete Semiconductor | `semiconductor_discrete` | ✅ |
| `1E05` | Crystals, Oscillators | `crystal` | ✅ |
| `1E07` | Filters | `filter` | ✅ |
| `1E16` | Sensors, Transducers | `sensor` | ✅ |
| `1E21` | Power Supplies - Board Mount | `power_module` | ✅ |
| `1E03` | Circuit Protection | `circuit_protection` | ✅ |
| `1E10` `1E11` `1E13` `1E18` | Isolators / Opto / Relays / Switches | 各自 | ✅ |
| `1E02` `1E14` `1E08` `1E12` | Capacitors / Resistors / Inductors / Pots | `passive` | ❌ |
| `1E04` | Connectors, Interconnects | `connector` | ❌ |
| `1E01` `3E01` `1E22` | Cable Assemblies / Wires | `cable` | ❌ |
| `1M` | ME（機構件） | `mechanical` | ❌ |
| `1E24` | Non Soldered Parts | `mechanical` | ❌ |
| `1E17` | Static Control, ESD | `mechanical` | ❌ |
| `1EA1` | PCB | `pcb` | ❌ |
| `1E20` | Test and Measurement | `instrument` | ❌ |
| `1E25` | Battery | `battery` | ❌ |
| `1E23` | DEV Board, Kit | `dev_board` | ❌ |
| `1E99` | Uncategorized | 未辨識 | 等人確認 |

⚠️ **`1E99_Uncategorized` 不等於「缺分類」。** 實測 183 顆裡有 182 顆是
`OPEN_0402`——那是**刻意保留的未貼位置**，是設計事實。工具用料號樣式
（`OPEN`/`NC`/`DNP`/`DNI`/`NOSTUFF` 開頭）把它們獨立歸成 `placeholder`，
不混進「需你確認」害人去查根本不存在的規格書。剩下真的沒分好的才進清單。

## refdes 前綴後援表

**只收真正跨公司通用的**：

`U`=IC．`Q`/`D`=分立半導體．`R`/`C`/`L`/`FB`=被動．`X`/`Y`=晶振．
`J`=連接器．`TP`=測試點．`H`/`MH`=鎖孔．`FM`=基準點．`F`=保護元件．
`S`=開關．`K`=電驛．`T`=變壓器．`W`=線材

**刻意不收**（這些是某家公司/某塊板自己的標籤，寫死就是遷就單一專案）：
`ME`、`FUSE`、`OPEN`、`Coupler`、`Cal`、`Main`、`GPIO`、`DAC`、`UC`、`AT`、
`CS`、`PO`、`CA`、`EXTOUT`、`A`、`M`。

`P` 也不收——跟「電位計」撞名。P 開頭的連接器靠 footprint 開頭是 `conn`
兜底，不會漏。

## 未辨識的怎麼收斂

`coverage` 最後會列出「分類未辨識，需你確認」。在 `ndd.json` 補：

```json
"part_class": {
  "OPEN": "placeholder",
  "ME": "mechanical",
  "b0017_dpu_v1:A12": "mechanical"
}
```

key 可以是 `<board>:<refdes>`（單一實例）、料號、或裸前綴（整批適用）。
**惰性例外表，隨用隨長，不是必填清單**——跟 `part_package`、`endpoints`
同一個慣例。

分不出來的分類，`signal_relevant()` 一律當「可能重要」：漏查一顆 IC 比多列
一顆機構件貴得多。
