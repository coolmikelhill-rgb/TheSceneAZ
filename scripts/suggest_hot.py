#!/usr/bin/env python3
"""Suggest Can't-Miss (HOT) candidates for the next 30 days.

Run weekly by .github/workflows/refresh-events.yml (Monday mornings, Arizona
time). Standard library only; no paid APIs and no follower counts.

It does NOT decide what is HOT. It writes suggestions to hot-candidates.json;
an editor checks each one and copies the ones that hold up into hot.json,
which is the only list the site features. (The weekly "Scene Picks" Claude
task does this review every Monday; anyone can also edit hot.json by hand.)

How a show becomes a suggestion:
  - Eligible: Arizona show in today .. today+29 (Arizona time), a real venue
    (not TBA), a public event or ticket link, not canceled, postponed or
    confirmed sold out.
  - Signals are read from the show's own official event page (description,
    event data, artist bios) and from the listings themselves. Each kind of
    signal counts once, however many sources repeat it.
  - Suggested when it has one STRONG signal or two different SUPPORTING ones.
    STRONG:
      * headliner scale: an arena/amphitheater headliner (the kind of act that
        tops a festival bill); flagged from the listing itself, so it works
        even when the ticket page can't be read; the editor confirms it's the
        headliner's own tour, not a tribute or a package show;
      * a multi-night run in a big room (two or more arena nights);
      * a date added or a venue upgraded because of demand;
      * a rare or returning Arizona stop: first time in Arizona / in years,
        a reunion, a comeback after a hiatus, a farewell or final tour, or an
        artist who rarely tours;
      * a hometown release show or homecoming; a one-off collaboration.
    SUPPORTING: a multi-night club run, an earlier sold-out run, new release
    or award news, an anniversary show, a co-headline bill, low tickets
    reported by the seller.
  - Google Trends is not used: there is no supported, keyless data source.
    Missing trend data never affects eligibility.

Usage:  python3 scripts/suggest_hot.py [--force]
  Without --force it only runs on Mondays (or when the last list is over a
  week old), so the twice-daily workflow can call it every time.
"""

import datetime as dt
import json
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import enrich_events as en  # noqa: E402  (polite fetcher, page readers, page choice)

ROOT = Path(__file__).resolve().parent.parent
EVENTS = ROOT / "events.json"
OUT = ROOT / "hot-candidates.json"
HOT = ROOT / "hot.json"
WINDOW_DAYS = 30
MAX_FETCHES = 160
PER_HOST = 35

STRONG = [
    ("added_by_demand", "Added date or show because of demand",
     r"(?:second|2nd|third|3rd|another|new|extra|additional)\s+(?:show|date|night|performance)s?\s+(?:has been\s+|have been\s+|just\s+)?"
     r"(?:added|announced)|added due to (?:popular |overwhelming |high )?demand|by (?:popular|overwhelming) demand|"
     r"due to (?:overwhelming|high|popular|incredible) demand"),
    ("venue_upgrade", "Moved to a bigger room",
     r"moved to (?:a |the )?(?:larger|bigger)|venue (?:upgrade|change)|upgraded to (?:a |the )?|new venue"),
    ("rare_stop", "Rare or returning Arizona stop",
     r"only (?:arizona|az|phoenix|tucson|southwest) (?:show|date|stop|appearance)|first (?:arizona|phoenix|tucson|az) "
     r"(?:show|appearance|date|headline)s?|first time (?:in|playing) (?:arizona|phoenix|tucson)|reunion tour|reunited for|"
     r"rare (?:arizona|phoenix|tucson|az) (?:show|appearance|date)|"
     r"first (?:\w+ )?(?:show|tour|time|concert|appearance)[^.|]{0,40}since (?:19|20)\d\d|"
     r"first [^.|]{0,30}\bin (?:over |more than |nearly )?(?:\d+|two|three|four|five|six|seven|eight|nine|ten|a decade|a dozen) years|"
     r"\bhiatus\b|\bcomeback\b|returns? to (?:arizona|phoenix|tucson|the road|touring|the stage)|"
     r"(?:farewell|final|last) (?:tour|show|run)|first headlin(?:e|ing) tour|rarely tours"),
    ("release_show", "Release or homecoming show",
     r"(?:album|record|ep|vinyl|single) release (?:show|party|celebration)|hometown (?:show|release|celebration)|homecoming"),
    ("one_off", "One-off collaboration or event",
     r"one[- ]night only|one[- ]off|special collaboration|together for the first time|one time only"),
]
SUPPORT = [
    ("sold_out_history", "Earlier Arizona run sold out",
     r"(?:last|previous) (?:show|visit|time|tour)[^.|]{0,50}sold out|sold[- ]out (?:tour|run|shows|crowd)|after selling out"),
    ("release_news", "New release or award news",
     r"new album|latest album|new record|debut album|new ep\b|(?:album|record|ep) release tour|breakout|"
     r"grammy[- ](?:nominated|winning|winner)|chart-topping"),
    ("anniversary", "Anniversary show or tour",
     r"\b\d{1,2}(?:st|nd|rd|th) anniversary|anniversary (?:tour|show|celebration)"),
    ("co_headline", "Co-headline or notable shared bill",
     r"co-?headlin"),
    ("low_tickets", "Seller reports low tickets",
     r"low tickets|limited tickets|few tickets (?:left|remain)|almost sold out|nearly sold out|selling fast"),
]
# Listing-level kinds that count as strong (no page needed).
STRONG_KINDS = {k for k, _, _ in STRONG} | {"headliner_scale", "multi_night_big"}
TRIBUTE = re.compile(r"tribute|salute to|\bexperience\b|plays the music of|celebrating the music|\blegacy of\b", re.I)
DRAFT = {
    "headliner_scale": "{artist} is headlining {venue} — arena-scale shows like this are the ones to lock in early.",
    "multi_night_big": "{artist} needed more than one night at {venue}; pick your date before both go.",
    "added_by_demand": "{venue} added another date for {artist} because the first one moved fast — this is the one to grab.",
    "venue_upgrade": "{artist} got moved to a bigger room, which tells you how fast this one moved.",
    "rare_stop": "{artist} doesn't come through Arizona often — {quote}.",
    "release_show": "{artist} is celebrating a new release on stage — {quote}.",
    "one_off": "A one-off: {quote}.",
    "multi_night": "{artist} is playing more than one night at {venue}, so pick your date before both go.",
}


