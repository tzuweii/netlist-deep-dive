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

  這裡只做兩件事：
    抽到一筆 -> 直接給答案（**已證明**無歧義），寫進快取
    抽到多筆 -> 攤開全部原文與頁碼，**拒絕替使用者挑**，標 [?] 等指定
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

# resolved_by 的合法值。**沒有「自動推導封裝」這種狀態** —— 封裝由
# `ndd_package` 依 netlist/BOM 證據判定，這裡只記錄這一筆原文是怎麼定案的。
NOT_APPLICABLE = "not_applicable"        # 只抽到一筆，**已證明**無歧義
USER_CONFIRMED = "user_confirmed"        # 多筆，使用者用 --pick 指定
UNRESOLVED = "unresolved_pending_user"   # 多筆且未指定 -> [?]，拒絕寫快取
RESOLVED_OK = (NOT_APPLICABLE, USER_CONFIRMED)

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


# ------------------------------------------------------------ 逐腳原文 --
def extract(path, pin, max_pages=20):
    """回傳 [(page, pin_name, direction, text, kind, pins), ...]。"""
    pin = str(pin).strip()
    hits = []
    seen = set()
    for i, text in _pages(path, max_pages):
        if not text.strip():
            continue
        for raw in text.splitlines():
            if NOISE_RX.search(raw):
                continue
            line = FOOTNOTE_RX.sub(" ", raw)   # `P1[1] 5 3 ...` 不剝就整列比不到
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


def lookup(project_dir, ddir, part, pin, explicit=None, package="",
           pick=None, verbose=True):
    """先查快取（含 SHA 有效性），未命中才抽取。回傳 list[dict]。

    抽到多筆代表這份 datasheet 涵蓋多種封裝／多處提到這支腳。**工具不替你挑**
    ——列出全部原文與頁碼，用 `--pick <n>` 指定，並在 `--package` 記下你的判斷
    依據。在指定之前不寫快取，避免把未解決的歧義保存成資產。
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

    if len(hits) > 1 and pick is None:
        if verbose:
            print("!! pin %s 抽到 %d 筆（這份 datasheet 可能涵蓋多種封裝）。"
                  "**拒絕替你挑，也不寫快取。**" % (pin, len(hits)))
            for i, (pg, name, d, txt, _k, pins) in enumerate(hits, start=1):
                print("   [%d] p.%-3s %-10s %-6s 腳號欄位 %-10s %s"
                      % (i, pg, name, d, ",".join(pins), txt[:60]))
            print("   -> 確認你的封裝後：--pick <n> [--package <你的封裝標籤>]")
            print("   -> 封裝可由 `ndd.py audit` 的封裝判定段推斷（只用 "
                  "netlist/BOM），推論結果一律標 [?]，請自行複核。")
        return []

    chosen = hits if len(hits) == 1 else [hits[pick - 1]]
    resolved = NOT_APPLICABLE if len(hits) == 1 else USER_CONFIRMED
    out = []
    for page, name, direction, text, _kind, _pins in chosen:
        row = {"part": part, "pin": str(pin), "pin_name": name,
               "direction": direction, "text": text,
               "source_file": src, "page": str(page), "sha256": sha,
               "package": package or "", "resolved_by": resolved,
               "corroborated_by": "", "verified_on": time.strftime("%Y-%m-%d")}
        append_cache(project_dir, row)
        out.append(row)
        if verbose:
            print("%s p.%s:  %s  %s  %s  %s  [%s]"
                  % (src, page, pin, name, direction, text, resolved))
    return out
