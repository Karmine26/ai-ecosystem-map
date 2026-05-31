#!/usr/bin/env python3
"""
Trump Stock Tweet Scanner
Scans Trump's public social media for stock/company mentions,
then updates TRUMP_STOCKS in ai_ecosystem_map.html.

Runs as a GitHub Action on a cron schedule.
"""

import json
import re
import sys
import urllib.request
from datetime import datetime, timezone

# --- Company-to-ticker lookup (curated to reduce false positives) ---
COMPANY_MAP = {
    # Exact $TICKER matches are handled separately via regex
    # This maps company names to (exchange:ticker, display_name)
    "nvidia": ("NASDAQ:NVDA", "NVIDIA"),
    "broadcom": ("NASDAQ:AVGO", "Broadcom"),
    "tesla": ("NASDAQ:TSLA", "Tesla"),
    "apple": ("NASDAQ:AAPL", "Apple"),
    "google": ("NASDAQ:GOOGL", "Google"),
    "alphabet": ("NASDAQ:GOOGL", "Alphabet"),
    "microsoft": ("NASDAQ:MSFT", "Microsoft"),
    "amazon": ("NASDAQ:AMZN", "Amazon"),
    "meta": ("NASDAQ:META", "Meta"),
    "palantir": ("NASDAQ:PLTR", "Palantir"),
    "servicenow": ("NYSE:NOW", "ServiceNow"),
    "dell": ("NYSE:DELL", "Dell"),
    "intel": ("NASDAQ:INTC", "Intel"),
    "ibm": ("NYSE:IBM", "IBM"),
    "oracle": ("NYSE:ORCL", "Oracle"),
    "qualcomm": ("NASDAQ:QCOM", "Qualcomm"),
    "micron": ("NASDAQ:MU", "Micron"),
    "amd": ("NASDAQ:AMD", "AMD"),
    "salesforce": ("NYSE:CRM", "Salesforce"),
    "snowflake": ("NYSE:SNOW", "Snowflake"),
    "taiwan semiconductor": ("NYSE:TSM", "TSMC"),
    "tsmc": ("NYSE:TSM", "TSMC"),
    "boeing": ("NYSE:BA", "Boeing"),
    "lockheed": ("NYSE:LMT", "Lockheed Martin"),
    "raytheon": ("NYSE:RTX", "RTX"),
    "general electric": ("NYSE:GE", "GE"),
    "exxon": ("NYSE:XOM", "ExxonMobil"),
    "chevron": ("NYSE:CVX", "Chevron"),
    "jpmorgan": ("NYSE:JPM", "JPMorgan"),
    "goldman": ("NYSE:GS", "Goldman Sachs"),
    "softbank": ("TSE:9984", "SoftBank"),
    "coreweave": ("NASDAQ:CRWV", "CoreWeave"),
    "foxconn": ("TWSE:2317", "Foxconn"),
    "tiktok": (None, "TikTok"),  # private, but worth tracking mentions
    "truth social": (None, "Truth Social"),
}

# Common cashtag-to-exchange mapping for $TICKER detection
EXCHANGE_MAP = {
    "NVDA": "NASDAQ", "AVGO": "NASDAQ", "TSLA": "NASDAQ", "AAPL": "NASDAQ",
    "GOOGL": "NASDAQ", "MSFT": "NASDAQ", "AMZN": "NASDAQ", "META": "NASDAQ",
    "PLTR": "NASDAQ", "NOW": "NYSE", "DELL": "NYSE", "INTC": "NASDAQ",
    "IBM": "NYSE", "ORCL": "NYSE", "AMD": "NASDAQ", "CRM": "NYSE",
    "MU": "NASDAQ", "TSM": "NYSE", "BA": "NYSE", "LMT": "NYSE",
    "QCOM": "NASDAQ", "GS": "NYSE", "JPM": "NYSE", "XOM": "NYSE",
    "SNOW": "NYSE", "CRWV": "NASDAQ",
}

# Words that look like tickers but aren't
FALSE_POSITIVES = {"AI", "US", "IT", "IS", "AT", "ON", "OR", "IN", "AN", "AM",
                   "TO", "DO", "GO", "NO", "SO", "BE", "BY", "UP", "IF", "A",
                   "THE", "AND", "FOR", "NOT", "BUT", "ALL", "CAN", "HAS",
                   "HER", "HIM", "HIS", "HOW", "ITS", "MAY", "NEW", "NOW",
                   "OLD", "SEE", "WAY", "WHO", "DID", "GET", "HAS", "LET",
                   "SAY", "SHE", "TOO", "USE", "DAD", "MOM", "WIN", "BIG",
                   "USA", "GDP", "CEO", "FBI", "CIA", "DOJ", "SEC", "FED",
                   "GOP", "DNC", "NYC", "DC", "LA"}


