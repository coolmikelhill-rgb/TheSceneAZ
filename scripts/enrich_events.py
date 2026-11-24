#!/usr/bin/env python3
"""Fill in tonight's and tomorrow's show details from official event pages.

Run by .github/workflows/refresh-events.yml after refresh_events.py, every
morning and late afternoon (Arizona time). Standard library only; no paid APIs.

For each show dated today or tomorrow in Arizona time, it:
  1. Picks the best event-specific page: the listing's own link when it is a
     venue, promoter or ticketing page; otherwise the venue's own event page
     from the calendar fetchers in sources.py; otherwise (last resort) an
     aggregator page such as Songkick, used for a listed start time only.
     Resale sites (SeatGeek, StubHub...) are never fetched.
  2. Checks that the page is about the same artist, venue and local date.
  3. Reads doors, show, listed time, published end and set times, the lowest
     general-admission price (advance / door when both are listed, known
     fees), any condition on free entry, the age minimum, ticket status,
     openers and short artist descriptions from the page.
  4. Writes them to the show's "details" block in events.json with the source
     URL and check date, plus an automatic "Go if" line built only from
     those sourced facts.

Rules that keep this safe:
  - Hand-entered "confirmed" blocks are never touched (the site shows them
    over "details"), and neither are hand-written "goIf" lines.
  - If a page can't be reached, doesn't match, or returns less than the last
    good check, the previous details and their original check date are kept.
  - Missing prices never become "Free" and missing ages never become "All ages".
  - robots.txt and Crawl-delay are honoured, each host is fetched politely,
    pages are fetched once per run, and shows checked recently are skipped.

Usage:  python3 scripts/enrich_events.py [--force] [--days N]
"""

import datetime as dt
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sources  # noqa: E402  (shared fetch helpers and venue calendar fetchers)

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "events.json"
AZ = dt.timezone(dt.timedelta(hours=-7))  # Arizona has no daylight saving time

RESALE = ("seatgeek.com", "stubhub.com", "vividseats.com", "ticketnetwork.com", "gametime.co",
          "tickpick.com", "viagogo.com")
AGGREGATORS = ("songkick.com", "bandsintown.com", "jambase.com", "19hz.info", "allevents.in")
MAX_FETCHES = 90          # per run, across all hosts
MIN_DELAY = 3.0           # seconds between requests to one host
MAX_DELAY = 15.0          # cap on a site's Crawl-delay
RECHECK_TODAY_HOURS = 6   # tonight's shows are re-checked at most this often
TIMEOUT = 20

MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
GENRE_WORDS = {"house": "house", "techno": "techno", "bass": "bass music", "dnb": "drum & bass",
               "rock": "rock", "hiphop": "hip-hop and R&B", "country": "country", "pop": "pop",
               "jazz": "jazz", "metal": "metal", "blues": "blues", "folk": "folk and Americana",
               "world": "Latin and world music"}


def log(msg):
    print(msg, flush=True)


def now_az():
    return dt.datetime.now(AZ)


# ----------------------------------------------------------------- fetching

