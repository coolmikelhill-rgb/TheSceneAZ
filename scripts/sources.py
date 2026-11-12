"""Extra event sources for TheSceneAZ. Standard library only.

Every fetcher returns a list of dicts in the site's event shape:
date, title, venue, city, genre, sub, scale, price, type, cta, source, link.
refresh_events.py runs each one in its own try/except, so one source failing
never stops the others.

API keys come from environment variables (GitHub Actions secrets). A source
whose key is missing is skipped with a log line, not an error.
"""

import datetime as dt
import html
import json
import os
import re
import time
import urllib.parse
import urllib.request

AZ = dt.timezone(dt.timedelta(hours=-7))
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
DAYS_AHEAD = 270  # how far ahead to pull from the big APIs

# ---------------------------------------------------------------- helpers

def _get(url, data=None, headers=None):
    h = {"User-Agent": BROWSER_UA, "Accept": "*/*"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def _json(url, **kw):
    return json.loads(_get(url, **kw))


def _clean(s):
    s = re.sub(r"<[^>]+>", " ", html.unescape(str(s or "")))
    return re.sub(r"\s+", " ", s).strip()


def _title_case_if_shouty(s):
    return s.title() if s.isupper() and len(s) > 3 else s


# Words in a genre/tag/title -> the site's 11 genres. First match wins.
GENRE_WORDS = [
    ("dnb", r"drum ?(&|and|n)? ?bass|\bdnb\b|jungle"),
    ("techno", r"techno|industrial|minimal|acid|hard ?groove"),
    ("bass", r"dubstep|riddim|bass music|\bbass\b|trap|hardstyle|future bass"),
    ("house", r"house|disco|trance|edm|dance|electronic|afro|garage"),
    ("metal", r"metal|deathcore|hardcore|grindcore|doom|thrash"),
    ("hiphop", r"hip.?hop|\brap\b|r&b|rnb|soul|urban"),
    ("jazz", r"jazz|swing|big band|bebop"),
    ("blues", r"blues|funk"),
    ("country", r"country|americana|bluegrass|folk|western|cumbia|norte|banda|regional mexican"),
    ("rock", r"rock|punk|indie|alternative|emo|grunge|shoegaze|ska|singer.?songwriter"),
    ("pop", r"pop|latin|reggae|world|k-pop|r&b|tejano|mariachi|other"),
]
NOT_MUSIC = re.compile(
    r"comedy|comedian|stand.?up|trivia|jeopardy|haunted|tarot|flea\b|dating|singles|mingle|^closed$|quiz|karaoke|bingo|yoga|workshop|market|podcast|"
    r"wrestling|magic|drag brunch|film|screening|tour of|lecture|"
    r"\bparking\b|\bsuite[s]?\b|\bpremium seating\b|\bupgrade\b|\bvip package\b|gift card", re.I)
SKIP_GENRES = re.compile(r"classical|opera|children|family|religious|gospel|comedy|theatre|theater|"
                         r"musical|spectacular|fairs|festivals? \(non-music\)", re.I)


def genre_of(*texts):
    for text in texts:
        t = (text or "").lower()
        if not t or t in {"undefined", "music"}:
            continue
        for genre, pat in GENRE_WORDS:
            if re.search(pat, t):
                return genre
    return None


ARENA = re.compile(r"arena|stadium|amphithea|pavilion|speedway|raceway|coliseum|ballpark|"
                   r"chase field|state farm|mortgage matchup|footprint|desert diamond|"
                   r"talking stick resort amph|fairgrounds", re.I)
TBA = re.compile(r"\btba\b|\btbd\b|secret|undisclosed|location|dm for|rsvp for", re.I)


def make(date, title, venue, city, genre, sub, price, source, link,
         scale=None, venue_scale=None, cta="Get Tickets"):
    venue = _clean(venue)
    tba = (not venue) or bool(TBA.search(venue))
    if scale is None:
        key = re.sub(r"[^a-z0-9]+", " ", venue.lower()).strip()
        scale = (venue_scale or {}).get(key) or ("warehouse" if tba else
                                                 "arena" if ARENA.search(venue) else "club")
    return {
        "date": date,
        "title": _title_case_if_shouty(_clean(title))[:140],
        "venue": "Location TBA" if tba else venue,
        "city": _clean(city) or "Phoenix",
        "genre": genre,
        "sub": _clean(sub)[:60],
        "scale": "warehouse" if tba else scale,
        "price": (_clean(price) or "See listing")[:80],
        "type": "community" if tba else "verified",
        "cta": "Event Page" if tba else cta,
        "source": source,
        "link": link,
    }


def _window():
    today = dt.datetime.now(AZ).date()
    return today, today + dt.timedelta(days=DAYS_AHEAD)


# ------------------------------------------------- 1. Ticketmaster Discovery
# Free key: https://developer.ticketmaster.com (5,000 calls/day, 5/sec).
# Covers Ticketmaster, Live Nation venues, Universe and FrontGate listings.

def ticketmaster(venue_scale):
    key = os.environ.get("TICKETMASTER_KEY")
    if not key:
        raise RuntimeError("TICKETMASTER_KEY not set")
    start, end = _window()
    out, seen = [], set()
    # The API refuses page*size >= 1000, so walk the calendar 14 days at a time.
    lo = start
    while lo <= end:
        hi = min(lo + dt.timedelta(days=13), end)
        page = 0
        while True:
            q = urllib.parse.urlencode({
                "apikey": key, "stateCode": "AZ", "countryCode": "US",
                "segmentName": "Music", "size": 199, "page": page, "sort": "date,asc",
                "startDateTime": f"{lo}T00:00:00Z",
                "endDateTime": f"{hi + dt.timedelta(days=1)}T06:59:59Z",
            })
            data = _json("https://app.ticketmaster.com/discovery/v2/events.json?" + q)
            time.sleep(0.25)  # stay under 5 requests/second
            for ev in data.get("_embedded", {}).get("events", []):
                if ev.get("id") in seen:
                    continue
                seen.add(ev.get("id"))
                d = ev.get("dates", {}).get("start", {}).get("localDate")
                status = ev.get("dates", {}).get("status", {}).get("code", "")
                vs = ev.get("_embedded", {}).get("venues", [{}])
                v = vs[0] if vs else {}
                if not d or status in {"canceled", "cancelled"} or v.get("state", {}).get("stateCode") != "AZ":
                    continue
                name = ev.get("name", "")
                c = (ev.get("classifications") or [{}])[0]
                g, sg = c.get("genre", {}).get("name", ""), c.get("subGenre", {}).get("name", "")
                if NOT_MUSIC.search(name) or SKIP_GENRES.search(g):
                    continue
                genre = genre_of(g, sg, name)
                if not genre:
                    continue
                pr = (ev.get("priceRanges") or [{}])[0]
                price = f"${pr['min']:.0f}+" if pr.get("min") else "See listing"
                t = ev.get("dates", {}).get("start", {}).get("localTime", "")[:5]
                if t:
                    hh, mm = int(t[:2]), t[3:]
                    price = f"{(hh % 12) or 12}:{mm}{'pm' if hh >= 12 else 'am'} · {price}"
                out.append(make(d, name, v.get("name", ""), v.get("city", {}).get("name", ""),
                                genre, " / ".join(x for x in (g, sg) if x and x != "Undefined"),
                                price, "Ticketmaster", ev.get("url", ""), venue_scale=venue_scale))
            pg = data.get("page", {})
            if page + 1 >= pg.get("totalPages", 0) or (page + 2) * 199 >= 1000:
                break
            page += 1
        lo = hi + dt.timedelta(days=1)
    return out


# ------------------------------------------------------------ 2. SeatGeek
# Free client id: https://seatgeek.com/account/develop . SeatGeek lists many
# bar and club shows that Ticketmaster does not (it measured 263 Arizona
# concerts in the 30 days from 2026-11-12, within 150 miles of Phoenix).

AZ_CENTRES = [(33.4484, -112.0740, 60),   # Phoenix metro
              (32.2217, -110.9265, 50),   # Tucson
              (35.1983, -111.6513, 60),   # Flagstaff / Sedona / Prescott side
              (32.6927, -114.6277, 40),   # Yuma
              (34.5400, -112.4685, 30),   # Prescott
              (34.4839, -114.3225, 40)]   # Lake Havasu


def seatgeek(venue_scale):
    cid = os.environ.get("SEATGEEK_CLIENT_ID")
    if not cid:
        raise RuntimeError("SEATGEEK_CLIENT_ID not set")
    start, end = _window()
    out, seen = [], set()
    for lat, lon, miles in AZ_CENTRES:
        page = 1
        while page <= 30:
            q = urllib.parse.urlencode({
                "client_id": cid, "lat": lat, "lon": lon, "range": f"{miles}mi",
                "taxonomies.name": "concert", "per_page": 100, "page": page,
                "datetime_local.gte": start.isoformat(), "datetime_local.lte": end.isoformat(),
                "sort": "datetime_local.asc",
            })
            data = _json("https://api.seatgeek.com/2/events?" + q)
            evs = data.get("events", [])
            for ev in evs:
                v = ev.get("venue") or {}
                if ev.get("id") in seen or v.get("state") != "AZ" or ev.get("date_tbd"):
                    continue
                seen.add(ev.get("id"))
                title = ev.get("title", "")
                if NOT_MUSIC.search(title):
                    continue
                perf = ev.get("performers") or []
                gnames = [g.get("name", "") for p in perf for g in (p.get("genres") or [])]
                genre = genre_of(" ".join(gnames), title) or "rock"
                low = (ev.get("stats") or {}).get("lowest_price")
                price = f"from ${low}" if low else "See listing"
                out.append(make(ev["datetime_local"][:10], title, v.get("name", ""), v.get("city", ""),
                                genre, " / ".join(dict.fromkeys(gnames))[:60] or "", price,
                                "SeatGeek", ev.get("url", ""), venue_scale=venue_scale))
            meta = data.get("meta", {})
            if page * 100 >= meta.get("total", 0) or not evs:
                break
            page += 1
    return out


# ------------------------------------------------------------ 3. Edmtrain
# Free key on request: https://edmtrain.com/developer-api . Raves, club
# nights and festivals (electronic only by default).

def edmtrain(venue_scale):
    key = os.environ.get("EDMTRAIN_KEY")
    if not key:
        raise RuntimeError("EDMTRAIN_KEY not set")
    locs = _json("https://edmtrain.com/api/locations?" +
                 urllib.parse.urlencode({"state": "Arizona", "client": key}))
    ids = ",".join(str(l["id"]) for l in locs.get("data", []))
    if not ids:
        return []
    data = _json("https://edmtrain.com/api/events?" +
                 urllib.parse.urlencode({"locationIds": ids, "livestreamInd": "false", "client": key}))
    out = []
    for ev in data.get("data", []):
        v = ev.get("venue") or {}
        artists = [a.get("name", "") for a in ev.get("artistList") or []]
        title = ev.get("name") or ", ".join(artists[:4])
        if not title or not ev.get("date"):
            continue
        city = (v.get("location") or "").split(",")[0]
        out.append(make(ev["date"][:10], title, v.get("name", ""), city,
                        genre_of(title) or "house", "Festival" if ev.get("festivalInd") else "Electronic",
                        ev.get("ages") or "See listing", "Edmtrain", ev.get("link", ""),
                        scale="arena" if ev.get("festivalInd") else None, venue_scale=venue_scale,
                        cta="Event Page"))
    return out


# -------------------------------------- 4. TicketWeb "Event Discovery" venues
# WordPress venue sites running TicketWeb's calendar plugin answer an
# admin-ajax call with every upcoming show. No key. Tested 2026-11-12:
# 128 music shows from these two rooms after filtering, 97 not yet on the site.

TICKETWEB_VENUES = [
    # (calendar page, city, default genre)
    ("https://www.crescentphx.com/calendar/", "Phoenix", "rock"),
    ("https://www.valleybarphx.com/calendar/", "Phoenix", "rock"),
]


def ticketweb_venues(venue_scale):
    out = []
    start, end = _window()
    for page_url, city, default_genre in TICKETWEB_VENUES:
        try:
            page = _get(page_url)
            m = re.search(r"action: 'get_events_for_calendar',\s*nonce: '(\w+)',.*?params: '(.*?)',", page, re.S)
            if not m:
                raise RuntimeError("calendar plugin not found")
            base = page_url.split("/calendar")[0]
            body = urllib.parse.urlencode({
                "action": "get_events_for_calendar", "nonce": m.group(1),
                "start": start.isoformat(), "end": end.isoformat(), "params": m.group(2),
                "monthview": "standard", "calcount": "1", "statefilter": "", "cityfilter": "",
                "locationfilter": "", "showmobilelist": "true"}).encode()
            data = json.loads(_get(base + "/wp-admin/admin-ajax.php", data=body,
                                   headers={"Referer": page_url}))
            popup = (data.get("popupdata") or "").replace("\\", "")
            links = dict(re.findall(r'id="tw-event-dialog-(\d+)".*?href="([^"]+)"', popup, re.S))
            for ev in data.get("events", []):
                title = _clean(ev.get("title"))
                if not title or NOT_MUSIC.search(title):
                    continue
                link = links.get(str(ev.get("id")), page_url)
                out.append(make(ev["start"][:10], title, _clean(ev.get("venue")), city,
                                genre_of(title) or default_genre, "", ev.get("displayTime") or "",
                                _clean(ev.get("venue")) or "Venue site", link, venue_scale=venue_scale))
        except Exception as exc:
            print(f"  ticketweb {page_url} skipped: {exc}")
    return out


# --------------------------------- 5. Venue pages with schema.org event data
# Any page that marks shows up as JSON-LD (MusicEvent/Event) or microdata
# (itemprop="startDate"). Tested 2026-11-12: The Van Buren 25 shows (JSON-LD),
# Hotel Congress / Club Congress, Tucson 179 events (microdata).
# Add a venue by adding a line here; nothing else changes.

SCHEMA_PAGES = [
    # (url, default city, default genre)
    ("https://www.thevanburenphx.com/", "Phoenix", "rock"),
    ("https://hotelcongress.com/events/", "Tucson", "rock"),
    # add more here, e.g. any venue page where view-source shows "MusicEvent"
]

# Rooms whose shows are nearly all one genre, used when the title says nothing.
VENUE_GENRE = {"the century room": "jazz", "westside blues jazz more": "blues",
               "rhythm room": "blues", "ravenscroft jazzbird lounge": "jazz"}


def _jsonld_events(page):
    for block in re.findall(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", page, re.S):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            it = stack.pop()
            if isinstance(it, list):
                stack.extend(it)
            elif isinstance(it, dict):
                stack.extend(v for v in it.values() if isinstance(v, (list, dict)))
                t = it.get("@type")
                t = " ".join(t) if isinstance(t, list) else str(t or "")
                if t.endswith("Event") and it.get("startDate"):
                    loc = it.get("location") or {}
                    loc = loc[0] if isinstance(loc, list) and loc else loc
                    addr = loc.get("address") if isinstance(loc, dict) else {}
                    city = addr.get("addressLocality", "") if isinstance(addr, dict) else ""
                    offers = it.get("offers") or {}
                    offers = offers[0] if isinstance(offers, list) and offers else offers
                    yield {"date": str(it["startDate"])[:10], "title": it.get("name", ""),
                           "venue": loc.get("name", "") if isinstance(loc, dict) else "",
                           "city": city, "link": it.get("url") or (offers.get("url") if isinstance(offers, dict) else ""),
                           "price": f"${offers['price']}" if isinstance(offers, dict) and offers.get("price") else ""}


def _microdata_events(page, page_url):
    # Splits the page at each itemprop="location" block (one per event card) and
    # reads the date, event name and nearest absolute link around it.
    parts = re.split(r'(?=<div[^>]*itemprop="location")', page)
    for chunk in parts[1:]:
        sd = re.search(r'itemprop="startDate"[^>]*content="(\d{4}-\d{2}-\d{2})', chunk)
        nm = re.search(r'itemprop="name"[^>]*>(.*?)<', chunk[sd.end():] if sd else "", re.S)
        vn = re.search(r'itemprop="name">(.*?)<', chunk)
        if not sd or not nm:
            continue
        before = page[:page.find(chunk)][-3000:]
        links = re.findall(r'href="(https?://[^"]+)"', before)
        yield {"date": sd.group(1), "title": nm.group(1), "venue": vn.group(1) if vn else "",
               "city": "", "link": links[-1] if links else page_url, "price": ""}


def schema_pages(venue_scale):
    out = []
    for url, city, default_genre in SCHEMA_PAGES:
        try:
            page = _get(url)
            found = list(_jsonld_events(page)) or list(_microdata_events(page, url))
            for e in found:
                if not e["title"] or NOT_MUSIC.search(e["title"]):
                    continue
                if not str(e["link"]).startswith("http"):
                    e["link"] = url
                vkey = re.sub(r"[^a-z0-9]+", " ", _clean(e["venue"]).lower()).strip()
                out.append(make(e["date"], e["title"], e["venue"], e["city"] or city,
                                genre_of(e["title"]) or VENUE_GENRE.get(vkey, default_genre), "", e["price"],
                                _clean(e["venue"]) or "Venue site", e["link"], venue_scale=venue_scale))
        except Exception as exc:
            print(f"  schema page {url} skipped: {exc}")
    return out


# ------------------------------------------- 6. Community submissions
# A Google Form ("Submit a show") feeds a Google Sheet. The owner types "yes"
# in the sheet's Approved column for each real show. The sheet is published
# as CSV (File > Share > Publish to web > CSV) and its URL is stored in the
# SUBMISSIONS_CSV_URL secret. Promoters need no account to submit.

SITE_GENRES = {"house", "techno", "bass", "dnb", "rock", "hiphop", "country",
               "pop", "jazz", "metal", "blues"}


def _form_date(s):
    s = (s or "").strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return s
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)  # Google Forms exports M/D/YYYY
    return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}" if m else ""


