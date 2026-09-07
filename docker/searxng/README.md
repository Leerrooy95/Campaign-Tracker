# SearXNG for Campaign Tracker

Backend for `statements.py` (Track B — what a candidate *said*, gathered by
web search across neutral angles). Optional: without it, that stage is
skipped and disclosed, and money + legislative record + votes still run
complete. No paid search API is used anywhere in this project by design —
this is the free, self-hosted route (see the repo's `CLAUDE.md`).

## Start it

```bash
cd docker/searxng
cp .env.example .env                                       # port override only
sed -i "s|ultrasecretkey|$(openssl rand -hex 32)|g" settings.yml
  # Mac: sed -i '' "s|ultrasecretkey|$(openssl rand -hex 32)|g" settings.yml
docker compose up -d
docker compose logs -f   # confirm it started; Ctrl+C to stop watching
```

SearXNG is now at `http://localhost:8080`. Point the main app at it — either
edit `SEARXNG_URL` in `run.sh` (it already defaults to
`http://localhost:8080`), or export it by hand:

```bash
export SEARXNG_URL=http://localhost:8080
```

## Verify the JSON API actually works

This is the one thing a stock SearXNG install does NOT do out of the box —
`settings.yml` in this folder turns it on. Confirm it before running the main
app:

```bash
curl -s "http://localhost:8080/search?q=test&format=json" | head -c 200
```

You should see JSON (a `{"query": "test", ...` payload), not an HTML page or
a 403. If you get a 403, `settings.yml` likely isn't being picked up — check
`docker compose logs` for a mount error.

**If statements.py keeps returning 0 statements, check `unresponsive_engines`
in that same response** (pipe through `python3 -m json.tool` instead of
`head -c 200` to see it):

```bash
curl -s "http://localhost:8080/search?q=test&format=json" | python3 -m json.tool
```

A general-search engine that's being blocked shows up here as
`["<engine>", "too many requests"]` (or a timeout). Brave's free scraper
engine (`brave` — not the paid Brave Search API, which this project never
uses) does this reliably even from a single low-volume self-hosted instance,
so `settings.yml` disables it by default; that's what the `engines:` block
at the bottom of the file is for. If another engine starts showing up the
same way, disable it the same way — add a `- name: <engine>` /
`disabled: true` pair to that block rather than touching anything else, since
`use_default_settings: true` means every other engine keeps its shipped
default.

## Stop / reset

```bash
docker compose down          # stop, keep the cache volume
docker compose down -v       # stop and wipe the cache volume too
```

## Why this compose file is minimal

This is one local SearXNG instance answering one local script's own JSON
queries — not a public search engine. So it deliberately skips the reverse
proxy and Valkey cache container the official
[searxng-docker](https://github.com/searxng/searxng-docker) setup runs (those
exist to support the rate limiter/bot-detection a public-facing instance
needs), and it binds to `127.0.0.1` only. If you want to run a public or
multi-user SearXNG instance instead, use searxng-docker directly and just
point `SEARXNG_URL` at it — keep it behind Cloudflare Access or Tailscale if
it's reachable from the internet, per `CLAUDE.md`.
