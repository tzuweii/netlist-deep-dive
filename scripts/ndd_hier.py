#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OrCAD Capture `.DSN` → 階層 + 腳位功能名（通用，不綁任何專案）。

呼叫 Capture 自己的 TCL API（`orDb_Dll_Tcl64.dll`）**唯讀**讀出設計資料庫，
產出兩份 CSV 再解析。這條路取代了先前用 `pstxprt.dat` / `pstxnet.dat` 文字
匯出檔反剖析的做法——那些檔案的方言隨 PSTWRITER 版本而變，實測兩片板就出現
7 處分歧；API 這一層沒有序列化，名字是「值」不是要拆的字串。

⚠️ **絕不寫入 Cadence 安裝目錄。** TCL 腳本隨 skill 一起發佈（與本檔同目錄），
   以絕對路徑餵給 tclsh；輸出一律落在分析資料夾。

⚠️ **路徑欄位只能給人看，不可程式解析。** block 名、pin 名、refdes 合法地
   含有 `/`、空白、`+`、`#`、`-` 與反斜線（四片板 14355 個名稱實測），所以
   **沒有任何分隔符是安全的**。結構一律走 `id` / `parent_id` / `depth`。

⚠️ `crosscheck()` 拿 `.asc` 當基準逐條對帳，**不符就是錯誤，不是警告**。
   階層寫錯不會讓任何東西崩潰，只會安靜地給出可信但錯誤的答案——開發期間
   `is_global` 恆為 0、`netlist_ignore` 失效、PST 方言三個 bug 全是這樣被
   對帳抓出來的，少了這道防線就會一路帶進結論。