def submissions(venue_scale):
    url = os.environ.get("SUBMISSIONS_CSV_URL")
    if not url:
        raise RuntimeError("SUBMISSIONS_CSV_URL not set")
    import csv
    import io
    rows = csv.DictReader(io.StringIO(_get(url)))
    out = []
    for r in rows:
        r = {(k or "").strip().lower(): v.strip() for k, v in r.items() if isinstance(v, str)}
        if r.get("approved", "").lower() not in {"yes", "y", "x", "true"}:
            continue
        date, link = _form_date(r.get("date")), r.get("ticket or event link", "")
        if not date or not link.startswith(("http://", "https://")):
            continue
        g = re.sub(r"[^a-z]", "", r.get("genre", "").lower())
        g = {"drumandbass": "dnb", "hiphoprb": "hiphop"}.get(g, g)
        if g not in SITE_GENRES:
            g = genre_of(r.get("genre"), r.get("event name")) or "house"
        out.append(make(date, r.get("event name", ""), r.get("venue (or tba)", ""),
                        r.get("city", ""), g, r.get("sub-genre", ""), r.get("price", ""),
                        "Community", link, venue_scale=venue_scale, cta="Event Page"))
    return out


# Order matters: earlier sources win when two describe the same show.
SOURCES = [
    ("Community", submissions),
    ("Venue sites (TicketWeb)", ticketweb_venues),
    ("Venue sites (schema.org)", schema_pages),
    ("Ticketmaster", ticketmaster),
    ("SeatGeek", seatgeek),
    # Edmtrain is left out on purpose: its API terms forbid combining its events
    # with other sources in a competing event-discovery site.
]
