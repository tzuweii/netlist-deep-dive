#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""從本地 datasheet PDF 抽出腳位原文，並做**惰性** package 解析。

════════════════════════════════════════════════════════════════════════════
設計上最重要的兩條：

**一、快取原文，永不快取解讀。**
  快取 datasheet 的逐字內容 + 出處（檔名／頁碼／SHA-256／封裝欄）。
  不是「pin1 與 pin3 內部連通」這種推出來的拓樸結論。原文快取 5 秒就能核對；
  推論快取會把錯誤凍結成永久資產，而且沒人看得出來。

**二、package 只在會改變答案時才解析。**
  三階，前兩階零成本：
    1. datasheet 腳位表只有一欄        -> not_applicable（**已證明**無歧義）
    2. 觀測腳位集 ⊆ 某欄合法集且唯一   -> auto_unique
    3. 多欄皆可容納／抽取失敗          -> unresolved_pending_user（問一次）

  ⚠️ **不得以已接腳數推定封裝。** netlist 的 pinmap 只含已接腳，系統性低估，
     用腳數判定會穩定偏向較小的封裝——錯的方向一致，用滿接樣本測永遠看不到。
     腳數只能「排除」候選，不能「證明」。
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
        "corroborated_by", "verified_on"]
LEGACY_COLS = ["part", "pin", "pin_name", "direction", "text",
               "source_file", "page", "sha256", "verified_on"]

# resolved_by 的合法值（有優先序）
NOT_APPLICABLE = "not_applicable"
SHARED_PINOUT = "shared_pinout"
AUTO_UNIQUE = "auto_unique"
USER_CONFIRMED = "user_confirmed"
DECLARED_UNVERIFIED = "declared_unverified"
UNRESOLVED = "unresolved_pending_user"
CONFLICT = "conflict"
RESOLVED_OK = (NOT_APPLICABLE, SHARED_PINOUT, AUTO_UNIQUE, USER_CONFIRMED)

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
# 封裝欄標題：字母開頭、含結尾腳數，例如 HVQFN24 / SO24 / TSSOP20 / PKG24
PKG_TOKEN_RX = re.compile(r"\b([A-Z][A-Z0-9\-]*?(\d{1,3}))\b")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()                 # ⚠️ 完整值，不再截前綴


# ------------------------------------------------------------ datasheet --
def find_datasheet(ddir, part, explicit=None):
    """⚠️ family datasheet 很常見（檔名只寫一個型號、內容涵蓋整個系列），
    只比檔名一定漏。漏掉會讓人誤標 [D 缺] 並重複採購已持有的規格書。"""
    if explicit:
        p = os.path.join(ddir, explicit)
        return p if os.path.exists(p) else None
    if not os.path.isdir(ddir):
        return None
    key = re.sub(r"[^A-Za-z0-9]", "", part.split(",")[0]).lower()
    alpha = re.match(r"^[A-Za-z]{3,}", part.split(",")[0])
    alpha = alpha.group(0).lower() if alpha else ""
    best, blen, via = None, 0, ""
    for f in sorted(os.listdir(ddir)):
        if not f.lower().endswith(".pdf"):
            continue
        stem = re.sub(r"[^a-z0-9]", "", os.path.splitext(f)[0].lower())
        for n in range(len(key), 4, -1):
            if key[:n] and key[:n] in stem and n > blen:
                best, blen, via = os.path.join(ddir, f), n, "料號前綴"
                break
    if best is None and len(alpha) >= 4:
        for f in sorted(os.listdir(ddir)):
            if f.lower().endswith(".pdf") and alpha in re.sub(
                    r"[^a-z0-9]", "", os.path.splitext(f)[0].lower()):
                best, via = os.path.join(ddir, f), "型號字母段 '%s'" % alpha.upper()
                break
    if best is None:
        base = re.sub(r"[^A-Za-z0-9]", "", part.split(",")[0]).upper()
        for cand in _search_in_text(ddir, base):
            best, via = cand, "PDF 內文含型號"
            break
    if best and via and not via.startswith("料號前綴"):
        print("   （以 %s 比對到 %s——請確認是同一份規格書）"
              % (via, os.path.basename(best)))
    return best


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


