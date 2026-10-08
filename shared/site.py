"""What the newsfeed and the events page share: fetching, the fallback and alert bookkeeping, the icon system,
the hover previews, and the page itself around each one's list (head, header, base styles, and the scripts
that age timestamps and place previews).
Each site's build.py adds its own readers, rows and styles on top."""

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

# The two pages, in the order the header names them.
SITES = {
    "news": ("Newsfeed", "https://news.grahamhagenah.com/"),
    "events": ("Events", os.environ.get("EVENTS_URL", "").strip() or "https://pushpin.city/boston/"),
}


def fetch(url, user_agent, attempts=3, timeout=20):
    """The address the request ended at, after redirects, and the response body."""
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.geturl(), response.read()
        except Exception as error:
            # Timeouts, dropped connections and 5xx errors (like the 520s Cloudflare gives when a site's own
            # server stumbles) are often momentary; a 404 won't change, except from YouTube's channel feeds,
            # which answer 404 now and then for a feed that's fine. Wait a little longer each time.
            momentary = (not isinstance(error, urllib.error.HTTPError) or error.code >= 500
                         or (error.code == 404 and "youtube.com/feeds/" in url))
            if not momentary or attempt == attempts - 1:
                raise
            time.sleep(2 * (attempt + 1))


def previous_build(url, user_agent):
    """The JSON the live page published last build: its sources' items, for falling back on, and which were
    failing. Empty if it can't be had."""
    try:
        return json.loads(fetch(url, user_agent)[1])
    except Exception as error:
        print(f"  no earlier build to fall back on ({error})", file=sys.stderr)
        return {}


def still_failing(errors, previous, built_at):
    """Each failing source's error and when it started failing, carried over from build to build, so
    alerts.py can tell a hiccup from an outage."""
    before = previous.get("failing", {})
    return {
        name: {"since": before.get(name, {}).get("since", built_at.isoformat()), "error": error}
        for name, error in errors.items()
    }


def icon_symbols(drawings):
    """Each icon drawn once, at the top of the page, for rows to point to. Drawings are 16×16 strokes."""
    return '<svg class="symbols" aria-hidden="true">' + "".join(
        f'<symbol id="icon-{name}" viewBox="0 0 16 16"><g fill="none" stroke="currentColor" stroke-width="1.5" '
        f'stroke-linecap="round" stroke-linejoin="round">{drawing}</g></symbol>'
        for name, drawing in drawings.items()
    ) + "</svg>"


def icon(name, label=None):
    """An icon; with no label, where the text beside it already says it, screen readers skip it."""
    described = f'role="img" aria-label="{html.escape(label)}"' if label else 'aria-hidden="true"'
    return f'<svg class="icon" {described}><use href="#icon-{name}"/></svg>'


def clean(text):
    """HTML or text as one line of plain text."""
    text = re.sub(r"<[^>]+>", "", html.unescape(text))
    text = re.sub(r"\s+", " ", text).strip()
    # Some feeds leave gaps where links were stripped: "“ Gimme", "essay , posted".
    return re.sub(r"([“(\[]) | ([,.;:!?)\]”])", r"\1\2", text)


BLOCK_TAG = re.compile(r"</?(p|div|blockquote|li|ul|ol|h[1-6]|br|pre|table|tr)\b[^>]*>", re.I)
BARE_LINKS = re.compile(r"[\s,]*(https?://\S+[\s,]*)+")


def excerpt(markup, max_chars=600, skip=None, min_words=4, max_paragraphs=3):
    """The first few paragraphs of an HTML snippet, as plain text, cut to about max_chars. Paragraphs of fewer
    than min_words (lone links, handles, "Thanks!") and any matching skip are left out."""
    markup = re.sub(r"(?is)<(script|style|figure)\b.*?</\1>", " ", markup or "")
    paragraphs = [clean(part) for part in re.split(r"\n\s*\n", BLOCK_TAG.sub("\n\n", markup))]
    kept = []
    budget = max_chars
    for paragraph in paragraphs:
        if len(paragraph.split()) < min_words or (skip and skip.match(paragraph)) or BARE_LINKS.fullmatch(paragraph):
            continue
        if len(paragraph) > budget:
            kept.append(paragraph[:budget].rsplit(" ", 1)[0] + "…")
            break
        kept.append(paragraph)
        budget -= len(paragraph)
        if len(kept) == max_paragraphs:
            break
    return kept


# The search box, at the end of a page's filter row. Each page's own script decides what it matches.
SEARCH = '<input class="search" type="search" placeholder="Search" aria-label="Search" autocomplete="off" spellcheck="false">'


def as_said(names):
    """Names in the order they'd be said, "The Sinclair" among the S's."""
    return sorted(names, key=lambda name: re.sub(r"^the\s+", "", name, flags=re.I).casefold())


