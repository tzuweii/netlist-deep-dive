# 取得 datasheet

**按需補齊，不是全部補齊。** 實測一個 4 板專案：104 種主動料號，但實際出現在
訊號鏈終點的只有 4 種——其餘 100 種的 datasheet 對那條鏈路毫無影響。

真正該預先補的只有兩類：**要建模的穿越件**（`ndd.py blockers` 會排出優先序）、
以及**會被問到腳位的終點**。其餘等問到再補，`pinfn` 找不到會明說 `[D 缺]` 並停
在具名位置——那是有用的答案，由工程師判斷值不值得去找。

這裡記錄實測可行的做法與限制。

---

## 流程

```bash
# 1. 盤點：列出 BOM 裡所有主動元件，看哪些已經有、哪些缺，並嘗試自動下載
PYTHONIOENCODING=utf-8 python scripts/ndd.py datasheets

# 2. 對 MISSING.md 裡的料號，用 WebSearch 找官方 datasheet 網址，再指定下載
python scripts/ndd.py datasheets --pn "PAC1954T-E/4MX" --url "https://..."
```

`datasheets.dir` 可以指向專案既有的 datasheet 資料夾，這樣已經有的就不會重抓。

⚠️ `init --run` / `migrate --run` **預設不下載**（要下載加 `--datasheets`）——
升級或建專案的指令不該無預警發網路請求。缺的一律列在 `MISSING.md`。

## 查腳位：抽到多筆時工具不替你挑

```
!! pin 14 抽到 2 筆（這份 datasheet 可能涵蓋多種封裝）。拒絕替你挑，也不寫快取。
   [1] p.4  SCL   腳號欄位 14,12   serial clock line
   [2] p.4  VDD   腳號欄位 16,14   supply voltage
   -> --pick <n> [--package <標籤>]
```

**抽到一筆才是已證明無歧義**，才會自動寫進快取。多筆時腳號欄位直接攤在眼前，
對照 `audit` 的封裝判定就能選定。

⚠️ **工具不解析 datasheet 的腳位表。** 每家排版都不同（腳註標記、跨行儲存格、
文字層把兩個腳號併成一個），通用地「看懂」是無底洞。封裝改由 netlist + BOM 的
證據排名判定（見 `models.md`）。

---

## 自動下載的實際限制（2026-08 實測）

| 來源 | 結果 |
|---|---|
| TI `ti.com/lit/ds/symlink/<slug>.pdf` | ✅ 可 |
| NXP `nxp.com/docs/en/data-sheet/<PN>.pdf` | ✅ 可（注意大小寫，`PCA9547.pdf` 而非 `pca9547.pdf`） |
| Microchip | ❌ HTTP 403，擋 curl，換 User-Agent 也沒用 |
| 代理商站（Mouser / Digikey） | ❌ 回 HTTP 200 但內容是 bot-check HTML，不是 PDF |

### ⚠️ HTTP 200 不代表拿到 PDF

擋機器人的站會回一頁 HTML 而不是檔案。`_fetch()` 會驗 `%PDF-` 魔術位元且
檔案 > 20 KB，不符就刪除並回報失敗。**不要拿掉這個檢查**——存下一堆 HTML
偽裝的「datasheet」，之後查腳位時才會發現，那時已經寫了一堆錯的模型。

---

## 抓不到是正常的

以下本來就不會有公開 datasheet，**直接列進 `MISSING.md` 請使用者提供**：

- 自製 IC / ASIC
- 自製被動元件（in-house divider、combiner）
- 連接器與機構件（多半要去原廠型錄頁，不是單一 PDF）
- 客製模組、裸板品項（`PCB1` 這類 BOM 行）

`MISSING.md` 會寫明：檔案放進 datasheet 目錄、檔名建議用料號小寫、
補齊後才能建立對應的腳位模型。

---

## 查證腳位時要注意

1. **翻 Pin Configuration / Pin Functions 那一頁**，不要看 Block Diagram 就下結論。
2. **確認封裝**。同一份 datasheet 常含多種封裝，腳位表會分欄
   （例如 PCA9547 的 SO24/TSSOP24 與 HVQFN24 腳號完全不同）。
   先從 netlist 的腳位數與 BOM 的料號後綴判斷是哪一種封裝。
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
