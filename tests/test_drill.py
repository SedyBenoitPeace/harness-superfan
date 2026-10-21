"""Run from the code folder: python3 -m unittest discover tests"""

import unittest

from superfan.drill import short_paths, what, drill, output_hint


def rec(sample, tokens=1000, after=10, cat="reading files", tool="Bash", agent_id="main", role="orchestrator", label=""):
    return {"tool": tool, "sample": sample, "label": label or sample.split()[0], "category": cat, "tokens": tokens,
            "calls_after": after, "carried": tokens * after, "carried_usd": None, "agent": role, "agent_id": agent_id,
            "role": role}


class Paths(unittest.TestCase):
    def test_absolute_and_quoted_paths_keep_two_parts(self):
        self.assertEqual(short_paths("cat '/Users/someone/Cloud Drive/proj/AGENTS.md'"), "cat '…/proj/AGENTS.md'")
        self.assertEqual(short_paths("sed -n 1,9p /home/u/src/a/b.ts && cat ~/x/y/z.md"),
                         "sed -n 1,9p …/a/b.ts && cat …/y/z.md")

    def test_relative_paths_and_urls_unchanged(self):
        self.assertEqual(short_paths("sed -n 1,240p app/page.tsx"), "sed -n 1,240p app/page.tsx")
        self.assertEqual(short_paths("curl https://example.com/a/b/c"), "curl https://example.com/a/b/c")


class What(unittest.TestCase):
    def test_sed_range_names_the_file(self):
        w = what(rec("sed -n '220,600p' app/page.tsx; sed -n '600,900p' app/page.tsx"))
        self.assertEqual((w["key"], w["verb"], w["lines"]), ("app/page.tsx", "sed", 381))

    def test_grep_context(self):
        w = what(rec("grep -n -A14 'id' FEATURES.json", label='grep "id"'))
        self.assertEqual((w["key"], w["context"]), ('grep "id"', 14))

    def test_read_tool(self):
        self.assertEqual(what(rec("/repo/src/deep/db/drive.ts", tool="Read"))["key"], "…/db/drive.ts")


class Drill(unittest.TestCase):
    def test_re_reads_agents_and_hints(self):
        outs = [rec("cat app/page.tsx", 9000, 50), rec("sed -n 1,80p app/page.tsx", 2000, 40),
                rec("cat app/page.tsx", 9000, 30), rec("cat app/page.tsx", 9000, 5, agent_id="w1", role="worker"),
                rec("cat AGENTS.md", 3000, 20, cat="skills and agent docs"),
                rec("npm test", 20000, 10, cat="package and test runners")]
        d = drill(outs)
        page = d["reading files"]["items"][0]
        self.assertEqual((page["what"], page["calls"], page["re_reads"], len(page["agents"])), ("app/page.tsx", 4, 2, 2))
        self.assertIn("read this file 3×", page["hint"])
        self.assertEqual(d["reading files"]["by_role"], {"orchestrator": 9000 * 80 + 2000 * 40, "worker": 45000})
        self.assertIn("Loaded once", d["skills and agent docs"]["items"][0]["hint"])
        self.assertIn("quiet reporter", output_hint(outs[-1]))

    def test_small_outputs_get_no_hint(self):
        self.assertEqual(output_hint(rec("git status", 200, 5, cat="git")), "")


if __name__ == "__main__":
    unittest.main()