def fetch_rss_feed():
    """
    Try multiple public RSS proxies for Trump's X/Truth Social feed.
    Returns list of dicts with 'text' and 'date' keys.
    """
    posts = []

    # Try multiple Nitter/RSS proxy instances
    rss_urls = [
        "https://nitter.privacydev.net/realDonaldTrump/rss",
        "https://nitter.poast.org/realDonaldTrump/rss",
        "https://nitter.net/realDonaldTrump/rss",
    ]

    for url in rss_urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read().decode("utf-8", errors="replace")

            # Simple XML parsing (no external deps)
            items = re.findall(r"<item>(.*?)</item>", data, re.DOTALL)
            for item in items[:20]:  # last 20 posts
                title = re.search(r"<title>(.*?)</title>", item, re.DOTALL)
                desc = re.search(r"<description>(.*?)</description>", item, re.DOTALL)
                pub_date = re.search(r"<pubDate>(.*?)</pubDate>", item)

                text = ""
                if desc:
                    text = re.sub(r"<[^>]+>", "", desc.group(1))  # strip HTML
                elif title:
                    text = re.sub(r"<[^>]+>", "", title.group(1))

                if text.strip():
                    posts.append({
                        "text": text.strip(),
                        "date": pub_date.group(1) if pub_date else "",
                    })

            if posts:
                print(f"Fetched {len(posts)} posts from {url}")
                return posts

        except Exception as e:
            print(f"Failed {url}: {e}")
            continue

    print("All RSS sources failed — no posts fetched")
    return posts


def extract_mentions(posts):
    """
    Extract stock/company mentions from post texts.
    Returns list of {ticker, name, reason, src} dicts.
    """
    mentions = []
    seen_tickers = set()

    for post in posts:
        text = post["text"]
        found_in_post = []

        # 1. Cashtag detection: $NVDA, $TSLA, etc.
        cashtags = re.findall(r"\$([A-Z]{1,5})\b", text)
        for tag in cashtags:
            if tag in FALSE_POSITIVES:
                continue
            exchange = EXCHANGE_MAP.get(tag, "NYSE")  # default to NYSE
            ticker = f"{exchange}:{tag}"
            if ticker not in seen_tickers:
                found_in_post.append({
                    "t": ticker,
                    "n": tag,  # will be refined if in COMPANY_MAP
                    "reason": _truncate(text, 120),
                    "src": "tweet",
                })
                seen_tickers.add(ticker)

        # 2. Company name detection
        text_lower = text.lower()
        for company, (ticker, name) in COMPANY_MAP.items():
            if company in text_lower and ticker and ticker not in seen_tickers:
                # Verify it's a real mention (not substring of another word)
                pattern = r"\b" + re.escape(company) + r"\b"
                if re.search(pattern, text_lower):
                    found_in_post.append({
                        "t": ticker,
                        "n": name,
                        "reason": _truncate(text, 120),
                        "src": "tweet",
                    })
                    seen_tickers.add(ticker)

        mentions.extend(found_in_post)

    return mentions


def _truncate(text, max_len):
    """Truncate text to max_len, adding ellipsis if needed."""
    text = text.replace('"', "'").replace("\n", " ").strip()
    if len(text) > max_len:
        return text[:max_len - 1] + "..."
    return text


def read_existing_tickers(html_path):
    """Read existing TRUMP_STOCKS tickers from the HTML file."""
    with open(html_path, "r", encoding="utf-8") as f:
        text = f.read()

    # Extract existing tickers
    match = re.search(r"const TRUMP_STOCKS=\[(.*?)\];", text, re.DOTALL)
    if not match:
        print("TRUMP_STOCKS not found in HTML")
        return set(), text

    existing = set(re.findall(r't:"([^"]+)"', match.group(1)))
    return existing, text


def update_html(html_path, new_entries):
    """Add new entries to TRUMP_STOCKS in the HTML file."""
    existing_tickers, text = read_existing_tickers(html_path)

    # Filter to truly new entries
    new_only = [e for e in new_entries if e["t"] not in existing_tickers]

    if not new_only:
        print("No new stocks to add")
        return False

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Build new JS entries
    new_lines = []
    for e in new_only:
        reason_escaped = e["reason"].replace("\\", "\\\\").replace('"', '\\"')
        line = f'  {{t:"{e["t"]}", n:"{e["n"]}", reason:"{reason_escaped}", date:"{today}", src:"{e["src"]}"}},'
        new_lines.append(line)

    # Insert before the closing ];
    insert_text = "\n".join(new_lines) + "\n"
    text = text.replace(
        "\n];\n\n/* ---------- Modal + TradingView ---------- */",
        "\n" + insert_text + "];\n\n/* ---------- Modal + TradingView ---------- */"
    )

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(text)

    print(f"Added {len(new_only)} new stocks: {', '.join(e['n'] for e in new_only)}")
    return True


def main():
    html_path = "ai_ecosystem_map.html"

    print(f"=== Trump Stock Scanner — {datetime.now(timezone.utc).isoformat()} ===")

    # Fetch posts
    posts = fetch_rss_feed()
    if not posts:
        print("No posts to scan — exiting")
        sys.exit(0)

    # Extract mentions
    mentions = extract_mentions(posts)
    if not mentions:
        print("No stock mentions found in recent posts")
        sys.exit(0)

    print(f"Found {len(mentions)} stock mentions: {', '.join(m['n'] for m in mentions)}")

    # Update HTML
    changed = update_html(html_path, mentions)

    if changed:
        print("HTML updated — commit needed")
        sys.exit(0)  # Action will commit
    else:
        print("No changes needed")
        sys.exit(0)


if __name__ == "__main__":
    main()