def menu(names, everything, label):
    """A menu for the filter's row, beside the search: everything, or just one of names, sorted as they'd be
    said. everything is what the first choice says ("All venues")."""
    ordered = as_said(names)
    options = "".join(f'<option value="{html.escape(name)}">{html.escape(name)}</option>' for name in ordered)
    return (f'<select class="pick" aria-label="{html.escape(label)}"><option value="">{html.escape(everything)}</option>'
            f"{options}</select>")


def preview(title, paragraphs):
    """The box that opens under a headline on hover: the full headline, shown only when the one-line one is cut
    off, then the item's first paragraphs. It goes right after the headline's title, which the hover is on."""
    body = "".join(f"<p>{html.escape(paragraph)}</p>" for paragraph in paragraphs)
    return (f'<div class="preview{"" if body else " title-only"}">'
            f'<p class="full-title">{html.escape(title)}</p>{body}</div>')


# The page around a list: its ground, its width, and the header at the top of it. A page written without the
# rest of these styles (pushpin.city's own, which lists its cities) takes these, so its header is the same.
HEADER_CSS = """
  html { background: #000; }
  /* Nothing scrolls the page sideways on a phone, whatever runs wide. */
  body { margin: 0; padding: 2rem 1.25rem 4rem; overflow-x: clip; color: #fff; background: #000;
         font: 17px/1.45 -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif; }
  main { max-width: 46rem; margin: 0 auto; }
  header { display: flex; justify-content: space-between; align-items: center; gap: 1rem; margin-bottom: 2rem; }
  .header-note { color: #666; font-size: .8rem; white-space: nowrap; }
  .header-note time { margin: 0; font-size: inherit; }
  /* The newsfeed and the events page, as a pair: the one you're on in white, the other a gray link to it. */
  .sites { display: flex; gap: .9rem; }
  .sites a, .sites a:visited { color: #555; font-size: 1.15rem; font-weight: 700; letter-spacing: -.01em; text-decoration: none; }
  .sites a:hover { color: #999; }
  .sites a[aria-current], .sites a[aria-current]:hover { color: #fff; }
"""

