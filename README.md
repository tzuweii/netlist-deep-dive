# netlist-deep-dive

> A Claude Code skill for deep PCB circuit analysis from PADS 2000 ASCII netlists (`.asc`) and PCBA BOMs (`.xlsx`).
> Produces queryable pin-map CSVs, cross-board end-to-end signal chains, an auditable architecture document, and a human-review checklist.

給 Claude Code 用的 skill：輸入 **`.asc` netlist + BOM**，產出可查詢的 pinmap CSV、
跨板端到端訊號鏈、架構文件、自動稽核報告與人工複驗清單。跨專案通用。

---

## 為什麼需要它

netlist 是逐 net 的文字檔，人工追一條跨板訊號要翻很多層；而分析結果寫成文件之後，
沒有任何機制能告訴你「文件哪一段已經和 netlist 對不上了」。

這個 skill 把分析流程工具化，並且**把驗證本身也工具化**——包含驗證解析器有沒有漏讀、
腳位模型是不是真的翻過 datasheet、連接器對接的推論到底有多強。

## 安裝

```bash
git clone <this repo> .claude/skills/netlist-deep-dive
```

需求：Python 3.8+、`openpyxl`、`curl`（下載 datasheet 用，非必要）。

## 使用

```bash
cd .claude/skills/netlist-deep-dive/scripts

# 掃描分析資料夾裡的 .asc / .xlsx，用 refdes 交集配對，產生 ndd.json 骨架
PYTHONIOENCODING=utf-8 python ndd.py init "C:/path/to/analysis"

# 人工補完 ndd.json（連接器對接、BOM 類型…），然後：
python ndd.py export        # pinmap_<board>.csv：逐腳事實表
python ndd.py audit         # 一致性稽核（含 parser 自我驗證）
python ndd.py mate          # 連接器對接：枚舉所有對應方式並排名
python ndd.py trace         # 跨板端到端訊號鏈 CSV
python ndd.py pinfn LMX2594 8   # 查腳位功能：抽 datasheet 原文並快取
python ndd.py datasheets    # 盤點/下載 datasheet，產出 MISSING.md
python ndd.py review        # 產出人工複驗清單 REVIEW.md
```

設定檔範例與逐欄說明見 `references/example-ndd.json`。

## 兩種使用方式

**日常問答（不必呼叫 skill）** —— 把「三源對照規約」抄進該專案的 `CLAUDE.md`，
它每個 session 自動載入，所以每次回答電路問題都會遵守，不需要手動觸發。

**建立新專案 / 完整分析（呼叫 skill）** —— `init` 建設定、`audit` / `mate` /
`trace` 跑驗證、`review` 產出人工複驗清單。換專案時走一次 Phase 0–6。

## 設計原則

1. **BOM 是料號的權威** —— 符號名稱與設計文件都可能落後於換料。
2. **腳位模型一定要翻過 datasheet** —— 沒有 `verified_against` 的模型會被**拒絕載入**。
   這是唯一會產生「整張表都錯但看起來完全合理」的地方。
3. **通則一定要展開逐顆比對** —— 文件寫 `U<n>03`，而通則幾乎一定有例外。
4. **跨板對接要枚舉排名** —— 「兩側樣式相符」不是證據：對稱的 GND 分布會讓數十種
   錯誤對應全部通過。
5. **netlist 描述設計，不描述你手上那片板** —— rework、飛線、換料都不在裡面。
6. **不確定就標記，不要補完。**

> netlist 證明「接線意圖」，layout 證明「實體位置」，只有系統行為能證明「兩者都對」。
> 三者不能互相取代。

## 內容

| 檔案 | 說明 |
|---|---|
| `SKILL.md` | Phase 0–6 工作流、核心原則、硬性規則 |
| `references/pitfalls.md` | 12 個實際踩過的坑，每個都會產生「看起來合理但是錯的」結論 |
| `references/verification.md` | 三層驗證方法，以及四類**結構上驗不到**的東西 |
| `references/datasheets.md` | datasheet 取得的實測限制與查證要點 |
| `references/example-ndd.json` | 去識別化的設定範例，逐欄註解 |
| `scripts/ndd.py` | CLI 進入點 |
| `scripts/ndd_pads.py` | netlist 解析 + 獨立邏輯的自我驗證 |
| `scripts/ndd_bom.py` | BOM 解析，自動偵測標題列位置 |
| `scripts/ndd_pinfn.py` | 抽 datasheet 腳位原文並快取（**快取原文，不快取解讀**） |
| `scripts/ndd_models.py` | 已查證的腳位模型庫（含強制檢查） |
| `scripts/ndd_graph.py` | 連接器對接排名 + 跨板追跡 |
| `scripts/ndd_audit.py` | 宣告式斷言引擎 |

內建 4 個逐頁查證過的腳位模型：`SN74CBTLV3126`、`SN74HCS244`、`PI49FCT3807`、
`PCA9547`。其他型號請自行查證後加入專案的 `models.json`——**不要照抄相近型號**。

## 限制

- 目前只支援 **PADS 2000 ASCII** 格式的 netlist（OrCAD Capture 經 `orpads2k64.dll` 匯出）。
- datasheet 自動下載只對少數原廠站有效（實測 TI、NXP 可；Microchip 403；
  代理商站回 bot-check HTML 而非 PDF）。其餘需人工補上。
- 工具**驗不到**：因果推論、實體板狀態（rework）、layout 決定的量（阻抗／耦合／
  footprint 方位）、線束。這些一律列進 `REVIEW.md` 交由人工確認。

## 授權

MIT
