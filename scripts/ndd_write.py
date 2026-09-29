# -*- coding: utf-8 -*-
"""撰寫 `<板>_Architecture.md`：三階段，每次呼叫都是一個**無工具、單回合**的
`claude -p`。

為什麼不用 subagent：實測 subagent 每次請求的固定開銷約 5 萬 token（系統提示
與工具定義），而且讀檔、寫檔、回報各佔一回合，每回合整份 context 重送一次——
一片板六個 subagent，光重送就上百萬。這裡把材料**直接放進訊息**、叫模型**直接
輸出檔案內容**：一次請求 = 材料送一次 + 輸出一次，固定開銷不到 1 千。

沒有工具（`--tools ""`）、不載 MCP（`--strict-mcp-config`）：兩者的定義都算在每次
請求的 context 裡，而這裡用不到。

規範在 `references/architecture-brief.md`，當系統提示用。
"""
from __future__ import print_function

import concurrent.futures
import io
import json
import os
import re
import shutil
import subprocess

from ndd_arch import PACK_DIR

HERE = os.path.dirname(os.path.abspath(__file__))
BRIEF = os.path.join(os.path.dirname(HERE), "references", "architecture-brief.md")
WORK = "work"
TIMEOUT = 60 * 60           # 單次呼叫上限；C 階段輸出最長，實測數分鐘
# 測試替換 `claude -p` 用（init 的測試會一路跑到這裡，不能真的呼叫）。
DEFAULT_RUNNER = None

_RX_GROUP = re.compile(r"^\s*(\d+)\s*[:：]\s*(.+?)\s*$")
_RX_FENCE = re.compile(r"^\s*```(?:markdown|md)?\s*\n(.*)\n```\s*$", re.S)


class WriteError(RuntimeError):
    pass


# ----------------------------------------------------------------------
# 組訊息
# ----------------------------------------------------------------------

def _read(p):
    with io.open(p, encoding="utf-8") as fh:
        return fh.read()


def _attach(names, d):
    """把檔案內容接成訊息：每份前面標檔名。"""
    out = []
    for n in names:
        out.append(u"<file name=\"%s\">\n%s\n</file>" % (n, _read(os.path.join(d, n))))
    return u"\n\n".join(out)


def prompt_a(board, d):
    return (u"板名：%s。你是 **A 階段**。依系統提示的「A 階段」要求，輸出 "
            u"`work/A_outline.md` 的完整內容。\n\n%s"
            % (board, _attach(["index.md", "00_skeleton.md"], d)))


def prompt_b(board, d, n, files):
    return (u"板名：%s。你是 **B 階段第 %d 組**。依系統提示的「B 階段」要求，輸出 "
            u"`work/B_%d.md` 的完整內容。\n\n%s\n\n%s"
            % (board, n, n, _attach([WORK + "/A_outline.md"], d), _attach(files, d)))


def prompt_c(board, d, bnames):
    return (u"板名：%s。你是 **C 階段**。依系統提示的「C 階段」要求，輸出 "
            u"`%s_Architecture.md` 的完整內容。\n\n%s"
            % (board, board, _attach([WORK + "/A_outline.md"] + bnames, d)))


def parse_groups(outline, available):
    """A 大綱裡 ```groups 區塊 -> [(組號, [檔名])]。

    檔名必須都在材料裡、每份只能出現一次、材料要全部分完——任何一條不成立都
    停下來，不替 A 補分組（漏分的材料等於那幾個方塊沒人寫）。"""
    m = re.search(r"```groups\s*\n(.*?)```", outline, re.S)
    if not m:
        raise WriteError(u"A_outline.md 沒有 ```groups 區塊")
    groups, seen = [], set()
    for ln in m.group(1).splitlines():
        g = _RX_GROUP.match(ln)
        if not g:
            continue
        files = [f.strip(u" `") for f in re.split(u"[,，、]", g.group(2)) if f.strip()]
        bad = [f for f in files if f not in available]
        if bad:
            raise WriteError(u"分組裡有不存在的檔案：%s" % u"、".join(bad))
        dup = [f for f in files if f in seen]
        if dup:
            raise WriteError(u"檔案被分到兩組：%s" % u"、".join(dup))
        seen.update(files)
        groups.append((int(g.group(1)), files))
    left = sorted(set(available) - seen)
    if left:
        raise WriteError(u"有材料沒分到任何一組：%s" % u"、".join(left))
    return groups


