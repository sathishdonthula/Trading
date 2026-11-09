import json
import os
import re
import unittest

from _load import ROOT

SKILLS = ["india-scanner", "india-news", "india-entry", "india-exit", "india-timemachine",
          "india-risk", "india-budget", "india-broker", "india-alerts", "india-analyst",
          "india-insiders"]
ABOUT = ("11 Claude skills for day trading Indian stocks on NSE and BSE: scan thousands of charts, "
         "track corporate announcements, time every entry and exit, and replay every outcome before "
         "you risk a rupee.")
# skills on the way to an order carry the safety block
ORDER_PATH = ["india-scanner", "india-entry", "india-exit", "india-timemachine", "india-budget",
              "india-insiders", "india-risk", "india-broker", "india-alerts"]
US_ONLY = ["trader/plan.json", "trader/orders.json", "place_stock_order", "New York time"]


def frontmatter(path):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    return (m.group(1) if m else ""), text


def every_text_file():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for f in files:
            if f.endswith((".md", ".py", ".json", ".csv", ".txt", ".xml")) or f == "LICENSE":
                yield os.path.join(base, f)


class Repo(unittest.TestCase):
    def test_eleven_skill_folders_at_the_root(self):
        found = sorted(d for d in os.listdir(ROOT)
                       if os.path.isfile(os.path.join(ROOT, d, "SKILL.md")))
        self.assertEqual(found, sorted(SKILLS))

    def test_frontmatter_name_and_description(self):
        for s in SKILLS:
            fm, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            self.assertIn("name: %s\n" % s, fm + "\n", s)
            self.assertRegex(fm, r"description: ", s)
            self.assertGreater(len(fm), 200, "%s description is too thin" % s)
            self.assertGreater(len(text.splitlines()), 60, s)

    def test_order_path_skills_carry_the_safety_rule(self):
        for s in ORDER_PATH:
            _, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            low = " ".join(text.lower().split())
            self.assertIn("not financial advice", low, s)
            self.assertIn("yes", low, s)

    def test_plugin_manifest_points_at_the_root_skills(self):
        with open(os.path.join(ROOT, ".claude-plugin", "plugin.json")) as fh:
            plugin = json.load(fh)
        self.assertEqual(plugin["name"], "india-day-trader-skill")
        self.assertEqual(plugin["skills"], "./")
        self.assertEqual(plugin["description"], ABOUT)
        with open(os.path.join(ROOT, ".claude-plugin", "marketplace.json")) as fh:
            market = json.load(fh)
        self.assertEqual(market["name"], "india-day-trader-skill")
        self.assertEqual(market["plugins"][0]["name"], "india-day-trader-skill")

    def test_readme_lists_every_skill(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            readme = " ".join(fh.read().split())
        for line in (ABOUT, "## Fine print", "not financial advice"):
            self.assertIn(line.lower(), readme.lower())
        for s in SKILLS:
            self.assertIn("/" + s, readme, s)

    def test_no_em_dashes(self):
        for path in every_text_file():
            rel = os.path.relpath(path, ROOT)
            if rel.startswith(os.path.join("tests", "fixtures")):  # fixtures are real data
                continue
            with open(path, encoding="utf-8", errors="replace") as fh:
                self.assertNotIn("\u2014", fh.read(), rel)

    def test_no_us_pack_paths_left_in_the_skills(self):
        for s in SKILLS:
            folder = os.path.join(ROOT, s)
            for f in os.listdir(folder):
                with open(os.path.join(folder, f), encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
                for old in US_ONLY:
                    self.assertNotIn(old, text, "%s/%s: %s" % (s, f, old))

    def test_every_tool_runs_on_python_3_8(self):
        for s in SKILLS:
            folder = os.path.join(ROOT, s)
            for f in os.listdir(folder):
                if not f.endswith(".py"):
                    continue
                with open(os.path.join(folder, f), encoding="utf-8") as fh:
                    src = fh.read()
                for bad in ("import zoneinfo", "from zoneinfo", ".removeprefix(", ".removesuffix(",
                            "list[", "dict[", "tuple["):
                    if bad.endswith("["):
                        self.assertNotRegex(src, r"(?<![\w.])%s" % re.escape(bad), "%s/%s" % (s, f))
                    else:
                        self.assertNotIn(bad, src, "%s/%s" % (s, f))
                self.assertNotRegex(src, r"(?m)^\s+match \w+.*:$", "%s/%s" % (s, f))

    def test_day_trading_limits_ship_in_the_example_config(self):
        with open(os.path.join(ROOT, "india-budget", "config.example.json")) as fh:
            cfg = json.load(fh)
        for k in ("max_loss_per_trade_inr", "max_daily_loss_inr", "max_trades_per_day",
                  "min_reward_risk", "require_stop", "no_entries_after", "flat_by"):
            self.assertIn(k, cfg, k)
        self.assertIs(cfg["require_stop"], True)
        self.assertIs(cfg.get("allow_short", False), False)
        self.assertIs(cfg.get("allow_margin", False), False)
        # "auto" = 15:00 for CAS (F&O) stocks, 15:10 for the rest; a fixed time must beat
        # Zerodha's earliest MIS auto square-off (15:12 for CAS stocks)
        self.assertTrue(cfg["flat_by"] == "auto" or cfg["flat_by"] < "15:12", cfg["flat_by"])

    def test_paper_trading_is_the_default(self):
        with open(os.path.join(ROOT, "india-budget", "config.example.json")) as fh:
            cfg = json.load(fh)
        self.assertIs(cfg["paper"], True)
        self.assertIs(cfg.get("allow_options", False), False)


if __name__ == "__main__":
    unittest.main()
