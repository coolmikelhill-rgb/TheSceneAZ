#!/usr/bin/env python3
"""Refresh TheSceneAZ listings into events.json.

Run daily by .github/workflows/refresh-events.yml. Standard library only.

What it does:
  1. Starts from events.json, or from the FALLBACK_EVENTS list in index.html
     the first time.
  2. Drops shows that have already happened (Arizona time).
  3. Adds new shows from 19hz.info's Arizona listing, skipping any that are
     already listed. Your hand-written entries always win over scraped ones.
  4. Writes events.json as {"updated": "YYYY-MM-DD", "events": [...]}.

If 19hz can't be reached, it still drops past shows and writes the file.
It never writes a file with fewer than MIN_EVENTS shows.

To add or fix a show by hand, edit events.json. The script keeps your edits.
"""

import datetime as dt
import html
import json
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sources  # noqa: E402  (scripts/sources.py: the other feeds)

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "index.html"
OUT = ROOT / "events.json"
HZ_URL = "https://19hz.info/eventlisting_Phoenix.php"
MIN_EVENTS = 30
AZ = dt.timezone(dt.timedelta(hours=-7))  # Arizona has no daylight saving time

GENRES = {"house", "techno", "bass", "dnb", "rock", "hiphop", "country",
          "pop", "jazz", "metal", "blues"}
KEYS = ["date", "title", "venue", "city", "genre", "sub", "scale",
        "price", "type", "cta", "source", "link"]

# 19hz tag words -> the site's genre values. First match wins.
GENRE_RULES = [
    ("dnb", r"drum ?(&|and|n)? ?bass|\bdnb\b|\bd&b\b|jungle"),
    ("techno", r"techno|industrial|minimal|acid|electro\b|hard ?groove"),
    ("bass", r"dubstep|riddim|\bbass\b|trap|future bass|wave|hardstyle|hardcore|bassline"),
    ("metal", r"metal|metalcore|deathcore"),
    ("hiphop", r"hip.?hop|\brap\b|r&b"),
    ("jazz", r"jazz"),
    ("rock", r"rock|punk|indie|emo|shoegaze"),
    ("pop", r"\bpop\b|synthpop|k-pop"),
    ("house", r"house|disco|garage|trance|edm|dance|afro|melodic|progressive|tech"),
]


NOT_MUSIC = re.compile(r"workshop|comedy|comedian|stand.?up|crawl|trivia|bingo|yoga|market|meditation", re.I)


def log(msg):
    print(msg, file=sys.stderr)


def today_az():
    return dt.datetime.now(AZ).date()


def norm(s):
    s = html.unescape(str(s or "")).lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def tokens(s):
    return {w for w in norm(s).split() if len(w) > 2 and w not in {"the", "and", "with", "presents", "more"}}


def load_base():
    """events.json if present, else the snapshot embedded in index.html."""
    if OUT.exists():
        data = json.loads(OUT.read_text(encoding="utf-8"))
        events = data["events"] if isinstance(data, dict) else data
        log(f"base: {len(events)} shows from events.json")
        return events
    src = INDEX.read_text(encoding="utf-8")
    start = src.index("const FALLBACK_EVENTS = [") + len("const FALLBACK_EVENTS = ")
    end = src.index("\n];", start) + 3
    block = src[start:end].rstrip(";")
    # The snapshot is JS with unquoted keys; quote the known keys to make it JSON.
    block = re.sub(r'([{,]\s*)(' + "|".join(KEYS) + r'):', r'\1"\2":', block)
    block = re.sub(r",(\s*[}\]])", r"\1", block)  # trailing commas
    events = json.loads(block)
    log(f"base: {len(events)} shows from index.html")
    return events


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "TheSceneAZ listings refresh (GitHub Actions)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def strip_tags(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def guess_genre(tags):
    """Genre from the first tag 19hz lists, falling back to all the tags."""
    first = tags.split(",")[0]
    for text in (first, tags):
        for genre, pattern in GENRE_RULES:
            if re.search(pattern, text.lower()):
                return genre
    return None