def strip_fence(txt):
    """模型偶爾把整份包在 ```markdown 裡——剝掉。"""
    m = _RX_FENCE.match(txt.strip())
    return (m.group(1) if m else txt.strip()) + u"\n"


# ----------------------------------------------------------------------
# 呼叫
# ----------------------------------------------------------------------

def _claude():
    exe = shutil.which("claude")
    if not exe:
        raise WriteError(u"找不到 `claude` 指令（Claude Code CLI）")
    return exe


def call(prompt, cwd, model=None, runner=None):
    """-> (輸出文字, usage dict)。`runner` 給測試替換。"""
    runner = runner or DEFAULT_RUNNER
    if runner is not None:
        return runner(prompt)
    cmd = [_claude(), "-p", "--tools", "", "--strict-mcp-config",
           "--no-session-persistence",
           "--output-format", "json", "--system-prompt-file", BRIEF]
    if model:
        cmd += ["--model", model]
    r = subprocess.run(cmd, input=prompt.encode("utf-8"), cwd=cwd,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=TIMEOUT)
    if r.returncode != 0:
        raise WriteError(u"claude 失敗（%d）：%s" % (
            r.returncode, r.stderr.decode("utf-8", "replace")[-800:]))
    d = json.loads(r.stdout.decode("utf-8"))
    if d.get("is_error"):
        raise WriteError(u"claude 回報錯誤：%s" % d.get("result"))
    u = d.get("usage") or {}
    return d.get("result") or u"", {
        "input": (u.get("input_tokens") or 0)
                 + (u.get("cache_creation_input_tokens") or 0)
                 + (u.get("cache_read_input_tokens") or 0),
        "output": u.get("output_tokens") or 0,
        "turns": d.get("num_turns"),
        "cost_usd": d.get("total_cost_usd"),
    }


def _write(p, txt):
    with io.open(p, "w", encoding="utf-8") as fh:
        fh.write(txt)


# ----------------------------------------------------------------------
# 三階段
# ----------------------------------------------------------------------

def run(proj_dir, board, stage="all", model=None, runner=None, echo=print):
    """跑 A／B／C（或 all）。回傳 [(階段, usage)]，並寫 `work/usage.json`。"""
    d = os.path.join(proj_dir, PACK_DIR, board)
    if not os.path.isfile(os.path.join(d, "00_skeleton.md")):
        raise WriteError(u"沒有撰寫材料 %s——先跑 `ndd.py facts`" % d)
    w = os.path.join(d, WORK)
    if not os.path.isdir(w):
        os.makedirs(w)
    log = []

    def one(tag, prompt, out):
        txt, u = call(prompt, w, model, runner)
        _write(out, strip_fence(txt))
        echo(u"  %s：輸入 %d、輸出 %d token → %s" % (tag, u["input"], u["output"],
                                                 os.path.basename(out)))
        return tag, u

    if stage in ("A", "all"):
        log.append(one("A", prompt_a(board, d), os.path.join(w, "A_outline.md")))
    if stage in ("B", "all"):
        mats = sorted(f for f in os.listdir(d)
                      if f.endswith(".md") and f not in ("index.md", "00_skeleton.md"))
        groups = parse_groups(_read(os.path.join(w, "A_outline.md")), mats)
        for f in os.listdir(w):
            if re.match(r"B_\d+\.md$", f):
                os.remove(os.path.join(w, f))
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(groups)) as ex:
            futs = [ex.submit(one, "B%d" % n, prompt_b(board, d, n, fs),
                              os.path.join(w, "B_%d.md" % n)) for n, fs in groups]
            log.extend(f.result() for f in futs)
    if stage in ("C", "all"):
        bn = sorted((WORK + "/" + f for f in os.listdir(w)
                     if re.match(r"B_\d+\.md$", f)), key=lambda s: int(re.findall(r"\d+", s)[-1]))
        if not bn:
            raise WriteError(u"work/ 裡沒有 B_*.md——先跑 B 階段")
        log.append(one("C", prompt_c(board, d, bn),
                       os.path.join(proj_dir, "%s_Architecture.md" % board)))

    up = os.path.join(w, "usage.json")
    prev = json.loads(_read(up)) if os.path.isfile(up) else {}
    prev.update(dict(log))
    _write(up, json.dumps(prev, ensure_ascii=False, indent=1))
    tot_i = sum(u["input"] for u in prev.values())
    tot_o = sum(u["output"] for u in prev.values())
    echo(u"  合計（work/usage.json 內所有階段）：輸入 %d、輸出 %d token" % (tot_i, tot_o))
    return log
