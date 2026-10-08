#!/usr/bin/env python3
"""Add or remove a site in feeds.txt, from a GitHub issue titled "Add <url> [name] [options]" or "Remove <url>".

The sources page opens those issues pre-filled; .github/workflows/sources.yml runs this with the title in
$ISSUE_TITLE. Writes the reply for the issue to reply.md, and changed= and summary= to $GITHUB_OUTPUT.
"""

import os
import re
from pathlib import Path

from news import build

ADDED_HEADING = "# Added from the sources page"


def same_site(a, b):
    def normalize(url):
        return re.sub(r"^https?://(www\.)?", "", url.strip().lower()).rstrip("/")

    return normalize(a) == normalize(b)


def add(line, lines):
    if not re.match(r"https?://", line, re.I):
        line = "https://" + line
    site = build.parse_site(line)
    if any(same_site(build.parse_site(other)["url"], site["url"]) for other in lines if build.is_site_line(other)):
        return False, f"{site['url']} is already in feeds.txt.", ""

    feed = build.load(site)
    if "error" in feed:
        return (
            False,
            f"Couldn’t find a feed at {site['url']} ({feed['error']}).\n\n"
            "If the site has an RSS or Atom link, add a site again using that link instead.",
            "",
        )

    if ADDED_HEADING not in lines:
        lines += ["", ADDED_HEADING]
    lines.append(line)

    count = len(feed["posts"])
    if count:
        found = f"Found {count} post{'' if count == 1 else 's'} from the last {site['days']} days"
    else:
        found = (
            f"It hasn’t posted in the last {site['days']} days, so it’ll show up when it next does. "
            "For a site that posts rarely, add days=14 to the end of its line in feeds.txt"
        )
    return (
        True,
        f"Added **{feed['name']}**. {found}, from {feed['feed_url']}.\n\n"
        "It’ll be on the page in a minute or two.",
        f"Add {feed['name']}",
    )


def remove(url, lines):
    url = url.split()[0]
    for i, line in enumerate(lines):
        if build.is_site_line(line) and same_site(build.parse_site(line)["url"], url):
            site = build.parse_site(line)
            name = site["name"] or site["url"]
            del lines[i]
            return True, f"Removed **{name}**. It’ll be gone from the page in a minute or two.", f"Remove {name}"
    return False, f"{url} isn’t in feeds.txt, so there was nothing to remove.", ""


def main():
    title = os.environ.get("ISSUE_TITLE", "").strip()
    action, _, rest = title.partition(" ")
    rest = rest.strip()
    lines = build.FEEDS_FILE.read_text().splitlines()

    if not rest:
        changed, reply, summary = False, "The title needs a site’s URL after “Add” or “Remove”.", ""
    elif action.lower() == "remove":
        changed, reply, summary = remove(rest, lines)
    else:
        changed, reply, summary = add(rest, lines)

    if changed:
        build.FEEDS_FILE.write_text("\n".join(lines) + "\n")
    Path("reply.md").write_text(reply + "\n")
    print(reply)
    with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a") as output:
        output.write(f"changed={'true' if changed else 'false'}\n")
        output.write(f"summary={summary}\n")


if __name__ == "__main__":
    main()
