# Newsfeed

A plain black page listing the latest posts, podcast episodes and videos from the sites you choose, newest
first. No accounts, no tracking, nothing to pay for: GitHub builds it every so often and publishes it.

**[Make your own →](https://news.grahamhagenah.com/make-your-own/)** — the steps in full, with pictures of
what you get.

In short:

1. Press **Use this template** above, and name your repo.
2. In your repo's **Settings → Pages**, set **Source** to **GitHub Actions**.
3. In the **Actions** tab, press the button to enable workflows, then run **Build and deploy**.
4. Your page is at `https://<your username>.github.io/<your repo>/`.

Then add your own sites by editing [`news/feeds.txt`](news/feeds.txt), or from the page's own **Add a site**
form, which opens a pre-filled issue that a bot applies for you.

## Adding a site

One per line. A site's homepage is enough — the build finds its feed:

    https://kottke.org/
    https://www.theguardian.com/news/series/the-long-read/rss   The Long Read   days=7

Anything after the address is the name to show. Then options:

- `limit=5` — at most 5 posts from that site, for one that posts constantly.
- `days=14` — keep its posts for 14 days, for a blog that posts rarely. The default is 3.
- `only="Full Performance"` — only posts whose titles have that in them.

**YouTube channels** work like any site: paste the channel's page. Its videos play on the page itself, in a
small window, and Shorts are left out. **Podcasts** work too: paste the show's feed and its episodes open in
Apple Podcasts.

## How often it updates

Every 15 minutes, in theory. In practice GitHub runs only a fraction of scheduled builds — often fewer than
one in ten — so the page can go a few hours stale. **Actions → Build and deploy → Run workflow** updates it
at once.

To make it reliable, have something else start the build. A free account at [cron-job.org](https://cron-job.org)
does it in about ten minutes:

1. **Make a token.** At [github.com/settings/personal-access-tokens](https://github.com/settings/personal-access-tokens),
   generate a fine-grained token. Under **Repository access** choose **Only select repositories** and pick this
   repo; under **Permissions → Repository permissions** set **Actions** to **Read and write**. Nothing else.
   Copy the token: it can start builds of this one repo and do nothing else.
2. **Make the job.** In cron-job.org, **Create cronjob**:
   - **URL:** `https://api.github.com/repos/<you>/<your repo>/actions/workflows/deploy.yml/dispatches`
   - **Schedule:** every 15 minutes.
   - On the **Advanced** tab, **Request method** `POST`, **Request body** `{"ref":"main"}`, and these headers:
     `Authorization: Bearer <your token>`, `Accept: application/vnd.github+json`,
     `X-GitHub-Api-Version: 2022-11-28`, `Content-Type: application/json`, `User-Agent: cron-job.org`.
   - Turn on its email for failures.
3. **Test run** should answer **204**. A 401 means the token was pasted wrong; a 403 usually means the Actions
   permission or the `User-Agent` header is missing.

The token expires on the date you chose; renew it then and paste the new one into the job.

## Running it on your own computer

    python3 -m news.build     # writes dist/news; open its index.html
    python3 -m unittest       # the feed readers, against saved samples

Nothing but the Python standard library.

---

Built from [grahamhagenah/news](https://github.com/grahamhagenah/news), which also runs
[Pushpin](https://pushpin.city) and a housing tracker. This copy is the newsfeed alone.
