"""Tests for build.py, against saved samples of real feeds (tests/fixtures), so they run offline and a change
that breaks the feed reader fails here instead of on the live page. Run from the repo's top folder with:
python3 -m unittest

The samples are real feeds trimmed to a few posts. When a feed changes and the reader is updated for it,
save a fresh sample alongside the fix.
"""

import html
import json
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from news import build  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "news"
# Which sample stands in for which address. The homepage names its feed, for feed discovery.
ROUTES = {
    "https://www.nytimes.com/": "nyt.xml",
    "https://kottke.org/": "kottke.xml",
    "https://hnrss.org/frontpage?points=150": "hackernews.xml",
    "https://feeds.megaphone.fm/the-big-picture": "bigpicture.xml",
    "https://www.slowboring.com/": "slowboring.xml",
    "https://waxy.org/": "waxy_home.html",
    "https://waxy.org/feed/": "waxy.xml",
    "https://www.youtube.com/feeds/videos.xml?channel_id=UC4eYXhJI4-7wSWc8UNRwD4A": "youtube.xml",
    "https://pitchfork.com/feed/feed-album-reviews/rss": "pitchfork_reviews.xml",
    "https://pitchfork.com/feed/reviews/best/albums/rss": "pitchfork_bnm.xml",
    "https://pitchfork.com/reviews/albums/actress-radical-frame/": "pitchfork_review.html",
    "https://www.reddit.com/r/boston/top/.rss?t=day": "reddit_boston.xml",
}


def sample(url, **kwargs):
    if url not in ROUTES:
        raise OSError(f"no sample for {url}")  # Like a site with no feed at that address.
    return url, (FIXTURES / ROUTES[url]).read_bytes()


def site(url, days=36500, **options):
    """A feeds.txt line. Days reach far back, so the samples' posts don't age out of the tests."""
    return dict(build.parse_site(url), days=days, **options)


class RedditLimit(unittest.TestCase):
    """Reddit's one request a minute."""

    def test_reddit_waits_its_turn(self):
        answers = []

        def answer(url, *args):
            answers.append(url)
            if len(answers) < 3:
                raise build.urllib.error.HTTPError(url, 429, "Too Many Requests", {"x-ratelimit-reset": "25"}, None)
            return sample(url)

        url = "https://www.reddit.com/r/boston/top/.rss?t=day"
        with mock.patch.object(build.shared, "fetch", answer), mock.patch.object(build.time, "sleep") as sleep:
            self.assertEqual(build.fetch(url)[0], url)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [26, 26])

    def test_only_reddit_waits(self):
        def answer(url, *args):
            raise build.urllib.error.HTTPError(url, 429, "Too Many Requests", {"x-ratelimit-reset": "25"}, None)

        with mock.patch.object(build.shared, "fetch", answer), mock.patch.object(build.time, "sleep") as sleep:
            with self.assertRaises(build.urllib.error.HTTPError):
                build.fetch("https://kottke.org/")
        sleep.assert_not_called()


class RedditTurns(unittest.TestCase):
    """Each build loads only a couple of subreddits, the ones loaded longest ago, and shows the rest as before."""

    def test_new_and_oldest_load(self):
        sites = [site(f"https://www.reddit.com/r/{name}/top/.rss?t=day") for name in ("a", "b", "c", "d")] + [site("https://kottke.org/")]
        previous = {"feeds": {sites[0]["url"]: {"fetched": "2026-11-07T10:00:00+00:00"},
                              sites[1]["url"]: {"fetched": "2026-11-07T08:00:00+00:00"},
                              sites[2]["url"]: {"fetched": "2026-11-07T09:00:00+00:00"}}}
        self.assertEqual(build.reddit_turns(sites, previous), {sites[1]["url"], sites[2]["url"], sites[3]["url"]})

    def test_as_before(self):
        now = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
        kept = {"name": "r/a", "fetched": "2026-11-07T10:00:00+00:00",
                "posts": [{"title": "Fresh", "link": "https://example.com/1", "date": "2026-11-07T09:00:00+00:00", "reddit": True},
                          {"title": "Old", "link": "https://example.com/2", "date": "2026-10-01T09:00:00+00:00", "reddit": True}]}
        feed = build.as_before(build.parse_site("https://www.reddit.com/r/a/top/.rss?t=day"), kept, now)
        self.assertEqual([post["title"] for post in feed["posts"]], ["Fresh"])
        self.assertEqual(feed["fetched"], datetime(2026, 11, 7, 10, tzinfo=timezone.utc), "keeps when it was loaded")
        gathered, failed, stale, errors = build.gather([build.parse_site(feed["url"])], [feed], {"feeds": {feed["url"]: kept}}, now)
        self.assertEqual(gathered[0]["fetched"], feed["fetched"])
        self.assertEqual((failed, stale, errors), ([], [], {}))


