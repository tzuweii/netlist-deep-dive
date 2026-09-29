#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`ndd_write`：三階段撰寫的派工。真正的 `claude -p` 呼叫以 runner 替換。

⚠️ 最容易悄悄壞掉的是 **A 的分組**：漏分一份材料不會噴錯，只會讓那幾個方塊
   在文件裡消失。所以分組不完整一律停下來，不替 A 補。
"""
import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import ndd_write as W                                          # noqa: E402

GROUPS = u"""# 大綱

```groups
1: 01_A.md, 01_B.md
2: 02_C.md, 00_power.md
```
"""


class WriteTest(unittest.TestCase):
    def setUp(self):
        self.proj = tempfile.mkdtemp(prefix="ndd_write_")
        self.d = os.path.join(self.proj, W.PACK_DIR, "b")
        os.makedirs(self.d)
        for f in ("index.md", "00_skeleton.md", "00_topology.md", "00_power.md",
                  "01_A.md", "01_B.md", "02_C.md"):
            with io.open(os.path.join(self.d, f), "w", encoding="utf-8") as fh:
                fh.write(u"內容 %s\n" % f)
        self.calls = []

    def runner(self, prompt):
        self.calls.append(prompt)
        if u"A 階段" in prompt:
            txt = GROUPS
        elif u"C 階段" in prompt:
            txt = u"```markdown\n# 文件\n```"
        else:
            txt = u"# 分塊"
        return txt, {"input": len(prompt), "output": 10, "turns": 1,
                     "cost_usd": 0}

    def test_all_stages_write_files_and_usage(self):
        W.run(self.proj, "b", runner=self.runner, echo=lambda *a: None)
        w = os.path.join(self.d, W.WORK)
        self.assertTrue(os.path.isfile(os.path.join(w, "B_1.md")))
        self.assertTrue(os.path.isfile(os.path.join(w, "B_2.md")))
        with io.open(os.path.join(self.proj, "b_Architecture.md"),
                     encoding="utf-8") as fh:
            self.assertEqual(fh.read(), u"# 文件\n")      # ``` 外框剝掉
        with io.open(os.path.join(w, "usage.json"), encoding="utf-8") as fh:
            self.assertEqual(sorted(json.load(fh)), ["A", "B1", "B2", "C"])
        self.assertEqual(len(self.calls), 4)

    def test_b_gets_only_its_own_files(self):
        """每組只附自己的分塊檔——附全部就又回到整份材料重送。"""
        W.run(self.proj, "b", runner=self.runner, echo=lambda *a: None)
        b1 = [c for c in self.calls if u"第 1 組" in c][0]
        self.assertIn(u"<file name=\"01_B.md\">", b1)
        self.assertNotIn(u"<file name=\"02_C.md\">", b1)
        self.assertIn(W.MODEL, b1)
        # 總覽只給 A 與 C，不分給 B。
        self.assertNotIn(u"<file name=\"00_topology.md\">", b1)

    def test_a_sees_overview_and_topology(self):
        """A 要從總覽與拓樸骨架重建架構——兩份都要附上。"""
        W.run(self.proj, "b", stage="A", runner=self.runner, echo=lambda *a: None)
        a = self.calls[0]
        self.assertIn(u"<file name=\"00_skeleton.md\">", a)
        self.assertIn(u"<file name=\"00_topology.md\">", a)
        self.assertNotIn(u"<file name=\"01_A.md\">", a)

    def test_c_gets_model_all_b_and_skeleton(self):
        W.run(self.proj, "b", runner=self.runner, echo=lambda *a: None)
        c = [x for x in self.calls if u"C 階段" in x][0]
        for f in (u"00_skeleton.md", u"work/" + W.MODEL, u"work/B_1.md", u"work/B_2.md"):
            self.assertIn(u"<file name=\"%s\">" % f, c)
        self.assertNotIn(u"<file name=\"01_A.md\">", c)

    def test_topology_is_not_a_b_material(self):
        """`00_topology*.md` 只給 A 看，不在分組範圍內——A 沒分它不可報「漏分」。"""
        with io.open(os.path.join(self.d, "00_topology_2.md"), "w",
                     encoding="utf-8") as fh:
            fh.write(u"續\n")
        self.assertEqual(W.overview_files(self.d),
                         ["00_skeleton.md", "00_topology.md", "00_topology_2.md"])
        W.run(self.proj, "b", runner=self.runner, echo=lambda *a: None)

    def test_groups_must_cover_every_material(self):
        with self.assertRaises(W.WriteError):
            W.parse_groups(u"```groups\n1: 01_A.md\n```",
                           ["01_A.md", "01_B.md"])

    def test_groups_reject_duplicates_and_unknown(self):
        with self.assertRaises(W.WriteError):
            W.parse_groups(u"```groups\n1: 01_A.md\n2: 01_A.md\n```", ["01_A.md"])
        with self.assertRaises(W.WriteError):
            W.parse_groups(u"```groups\n1: 99_X.md\n```", ["01_A.md"])

    def test_groups_accept_backticks_and_cjk_comma(self):
        g = W.parse_groups(u"```groups\n1：`01_A.md`、01_B.md\n```",
                           ["01_A.md", "01_B.md"])
        self.assertEqual(g, [(1, ["01_A.md", "01_B.md"])])


if __name__ == "__main__":
    unittest.main()