"""
import csv
import glob
import io
import os
import re
import subprocess
import sys

TCL_NAME = "ndd_export.tcl"
DLL_REL = os.path.join("tools", "bin", "orDb_Dll_Tcl64.dll")
TCLSH_REL = os.path.join("tools", "bin", "tclsh.exe")
DEFAULT_TIMEOUT = 900


class HierError(Exception):
    """階層處理失敗。呼叫端應該讓 init 停下來，不要默默跳過。"""


# ---------------------------------------------------------------- Cadence 定位
def find_cadence(hint=None):
    """-> (tclsh 路徑, SPB 根目錄)；找不到就丟 HierError。

    不寫死版本號：掃所有 `SPB_*` 取版本最高的一套。
    """
    roots = []
    if hint:
        roots.append(hint)
    env = os.environ.get("CDSROOT") or os.environ.get("SPB_ROOT")
    if env:
        roots.append(env)
    for base in (r"C:\Cadence", r"D:\Cadence", r"C:\Program Files\Cadence"):
        roots.extend(sorted(glob.glob(os.path.join(base, "SPB_*"))))
    # 走過的 PATH 也算——使用者可能裝在非標準位置
    for p in os.environ.get("PATH", "").split(os.pathsep):
        m = re.search(r"^(.*[\\/]SPB_[^\\/]+)[\\/]", p + os.sep, re.I)
        if m:
            roots.append(m.group(1))

    seen, cands = set(), []
    for r in roots:
        r = os.path.normpath(r)
        if r.lower() in seen or not os.path.isdir(r):
            continue
        seen.add(r.lower())
        tclsh, dll = os.path.join(r, TCLSH_REL), os.path.join(r, DLL_REL)
        if os.path.isfile(tclsh) and os.path.isfile(dll):
            if os.path.getsize(tclsh) == 0:
                continue          # 0 byte 的執行檔會被 Windows 拒絕執行
            cands.append((_ver_key(r), r, tclsh))
    if not cands:
        raise HierError(
            "找不到可用的 OrCAD Capture 安裝（需要 tools/bin/tclsh.exe 與 "
            "orDb_Dll_Tcl64.dll）。\n"
            "  .DSN 的階層與腳位功能名只能由 Capture 自己的 API 讀出，\n"
            "  這台機器沒有 Capture 就無法處理 .DSN。")
    cands.sort(reverse=True)
    return cands[0][2], cands[0][1]


def _ver_key(root):
    m = re.search(r"SPB_(\d+)\.(\d+)", root, re.I)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


# ---------------------------------------------------------------- 轉換
def convert(dsn, outdir, tclsh=None, timeout=DEFAULT_TIMEOUT, echo=None):
    """跑 TCL 匯出器。-> (parts_csv, nodes_csv)

    唯讀：腳本從不呼叫 Save。開檔期間 Capture 會在 .DSN 旁建 `.DSNlck`，
    腳本結束時自行清除。
    """
    dsn = os.path.abspath(dsn)
    if not os.path.isfile(dsn):
        raise HierError("找不到 .DSN：%s" % dsn)
    lock = dsn + "lck"
    if os.path.exists(lock):
        raise HierError(
            "%s 存在——這份設計正開在 Capture 裡（或上次沒正常關閉）。\n"
            "  請先關閉該設計再重跑；本工具不會去動那個鎖檔。" % os.path.basename(lock))

    if tclsh is None:
        tclsh, _ = find_cadence()
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), TCL_NAME)
    if not os.path.isfile(script):
        raise HierError("skill 安裝不完整：缺少 %s" % script)
    if not os.path.isdir(outdir):
        os.makedirs(outdir)

    cmd = [tclsh, script, dsn.replace("\\", "/"), os.path.abspath(outdir).replace("\\", "/")]
    try:
        pr = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out, _ = pr.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        pr.kill()
        raise HierError(
            "轉換逾時（%ds）：%s\n"
            "  最常見的原因是那份設計正開在 Capture 裡，開檔會無限等待。"
            % (timeout, os.path.basename(dsn)))
    text = out.decode("utf-8", "replace") if out else ""
    if echo:
        for line in text.splitlines():
            echo("    " + line)
    if pr.returncode != 0:
        raise HierError("tclsh 失敗（exit %s）：\n%s" % (pr.returncode, text[-2000:]))

    stem = os.path.splitext(os.path.basename(dsn))[0]
    parts = os.path.join(outdir, "%s_parts.csv" % stem)
    nodes = os.path.join(outdir, "%s_nodes.csv" % stem)
    for p in (parts, nodes):
        if not os.path.isfile(p):
            raise HierError("轉換未產生 %s\ntclsh 輸出：\n%s" % (p, text[-2000:]))
    if "WARN" in text:
        raise HierError("轉換回報 WARN，視為失敗：\n%s" % text[-2000:])
    return parts, nodes


# ---------------------------------------------------------------- 解析
class Hierarchy(object):
    """兩份 CSV 的查詢介面。介面刻意比照 `ndd_pads.Netlist`。"""

    def __init__(self, parts_csv, nodes_csv):
        self.parts_csv, self.nodes_csv = parts_csv, nodes_csv
        self.parts = []          # 全部 occurrence（含 block 與 netlist_ignore）
        self.nodes = []          # 全部節點（含階層 port）
        self.by_id = {}
        self.children = {}
        self._paths = None
        self._pin_names = None
        self._load()

    def _load(self):
        with io.open(self.parts_csv, encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                self.parts.append(r)
                self.by_id[r["id"]] = r
                self.children.setdefault(r["parent_id"], []).append(r["id"])
        with io.open(self.nodes_csv, encoding="utf-8", newline="") as fh:
            self.nodes = list(csv.DictReader(fh))

    # ---- 查詢 -------------------------------------------------------------
    def real_parts(self):
        """實際會出現在 netlist 裡的零件（排除 block 與 PCB_LABEL 之類）。"""
        return [r for r in self.parts
                if r["is_block"] != "1" and r["netlist_ignore"] != "1"]

    def blocks(self):
        return [r for r in self.parts if r["is_block"] == "1"]

    def pins(self):
        return [n for n in self.nodes if n["kind"] == "pin"]

    def ports(self):
        """階層 port——`.asc` 分不出來的東西。"""
        return [n for n in self.nodes if n["kind"] == "port"]

    def path_of(self, pid):
        """沿 parent_id 上溯，回傳由外而內的名稱串列。**不切任何字串。**"""
        out, seen = [], set()
        while pid in self.by_id and pid not in seen:
            seen.add(pid)
            r = self.by_id[pid]
            out.append(r["refdes"])
            pid = r["parent_id"]
        return list(reversed(out))

    def part_paths(self):
        """-> {base_refdes: [由外而內的 block 名, ...]}（**不含零件自己**）。

        給人看用的階層位置。空串列 = 掛在 root（頂層原理圖）。
        """
        if self._paths is None:
            out = {}
            for r in self.real_parts():
                rd = r["base_refdes"] or r["refdes"]
                out[rd] = self.path_of(r["id"])[:-1]
            self._paths = out
        return self._paths

    def block_of(self, refdes, sep=" / "):
        """單一零件的階層位置字串；不在階層裡就回空字串。"""
        return sep.join(self.part_paths().get(refdes) or [])

    def pin_names(self):
        """-> {(refdes, pin_number): pin_name}，只收有功能名的。"""
        if self._pin_names is None:
            out = {}
            for n in self.pins():
                nm = n["pin_name"]
                if nm and nm != n["pin_number"]:
                    out[(n["refdes"], n["pin_number"])] = nm.replace(chr(92), "")
            self._pin_names = out
        return self._pin_names

    def active_low(self):
        """Capture 用逐字元反斜線表示上劃線：`P\\W\\R\\D\\N\\` = PWRDN（低有效）。

        `.asc` 與 `pstchip.dat` 都不帶這個資訊。
        """
        bs = chr(92)
        out = {}
        for n in self.pins():
            if bs in n["pin_name"]:
                out[(n["refdes"], n["pin_number"])] = n["pin_name"].replace(bs, "")
        return out

    # ---- 自我驗證 ---------------------------------------------------------
    def selfcheck(self):
        """不重用 `_load` 的任何邏輯，重數一次兩份 CSV，並驗結構不變量。"""
        def recount(path):
            with io.open(path, encoding="utf-8", newline="") as fh:
                rd = csv.reader(fh)
                hdr = next(rd, None)
                return sum(1 for row in rd if row), (hdr or [])

        np_, phdr = recount(self.parts_csv)
        nn_, nhdr = recount(self.nodes_csv)

        ids = [r["id"] for r in self.parts]
        dup_ids = len(ids) - len(set(ids))
        idset = set(ids)
        bad_parent = [r["id"] for r in self.parts
                      if r["parent_id"] not in ("", "0") and r["parent_id"] not in idset]
        pins = self.pins()
        orphan = [n for n in pins if not n["owner_id"] or n["owner_id"] not in idset]

        # parent 鏈自己走一次，和 API 給的 depth 對照——兩個獨立來源互證
        depth_bad = []
        for r in self.parts:
            n, pid, seen = 0, r["parent_id"], set()
            while pid in self.by_id and pid not in seen:
                seen.add(pid)
                n += 1
                pid = self.by_id[pid]["parent_id"]
            try:
                if int(r["depth"]) != n + 1:
                    depth_bad.append((r["refdes"], r["depth"], n + 1))
            except ValueError:
                depth_bad.append((r["refdes"], r["depth"], "非數字"))

        missing_cols = [c for c in ("id", "parent_id", "depth", "refdes", "base_refdes",
                                    "is_block", "netlist_ignore") if c not in phdr]
        missing_cols += [c for c in ("net", "owner_id", "refdes", "pin_number",
                                     "pin_name", "kind") if c not in nhdr]
        ok = (np_ == len(self.parts) and nn_ == len(self.nodes) and not dup_ids
              and not bad_parent and not orphan and not depth_bad and not missing_cols)
        return {
            "ok": ok,
            "parts": (np_, len(self.parts)),
            "nodes": (nn_, len(self.nodes)),
            "dup_ids": dup_ids,
            "bad_parent": bad_parent,
            "orphan_pins": [(n["net"], n["refdes"]) for n in orphan[:10]],
            "depth_mismatch": depth_bad[:10],
            "missing_cols": missing_cols,
        }

    def __repr__(self):
        return "<Hierarchy %d parts / %d blocks / %d pins / %d ports>" % (
            len(self.real_parts()), len(self.blocks()),
            len(self.pins()), len(self.ports()))


# ---------------------------------------------------------------- 交叉驗證
def crosscheck(hier, nl):
    """拿 `.asc`（`ndd_pads.Netlist`）當基準逐條對帳。

    `.asc` 是經年累月驗證過的接線權威；階層這條路是新的。兩者描述同一塊板，
    **任何不一致都代表其中一邊錯了**，必須當場停下來，不能只記一行警告。
    """
    h_nets = {}
    for n in hier.pins():
        h_nets.setdefault(n["net"], set()).add((n["refdes"], n["pin_number"]))
    a_nets = {k: set(v) for k, v in nl.nets.items()}

    only_h = sorted(set(h_nets) - set(a_nets))
    only_a = sorted(set(a_nets) - set(h_nets))
    differ = [k for k in (set(h_nets) & set(a_nets)) if h_nets[k] != a_nets[k]]

    # PADS 不接受 `*`、`/` 這類字元，formatter 會把整條 net 改名成 `X#####`
    # （實測 T_RADAR_T2：`GPU_SYS_RESET*` -> `X00697`）。名稱不同但**節點集合
    # 完全相同**就是改名，不是接錯——照樣比對得起來，只是 `.asc` 那邊丟失了
    # 設計者取的原名。這種情況要能與真正的接線不一致分開。
    by_nodes = {}
    for k in only_a:
        by_nodes.setdefault(frozenset(a_nets[k]), []).append(k)
    renamed, unmatched_h = [], []
    for k in only_h:
        cand = by_nodes.get(frozenset(h_nets[k]))
        if cand and len(cand) == 1:
            renamed.append((k, cand[0]))
        else:
            unmatched_h.append(k)
    matched_a = {b for _, b in renamed}
    unmatched_a = [k for k in only_a if k not in matched_a]

    h_parts = {r["base_refdes"] for r in hier.real_parts()}
    a_parts = set(nl.parts)
    ok = (not unmatched_h and not unmatched_a and not differ
          and h_parts == a_parts)
    return {
        "ok": ok,
        "nets": (len(h_nets), len(a_nets)),
        "nodes": (sum(len(v) for v in h_nets.values()),
                  sum(len(v) for v in a_nets.values())),
        "renamed": renamed,                 # PADS 改過名，接線相同
        "only_in_dsn": unmatched_h[:10],
        "only_in_asc": unmatched_a[:10],
        "net_differ": sorted(differ)[:10],
        "n_differ": len(differ),
        "parts": (len(h_parts), len(a_parts)),
        "parts_only_dsn": sorted(h_parts - a_parts)[:10],
        "parts_only_asc": sorted(a_parts - h_parts)[:10],
    }


def format_report(tag, sc, cc):
    """一段人看得懂的摘要；失敗時把該追的細節印出來。"""
    L = ["%s：" % tag]
    L.append("  自我驗證 %s  parts %s  nodes %s"
             % ("OK" if sc["ok"] else "**FAIL**", sc["parts"], sc["nodes"]))
    if not sc["ok"]:
        for k in ("dup_ids", "bad_parent", "orphan_pins", "depth_mismatch", "missing_cols"):
            if sc[k]:
                L.append("    %s: %s" % (k, sc[k]))
    L.append("  對帳 .asc %s  nets %s  節點 %s  零件 %s"
             % ("OK" if cc["ok"] else "**FAIL**", cc["nets"], cc["nodes"], cc["parts"]))
    if cc.get("renamed"):
        L.append("  PADS 改寫了 %d 條 net 名（含 .asc 不接受的字元，接線相同）："
                 % len(cc["renamed"]))
        for a, b in cc["renamed"][:5]:
            L.append("    %s  ->  %s" % (a, b))
    if not cc["ok"]:
        for k in ("only_in_dsn", "only_in_asc", "net_differ",
                  "parts_only_dsn", "parts_only_asc"):
            if cc[k]:
                L.append("    %s: %s" % (k, cc[k]))
    return "\n".join(L)


# ---------------------------------------------------------------- CLI（自測用）
def main(argv=None):
    import argparse
    from ndd_pads import Netlist
    ap = argparse.ArgumentParser(description="轉換並驗證單一 .DSN")
    ap.add_argument("dsn")
    ap.add_argument("asc", nargs="?", help="對帳基準；省略則只做自我驗證")
    ap.add_argument("-o", "--outdir", default=".")
    a = ap.parse_args(argv)

    tclsh, root = find_cadence()
    print("Cadence : %s" % root)
    p, n = convert(a.dsn, a.outdir, tclsh=tclsh, echo=lambda s: print(s))
    h = Hierarchy(p, n)
    print(repr(h))
    sc = h.selfcheck()
    cc = crosscheck(h, Netlist(a.asc)) if a.asc else {
        "ok": True, "nets": ("-", "-"), "nodes": ("-", "-"), "parts": ("-", "-"),
        "only_in_dsn": [], "only_in_asc": [], "net_differ": [], "n_differ": 0,
        "parts_only_dsn": [], "parts_only_asc": []}
    print(format_report(os.path.basename(a.dsn), sc, cc))
    return 0 if (sc["ok"] and cc["ok"]) else 1


if __name__ == "__main__":
    sys.exit(main())