class Reading(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(build, "fetch", sample)
        patcher.start()
        self.addCleanup(patcher.stop)

    def read(self, url):
        feed = build.read_feed(site(url))
        self.assertTrue(feed["posts"], f"no posts read from the sample for {url}")
        for post in feed["posts"]:
            self.assertTrue(post["title"].strip())
            self.assertTrue(post["link"].startswith("http"), post["link"])
            self.assertTrue(post["date"] is None or post["date"].tzinfo is not None)
            self.assertIsInstance(post["summary"], list)
        return feed

    def test_rss(self):
        feed = self.read("https://www.nytimes.com/")
        self.assertTrue(feed["name"])
        self.assertFalse(any(post["podcast"] for post in feed["posts"]))

    def test_atom(self):
        self.read("https://kottke.org/")

    def test_hacker_news_comments(self):
        feed = self.read("https://hnrss.org/frontpage?points=150")
        self.assertTrue(all("news.ycombinator.com" in post["comments"] for post in feed["posts"]))
        self.assertTrue(all(isinstance(post["comment_count"], int) for post in feed["posts"]))
        self.assertFalse(any(line.startswith(("Points:", "Article URL")) for post in feed["posts"] for line in post["summary"]))

    def test_reddit_posts_link_past_reddit(self):
        feed = self.read("https://www.reddit.com/r/boston/top/.rss?t=day")
        posts = {post["title"][:20]: post for post in feed["posts"]}
        article = posts["Mass. voters narrowl"]
        self.assertTrue(article["link"].startswith("https://www.bostonglobe.com/"), "an article goes to the article")
        self.assertIn("reddit.com/r/boston/comments/", article["comments"], "with its thread as the comments")
        self.assertEqual(article["summary"], [], "no 'submitted by' or [link] [comments] as a summary")
        image = posts["Sunsets in boston ar"]
        self.assertTrue(image["link"].startswith("https://i.redd.it/"), "an image is the image itself")
        text = posts["Turn your headlights"]
        self.assertIn("reddit.com/r/boston/comments/", text["link"], "a post that's only text is its thread")
        self.assertIsNone(text["comments"], "already one click away")
        self.assertTrue(text["summary"], "with its text as the summary")
        gallery = posts["It's always the ones"]
        self.assertIn("/comments/", gallery["link"], "a gallery is its thread, not Reddit's gallery viewer")
        self.assertEqual(posts["Phil Eng defies City"]["video"], "Y44jT-qoCEs", "a YouTube video plays here")
        self.assertTrue(all(post["reddit"] for post in feed["posts"]))
        page = build.render_index([feed], build.all_posts([feed]), [], [], datetime.now(timezone.utc))
        self.assertIn('<button data-show="reddit">', page, "a filter of their own")
        self.assertNotIn('<button data-show="videos">', page, "a video shared on Reddit is a Reddit post")
        self.assertEqual(page.count(" data-reddit"), len(feed["posts"]))
        self.assertIn('data-video="Y44jT-qoCEs" data-reddit', page, "and still plays here")

    def test_bare_ampersands(self):
        body = (b'<?xml version="1.0"?><rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel>'
                b'<title>Rest of World</title><item><title>A story &amp; more</title><link>https://example.com/a</link>'
                b'<media:content url="https://example.com/a.jpg?width=380&dpr=2"/></item></channel></rss>')
        root = build.parse_xml(body)
        self.assertIsNotNone(root)
        self.assertEqual(build.child_text(build.children(root, "channel")[0], "title"), "Rest of World")

    def test_youtube_api_when_a_channel_feed_fails(self):
        feed_url = "https://www.youtube.com/feeds/videos.xml?channel_id=UC4eYXhJI4-7wSWc8UNRwD4A"
        asked = []

        def answer(url, **kwargs):
            asked.append(url)
            if url.startswith(build.YOUTUBE_PLAYLIST_API):
                return url, (FIXTURES / "youtube_api.json").read_bytes()
            raise OSError("HTTP Error 404: Not Found")

        with mock.patch.object(build, "fetch", answer), mock.patch.dict(os.environ, {"YOUTUBE_KEY": "secret-key"}):
            feed = build.load(site(f"{feed_url}   Tiny Desk"))
        self.assertNotIn("error", feed)
        self.assertEqual([post["title"] for post in feed["posts"]], ["Tori Kelly: Tiny Desk Concert", "Ratboys: Tiny Desk Concert"])
        self.assertEqual(feed["posts"][0]["video"], "dQw4w9WgXcQ")
        self.assertEqual(feed["posts"][0]["link"], "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertTrue(feed["posts"][0]["summary"][0].startswith("Tori Kelly sings"))
        self.assertEqual(feed["url"], feed_url, "listed under its feed, as before")
        self.assertIn("playlistId=UULF4eYXhJI4-7wSWc8UNRwD4A", asked[-1], "its uploads without Shorts")

    def test_youtube_api_error_keeps_the_key_out(self):
        def answer(url, **kwargs):
            raise OSError(f"HTTP Error 400: Bad Request for {url}")
        with mock.patch.object(build, "fetch", answer), mock.patch.dict(os.environ, {"YOUTUBE_KEY": "secret-key"}):
            feed = build.load(site("https://www.youtube.com/feeds/videos.xml?channel_id=UC4eYXhJI4-7wSWc8UNRwD4A"))
        self.assertIn("the YouTube API", feed["error"])
        self.assertNotIn("secret-key", feed["error"])

    def test_no_youtube_api_without_a_key(self):
        asked = []
        def answer(url, **kwargs):
            asked.append(url)
            raise OSError("HTTP Error 404: Not Found")
        with mock.patch.object(build, "fetch", answer), mock.patch.dict(os.environ, {"YOUTUBE_KEY": ""}):
            feed = build.load(site("https://www.youtube.com/feeds/videos.xml?channel_id=UC4eYXhJI4-7wSWc8UNRwD4A"))
        self.assertIn("404", feed["error"])
        self.assertFalse(any(url.startswith(build.YOUTUBE_PLAYLIST_API) for url in asked))

    def test_youtube_videos_without_shorts(self):
        feed = self.read("https://www.youtube.com/feeds/videos.xml?channel_id=UC4eYXhJI4-7wSWc8UNRwD4A")
        self.assertEqual([(post["title"], post["video"]) for post in feed["posts"]],
                         [("Tori Kelly: Tiny Desk Concert", "O8AlOZV0BSI"), ("Ratboys: Tiny Desk Concert", "oRg3Tk3jHno")])
        self.assertFalse(any(post["podcast"] for post in feed["posts"]))
        # Its description, from YouTube's <media:group>, without the byline it starts with.
        self.assertTrue(feed["posts"][0]["summary"][0].startswith("When Tori Kelly pulled up to the Desk"))

    def test_podcast_episodes_without_web_pages(self):
        feed = self.read("https://feeds.megaphone.fm/the-big-picture")
        self.assertTrue(all(post["podcast"] and post["audio"] for post in feed["posts"]))
        self.assertTrue(any(post["link"] == post["audio"] for post in feed["posts"]),
                        "an episode with no page of its own links to its audio")

    def test_newsletter_previews(self):
        feed = self.read("https://www.slowboring.com/")
        self.assertTrue(all(post["summary"] for post in feed["posts"]))

    def test_only_posts_whose_titles_have_a_phrase(self):
        line = 'https://www.youtube.com/feeds/videos.xml?channel_id=UC4eYXhJI4-7wSWc8UNRwD4A   Tiny Desk   days=36500   only="ratboys: tiny"'
        parsed = build.parse_site(line)
        self.assertEqual((parsed["name"], parsed["only"], parsed["days"]), ("Tiny Desk", "ratboys: tiny", 36500))
        feed = build.load(parsed)
        self.assertEqual([post["title"] for post in feed["posts"]], ["Ratboys: Tiny Desk Concert"])

    def test_tag_and_page_titles_options(self):
        parsed = build.parse_site('https://pitchfork.com/feed/reviews/best/albums/rss   Pitchfork   days=30   titles=page   tag="BNM"')
        self.assertEqual((parsed["name"], parsed["days"], parsed["page_titles"], parsed["tag"]), ("Pitchfork", 30, True, "BNM"))
        plain = build.parse_site("https://pitchfork.com/feed/feed-album-reviews/rss   Pitchfork")
        self.assertEqual((plain["page_titles"], plain["tag"]), (False, ""))

    def test_page_titles_add_what_the_feed_leaves_out(self):
        sites = [site("https://pitchfork.com/feed/feed-album-reviews/rss", page_titles=True)]
        feeds = [build.read_feed(sites[0])]
        self.assertEqual(feeds[0]["posts"][0]["title"], "Radical Frame", "the feed has only the album")
        build.fill_page_titles(sites, feeds, {})
        titles = [post["title"] for post in feeds[0]["posts"]]
        self.assertEqual(titles[0], "Actress: Radical Frame")
        self.assertEqual(titles[1], "Fell Asleep in the Sun", "a page that can't be had keeps the feed's title")

    def test_page_titles_come_from_the_last_build_when_it_had_them(self):
        sites = [site("https://pitchfork.com/feed/feed-album-reviews/rss", page_titles=True)]
        feeds = [build.read_feed(sites[0])]
        link = feeds[0]["posts"][1]["link"]
        previous = {"feeds": {"x": {"posts": [{"link": link, "title": "After: Fell Asleep in the Sun"}]}}}
        with mock.patch.object(build, "page_title", return_value="") as fetched:
            build.fill_page_titles(sites, feeds, previous)
        self.assertNotIn(mock.call(link), fetched.call_args_list, "fetched a page the last build had")
        self.assertEqual(feeds[0]["posts"][1]["title"], "After: Fell Asleep in the Sun")

    def test_a_tagged_feed_tags_its_posts(self):
        feed = build.read_feed(site("https://pitchfork.com/feed/reviews/best/albums/rss", tag="BNM"))
        self.assertTrue(all(post["tag"] == "BNM" for post in feed["posts"]))

    def test_finds_a_homepages_feed(self):
        feed = self.read("https://waxy.org/")
        self.assertEqual(feed["feed_url"], "https://waxy.org/feed/")

    def test_a_site_without_a_feed_is_an_error(self):
        feed = build.load(site("https://example.com/"))
        self.assertIn("error", feed)
        self.assertEqual(feed["posts"], [])

    def test_limit_and_days(self):
        self.assertEqual(len(build.read_feed(site("https://www.nytimes.com/", limit=2))["posts"]), 2)
        self.assertEqual(build.read_feed(site("https://www.nytimes.com/", days=0))["posts"], [])


class ApplePodcasts(unittest.TestCase):
    EPISODES = "https://podcasts.apple.com/us/podcast"
    SHOW = f"{EPISODES}/the-big-picture/id1080911743"

    def apple(self, url, asked):
        name = "apple_search.json" if url == build.APPLE_SEARCH_URL else "apple_lookup.json"
        self.asked.append((name, asked))
        return json.loads((FIXTURES / name).read_text())

    def setUp(self):
        self.asked = []
        with mock.patch.object(build, "fetch", sample):
            self.feed = build.read_feed(site("https://feeds.megaphone.fm/the-big-picture"))

    def links(self):
        with mock.patch.object(build, "apple_json", self.apple):
            build.link_to_apple(self.feed)
        return [post["link"] for post in self.feed["posts"]]

    def test_links_an_episode_by_its_audio_file(self):
        # Apple's own title for it is shorter than the feed's, so the audio file is what finds it.
        self.assertEqual(self.links()[0], f"{self.EPISODES}/the-winners-and-losers-of-the-summer/id1080911743?i=1000790786635")

    def test_links_an_episode_by_its_title(self):
        # Apple serves this one from a tracking address, so the title, punctuation aside, is what finds it.
        self.assertEqual(self.links()[1], f"{self.EPISODES}/the-10-best-movies/id1080911743?i=1000789112233")

    def test_opens_the_show_for_an_episode_apple_hasnt_picked_up(self):
        self.assertEqual(self.links()[2], self.SHOW)

    def test_finds_the_show_by_the_feed_it_carries(self):
        # A search by name returns another show first; the feed address is what tells them apart.
        self.links()
        self.assertEqual(self.asked[0][1]["term"], "The Big Picture")
        self.assertEqual(self.asked[1][1]["id"], "1080911743")

    def test_takes_the_show_a_feed_names(self):
        self.feed["apple"] = "1080911743"
        self.links()
        self.assertEqual([name for name, _ in self.asked], ["apple_lookup.json"])

    def test_keeps_web_links_when_apple_cannot_be_reached(self):
        before = [post["link"] for post in self.feed["posts"]]
        with mock.patch.object(build, "apple_json", side_effect=OSError("no answer")):
            build.link_to_apple(self.feed)
        self.assertEqual([post["link"] for post in self.feed["posts"]], before)

    def test_leaves_a_newsletter_with_episodes_alone(self):
        feed = {"name": "Mixed", "feed_url": "x", "apple": "", "posts": [
            {"title": "Episode", "link": "https://a", "audio": "https://a.mp3", "podcast": True},
            {"title": "Essay", "link": "https://b", "audio": None, "podcast": False},
        ]}
        with mock.patch.object(build, "apple_json", side_effect=AssertionError("looked up")):
            build.link_to_apple(feed)
        self.assertEqual(feed["posts"][0]["link"], "https://a")


class Fallback(unittest.TestCase):
    built = datetime(2026, 9, 20, 16, 0, tzinfo=timezone.utc)
    blog = site("https://blog.example/", days=3, name="A Blog")

    def post(self, hours_ago, title="Post"):
        return {"title": title, "link": f"https://blog.example/{title}", "date": self.built - timedelta(hours=hours_ago),
                "summary": [], "comments": None, "comment_count": None, "audio": None, "podcast": False}

    def feed(self, posts=(), error=None):
        feed = {"name": "A Blog", "url": self.blog["url"], "feed_url": "https://blog.example/feed", "apple": "", "posts": list(posts)}
        return dict(feed, error=error) if error else feed

    def previous(self, hours_old, post_hours_ago=10):
        kept = build.saved(dict(self.post(post_hours_ago, "Kept")))
        return {"feeds": {self.blog["url"]: {"name": "A Blog", "fetched": (self.built - timedelta(hours=hours_old)).isoformat(), "posts": [kept]}}}

    def gather(self, feed, previous):
        return build.gather([self.blog], [feed], previous, self.built)

    def test_fresh(self):
        feeds, failed, stale, errors = self.gather(self.feed([self.post(1)]), self.previous(1))
        self.assertEqual(([p["title"] for p in feeds[0]["posts"]], failed, stale, errors), (["Post"], [], [], {}))
        self.assertEqual(feeds[0]["fetched"], self.built)

    def test_error_uses_recent_posts(self):
        feeds, failed, stale, errors = self.gather(self.feed(error="HTTP Error 520"), self.previous(6))
        self.assertEqual([p["title"] for p in feeds[0]["posts"]], ["Kept"])
        self.assertTrue(feeds[0]["restored"])
        self.assertEqual(feeds[0]["fetched"], self.built - timedelta(hours=6), "the copy keeps its age")
        self.assertEqual([name for name, _ in stale], ["A Blog"])
        self.assertEqual(errors, {"A Blog": "HTTP Error 520"})

    def test_error_with_old_copy_fails(self):
        feeds, failed, stale, _ = self.gather(self.feed(error="timed out"), self.previous(72))
        self.assertEqual((feeds[0]["posts"], failed, stale), ([], ["A Blog"], []))

    def test_empty_after_having_posts_uses_them(self):
        feeds, failed, stale, errors = self.gather(self.feed(), self.previous(1))
        self.assertEqual([p["title"] for p in feeds[0]["posts"]], ["Kept"])
        self.assertEqual(errors["A Blog"], "returned no posts")

    def test_quiet_blog_is_not_a_failure(self):
        # Its last post has aged out of its three-day window, so nothing is missing.
        feeds, failed, stale, errors = self.gather(self.feed(), self.previous(1, post_hours_ago=100))
        self.assertEqual((feeds[0]["posts"], failed, stale, errors), ([], [], [], {}))

    def test_failing_since_carries_over(self):
        earlier = {"failing": {"A Blog": {"since": "2026-09-20T01:00:00+00:00", "error": "520"}}}
        failing = build.still_failing({"A Blog": "timed out"}, earlier, self.built)
        self.assertEqual(failing["A Blog"], {"since": "2026-09-20T01:00:00+00:00", "error": "timed out"})

    def test_a_post_in_two_feeds_shows_once_with_its_tag(self):
        now = datetime.now(timezone.utc)
        post = lambda link, tag="": {"title": link, "link": link, "date": now, "tag": tag}
        feeds = [{"name": "Pitchfork", "posts": [post("a"), post("b")]},
                 {"name": "Pitchfork", "posts": [post("b", "BNM"), post("c", "BNM")]}]
        posts = build.all_posts(feeds)
        self.assertEqual(sorted(p["link"] for p in posts), ["a", "b", "c"])
        self.assertEqual({p["link"]: p["tag"] for p in posts}, {"a": "", "b": "BNM", "c": "BNM"})

    def test_saved_posts_survive_the_round_trip(self):
        post = self.post(2)
        self.assertEqual(build.restored(json.loads(json.dumps(build.saved(post)))), post)


class Episodes(unittest.TestCase):
    def test_length_from_itunes_duration_however_written(self):
        entry = lambda said: build.ET.fromstring(f'<item xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">'
                                                  f'<itunes:duration>{said}</itunes:duration></item>')
        self.assertEqual(build.entry_duration(entry("1:02:03")), 3723)
        self.assertEqual(build.entry_duration(entry("62:03")), 3723)
        self.assertEqual(build.entry_duration(entry("3723")), 3723)
        self.assertIsNone(build.entry_duration(entry("about an hour")))
        self.assertEqual([build.episode_length(s) for s in (3723, 2880, 3600, 20)], ["1h 2m", "48m", "1h", "1m"])

    def test_picture_its_own_else_its_shows(self):
        show = build.ET.fromstring('<channel xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">'
                                   '<itunes:image href="https://example.com/show.jpg"/></channel>')
        own = build.ET.fromstring('<item xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">'
                                  '<itunes:image href="https://example.com/episode.jpg"/></item>')
        self.assertEqual(build.entry_artwork(own, show), "https://example.com/episode.jpg")
        self.assertEqual(build.entry_artwork(build.ET.fromstring("<item/>"), show), "https://example.com/show.jpg")


class Page(unittest.TestCase):
    def test_renders_posts_filter_and_failures(self):
        with mock.patch.object(build, "fetch", sample):
            feeds = [build.read_feed(site(url)) for url in ("https://www.nytimes.com/", "https://feeds.megaphone.fm/the-big-picture",
                                                            "https://www.youtube.com/feeds/videos.xml?channel_id=UC4eYXhJI4-7wSWc8UNRwD4A")]
        posts = build.all_posts(feeds)
        page = build.render_index(feeds, posts, ["Broken Blog"], [("Slow Site", datetime.now(timezone.utc))], datetime.now(timezone.utc))
        self.assertEqual(page.count('<span class="source">'), len(posts))
        self.assertEqual(len(re.findall(r'<li class="row" data-from="[^"]*" data-podcast data-audio="https?://[^"]+"'
                                        r' data-art="https?://[^"]+" data-duration="\d+">', page)),
                         sum(post["podcast"] for post in posts), "each episode with its audio, picture and length, to play here")
        self.assertIn('<span class="length">3h 4m</span>', page, "an episode's length, after how long ago it was posted")
        # Its page stays a link beside its name, which plays it; and the player's bar is on the page, unseen till then.
        self.assertEqual(page.count('">Apple Podcasts</a>') + page.count('">Episode page</a>'),
                         sum(post["podcast"] and post["link"] != post["audio"] for post in posts), "where it has a page")
        self.assertIn('<section class="listen" aria-label="Podcast player" hidden>', page)
        self.assertIn("navigator.mediaSession", page, "the lock screen's and headphones' controls")
        speeds = page.split('<div class="listen-speeds" role="menu" aria-label="Speed" hidden>')[1].split("</div>")[0]
        self.assertEqual(re.findall(r'data-speed="([\d.]+)"', speeds), ["0.75", "1", "1.25", "1.5", "1.75", "2"], "its speeds, in a menu")
        self.assertIn('data-show="podcasts"', page)
        self.assertIn('data-show="videos"', page)
        self.assertEqual(len(re.findall(r'<li class="row" data-from="[^"]*" data-video="', page)), 2)
        # A menu of the sources on the page, beside the search.
        menu = page.split('<select class="pick" aria-label="Source">')[1].split("</select>")[0]
        self.assertEqual(re.findall(r'<option value="([^"]*)">', menu)[0], "", "All sources first")
        self.assertEqual({html.unescape(name) for name in re.findall(r'<option value="([^"]+)">', menu)},
                         {post["source"] for post in posts})
        self.assertIn('<dialog class="player"', page)
        self.assertIn("Couldn’t load Broken Blog", page)
        self.assertIn("Couldn’t reach Slow Site", page)

    def test_rss_feed(self):
        import xml.etree.ElementTree as ET
        with mock.patch.object(build, "fetch", sample):
            feeds = [build.read_feed(site(url)) for url in ("https://www.nytimes.com/", "https://feeds.megaphone.fm/the-big-picture")]
        posts = build.all_posts(feeds)
        channel = ET.fromstring(build.render_rss(posts, datetime.now(timezone.utc))).find("channel")
        items = channel.findall("item")
        self.assertEqual(len(items), len(posts))
        self.assertEqual([item.findtext("link") for item in items], [post["link"] for post in posts])
        self.assertEqual([item.findtext("title") for item in items], [post["title"] for post in posts])
        self.assertEqual(sum(item.find("enclosure") is not None for item in items), sum(post["podcast"] for post in posts))
        self.assertTrue(all(item.findtext("pubDate") for item in items if posts[items.index(item)]["date"]))

    def test_articles_feed_leaves_out_episodes_and_videos(self):
        import xml.etree.ElementTree as ET
        with mock.patch.object(build, "fetch", sample):
            feeds = [build.read_feed(site(url)) for url in ("https://www.nytimes.com/", "https://feeds.megaphone.fm/the-big-picture",
                                                            "https://www.youtube.com/feeds/videos.xml?channel_id=UC4eYXhJI4-7wSWc8UNRwD4A")]
        posts = build.all_posts(feeds)
        channel = ET.fromstring(build.render_rss(posts, datetime.now(timezone.utc), "articles.xml")).find("channel")
        links = [item.findtext("link") for item in channel.findall("item")]
        articles = [post["link"] for post in posts if not post["podcast"] and not post["video"]]
        self.assertTrue(articles)
        self.assertLess(len(articles), len(posts))
        self.assertEqual(links, articles)
        self.assertIsNone(channel.find("item/enclosure"))
        self.assertTrue(channel.find("{http://www.w3.org/2005/Atom}link").get("href").endswith("/articles.xml"))

    def test_artist_and_title(self):
        self.assertEqual(build.artist_and_title("Beck: Ride Lonesome"), ("Beck", "Ride Lonesome"))
        self.assertEqual(build.artist_and_title("Yung Lean: “That’s It” [ft. Future]"), ("Yung Lean", "That's It"))
        self.assertEqual(build.artist_and_title("Fell Asleep in the Sun"), ("", "Fell Asleep in the Sun"))


class AppleMusic(unittest.TestCase):
    """Links to reviewed albums and songs, against a stand-in for Apple's search."""

    RESULTS = {"results": [
        {"artistName": "Some Tribute Band", "collectionName": "Ride Lonesome", "collectionViewUrl": "https://music.apple.com/us/album/wrong/1"},
        {"artistName": "Beck", "collectionName": "Ride Lonesome", "collectionViewUrl": "https://music.apple.com/us/album/ride-lonesome/2?uo=4"},
    ]}

    def feeds(self, *titles, music="album"):
        now = datetime.now(timezone.utc)
        sites = [dict(build.parse_site("https://pitchfork.com/feed/feed-album-reviews/rss"), music=music)]
        return sites, [{"posts": [{"title": title, "link": f"https://pitchfork.com/{i}", "date": now} for i, title in enumerate(titles)]}]

    def test_links_the_same_artists_record(self):
        with mock.patch.object(build, "apple_json", return_value=self.RESULTS):
            self.assertEqual(build.apple_music("album", "Beck: Ride Lonesome"), "https://music.apple.com/us/album/ride-lonesome/2")

    def test_someone_elses_record_of_the_same_name_is_no_match(self):
        with mock.patch.object(build, "apple_json", return_value=self.RESULTS):
            self.assertIsNone(build.apple_music("album", "Actress: Ride Lonesome"))

    def test_falls_back_to_apple_musics_search(self):
        sites, feeds = self.feeds("Actress: Radical Frame")
        with mock.patch.object(build, "apple_json", return_value={"results": []}):
            build.link_to_apple_music(sites, feeds, {}, datetime.now(timezone.utc))
        post = feeds[0]["posts"][0]
        self.assertEqual(post["music"], "https://music.apple.com/us/search?term=Actress+Radical+Frame")
        self.assertFalse(post["music_found"])

    def test_keeps_a_found_link_and_rechecks_a_search_after_a_day(self):
        now = datetime.now(timezone.utc)
        sites, feeds = self.feeds("Beck: Ride Lonesome", "Actress: Radical Frame", "Fine: Everything")
        previous = {"feeds": {"x": {"posts": [
            {"link": "https://pitchfork.com/0", "music": "https://music.apple.com/found", "music_found": True, "music_checked": (now - timedelta(days=9)).isoformat()},
            {"link": "https://pitchfork.com/1", "music": "https://music.apple.com/us/search?term=a", "music_found": False, "music_checked": (now - timedelta(hours=2)).isoformat()},
            {"link": "https://pitchfork.com/2", "music": "https://music.apple.com/us/search?term=b", "music_found": False, "music_checked": (now - timedelta(days=2)).isoformat()},
        ]}}}
        with mock.patch.object(build, "apple_json", return_value={"results": []}) as searched:
            build.link_to_apple_music(sites, feeds, previous, now)
        self.assertEqual(searched.call_count, 1, "only the search from two days ago is tried again")
        self.assertEqual([post["music"] for post in feeds[0]["posts"]][:2],
                         ["https://music.apple.com/found", "https://music.apple.com/us/search?term=a"])

    def test_a_collaboration_credited_differently(self):
        results = {"results": [{"artistName": "Charli xcx, Robyn & Yung Lean", "trackName": "360 featuring robyn & yung lean",
                                "trackViewUrl": "https://music.apple.com/us/album/360/1?i=2&uo=4"}]}
        with mock.patch.object(build, "apple_json", return_value=results):
            self.assertEqual(build.apple_music("song", "Yung Lean / Charli xcx: “360”"), "https://music.apple.com/us/album/360/1?i=2")
            self.assertIsNone(build.apple_music("song", "Yung Lean / Metro Boomin: “That’s It”"), "one artist in common isn't enough")

    def test_artist_names(self):
        self.assertEqual(build.artist_names("Yung Lean / Metro Boomin"), {"yung lean", "metro boomin"})
        self.assertEqual(build.artist_names("Florence and the Machine"), {"florence and the machine"})

    def test_a_failed_search_still_gives_a_link(self):
        sites, feeds = self.feeds("Beck: Ride Lonesome")
        with mock.patch.object(build, "apple_json", side_effect=OSError("down")):
            build.link_to_apple_music(sites, feeds, {}, datetime.now(timezone.utc))
        self.assertIn("music.apple.com/us/search", feeds[0]["posts"][0]["music"])

    def test_the_option(self):
        self.assertEqual(build.parse_site("https://x/rss   Pitchfork   music=song")["music"], "song")
        self.assertEqual(build.parse_site("https://x/rss   Pitchfork")["music"], "")


class Tags(unittest.TestCase):
    def test_best_new_track_is_spelled_out(self):
        self.assertEqual(build.render_tag("BNT"), '<span class="tag" title="Best New Track"><img src="bnm.svg" '
                         'alt="Best New Track" width="17" height="9"><span aria-hidden="true">BNT</span></span>')
        self.assertEqual(build.render_tag("Staff pick"), '<span class="tag">Staff pick</span>')

    def test_tags_show_beside_titles(self):
        with mock.patch.object(build, "fetch", sample):
            feeds = [build.read_feed(site("https://pitchfork.com/feed/reviews/best/albums/rss", tag="BNM"))]
        posts = build.all_posts(feeds)
        page = build.render_index(feeds, posts, [], [], datetime.now(timezone.utc))
        self.assertEqual(page.count('</a> <span class="tag" title="Best New Music"><img src="bnm.svg" alt="Best New Music" '
                                    'width="17" height="9"><span aria-hidden="true">BNM</span></span>'), len(posts))




if __name__ == "__main__":
    unittest.main()
