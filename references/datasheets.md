# 取得 datasheet

**按需補齊，不是全部補齊。** 實測一個 4 板專案：104 種主動料號，但實際出現在
訊號鏈終點的只有 4 種——其餘 100 種的 datasheet 對那條鏈路毫無影響。

真正該預先補的只有兩類：**要建模的穿越件**（`ndd.py blockers` 會排出優先序）、
以及**會被問到腳位的終點**。其餘等問到再補。`datasheets/INDEX.md` 標「缺」的，
標 `[D 缺]` 並停在具名位置——那是有用的答案，由工程師判斷值不值得去找。

這裡記錄實測可行的做法與限制。

---

## 流程

```bash
# 1. 盤點：需要規格書的料號各對到哪份檔案，寫成 datasheets/INDEX.md；缺的嘗試自動下載
python scripts/ndd.py datasheets

# 2. 對 INDEX.md 標「缺」的料號，用 WebSearch 找官方 datasheet 網址，再指定下載
python scripts/ndd.py datasheets --pn "PAC1954T-E/4MX" --url "https://..."
```

`datasheets.dir` 可以指向專案既有的 datasheet 資料夾，這樣已經有的就不會重抓。

## 查腳位：抽到多筆時，依 symbol 選欄

一份規格書涵蓋多種封裝時，每支腳都會抽到好幾筆（一欄一筆）。`.DSN` 的 netlist
腳號本來就來自那顆 symbol，所以用 symbol 的「腳號＋腳名」去比規格書各欄，全部
對得上的那一欄就是這塊板用的腳號：

```
OK 依 symbol 選封裝欄：選定p.4 腳位表第 2 欄（14 支腳與 symbol 吻合、0 支矛盾）；
   電源地佐證：第 14 腳 VDD 接電源
PCA9554B_PCA9554C.pdf p.4:  12  SCL    serial clock line  [symbol_selected]
```

用 symbol 選欄之後，「規格書腳名與 symbol 一致」就必然成立，失去獨立檢查。所以
還要過一道**不依賴 symbol** 的關：選出那一欄叫 VDD／VSS 的腳，netlist 上真的接
電源／地。要用 `--board <板> pinfn --refdes <零件>` 才有 netlist 可驗。

選不出時印出原因、攤開每筆，**不替你挑、不寫快取**：

| 原因 | 通常代表 |
|---|---|
| 腳名不符 | symbol 建錯，或規格書版本不對——T6，講給使用者 |
| 電源地接法不符 | 選出的欄與板子實際接法矛盾——同上 |
| 沒有可驗證的電源地腳／比對的腳太少 | 證據不足 |
| 該欄沒有這支腳 | 散熱墊這類不在腳位表的腳，或規格書文字層把兩個腳號黏在一起（`8 6` 變成 `86`） |

**抽到一筆才是已證明無歧義**，會直接寫進快取。

⚠️ **工具不解析 datasheet 的腳位表。** 每家排版都不同（腳註標記、跨行儲存格、
文字層把兩個腳號併成一個），通用地「看懂」是無底洞。沒建模型的零件靠上面的
symbol 選欄；有建模型的，封裝由 netlist + BOM 的證據排名判定（見 `models.md`）。

---

## 自動下載的實際限制（2026-08 實測）

| 來源 | 結果 |
|---|---|
| TI `ti.com/lit/ds/symlink/<slug>.pdf` | ✅ 可 |
| NXP `nxp.com/docs/en/data-sheet/<PN>.pdf` | ✅ 可（注意大小寫，`PCA9547.pdf` 而非 `pca9547.pdf`） |
| Microchip | ❌ HTTP 403，擋 curl，換 User-Agent 也沒用 |
| 代理商站（Mouser / Digikey） | ❌ 回 HTTP 200 但內容是 bot-check HTML，不是 PDF |

---

## 抓不到是正常的

以下本來就不會有公開 datasheet，`INDEX.md` 會標「缺」，**請使用者提供**：

- 自製 IC / ASIC
- 自製被動元件（in-house divider、combiner）
- 客製模組、裸板品項（`PCB1` 這類 BOM 行）

連接器、被動、機構件、預留位不需要規格書，`INDEX.md` 不列。表尾會寫明：
檔案放進 datasheet 目錄、檔名建議用料號小寫、補齊後才能建立對應的腳位模型。

---

## 查證腳位時要注意

1. **翻 Pin Configuration / Pin Functions 那一頁**，不要看 Block Diagram 就下結論。
2. **確認封裝**。同一份 datasheet 常含多種封裝，腳位表會分欄
   （例如 PCA9547 的 SO24/TSSOP24 與 HVQFN24 腳號完全不同）。`pinfn` 會依
   symbol 選欄（見上）；自己翻表時也照同一個方法：symbol 的腳號＋腳名對哪一欄，
   再看該欄電源地腳在 netlist 上有沒有接對。**不可用 netlist 的已接腳數推定封裝**
   （見 `pitfalls.md` T2）。
3. **確認致能極性**。`OE` 是高態還是低態，datasheet 會寫 active HIGH / LOW。
   同一塊板上混用兩種很常見。
4. **不連續的腳號要照抄**，不要用等差級數推
   （`PI49FCT3807` 的輸出是 3,5,7,9,**11,12**,14,16,18,19）。
5. **family datasheet**：檔名只寫一個型號，內容可能涵蓋整個系列
   （`tmp101.pdf` 涵蓋 TMP100+TMP101；`tla2022.pdf` 涵蓋 TLA2021/2022/2024）。
   **不要因為檔名不符就判定沒有 datasheet，先打開看。**
6. 把「檔名 + 頁碼 + 文件編號」寫進 `verified_against`，例如
   `PCA9547.pdf p.5 Table 3 HVQFN24 欄（NXP Rev.4）`。
   只寫「datasheet」等於沒寫，之後沒人能複驗。

---

## 要標 `[D 缺]` 之前

`INDEX.md`、`coverage`／`blockers` 的 DS 欄、`pinfn` 找檔用的是**同一套比對**：
檔名含完整料號 → 檔名就是料號主體 → 檔名含完整型號（系列規格書）→ PDF 前 3 頁
內文（會剝除常見訂購碼後綴，`ADG919BCPZ` 也試 `ADG919`）。檔名**至少要含完整型號**
（料號去掉訂購碼，`PCA9547BS,118` → `PCA9547`）才算，只共用前幾碼的不算——實測
`PCA9547`（I²C mux）曾因共用 `PCA95` 被配到 `PCA9554`（GPIO 擴充器）的規格書。

| `INDEX.md` 說 | 怎麼用 |
|---|---|
| **已確認** | 直接用（仍要 `pinfn` 開檔對名才知道腳位功能） |
| **待確認** | 多半是系列規格書（`PCA9554B_PCA9554C.pdf` 涵蓋 PCA9554BBSHP）。開檔確認型號在內，再在 `ndd.json` 的 `datasheets.files` 記一行 |
| **缺** | 可以寫 `[D 缺]`，除了下面這種會漏 |

唯一**不會警告**的漏網：型號只出現在規格書後段（例如最後的訂購資訊頁）的系列
規格書——內文搜尋只掃前 3 頁。掃描檔、沒裝 `pypdf` 這兩種，`INDEX.md` 會在表頭
警告。

真正的保險在 `pinfn` 的**自動對名**：抽出來的腳位名對不上 `.DSN` symbol（或
symbol 對不上任何一欄）就當場攤開、不寫快取——就算比對到相近型號的規格書，也會
在這一步被抓到。
