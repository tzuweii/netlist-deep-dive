#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""從本地 datasheet PDF 抽出「某料號某支腳」的功能列，並快取原文。

════════════════════════════════════════════════════════════════════════════
設計上最重要的一條：**快取原文，永不快取解讀。**

  快取的是 datasheet 的逐字內容 + 出處（檔名／頁碼／SHA256）。
  不是「pin1 與 pin3 內部連通」這種我推出來的拓樸結論。

  差別在於：原文快取你 5 秒就能核對；推論快取會把我的錯誤凍結成永久資產，
  而且沒人看得出來。datasheet 換版時 SHA256 不符，整批失效並要求重抽。
════════════════════════════════════════════════════════════════════════════

用法：
    python ndd.py pinfn LMX2594 8
    python ndd.py pinfn LMK00304 15 --file lmk00304.pdf
    python ndd.py pinfn --list
"""
import csv
import hashlib
import io
import os
import re
import time

CACHE = "verified-pins.csv"
COLS = ["part", "pin", "pin_name", "direction", "text",
        "source_file", "page", "sha256", "verified_on"]

# ⚠️ 腳位表至少有三種排版，只認一種會**靜靜漏掉正確的列**——那比抽不到更危險，
#    因為你會以為查過了。實測：
#      A 腳號在前  "8 OSCinP Input Reference input clock (+)."          (LMX2594)
#                  "14, 15 CLKin0, CLKin0* I Universal clock input 0"   (LMK00304)
#      B 腳名在前  "1A 2 I/O Channel 1 input or output"                 (SN74CBTLV3126)
#      C 腳名在前 + 多個封裝欄  "A2 21 18 address input 2"              (PCA9547 SO24/HVQFN24)
#    C 型無法從單列判斷哪一欄對應你的封裝，所以一律列出並警告。
PIN_FIRST_RX = re.compile(
    r"^\s*(?P<pins>\d{1,3}(?:\s*,\s*\d{1,3})*)\s+"
    r"(?P<name>[A-Za-z_][\w/*+().\-]*(?:\s*,\s*[A-Za-z_][\w/*+().\-]*)*)\s+"
    r"(?P<rest>\S.*)$")
# ⚠️ 腳名可能以數字開頭（TI 的 1A1 / 2Y4 / 4OE），所以不能要求首字是字母，
#    改成「必須含至少一個字母」——否則會把 TI 的表整張漏掉。
_NM = r"(?=[\w/*+().\-]*[A-Za-z])[\w/*+().\-]{1,16}"
NAME_FIRST_RX = re.compile(
    r"^\s*(?P<name>" + _NM + r"(?:\s*,\s*" + _NM + r")*)\s+"
    r"(?P<pins>\d{1,3}(?:[\s,]+\d{1,3}){0,3})\s+"
    r"(?P<rest>\S.*)$")
DIR_RX = re.compile(r"^(I/O|I|O|P|Input|Output|Supply|Ground|Power|GND|In|Out)\b",
                    re.I)
# 目錄頁／頁尾的假命中特徵
NOISE_RX = re.compile(r"\.{4,}|Submit Document|Copyright|www\.|Product Folder"
                      r"|^\s*\d+\s*$|Feedback")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def find_datasheet(ddir, part, explicit=None):
    """在 datasheet 目錄找對應檔案。⚠️ family datasheet 很常見（檔名只寫一個
    型號、內容涵蓋整個系列），所以找不到時要提示使用者手動指定 --file。"""
    if explicit:
        p = os.path.join(ddir, explicit)
        return p if os.path.exists(p) else None
    if not os.path.isdir(ddir):
        return None
    key = re.sub(r"[^A-Za-z0-9]", "", part.split(",")[0]).lower()
    best, blen = None, 0
    for f in os.listdir(ddir):
        if not f.lower().endswith(".pdf"):
            continue
        stem = re.sub(r"[^a-z0-9]", "", os.path.splitext(f)[0].lower())
        for n in range(len(key), 4, -1):
            if key[:n] and key[:n] in stem and n > blen:
                best, blen = os.path.join(ddir, f), n
                break
    return best


def extract(path, pin, max_pages=20):
    """回傳 [(page, pin_name, direction, text), ...]，可能多筆（多個封裝欄）。"""
    try:
        from pypdf import PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader
        except ImportError:
            raise RuntimeError("需要 pypdf：pip install pypdf")
    reader = PdfReader(path)
    pin = str(pin).strip()
    hits = []
    seen = set()
    for i, page in enumerate(reader.pages[:max_pages], start=1):
        text = page.extract_text() or ""
        if not text.strip():
            continue
        for line in text.splitlines():
            if NOISE_RX.search(line):          # 目錄／頁尾，不是腳位表
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
                if len(desc) < 3:              # 沒有描述文字，多半是誤命中
                    continue
                note = ""
                if kind != "A" and len(pins) > 1:
                    note = "（此列有 %d 個封裝欄：%s，請確認你的封裝）" % (
                        len(pins), "/".join(pins))
                key = (i, name, desc[:60])
                if key in seen:
                    continue
                seen.add(key)
                hits.append((i, name, direction, (desc[:240] + note)))
                break
    return hits


def load_cache(project_dir):
    p = os.path.join(project_dir, CACHE)
    rows = []
    if os.path.exists(p):
        with io.open(p, encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
    return p, rows


def append_cache(project_dir, row):
    p, rows = load_cache(project_dir)
    new = not os.path.exists(p)
    with io.open(p, "a", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        if new:
            w.writeheader()
        w.writerow({c: row.get(c, "") for c in COLS})


def lookup(project_dir, ddir, part, pin, explicit=None, verbose=True):
    """先查快取（含 SHA256 有效性），未命中才抽取。回傳 list[dict]。"""
    _p, rows = load_cache(project_dir)
    cached = [r for r in rows
              if r["part"].upper() == part.upper() and r["pin"] == str(pin)]
    ds = find_datasheet(ddir, part, explicit)

    if cached:
        if ds and os.path.exists(ds):
            cur = sha256(ds)
            stale = [r for r in cached if r["sha256"] and r["sha256"] != cur]
            if stale:
                if verbose:
                    print("!! 快取的 SHA256 與現行 datasheet 不符（換版？），重新抽取")
                cached = []
        if cached:
            if verbose:
                for r in cached:
                    print("[快取] %s pin %s = %s %s | %s"
                          % (r["part"], r["pin"], r["pin_name"],
                             ("(%s)" % r["direction"]) if r["direction"] else "",
                             r["text"]))
                    print("       出處 %s p.%s  sha %s"
                          % (r["source_file"], r["page"], r["sha256"]))
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
            print("   請人工開啟該 PDF 確認後，用 --note 手動記錄。")
        return []

    sha = sha256(ds)
    out = []
    for page, name, direction, text in hits:
        row = {"part": part, "pin": str(pin), "pin_name": name,
               "direction": direction, "text": text,
               "source_file": os.path.basename(ds), "page": str(page),
               "sha256": sha, "verified_on": time.strftime("%Y-%m-%d")}
        append_cache(project_dir, row)
        out.append(row)
        if verbose:
            print("%s p.%s:  %s  %s  %s  %s"
                  % (os.path.basename(ds), page, pin, name, direction, text))
    if verbose and len(hits) > 1:
        print("   ⚠️ 抽到 %d 筆——這份 datasheet 可能涵蓋多種封裝，"
              "請確認你的封裝對應哪一欄。" % len(hits))
    return out
