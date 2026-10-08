#!/usr/bin/env python3
"""Fetch every site in feeds.txt and write the latest posts to dist/news. Run from the repo's top folder:
python3 -m news.build"""

import gzip
import html
import json
import os
import re
import shutil
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from shared import site as shared

ROOT = Path(__file__).parent
FEEDS_FILE = ROOT / "feeds.txt"
OUT_DIR = ROOT.parent / "dist" / "news"
# Whose newsfeed this is, so a copy of it in someone else's repo points at theirs: the repo GitHub is building
# (GITHUB_REPOSITORY), where it's published (SITE_URL, for a custom domain; otherwise GitHub Pages' own address
# for the repo), and a sister site to name in the header beside it (EVENTS_URL). A copy needs none of them set.
REPO = os.environ.get("GITHUB_REPOSITORY") or "grahamhagenah/news"
OWNER, _, NAME = REPO.partition("/")
REPO_URL = f"https://github.com/{REPO}"
SITE_URL = (os.environ.get("SITE_URL") or f"https://{OWNER}.github.io/{NAME}/").rstrip("/") + "/"
SISTER_SITE = os.environ.get("EVENTS_URL", "").strip()
POSTS_PER_FEED = 15  # Per site; override with limit=N in feeds.txt.
PAGE_SIZE = 30  # Posts per page of the list.
DAYS_TO_KEEP = 3  # Older posts are dropped; override with days=N in feeds.txt.
PREVIEW_CHARS = 600  # Roughly how much text the hover preview shows.
# Each build publishes its feeds' posts beside the page; a feed that fails next time falls back to its copy
# there, if it's no older than this.
FEEDS_URL = SITE_URL + "feeds.json"
FALLBACK_LIMIT = timedelta(days=2)
# What sites see when the build fetches them. Keep it: some sites' bot filters (Marginal Revolution,
# InsideEVs) block a user agent containing "newsfeed" but allow this one.
USER_AGENT = "Mozilla/5.0 (compatible; rss-reader/1.0)"

# Apple's own catalogue, which its Podcasts app opens from: a show is found by name and told apart by the feed
# it carries, and its episodes come back with the audio file each points at, which is what an episode here is
# matched to. Failures fall back to web links.
APPLE_SEARCH_URL = "https://itunes.apple.com/search"
APPLE_LOOKUP_URL = "https://itunes.apple.com/lookup"

FEED_TYPES = {"application/rss+xml", "application/atom+xml", "application/rdf+xml"}
COMMON_FEED_PATHS = ["/feed", "/rss", "/feed.xml", "/rss.xml", "/atom.xml", "/index.xml"]


def is_site_line(line):
    line = line.strip()
    return bool(line) and not line.startswith("#")


def parse_site(line):
    """A feeds.txt line: a URL, then an optional name and options like limit=5, days=14, only="Full
    Performance" (only posts whose titles have that in them), tag="BNM" (a label beside each of its posts'
    titles), titles=page (each post's title from its own page, for feeds whose titles leave things out) or
    music=album / music=song (a link to each reviewed album or song on Apple Music)."""
    site = {"url": "", "name": "", "limit": POSTS_PER_FEED, "days": DAYS_TO_KEEP, "apple": "", "only": "",
            "tag": "", "page_titles": False, "music": ""}
    for quoted in ("only", "tag"):
        found = re.search(rf'\s{quoted}="([^"]*)"', line)
        if found:
            line = line[:found.start()] + line[found.end():]
            site[quoted] = found.group(1)
    url, *words = line.split()
    site["url"] = url
    name = []
    for word in words:
        option = re.fullmatch(r"(limit|days)=(\d+)", word)
        show = re.fullmatch(r"apple=(\d+)", word)
        if option:
            site[option.group(1)] = int(option.group(2))
        elif show:
            site["apple"] = show.group(1)
        elif word == "titles=page":
            site["page_titles"] = True
        elif word in ("music=album", "music=song"):
            site["music"] = word.split("=")[1]
        else:
            name.append(word)
    site["name"] = " ".join(name)
    return site


def read_sites():
    return [parse_site(line) for line in FEEDS_FILE.read_text().splitlines() if is_site_line(line)]


def fetch(url, attempts=3, timeout=20):
    """The address the request ended at, and the body."""
    return shared.fetch(url, USER_AGENT, attempts, timeout)


class FeedLinkFinder(HTMLParser):
    """Collects <link rel="alternate" type="application/rss+xml" href="..."> tags."""

    def __init__(self):
        super().__init__()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        rel = (attrs.get("rel") or "").lower().split()
        if tag == "link" and "alternate" in rel and attrs.get("type") in FEED_TYPES and attrs.get("href"):
            self.hrefs.append(attrs["href"])


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


def parse_xml(body):
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return None
    return root if local_name(root.tag) in ("rss", "feed", "RDF") else None


def find_feed(url):
    """Return (feed_url, parsed_root), following the page's feed link if given a homepage."""
    page_url, body = fetch(url)
    root = parse_xml(body)
    if root is not None:
        return page_url, root

    finder = FeedLinkFinder()
    finder.feed(body.decode("utf-8", "replace"))
    for candidate in finder.hrefs + COMMON_FEED_PATHS:
        feed_url = urljoin(page_url, candidate)
        try:
            feed_url, body = fetch(feed_url)
        except Exception:
            continue
        root = parse_xml(body)
        if root is not None:
            return feed_url, root
    raise ValueError("no RSS or Atom feed found")


def children(element, name):
    return [child for child in element if local_name(child.tag) == name]


def child_text(element, *names):
    for name in names:
        for child in children(element, name):
            text = "".join(child.itertext()).strip()
            if text:
                return text
    return ""


clean = shared.clean


def parse_date(text):
    if not text:
        return None
    try:
        date = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        iso = re.sub(r"\.\d+", "", text).replace("Z", "+00:00")
        iso = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", iso)
        try:
            date = datetime.fromisoformat(iso)
        except ValueError:
            return None
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    return date.astimezone(timezone.utc)


def entry_link(entry):
    for link in children(entry, "link"):
        if link.text and link.text.strip():
            return link.text.strip()
        if link.get("href") and link.get("rel", "alternate") == "alternate":
            return link.get("href")
    guid = child_text(entry, "guid", "id")
    return guid if guid.startswith("http") else ""


def without_tracking(url):
    """Drop utm_ tracking parameters, which some feeds add to every link."""
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    kept = [(key, value) for key, value in query if not key.startswith("utm_")]
    return urlunsplit(parts._replace(query=urlencode(kept))) if len(kept) < len(query) else url


def entry_comments(entry, link, feed_url):
    """The post's discussion page and comment count, when the feed has them (Hacker News does)."""
    url, count = None, None
    # <comments> holds the discussion URL; WordPress's <slash:comments> holds a count.
    for child in children(entry, "comments"):
        text = (child.text or "").strip()
        if text.startswith("http"):
            url = urljoin(feed_url, text)
        elif text.isdigit():
            count = int(text)
    hnrss_count = re.search(r"# Comments: (\d+)", child_text(entry, "description"))
    if hnrss_count:
        count = int(hnrss_count.group(1))
    # Comments on the post's own page (most blogs, or an Ask HN post) are already one click away.
    if url and url.split("#")[0] == link.split("#")[0]:
        return None, None
    return url, count


def entry_audio(entry):
    """The episode's audio file, when the post is a podcast episode."""
    # RSS <enclosure>, Media RSS <media:content medium="audio">, or Atom <link rel="enclosure">.
    for child in children(entry, "enclosure") + children(entry, "content") + children(entry, "link"):
        if local_name(child.tag) == "link" and child.get("rel") != "enclosure":
            continue
        kind = child.get("type") or child.get("medium") or ""
        url = child.get("url") or child.get("href")
        if kind.startswith("audio") and url:
            return url
    return None


# A YouTube video's address, and its id. Shorts, at youtube.com/shorts/…, aren't among them: they're left out.
YOUTUBE_VIDEO = re.compile(r"^https://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)([\w-]{11})")


def media_description(entry):
    """The description YouTube's feeds (and other Media RSS feeds) keep in <media:group>, as plain text with
    a line to each paragraph."""
    for group in children(entry, "group"):
        text = child_text(group, "description")
        if text:
            return html.escape(text).replace("\n", "<br>")
    return ""


