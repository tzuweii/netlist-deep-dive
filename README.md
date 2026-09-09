# netlist-deep-dive

> A Claude Code skill for deep PCB circuit analysis from PADS 2000 ASCII netlists (`.asc`) and PCBA BOMs (`.xlsx`).
> Produces queryable pin-map CSVs, cross-board end-to-end signal chains, a separate functional-topology hint output, an auditable architecture document, and a human-review checklist.

給 Claude Code 用的 skill：輸入 **`.asc` netlist + BOM**，產出可查詢的 pinmap CSV、
跨板端到端訊號鏈、功能拓樸提示、架構文件、自動稽核報告與人工複驗清單。跨專案通用。

---

## 為什麼需要它

netlist 是逐 net 的文字檔，人工追一條跨板訊號要翻很多層；而分析結果寫成文件之後，
沒有任何機制能告訴你「文件哪一段已經和 netlist 對不上了」。

這個 skill 把分析流程工具化，並且**把驗證本身也工具化**——包含驗證解析器有沒有漏讀、
元件模型是不是真的翻過 datasheet、連接器對接的推論到底有多強。

核心設計原則是一句話：**能空過的檢查比沒有檢查更糟**，因為它會亮綠燈。

## 安裝

```bash
git clone <this repo> .claude/skills/netlist-deep-dive
```

需求：Python 3.8+、`openpyxl`、`pypdf`、`curl`（下載 datasheet 用，非必要）。

```bash
python -m unittest discover -s tests     # 回歸測試
```

## 使用

```bash
cd .claude/skills/netlist-deep-dive/scripts

PYTHONIOENCODING=utf-8 python ndd.py init "C:/path/to/analysis"

# 人工補完 ndd.json（bom_scope、mates、endpoints…），然後：
python ndd.py export        # pinmap_<board>.csv：逐腳事實表
python ndd.py audit         # 一致性稽核（含 parser 自我驗證、模型檢查）
python ndd.py coverage      # per-MPN 三源覆蓋，看缺口在哪
python ndd.py mate          # 連接器對接：枚舉所有對應方式並排名
python ndd.py trace         # signal_chain.csv + topology_hint.csv
python ndd.py pinfn --board a --refdes U939 15   # 由工具鎖定三源查腳位
python ndd.py datasheets    # 盤點/下載 datasheet
python ndd.py manifest      # 輸入檔完整 SHA-256 + 工具版本
python ndd.py blockers      # 訊號鏈停在哪些料號上（建模投報率）
python ndd.py review        # 產出人工複驗清單 REVIEW.md
```

**從 v0 升級**：`python ndd.py migrate "C:/path/to/analysis" --run` —— 一個指令
升級設定、還原原本內建的模型、重跑所有流程，並產出升級報告。

設定檔逐欄說明見 `references/example-ndd.json`。

## 兩種使用方式

**日常問答（不必呼叫 skill）** —— 把「三源對照規約」抄進該專案的 `CLAUDE.md`，
它每個 session 自動載入。

**建立新專案 / 完整分析（呼叫 skill）** —— 走一次 Phase 0–6。

## 設計原則

1. **能空過的檢查比沒有檢查更糟** —— 每個檢查都要問：它的 PASS 可不可能是空的？
2. **傳輸與功能影響分開** —— 「能控制它」不等於「訊號穿過它」。控制關係進
   `topology_hint.csv`，永不得成為 BFS 的下一跳。
3. **方向必須顯式** —— 對稱的導通模型會讓單向元件逆走，還會在 fanout 的兩個
   輸出之間**憑空捏造**一條不存在的路徑。
4. **`always` 要付證明，`conditional` 是預設** —— 反過來就會靜默升級確定性。
5. **必填放在它免費的地方，惰性放在它昂貴的地方** —— 建模時人已在讀 datasheet，
   順手記下 VSS/VDD 是哪幾支腳成本為零；要求為全專案兩百顆 IC 預先填封裝則是
   純浪費。
6. **推論要標記** —— 由 netlist/BOM 推出的封裝一律帶 `[?]`，證據不足就列入待補，
   **不替使用者假設**。
7. **包含關係優於基數** —— netlist 只有已接腳，任何依賴「數量相等」的判定都會
   系統性偏移。
8. **降級標記優於硬拒絕** —— 但標記必須沿路徑傳遞到最終輸出，否則等於沒標。
9. **原始來源是資產，結論是拋棄式的。**

> netlist 證明「接線意圖」，layout 證明「實體位置」，只有系統行為能證明「兩者都對」。
> 三者不能互相取代。

## 內容

| 檔案 | 說明 |
|---|---|
| `CHANGELOG.md` | 版本紀錄與 v0 對比 |
| `SPEC.md` | 行為契約與施工規格 |
| `SKILL.md` | Phase 0–6 工作流、三源對照規約、硬性規則 |
| `references/models.md` | transfer／control／endpoint 的分界與 schema |
| `references/pitfalls.md` | 實際踩過的坑，每個都會產生「看起來合理但是錯的」結論 |
| `references/verification.md` | 三層驗證方法，以及**結構上驗不到**的四類 |
| `references/datasheets.md` | datasheet 取得的實測限制 |
| `references/example-ndd.json` | 去識別化的設定範例，逐欄註解 |
| `references/example-models.json` | 可複製的元件模型範例（**不自動載入**） |
| `scripts/ndd.py` | CLI 進入點 |
| `scripts/ndd_pads.py` | netlist 解析 + 獨立邏輯的自我驗證 |
| `scripts/ndd_bom.py` | BOM 解析、ambiguity、`bom_scope` |
| `scripts/ndd_confidence.py` | confidence／gating 兩軸與 caveat 的唯一定義處 |
| `scripts/ndd_pinfn.py` | datasheet 原文抽取與快取（**不做封裝判定**） |
| `scripts/ndd_package.py` | 封裝判定：只用 netlist + BOM 的證據排名 |
| `scripts/ndd_models.py` | 有向 transfer 模型、control 推導 |
| `scripts/ndd_graph.py` | 對接排名 + 跨板追跡 + hint graph |
| `scripts/ndd_audit.py` | 宣告式斷言引擎 |
| `tests/` | 合成 fixture 回歸測試 |

**模型不會自動載入。** `references/example-models.json` 有四顆可直接複製的範例
（bus switch / buffer / clock fanout / I²C mux），用 `ndd.py models --add <名稱>`
放進專案。

手動複製是刻意的：模型是「某人對 datasheet 的解讀」，自動塞進每個專案等於讓你
在不知情下用別人的解讀去追訊號。複製之後它就是**你的宣告**，`audit` 與
`REVIEW.md` 會把它列進你要複核的清單。

## 限制

- 目前只支援 **PADS 2000 ASCII** 格式的 netlist。
- datasheet 自動下載只對少數原廠站有效；其餘需人工補上。
- **工具不解析 datasheet 的腳位表。** 每家排版都不同（腳註標記、跨行儲存格、
  文字層把兩個腳號併成一個），通用地「看懂」是無底洞，而且一列錯位就讓整張表
  作廢。封裝改由 netlist + BOM 的證據排名判定，推論結果一律標 `[?]`。
- `pinfn` 抽到多筆時會攤開全部原文與腳號欄位，**拒絕替你挑**；抽不到時明說
  「請人工開 PDF」，不猜。
- 工具**驗不到**：因果推論、實體板狀態（rework）、layout 決定的量（阻抗／耦合／
  footprint 方位）、線束、韌體 runtime 狀態。這些一律列進 `REVIEW.md`。

## 授權

MIT
