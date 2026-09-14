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
        "corroborated_by", "active_low", "verified_on"]
LEGACY_COLS = ["part", "pin", "pin_name", "direction", "text",
               "source_file", "page", "sha256", "verified_on"]

# resolved_by 的合法值。**沒有「自動推導封裝」這種狀態** —— 封裝由
# `ndd_package` 依 netlist/BOM 證據判定，這裡只記錄這一筆原文是怎麼定案的。
NOT_APPLICABLE = "not_applicable"        # 只抽到一筆，**已證明**無歧義
USER_CONFIRMED = "user_confirmed"        # 多筆，使用者用 --pick 指定
UNRESOLVED = "unresolved_pending_user"   # 多筆且未指定 -> [?]，拒絕寫快取
SYMBOL = "capture_symbol"                # 由 .DSN 的 Capture symbol 帶出的腳位名
RESOLVED_OK = (NOT_APPLICABLE, USER_CONFIRMED)

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
    tmp = p + ".tmp"
    with io.open(tmp, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        w.writeheader()
        for r in keep + rows:
            w.writerow({c: r.get(c, "") for c in COLS})
    if os.path.exists(p):
        os.remove(p)
    os.rename(tmp, p)
    return len(keep), len(rows)


def import_symbols(project_dir, boards, verbose=True):
    """把各板 `.DSN` 的 symbol 腳位名寫進快取。

    `boards`: [(board_key, hier, pn_of_refdes, nodes_csv_path), ...]
    """
    merged, conflicts, seen_src = {}, [], {}
    for key, hier, pn_of, nodes_csv in boards:
        if hier is None:
            continue
        got, conf = symbol_names(hier, pn_of)
        conflicts.extend((key,) + c for c in conf)
        src = os.path.basename(nodes_csv) if nodes_csv else ""
        for k, (nm, al, rds) in got.items():
            prev = merged.get(k)
            if prev and norm_name(prev[0]) != norm_name(nm):
                # 跨板同一料號給出不同名字——同樣不可合併
                conflicts.append((key, k[0], k[1],
                                  {prev[0]: prev[2], nm: ["%s:%s" % (key, r)
                                                          for r in rds]}))
                merged.pop(k, None)
                continue
            if prev:
                prev[2].extend("%s:%s" % (key, r) for r in rds)
                continue
            merged[k] = [nm, al, ["%s:%s" % (key, r) for r in rds]]
            seen_src[k] = (src, nodes_csv)
    bad = {(c[1], c[2]) for c in conflicts}
    rows, today = [], time.strftime("%Y-%m-%d")
    for (mpn, pin), (nm, al, rds) in sorted(merged.items()):
        if (mpn, pin) in bad:
            continue
        src, path = seen_src[(mpn, pin)]
        rows.append({"part": mpn, "pin": pin, "pin_name": nm, "direction": "",
                     "text": "", "source_file": src, "page": "",
                     "sha256": sha256(path) if path and os.path.exists(path) else "",
                     "package": "", "resolved_by": SYMBOL,
                     "corroborated_by": " ".join(rds[:4]),
                     "active_low": "1" if al else "",
                     "verified_on": today})
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
    # symbol 是**獨立來源**：名字對得上就互相佐證，對不上就要當場講出來。
    # 最常見的成因是封裝選錯（同料號不同封裝腳位不同），靜靜採用 datasheet
    # 那筆等於把錯誤封裝的腳位名凍結成資產。
    sym = [r for r in rows
           if r["part"].upper() == part.upper() and r["pin"] == str(pin)
           and r.get("resolved_by") == SYMBOL]
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