def parse_19hz(page, venue_scale):
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S):
        cells = re.findall(r"<td[^>]*>(.*?)(?=<td|$)", row, re.S)
        if len(cells) < 7:
            continue
        when, main, tags, price, org, links, sortdate = cells[:7]
        m = re.search(r"(\d{4})/(\d{2})/(\d{2})", sortdate)
        a = re.search(r"<a [^>]*href=['\"]([^'\"]+)['\"][^>]*>(.*?)</a>", main, re.S)
        if not m or not a:
            continue
        date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        link = html.unescape(a.group(1)).strip()
        title = strip_tags(a.group(2))
        rest = strip_tags(main[a.end():])
        vm = re.match(r"@\s*(.*?)\s*\(([^()]*)\)\s*$", rest)
        venue, city = (vm.group(1), vm.group(2)) if vm else (rest.lstrip("@ ").strip(), "Phoenix")
        city = re.split(r"[,/]", city)[0].strip() or "Phoenix"
        if not title or not link.startswith(("http://", "https://")):
            continue
        tags_txt = strip_tags(tags)
        genre = guess_genre(tags_txt)
        # Untagged 19hz listings are often comedy, workshops or bar crawls.
        # Only add shows with a music tag the site can file under a genre.
        if not genre or NOT_MUSIC.search(title):
            continue

        tba = not venue or re.search(r"\btba\b|location|secret|undisclosed", venue, re.I)
        time = re.search(r"\(([^)]*)\)", strip_tags(when))
        bits = [time.group(1) if time else "", strip_tags(price).replace(" | ", " · "), strip_tags(org)]
        price_txt = " · ".join(b for b in bits if b) or "See listing"

        out.append({
            "date": date,
            "title": title,
            "venue": "Location TBA" if tba else venue,
            "city": city,
            "genre": genre,
            "sub": " / ".join(t.strip().title() for t in tags_txt.split(",")[:2] if t.strip()),
            "scale": "warehouse" if tba else venue_scale.get(norm(venue), "club"),
            "price": price_txt[:80],
            "type": "community" if tba else "verified",
            "cta": "Event Page" if tba else "Get Tickets",
            "source": "19hz",
            "link": link,
        })
    return out


def venue_key(v):
    return re.sub(r"^the ", "", norm(v))


def is_duplicate(new, existing_by_date, links):
    """Same link, or same day with mostly the same title words.

    A room that hosts two shows in one night (early and late) keeps both,
    unless the titles share a word.
    """
    if new["link"] in links:
        return True
    nv, nt = venue_key(new["venue"]), tokens(new["title"])
    for old in existing_by_date.get(new["date"], []):
        ot = tokens(old["title"])
        overlap = len(nt & ot) / min(len(nt), len(ot)) if nt and ot else 0
        if overlap >= 0.5:
            return True
        if nv and nv == venue_key(old["venue"]) and nv != "location tba" and (overlap > 0 or not nt or not ot):
            return True
    return False


def valid(e):
    return (isinstance(e, dict)
            and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(e.get("date", "")))
            and e.get("title") and e.get("venue") and e.get("city")
            and str(e.get("link", "")).startswith(("http://", "https://")))


def main():
    today = today_az().isoformat()
    base = [e for e in load_base() if valid(e)]
    kept = [e for e in base if e["date"] >= today]
    log(f"dropped {len(base) - len(kept)} past shows, kept {len(kept)}")

    venue_scale = {norm(e["venue"]): e["scale"] for e in base if e.get("scale")}
    by_date, links = {}, {e["link"] for e in kept}
    for e in kept:
        by_date.setdefault(e["date"], []).append(e)

    added = 0
    try:
        scraped = parse_19hz(fetch(HZ_URL), venue_scale)
        log(f"19hz: {len(scraped)} listings")
        for e in scraped:
            if e["date"] < today or not valid(e) or is_duplicate(e, by_date, links):
                continue
            kept.append(e)
            by_date.setdefault(e["date"], []).append(e)
            links.add(e["link"])
            added += 1
    except Exception as exc:  # keep going: a source outage must not stop the prune
        log(f"19hz skipped: {exc}")
    log(f"added {added} new shows from 19hz")

    for name, fetch_source in sources.SOURCES:
        n = 0
        try:
            got = fetch_source(venue_scale)
            for e in got:
                if (e["date"] < today or not valid(e) or e.get("genre") not in GENRES
                        or is_duplicate(e, by_date, links)):
                    continue
                kept.append(e)
                by_date.setdefault(e["date"], []).append(e)
                links.add(e["link"])
                n += 1
            log(f"{name}: {len(got)} found, {n} new")
        except Exception as exc:  # one broken source never stops the rest
            log(f"{name} skipped: {exc}")

    if len(kept) < MIN_EVENTS:
        log(f"only {len(kept)} shows; refusing to write events.json")
        return 1

    kept.sort(key=lambda e: (e["date"], norm(e["title"])))
    data = {"updated": today, "events": [{k: e[k] for k in KEYS if k in e} for e in kept]}
    text = json.dumps(data, ensure_ascii=False, indent=1) + "\n"
    old = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
    if old and json.loads(old).get("events") == data["events"]:
        log("no changes")
        return 0
    OUT.write_text(text, encoding="utf-8")
    log(f"wrote {len(kept)} shows to events.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