class Fetcher:
    """Polite fetcher: robots.txt, per-host delay, one fetch per URL per run."""

    def __init__(self, budget=MAX_FETCHES):
        self.budget = budget
        self.robots = {}     # host -> (disallow prefixes, allow prefixes, delay) or None (blocked)
        self.last = {}       # host -> time of last request
        self.pages = {}      # url -> (final url, html) or None
        self.blocked_hosts = set()

    def _raw(self, url):
        req = urllib.request.Request(url, headers={"User-Agent": sources.BROWSER_UA,
                                                   "Accept": "text/html,application/xhtml+xml"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.geturl(), r.read(3_000_000).decode("utf-8", errors="replace")

    def _rules(self, host):
        if host in self.robots:
            return self.robots[host]
        rules = ([], [], MIN_DELAY)
        try:
            _, txt = self._raw(f"https://{host}/robots.txt")
            if "<html" in txt[:500].lower():
                txt = ""  # some sites answer robots.txt with a challenge page; treat as no rules
            groups, current, agents = {}, None, []
            for line in txt.splitlines():
                line = line.split("#", 1)[0].strip()
                if ":" not in line:
                    continue
                k, v = (x.strip() for x in line.split(":", 1))
                k = k.lower()
                if k == "user-agent":
                    if current is not None:
                        agents, current = [], None
                    agents.append(v.lower())
                    continue
                if current is None:
                    current = {"dis": [], "allow": [], "delay": None}
                    for a in agents:
                        groups[a] = current
                if k == "disallow" and v:
                    current["dis"].append(v)
                elif k == "allow" and v:
                    current["allow"].append(v)
                elif k == "crawl-delay":
                    try:
                        current["delay"] = float(v)
                    except ValueError:
                        pass
            g = groups.get("*")
            if g:
                delay = min(MAX_DELAY, max(MIN_DELAY, g["delay"] or MIN_DELAY))
                rules = (g["dis"], g["allow"], delay)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                rules = None  # the site refuses automated access; leave it alone
        except Exception:
            pass  # no robots.txt reachable: default polite rules
        self.robots[host] = rules
        return rules

    def allowed(self, url):
        p = urllib.parse.urlparse(url)
        rules = self._rules(p.netloc)
        if rules is None:
            return False
        dis, allow, _ = rules
        path = p.path or "/"
        best_dis = max((len(d) for d in dis if path.startswith(d)), default=-1)
        best_allow = max((len(a) for a in allow if path.startswith(a)), default=-1)
        return best_dis < 0 or best_allow >= best_dis

    def get(self, url):
        if url in self.pages:
            return self.pages[url]
        host = urllib.parse.urlparse(url).netloc
        result = None
        if host in self.blocked_hosts:
            log(f"    skip {host}: refused earlier this run")
        elif self.budget <= 0:
            log("    skip: fetch budget for this run is used up")
        elif not self.allowed(url):
            log(f"    skip {url}: not allowed by robots.txt")
        else:
            delay = (self.robots.get(host) or ([], [], MIN_DELAY))[2]
            wait = self.last.get(host, 0) + delay - time.time()
            if wait > 0:
                time.sleep(wait)
            self.budget -= 1
            try:
                result = self._raw(url)
                if re.search(r"captcha-delivery|cf-challenge|Please enable JS", result[1][:3000]):
                    log(f"    {host} answered with a bot challenge; skipping the host")
                    self.blocked_hosts.add(host)
                    result = None
            except urllib.error.HTTPError as exc:
                log(f"    fetch {url} failed: HTTP {exc.code}")
                if exc.code in (401, 403, 429):
                    self.blocked_hosts.add(host)
            except Exception as exc:
                log(f"    fetch {url} failed: {exc}")
            self.last[host] = time.time()
        self.pages[url] = result
        return result


# ----------------------------------------------------------------- matching

STOP = {"the", "and", "with", "tour", "live", "presents", "present", "feat", "featuring", "special",
        "guest", "guests", "night", "show", "tickets", "ticket", "buy", "event", "concert", "w",
        "from", "for", "tue", "wed", "thu", "fri", "sat", "sun", "mon", "phoenix", "tucson", "mesa",
        "tempe", "arizona", "az"}
GENERIC_VENUE = {"the", "theater", "theatre", "music", "hall", "room", "club", "center", "centre",
                 "lounge", "bar", "live", "ballroom", "arena", "amphitheatre", "amphitheater",
                 "stage", "and", "grill", "nightlife", "phoenix", "tucson", "mesa", "tempe"}


def words(s):
    s = html.unescape(str(s or "")).lower().replace("’", "'")
    return [w for w in re.findall(r"[a-z0-9]+", s)]


def title_tokens(s):
    return {w for w in words(s) if len(w) >= 3 and w not in STOP}


def venue_tokens(v):
    toks = {w for w in words(v) if len(w) >= 3 and w not in GENERIC_VENUE}
    return toks or {w for w in words(v) if len(w) >= 3}


def date_mentions(text):
    """(month, day) pairs written in a page title or description."""
    t = html.unescape(text or "").lower()
    out = set()
    mon = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
    for m in re.finditer(mon + r"\s+(\d{1,2})(?:st|nd|rd|th)?\b", t):
        out.add((MONTHS.index(m.group(1)) + 1, int(m.group(2))))
    for m in re.finditer(r"\b(\d{1,2})(?:st|nd|rd|th)?[\s-]+" + mon + r"(?![a-z])", t):
        out.add((MONTHS.index(m.group(2)) + 1, int(m.group(1))))
    for m in re.finditer(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", t):
        a, b = int(m.group(1)), int(m.group(2))
        if 1 <= a <= 12 and 1 <= b <= 31:
            out.add((a, b))
    return {x for x in out if 1 <= x[1] <= 31}


def ld_matches_date(obj, date_iso):
    start = str(obj.get("startDate") or "")
    if start[:10] == date_iso:
        return True
    # A listing dated the evening before a show that starts after midnight.
    nxt = (dt.date.fromisoformat(date_iso) + dt.timedelta(days=1)).isoformat()
    m = re.match(r"\d{4}-\d{2}-\d{2}T(\d{2})", start)
    return start[:10] == nxt and m is not None and int(m.group(1)) < 5


# ----------------------------------------------------------------- page reading

def meta(page, name):
    for pat in (rf'<meta[^>]+(?:property|name)="{re.escape(name)}"[^>]+content="([^"]*)"',
                rf'<meta[^>]+content="([^"]*)"[^>]+(?:property|name)="{re.escape(name)}"'):
        m = re.search(pat, page)
        if m:
            return clean(m.group(1))
    return ""


def clean(s):
    s = html.unescape(html.unescape(str(s or "")))
    s = re.sub(r"<br\s*/?>", " | ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s).replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def page_text(page):
    p = re.sub(r"<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", page, flags=re.S | re.I)
    return clean(p)


T_OPT = r"(\d{1,2})(?::(\d{2}))?\s*(?:([ap])\.?\s*m\b\.?)?"
T_REQ = r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\b\.?"


def fmt_time(h, m, ap):
    h = int(h)
    m = int(m or 0)
    if ap:
        ap = ap.lower()
    elif 1 <= h <= 11:
        ap = "p"   # venue pages write "Doors at 7:00 / Show at 7:30" for evening shows
    else:
        return ""
    if not 1 <= h <= 12 or m > 59:
        return ""
    return f"{h}:{m:02d} {'AM' if ap == 'a' else 'PM'}"


def ld_time(value):
    m = re.match(r"\d{4}-\d{2}-\d{2}T(\d{2}):(\d{2})", str(value or ""))
    if not m:
        return ""
    h, mi = int(m.group(1)), int(m.group(2))
    if h == 0 and mi == 0:
        return ""  # midnight usually means "no time given"
    return f"{(h % 12) or 12}:{mi:02d} {'AM' if h < 12 else 'PM'}"


def read_times(text):
    """Doors / show / listed / ends / sets from one block of listing text."""
    t = text
    out = {}
    m = (re.search(r"\bdoors?(?:\s+open)?(?:\s+at)?\s*[:\-]?\s*" + T_OPT, t, re.I)
         or re.search(T_REQ + r"\s*\(?\s*doors?\b", t, re.I))
    if m:
        out["doors"] = fmt_time(*m.groups()[:3])
    m = (re.search(r"\b(?:show|showtime|show time|music|starts?|start time)(?:\s+(?:at|starts?))?\s*[:\-]?\s*" + T_OPT
                   + r"(?!\s*\+)", t, re.I)
         or re.search(T_REQ + r"\s*(?:show|start)\b", t, re.I))
    if m and m.group(1):
        out["show"] = fmt_time(*m.groups()[:3])
    m = re.search(r"((?:" + T_REQ + r")(?:\s*(?:&|and|,)\s*(?:" + T_REQ + r"))+)\s*sets?\b", t, re.I)
    if m:
        times = [fmt_time(*x) for x in re.findall(T_REQ, m.group(1), re.I)]
        out["sets"] = [{"time": x} for x in times if x]
    if re.search(r"set\s*times?", t, re.I):
        named = [(fmt_time(h, mi, ap), name.strip(" -–:|"))
                 for h, mi, ap, name in re.findall("(?i:" + T_REQ + r")\s*[-–:|]\s*([A-Z][\w&'.\- ]{1,40}?)(?=\s*(?:\||,|$|\d{1,2}(?::\d{2})?\s*[apAP][mM]))", t)]
        named = [(x, n) for x, n in named if x and not re.match(r"(doors|show|music)\b", n, re.I)]
        if len(named) >= 2:
            out["sets"] = [{"time": x, "name": n} for x, n in named]
    m = re.search(r"\b(?:ends?|until|till|til|close)\s*[:\-]?\s*" + T_REQ, t, re.I)
    rng = re.search(T_REQ + r"\s*[-–]\s*" + T_REQ, t, re.I)
    if m:
        out["ends"] = fmt_time(*m.groups()[:3])
    elif rng:
        out["ends"] = fmt_time(*rng.groups()[3:6])
    if not out.get("show") and not out.get("doors") and not out.get("sets"):
        first = rng or next((x for x in re.finditer(T_REQ, t, re.I)
                             if not re.search(r"(before|until|till|til|after|by)\s*$", t[max(0, x.start() - 8):x.start()], re.I)), None)
        if first:
            out["listed"] = fmt_time(*first.groups()[:3])
    if out.get("doors") and out.get("show") == out["doors"] and "sets" not in out:
        pass  # doors and music at the same time is a real (if rare) listing
    return {k: v for k, v in out.items() if v}


EXCLUDE_TIER = re.compile(r"vip|table|bottle|booth|suite|parking|resale|platinum|meet|package|premium|"
                          r"upgrade|cabana|balcony|bleacher|reserved seat|member|family|donor", re.I)


def money(x):
    v = float(x)
    return f"${v:.2f}".replace(".00", "") if v % 1 else f"${int(v)}"


def read_price(text):
    """Lowest general-admission price, advance / door prices and fees from listing text."""
    tiers = []
    for m in re.finditer(r"([^$|()]{0,45})\$\s?(\d+(?:\.\d{1,2})?)(?:\s*[-–]\s*\$?\s?(\d+(?:\.\d{1,2})?))?((?:\s*\+\s*(?:\w+\s+)?fees?)?(?:\s*\([^)$]{0,24}\))?)", text):
        label, lo, hi, after = m.group(1).lower(), float(m.group(2)), m.group(3), m.group(4).lower()
        if lo <= 0 or EXCLUDE_TIER.search(label) or EXCLUDE_TIER.search(after[:12]):
            continue
        kind = ("door" if re.search(r"day of|door|\bdos\b", label) else
                "adv" if re.search(r"adv|presale|early", label) else
                "ga" if re.search(r"general|\bga\b|admission|ticket|cover", label) else "other")
        tiers.append({"kind": kind, "lo": lo, "hi": float(hi) if hi else None})
    if not tiers:
        return {}
    labelled = [t for t in tiers if t["kind"] != "other"]
    use = labelled or tiers
    out = {"min": min(t["lo"] for t in use)}
    adv = next((t for t in use if t["kind"] in ("adv", "ga")), None)
    door = next((t for t in use if t["kind"] == "door"), None)
    if adv and door and door["lo"] != adv["lo"]:
        out["text"] = f"{money(adv['lo'])} adv · {money(door['lo'])} door"
    else:
        t0 = min(use, key=lambda t: t["lo"])
        out["text"] = money(t0["lo"]) + (f"–{money(t0['hi']).lstrip('$')}" if t0["hi"] and t0["hi"] > t0["lo"] else "")
    if re.search(r"\+\s*(?:\w+\s+)?fees?\b", text, re.I):
        out["fees"] = "+ fees"
    elif re.search(r"fees? included|all[- ]in|incl\.? fees", text, re.I):
        out["fees"] = "fees included"
    return out


def read_free(text, has_price):
    m = re.search(r"\b(?:free(?:\s+(?:entry|admission|show|event|cover))?|no cover)\b"
                  r"(?!\s*(?:parking|drinks?|shots?|gifts?|merch|downloads?|streams?|t-?shirts?))([^|.]{0,40})",
                  text, re.I)
    if not m:
        return None
    tail = m.group(1)
    cond = re.search(r"(w/|with)\s*(?:an?\s+)?rsvp|rsvp\s*(?:required|needed)?|before\s+" + T_OPT +
                     r"|until\s+" + T_OPT + r"|with\s+(?:a\s+)?(?:ticket|wristband|student id|code)", tail, re.I)
    if has_price and not (cond and re.match(r"(before|until)", cond.group(0), re.I)):
        return None   # a priced show that mentions "free" somewhere else
    if not cond and re.search(r"\brsvp\b", text, re.I):
        return {"condition": "with RSVP"}
    if cond:
        c = cond.group(0).strip()
        c = re.sub(r"^w/", "with", c, flags=re.I)
        tm = re.search(T_OPT, c)
        if tm and re.match(r"(before|until)", c, re.I) and tm.group(1):
            c = f"{c.split()[0].lower()} {fmt_time(*tm.groups())}"
        if re.match(r"rsvp", c, re.I):
            c = "with RSVP"
        c = re.sub(r"\b(am|pm)\b", lambda m: m.group(1).upper(), c.lower()).replace("rsvp", "RSVP")
        return {"condition": c}
    return {"condition": ""}


def read_ages(text):
    # "16+", "18 and over" -- but not "$15 + fees"
    nums = [int(x) for x in re.findall(r"(?<![$\d.,])\b(1[3-9]|2[01])(?:\+|\s+and\s+(?:up|over)|\s*&\s*(?:up|over)|\s+years? and older)(?!\s*(?:\w+\s+)?fees?\b)",
                                       text, re.I)]
    all_ages = re.search(r"\ball[\s-]ages\b", text, re.I)
    if nums and all_ages:
        return None  # contradictory; say nothing rather than guess
    if nums:
        n = min(nums)
        return {"ages": f"{n}+", "minAge": n}
    if all_ages:
        return {"ages": "All ages", "minAge": 0}
    return None


def read_status(text):
    if re.search(r"\bcancel+ed\b", text, re.I):
        return "Canceled"
    if re.search(r"\bpostponed\b", text, re.I):
        return "Postponed"
    if re.search(r"\bsold[\s-]?out\b", text, re.I):
        return "Sold out"
    return ""


COMMON_LEAD = {"local", "indie", "riot", "psych", "psych-pop", "jazz", "rock", "pop", "folk", "punk", "garage",
               "experimental", "electronic", "ambient", "soul", "funk", "hip-hop", "rap", "country", "bluegrass",
               "singer-songwriter", "dream", "dream-pop", "post-punk", "noise", "swinging", "acclaimed", "legendary",
               "award-winning", "multi-instrumentalist", "self-taught", "young", "veteran", "seven-piece", "five-piece",
               "four-piece", "three-piece", "two-piece", "monthly", "weekly", "beloved", "underground", "alternative",
               "alt-country", "americana", "latin", "classical", "touring", "new", "emerging", "chicago-based",
               "phoenix-based", "tucson-based", "la-based", "nyc-based", "brooklyn-based"}
NOUNS = (r"band|musician|vocalist|singer|duo|trio|quartet|quintet|sextet|artist|songwriter|DJ|producer|group|"
         r"ensemble|collective|orchestra|outfit|project|rapper|guitarist|pianist|lineup|showcase")
FROM = r"(\s+(?-i:of\s+[a-z][\w-]+|from\s+[A-Z][\w.]*(?:,?\s+[A-Z]{2}\b)?))?"


def describe(bio):
    """A short, sourced description of an act: 'an indie rock musician from Phx'."""
    b = clean(bio)
    m = (re.match(r"((?:(?!is\b|are\b|was\b)[\w'’&-]+\s){0,5}?(?:" + NOUNS + r"))\b" + FROM, b)
         or re.search(r"\b(?:is|are|forms?|formed)\s+(?:an?|the)\s+((?:[\w'’&-]+\s){0,6}?(?:" + NOUNS + r"))\b" + FROM, b, re.I))
    if not m:
        return ""
    d = (m.group(1) + (m.group(2) or "")).strip()
    if len(d) > 70 or len(d.split()) < 2 or re.search(r"\b(?:tickets?|buy|presents?)\b", d, re.I):
        return ""
    first = d.split()[0]
    if first.lower() in COMMON_LEAD or first.lower().endswith("-based"):
        d = d[0].lower() + d[1:]
    return ("an " if re.match(r"[aeiou]", d, re.I) else "a ") + d


def ticketweb_artists(page):
    """[(name, genre, bio)] from the 'Artists' list on TicketWeb venue pages."""
    block = re.search(r'<div class="artist-list">(.*?)(?:<div class="[^"]*(?:related|you-may)|</main>|$)', page, re.S)
    if not block:
        return []
    out = []
    parts = re.split(r"<h4[^>]*>", block.group(1))[1:]
    for part in parts:
        name = clean(re.split(r"</h4>", part)[0])
        genre = clean((re.search(r'class="tw-genre">(.*?)</div>', part, re.S) or [None, ""])[1])
        spans = [clean(s) for s in re.findall(r"<span>(.*?)</span>", part, re.S)]
        bio = next((s for s in spans if len(s) > 12), "")
        # Skip headings that aren't acts (a venue address, a ticket note).
        if name and not re.search(r"\d{3,}|\b(?:AZ|Ave|St|Rd|Blvd|Street|Tickets?)\b|\|", name):
            out.append((name, genre, bio))
    return out


def read_page(e, url, page, final_url, official):
    """Everything the page says about this show, or None if it's a different show."""
    date_iso = e["date"]
    when = dt.date.fromisoformat(date_iso)
    host = urllib.parse.urlparse(final_url).netloc.lower()
    og_title = meta(page, "og:title") or clean((re.search(r"<title>(.*?)</title>", page, re.S) or [None, ""])[1])
    desc = meta(page, "og:description") or meta(page, "description") or meta(page, "twitter:description")
    lds = list(sources.jsonld_objects(page))
    ld = next((o for o in lds if ld_matches_date(o, date_iso)
               and title_tokens(e["title"]) & title_tokens(o.get("name"))), None)
    ld_desc = clean(ld.get("description")) if ld else ""

    # Artist: the headliner's name must appear in the page's own title or event data.
    want = title_tokens(re.split(r"\s+(?:w/|with|feat\.?|\+|\|)\s+", e["title"], maxsplit=1, flags=re.I)[0]) or title_tokens(e["title"])
    have = title_tokens(og_title) | title_tokens(ld.get("name") if ld else "") | title_tokens(desc[:160])
    if not want or len(want & have) < max(1, (len(want) + 1) // 2):
        return None, "artist not found on page"

    # Venue: on the venue's own site, or named in the event data or description.
    vt = venue_tokens(e["venue"])
    loc = ld.get("location") if ld else None
    loc = loc[0] if isinstance(loc, list) and loc else loc
    loc_name = loc.get("name", "") if isinstance(loc, dict) else ""
    venue_text = " ".join([loc_name, og_title, desc, host.replace(".", " ")]).lower()
    if not any(t in venue_text or t in host.replace("-", "") for t in vt):
        return None, f"venue '{e['venue']}' not found on page"

    # Date: event data on the same date, or the same date written in the title/description/URL.
    mentions = date_mentions(og_title + " " + desc[:400] + " " + urllib.parse.unquote(final_url).replace("-", " "))
    date_ok = bool(ld) or (when.month, when.day) in mentions
    if not date_ok:
        return None, "date not confirmed on page"

    status = read_status(og_title + " " + desc[:300] + " " + (ld.get("name", "") if ld else ""))
    if ld and re.search(r"Cancelled", str(ld.get("eventStatus"))):
        status = "Canceled"
    elif ld and re.search(r"Postponed|Rescheduled", str(ld.get("eventStatus"))):
        status = "Postponed"

    d = {"url": final_url if official else url, "official": official}
    if status:
        d["status"] = status
    if status in ("Canceled", "Postponed"):
        return d, "ok"   # a canceled or moved show: the status is what matters

    if not official:
        # Aggregators: a listed start time only. Their prices can include resale.
        t = ld_time(ld.get("startDate")) if ld else ""
        if t:
            d["listed"] = t
        return d, "ok"

    # The text that belongs to this show: description fields, plus the body text
    # right after this show's date (venue pages put "7:00 p.m. $33.50" there).
    body = page_text(page)
    window = ""
    for m in re.finditer(r"(?:%s)[a-z]*\.?\s+%d(?:st|nd|rd|th)?(?:,?\s+%d)?" % (MONTHS[when.month - 1], when.day, when.year),
                         body, re.I):
        window = body[m.end(): m.end() + 160]
        window = re.split(r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b", window, maxsplit=1, flags=re.I)[0]
        if re.search(T_REQ, window, re.I):
            break
    text = " | ".join(x for x in (desc, ld_desc[:600]) if x)

    times = read_times(text) or read_times(window)
    if ld and not any(k in times for k in ("doors", "show", "sets")):
        doors, start = ld_time(ld.get("doorTime")), ld_time(ld.get("startDate"))
        if doors and start and doors != start:
            times.update({"doors": doors, "show": start})
            times.pop("listed", None)
        elif start and "listed" not in times:
            times["listed"] = start
    elif ld and "doors" not in times and times.get("show"):
        doors, start = ld_time(ld.get("doorTime")), ld_time(ld.get("startDate"))
        if doors and start and doors != start and start == times["show"]:
            times["doors"] = doors
    d.update(times)

    price = read_price(text) or read_price(window)
    if not price and ld:
        offers = ld.get("offers") or []
        offers = offers if isinstance(offers, list) else [offers]
        vals = []
        for o in offers:
            if not isinstance(o, dict) or EXCLUDE_TIER.search(str(o.get("name") or o.get("category") or "")):
                continue
            for k in ("price", "lowPrice"):
                try:
                    v = float(str(o.get(k)).replace("$", "").replace(",", ""))
                except (TypeError, ValueError):
                    continue
                if v > 0:   # ticketing sites put 0.00 on paid shows; never read 0 as free
                    vals.append(v)
        if vals:
            price = {"min": min(vals), "text": money(min(vals))}
        if offers and all(isinstance(o, dict) and "SoldOut" in str(o.get("availability")) for o in offers):
            d["status"] = "Sold out"
    if price:
        d["price"] = price
    free = read_free(text, bool(price))
    if free is not None:
        d["free"] = free
    ages = read_ages(text) or read_ages(window)
    if ages:
        d.update(ages)

    m = re.search(r"\b(\w+)\s+drink minimum", text + " " + window, re.I)
    if m:
        d["note"] = f"{m.group(1).lower()}-drink minimum"

    # Lineup: TicketWeb's artist list, or the event data's performers.
    acts = ticketweb_artists(page)
    if acts:
        d["lineup"] = [{"name": sources._title_case_if_shouty(n),
                        **({"about": describe(b)} if describe(b) else {}),
                        **({"genre": g} if g else {})} for n, g, b in acts[:6]]
    elif ld:
        perf = ld.get("performer") or []
        perf = perf if isinstance(perf, list) else [perf]
        names = [clean(p.get("name")) for p in perf if isinstance(p, dict) and p.get("name")]
        if len(names) > 1:
            d["lineup"] = [{"name": sources._title_case_if_shouty(n)} for n in names[:6]]
    about = describe(desc) or describe(ld_desc)
    if about:
        d["about"] = about
    return d, "ok"


# ----------------------------------------------------------------- choosing pages

_official_index = None


def official_index():
    """Exact venue event pages from the calendar fetchers already in sources.py."""
    global _official_index
    if _official_index is None:
        _official_index = []
        for name, fn in (("TicketWeb venues", sources.ticketweb_venues), ("schema.org venues", sources.schema_pages)):
            try:
                got = fn({})
                _official_index.extend(got)
                log(f"  official calendars ({name}): {len(got)} shows")
            except Exception as exc:
                log(f"  official calendars ({name}) skipped: {exc}")
    return _official_index


def host_of(url):
    return urllib.parse.urlparse(url).netloc.lower()


def is_homepage(url):
    p = urllib.parse.urlparse(url)
    return p.path in ("", "/") and not p.query


def candidates(e):
    """Pages to try, best first: (url, official?)."""
    link = e["link"]
    h = host_of(link)
    out = []
    if not any(h.endswith(r) for r in RESALE + AGGREGATORS) and not is_homepage(link):
        out.append((link, True))
    want, vt = title_tokens(e["title"]), venue_tokens(e["venue"])
    for o in official_index():
        if (o.get("date") == e["date"] and o.get("link") and o["link"] != link and not is_homepage(o["link"])
                and vt & venue_tokens(o.get("venue", "") or "")
                and len(want & title_tokens(o.get("title"))) >= max(1, len(want) // 2)):
            out.append((o["link"], True))
    if any(h.endswith(a) for a in AGGREGATORS):
        out.append((link, False))
    seen, uniq = set(), []
    for u, off in out:
        if u not in seen:
            seen.add(u)
            uniq.append((u, off))
    return uniq


def completeness(d):
    if not d:
        return 0
    return (bool(d.get("doors") or d.get("show") or d.get("listed") or d.get("sets"))
            + bool(d.get("price") or d.get("free")) + bool(d.get("ages")) + bool(d.get("lineup")))


# ----------------------------------------------------------------- Go if

def short_time(t):
    m = re.match(r"(\d{1,2}):(\d{2}) (AM|PM)", t or "")
    if not m:
        return t or ""
    return m.group(1) + ("" if m.group(2) == "00" else ":" + m.group(2))


def minutes(t):
    m = re.match(r"(\d{1,2}):(\d{2}) (AM|PM)", t or "")
    if not m:
        return None
    h = int(m.group(1)) % 12 + (12 if m.group(3) == "PM" else 0)
    v = h * 60 + int(m.group(2))
    return v + 24 * 60 if h < 5 else v   # after midnight belongs to the night before


def plan(e):
    d, c = e.get("details") or {}, e.get("confirmed") or {}
    start = (c.get("show") or d.get("show") or d.get("listed")
             or ((d.get("sets") or [{}])[0].get("time")) or c.get("doors") or d.get("doors"))
    return {"doors": c.get("doors") or d.get("doors"), "show": c.get("show") or d.get("show"),
            "start": start, "ends": c.get("ends") or d.get("ends"), "sets": d.get("sets") or [],
            "lineup": d.get("lineup") or [], "status": c.get("status") or d.get("status") or ""}


FORMATS = [
    (r"extended set|all night long|\bb2b\b", "an extended DJ set"),
    (r"album release|record release|ep release", "a release show"),
    (r"\btribute\b", "a tribute show"),
    (r"christmas|holiday", "a holiday show"),
    (r"anniversary", "an anniversary show"),
    (r"farewell|final show|last show", "a farewell show"),
    (r"recital", "a student recital"),
    (r"open mic", "an open mic"),
    (r"\bjam\b", "a jam session"),
]


def go_if(e, same_night):
    """One or two sourced sentences on what kind of night this is, or '' if too little is known."""
    d = e.get("details") or {}
    if not d.get("official") or d.get("status") in ("Canceled", "Postponed"):
        return ""
    p = plan(e)
    lineup = p["lineup"]
    head = lineup[0] if lineup else {}
    about = head.get("about") or d.get("about") or ""
    sub = (e.get("sub") or "").split("/")[0].strip()
    sound = " ".join(w if re.match(r"[A-Z]-|[A-Z]{2}|.*&", w) else w.lower() for w in sub.split()) if sub else ""
    fmt = next((label for pat, label in FORMATS if re.search(pat, e["title"] + " " + (e.get("sub") or ""), re.I)), "")

    if about:
        lead = f"Go if you want to catch {about}"
    elif fmt and sound:
        lead = f"Go if you want {fmt} of {sound}"
    elif fmt:
        lead = f"Go if you want {fmt}"
    elif sound:
        lead = f"Go if you're in the mood for {sound}"
    else:
        return ""
    if fmt and about:
        lead += f" — it's {fmt}"

    # A reason to pick this show over the others in town that night.
    day = WEEKDAYS[dt.date.fromisoformat(e["date"]).weekday()]
    city = [x for x in same_night if x["city"] == e["city"] and x is not e and x.get("type") == "verified"]
    mine = minutes(p["start"])
    starts = [minutes(plan(x)["start"]) for x in city]
    reason = ""
    if not any(x["genre"] == e["genre"] for x in city) and len(city) >= 2:
        reason = f", and it's the only {GENRE_WORDS.get(e['genre'], 'show of its kind')} show in {e['city']} on {day}"
    elif mine is not None and starts and all(s is None or mine < s for s in starts) and mine <= 19 * 60 + 30:
        reason = f", and it's the earliest start in {e['city']} on {day}"
    elif minutes(p["ends"]) and minutes(p["ends"]) >= 24 * 60 and all(
            (minutes(plan(x)["ends"]) or 0) < minutes(p["ends"]) for x in city):
        reason = f", and it runs later than anything else in {e['city']} on {day}"
    if reason and " — " not in lead:
        reason = " — " + reason[len(", and "):]
    first = lead + reason + "."

    second = ""
    support = [a for a in lineup[1:4]]
    if support:
        named = [a["name"] + (f" ({a['about'].split(' ', 1)[1]})" if a.get("about") else "") for a in support]
        joined = named[0] if len(named) == 1 else ", ".join(named[:-1]) + " and " + named[-1]
        second = f"{joined} {'is' if len(named) == 1 else 'are'} on the bill too."
    elif len(p["sets"]) >= 2:
        second = "Sets at " + " and ".join(short_time(s["time"]) for s in p["sets"][:3]) + "."
    elif p["doors"] and p["show"] and p["doors"] != p["show"]:
        second = f"Doors are at {short_time(p['doors'])}, but the show starts at {short_time(p['show'])}."
    out = (first + " " + second).strip()
    return out if len(out) <= 320 else first


# ----------------------------------------------------------------- main

def main(argv):
    force = "--force" in argv
    days = 2
    if "--days" in argv:
        days = max(1, min(7, int(argv[argv.index("--days") + 1])))
    data = json.loads(OUT.read_text(encoding="utf-8"))
    events = data["events"]
    now = now_az()
    today = now.date()
    window = {(today + dt.timedelta(days=i)).isoformat() for i in range(days)}
    todo = [e for e in events if e.get("date") in window]
    log(f"enrich: {len(todo)} shows on {', '.join(sorted(window))} (Arizona time)")

    fetcher = Fetcher()
    stamp = now.isoformat(timespec="minutes")
    checked = kept = failed = skipped = 0
    for e in todo:
        old = e.get("details") or {}
        tried = old.get("tried")
        if not force and tried:
            age_h = (now - dt.datetime.fromisoformat(tried)).total_seconds() / 3600
            if (e["date"] == today.isoformat() and age_h < RECHECK_TODAY_HOURS) or (
                    e["date"] != today.isoformat() and tried[:10] == today.isoformat()):
                skipped += 1
                continue
        log(f"  {e['date']} {e['title'][:60]} @ {e['venue']}")
        found, why = None, "no event page to check"
        cands = candidates(e)
        if not cands:
            why = "only a resale or homepage link, and no venue event page found"
        for url, official in cands:
            got = fetcher.get(url)
            if not got:
                why = "page unavailable"
                continue
            final, page = got
            found, why = read_page(e, url, page, final, official)
            if found:
                break
            log(f"    {url}: {why}")
        if found:
            found["checked"] = today.isoformat()
            found["tried"] = stamp
            if found.get("status") or completeness(found) >= completeness(old) or not old.get("checked"):
                if old.get("goIf") and not found.get("goIf"):
                    pass
                e["details"] = found
                checked += 1
                log(f"    ok: {json.dumps({k: v for k, v in found.items() if k not in ('url', 'tried', 'checked')}, ensure_ascii=False)[:300]}")
            else:
                old["tried"] = stamp
                e["details"] = old
                kept += 1
                log(f"    new check had fewer details; kept the {old.get('checked')} check")
        else:
            failed += 1
            log(f"    no details: {why}" + ("; kept the earlier check" if old.get("checked") else ""))
            e["details"] = {**old, "tried": stamp} if old.get("checked") else {"tried": stamp, "note": why}

    # Go if lines for every enriched show in the window (recomputed, since the
    # night's lineup can change). Hand-written lines live in "confirmed" and win.
    for e in todo:
        d = e.get("details")
        if not d or not d.get("checked"):
            continue
        same = [x for x in events if x["date"] == e["date"]]
        line = go_if(e, same)
        if line:
            d["goIf"] = line
        else:
            d.pop("goIf", None)

    log(f"enrich: {checked} updated, {kept} kept (new check was thinner), {failed} without details, "
        f"{skipped} checked recently, {MAX_FETCHES - fetcher.budget} fetches")
    text = json.dumps(data, ensure_ascii=False, indent=1) + "\n"
    if text != OUT.read_text(encoding="utf-8"):
        OUT.write_text(text, encoding="utf-8")
        log("enrich: wrote events.json")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
