"""Tests for shared/: the page both sites are built on, and the alerts. Run from the repo's top folder with:
python3 -m unittest"""

import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared import alerts, site  # noqa: E402


class Page(unittest.TestCase):
    def test_header_names_both_pages_marking_this_one(self):
        news = site.page("news", "Newsfeed", "<ul></ul>", updated=datetime(2026, 9, 12, tzinfo=timezone.utc))
        events = site.page("events", "Events", "<ul></ul>")
        # Events is Pushpin Boston now; the page that used to be here sends a reader on to it.
        self.assertIn('<a href="./" aria-current="page">Newsfeed</a><a href="https://pushpin.city/boston/">Events</a>', news)
        self.assertIn('<a href="https://news.grahamhagenah.com/">Newsfeed</a><a href="./" aria-current="page">Events</a>', events)
        self.assertIn('Updated <time class="updated" datetime="2026-09-12T00:00:00+00:00">', news)
        self.assertNotIn('<span class="header-note">', events, "no updated time given, no note")

    def test_each_site_adds_its_own_styles_and_icons(self):
        page = site.page("events", "Events", "", css=".mine { color: red; }", symbols=site.icon_symbols({"film": "<rect/>"}))
        self.assertLess(page.index(".row {"), page.index(".mine {"), "a site's styles come after the shared ones")
        self.assertIn('<symbol id="icon-film"', page)
        self.assertIn('content="Events"', page)

    def test_a_way_back_to_the_top(self):
        page = site.page("events", "Events", "<ul></ul>")
        self.assertIn('<button class="to-top" type="button" aria-label="Back to top">', page)
        self.assertLess(page.index("</main>"), page.index('class="to-top"'), "outside the page's content")

    def test_menu(self):
        menu = site.menu({"The Sinclair", "Brattle", "Arts at the Armory", "Club Passim"}, "All venues", "Venue")
        self.assertEqual(re.findall(r'<option value="([^"]*)">', menu), ["", "Arts at the Armory", "Brattle", "Club Passim", "The Sinclair"],
                         "everything first, then as they'd be said: The Sinclair among the S's")
        self.assertIn('<select class="pick" aria-label="Venue"><option value="">All venues</option>', menu)

    def test_icons(self):
        self.assertIn('aria-label="Film"', site.icon("film", "Film"))
        self.assertIn('aria-hidden="true"', site.icon("film"))


class Alerts(unittest.TestCase):
    def run_alerts(self, records, open_titles):
        """Runs alerts.py on these sites' failures, with these issues open; returns what it opened and closed."""
        calls, arguments, files = [], [], []
        for site_name, failing in records.items():
            if failing is None:  # Not built this run.
                arguments.append(f"{site_name}=/nonexistent/{site_name}.json")
                continue
            record = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
            json.dump({"failing": failing}, record)
            record.close()
            files.append(record.name)
            arguments.append(f"{site_name}={record.name}")
        issues = [{"number": number, "title": title} for number, title in enumerate(open_titles, start=1)]

        def gh(*args):
            calls.append(args)
            return json.dumps(issues) if args[:2] == ("issue", "list") else ""

        with mock.patch.object(alerts, "gh", gh), mock.patch.object(sys, "argv", ["alerts", *arguments]):
            alerts.main()
        for name in files:
            os.unlink(name)
        opened = [args[3] for args in calls if args[:2] == ("issue", "create")]
        closed = [issues[int(args[2]) - 1]["title"] for args in calls if args[:2] == ("issue", "close")]
        return opened, closed

    def failure(self, hours):
        return {"since": (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(), "error": "HTTP Error 520"}

    def test_opens_after_six_hours_only(self):
        opened, _ = self.run_alerts({"news": {"Kottke": self.failure(7), "Waxy.org": self.failure(1)}}, [])
        self.assertEqual(opened, ["Source failing: Kottke (news)"])

    def test_doesnt_open_twice(self):
        opened, closed = self.run_alerts({"news": {"Kottke": self.failure(9)}}, ["Source failing: Kottke (news)"])
        self.assertEqual((opened, closed), ([], []))

    def test_closes_once_it_loads_again(self):
        _, closed = self.run_alerts({"news": {}}, ["Source failing: Volts (news)"])
        self.assertEqual(closed, ["Source failing: Volts (news)"])

    def test_leaves_a_site_that_wasnt_built_alone(self):
        opened, closed = self.run_alerts(
            {"news": {}, "events": None},
            ["Source failing: Coolidge Corner (events)", "Source failing: Volts (news)"],
        )
        self.assertEqual((opened, closed), ([], ["Source failing: Volts (news)"]))


if __name__ == "__main__":
    unittest.main()


class Scripts(unittest.TestCase):
    """The pages' scripts are written inside Python strings, where an escape like \\n turns into a real line
    break; one that no longer parses breaks the whole page (its filter, search and paging) without any other
    test noticing. Node, where it's installed (as on GitHub's runners), checks each one parses."""

    def test_page_scripts_parse(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node isn't installed")
        # Whichever pages this copy has: the newsfeed on its own (news/template.py) has no events page.
        scripts = {}
        for page in ("events", "news"):
            try:
                scripts[page] = importlib.import_module(f"{page}.build").INDEX_JS
            except ModuleNotFoundError:
                continue
        for name in ("AGES", "PREVIEWS", "SEARCH_KEYS", "TO_TOP"):
            scripts[name] = "".join(re.findall(r"<script>(.*?)</script>", getattr(site, name), re.S))
        for name, script in scripts.items():
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as file:
                file.write(script)
            result = subprocess.run([node, "--check", file.name], capture_output=True, text=True)
            os.unlink(file.name)
            self.assertEqual(result.returncode, 0, f"{name}: {result.stderr}")

