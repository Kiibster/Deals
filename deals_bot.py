#!/usr/bin/env python3
"""
Canadian Deals Bot
------------------
Watches Canadian deal feeds (RedFlagDeals Hot Deals, SmartCanucks, deal
subreddits) and posts every NEW deal to a Discord channel via webhook.

Uses only the Python standard library - nothing to install.

Usage:
    python deals_bot.py --once    # check feeds one time, then exit (for GitHub Actions)
    python deals_bot.py --loop    # keep running, check every few minutes (for a PC/server)

Configuration lives in config.json next to this file.
The Discord webhook URL can be set either in config.json ("webhook_url")
or via the DISCORD_WEBHOOK_URL environment variable (env var wins).
"""

import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
STATE_FILE = os.path.join(BASE_DIR, "seen.json")

# A browser-like User-Agent helps get past basic bot filtering (e.g. Cloudflare)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
ATOM = "{http://www.w3.org/2005/Atom}"
MAX_SEEN = 8000  # how many old deal IDs to remember for de-duplication


# ---------------------------------------------------------------- utilities

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        log(f"WARNING: could not read {os.path.basename(path)} ({e}); using defaults")
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=0)
    os.replace(tmp, path)


def http_get(url, timeout=25):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, "
                      "application/xml, text/xml, */*",
            "Accept-Language": "en-CA,en;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def strip_html(text):
    """Remove any HTML tags/entities that sneak into feed titles."""
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def split_retailer(title):
    """RFD-style titles look like '[Amazon.ca] Thing for $99'.
    Returns (retailer_or_None, cleaned_title)."""
    m = re.match(r"^\s*\[([^\]]{1,60})\]\s*(.+)$", title, flags=re.S)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None, title


# ---------------------------------------------------------------- feed parsing

def parse_feed(xml_bytes):
    """Parse RSS 2.0 or Atom. Returns a list of dicts newest-first
    (that's the order feeds are published in)."""
    root = ET.fromstring(xml_bytes)
    entries = []

    # --- RSS 2.0: <channel><item> ---
    for item in root.iter("item"):
        title = strip_html(item.findtext("title") or "")
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or "").strip()
        if not link and guid.startswith("http"):
            link = guid
        uid = guid or link or title
        if title and uid:
            entries.append({"title": title, "link": link, "uid": uid})

    # --- Atom: <feed><entry> ---
    for entry in root.iter(f"{ATOM}entry"):
        title = strip_html(entry.findtext(f"{ATOM}title") or "")
        link = ""
        for l in entry.findall(f"{ATOM}link"):
            if l.get("rel") in (None, "alternate"):
                link = (l.get("href") or "").strip()
                break
        uid = (entry.findtext(f"{ATOM}id") or "").strip() or link or title
        if title and uid:
            entries.append({"title": title, "link": link, "uid": uid})

    return entries


# ---------------------------------------------------------------- discord

def post_to_discord(webhook_url, item):
    retailer, clean_title = split_retailer(item["title"])
    embed = {
        "title": (clean_title or item["title"])[:256],
        "url": item["link"] or None,
        "color": item.get("color", 15158332),
        "footer": {"text": item["source"]},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if retailer:
        embed["author"] = {"name": retailer[:250]}

    payload = json.dumps({
        "username": "Canadian Deals \U0001F1E8\U0001F1E6",
        "embeds": [embed],
    }).encode("utf-8")

    for attempt in range(4):
        req = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20):
                return True
        except urllib.error.HTTPError as e:
            if e.code == 429:  # Discord rate limit - wait and retry
                wait = 5.0
                try:
                    wait = float(json.loads(e.read().decode()).get("retry_after", 5)) + 0.5
                except Exception:
                    pass
                log(f"Discord rate limit hit, waiting {wait:.1f}s...")
                time.sleep(wait)
            else:
                log(f"ERROR posting to Discord (HTTP {e.code}): check your webhook URL")
                return False
        except Exception as e:
            log(f"ERROR posting to Discord: {e}")
            time.sleep(3)
    return False


# ---------------------------------------------------------------- main cycle

def passes_filters(title, include_kw, exclude_kw):
    t = title.lower()
    if exclude_kw and any(k in t for k in exclude_kw):
        return False
    if include_kw and not any(k in t for k in include_kw):
        return False
    return True


def run_cycle(cfg):
    webhook = (os.environ.get("DISCORD_WEBHOOK_URL") or cfg.get("webhook_url", "")).strip()
    have_webhook = webhook.startswith("https://")

    state = load_json(STATE_FILE, {"seen": []})
    seen_list = state.get("seen", [])
    seen_set = set(seen_list)
    first_run = len(seen_set) == 0

    include_kw = [k.lower() for k in cfg.get("include_keywords", []) if k.strip()]
    exclude_kw = [k.lower() for k in cfg.get("exclude_keywords", []) if k.strip()]

    new_items = []
    for feed in cfg.get("feeds", []):
        name, url = feed.get("name", "feed"), feed.get("url", "")
        try:
            raw = http_get(url)
            entries = parse_feed(raw)
            fresh = 0
            for e in entries:
                key = hashlib.sha1(f"{name}|{e['uid']}".encode("utf-8")).hexdigest()
                if key in seen_set:
                    continue
                seen_set.add(key)
                seen_list.append(key)
                e["source"] = name
                e["color"] = feed.get("color", 15158332)
                new_items.append(e)
                fresh += 1
            log(f"OK  {name}: {len(entries)} items in feed, {fresh} new")
        except Exception as e:
            log(f"SKIP {name}: could not fetch/parse ({e})")

    # keyword filters
    to_post = [e for e in new_items if passes_filters(e["title"], include_kw, exclude_kw)]

    # On the very first run every item in the feed is "new" - don't flood
    # the channel with 100 old deals. Post just the newest few and remember
    # the rest silently.
    if first_run:
        limit = int(cfg.get("first_run_posts", 5))
        log(f"First run: remembering all current deals, posting only the newest {limit}.")
        to_post = to_post[:limit]

    to_post = to_post[: int(cfg.get("max_posts_per_cycle", 25))]
    to_post.reverse()  # post oldest first so the channel reads chronologically

    if not have_webhook:
        if to_post:
            log("No Discord webhook configured - printing deals instead of posting:")
            for e in to_post:
                log(f"  [{e['source']}] {e['title']}  ->  {e['link']}")
        log("Paste your webhook URL into config.json (webhook_url) to start posting.")
        log("State NOT saved, so these deals will still count as new next time.")
        return

    posted = 0
    for e in to_post:
        if post_to_discord(webhook, e):
            posted += 1
            time.sleep(1.3)  # stay politely under Discord's webhook rate limit

    if posted or new_items or first_run:
        state["seen"] = seen_list[-MAX_SEEN:]
        save_json(STATE_FILE, state)

    log(f"Cycle done: {len(new_items)} new deal(s) found, {posted} posted to Discord.")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "--once"
    cfg = load_json(CONFIG_FILE, {})
    if not cfg.get("feeds"):
        log("ERROR: config.json is missing or has no feeds. Keep it next to this script.")
        sys.exit(1)

    if mode == "--loop":
        interval = max(120, int(cfg.get("poll_seconds", 300)))
        log(f"Running continuously, checking feeds every {interval}s. Ctrl+C to stop.")
        while True:
            try:
                run_cycle(cfg)
            except KeyboardInterrupt:
                log("Stopped.")
                return
            except Exception as e:
                log(f"Cycle error (will retry): {e}")
            time.sleep(interval)
    else:
        run_cycle(cfg)


if __name__ == "__main__":
    main()