def log(msg):
    print(msg, flush=True)


def headliner(title):
    return re.split(r"\s+(?:w/|with|feat\.?|\+|\||-|–|—|:)\s+|\s*\(", title, maxsplit=1)[0].strip()


def should_run(force, today):
    if force or not OUT.exists():
        return True
    try:
        last = dt.date.fromisoformat(json.loads(OUT.read_text(encoding="utf-8")).get("generated", "2000-01-01"))
    except (ValueError, json.JSONDecodeError):
        return True
    return (today.weekday() == 0 and last != today) or (today - last).days >= 7


def page_texts(e, page, final_url):
    """The text on an event page that belongs to this show (not related events)."""
    lds = list(en.sources.jsonld_objects(page))
    ld = next((o for o in lds if en.ld_matches_date(o, e["date"])), None)
    bits = [en.meta(page, "og:title"), en.meta(page, "og:description") or en.meta(page, "description")]
    if ld:
        bits += [en.clean(ld.get("name")), en.clean(ld.get("description"))[:1500]]
    bits += [f"{n}: {b}" for n, _, b in en.ticketweb_artists(page) if b]
    return " | ".join(b for b in bits if b)


def quote_around(text, m):
    a = max(0, text.rfind("|", 0, m.start()) + 1, m.start() - 90)
    b = text.find("|", m.end())
    b = min(b if b > 0 else len(text), m.end() + 90)
    return re.sub(r"\s+", " ", text[a:b]).strip(" .|")