# hnrss describes link posts with these lines instead of any article text, and Tiny Desk's videos start
# with a byline, "Ashley Pointer | September 10, 2026".
BOILERPLATE = re.compile(r"^(Article URL|Comments URL|Points|# Comments):|^[^|]{1,60} \| [A-Z][a-z]+ \d{1,2}, \d{4}$")


def excerpt(markup, max_chars=PREVIEW_CHARS):
    """The first few paragraphs of an HTML snippet, as plain text, cut to about max_chars."""
    return shared.excerpt(markup, max_chars, skip=BOILERPLATE)


class MetaDescriptionFinder(HTMLParser):
    """Collects the summary a page offers for link previews."""

    KEYS = ("og:description", "twitter:description", "description")
    TITLE_KEYS = ("og:title", "twitter:title")

    def __init__(self):
        super().__init__()
        self.found = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        if tag == "meta" and key in self.KEYS + self.TITLE_KEYS and attrs.get("content"):
            self.found.setdefault(key, attrs["content"])


def page_summary(url):
    """For posts whose feed has no text, use the linked page's own link-preview summary."""
    try:
        _, body = fetch(url, attempts=1, timeout=10)
    except Exception:
        return []
    finder = MetaDescriptionFinder()
    finder.feed(body[:500_000].decode("utf-8", "replace"))
    for key in finder.KEYS:
        # Very short descriptions are usually the site's tagline, not a summary of the page.
        if len(finder.found.get(key, "")) >= 40:
            return excerpt(html.escape(finder.found[key]))
    return []


def page_title(url):
    """The title a page gives for link previews, which on some sites says more than its feed does (Pitchfork's
    review feed titles are only the album; its pages' are "Artist: Album")."""
    try:
        _, body = fetch(url, attempts=1, timeout=10)
    except Exception:
        return ""
    finder = MetaDescriptionFinder()
    finder.feed(body[:500_000].decode("utf-8", "replace"))
    return next((clean(finder.found[key]) for key in finder.TITLE_KEYS if finder.found.get(key)), "")


def fill_page_titles(sites, feeds, previous):
    """For titles=page lines, each post's title from its page: from the last build when it had it, so a page
    is fetched once, not every build. A page that can't be had leaves the feed's title."""
    known = {post["link"]: post["title"] for kept in previous.get("feeds", {}).values() for post in kept.get("posts", [])}
    wanted = [(post, feed) for site, feed in zip(sites, feeds) if site["page_titles"] for post in feed["posts"]]
    missing = [post for post, _ in wanted if post["link"] not in known]
    with ThreadPoolExecutor(max_workers=8) as pool:
        for post, title in zip(missing, pool.map(lambda post: page_title(post["link"]), missing)):
            if title:
                known[post["link"]] = title
    for post, _ in wanted:
        post["title"] = known.get(post["link"]) or post["title"]


def read_feed(site):
    feed_url, root = find_feed(site["url"])
    channel = children(root, "channel")
    meta = channel[0] if channel else root
    # RSS 2.0 nests items in <channel>; Atom and RSS 1.0 keep them at the top level.
    entries = children(meta, "item") or children(root, "item") or children(root, "entry")
    cutoff = datetime.now(timezone.utc) - timedelta(days=site["days"])

    posts = []
    for entry in entries:
        title = clean(child_text(entry, "title"))
        audio = entry_audio(entry)
        # Many podcast feeds give an episode no web page of its own, just the audio file.
        link = entry_link(entry) or audio
        date = parse_date(child_text(entry, "pubDate", "published", "updated", "date"))
        if "youtube.com/shorts/" in link:
            continue  # Shorts are the endless-scroll kind of video this page is meant to be free of.
        if title and link and (date is None or date >= cutoff) and site["only"].casefold() in title.casefold():
            link = without_tracking(urljoin(feed_url, link))
            video = YOUTUBE_VIDEO.match(link)
            summary = excerpt(child_text(entry, "encoded", "content", "description", "summary") or media_description(entry))
            comments, comment_count = entry_comments(entry, link, feed_url)
            posts.append({
                "title": title,
                "link": link,
                "date": date,
                "summary": summary,
                "comments": comments,
                "comment_count": comment_count,
                "audio": audio,
                "podcast": audio is not None,
                "video": video.group(1) if video else None,  # A YouTube video's id, for playing it here.
                "tag": site["tag"],
                "music": None,  # Apple Music, for music=album or music=song lines; see link_to_apple_music.
            })

    return {
        "name": site["name"] or clean(child_text(meta, "title")) or site["url"],
        "url": site["url"],
        "feed_url": feed_url,
        "apple": site["apple"],
        "tag": site["tag"],
        "posts": posts[: site["limit"]],
    }


# A YouTube channel's feed, by the channel's id, and the YouTube Data API's list of a playlist's videos.
YOUTUBE_CHANNEL_FEED = re.compile(r"youtube\.com/feeds/videos\.xml\?channel_id=UC([\w-]{22})")
YOUTUBE_PLAYLIST_API = "https://www.googleapis.com/youtube/v3/playlistItems"


def read_youtube_api(site, channel, key):
    """A YouTube channel's latest videos from the YouTube Data API, for when its feed won't load: YouTube's
    feeds answer 404 now and then, for a while, for a channel that's fine. The channel's UULF playlist is its
    uploads without Shorts or live streams, as its feed is once Shorts are left out. One request costs one
    unit of the key's 10,000 a day."""
    query = urlencode({"part": "snippet,contentDetails", "playlistId": f"UULF{channel}", "maxResults": 15, "key": key})
    items = json.loads(fetch(f"{YOUTUBE_PLAYLIST_API}?{query}")[1]).get("items", [])
    cutoff = datetime.now(timezone.utc) - timedelta(days=site["days"])
    posts = []
    for item in items:
        snippet, details = item.get("snippet", {}), item.get("contentDetails", {})
        video = details.get("videoId") or snippet.get("resourceId", {}).get("videoId")
        date = parse_date(details.get("videoPublishedAt") or snippet.get("publishedAt"))
        title = clean(snippet.get("title", ""))
        # A video made private or deleted since stays in the playlist, under a stand-in title.
        if not video or title in ("Private video", "Deleted video") or (date and date < cutoff) \
                or site["only"].casefold() not in title.casefold():
            continue
        posts.append({
            "title": title,
            "link": f"https://www.youtube.com/watch?v={video}",
            "date": date,
            "summary": excerpt(html.escape(snippet.get("description", "")).replace("\n", "<br>")),
            "comments": None,
            "comment_count": None,
            "audio": None,
            "podcast": False,
            "video": video,
        })
    name = site["name"] or (items[0]["snippet"].get("channelTitle", "") if items else "") or site["url"]
    return {"name": name, "url": site["url"], "feed_url": site["url"], "apple": site["apple"],
            "posts": posts[: site["limit"]]}


def load(site):
    try:
        return read_feed(site)
    except Exception as error:
        # A YouTube channel whose feed won't load comes from the YouTube Data API instead, given a key (the
        # YOUTUBE_KEY secret). The key is kept out of any error, which the page publishes.
        channel, key = YOUTUBE_CHANNEL_FEED.search(site["url"]), os.environ.get("YOUTUBE_KEY", "").strip()
        if channel and key:
            try:
                feed = read_youtube_api(site, channel.group(1), key)
                print(f"  {feed['name']}: its feed failed ({error}), so its videos came from the YouTube API", file=sys.stderr)
                return feed
            except Exception as api_error:
                error = f"{error}; the YouTube API: {str(api_error).replace(key, '…')}"
        return {"name": site["name"] or site["url"], "url": site["url"], "error": str(error), "posts": []}


def apple_json(url, asked):
    address = f"{url}?{urllib.parse.urlencode(asked)}"
    with urllib.request.urlopen(urllib.request.Request(address, headers={"User-Agent": USER_AGENT}), timeout=20) as response:
        body = response.read()
    return json.loads(gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body)


def comparable(text):
    return re.sub(r"\W+", " ", html.unescape(text or "")).strip().lower()