BASE_CSS = HEADER_CSS + """
  /* Its choices at the top of the row, as a link sits, so a page whose choices are buttons (which center
     their text in the row's height, set by the menu and search) puts them at the same height as one of links. */
  .filter { display: flex; flex-wrap: wrap; align-items: flex-start; gap: .4rem 1.1rem; margin: -.75rem 0 1.75rem; }
  .filter button, .filter a { padding: 0; border: 0; background: none; color: #666; font: inherit; font-size: .8rem;
                              text-decoration: none; cursor: pointer; }
  .filter button:hover, .filter a:hover { color: #999; text-decoration: none; }
  .filter button[aria-pressed="true"], .filter a[aria-current="page"] { color: #fff; }
  .filter .icon { margin-right: .4em; vertical-align: -1px; }
  @media (max-width: 34rem) { .filter { column-gap: .9rem; } .filter button, .filter a { font-size: .95rem; } }
  /* Whatever the filter and pager hide stays hidden, however specific the rules that lay it out. */
  [hidden] { display: none !important; }
  ul { margin: 0; padding: 0; list-style: none; }
  /* Rows: the headline first, led by its kind's icon and one line tall, with its details after it, and where it's
     from at the end of the row, where those line up down the page to be scanned. The parts are set down with
     spaces between them, which the columns ignore and a phone's line of text breaks at. */
  .row { position: relative; display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: .6rem; align-items: baseline;
         padding: .4rem 0; }
  .row > .icon { align-self: center; }
  .symbols { position: absolute; width: 0; height: 0; overflow: hidden; }
  .icon { flex: none; width: 12px; height: 12px; }
  .source { display: flex; justify-content: flex-end; align-items: center; min-width: 0; max-width: 14rem; color: #666;
            font-size: .8em; text-align: right; }
  .source > span { min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
  .headline { display: flex; align-items: baseline; min-width: 0; position: relative; }
  .headline .title { min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
  /* A headline's preview, opening under it (or over it, near the bottom of the window) after a short hover. */
  .preview { position: absolute; z-index: 1; top: calc(100% + .5rem); left: -1rem; width: min(34rem, calc(100% + 1rem));
             box-sizing: border-box; padding: .9rem 1rem; background: #000; border: 1px solid #333; border-radius: 6px;
             color: #bbb; font-size: .85em; font-weight: normal; line-height: 1.5; white-space: normal; pointer-events: none;
             visibility: hidden; opacity: 0; transition: opacity .1s, visibility 0s .1s; }
  .preview.above { top: auto; bottom: calc(100% + .5rem); }
  .preview p { margin: 0 0 .7em; }
  .preview p:last-child { margin-bottom: 0; }
  .preview .full-title { display: none; color: #fff; }
  .truncated .preview .full-title { display: block; }
  .headline:not(.truncated) .preview.title-only { display: none; }
  .headline .title:hover ~ .preview { visibility: visible; opacity: 1; transition: opacity .1s .4s, visibility 0s .4s; }
  @media (hover: none), (max-width: 34rem) { .preview { display: none; } }
  /* The search, at the end of the filter's row and as quiet as the filter. */
  .search { margin-left: auto; width: 10rem; padding: 0 0 .15rem; border: 0; border-bottom: 1px solid #333;
            border-radius: 0; background: none; color: #fff; font: inherit; font-size: .8rem; outline: none;
            -webkit-appearance: none; appearance: none; }
  .search::placeholder { color: #555; }
  .search:focus { border-bottom-color: #777; }
  .search::-webkit-search-cancel-button { display: none; }
  /* A menu beside it (the events' venues, the newsfeed's sources), as quiet as the filter: a small arrow
     after it, and white once something's chosen. As wide as what's chosen, where the browser can size it so. */
  .pick { max-width: 12rem; padding: 0 1.05em 0 0; border: 0; border-radius: 0; color: #666;
          background: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 10 6'%3E%3Cpath d='M1 1l4 4 4-4' fill='none' stroke='%23666' stroke-width='1.4' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E") no-repeat right center / .6em;
          font: inherit; font-size: .8rem; text-overflow: ellipsis; cursor: pointer; field-sizing: content;
          -webkit-appearance: none; appearance: none; }
  .pick:hover { color: #999; }
  .pick.chosen { color: #fff; }
  .pick:focus-visible { outline: 1px solid #444; outline-offset: 3px; }
  .pick option { background: #000; color: #fff; }
  /* The menu and the search, together at the end of the filter's row. On a phone they're on a line of their
     own under it, from the left, the search taking the rest of it, at 16px, since iPhones zoom in on a smaller
     field when it's tapped. Neither may push the line wider than the screen: a long name ends in "…" past 60%
     of it, and the search takes whatever's left. */
  .finders { display: flex; align-items: baseline; gap: 1.4rem; margin-left: auto; }
  .finders .search { margin-left: 0; }
  @media (max-width: 34rem) {
    .finders { flex-basis: 100%; min-width: 0; gap: 1.1rem; margin: .4rem 0 0; }
    .finders .search { flex: 1 1 0; min-width: 3rem; width: 0; font-size: 1rem; }
    .pick { flex: 0 1 auto; min-width: 0; max-width: 60%; font-size: 1rem; }
  }
  .pager { display: flex; justify-content: space-between; margin-top: 2.5rem; color: #666; font-size: .8rem; }
  .pager a, .pager a:visited { color: #999; }
  .empty { color: #666; font-size: .9rem; }
  /* Back to the top: a quiet arrow in the corner, faded in once the page has been scrolled a screen or so. */
  .to-top { position: fixed; z-index: 2; right: max(1.5rem, env(safe-area-inset-right)); bottom: max(1.5rem, env(safe-area-inset-bottom));
            display: grid; place-items: center; width: 2.25rem; height: 2.25rem; padding: 0; border: 1px solid #262626;
            border-radius: 50%; background: rgba(0, 0, 0, .8); color: #777; cursor: pointer;
            opacity: 0; visibility: hidden; transition: opacity .2s, visibility 0s .2s, color .15s, border-color .15s; }
  .to-top.shown { opacity: 1; visibility: visible; transition: opacity .2s, color .15s, border-color .15s; }
  .to-top:hover, .to-top:focus-visible { color: #ddd; border-color: #444; outline: none; }
  .to-top svg { width: 14px; height: 14px; }
  @media (max-width: 34rem) { .to-top { right: max(1rem, env(safe-area-inset-right)); bottom: max(1rem, env(safe-area-inset-bottom)); } }
  a { color: #fff; text-decoration: none; }
  a:hover { text-decoration: underline; }
  footer { margin-top: 4rem; color: #666; font-size: .8em; }
  footer p { margin: .4rem 0; }
  footer a, footer a:visited { color: #999; text-decoration: underline; text-decoration-color: #555; text-underline-offset: .2em; }
  @media (max-width: 34rem) {
    /* Too narrow for columns, so a row reads as a line instead, wrapping where a sentence would, with the icon
       in a slot at the left edge beside its first line: a size that holds its own beside the headline, a clear
       space after it, and set by the middle of the letters rather than the middle of the line, which sits low
       by the depth of a descender. The spaces between the parts are where the line breaks, so the room after the
       headline goes at its end, out of sight when the line breaks there; the small grey parts after it are
       spaced by their spaces alone. */
    .row { display: block; padding-left: calc(14px + .7em); }
    .row > .icon { position: absolute; left: 0; width: 14px; height: 14px; top: calc(.4rem + .72em - 7px); }
    .headline, .headline .title { display: inline; }
    .headline .title { white-space: normal; }
    .headline > .title { margin-right: .3em; }
    .source { display: inline; max-width: none; text-align: left; }
    .source > span { display: inline; overflow: visible; white-space: normal; }
  }
"""