def main(argv):
    force = "--force" in argv
    today = en.now_az().date()
    if not should_run(force, today):
        log("suggest_hot: not Monday and the list is under a week old; nothing to do")
        return 0
    end = today + dt.timedelta(days=WINDOW_DAYS - 1)
    data = json.loads(EVENTS.read_text(encoding="utf-8"))
    events = data["events"]
    approved = set()
    if HOT.exists():
        for h in json.loads(HOT.read_text(encoding="utf-8")).get("approved", []):
            approved.add((h.get("link"), h.get("date")))

    def eligible(e):
        d, c = e.get("details") or {}, e.get("confirmed") or {}
        status = (c.get("status") or d.get("status") or "").lower()
        return (today.isoformat() <= e["date"] <= end.isoformat() and e.get("type") == "verified"
                and e.get("venue") and "tba" not in e["venue"].lower()
                and str(e.get("link", "")).startswith("http")
                and not any(s in status for s in ("cancel", "postpon", "sold out")))

    pool = [e for e in events if eligible(e)]
    log(f"suggest_hot: {len(pool)} eligible shows, {today} .. {end}")

    # Listing-level signals: multi-night runs (one signal however many nights).
    # A run is 2-4 nights within a few days of each other; weekly residencies don't count.
    runs = {}
    for e in pool:
        toks = frozenset(en.title_tokens(headliner(e["title"])))
        if toks:
            runs.setdefault((toks, e["venue"]), []).append(e)
    for group in runs.values():
        dates = sorted({x["date"] for x in group})
        span = (dt.date.fromisoformat(dates[-1]) - dt.date.fromisoformat(dates[0])).days
        if 2 <= len(dates) <= 4 and span <= 3:
            for x in group:
                x["_run"] = dates

    # Page-level signals, fetching hosts in turn so each site's delay overlaps the others.
    fetcher = en.Fetcher(budget=MAX_FETCHES)
    per_host = {}
    by_host = {}
    no_page = []   # still scanned for listing-level signals (arena headliner, multi-night run)
    for e in pool:
        cands = [u for u, official in en.candidates(e) if official]
        if cands:
            by_host.setdefault(en.host_of(cands[0]), []).append((e, cands[0]))
        else:
            no_page.append((e, None))
    order = []
    while any(by_host.values()):
        for h in list(by_host):
            if by_host[h]:
                order.append(by_host[h].pop(0))
    found = []
    for e, url in order + no_page:
        sig = {}
        title_text = e["title"]
        for kind, label, pat in STRONG + SUPPORT:
            m = re.search(pat, title_text, re.I)
            if m:
                sig[kind] = {"label": label, "quote": title_text, "source": e["link"]}
        h = en.host_of(url) if url else ""
        if url and per_host.get(h, 0) < PER_HOST:
            got = fetcher.get(url)
            per_host[h] = per_host.get(h, 0) + 1
            if got:
                final, page = got
                matched, _ = en.read_page(e, url, page, final, True)
                if matched:
                    text = page_texts(e, page, final)
                    for kind, label, pat in STRONG + SUPPORT:
                        m = re.search(pat, text, re.I)
                        if m and kind not in sig:
                            sig[kind] = {"label": label, "quote": quote_around(text, m), "source": final}
                    if matched.get("status") == "Low tickets" and "low_tickets" not in sig:
                        sig["low_tickets"] = {"label": "Seller reports low tickets", "quote": "Ticket page: low tickets",
                                              "source": final}
        if e.get("_run"):
            big = e.get("scale") == "arena"
            sig.setdefault("multi_night_big" if big else "multi_night",
                           {"label": "Multi-night run in a big room" if big else "Multi-night run", "source": e["link"],
                            "quote": "Dates at this venue: " + ", ".join(e["_run"])})
        if e.get("scale") == "arena" and not TRIBUTE.search(e["title"]):
            sig.setdefault("headliner_scale", {"label": "Arena/amphitheater headliner", "source": e["link"],
                                               "quote": f"Headlining {e['venue']} ({e['city']}) — editor to confirm it's the artist's own tour"})
        strong = [k for k in sig if k in STRONG_KINDS]
        support = [k for k in sig if k not in strong]
        if strong or len(support) >= 2:
            k0 = (strong or support)[0]
            draft = DRAFT.get(k0, "{artist} at {venue}: {quote}.").format(
                artist=headliner(e["title"]), venue=e["venue"], quote=sig[k0]["quote"][:110])
            found.append({
                "link": e["link"], "date": e["date"], "title": e["title"], "venue": e["venue"], "city": e["city"],
                "strength": "strong" if strong else "two supporting",
                "signals": [{"kind": k, "strength": "strong" if k in strong else "supporting", **v} for k, v in sig.items()],
                "evidence": sorted({v["source"] for v in sig.values()}),
                "suggestedWhy": draft, "checked": today.isoformat(),
                "approved": (e["link"], e["date"]) in approved,
            })
    for e in pool:
        e.pop("_run", None)
    found.sort(key=lambda c: (c["strength"] != "strong", c["date"]))
    out = {
        "_how_it_works": ("Suggestions only. Each candidate has one strong signal or two different supporting signals "
                          "from the show's official page or listings. An editor checks the evidence links, rewrites "
                          "suggestedWhy as a specific one-line tip, and copies approved shows into hot.json "
                          "(link, date, why, evidence, checked). Nothing here appears on the site by itself."),
        "generated": today.isoformat(), "window": [today.isoformat(), end.isoformat()],
        "fetches": MAX_FETCHES - fetcher.budget, "candidates": found,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    log(f"suggest_hot: {len(found)} candidates ({sum(c['strength'] == 'strong' for c in found)} strong), "
        f"{out['fetches']} fetches; wrote {OUT.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
