#!/usr/bin/env python3
"""Open a GitHub issue for each source that has been failing for a while, and close it once it loads again.

Runs on GitHub after each build (see .github/workflows/deploy.yml), reading the failures each site's build
recorded in the JSON it publishes:

    python3 -m shared.alerts news=dist/news/feeds.json events=dist/events/listings.json

GitHub emails the repo's owner about new issues, so a source that breaks for good doesn't go unnoticed. A
short hiccup doesn't count: a source must fail every build for ALERT_AFTER first. Only the sites built this
run are checked; one that wasn't (the events page skips most runs) keeps its issues as they are.
"""

import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ALERT_AFTER = timedelta(hours=6)
TITLE = re.compile(r"Source failing: (?P<name>.+) \((?P<site>\w+)\)$")


def gh(*args):
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def describe(name, site, failure, now):
    hours = round((now - datetime.fromisoformat(failure["since"])).total_seconds() / 3600)
    fix = "its line in news/feeds.txt" if site == "news" else "its reader in events/build.py or its line in events/sources.txt"
    return (
        f"{name}, on the {site} page, has failed every build for about {hours} hours, since "
        f"{failure['since'][:16].replace('T', ' ')} UTC.\n\n"
        f"Last error: `{failure['error']}`\n\n"
        "Until it loads again, the page shows its last good copy for up to two days, then says it couldn't load "
        f"it. If the site changed, update {fix}. This issue closes itself once it loads again."
    )


def main():
    failing = {}  # (site, name) → failure, for each site built this run.
    for argument in sys.argv[1:]:
        site, path = argument.split("=", 1)
        if Path(path).exists():
            for name, failure in json.loads(Path(path).read_text()).get("failing", {}).items():
                failing[(site, name)] = failure
        else:
            print(f"{path} wasn't written, so the {site} page wasn't checked (not built this run, or its build failed).")
    checked = {argument.split("=", 1)[0] for argument in sys.argv[1:] if Path(argument.split("=", 1)[1]).exists()}
    now = datetime.now(timezone.utc)

    listed = json.loads(gh("issue", "list", "--state", "open", "--search", '"Source failing" in:title',
                           "--json", "number,title", "--limit", "100"))
    open_issues = {}
    for issue in listed:
        match = TITLE.match(issue["title"])
        if match:
            open_issues[(match["site"], match["name"])] = issue["number"]

    for (site, name), failure in failing.items():
        if (site, name) not in open_issues and now - datetime.fromisoformat(failure["since"]) >= ALERT_AFTER:
            gh("issue", "create", "--title", f"Source failing: {name} ({site})", "--body", describe(name, site, failure, now))
            print(f"Opened an issue: {name} ({site}) has been failing since {failure['since']}")
    for (site, name), number in open_issues.items():
        if site in checked and (site, name) not in failing:
            gh("issue", "close", str(number), "--comment", f"{name} is loading again.")
            print(f"Closed the issue for {name} ({site}): it's loading again")


if __name__ == "__main__":
    main()