# ------------------------------------------------- 逐 package 欄合法集 --
def extract_pin_tables(path, max_pages=20):
    """回傳 (legal, reason)。

    `legal` 是 `{封裝欄標題: set(pin label)}`；抽不到可信的多欄表時回傳
    `(None, 原因)`。

    ⚠️ **必須全部成立才算成功**，任一不成立即視為抽取失敗：
        ① 找得到明確的封裝欄標題列
        ② 每個資料列的 pin 欄位數與標題欄數**相等**（不是「至少」）
        ③ 同一欄內無重複 pin
        ④ 欄內腳位數與封裝名稱隱含腳數相符

    PDF 文字抽取會失去欄位幾何，這是最容易產生「看似合法但錯位」候選的地方。
    失敗時**絕不可**退回腳數猜測——那會把整個修正抵銷掉。
    """
    for _pg, text in _pages(path, max_pages):
        lines = [l for l in text.splitlines() if l.strip()]
        for i, line in enumerate(lines):
            if NOISE_RX.search(line):
                continue
            toks = PKG_TOKEN_RX.findall(line.upper())
            heads = [t[0] for t in toks]
            counts = [int(t[1]) for t in toks]
            if len(heads) < 2 or len(set(heads)) != len(heads):
                continue
            cols = {h: {} for h in heads}
            ok_rows = 0
            for row in lines[i + 1:i + 200]:
                if NOISE_RX.search(row):
                    continue
                m = NAME_FIRST_RX.match(row)
                if not m:
                    continue
                pins = [p for p in re.split(r"[\s,]+", m.group("pins")) if p]
                if len(pins) != len(heads):      # ② 欄數必須相等
                    continue
                fn = m.group("name").split(",")[0].strip()
                for h, p in zip(heads, pins):
                    if p in cols[h]:
                        return None, "欄 %s 內有重複 pin（欄位錯位）" % h
                    cols[h][p] = fn              # ⚠️ 同時記下**腳位功能**
                ok_rows += 1
            if ok_rows < 3:
                continue
            for h, cnt in zip(heads, counts):
                if len(cols[h]) != cnt:
                    return None, ("欄 %s 抽出 %d 個 pin，但封裝名隱含 %d 個"
                                  "（欄位錯位）" % (h, len(cols[h]), cnt))
            return cols, ""
    return None, "找不到可信的多封裝欄腳位表"


def resolve_package(legal, observed, declared=None):
    """三階解析。回傳 (package, resolved_by, corroborated_by, note)。

    `legal` 是 `{封裝欄: {pin: 腳位功能}}`。

    ⚠️ **shared_pinout 必須比對腳位功能，不能只比 pin label 集合。** 兩個封裝
       同為 1–N 是常態，label 集合相同完全不代表 pin 5 是同一個訊號——那正是
       package 解析要解決的問題本身。只比 label 等於把問題當成答案。
    """
    if declared:
        if legal:
            cand = [h for h in legal
                    if h.upper() in declared.upper()
                    or declared.upper() in h.upper()]
            if not cand:
                return (declared, CONFLICT, "",
                        "指定的 %s 不在腳位表的封裝欄中" % declared)
            miss = set(observed) - set(legal[cand[0]])
            if miss:
                return (cand[0], CONFLICT, "",
                        "觀測腳位 %s 不存在於 %s 的合法集"
                        % (",".join(sorted(miss)[:5]), cand[0]))
            return cand[0], USER_CONFIRMED, "pin_set", ""
        return declared, DECLARED_UNVERIFIED, "", "無法由腳位表佐證"
    if legal is None:
        return "", UNRESOLVED, "", "腳位表抽取失敗，不得退回腳數猜測"
    if len(legal) == 1:
        return list(legal)[0], NOT_APPLICABLE, "", ""
    obs = set(str(p) for p in observed)
    # ⚠️ 用**包含關係**而非基數：只能觀測到已接腳，所以方向必須是 observed ⊆ legal
    fits = [h for h in legal if obs and obs <= set(legal[h])]
    if len(fits) == 1:
        return fits[0], AUTO_UNIQUE, "pin_set", ""
    if not fits:
        return "", CONFLICT, "", "觀測腳位不被任何封裝欄容納"
    # 候選封裝在「實際用到的腳」上**功能完全一致** -> 封裝不改變答案，不必問
    base = {p: legal[fits[0]][p] for p in obs}
    if all(base == {p: legal[h][p] for p in obs} for h in fits[1:]):
        return "", SHARED_PINOUT, "pin_function", "候選封裝在使用到的腳上功能一致"
    return "", UNRESOLVED, "", "多個封裝欄皆可容納：%s" % "／".join(sorted(fits))