def apple_show(feed):
    """Which show in Apple's catalogue a feed is: the one its line names (apple=…), or the one a search by name
    returns carrying this feed. Nothing without that, since the nearest thing by name is often another show."""
    if feed["apple"]:
        return feed["apple"]
    found = apple_json(APPLE_SEARCH_URL, {"term": feed["name"], "media": "podcast", "limit": 10})
    wanted = (feed["feed_url"] or "").rstrip("/")
    for show in found.get("results", []):
        if (show.get("feedUrl") or "").rstrip("/") == wanted:
            return str(show["collectionId"])
    return ""


def apple_links(feed):
    """Apple Podcasts links for a show's episodes, keyed by audio file and by title, and the show's own page."""
    show = apple_show(feed)
    if not show:
        return {}, None
    answer = apple_json(APPLE_LOOKUP_URL, {"id": show, "media": "podcast", "entity": "podcastEpisode", "limit": 200})
    results = answer.get("results") or []
    links = {}
    for episode in results:
        url = episode.get("trackViewUrl")
        if episode.get("wrapperType") != "podcastEpisode" or not url:
            continue
        links[episode.get("episodeUrl")] = url
        links[comparable(episode.get("trackName"))] = url
    showing = next((item.get("collectionViewUrl") for item in results if item.get("wrapperType") == "track"), None)
    return links, showing or f"https://podcasts.apple.com/podcast/id{show}"


def link_to_apple(feed):
    """Point podcast episodes at Apple Podcasts, whose links open in the Podcasts app."""
    episodes = [post for post in feed["posts"] if post["podcast"]]
    # Only whole podcast feeds: a newsletter with the odd episode in it isn't a show in Apple's catalogue,
    # and the nearest thing by name would be someone else's. Those episodes keep web links.
    if len(episodes) < len(feed["posts"]):
        return
    try:
        links, show_url = apple_links(feed)
    except Exception as error:
        print(f"  {feed['name']}: Apple Podcasts lookup failed, keeping web links ({error})", file=sys.stderr)
        return
    linked = 0
    for post in episodes:
        episode_url = links.get(post["audio"]) or links.get(comparable(post["title"]))
        linked += bool(episode_url)
        # An episode Apple hasn't picked up yet opens the show, where it will appear.
        post["link"] = episode_url or show_url or post["link"]
    print(f"  {feed['name']}: {linked} of {len(episodes)} episodes linked to Apple Podcasts")


APPLE_MUSIC_SEARCH = "https://music.apple.com/us/search"
MUSIC_RECHECK = timedelta(days=1)


def artist_and_title(title):
    """A review's "Artist: Album" or "Artist: “Song” [ft. Someone]" as the artist and the plain name."""
    artist, colon, name = title.partition(": ")
    if not colon:
        artist, name = "", title
    name = re.sub(r"\s*\[(?:ft|feat)\.[^\]]*\]", "", name).strip().strip("“”\"")
    # Apple's search matches a plain apostrophe more reliably than a curly one.
    return artist.strip().replace("’", "'"), name.replace("’", "'")


def apple_music(kind, title):
    """The album or song on Apple Music, from a search by artist and name that returns the same name by one of
    the same artists (a collaboration can be credited differently), or None. Anything less is too often somebody
    else's record of the same name."""
    artist, name = artist_and_title(title)
    if not artist:
        return None
    found = apple_json(APPLE_SEARCH_URL, {"term": f"{artist} {name}", "entity": kind, "limit": 25, "country": "US"})
    field, link = ("trackName", "trackViewUrl") if kind == "song" else ("collectionName", "collectionViewUrl")
    wanted = artist_names(artist)
    for item in found.get("results", []):
        if wanted & artist_names(item.get("artistName")) and comparable(name) in comparable(item.get(field)):
            return re.sub(r"[?&]uo=\d+", "", item[link])
    return None


def artist_names(artists):
    """Each artist in a credit, for telling whether two credits share one: Pitchfork's "Yung Lean / Metro Boomin",
    Apple's "Charli xcx, Robyn & Yung Lean". Only "/", "&" and "," split it, so "Florence and the Machine" is one."""
    return {comparable(name) for name in re.split(r"\s*[/&,]\s*", artists or "") if comparable(name)}


def music_search(title):
    """Apple Music's search for a review's artist and name: on an iPhone, the Music app with it typed in."""
    artist, name = artist_and_title(title)
    return f"{APPLE_MUSIC_SEARCH}?{urllib.parse.urlencode({'term': f'{artist} {name}'.strip()})}"


def link_to_apple_music(sites, feeds, previous, now):
    """For music= lines, each post's album or song on Apple Music, or Apple Music's search for it when a search
    doesn't find it. A found link is kept from build to build; one not found is looked for again a day later,
    since a release often reaches Apple's search after its review. One search at a time, to go easy on Apple."""
    known = {post["link"]: post for kept in previous.get("feeds", {}).values() for post in kept.get("posts", [])}
    for site, feed in zip(sites, feeds):
        if not site["music"]:
            continue
        for post in feed["posts"]:
            before = known.get(post["link"], {})
            checked = before.get("music_checked")
            if before.get("music") and (before.get("music_found") or now - datetime.fromisoformat(checked) < MUSIC_RECHECK):
                post.update(music=before["music"], music_found=before.get("music_found", False), music_checked=checked)
                continue
            try:
                found = apple_music(site["music"], post["title"])
            except Exception as error:
                print(f"  {post['title']}: Apple Music search failed ({error})", file=sys.stderr)
                found = None
            post.update(music=found or music_search(post["title"]), music_found=bool(found), music_checked=now.isoformat())


def render_time(date, css_class=""):
    attr = f' class="{css_class}"' if css_class else ""
    return f'<time{attr} datetime="{date.isoformat()}">{date.strftime("%b %-d")}</time>'


