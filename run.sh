#!/usr/bin/env bash
# run.sh — start Campaign Tracker with your keys.
#
# This is where the keys go. There are NO key fields in the web UI for the
# Congress API or SearXNG — the tool reads them from the environment, and this
# script sets them, then launches the app. Edit the three values below, save,
# then from a terminal:
#
#     chmod +x run.sh      # one time, makes it runnable
#     ./run.sh             # starts the tool at http://localhost:5000
#
# Stop it with Ctrl+C. To stop using a key, just blank it out ("").

cd "$(dirname "$0")"   # run from the tool's folder no matter where you call it

# --- 1. FEC key (money side). Free from https://api.open.fec.gov/developers/ ---
export FEC_API_KEY=""

# --- 2. Congress key (legislative record). Free from https://api.data.gov/signup/ ---
export CONGRESS_API_KEY=""

# --- 3. SearXNG (statement gathering). Use the SAME URL your SearXNG web UI
#        opens at in the browser. Common default is http://localhost:8080.
#        Make sure SearXNG is running first — from the repo root:
#            cd docker/searxng && cp .env.example .env
#            sed -i "s|ultrasecretkey|$(openssl rand -hex 32)|g" settings.yml
#            docker compose up -d
#        See docker/searxng/README.md for details and how to verify it.     ---
export SEARXNG_URL="http://localhost:8080"


# --- Anthropic key (translation layer / plain-language report) is NOT set
#     here on purpose. Paste it into the "Anthropic API key" field in the web
#     UI for each run instead — it's used for that request only and never
#     stored, so there's nothing to remember to scrub before zipping. ---

# --- launch ---
python3 app.py