# Timestamps age in the reader's browser, so a page built hours ago still reads right: "16m" beside an item,
# "Updated 2h ago" in the header. (Showtimes are <time data-time>, with no datetime, and are left alone.)
AGES = """
<script>
  for (const t of document.querySelectorAll("time[datetime]")) {
    const s = Math.max(60, (Date.now() - new Date(t.dateTime)) / 1000);
    const age = s < 3600 ? Math.round(s / 60) + "m" : s < 86400 ? Math.round(s / 3600) + "h" : Math.round(s / 86400) + "d";
    t.textContent = t.matches(".updated, .ago") ? age + " ago" : age;
  }
</script>"""

# Previews open below their headline, or above it when they'd run off the bottom of the window, and lead with
# the full headline when the one-line one is cut off.
PREVIEWS = """
<script>
  for (const title of document.querySelectorAll(".headline .title")) {
    title.addEventListener("mouseenter", () => {
      const headline = title.closest(".headline");
      headline.classList.toggle("truncated", title.scrollWidth > title.clientWidth);
      const preview = headline.querySelector(".preview");
      if (preview) preview.classList.toggle("above", title.getBoundingClientRect().bottom + preview.offsetHeight + 24 > innerHeight);
    });
  }
</script>"""


# The arrow back to the top, shown once the page is scrolled more than a screen down. From the keyboard, it
# also puts focus back at the top, on the header, rather than on itself as it fades out.
TO_TOP = """
<button class="to-top" type="button" aria-label="Back to top"><svg viewBox="0 0 16 16" aria-hidden="true"><path
  d="M8 13V3.5M3.5 8L8 3.5 12.5 8" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"
  stroke-linejoin="round"/></svg></button>
<script>
  {
    const button = document.querySelector(".to-top");
    const place = () => button.classList.toggle("shown", scrollY > innerHeight);
    addEventListener("scroll", place, {passive: true});
    place();
    button.addEventListener("click", event => {
      const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
      scrollTo({top: 0, behavior: still ? "auto" : "smooth"});
      if (event.detail === 0) document.querySelector("header a")?.focus({preventScroll: true});
    });
  }
</script>"""


# "/" goes to the search, as on many sites, unless something (a video) is open over the page; Esc empties it.
SEARCH_KEYS = """
<script>
  {
    const search = document.querySelector(".search");
    if (search) document.addEventListener("keydown", event => {
      if (event.key === "/" && document.activeElement !== search && !event.metaKey && !event.ctrlKey
          && search.offsetParent && !document.querySelector("dialog[open]")) {
        event.preventDefault();
        search.focus();
      } else if (event.key === "Escape" && document.activeElement === search && search.value) {
        search.value = "";
        search.dispatchEvent(new Event("input"));
      }
    });
  }
</script>"""


def page(site, title, body, css="", head="", symbols="", updated=None, links=None, indexable=False, marked=None, here=None):
    """A whole page: the shared head, header and styles, then this site's styles, icons and body. Its header
    links are the two sites, unless it gives its own as (label, address, whether it's this page); marked gives
    a label's own markup, for one set in two weights ("Pushpin <span>Boston</span>"). here names the page
    after them ("Film"); an empty one is there for a script to fill in. Only a page meant for anyone (the
    public events page) lets search engines list it."""
    marked = marked or {}
    name = SITES[site][0] if links is None else links[0][0]
    if links is None:
        links = [(label, "./" if key == site else url, key == site) for key, (label, url) in SITES.items()]
    current_page = ' aria-current="page"'
    links = "".join(
        f'<a href="{html.escape(url)}"{current_page if current else ""}>{marked.get(label, html.escape(label))}</a>'
        for label, url, current in links
    )
    here_label = "" if here is None else f'<span class="here"{"" if here else " hidden"}>{html.escape(here)}</span>'
    note = (
        f'<span class="header-note">Updated <time class="updated" datetime="{updated.isoformat()}">'
        f'{updated.strftime("%b")} {updated.day}</time></span>' if updated else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<meta name="robots" content="{"index, follow" if indexable else "noindex, nofollow"}">
<meta name="theme-color" content="#000000">
<meta name="apple-mobile-web-app-title" content="{name}">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
{head}
<title>{title}</title>
<style>{BASE_CSS}{css}</style>
</head>
<body>
{symbols}
<main>
<header><nav class="sites" aria-label="Sites">{links}{here_label}</nav>{note}</header>
{body}
</main>{TO_TOP}{AGES}{PREVIEWS}{SEARCH_KEYS}
</body>
</html>
"""