def all_posts(feeds):
    """Every feed's posts, newest first. A post two feeds share (a Pitchfork review also in its Best New Music)
    shows once, with the tag either gives it."""
    posts, by_link = [], {}
    for feed in feeds:
        for post in feed["posts"]:
            same = by_link.get(post["link"])
            if same:
                same["tag"] = same.get("tag") or post.get("tag", "")
                continue
            by_link[post["link"]] = dict(post, source=feed["name"])
            posts.append(by_link[post["link"]])
    # Newest first; posts without a date sink to the bottom.
    posts.sort(key=lambda post: post["date"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return posts


def render_json(posts, built_at):
    """The current list, which the open page checks for new posts, and which works as an export."""
    return json.dumps(
        {
            "updated": built_at.isoformat(),
            "posts": [
                {
                    "title": post["title"],
                    "link": post["link"],
                    "source": post["source"],
                    "date": post["date"].isoformat() if post["date"] else None,
                    "comments": post["comments"],
                    "podcast": post["podcast"],
                    "video": post.get("video"),
                    "tag": post.get("tag") or None,
                    "music": post.get("music"),
                }
                for post in posts
            ],
        },
        ensure_ascii=False,
        indent=1,
    )


# Each post's mark, before its source: a page of text for an article, headphones for a podcast episode, a
# screen with a play button for a video.
# Drawn once in the page; rows point to the drawing. Unread, it takes its kind's color; read, it goes gray.
ICON_DRAWINGS = {
    "article": '<rect x="3" y="1.75" width="10" height="12.5" rx="1.5"/><path d="M5.75 5.5h4.5M5.75 8h4.5M5.75 10.5h2.75"/>',
    "podcast": '<path d="M2.75 10.5V8a5.25 5.25 0 0 1 10.5 0v2.5"/>'
               '<rect x="1.5" y="9.5" width="3.25" height="5" rx="1.25" fill="currentColor" stroke="none"/>'
               '<rect x="11.25" y="9.5" width="3.25" height="5" rx="1.25" fill="currentColor" stroke="none"/>',
    "video": '<rect x="1.5" y="2.75" width="13" height="10.5" rx="2"/><path d="M6.5 5.75v4.5L10.25 8z" fill="currentColor"/>',
}
ICON_SYMBOLS = shared.icon_symbols(ICON_DRAWINGS)


def icon(kind, decorative=False):
    """A post's mark. Beside a label that already says it (the filter), it's hidden from screen readers."""
    return shared.icon(kind, None if decorative else kind.capitalize())


# Tags with a mark of their own, drawn before the label, and what the label is short for: Pitchfork's arrows
# for its Best New Music and Best New Track. The mark's alt text says it in full, so a screen reader reads that instead of the
# letters, and hovering the label shows it.
TAG_ICONS = {"BNM": ("bnm.svg", "Best New Music"), "BNT": ("bnm.svg", "Best New Track")}


def render_tag(tag):
    if not tag:
        return ""
    if tag not in TAG_ICONS:
        return f'<span class="tag">{html.escape(tag)}</span>'
    src, meaning = TAG_ICONS[tag]
    return (f'<span class="tag" title="{html.escape(meaning)}"><img src="{src}" alt="{html.escape(meaning)}" width="17" height="9">'
            f'<span aria-hidden="true">{html.escape(tag)}</span></span>')


def render_index(feeds, posts, failed, stale, built_at):
    items = []
    for post in posts:
        when = render_time(post["date"]) if post["date"] else ""
        kind = "podcast" if post["podcast"] else "video" if post.get("video") else "article"
        mark = icon(kind)
        marked = " data-podcast" if post["podcast"] else f' data-video="{post["video"]}"' if post.get("video") else ""
        preview = shared.preview(post["title"], post["summary"])
        tag = render_tag(post.get("tag"))
        comments = ""
        if post["comments"]:
            count = post["comment_count"]
            label = "comments" if count is None else "1 comment" if count == 1 else f"{count} comments"
            comments = f'<a class="comments" href="{html.escape(post["comments"])}">{label}</a>'
        music = f'<a class="comments" href="{html.escape(post["music"])}">Apple Music</a>' if post.get("music") else ""
        # A phone's row reads as one line, so who it's from goes right after how long ago, before any links:
        # "2d from Pitchfork · Apple Music", not "2d Apple Music from Pitchfork". On desktop the source has its
        # own column at the end of the row, so this copy is hidden there, and that one on phones.
        via = f'<span class="via">from {html.escape(post["source"])}</span>'
        # A phone line breaks after the dot, never before it, so no line starts with a stray "·".
        links = f'<span class="sep">\u00a0·</span> '.join(link for link in (comments, music) if link)
        if links:
            via += '<span class="sep">\u00a0·</span>'
        # As Pushpin's rows are: the kind's icon, the headline and how long ago with its comments, and the source
        # at the end of the row; spaces between the parts, for a phone's line to break at.
        items.append(
            f'<li class="row" data-from="{html.escape(post["source"])}"{marked}>{mark}'
            f'<div class="headline"><a class="title" href="{html.escape(post["link"])}">{html.escape(post["title"])}</a> '
            + " ".join(part for part in (tag, when, via, links) if part)
            + f'{preview}</div> <span class="source"><span>{html.escape(post["source"])}</span></span></li>'
        )

    # Feeds that failed; a feed that just hasn't posted lately isn't one.
    failed_note = f"<p>Couldn’t load {html.escape(', '.join(failed))}.</p>\n" if failed else ""
    failed_note += "".join(
        f'<p>Couldn’t reach {html.escape(name)}; its posts are from {render_time(fetched, "updated")}.</p>\n'
        for name, fetched in stale
    )

    # Only worth offering when there's something to choose between, and only the kinds there are.
    kinds = [("podcasts", "podcast", "Podcasts", any(post["podcast"] for post in posts)),
             ("videos", "video", "Videos", any(post.get("video") for post in posts))]
    show_filter = (
        '<nav class="filter" aria-label="Show"><button data-show="all">All</button>'
        f'<button data-show="articles">{icon("article", decorative=True)}Articles</button>'
        + "".join(f'<button data-show="{key}">{icon(mark, decorative=True)}{label}</button>' for key, mark, label, there in kinds if there)
        + f'<span class="finders">{shared.menu({post["source"] for post in posts}, "All sources", "Source")}{shared.SEARCH}</span></nav>\n'
        if any(there for *_, there in kinds)
        else ""
    )

    body = (
        show_filter + '<button class="new-posts" hidden></button>\n'
        + PLAYER +
        f'<ul class="posts" data-page-size="{PAGE_SIZE}">\n' + "\n".join(items) + "\n</ul>\n"
        '<p class="empty" hidden></p>\n'
        '<nav class="pager"></nav>\n'
        f"<footer>\n{failed_note}"
        '<p><a href="sources.html">Add or remove sites</a></p>\n'
        "</footer>\n"
        f"<script>{INDEX_JS}</script>"
    )
    return page("Newsfeed", body, updated=built_at)


# Where a video plays: over the list, with a way to close it, and nothing else. (The player shows its name.)
PLAYER = """<dialog class="player" aria-label="Video">
<button class="player-copy" aria-label="Copy link"><svg viewBox="0 0 16 16" aria-hidden="true"><g class="copy-link"><path d="M6.5 9.5l3-3M7 4.5l1-1a2.5 2.5 0 0 1 3.5 3.5l-1 1M9 11.5l-1 1a2.5 2.5 0 0 1-3.5-3.5l1-1"/></g><g class="copy-done"><path d="M3.5 8.5l3 3 6-6"/></g></svg></button>
<button class="player-close" aria-label="Close"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 3.5l9 9M12.5 3.5l-9 9"/></svg></button>
<div class="player-bar"></div>
<div class="player-frame"></div>
<p class="player-note" hidden>This video can’t be played here. <a href="">Watch it on YouTube</a></p>
</dialog>
"""

INDEX_JS = """
  const links = [...document.querySelectorAll(".posts a.title")];

  // Put a green dot beside headlines you haven't clicked. Browsers keep visited links private from pages,
  // so clicks are remembered here instead, in this browser only, for 30 days.
  let clicked = {};
  try { clicked = JSON.parse(localStorage.getItem("reader-clicked") || "{}"); } catch (error) {}
  const monthAgo = Date.now() - 30 * 24 * 60 * 60 * 1000;
  for (const href in clicked) if (clicked[href] < monthAgo) delete clicked[href];
  const saveClicked = () => {
    try { localStorage.setItem("reader-clicked", JSON.stringify(clicked)); } catch (error) {}
  };
  for (const a of links) {
    const li = a.closest("li");
    li.classList.toggle("unread", !clicked[a.href]);
    const markRead = event => {
      if (event.button > 1) return; // Right-clicks only open a menu.
      clicked[a.href] = Date.now();
      li.classList.remove("unread");
      saveClicked();
    };
    a.addEventListener("click", markRead);
    a.addEventListener("auxclick", markRead);
  }
  saveClicked();

  // Videos play here, in a small window in the corner of the page: no comments, no suggestions, no next
  // video. It's YouTube's own player, its privacy-enhanced version, loaded the first time a video is opened;
  // when the video ends the window closes, before YouTube can offer another. A click with a modifier key
  // still opens YouTube, in a new tab.
  const dialog = document.querySelector(".player");
  const note = dialog.querySelector(".player-note");
  let player = null, youtube = null, playing = "";
  const loadYouTube = () => youtube ??= new Promise(resolve => {
    window.onYouTubeIframeAPIReady = resolve;
    document.head.append(Object.assign(document.createElement("script"), { src: "https://www.youtube.com/iframe_api" }));
  });
  // The player itself, made once for each video and left alone after that. It carries YouTube's own controls,
  // whose full-screen button is the way to the whole screen: YouTube takes its frame there and hands it back
  // to the corner afterwards, still playing, which is surer than asking for the screen ourselves.
  function mount(videoId) {
    const holder = document.createElement("div");
    dialog.querySelector(".player-frame").replaceChildren(holder);
    let started = false;
    dialog.classList.remove("ready");  // Until the video is there to show, the frame stays dark and empty.
    return new YT.Player(holder, {
      host: "https://www.youtube-nocookie.com",
      videoId,
      width: "100%",
      height: "100%",
      // Related videos from the same channel only, no annotations, and inline on phones until made full
      // screen.
      playerVars: { autoplay: 1, rel: 0, iv_load_policy: 3, playsinline: 1 },
      events: {
        onReady: () => dialog.classList.add("ready"),
        onStateChange: event => {
          // The player takes the keyboard when it starts; give it back once, so Esc closes the window.
          if (event.data === YT.PlayerState.PLAYING && !started) {
            started = true;
            dialog.querySelector(".player-close").focus();
          }
          if (event.data === YT.PlayerState.ENDED) dialog.close();
        },
        onError: () => { note.hidden = false; }, // Its channel doesn't allow it to play elsewhere, most often.
      },
    });
  }

  async function play(a) {
    // Another video, with one already playing (in the corner, most likely): the window closes on the old one
    // first, and the wait is for the closing to be done with — it takes the player apart a moment later, and
    // would take the new one apart with it.
    if (dialog.open) {
      dialog.classList.remove("corner");
      document.body.classList.remove("video-corner");
      dialog.close();
      await new Promise(done => setTimeout(done));
    }
    dialog.setAttribute("aria-label", a.textContent);
    playing = a.href;  // What the copy button sends on: the video's own address, not this page's.
    note.querySelector("a").href = a.href;
    note.hidden = true;
    // A video is something to have on while the list is read, so the corner is where it plays; YouTube's own
    // full-screen button is what gives it the whole screen.
    dialog.classList.add("corner");
    document.body.classList.add("video-corner");
    dialog.show();
    await loadYouTube();
    if (!dialog.open) return; // Closed while the player loaded.
    player = mount(a.closest("li").dataset.video);
  }
  dialog.addEventListener("close", () => {
    if (player) player.destroy();
    player = null;
    dialog.querySelector(".player-frame").replaceChildren();
    dialog.classList.remove("corner");
    dialog.style.inset = placed = "";  // Back to the corner it starts in, for the next video.
    document.body.classList.remove("video-corner");
  });
  // Dragging the corner window by its bar: the video itself can't be taken hold of, being YouTube's own page,
  // which answers every press inside it. Where it's put is remembered while it plays and forgotten when it
  // closes, and it's kept whole on the screen, both as it's dragged and if the window is resized under it.
  const bar = dialog.querySelector(".player-bar");
  let held = null, placed = "";
  const putAt = (left, top) => {
    const box = dialog.getBoundingClientRect();
    const x = Math.min(Math.max(left, 8), Math.max(8, innerWidth - box.width - 8));
    const y = Math.min(Math.max(top, 8), Math.max(8, innerHeight - box.height - 8));
    placed = `${y}px auto auto ${x}px`;
    dialog.style.inset = placed;
  };
  bar.addEventListener("pointerdown", event => {
    const box = dialog.getBoundingClientRect();
    held = { x: event.clientX - box.left, y: event.clientY - box.top };
    bar.setPointerCapture(event.pointerId);
    dialog.classList.add("dragging");
  });
  bar.addEventListener("pointermove", event => {
    if (held) putAt(event.clientX - held.x, event.clientY - held.y);
  });
  for (const ending of ["pointerup", "pointercancel"]) {
    bar.addEventListener(ending, () => { held = null; dialog.classList.remove("dragging"); });
  }
  addEventListener("resize", () => {
    if (dialog.style.inset && dialog.classList.contains("corner")) {
      const box = dialog.getBoundingClientRect();
      putAt(box.left, box.top);
    }
  });
  // The video's own address, to send to someone or keep: taken from the link that opened it, since the frame
  // is YouTube's page and says nothing about where it came from. The mark turns to a tick for a moment.
  const copy = dialog.querySelector(".player-copy");
  copy.addEventListener("click", async () => {
    if (!playing) return;
    try {
      await navigator.clipboard.writeText(playing);
    } catch {
      const field = Object.assign(document.createElement("textarea"), { value: playing });
      document.body.append(field);
      field.select();
      document.execCommand("copy");
      field.remove();
    }
    copy.classList.add("copied");
    copy.setAttribute("aria-label", "Link copied");
    setTimeout(() => {
      copy.classList.remove("copied");
      copy.setAttribute("aria-label", "Copy link");
    }, 1600);
  });
  dialog.querySelector(".player-close").addEventListener("click", () => dialog.close());
  // Esc, which the dialog closes on by itself in most browsers. Once the video has been clicked, keys go to
  // YouTube's player instead, and the × or a click outside the video closes it.
  document.addEventListener("keydown", event => { if (event.key === "Escape" && dialog.open) dialog.close(); });
  for (const a of links) {
    if (!a.closest("li").dataset.video) continue;
    a.addEventListener("click", event => {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      play(a);
    });
  }

  // Opened from the home screen there's no pull-to-refresh, so reload on return after five minutes away.
  let hiddenAt = 0;
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) hiddenAt = Date.now();
    else if (hiddenAt && Date.now() - hiddenAt > 5 * 60 * 1000) location.reload();
  });

  // While the page is on screen, check every two minutes whether a newer build has posts this page
  // doesn't, and offer a button to load them.
  const button = document.querySelector(".new-posts");
  const onPage = new Set(links.map(a => a.href));
  async function checkForNewPosts() {
    if (document.hidden) return;
    try {
      const latest = await (await fetch("posts.json?" + Date.now(), { cache: "no-store" })).json();
      const count = latest.posts.filter(post => !onPage.has(new URL(post.link, location.href).href)).length;
      button.textContent = count === 1 ? "1 new post" : count + " new posts";
      button.hidden = count === 0;
    } catch (error) {}
  }
  setInterval(checkForNewPosts, 2 * 60 * 1000);
  document.addEventListener("visibilitychange", checkForNewPosts);
  button.addEventListener("click", () => {
    // New posts arrive at the top, so go to page one. Reloading (rather than following a link)
    // makes the browser fetch the page fresh instead of from its cache.
    history.replaceState(null, "", location.pathname);
    scrollTo(0, 0);
    location.reload();
  });

  // Show one page of posts at a time; ?page=2 shows the next set. Every post is in the page, so the
  // new-post check and the unread dots still see all of them.
  const list = document.querySelector(".posts");
  const pageSize = Number(list.dataset.pageSize);
  const items = [...list.children];
  const pager = document.querySelector(".pager");
  const empty = document.querySelector(".empty");

  // The filter shows every post, or only articles, podcast episodes or videos. The choice is remembered in
  // this browser, and paging counts only the posts it shows. The menu beside it shows one source's posts, of
  // every kind, and the search the posts with every word typed somewhere in their headline, source or preview
  // (accents aside); both are kept in the address (?source=, ?q=).
  const filter = document.querySelector(".filter");
  const search = document.querySelector(".search");
  const from = document.querySelector(".filter .pick");
  let show = "all";
  try { show = (filter && localStorage.getItem("reader-show")) || "all"; } catch (error) {}
  const plain = text => text.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
  const searchable = new Map(items.map(li => [li, plain(li.textContent)]));
  const query = () => (search && search.offsetParent ? search.value.trim() : "");
  if (search) search.value = new URLSearchParams(location.search).get("q") || "";
  if (from) {
    from.value = new URLSearchParams(location.search).get("source") || "";
    if (from.selectedIndex < 0) from.value = ""; // A source with nothing on the page now.
  }
  const address = page => {
    const params = new URLSearchParams();
    if (query()) params.set("q", query());
    if (from && from.value) params.set("source", from.value);
    if (page > 1) params.set("page", page);
    return params.toString() ? "?" + params : location.pathname;
  };

  const kind = li => li.hasAttribute("data-podcast") ? "podcasts" : li.dataset.video ? "videos" : "articles";

  function showPosts() {
    const words = plain(query()).split(/\\s+/).filter(Boolean);
    const source = from ? from.value : "";
    const shown = items.filter(li => (show === "all" || kind(li) === show) && (!source || li.dataset.from === source)
      && words.every(word => searchable.get(li).includes(word)));
    const pages = Math.max(1, Math.ceil(shown.length / pageSize));
    const page = Math.min(pages, Math.max(1, parseInt(new URLSearchParams(location.search).get("page")) || 1));
    items.forEach(li => { li.hidden = true; });
    shown.forEach((li, i) => { li.hidden = i < (page - 1) * pageSize || i >= page * pageSize; });
    list.classList.add("paged");
    const link = (n, text) => `<a href="${address(n)}">${text}</a>`;
    pager.innerHTML = pages < 2 ? "" :
      (page > 1 ? link(page - 1, "← Newer") : "<span></span>") +
      `<span>Page ${page} of ${pages}</span>` +
      (page < pages ? link(page + 1, "Older →") : "<span></span>");
    empty.textContent = words.length ? `Nothing ${source ? "from " + source : "here"} matches “${query()}”.`
      : source ? `Nothing from ${source} right now.`
      : { podcasts: "No podcast episodes right now.", videos: "No videos right now." }[show] || "No articles right now.";
    if (from) from.classList.toggle("chosen", Boolean(source));
    empty.hidden = shown.length > 0;
    if (filter) for (const b of filter.querySelectorAll("button")) b.setAttribute("aria-pressed", b.dataset.show === show);
  }
  showPosts();

  if (filter) filter.addEventListener("click", event => {
    const b = event.target.closest("button");
    if (!b) return;
    show = b.dataset.show;
    try { localStorage.setItem("reader-show", show); } catch (error) {}
    history.replaceState(null, "", address(1)); // Back to page one.
    showPosts();
  });
  if (search) search.addEventListener("input", () => {
    history.replaceState(null, "", address(1));
    showPosts();
  });
  // Choosing a source shows all its posts, whatever their kind.
  if (from) from.addEventListener("change", () => {
    show = "all";
    try { localStorage.setItem("reader-show", show); } catch (error) {}
    history.replaceState(null, "", address(1));
    showPosts();
  });
"""


ISSUE_NOTE = (
    "Press Create to make this change. Within a minute a bot updates feeds.txt, replies here, "
    "and closes this issue."
)


def render_sources(feeds, built_at):
    rows = []
    for feed in feeds:
        count = len(feed["posts"])
        status = (
            f"{count} posts, from earlier" if feed.get("restored")
            else "couldn’t load" if "error" in feed
            else f"{count} posts" if count
            else "no recent posts"
        )
        remove = f"{REPO_URL}/issues/new?" + urlencode({"title": f"Remove {feed['url']}", "body": ISSUE_NOTE})
        rows.append(
            f'<li><a href="{html.escape(feed["url"])}">{html.escape(feed["name"])}</a>'
            + render_tag(feed.get("tag"))
            + f'<span class="note">{status}</span><a class="remove" href="{html.escape(remove)}" target="_blank" rel="noopener">remove</a></li>'
        )

    body = f"""<h1>Add a site</h1>
<form class="add-site" action="{REPO_URL}/issues/new" data-note="{html.escape(ISSUE_NOTE)}">
<input name="site" placeholder="https://example.com/" autocapitalize="off" autocorrect="off" spellcheck="false" required>
<input name="label" placeholder="Name (optional)">
<button>Add</button>
</form>
<p class="help">This opens a pre-filled GitHub issue; press Create. Within a minute a bot finds the site’s feed,
adds it, and replies on the issue, or tells you if it couldn’t find one. Removing a site works the same way.</p>

<h1>Sources</h1>
<ul class="sources">
{chr(10).join(rows)}
</ul>

<h1>Give a site more or less room</h1>
<p>Each site shows up to {POSTS_PER_FEED} posts from the last {DAYS_TO_KEEP} days. Change that for one site with
options after its name, when you add it or in feeds.txt:</p>
<ul class="options">
<li><code>limit=5</code> shows at most 5 posts, for sites that post constantly.</li>
<li><code>days=14</code> keeps posts for 14 days, for blogs that post rarely.</li>
</ul>
<p>For example, a name of <code>The New York Times limit=8</code>. To change a site that’s already here, edit
<a href="{REPO_URL}/edit/main/news/feeds.txt">feeds.txt on GitHub</a>. If a site doesn’t show up, the
<a href="{REPO_URL}/actions">build log</a> says what it returned.</p>

<h1>Export</h1>
<p><a href="feeds.opml" download>Download these sites as OPML</a>, the file format other feed readers import.
The current list of posts is also available as <a href="posts.json">JSON</a>.</p>

<footer><p>Updated {render_time(built_at, "updated")} · <a href="./">Back to the news</a> ·
<a href="make-your-own.html">Make your own newsfeed</a></p></footer>
<script>
  document.querySelector(".add-site").addEventListener("submit", event => {{
    event.preventDefault();
    const form = event.target;
    const title = ["Add", form.elements.site.value.trim(), form.elements.label.value.trim()].filter(Boolean).join(" ");
    // Open GitHub in a new tab, and clear the form so this page is ready for the next site.
    window.open(form.action + "?" + new URLSearchParams({{ title, body: form.dataset.note }}), "_blank", "noopener");
    form.reset();
  }});
</script>"""
    return page("Sources", body)


TEMPLATE_REPO = "https://github.com/grahamhagenah/newsfeed"


def render_guide(built_at):
    """Make your own: how someone else sets up a newsfeed of their own from the template, which is this
    newsfeed's code on its own. Written for someone who has never used GitHub."""
    body = f"""<h1>Make your own</h1>
<p class="lead">This page is a list of the latest posts from the sites I follow. You can have one of your own,
with your own sites, in about ten minutes. It costs nothing, there\u2019s nothing to install, and nothing to keep
running: GitHub builds the page every so often and publishes it for you.</p>
<p>You need a <a href="https://github.com/signup">GitHub account</a>, which is free. You don\u2019t need to know how
to code \u2014 your list of sites is one line per site in a text file.</p>

<h1>Set it up</h1>
<ol>
<li><strong>Make your copy.</strong> Open <a href="{TEMPLATE_REPO}">the newsfeed template</a> and press the green
<strong>Use this template</strong> button, then <strong>Create a new repository</strong>. Give it a name
(<code>newsfeed</code> is a fine one), leave it <strong>Public</strong>, and press <strong>Create</strong>.
Public is what makes the page free to publish; the only thing in it is your list of sites.</li>
<li><strong>Turn on publishing.</strong> In your new repository, go to <strong>Settings</strong> \u2192
<strong>Pages</strong>, and under <strong>Source</strong> choose <strong>GitHub Actions</strong>.</li>
<li><strong>Turn on the builds.</strong> Go to the <strong>Actions</strong> tab and press the button that
enables workflows. Choose <strong>Build and deploy</strong> on the left, then <strong>Run workflow</strong>.</li>
<li><strong>Open your page.</strong> A minute or so later it\u2019s at
<code>https://&lt;your username&gt;.github.io/&lt;your repository&gt;/</code>. It starts with a handful of sites
so there\u2019s something to look at.</li>
</ol>

<h1>Add your own sites</h1>
<p>Your page has its own <strong>Add or remove sites</strong> page, the same as
<a href="sources.html">this one</a>. Paste a site\u2019s address, press Add, and press Create on the GitHub page
that opens: a minute later it\u2019s on your page. Removing one works the same way.</p>
<p>Or edit the list directly: <code>news/feeds.txt</code> in your repository, one site per line.</p>
<ul class="options">
<li><strong>Any site with a feed:</strong> paste its home page, like <code>https://kottke.org/</code>. The build
finds the feed itself. Most news sites and blogs have one.</li>
<li><strong>YouTube channels:</strong> paste the channel\u2019s page. Its videos play on your page, in a small
window you can drag around, and Shorts are left out.</li>
<li><strong>Podcasts:</strong> paste the show\u2019s feed, and its episodes open in Apple Podcasts.</li>
</ul>
<p>Anything after the address is the name to show, and then options:</p>
<ul class="options">
<li><code>limit=5</code> \u2014 at most 5 posts from that site, for one that posts constantly.</li>
<li><code>days=14</code> \u2014 keep its posts for 14 days, for a blog that posts rarely. The usual is {DAYS_TO_KEEP}.</li>
<li><code>only="Full Performance"</code> \u2014 only posts whose titles have that in them.</li>
</ul>
<p>So a line can be as plain as <code>https://waxy.org/</code> or as fussy as
<code>https://www.nytimes.com/ The New York Times limit=8</code>.</p>

<h1>How often it updates</h1>
<p>The build is set to run every 15 minutes, but GitHub treats scheduled builds as a favour rather than a
promise and skips most of them \u2014 often nine in ten \u2014 so your page can sit a few hours out of date. Two ways
around it:</p>
<ul class="options">
<li><strong>Press the button.</strong> <strong>Actions</strong> \u2192 <strong>Build and deploy</strong> \u2192
<strong>Run workflow</strong> updates it at once.</li>
<li><strong>Have something else start it.</strong> A free account at <a href="https://cron-job.org">cron-job.org</a>
can ask GitHub to build every 15 minutes, which is what keeps this page current. The
<a href="{TEMPLATE_REPO}#how-often-it-updates">template\u2019s README</a> has the exact settings.</li>
</ul>

<h1>If something looks wrong</h1>
<ul class="options">
<li><strong>A site you added isn\u2019t there.</strong> It may not publish a feed. Look for an RSS or Atom link on
the site and paste that address instead.</li>
<li><strong>A site says \u201ccouldn\u2019t load\u201d.</strong> Its posts from the last good build keep showing for two
days. Some sites turn away anything that isn\u2019t a person with a browser; those can\u2019t be read.</li>
<li><strong>Nothing updates at all.</strong> Check the <strong>Actions</strong> tab for a red build and open it
to see what failed.</li>
</ul>

<footer><p>Updated {render_time(built_at, "updated")} \u00b7 <a href="./">Back to the news</a> \u00b7
<a href="sources.html">Add or remove sites</a></p></footer>"""
    return page("Make your own", body)


def render_opml(feeds, built_at):
    def attr(value):
        return html.escape(value, quote=True)

    outlines = [
        f'    <outline type="rss" text="{attr(feed["name"])}" title="{attr(feed["name"])}" '
        f'xmlUrl="{attr(feed.get("feed_url", feed["url"]))}" htmlUrl="{attr(feed["url"])}"/>'
        for feed in feeds
    ]
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head>
    <title>Newsfeed sources</title>
    <dateCreated>{format_datetime(built_at)}</dateCreated>
  </head>
  <body>
{chr(10).join(outlines)}
  </body>
</opml>
"""


HEAD = """<link rel="icon" href="favicon.svg" type="image/svg+xml">
<link rel="icon" href="favicon-32.png" sizes="32x32" type="image/png">
<link rel="apple-touch-icon" href="apple-touch-icon.png">
<link rel="manifest" href="manifest.webmanifest">"""

# The newsfeed's own styles, on top of the ones it shares with the events page (shared/site.py).
CSS = """
  /* Unread marks, and the filter's matching marks: green for articles, violet for podcast episodes, coral for
     videos. */
  :root { --article: #34d399; --podcast: #a78bfa; --video: #fb7185; }
  h1 { margin: 3rem 0 .75rem; color: #777; font-size: .75rem; font-weight: 600;
       letter-spacing: .08em; text-transform: uppercase; }
  h1:first-child, header + h1 { margin-top: 0; }
  .filter .icon { color: var(--article); }
  .filter [data-show="podcasts"] .icon { color: var(--podcast); }
  .filter [data-show="videos"] .icon { color: var(--video); }
  li { padding: .4rem 0; }
  ol { padding-left: 1.25rem; }
  ol li { padding: .3rem 0; }
  /* Until the script picks the page, show the first one, so the whole list never flashes up. */
  .posts:not(.paged) li:nth-child(n+PAGE_START) { display: none; }
  .note, time { color: #666; font-size: .8em; }
  .headline time, .headline .comments, .headline .tag { flex: none; }
  .headline > .via, .headline > .sep { display: none; }
  /* A label a feeds.txt line gives its posts, like Pitchfork's Best New Music: small, white like its mark,
     sitting with the time rather than shouting over the headline. */
  .tag { margin-left: .6em; color: #fff; font-size: .7em; font-weight: 600; letter-spacing: .06em; white-space: nowrap; }
  .tag img { height: .85em; width: auto; margin-right: .35em; vertical-align: .05em; }
  /* Each post's icon: in its kind's color while the post is unread, gray once it's read. */
  .row > .icon { color: #555; }
  .unread > .icon { color: var(--article); }
  .row[data-podcast].unread > .icon { color: var(--podcast); }
  .row[data-video].unread > .icon { color: var(--video); }
  @media (max-width: 34rem) {
    /* A phone's line: the headline, how long ago, its comments, and who it's from. */
    .headline > time, .headline > .comments, .headline > .tag { margin-left: 0; }
    .headline > .via { display: inline; color: #666; font-size: .8em; white-space: nowrap; }
    .headline > .sep { display: inline; color: #666; font-size: .8em; }
    .posts .row > .source { display: none; }
  }
  /* The video player: a window of its own in the corner of the page, alone on black. */
  .player { max-width: none; max-height: none; padding: 0; border: 0; background: none; color: #fff;
            overflow: visible; }
  /* Its buttons: a thin × to close and a link to copy, each in a circle that lights up faintly on hover. */
  .player-close, .player-copy { position: fixed; top: 1rem; display: grid; place-items: center; width: 2.25rem;
                  height: 2.25rem; padding: 0; border: 0; border-radius: 50%; background: none; color: #777; cursor: pointer;
                  transition: background-color .15s, color .15s; }
  .player-close { right: 1rem; }
  .player-copy { right: 3.5rem; }
  /* Its mark turns to a tick for a moment once the link is taken. */
  .player-copy .copy-done, .player-copy.copied .copy-link { display: none; }
  .player-copy.copied .copy-done { display: block; }
  .player-copy.copied { color: #4ade80; }
  .player-close svg, .player-copy svg { width: 1rem; height: 1rem; fill: none;
                   stroke: currentColor; stroke-width: 1.5; stroke-linecap: round; stroke-linejoin: round; }
  .player-close:hover, .player-close:focus-visible,
  .player-copy:hover, .player-copy:focus-visible { background: #1a1a1a; color: #fff; }
  .player-close:focus, .player-copy:focus { outline: none; }
  /* In the corner: a small window of its own, over the page but out of the way, the page reading and scrolling
     behind it. Its buttons come along, on the video itself, where there's nowhere else to put them. */
  .player.corner { position: fixed; z-index: 3; inset: auto 1.25rem 1.25rem auto; width: min(26rem, calc(100vw - 2rem));
                   border-radius: 12px; overflow: hidden;
                   /* A window of its own, not a hole in the page: a hairline around it, a dark ground under it,
                      and a shadow deep enough to lift it off a page that's black to begin with. */
                   box-shadow: 0 0 0 1px #333, 0 18px 50px rgba(0, 0, 0, .85), 0 2px 8px rgba(0, 0, 0, .6); }
  .player.corner .player-frame { border-radius: 0 0 12px 12px; }
  /* A bar across the top: what the buttons sit in, and what the window is taken hold of by. Above the video
     rather than over it, so it covers none of it. */
  .player-bar { display: none; }
  .player.corner .player-bar { display: block; height: 2rem; cursor: grab; touch-action: none;
                               /* Glass: the list behind it blurred and darkened, so the bar belongs to the
                                  window while still showing there's a page under it. */
                               background: rgba(18, 18, 18, .55); backdrop-filter: blur(14px) saturate(1.4);
                               -webkit-backdrop-filter: blur(14px) saturate(1.4);
                               border-bottom: 1px solid rgba(255, 255, 255, .07); }
  .player.corner.dragging .player-bar { cursor: grabbing; }
  /* In the bar, and above it: the bar is glass, which blurs whatever it's drawn over, and these are drawn
     before it. */
  .player.corner .player-close, .player.corner .player-copy {
                   position: absolute; z-index: 1; top: .15rem; width: 1.7rem; height: 1.7rem; background: none; color: #999; }
  .player.corner .player-close { right: .3rem; }
  .player.corner .player-copy { right: 2.15rem; }
  .player.corner .player-close svg, .player.corner .player-copy svg { width: .95rem; height: .95rem; }
  .player.corner .player-close:hover, .player.corner .player-copy:hover { background: #262626; color: #fff; }

  .player.corner .player-note { padding: 0 .6rem .6rem; }
  /* Coming up in the corner: a short rise and fade, so a window appearing at the edge of the eye is a movement
     rather than a jump; and the video itself fades in when it's there, rather than snapping in over the dark. */
  .player.corner[open] { animation: corner-in .25s ease-out; }
  @keyframes corner-in { from { opacity: 0; transform: translateY(.75rem) scale(.97); } }
  .player-frame > * { opacity: 0; transition: opacity .3s ease-out; }
  .player.ready .player-frame > * { opacity: 1; }
  @media (prefers-reduced-motion: reduce) {
    .player.corner[open] { animation: none; }
    .player-frame > * { transition: none; opacity: 1; }
  }
  /* The way back to the top sits where the video now is, so it steps up over it. */
  body.video-corner .to-top { bottom: calc(4.25rem + min(26rem, 100vw - 2rem) * 0.5625); }  /* Over the bar too. */
  @media (max-width: 34rem) {
    /* A phone: the bar deep enough for a thumb, its buttons with it, and the way back to the top above them. */
    .player.corner .player-bar { height: 2.4rem; }
    .player.corner .player-close, .player.corner .player-copy {
                     top: .3rem; width: 1.9rem; height: 1.9rem; }
    .player.corner .player-close { right: .4rem; }
    .player.corner .player-copy { right: 2.5rem; }
    .player.corner .player-close svg, .player.corner .player-copy svg { width: 1rem; height: 1rem; }
    body.video-corner .to-top { bottom: calc(4.6rem + min(26rem, 100vw - 2rem) * 0.5625); }
  }
  .player-frame { aspect-ratio: 16 / 9; background: #111; }
  /* The whole screen, which YouTube's own button asks for: it takes the frame there and the window around it
     stays where it is, out of the way until the video comes back to it. */
  .player-frame iframe { display: block; width: 100%; height: 100%; border: 0; }
  .player-note { margin: .75rem 0 0; color: #999; font-size: .85rem; }
  .player-note a { color: #fff; text-decoration: underline; text-underline-offset: .2em; }
  .new-posts { position: fixed; z-index: 2; top: calc(env(safe-area-inset-top) + .75rem); left: 50%;
               transform: translateX(-50%); padding: .45rem 1.1rem; border: 0; border-radius: 999px;
               background: #fff; color: #000; font-family: inherit; font-size: .8rem; font-weight: 600; cursor: pointer; }
  a:visited { color: #666; }
  .comments, .comments:visited { margin-left: .6em; color: #666; font-size: .8em; white-space: nowrap; }
  p a, ol a { text-decoration: underline; text-decoration-color: #555; text-underline-offset: .2em; }
  time, .note { margin-left: .6em; white-space: nowrap; }
  footer time { margin: 0; font-size: inherit; }
  /* The sources page. */
  .remove, .remove:visited { margin-left: .8em; color: #666; font-size: .8em; }
  .sources { columns: 2; column-gap: 2.5rem; }
  .sources li { break-inside: avoid; }
  @media (max-width: 34rem) { .sources { columns: 1; } }
  .add-site { display: flex; flex-wrap: wrap; gap: .5rem; }
  .add-site input { flex: 1 1 12rem; min-width: 0; padding: .55rem .75rem; border: 1px solid #333; border-radius: 6px;
                    background: #000; color: #fff; font: inherit; font-size: .9rem; }
  .add-site input::placeholder { color: #555; }
  .add-site input:focus { outline: none; border-color: #888; }
  .add-site button { padding: .55rem 1.3rem; border: 0; border-radius: 999px; background: #fff; color: #000;
                     font: inherit; font-size: .85rem; font-weight: 600; cursor: pointer; }
  .help { color: #777; font-size: .85em; }
  code { color: #ccc; font-size: .9em; }
""".replace("PAGE_START", str(PAGE_SIZE + 1))


def page(title, body, updated=None):
    # The header names the sister site beside this one, where there is one; alone, the newsfeed is the header.
    links = None if SISTER_SITE else [("Newsfeed", "./", True)]
    return shared.page("news", title, body, css=CSS, head=HEAD, symbols=ICON_SYMBOLS, updated=updated, links=links)


def saved(post):
    """A post as feeds.json keeps it."""
    return dict(post, date=post["date"].isoformat() if post["date"] else None)


def restored(kept):
    return dict(kept, date=datetime.fromisoformat(kept["date"]) if kept["date"] else None)


def previous_build():
    """The live page's feeds.json: each feed's posts from the last build, and which feeds were failing."""
    return shared.previous_build(FEEDS_URL, USER_AGENT)


def gather(sites, feeds, previous, built_at):
    """Each feed as it loaded, or, when it failed, its last good posts from the previous build if they're
    recent enough. Returns the feeds; the ones that failed with nothing to fall back on; the ones shown from
    before, with when; and why each failing one failed. Saved copies are keyed by the feed's feeds.txt URL,
    which stays the same when the feed is down."""
    kept_feeds = previous.get("feeds", {})
    gathered, failed, stale, errors = [], [], [], {}
    for site, feed in zip(sites, feeds):
        cutoff = built_at - timedelta(days=site["days"])
        kept = kept_feeds.get(site["url"])
        error = feed.get("error")
        recent = [post for post in (kept or {}).get("posts", []) if not post["date"] or datetime.fromisoformat(post["date"]) >= cutoff]
        # A feed that had posts in its window last time and has none now is more likely broken for the
        # moment (served empty) than suddenly quiet, so it gets the same fallback.
        if not error and not feed["posts"] and recent:
            error = "returned no posts"
        if not error:
            feed["fetched"] = built_at
            gathered.append(feed)
            continue
        name = (kept or {}).get("name") or feed["name"]
        errors[name] = str(error)
        print(f"✗ {name}: {error}", file=sys.stderr)
        if not kept or built_at - datetime.fromisoformat(kept["fetched"]) > FALLBACK_LIMIT:
            failed.append(name)
            gathered.append(feed)
            continue
        # Keep its last good posts, and when they were fetched, so they still age out. Their links are
        # already resolved (to Apple Podcasts, for episodes), so the feed isn't looked up again.
        fetched = datetime.fromisoformat(kept["fetched"])
        stale.append((name, fetched))
        print(f"  {name}: showing its posts from {kept['fetched']} instead", file=sys.stderr)
        gathered.append(dict(feed, name=name, posts=[restored(post) for post in recent][: site["limit"]],
                             fetched=fetched, restored=True))
    return gathered, failed, stale, errors


still_failing = shared.still_failing


def main():
    sites = read_sites()
    with ThreadPoolExecutor(max_workers=8) as pool:
        feeds = list(pool.map(load, sites))

    missing = [post for feed in feeds for post in feed["posts"] if not post["summary"]]
    with ThreadPoolExecutor(max_workers=16) as pool:
        for post, summary in zip(missing, pool.map(lambda post: page_summary(post["link"]), missing)):
            post["summary"] = summary

    for feed in feeds:
        if "error" not in feed:
            previews = sum(1 for post in feed["posts"] if post["summary"])
            print(f"✓ {feed['name']}: {len(feed['posts'])} posts, {previews} with previews, from {feed['feed_url']}")

    built_at = datetime.now(timezone.utc)
    previous = previous_build()
    fill_page_titles(sites, feeds, previous)
    link_to_apple_music(sites, feeds, previous, built_at)
    feeds, failed, stale, errors = gather(sites, feeds, previous, built_at)
    if len(failed) + len(stale) == len(sites) or not any(feed["posts"] for feed in feeds):
        sys.exit("No feeds loaded — not writing the page.")

    # One podcast at a time, to go easy on an API that isn't meant for public use.
    for feed in feeds:
        if not feed.get("restored") and any(post["podcast"] for post in feed["posts"]):
            link_to_apple(feed)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / "static", OUT_DIR, dirs_exist_ok=True)
    posts = all_posts(feeds)
    (OUT_DIR / "index.html").write_text(render_index(feeds, posts, failed, stale, built_at))
    (OUT_DIR / "posts.json").write_text(render_json(posts, built_at))
    (OUT_DIR / "sources.html").write_text(render_sources(feeds, built_at))
    (OUT_DIR / "make-your-own.html").write_text(render_guide(built_at))
    (OUT_DIR / "feeds.opml").write_text(render_opml(feeds, built_at))
    record = {
        "built": built_at.isoformat(),
        "feeds": {
            feed["url"]: {"name": feed["name"], "fetched": feed["fetched"].isoformat(), "posts": [saved(post) for post in feed["posts"]]}
            for feed in feeds if "fetched" in feed
        },
        "failing": still_failing(errors, previous, built_at),
    }
    (OUT_DIR / "feeds.json").write_text(json.dumps(record, ensure_ascii=False))
    print(f"Wrote {OUT_DIR.relative_to(ROOT.parent)}/index.html, posts.json, sources.html, make-your-own.html, "
          "feeds.opml and feeds.json")


if __name__ == "__main__":
    main()
