#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PADS 2000 ASCII netlist 解析器（通用，不綁任何專案）。

檔案結構：
    *PADS2000*
    *PART*
    <refdes>  <footprint>          # 每行一顆
    *NET*
    *SIGNAL* <net name>
    <refdes>.<pin> <refdes>.<pin>  # 可跨行
    *END*

⚠️ 本模組附帶 `selfcheck()`：用**完全獨立的邏輯**重數一次原始檔，比對 parser
   有沒有靜靜吞掉幾行。這是整條工具鏈最底層的假設——parser 若漏讀，上面所有
   結論都不成立而且不會有任何跡象。每次稽核都應該重跑。

⚠️ `pins()` 回傳的是**已接腳位**，不是元件總腳數——未接（NC）腳不出現在任何
   net，就永遠不會進 `pinmap`。所以 `len(nl.pins(rd))` 系統性低估，**不得**用來
   反推封裝（會穩定偏向較小的封裝）。它只能用來「排除」候選，不能「證明」。
"""
import fnmatch
import io
import os
import re


class Netlist(object):
    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)
        self.parts = {}       # refdes -> footprint
        self.nets = {}        # net    -> [(refdes, pin), ...]
        self.pinmap = {}      # refdes -> {pin: net}
        # ⚠️ 同一支腳出現一次以上時，pinmap 只能留一個值，被覆蓋的那條靜默丟掉，
        #    而 selfcheck 原本比的兩個數字**都**仍然相等，所以會回報 PASS。
        #
        #    這**不代表設計短路** —— 原理圖工具遇到短路會把兩條 net 合併成一條
        #    再匯出，不會讓一支腳出現兩次。真正的成因是：parser 誤框（某行沒被
        #    認成 *SIGNAL* 標頭，底下的腳被歸到前一條 net）、手改 .asc、串接多
        #    個檔案、非 PADS 2000 方言。**偵測 parser 誤框正是第 0 層的職責。**
        self.dup_pins = {}    # (refdes, pin) -> [net, ...]
        self._parse()

    def _parse(self):
        mode = None
        cur = None
        with io.open(self.path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                s = line.strip()
                if not s or s.startswith("*PADS"):
                    continue
                if s.startswith("*PART*"):
                    mode, cur = "PART", None
                    continue
                if s.startswith("*NET*"):
                    mode, cur = "NET", None
                    continue
                if s.startswith("*SIGNAL*"):
                    cur = s.split(None, 1)[1].strip()
                    self.nets.setdefault(cur, [])
                    continue
                if s.startswith("*END*"):
                    mode, cur = None, None
                    continue
                if mode == "PART":
                    f = s.split()
                    if len(f) >= 2:
                        self.parts[f[0]] = f[1]
                elif mode == "NET" and cur is not None:
                    for tok in s.split():
                        if "." not in tok:
                            continue
                        refdes, pin = tok.rsplit(".", 1)
                        self.nets[cur].append((refdes, pin))
                        prev = self.pinmap.setdefault(refdes, {}).get(pin)
                        if prev is not None:
                            d = self.dup_pins.setdefault((refdes, pin), [prev])
                            d.append(cur)          # 同 net 重複也要指名，否則
                        self.pinmap[refdes][pin] = cur   # 只報數字不可行動

    # ---- 查詢 -------------------------------------------------------------
    def pins(self, refdes):
        return dict(sorted(self.pinmap.get(refdes, {}).items(), key=_pinkey))

    def pin_net(self, refdes, pin):
        return self.pinmap.get(refdes, {}).get(str(pin))

    def net(self, name):
        return self.nets.get(name, [])

    def find_net(self, pattern):
        """glob；以 / 前後包住則視為 regex。"""
        if pattern.startswith("/") and pattern.endswith("/"):
            rx = re.compile(pattern[1:-1], re.I)
            return sorted(n for n in self.nets if rx.search(n))
        pat = pattern if any(c in pattern for c in "*?[") else "*%s*" % pattern
        return sorted(n for n in self.nets if fnmatch.fnmatch(n.upper(), pat.upper()))

    def find_part(self, pattern):
        pat = pattern if any(c in pattern for c in "*?[") else "*%s*" % pattern
        pat = pat.upper()
        return sorted((r for r, fp in self.parts.items()
                       if fnmatch.fnmatch(r.upper(), pat)
                       or fnmatch.fnmatch(fp.upper(), pat)), key=refkey)

    # ---- 分類 -------------------------------------------------------------
    PASSIVE_RX = re.compile(r"^(C|R|L|FB)\d")
    MECH_RX = re.compile(r"^(H|MH|FM|FIDUCIAL|Hole)\d*$|^(TP|EN|PG|SEL)_?\d")

    def is_passive(self, refdes):
        return bool(self.PASSIVE_RX.match(refdes))

    def is_mech(self, refdes):
        fp = self.parts.get(refdes, "")
        return bool(self.MECH_RX.match(refdes)) or fp.startswith(
            ("hole", "Hole", "FIDUCIAL", "TP_", "MTG"))

    def actives(self):
        return sorted((r for r in self.parts
                       if not self.is_passive(r) and not self.is_mech(r)), key=refkey)

    def single_pin_nets(self):
        """只掛一支腳的網路 = 懸空 / 未接。"""
        return sorted(n for n, v in self.nets.items() if len(v) == 1)

    # ---- 自我驗證 ---------------------------------------------------------
    def selfcheck(self):
        """不重用 _parse 的任何邏輯，重數一次原始檔。"""
        mode = None
        part_lines = 0
        sig_names = []
        pin_tokens = 0
        stray = []
        with io.open(self.path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                s = line.strip()
                if not s or s.startswith("*PADS"):
                    continue
                if s.startswith("*PART*"):
                    mode = "P"
                    continue
                if s.startswith("*NET*"):
                    mode = "N"
                    continue
                if s.startswith("*END*"):
                    mode = None
                    continue
                if s.startswith("*SIGNAL*"):
                    sig_names.append(s.split(None, 1)[1].strip())
                    continue
                if s.startswith("*"):
                    stray.append(s)
                    continue
                if mode == "P":
                    if len(s.split()) >= 2:
                        part_lines += 1
                    else:
                        stray.append("PART:" + s)
                elif mode == "N":
                    toks = s.split()
                    pin_tokens += sum(1 for t in toks if "." in t)
                    if any("." not in t for t in toks):
                        stray.append("NET:" + s)
                else:
                    stray.append("區塊外:" + s)
        got_pins = sum(len(v) for v in self.nets.values())
        # ⚠️ 第三個計數，缺它就抓不到「一支腳掛兩條 net」：pin_tokens 與 got_pins
        #    在該情境下**都會**是 2，只有 pinmap 少了一筆。
        mapped_pins = sum(len(v) for v in self.pinmap.values())
        dup = []
        for (rd, p), nets in sorted(self.dup_pins.items()):
            uniq = list(dict.fromkeys(nets))
            dup.append("%s.%s -> %s" % (rd, p, " / ".join(uniq)) if len(uniq) > 1
                       else "%s.%s 在 %s 內重複 %d 次" % (rd, p, uniq[0], len(nets)))
        ghost = [r for r in self.pinmap if r not in self.parts]
        ok = (part_lines == len(self.parts) and len(sig_names) == len(self.nets)
              and pin_tokens == got_pins and pin_tokens == mapped_pins
              and len(sig_names) == len(set(sig_names))
              and not dup and not ghost and not stray)
        return {
            "ok": ok,
            "parts": (part_lines, len(self.parts)),
            "nets": (len(sig_names), len(self.nets)),
            "pins": (pin_tokens, got_pins),
            "mapped": (pin_tokens, mapped_pins),
            "dup_pins": dup,
            "dup_signal": len(sig_names) - len(set(sig_names)),
            "ghost": ghost,
            "stray": stray,
        }

    def __repr__(self):
        return "<Netlist %s: %d parts / %d signals>" % (
            self.name, len(self.parts), len(self.nets))


def _pinkey(item):
    p = item[0]
    if p.isdigit():
        return (0, int(p), "")
    m = re.match(r"^([A-Za-z]+)(\d+)$", p)          # A1, B10, AN12 …
    if m:
        return (1, int(m.group(2)), m.group(1))
    return (2, 0, p)


def refkey(r):
    m = re.match(r"^([A-Za-z_]+)(\d*)(.*)$", r)
    if not m:
        return (r, 0, "")
    return (m.group(1), int(m.group(2) or 0), m.group(3))
