#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""從本地 datasheet PDF 抽出**某支腳的原文**，並快取原文與出處。

════════════════════════════════════════════════════════════════════════════
**快取原文，永不快取解讀。**
  快取 datasheet 的逐字內容 + 出處（檔名／頁碼／SHA-256）。不是「pin1 與
  pin3 內部連通」這種推出來的結論。原文快取 5 秒就能核對；推論快取會把錯誤
  凍結成永久資產，而且沒人看得出來。

**本模組不做封裝判定。**
  每家 datasheet 的腳位表排版都不同（腳註標記、跨行儲存格、文字層把兩個腳號
  併成一個），要通用地「看懂」表格是無底洞，而且一列錯位就讓整張表作廢。
  封裝改由 `ndd_package` 依 **netlist + BOM** 的證據判定。

  這裡只做三件事：
    抽到一筆 -> 直接給答案（**已證明**無歧義），寫進快取
    抽到多筆 -> 用 `.DSN` symbol 的「腳號＋腳名」選出是規格書哪一欄，再以
                netlist 電源地接法獨立佐證；兩道都過才寫進快取
    仍選不出 -> 攤開全部原文與頁碼，**拒絕替使用者挑**，標 [?] 等指定
════════════════════════════════════════════════════════════════════════════
"""
import csv
import hashlib
import io
import os
import re
import time

CACHE = "verified-pins.csv"
COLS = ["part", "pin", "pin_name", "direction", "text",
        "source_file", "page", "sha256", "package", "resolved_by",
        "corroborated_by", "active_low", "verified_on", "board"]
# `board` 只有 symbol 列會填：同一料號在不同板上的 symbol 腳位名**可能不同**
# （各 .DSN 各存一份 symbol，實測 SN74CBTLV3126 在 interposer 叫 OE1、在
# fecu_fm 叫 SEL1），所以 symbol 列依板分開記錄，不跨板合併。
LEGACY_COLS = ["part", "pin", "pin_name", "direction", "text",
               "source_file", "page", "sha256", "verified_on"]

# resolved_by 的合法值，記錄這一筆原文是怎麼定案的。**不從 datasheet 表格
# 推導封裝**——多筆時靠 symbol 選欄 + netlist 佐證，或由人指定。
NOT_APPLICABLE = "not_applicable"        # 只抽到一筆，**已證明**無歧義
USER_CONFIRMED = "user_confirmed"        # 多筆，使用者用 --pick 指定
UNRESOLVED = "unresolved_pending_user"   # 多筆且未指定 -> [?]，拒絕寫快取
SYMBOL = "capture_symbol"                # 由 .DSN 的 Capture symbol 帶出的腳位名
SYMBOL_SELECTED = "symbol_selected"      # 多筆，依 symbol 選欄 + netlist 電源地佐證
RESOLVED_OK = (NOT_APPLICABLE, USER_CONFIRMED, SYMBOL_SELECTED)

# ⚠️ **symbol 列不算 datasheet 列。**
#    symbol 的腳位名是照 datasheet 建的，所以名字可信；但它**沒有原文、沒有
#    頁碼**，回答不了「這支腳是做什麼的」。所以 `SYMBOL` 不放進 `RESOLVED_OK`
#    ——有 symbol 列不會讓 datasheet 抽取被跳過，兩者是互相佐證的關係，不是
#    互相取代。真正的價值在於：名字對得起來 = 兩個獨立來源說同一件事；對不
#    起來 = 封裝選錯或 symbol 建錯，必須當場攤開給人看。

# ⚠️ 腳位表至少有三種排版，只認一種會**靜靜漏掉正確的列**——那比抽不到更危險。
PIN_FIRST_RX = re.compile(
    r"^\s*(?P<pins>\d{1,3}(?:\s*,\s*\d{1,3})*)\s+"
    r"(?P<name>[A-Za-z_][\w/*+().\-]*(?:\s*,\s*[A-Za-z_][\w/*+().\-]*)*)\s+"
    r"(?P<rest>\S.*)$")
_NM = r"(?=[\w/*+().\-]*[A-Za-z])[\w/*+().\-]{1,16}"
NAME_FIRST_RX = re.compile(
    r"^\s*(?P<name>" + _NM + r"(?:\s*,\s*" + _NM + r")*)\s+"
    r"(?P<pins>\d{1,3}(?:[\s,]+\d{1,3}){0,3})\s+"
    r"(?P<rest>\S.*)$")
DIR_RX = re.compile(r"^(I/O|I|O|P|Input|Output|Supply|Ground|Power|GND|In|Out)\b",
                    re.I)
NOISE_RX = re.compile(r"\.{4,}|Submit Document|Copyright|www\.|Product Folder"
                      r"|^\s*\d+\s*$|Feedback")
# 腳註標記：`P1[1] 5 3 Port ...` —— 抽逐腳原文時先剝掉，否則整列比不到。
FOOTNOTE_RX = re.compile(r"\[\d+\]")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()                 # ⚠️ 完整值，不再截前綴


# ------------------------------------------------------------ datasheet --
# `locate()` 的比對方式。只有前三種算「已確認」，其餘都要人看過一眼。
VIA_USER = "人工指定"
VIA_EXACT = "檔名含完整料號"
VIA_BASE = "檔名就是料號主體"         # pca9547.pdf 對 PCA9547PW,118
VIA_FAMILY = "檔名含型號 %s（系列規格書）"
VIA_TEXT = "PDF 前 3 頁內文含型號"
CONFIRMED_VIA = (VIA_USER, VIA_EXACT, VIA_BASE)


def model_of(part):
    """料號去掉訂購碼之後的型號：到最後一個數字為止。

    `PCA9547BS,118` -> pca9547、`AD5667RBCPZ-R2` -> ad5667、
    `XC7Z100-1FFG900I` -> xc7z100、`74LVC2G34GV,125` -> 74lvc2g34。
    沒有數字的料號整個都是型號。
    """
    s = re.split(r"[,/ ]", part)[0].split("-")[0]
    k = re.sub(r"[^A-Za-z0-9]", "", s).lower()
    m = re.match(r"^(.*\d)", k)
    return m.group(1) if m else k


def locate(ddir, part, explicit=None):
    """料號 -> 規格書。回傳 (路徑或 None, 比對方式)。

    **唯一**的實作：`pinfn`、`datasheets`（INDEX.md）、`coverage`、`blockers`
    都走這裡。曾經三處各用各的比對（完整料號／前 6 碼／三層後援），同一顆料
    在 MISSING.md 說缺、在 coverage 說有、`pinfn` 又確實找得到。

    ⚠️ family datasheet 很常見（檔名只寫一個型號、內容涵蓋整個系列），
    只比檔名一定漏。漏掉會讓人誤標 [D 缺] 並重複採購已持有的規格書。
    """
    if explicit:
        p = os.path.join(ddir, explicit)
        return (p, VIA_USER) if os.path.exists(p) else (None, "人工指定的檔案不存在")
    if not os.path.isdir(ddir):
        return None, "規格書資料夾不存在"
    key = re.sub(r"[^A-Za-z0-9]", "", part.split(",")[0]).lower()
    # ⚠️ 檔名至少要含**完整型號**才算。只比前幾碼會把 PCA9547（I²C mux）配到
    #    PCA9554B_PCA9554C.pdf（GPIO 擴充器）——兩顆只共用 `PCA95`。
    floor = max(len(model_of(part)), 5)
    best, blen, via = None, 0, ""
    for f in sorted(os.listdir(ddir)):
        if not f.lower().endswith(".pdf"):
            continue
        stem = re.sub(r"[^a-z0-9]", "", os.path.splitext(f)[0].lower())
        for n in range(len(key), floor - 1, -1):
            if key[:n] in stem and n > blen:
                best, blen = os.path.join(ddir, f), n
                via = (VIA_EXACT if n == len(key) else
                       VIA_BASE if key[:n] == stem else
                       VIA_FAMILY % key[:n].upper())
                break
    if best is None:
        base = re.sub(r"[^A-Za-z0-9]", "", part.split(",")[0]).upper()
        for cand in _search_in_text(ddir, base):
            best, via = cand, VIA_TEXT
            break
    if best is None:
        via = ("缺（未安裝 pypdf，沒有做內文搜尋）" if not text_search_enabled()
               else "缺")
    return best, via


def find_datasheet(ddir, part, explicit=None):
    best, via = locate(ddir, part, explicit)
    if best and via not in CONFIRMED_VIA:
        print("   （以「%s」比對到 %s——請確認是同一份規格書）"
              % (via, os.path.basename(best)))
    return best


def text_search_enabled():
    try:
        import pypdf  # noqa: F401
        return True
    except ImportError:
        return False


def textless_pdfs(ddir):
    """前 3 頁抽不出文字的 PDF（多半是掃描檔）——內文搜尋對它們無效。"""
    return sorted(os.path.basename(p) for p, t in _build_index(ddir).items()
                  if not t)


INDEX_NAME = "INDEX.md"


def index_stale(ddir):
    """INDEX.md 比規格書資料夾舊（之後有放進或移除檔案）就回傳 True。"""
    ip = os.path.join(ddir, INDEX_NAME)
    if not os.path.isfile(ip):
        return False
    newest = os.path.getmtime(ddir)
    for f in os.listdir(ddir):
        if f.lower().endswith(".pdf"):
            newest = max(newest, os.path.getmtime(os.path.join(ddir, f)))
    return newest > os.path.getmtime(ip) + 1


_TEXT_INDEX = {}


def _build_index(ddir, pages=3):
    """**只建一次** —— 否則每個缺料號都重掃全部 PDF，會跑到逾時。"""
    if ddir in _TEXT_INDEX:
        return _TEXT_INDEX[ddir]
    try:
        from pypdf import PdfReader
    except ImportError:
        _TEXT_INDEX[ddir] = {}
        return {}
    idx = {}
    for f in sorted(os.listdir(ddir)):
        if not f.lower().endswith(".pdf"):
            continue
        p = os.path.join(ddir, f)
        try:
            r = PdfReader(p)
            txt = "".join((pg.extract_text() or "") for pg in r.pages[:pages])
        except Exception:
            txt = ""
        idx[p] = re.sub(r"[^A-Za-z0-9]", "", txt).upper()
    _TEXT_INDEX[ddir] = idx
    return idx


def _search_in_text(ddir, needle):
    keys = [needle]
    trimmed = re.sub(r"(BCPZ|ACPZ|IDGKR|RGT|RGR|DGVR|PWR|TR\d*|R\d|Z)$", "", needle)
    if trimmed != needle and len(trimmed) >= 5:
        keys.append(trimmed)
    idx = _build_index(ddir)
    for k in keys:
        if not k or len(k) < 5:
            continue
        for path, flat in idx.items():
            if k in flat:
                yield path
                return


def _pages(path, max_pages):
    try:
        from pypdf import PdfReader
    except ImportError:                          # pragma: no cover
        try:
            from PyPDF2 import PdfReader
        except ImportError:
            raise RuntimeError("需要 pypdf：pip install pypdf")
    reader = PdfReader(path)
    for i, page in enumerate(reader.pages[:max_pages], start=1):
        yield i, (page.extract_text() or "")


# ------------------------------------------------------------ 逐腳原文 --
_PARSED = {}


def _parsed_lines(path, max_pages):
    """每一行所有可能的腳位列解析 -> [(page, [(kind, names, pins, dir, desc)])]。

    同一份 PDF 只解析一次：依 symbol 選欄時要對整顆零件的每支腳各查一次。
    """
    try:
        st = os.stat(path)
        memo_key = (path, max_pages, st.st_mtime, st.st_size)
    except OSError:
        memo_key = None
    if memo_key in _PARSED:
        return _PARSED[memo_key]
    out = []
    for i, text in _pages(path, max_pages):
        if not text.strip():
            continue
        for raw in text.splitlines():
            if NOISE_RX.search(raw):
                continue
            line = FOOTNOTE_RX.sub(" ", raw)   # `P1[1] 5 3 ...` 不剝就整列比不到
            parses = []
            for rx, kind in ((PIN_FIRST_RX, "A"), (NAME_FIRST_RX, "B/C")):
                m = rx.match(line)
                if not m:
                    continue
                pins = [p.strip() for p in re.split(r"[\s,]+", m.group("pins")) if p]
                names = [n.strip() for n in m.group("name").split(",")]
                rest = m.group("rest").strip()
                dm = DIR_RX.match(rest)
                direction = dm.group(0) if dm else ""
                desc = (rest[len(direction):].strip() if direction else rest)
                if len(desc) < 3:
                    continue
                parses.append((kind, names, pins, direction, desc))
            if parses:
                out.append((i, parses))
    if memo_key is not None:
        _PARSED[memo_key] = out
    return out


def extract(path, pin, max_pages=20):
    """回傳 [(page, pin_name, direction, text, kind, pins), ...]。"""
    pin = str(pin).strip()
    hits = []
    seen = set()
    for i, parses in _parsed_lines(path, max_pages):
        for kind, names, pins, direction, desc in parses:
            if pin not in pins:
                continue
            if kind == "A" and len(names) == len(pins):
                name = names[pins.index(pin)]
            else:
                name = names[0]
            key = (i, name, desc[:60])
            if key in seen:
                continue
            seen.add(key)
            hits.append((i, name, direction, desc[:240], kind, pins))
            break
    return hits


# ------------------------------------------------ 依 symbol 選封裝欄 --
# 規格書腳位名看起來是電源／地的，用來做**不依賴 symbol** 的獨立佐證。
_GND_NAME_RX = re.compile(r"^(VSS|GND|AGND|DGND|PGND|VEE|AVSS|DVSS)\w*$", re.I)
_PWR_NAME_RX = re.compile(r"^(VDD|VCC|AVDD|DVDD|VDDIO|VCCIO|VIO|VDDA|VDDD)\w*$", re.I)
MIN_SYMBOL_VOTES = 3


def select_by_symbol(path, pin, symbol, role_of):
    """規格書涵蓋多種封裝時，用 `.DSN` symbol 的「腳號＋腳名」選出是哪一欄。

    `symbol`: {腳號: 腳名}，同一料號的 symbol 列。
    `role_of`: 腳號 -> "GND"/"PWR"/"SIG"，netlist 上沒接回傳 None。

    回傳 (選中的那一筆 hit 或 None, 說明)。

    ⚠️ 用 symbol 選欄之後，「datasheet 腳名與 symbol 一致」就變成必然成立，
       失去獨立檢查的意義。所以選出的欄還要過一道**不依賴 symbol** 的關：
       規格書上叫 VDD／VSS 的腳，在 netlist 上真的接在電源／地。沒有任何一支
       電源地腳可驗證、或有任何一支矛盾，就不定案。
    """
    pin = str(pin)
    if not symbol:
        return None, "symbol 沒有這顆料號的腳位名"
    agree, disagree = {}, {}
    for num, sname in symbol.items():
        for pg, name, _d, _t, _k, pins in extract(path, num):
            grp = (pg, len(pins))
            for c, n in enumerate(pins):
                if n != num:
                    continue
                bucket = agree if norm_name(name) == norm_name(sname) else disagree
                bucket.setdefault(grp, {}).setdefault(c, set()).add(num)
    if not agree:
        return None, "symbol 的腳名在規格書腳位表裡一支都對不上"
    grp = max(agree, key=lambda g: max(len(v) for v in agree[g].values()))
    cols = agree[grp]
    ranked = sorted(cols, key=lambda c: -len(cols[c]))
    best = ranked[0]
    n_ok = len(cols[best])
    bad = sorted(disagree.get(grp, {}).get(best, set()), key=_pin_order)
    label = "p.%d 腳位表第 %d 欄" % (grp[0], best + 1)
    if n_ok < MIN_SYMBOL_VOTES:
        return None, "只有 %d 支腳能比對，不足以判定封裝" % n_ok
    if len(ranked) > 1 and len(cols[ranked[1]]) == n_ok:
        return None, "兩欄各有 %d 支腳與 symbol 相符，分不出是哪一欄" % n_ok
    if bad:
        return None, ("%s 有 %d 支腳與 symbol 相符，但 %s 腳名不符"
                      % (label, n_ok, "、".join(bad)))

    # 獨立佐證：這一欄的電源／地腳，netlist 上真的接電源／地
    support, contra, seen = [], [], set()
    for pg, parses in _parsed_lines(path, 20):
        if pg != grp[0]:
            continue
        for kind, names, pins, _d, _desc in parses:
            if len(pins) != grp[1]:
                continue
            num = pins[best]
            name = names[best] if kind == "A" and len(names) == len(pins) else names[0]
            want = ("GND" if _GND_NAME_RX.match(name) else
                    "PWR" if _PWR_NAME_RX.match(name) else None)
            if want is None or num in seen:
                break
            seen.add(num)
            got = role_of(num)
            desc = "第 %s 腳 %s" % (num, name)
            if got == want:
                support.append(desc + " 接" + ("地" if want == "GND" else "電源"))
            elif got is None:
                contra.append(desc + " netlist 上沒接")
            elif got in ("GND", "PWR"):
                contra.append(desc + " 卻接" + ("地" if got == "GND" else "電源"))
            break
    if contra:
        return None, ("symbol 指向%s，但電源地接法不符：%s"
                      % (label, "；".join(contra)))
    if not support:
        return None, "symbol 指向%s，但這一欄沒有可在 netlist 上驗證的電源地腳" % label

    mine = [h for h in extract(path, pin)
            if h[0] == grp[0] and len(h[5]) == grp[1] and h[5][best] == pin]
    if len(mine) != 1:
        return None, ("封裝已判定為%s，但該欄%s第 %s 腳"
                      % (label, "沒有" if not mine else "有多筆", pin))
    why = ("選定%s（%d 支腳與 symbol 吻合、0 支矛盾）；電源地佐證：%s"
           % (label, n_ok, "、".join(support)))
    return mine[0], why


def _pin_order(p):
    return (0, int(p)) if p.isdigit() else (1, p)


# ---------------------------------------------------------------- 快取 --
def load_cache(project_dir):
    """讀取快取並**遷移**舊列。

    ⚠️ 舊列缺 package／resolved_by，且 SHA 由 16 字元前綴改為完整值。舊列若被
       當成已解析讀入，等於把未鎖定 package 的資料洗成合法覆蓋——正是要防的事。
       一律標為 `unresolved_pending_user`，並註記 SHA 為前綴。
    """
    p = os.path.join(project_dir, CACHE)
    rows, migrated = [], 0
    if os.path.exists(p):
        with io.open(p, encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                if "resolved_by" not in r or r.get("resolved_by") is None:
                    r = dict(r)
                    r["package"] = ""
                    r["resolved_by"] = UNRESOLVED
                    r["corroborated_by"] = ""
                    migrated += 1
                rows.append(r)
    return p, rows, migrated


def append_cache(project_dir, row):
    p = os.path.join(project_dir, CACHE)
    new = not os.path.exists(p)
    if not new:
        with io.open(p, encoding="utf-8-sig", newline="") as fh:
            header = next(csv.reader(fh), [])
        if header != COLS:
            # ⚠️ 欄位增加過（例如 `board`）。直接附加會讓新列欄位錯位，
            #    先用新表頭整份重寫（沿用 replace 的暫存置換）。
            _p, old, _m = load_cache(project_dir)
            _rewrite(p, old)
    with io.open(p, "a", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        if new:
            w.writeheader()
        w.writerow({c: row.get(c, "") for c in COLS})


# ------------------------------------------------- Capture symbol 腳位名 --
def norm_name(n):
    """比對用的正規化：大小寫與分隔符（`/`、`_`、空白、反斜線、`#`）不算差異。

    ⚠️ **`+` 與 `-` 一定要留著。** `SENSE3+` 與 `SENSE3-` 是兩支不同的腳，
       一起正規化掉會讓差動對的兩隻腳看起來同名 —— 該報衝突的地方變成靜靜
       合併，而且合併後留下的是哪一個完全看順序。
    """
    return re.sub(r"[^A-Z0-9+\-]", "", (n or "").upper())


def symbol_names(hier, pn_of_refdes):
    """單一板：-> {(MPN, pin): (name, active_low, [refdes...])}，衝突另計。

    回傳 (對映, 衝突清單)。衝突 = 同一 MPN 的同一支腳，不同 refdes 的 symbol
    給出不同名字 —— 代表其中一顆用錯 symbol 或 BOM 標錯料號，**不可合併**。
    """
    names, low = hier.pin_names(), hier.active_low()
    bucket = {}
    for (rd, pin), nm in names.items():
        mpn = pn_of_refdes(rd)
        if not mpn:
            continue
        bucket.setdefault((mpn, pin), {}).setdefault(
            norm_name(nm), [nm, (rd, pin) in low, []])[2].append(rd)
    out, conflicts = {}, []
    for key, variants in bucket.items():
        if len(variants) > 1:
            conflicts.append((key[0], key[1],
                              {v[0]: sorted(v[2]) for v in variants.values()}))
            continue
        nm, al, rds = list(variants.values())[0]
        out[key] = (nm, al, sorted(rds))
    return out, conflicts


def replace_symbol_rows(project_dir, rows):
    """整批換掉快取裡的 symbol 列（不動 datasheet 列）。

    ⚠️ 用「先寫暫存再置換」——直接原地重寫時中途掛掉會把人工確認過的
       datasheet 列一起弄丟，那是無法從任何地方重建的資產。
    """
    p = os.path.join(project_dir, CACHE)
    _p, old, _m = load_cache(project_dir)
    keep = [r for r in old if r.get("resolved_by") != SYMBOL]
    _rewrite(p, keep + rows)
    return len(keep), len(rows)


def _rewrite(p, rows):
    tmp = p + ".tmp"
    with io.open(tmp, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") or "" for c in COLS})
    if os.path.exists(p):
        os.remove(p)
    os.rename(tmp, p)


def symbol_map(rows, part, board=None):
    """某料號的 symbol 腳位名 -> {腳號: 列}。

    指定 `board` 時只用那塊板的 symbol（舊版沒有 board 欄的列視為各板通用）。
    沒指定時，各板名字一致的腳才採用；**各板不一致的腳一律不採用**——不知道
    問的是哪塊板，就不能替你挑一個。
    """
    mine = [r for r in rows if r["part"].upper() == part.upper()
            and r.get("resolved_by") == SYMBOL]
    if board:
        own = [r for r in mine if r.get("board") == board]
        legacy = [r for r in mine if not r.get("board")]
        out = {r["pin"]: r for r in legacy}
        out.update({r["pin"]: r for r in own})
        return out
    by_pin = {}
    for r in mine:
        by_pin.setdefault(r["pin"], []).append(r)
    return {pin: rs[0] for pin, rs in by_pin.items()
            if len({norm_name(r["pin_name"]) for r in rs}) == 1}


def import_symbols(project_dir, boards, verbose=True):
    """把各板 `.DSN` 的 symbol 腳位名寫進快取。

    `boards`: [(board_key, hier, pn_of_refdes, nodes_csv_path), ...]

    **依板分開記錄，不跨板合併。** 各 .DSN 各存一份 symbol，同一料號在兩塊板
    上腳位名不同是正常的（命名習慣不同），不是錯誤。只有**同一塊板**裡同料號
    兩顆給出不同名字才算衝突——那代表同一張圖混用了兩版 symbol 或 BOM 標錯料號。
    """
    conflicts, rows, today = [], [], time.strftime("%Y-%m-%d")
    for key, hier, pn_of, nodes_csv in boards:
        if hier is None:
            continue
        got, conf = symbol_names(hier, pn_of)
        conflicts.extend((key, c[0], c[1],
                          {nm: ["%s:%s" % (key, r) for r in rds]
                           for nm, rds in c[2].items()}) for c in conf)
        src = os.path.basename(nodes_csv) if nodes_csv else ""
        sha = sha256(nodes_csv) if nodes_csv and os.path.exists(nodes_csv) else ""
        for (mpn, pin), (nm, al, rds) in sorted(got.items()):
            rows.append({"part": mpn, "pin": pin, "pin_name": nm, "direction": "",
                         "text": "", "source_file": src, "page": "",
                         "sha256": sha, "package": "", "resolved_by": SYMBOL,
                         "corroborated_by": " ".join(
                             "%s:%s" % (key, r) for r in rds[:4]),
                         "active_low": "1" if al else "",
                         "verified_on": today, "board": key})
    kept, wrote = replace_symbol_rows(project_dir, rows)
    if verbose:
        print("  symbol 腳位名：寫入 %d 筆（保留 %d 筆 datasheet 原文列）"
              % (wrote, kept))
        n_low = sum(1 for r in rows if r["active_low"])
        if n_low:
            print("  其中 %d 支為低有效（symbol 上有上劃線）" % n_low)
        if conflicts:
            print("  !! %d 組同料號腳位名不一致，**未寫入**，請人工釐清："
                  % len(conflicts))
            for c in conflicts[:8]:
                print("     %s pin %s" % (c[1], c[2]))
                for nm, rds in sorted(c[3].items()):
                    print("       %-14s %s%s"
                          % (nm, " ".join(rds[:3]),
                             " …共 %d 顆" % len(rds) if len(rds) > 3 else ""))
    return {"written": len(rows), "kept": kept, "conflicts": conflicts,
            "active_low": sum(1 for r in rows if r["active_low"]),
            "conflict_lines": [
                "`%s` pin %s —— %s" % (
                    c[1], c[2],
                    " vs ".join("`%s`（%s%s）"
                                % (nm, " ".join(rds[:2]),
                                   " 等 %d 顆" % len(rds) if len(rds) > 2 else "")
                                for nm, rds in sorted(c[3].items())))
                for c in conflicts]}


def lookup(project_dir, ddir, part, pin, explicit=None, package="",
           pick=None, verbose=True, role_of=None, board=None):
    """先查快取（含 SHA 有效性），未命中才抽取。回傳 list[dict]。

    抽到多筆代表這份 datasheet 涵蓋多種封裝／多處提到這支腳。先用
    `select_by_symbol()` 依 `.DSN` symbol 選欄、再以 netlist 電源地接法佐證
    （`role_of` 由呼叫端給，只有指定了哪一顆零件才有）；兩道都過才定案。
    否則**工具不替你挑**——列出全部原文與頁碼，用 `--pick <n>` 指定，並在
    `--package` 記下你的判斷依據。在指定之前不寫快取，避免把未解決的歧義保存
    成資產。
    """
    _p, rows, migrated = load_cache(project_dir)
    if migrated and verbose:
        print("!! 快取有 %d 列是舊 schema，已一律視為 %s（需重新確認）"
              % (migrated, UNRESOLVED))
    cached = [r for r in rows
              if r["part"].upper() == part.upper() and r["pin"] == str(pin)
              and r.get("resolved_by") in RESOLVED_OK]
    ds = find_datasheet(ddir, part, explicit)

    if cached and ds and os.path.exists(ds):
        cur = sha256(ds)
        stale = [r for r in cached
                 if r["sha256"] and (len(r["sha256"]) != len(cur)
                                     or r["sha256"] != cur)]
        if stale:
            if verbose:
                print("!! 快取的 SHA-256 與現行 datasheet 不符或為舊格式，重新抽取")
            cached = []
    if cached:
        if verbose:
            for r in cached:
                print("[快取] %s pin %s = %s %s | %s"
                      % (r["part"], r["pin"], r["pin_name"],
                         ("(%s)" % r["direction"]) if r["direction"] else "",
                         r["text"]))
                print("       出處 %s p.%s  package %s (%s)"
                      % (r["source_file"], r["page"],
                         r.get("package") or "-", r.get("resolved_by")))
        return cached

    if not ds:
        if verbose:
            print("!! 找不到 %s 的 datasheet（目錄 %s）" % (part, ddir))
            print("   請補上該檔，或用 --file 指定。**在補上之前，該腳位功能"
                  "一律標記 [D 缺]，不得以推論代替。**")
        return []

    hits = extract(ds, pin)
    if not hits:
        if verbose:
            print("!! %s 裡抽不到 pin %s 的腳位列（可能是掃描影像，或表格格式特殊）"
                  % (os.path.basename(ds), pin))
            print("   請人工開啟該 PDF 確認 —— 這種情況**不要猜**。")
        return []

    sha = sha256(ds)
    src = os.path.basename(ds)

    sym_rows = symbol_map(rows, part, board)
    sym_map = {p: r["pin_name"] for p, r in sym_rows.items()}
    auto, why = None, ""
    if len(hits) > 1 and pick is None:
        if role_of is None:
            why = "沒指定 --board/--refdes，無法用 netlist 佐證封裝"
        else:
            auto, why = select_by_symbol(ds, pin, sym_map, role_of)
        if verbose:
            print("%s 依 symbol 選封裝欄：%s" % ("OK" if auto else "!!", why))

    if len(hits) > 1 and pick is None and auto is None:
        if verbose:
            print("!! pin %s 抽到 %d 筆（這份 datasheet 可能涵蓋多種封裝）。"
                  "**拒絕替你挑，也不寫快取。**" % (pin, len(hits)))
            for i, (pg, name, d, txt, _k, pins) in enumerate(hits, start=1):
                print("   [%d] p.%-3s %-10s %-6s 腳號欄位 %-10s %s"
                      % (i, pg, name, d, ",".join(pins), txt[:60]))
            print("   -> 確認你的封裝後：--pick <n> [--package <你的封裝標籤>]")
            print("   -> 上一行是自動選欄沒成功的原因；請據此確認封裝，"
                  "不要只看腳名挑一筆。")
        return []

    if auto is not None:
        chosen, resolved, package = [auto], SYMBOL_SELECTED, package or why
    else:
        chosen = hits if len(hits) == 1 else [hits[pick - 1]]
        resolved = NOT_APPLICABLE if len(hits) == 1 else USER_CONFIRMED
    # symbol 是**獨立來源**：名字對得上就互相佐證，對不上就要當場講出來。
    # 最常見的成因是封裝選錯（同料號不同封裝腳位不同），靜靜採用 datasheet
    # 那筆等於把錯誤封裝的腳位名凍結成資產。
    sym = [sym_rows[str(pin)]] if str(pin) in sym_rows else []
    out = []
    for page, name, direction, text, _kind, _pins in chosen:
        corro = ""
        if sym:
            if norm_name(sym[0]["pin_name"]) == norm_name(name):
                corro = "%s(%s)" % (SYMBOL, sym[0]["pin_name"])
            elif verbose:
                print("!! symbol 說這支腳是 %s，datasheet 這筆是 %s —— "
                      "兩個來源不一致。" % (sym[0]["pin_name"], name))
                print("   最常見是封裝選錯；請確認封裝後用 --pick / --package "
                      "指定，或回頭檢查 symbol。")
        row = {"part": part, "pin": str(pin), "pin_name": name,
               "direction": direction, "text": text,
               "source_file": src, "page": str(page), "sha256": sha,
               "package": package or "", "resolved_by": resolved,
               "corroborated_by": corro,
               "active_low": (sym[0].get("active_low") or "") if sym else "",
               "verified_on": time.strftime("%Y-%m-%d")}
        append_cache(project_dir, row)
        out.append(row)
        if verbose:
            print("%s p.%s:  %s  %s  %s  %s  [%s]"
                  % (src, page, pin, name, direction, text, resolved))
    return out
