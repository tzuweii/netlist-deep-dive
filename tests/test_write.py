#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`ndd_write`：一次 `claude -p` 寫 Architecture。真正的呼叫以 runner 替換。"""
import io
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import ndd_write as W                                          # noqa: E402


class WriteTest(unittest.TestCase):
    def setUp(self):
        self.proj = tempfile.mkdtemp(prefix="ndd_write_")
        self.calls = []

    def runner(self, prompt):
        self.calls.append(prompt)
        return u"```markdown\n# 文件\n```", {"input": len(prompt), "output": 10,
                                             "cost_usd": 0}

    def test_one_call_writes_architecture(self):
        u = W.run(self.proj, "b", u"# 材料\n子電路樹", runner=self.runner,
                  echo=lambda *a: None)
        self.assertEqual(len(self.calls), 1)
        self.assertIn(u"<file name=\"material.md\">", self.calls[0])
        self.assertIn(u"子電路樹", self.calls[0])
        with io.open(os.path.join(self.proj, "b_Architecture.md"),
                     encoding="utf-8") as fh:
            self.assertEqual(fh.read(), u"# 文件\n")      # ``` 外框剝掉
        self.assertEqual(u["output"], 10)

    def test_brief_exists_and_is_short(self):
        """規範是系統提示，每次呼叫都送——保持精簡，只講原則不講模板。"""
        with io.open(W.BRIEF, encoding="utf-8") as fh:
            txt = fh.read()
        self.assertIn(u"方塊圖", txt)
        self.assertLess(len(txt.encode("utf-8")), 6000)


if __name__ == "__main__":
    unittest.main()
