# -*- coding: utf-8 -*-
"""撰寫 `<板>_Architecture.md`：**一次**無工具、單回合的 `claude -p`。

材料是 Facts 的全板部分（組成、子電路之間的連線、介面、電源摘要、未貼件）加
§11 訊號鏈摘要——夠 LLM 重建系統方塊與拓樸。逐腳細節不給：那是查證用的，
讀的人要時查 Facts 或 `ndd.py`。

沒有工具（`--tools ""`）、不載 MCP（`--strict-mcp-config`）：兩者的定義都算在
每次請求的 context 裡，而這裡用不到。規範在 `references/architecture-brief.md`，
當系統提示用。
"""
from __future__ import print_function

import io
import json
import os
import re
import shutil
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
BRIEF = os.path.join(os.path.dirname(HERE), "references", "architecture-brief.md")
TIMEOUT = 60 * 60
# 測試替換 `claude -p` 用（init 的測試會一路跑到這裡，不能真的呼叫）。
DEFAULT_RUNNER = None

_RX_FENCE = re.compile(r"^\s*```(?:markdown|md)?\s*\n(.*)\n```\s*$", re.S)


class WriteError(RuntimeError):
    pass


def prompt(board, material):
    return (u"板名：%s。依系統提示，輸出 `%s_Architecture.md` 的完整內容。\n\n"
            u"<file name=\"material.md\">\n%s\n</file>" % (board, board, material))


def strip_fence(txt):
    """模型偶爾把整份包在 ```markdown 裡——剝掉。"""
    m = _RX_FENCE.match(txt.strip())
    return (m.group(1) if m else txt.strip()) + u"\n"


def _claude():
    exe = shutil.which("claude")
    if not exe:
        raise WriteError(u"找不到 `claude` 指令（Claude Code CLI）")
    return exe


def call(text, cwd, model=None, runner=None):
    """-> (輸出文字, usage dict)。`runner` 給測試替換。"""
    runner = runner or DEFAULT_RUNNER
    if runner is not None:
        return runner(text)
    cmd = [_claude(), "-p", "--tools", "", "--strict-mcp-config",
           "--no-session-persistence", "--output-format", "json",
           "--system-prompt-file", BRIEF]
    if model:
        cmd += ["--model", model]
    r = subprocess.run(cmd, input=text.encode("utf-8"), cwd=cwd,
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
        "cost_usd": d.get("total_cost_usd"),
    }


def run(proj_dir, board, material, model=None, runner=None, echo=print):
    """寫出 `<板>_Architecture.md`，回傳 usage。"""
    # `--config ndd.json`（相對路徑）時專案目錄是 ""——當 cwd 會找不到。
    proj_dir = os.path.abspath(proj_dir or ".")
    txt, u = call(prompt(board, material), proj_dir, model, runner)
    out = os.path.join(proj_dir, "%s_Architecture.md" % board)
    with io.open(out, "w", encoding="utf-8") as fh:
        fh.write(strip_fence(txt))
    echo(u"  輸入 %d、輸出 %d token → %s" % (u["input"], u["output"],
                                         os.path.basename(out)))
    return u