# ------------------------------------------------------------ 逐腳原文 --
def extract(path, pin, max_pages=20):
    """回傳 [(page, pin_name, direction, text, kind, pins), ...]。"""
    pin = str(pin).strip()
    hits = []
    seen = set()
    for i, text in _pages(path, max_pages):
        if not text.strip():
            continue
        for line in text.splitlines():
            if NOISE_RX.search(line):
                continue
            for rx, kind in ((PIN_FIRST_RX, "A"), (NAME_FIRST_RX, "B/C")):
                m = rx.match(line)
                if not m:
                    continue
                pins = [p.strip() for p in re.split(r"[\s,]+", m.group("pins")) if p]
                if pin not in pins:
                    continue
                names = [n.strip() for n in m.group("name").split(",")]
                if kind == "A" and len(names) == len(pins):
                    name = names[pins.index(pin)]
                else:
                    name = names[0]
                rest = m.group("rest").strip()
                dm = DIR_RX.match(rest)
                direction = dm.group(0) if dm else ""
                desc = (rest[len(direction):].strip() if direction else rest)
                if len(desc) < 3:
                    continue
                key = (i, name, desc[:60])
                if key in seen:
                    continue
                seen.add(key)
                hits.append((i, name, direction, desc[:240], kind, pins))
                break
    return hits


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
    with io.open(p, "a", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        if new:
            w.writeheader()
        w.writerow({c: row.get(c, "") for c in COLS})


def lookup(project_dir, ddir, part, pin, explicit=None, observed=None,
           declared_package=None, verbose=True):
    """先查快取（含 SHA 有效性），未命中才抽取。回傳 list[dict]。"""
    _p, rows, migrated = load_cache(project_dir)
    if migrated and verbose:
        print("!! 快取有 %d 列是舊 schema，已一律視為 %s（需重新解析封裝）"
              % (migrated, UNRESOLVED))
    cached = [r for r in rows
              if r["part"].upper() == part.upper() and r["pin"] == str(pin)
              and r.get("resolved_by") in RESOLVED_OK]
    ds = find_datasheet(ddir, part, explicit)

    if cached and ds and os.path.exists(ds):
        cur = sha256(ds)
        # 前綴 SHA 與完整 SHA 不可直接比對；長度不同即視為待重抽
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

    legal, why = extract_pin_tables(ds)
    pkg, resolved_by, corrob, note = resolve_package(
        legal, observed or [], declared_package)
    if verbose and legal is None and why:
        print("   （腳位表逐欄抽取失敗：%s）" % why)
    if verbose and note:
        print("   （package: %s）" % note)

    hits = extract(ds, pin)
    if not hits:
        if verbose:
            print("!! %s 裡抽不到 pin %s 的腳位列（可能是掃描影像，或表格格式特殊）"
                  % (os.path.basename(ds), pin))
        return []

    if resolved_by not in RESOLVED_OK:
        if verbose:
            print("!! package 未解析（%s）——**拒絕寫入快取**，避免把未解決的"
                  "歧義保存成資產。" % resolved_by)
            print("   請用 --package <欄標題> 指定；可選的欄：%s"
                  % ("／".join(sorted(legal)) if legal else "（抽取失敗）"))
            for page, name, direction, text, _k, _p in hits:
                print("   [未快取] p.%s  %s  %s  %s" % (page, name, direction, text))
        return []

    sha = sha256(ds)
    out = []
    for page, name, direction, text, _kind, _pins in hits:
        row = {"part": part, "pin": str(pin), "pin_name": name,
               "direction": direction, "text": text,
               "source_file": os.path.basename(ds), "page": str(page),
               "sha256": sha, "package": pkg, "resolved_by": resolved_by,
               "corroborated_by": corrob,
               "verified_on": time.strftime("%Y-%m-%d")}
        append_cache(project_dir, row)
        out.append(row)
        if verbose:
            print("%s p.%s:  %s  %s  %s  %s  [package %s / %s]"
                  % (os.path.basename(ds), page, pin, name, direction, text,
                     pkg or "-", resolved_by))
    return out
