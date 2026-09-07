#!/usr/bin/env bash
# run.sh — start Campaign Tracker.
#
# Your keys go in a `.env` file (see `.env.example`), NOT in this script.
# This file IS tracked in git; `.env` is gitignored on purpose, so a
# `git add -A` / `git commit -a` can never accidentally publish a live key —
# see Security_Recommendations.md ("run.sh as the tracked home for live API
# keys"). Earlier versions of this script had you paste keys directly into
# clearly-marked slots here; if you're updating from one of those, move your
# values into `.env` the same way.
#
# One-time setup:
#     cp .env.example .env
#     $EDITOR .env             # paste FEC_API_KEY / CONGRESS_API_KEY /
#                               # SEARXNG_URL in — .env.example says where to
#                               # get free keys and what each stage needs
# Then, every time:
#     chmod +x run.sh          # one time, makes it runnable
#     ./run.sh                 # starts the tool at http://localhost:5000
#
# Stop it with Ctrl+C.
#
# No `.env` yet, or FEC_API_KEY left blank? The app still runs — it falls
# back to DEMO MODE on synthetic fixtures, good enough to confirm the
# install works before you invest in real keys.
#
# The Anthropic key (plain-language report) is NOT set here on purpose —
# paste it into the web UI's "Anthropic API key" field per run instead; see
# .env.example.
#
# Want statements (what the candidate SAID)? That track needs SearXNG —
# optional, see docker/searxng/README.md for setup (kept there, not
# duplicated here, so there's exactly one place to keep in sync).

cd "$(dirname "$0")"   # run from the tool's folder no matter where you call it

if [ -f .env ]; then
    set -a   # export every var .env defines, without listing them by name
    # shellcheck disable=SC1091
    source .env
    set +a
else
    echo "No .env found — copy .env.example to .env and add your keys for" >&2
    echo "real data (FEC/Congress). Running without one is fine too: the" >&2
    echo "app falls back to DEMO MODE on synthetic fixtures." >&2
fi

# --- launch ---
python3 app.py
