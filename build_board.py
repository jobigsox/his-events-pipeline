#!/usr/bin/env python3
"""
Build the HIS Toronto curation board.

Crawls every enabled source in sources.json, scores and classifies what it finds
for a rolling window starting today, and writes board.html.

  python build_board.py                 # 3-month window from today
  python build_board.py --months 4      # longer window
  python build_board.py --from 2026-09-01
  python build_board.py --top 8         # rows kept per stream per month
  python build_board.py --json out.json # also dump the normalized events

Standard library only, so it runs anywhere with Python 3.8+.
"""

import argparse
import datetime as dt
import html
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

CATEGORIES = ("social", "practical", "spiritual")

CITY = {}          # loaded from cities/<slug>.json at run time

MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

MIN_SCORE = 55      # below this it isn't worth a curator's attention
MAX_REPEATS = 2     # occurrences kept per identical title, per source, per month

# Sources whose events default to a neutral/non-free `cost` string ("Paid",
# with no dollar amount to match the scorer's cheap-ticket regex) so they
# never accumulate the 25 cost points a free event gets - without this
# exemption almost everything from an aggregator like this drops below
# MIN_SCORE on cost alone, despite being exactly the kind of real festival/
# concert a student would go to. Capped instead to the best few per rolling
# 4-week window, below, so they can't crowd out the free practical/social feed.
TICKETED_KINDS = {"ticketmaster", "ottawa_tourism", "audienceview", "visit_kingston",
                  "downtown_kelowna_events"}
# Feeds whose listings never carry a price, so the cost part of `score` can't
# judge them: capped like ticketed feeds unless a source sets "capped": false.
CAPPED_BY_DEFAULT = {"imis_events"}

# ---------------------------------------------------------------- scoring

NEWCOMER = ("newcomer", "international", "immigrant", "refugee", "settlement",
            "permit", "visa", "esl", "orientation", "arrival", "citizenship",
            "uhip", "sin ", "intercultural", "language")
STUDENT = ("student", "campus", "grad", "undergrad", "university", "college",
           "career", "job", "intern", "resume", "linkedin", "employment",
           "young adult", "youth")
CONNECT = ("social", "meet", "mixer", "games", "festival", "dinner", "brunch",
           "community", "chat", "conversation", "welcome", "tour", "picnic",
           "bbq", "coffee", "gathering", "celebration", "concert", "film")
EXCLUDE = ("senior", "age 55", "0-6", "2-6", "kids", "children", "child care",
           "earlyon", "toddler", "parent", "caregiver", "disabilit", "13-17",
           "high school", "grade ", "prenatal", "pregnan", "preschool",
           "storytime", "babies", "baby ")

# "service" alone is a trap: it matches "Employment Services" and "Support
# Services" far more often than a worship service. Keep the terms unambiguous.
SPIRITUAL_HINT = ("worship", "church", "bible", "prayer", "faith", "gospel",
                  "ministry", "chapel", "congregation", "sermon", "scripture",
                  "sunday service", "worship service", "mass", "parish")
PRACTICAL_HINT = ("permit", "visa", "sin", "insurance", "uhip", "tax", "bank",
                  "housing", "job", "career", "resume", "employment", "legal",
                  "clinic", "workshop", "info session", "orientation", "q&a",
                  "citizenship", "health", "training", "program")
SOCIAL_HINT = ("festival", "concert", "party", "games", "social", "tour",
               "dinner", "brunch", "picnic", "film", "movie", "dance", "music",
               "celebration", "market", "sports", "walk")


def _has(text, words):
    return any(w in text for w in words)


def classify(title, description, source_default):
    t = (title + " " + description).lower()
    # Only trust a spiritual read when the source is a faith organisation, or
    # the title itself says so — a stray mention in a description isn't enough.
    if _has(t, SPIRITUAL_HINT) and (source_default == "spiritual"
                                    or _has(title.lower(), SPIRITUAL_HINT)):
        return "spiritual"
    social = sum(1 for w in SOCIAL_HINT if w in t)
    practical = sum(1 for w in PRACTICAL_HINT if w in t)
    if social > practical:
        return "social"
    if practical > social:
        return "practical"
    return source_default


def score(ev):
    """0-100. Cost 25, newcomer fit 25, student fit 25, access 15, connection 10."""
    t = (ev["title"] + " " + ev.get("description", "")).lower()
    pts = 0

    cost = (ev.get("cost") or "").strip().lower()
    if cost in ("", "free", "0", "$0", "free!"):
        pts += 25
    elif re.search(r"\$\s*(\d+)", cost) and int(re.search(r"\$\s*(\d+)", cost).group(1)) <= 15:
        pts += 16

    pts += 25 if _has(t, NEWCOMER) else 6
    pts += 25 if _has(t, STUDENT) else 6

    venue = (ev.get("venue") or "").lower()
    central = CITY.get("central_venue_words", [])
    if "online" in venue or "virtual" in venue or "zoom" in venue:
        pts += 11
    elif any(w in venue for w in central):
        pts += 15
    else:
        pts += 8

    pts += 10 if _has(t, CONNECT) else 3

    if _has(t, EXCLUDE):
        pts -= 45

    return max(0, min(100, pts))


# ---------------------------------------------------------------- fetching

_TRANSIENT_HTTP = (429, 502, 503, 504)   # rate-limited / gateway hiccups - standard
                                          # client robustness to retry, unlike a 403
                                          # (which means "not welcome" rather than
                                          # "try again"; that gets no blanket retry
                                          # here, only the govstack-specific handling)


def fetch_text(url, timeout=30, accept="text/html,application/xhtml+xml,*/*", extra_headers=None):
    headers = {
        "User-Agent": UA,
        "Accept": accept,
        "Accept-Language": "en-CA,en;q=0.9",
    }
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                # Blindly decoding as UTF-8 mojibakes smart quotes/dashes on sites
                # that declare (and actually serve) a different charset, e.g.
                # windows-1252 WordPress installs - respect the server's own header
                # when present, only falling back to UTF-8 if it's missing or wrong.
                charset = r.headers.get_content_charset()
                if charset:
                    try:
                        return raw.decode(charset, "strict")
                    except (LookupError, UnicodeDecodeError):
                        pass
                return raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code not in _TRANSIENT_HTTP or attempt == 2:
                raise
            retry_after = e.headers.get("Retry-After") if e.headers else None
            try:
                wait = float(retry_after) if retry_after else 1.5 * (attempt + 1)
            except ValueError:
                wait = 1.5 * (attempt + 1)
            time.sleep(min(wait, 10))


def fetch(url, timeout=25):
    text = fetch_text(url, timeout, "application/json, text/plain, */*")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Some WordPress installs intermittently prepend a stray PHP
        # notice/warning (a plugin deprecation, a theme function emitting
        # output too early) before the real JSON body - St. Paul's Bloor
        # Street's tribe feed hit exactly this (JSONDecodeError on an
        # otherwise-valid-looking response). Trimming to the outermost
        # {...} or [...] recovers the real payload in that case; if the
        # response genuinely isn't JSON at all, the trim still fails to
        # parse and the original error is what the caller sees - this
        # never masks a truly broken feed, only a cosmetic prefix on one.
        start = min((i for i in (text.find("{"), text.find("[")) if i != -1), default=-1)
        end = max(text.rfind("}"), text.rfind("]"))
        if start != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def parse_tribe(src, start, end):
    """The Events Calendar REST API."""
    out, page, pages = [], 1, 1
    while page <= pages and page <= int(src.get("max_pages") or 6):
        q = urllib.parse.urlencode({
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "per_page": 50,
            "page": page,
        })
        sep = "&" if "?" in src["url"] else "?"
        data = fetch(src["url"] + sep + q)
        pages = int(data.get("total_pages") or 1)
        for e in data.get("events", []):
            venue = ""
            v = e.get("venue") or {}
            if isinstance(v, list):                 # some tribe sites report multiple venues
                v = v[0] if v and isinstance(v[0], dict) else {}
            if v:
                venue = ", ".join(x for x in [v.get("venue"), v.get("address"), v.get("city")] if x)
            had_venue = bool(venue)
            if not venue:
                venue = "Online"
            cost = (e.get("cost") or "").strip() or "Free"
            img = None
            if isinstance(e.get("image"), dict):
                img = e["image"].get("url")
            out.append({
                "source_id": src["id"], "source": src["name"],
                "title": strip_html(e.get("title")),
                "description": strip_html(e.get("description"))[:400],
                "start": e.get("start_date"), "end": e.get("end_date"),
                "utc": src.get("tz") == "utc",
                "venue": venue, "cost": cost, "no_venue": not had_venue,
                "url": e.get("url"), "image": img,
            })
        page += 1
    return out


def parse_sqs(src, start, end):
    """Squarespace ?format=json — startDate is epoch ms."""
    try:
        data = fetch(src["url"])
    except json.JSONDecodeError:
        # Some Squarespace sites serve HTML at ?format=json (collection not a
        # real events collection). Nothing to parse; report empty, not a crash.
        return []
    out = []
    for e in data.get("items", []):
        ms = e.get("startDate")
        if not ms:
            continue
        s = dt.datetime.fromtimestamp(ms / 1000)
        if not (start <= s.date() <= end):
            continue
        ems = e.get("endDate") or ms
        loc = e.get("location") or {}
        venue = ", ".join(x for x in [loc.get("addressTitle"), loc.get("addressLine1"),
                                      loc.get("addressLine2")] if x) or "See event page"
        base = src["home"].rstrip("/")
        out.append({
            "source_id": src["id"], "source": src["name"],
            "title": strip_html(e.get("title")),
            "description": strip_html(e.get("excerpt") or e.get("body"))[:400],
            "start": s.strftime("%Y-%m-%d %H:%M:%S"),
            "end": dt.datetime.fromtimestamp(ems / 1000).strftime("%Y-%m-%d %H:%M:%S"),
            "utc": False, "venue": venue, "cost": "Free",
            "url": base + (e.get("fullUrl") or ""),
            "image": e.get("assetUrl"),
        })
    return out


def parse_sqs_rss(src, start, end):
    """Squarespace event collection displayed as a *calendar* rather than a
    list. `?format=json` on a calendar-display collection returns only a bare
    array of {title,start,end,url} for the current month with no working
    pagination (a `month=` param is silently ignored - verified across two
    tenants), so `parse_sqs` (which wants the list-display `items[]` shape)
    gets nothing useful.

    `?format=rss` on the same collection DOES span a wide window (~6 months)
    and carries title/description/image, but its only date is the trailing
    `/YYYY-MM-DD` on each item's link (the <pubDate> is the post's creation
    date, not the event's). So: take the item list + metadata from the RSS,
    and layer the current month's precise start/end *times* on top from the
    JSON feed where an item's date falls in that window. Events further out
    get a date but no time (the board shows "Time on the event page").

    `src["url"]` is the collection page, e.g. https://<site>/events/ .

    `src["title_filter"]` (optional) is a case-insensitive regex; only items
    whose title matches are kept. A church's Squarespace calendar carries its
    whole worship schedule (Sunday services, Communion, weekly groups) and a
    free event at a central address floors at ~55 with zero newcomer/student
    signal, so a source registered for one specific programme (a monthly
    newcomer meal, a young-adults night) needs to say so - same idea as the
    OPL `?text=` query scoping and the queensu_events title noise filter.
    """
    coll = src["url"].rstrip("/")
    keep = re.compile(src["title_filter"], re.I) if src.get("title_filter") else None

    times = {}  # url-path -> (start_iso, end_iso)
    try:
        for e in fetch(coll + "/?format=json"):
            u = e.get("url")
            if u and e.get("start"):
                times[u] = (e["start"][:19].replace("T", " "),
                            (e.get("end") or e["start"])[:19].replace("T", " "))
    except (json.JSONDecodeError, urllib.error.URLError, KeyError):
        pass

    xml = fetch_text(coll + "/?format=rss", accept="application/rss+xml, application/xml, */*")
    out, seen = [], set()
    for block in re.findall(r"<item>(.*?)</item>", xml, re.S):
        link_m = re.search(r"<link>([^<]+)</link>", block)
        title_m = re.search(r"<title>(.*?)</title>", block, re.S)
        if not (link_m and title_m):
            continue
        path = re.sub(r"^https?://[^/]+", "", link_m.group(1).strip())
        date_m = re.search(r"/(\d{4})-(\d{2})-(\d{2})$", path)
        if not date_m:
            continue
        d = dt.date(int(date_m.group(1)), int(date_m.group(2)), int(date_m.group(3)))
        if not (start <= d <= end) or path in seen:
            continue
        title = strip_html(title_m.group(1))
        if keep and not keep.search(title):
            continue
        seen.add(path)
        sd, ed = times.get(path, (d.strftime("%Y-%m-%d 00:00:00"),
                                  d.strftime("%Y-%m-%d 00:00:00")))
        desc_m = re.search(r"<description>(.*?)</description>", block, re.S)
        img_m = re.search(r'<itunes:image href="([^"]+)"', block)
        out.append({
            "source_id": src["id"], "source": src["name"],
            "title": title,
            "description": strip_html(desc_m.group(1))[:400] if desc_m else "",
            "start": sd, "end": ed, "utc": False,
            "venue": src.get("default_venue", src["name"]), "cost": "Free",
            "url": link_m.group(1).strip(),
            "image": img_m.group(1) if img_m else None,
        })
    return out


def parse_communico(src, start, end):
    """Communico / libnet calendar (Hamilton Public Library and friends).

    The public site hides this behind a widget; the widget calls
    /eeventcaldata with a JSON `req` blob. `days` spans the whole window in
    one shot, so a quarter is a single request.
    """
    days = (end - start).days + 1
    req = json.dumps({"private": False, "date": start.isoformat(), "days": days,
                      "locations": [], "ages": [], "types": []})
    url = src["url"] + "?event_type=0&req=" + urllib.parse.quote(req)
    rows = fetch(url, timeout=60)
    base = src.get("event_url_base") or (src["home"].rstrip("/") + "/event/")
    out = []
    for e in rows if isinstance(rows, list) else []:
        ages = (e.get("ages") or "")
        # The age band is authoritative here — far better than guessing from
        # the title whether "Storytime" is for toddlers.
        if re.search(r"early years|children|kids|\(0 to|\(6 to|\(3 to|teen|tween", ages, re.I):
            continue
        city_name = CITY.get("city", "")
        venue = ", ".join(x for x in [e.get("venue_name") or e.get("location"),
                                      e.get("venue_room")] if x) or e.get("library") or city_name
        # Communico only ever returns the bare branch name (e.g. "Chinguacousy"),
        # never the city — so central_venue_words' catch-all city-name entry
        # (every communico city lists its own name there) never matched any
        # branch except the rare one whose branch name happens to double as a
        # landmark word. Appending the city here makes "every library branch
        # in this city is reasonably reachable" true for every branch, not
        # just the one that got lucky. (Also drops the old hardcoded
        # "Hamilton" fallback, which was wrong for every other communico
        # city's zero-venue edge case.)
        if city_name and city_name.lower() not in venue.lower():
            venue = venue + ", " + city_name
        cost = (e.get("registration_cost") or "").strip()
        out.append({
            "source_id": src["id"], "source": src["name"],
            "title": strip_html(e.get("title")),
            "description": strip_html(e.get("description") or e.get("long_description"))[:400],
            "start": e.get("event_start"), "end": e.get("event_end"),
            "utc": False, "venue": venue,
            "cost": "Free" if cost in ("", "0", "0.00", "$0") else cost,
            "url": base + str(e.get("id")), "image": None,
        })
    return out


OPL_BLOCK = '<div class="entity-panels-entity node node-search-result node-event"'


def parse_opl(src, start, end):
    """Ottawa Public Library's Drupal 7 booking site.

    No JSON API of any kind (jsonapi/, _format=json and friends all 404), and
    BiblioCommons' own events module is switched off, so the listing HTML is
    the only way in. Markup is Drupal Views panels; the useful bits sit in
    field-name-* wrappers.
    """
    pages = int(src.get("pages") or 12)
    base = src["home"].rstrip("/")
    out, seen = [], set()

    for pg in range(pages):
        # Drupal multi-pager: the listing is the SECOND pager on the page, so
        # the parameter is page=0,N. A plain page=N silently returns page one.
        url = src["url"] + ("&" if "?" in src["url"] else "?") + "page=" + urllib.parse.quote("0,%d" % pg)
        try:
            raw = fetch_text(url)
        except Exception:                       # noqa: BLE001 - stop paging, keep what we have
            break
        blocks = raw.split(OPL_BLOCK)[1:]
        if not blocks:
            break

        for b in blocks:
            b = b[:8000]
            a = re.search(r'<a href="(/[^"]*)"[^>]*>(.*?)</a>', b, re.S)
            if not a:
                continue
            href = a.group(1)
            if href in seen:
                continue
            seen.add(href)

            title = strip_html(a.group(2))
            dates = re.search(r'field-name-field-event-dates(.*?)</div>\s*</div>', b, re.S)
            dtxt = strip_html(dates.group(1)) if dates else strip_html(b)
            m = re.search(r"(\w{3})\w*\s+(\d{1,2}),\s*(20\d\d)\s*at\s*(\d{1,2}):(\d{2})\s*([ap])m", dtxt, re.I)
            if not m:
                continue
            try:
                mon = MONTH_ABBR.index(m.group(1).title()) + 1
            except ValueError:
                continue
            hh = int(m.group(4)) % 12 + (12 if m.group(6).lower() == "p" else 0)
            sd = dt.datetime(int(m.group(3)), mon, int(m.group(2)), hh, int(m.group(5)))

            body = re.search(r'field-name-body(.*?)field-name-field-registration', b, re.S)
            desc = strip_html(body.group(1)) if body else ""

            # The branch is a bare text node sitting between the title field and
            # the dates field — there is no class of its own to grab.
            venue = ""
            di = b.find("field-name-field-event-dates")
            if di > a.end():
                # slice from AFTER the title link so no class attributes leak in
                # the slice can end mid-tag, and a truncated "<div class=..."
                # has no ">" so strip_html would leave it behind
                raw_tail = re.sub(r"<[^>]*$", "", b[a.end():di])
                tail = strip_html(raw_tail).replace(title, " ")
                cand = [x.strip(" >|-") for x in re.split(r"[|>]+", tail)
                        if 2 < len(x.strip(" >|-")) < 60]
                if cand:
                    venue = max(cand, key=len)
            if not venue or "field-" in venue:
                venue = "Ottawa Public Library"

            out.append({
                "source_id": src["id"], "source": src["name"],
                "title": title, "description": desc[:400],
                "start": sd.strftime("%Y-%m-%d %H:%M:%S"),
                "end": (sd + dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
                "utc": False, "venue": venue, "cost": "Free",
                "url": base + href, "image": None,
            })
    return out


def parse_probe(src, start, end):
    """Registered but not yet machine-readable — reported, never guessed at."""
    raise NotImplementedError("no feed parser yet")


def _manual_events_for(src, start, end):
    """manual_events entries keyed to this source_id - pulled out of
    parse_manual so a `probe` source (no live feed) can still surface its
    hand-typed dates instead of silently dropping them (collect() used to
    skip probe sources before any parser ran, manual or not)."""
    out = []
    for e in CITY.get("manual_events", []):
        if e.get("source_id") != src["id"]:
            continue
        s = dt.datetime.fromisoformat(e["start"])
        if not (start <= s.date() <= end):
            continue
        out.append({
            "source_id": src["id"], "source": src["name"],
            "title": e["title"], "description": "",
            "start": e["start"], "end": e.get("end") or e["start"],
            "utc": False, "venue": e.get("venue", ""), "cost": e.get("cost", "Free"),
            "url": e["url"], "image": e.get("image"),
            "forced_category": e.get("category"),
            "already_live": e.get("already_live", False),
            "verify": e.get("verify"),
            "hand_entered": True,
        })
    return out


def parse_manual(src, start, end):
    return _manual_events_for(src, start, end)


def _parse_mcmaster_date(raw):
    """'Month D, YYYY[ @ H:MM AM[ to H:MM AM]][ to Month D, YYYY]' -> (start, end|None)."""
    raw = raw.strip()
    m = re.match(r"([A-Za-z]+ \d{1,2}, \d{4})\s*@\s*(\d{1,2}:\d{2}\s*[AP]M)"
                 r"(?:\s*to\s*(\d{1,2}:\d{2}\s*[AP]M))?", raw)
    if m:
        date_s, t1, t2 = m.groups()
        sd = dt.datetime.strptime(date_s + " " + t1, "%B %d, %Y %I:%M %p")
        ed = dt.datetime.strptime(date_s + " " + t2, "%B %d, %Y %I:%M %p") if t2 else None
        return sd, ed
    m = re.match(r"([A-Za-z]+ \d{1,2}, \d{4})(?:\s*to\s*([A-Za-z]+ \d{1,2}, \d{4}))?", raw)
    if m:
        d1, d2 = m.groups()
        sd = dt.datetime.strptime(d1, "%B %d, %Y")
        ed = dt.datetime.strptime(d2, "%B %d, %Y") if d2 else None
        return sd, ed
    return None, None


def parse_event_espresso(src, start, end):
    """Event Espresso REST API (wp-json/ee/vX.Y.Z/...) - datetimes and events
    are separate resources, so pull upcoming datetimes then batch-join events
    by EVT_ID. `src["url"]` is the site's ee namespace base, e.g.
    'https://site/wp-json/ee/v4.8.36'."""
    dt_url = (src["url"] + "/datetimes?" + urllib.parse.urlencode({
        "where[DTT_EVT_start][>=]": start.strftime("%Y-%m-%dT00:00:00-04:00"),
        "order_by": "DTT_EVT_start", "order": "asc", "limit": 200,
    }))
    dtts = fetch(dt_url)
    if not dtts:
        return []
    evt_ids = sorted({d["EVT_ID"] for d in dtts})
    events_by_id = {}
    for i in range(0, len(evt_ids), 8):
        # Sucuri's WAF 403s an EVT_ID IN(...) list once it hits ~10 comma-
        # separated values (looks like a generic "too many list args" rule) -
        # keep batches small rather than tripping it.
        chunk = evt_ids[i:i + 8]
        ev_url = (src["url"] + "/events?" + urllib.parse.urlencode({
            "where[EVT_ID][IN]": ",".join(str(x) for x in chunk), "limit": 50,
        }))
        for e in fetch(ev_url):
            events_by_id[e["EVT_ID"]] = e

    out = []
    for d in dtts:
        ev = events_by_id.get(d["EVT_ID"])
        if not ev:
            continue
        desc = (ev.get("EVT_desc") or {}).get("rendered", "")
        out.append({
            "source_id": src["id"], "source": src["name"],
            "title": html.unescape(ev.get("EVT_name", "")).strip(),
            "description": strip_html(desc)[:400],
            "start": d["DTT_EVT_start"], "end": d.get("DTT_EVT_end") or d["DTT_EVT_start"],
            "utc": False, "venue": src.get("default_venue", src["name"]),
            "cost": "Paid", "url": ev.get("link"), "image": None,
        })
    return out


def parse_mcmaster_cards(src, start, end):
    """Custom WordPress 'filtered items' card grid (McMaster Brighter World theme).

    No REST endpoint - everything renders into one HTML page with a hidden
    'Show more' button, so a single fetch already has the full listing."""
    page = fetch_text(src["url"])
    out = []
    for chunk in page.split("<div class='col-xl-3 col-lg-6'>")[1:]:
        m = re.search(r"<a href='([^']+)' id='post-\d+-\d+'>([^<]*)</a>", chunk)
        if not m:
            continue
        url, title = m.group(1), html.unescape(m.group(2)).strip()
        tm = re.search(r"<time>([^<]+)</time>", chunk)
        if not tm:
            continue
        sd, ed = _parse_mcmaster_date(html.unescape(tm.group(1)))
        if not sd:
            continue
        dm = re.search(r"<p class='card-text'>(.*?)</p>", chunk, re.S)
        out.append({
            "source_id": src["id"], "source": src["name"],
            "title": title, "description": strip_html(dm.group(1)) if dm else "",
            "start": sd.strftime("%Y-%m-%d %H:%M:%S"),
            "end": (ed or sd).strftime("%Y-%m-%d %H:%M:%S"),
            "utc": False, "venue": "McMaster University", "cost": "Free",
            "url": url, "image": None,
        })
    return out


def parse_mohawk_drupal(src, start, end):
    """Drupal Views 'event-list' teasers, paginated with ?page=N (0-indexed)."""
    out = []
    for page_no in range(0, 8):
        sep = "&" if "?" in src["url"] else "?"
        page = fetch_text(src["url"] + sep + "page=%d" % page_no)
        articles = page.split("<article ")[1:]
        if not articles:
            break
        for chunk in articles:
            m = re.search(r"<h2><a href='?\"?([^'\"]+)['\"] hreflang=\"en\">([^<]*)</a></h2>", chunk)
            if not m:
                m = re.search(r'<h2><a href="([^"]+)" hreflang="en">([^<]*)</a></h2>', chunk)
            if not m:
                continue
            url = urllib.parse.urljoin(src["url"], m.group(1))
            title = html.unescape(m.group(2)).strip()
            sm = re.search(r"field-event-start-time[^>]*>\s*<time datetime=\"([^\"]+)\"", chunk)
            if not sm:
                continue
            em = re.search(r"field-event-end-time[^>]*>\s*<time datetime=\"([^\"]+)\"", chunk)
            im = re.search(r"<img loading=\"lazy\" src=\"([^\"]+)\"", chunk)
            desc = re.search(r"field--name-body[^>]*><p>(.*?)</p>", chunk, re.S)
            out.append({
                "source_id": src["id"], "source": src["name"],
                "title": title, "description": strip_html(desc.group(1)) if desc else "",
                "start": sm.group(1), "end": em.group(1) if em else sm.group(1),
                # The Z on these timestamps is a lie: Drupal renders the LOCAL
                # time into the attribute and appends Z anyway. The visible text
                # proves it -- datetime="2026-08-17T09:00:00Z" displays as
                # "9:00 am". Treating it as real UTC shifted everything 4h early.
                "utc": False, "venue": "Mohawk College", "cost": "Free",
                "url": url,
                "image": urllib.parse.urljoin(src["url"], im.group(1)) if im else None,
            })
        if len(articles) < 8:      # short page - last one
            break
    return out


def parse_ymca_drupal(src, start, end):
    """Open Y (YMCA's shared Drupal distro) events listing - server-rendered
    Views teasers, same family as parse_mohawk_drupal but different markup.
    JSON:API is enabled site-wide but the 'event' bundles it exposes
    (node--event, node--lb_event) don't carry the real listing - the actual
    data only exists in the rendered HTML at /events."""
    out = []
    for page_no in range(0, 6):
        sep = "&" if "?" in src["url"] else "?"
        page = fetch_text(src["url"] + sep + "page=%d" % page_no)
        rows = page.split('<div class="views-row">')[1:]
        if not rows:
            break
        for chunk in rows:
            m = re.search(r'<h3\s*>\s*<a href="([^"]+)"[^>]*>\s*<span>([^<]*)</span>', chunk)
            if not m:
                continue
            url = urllib.parse.urljoin(src["url"], m.group(1))
            title = html.unescape(m.group(2)).strip()
            times = re.findall(r'<time datetime="([^"]+)"', chunk)
            if not times:
                continue
            venue_m = re.search(r'teaser-event-location">.*?</span>\s*([^<]+?)\s*</div>', chunk, re.S)
            desc_m = re.search(r'class="body field-item">\s*(.*?)\s*</div>', chunk, re.S)
            img_m = re.search(r'<img [^>]*src="([^"]+)"', chunk)
            out.append({
                "source_id": src["id"], "source": src["name"],
                "title": title, "description": strip_html(desc_m.group(1)) if desc_m else "",
                "start": times[0], "end": times[1] if len(times) > 1 else times[0],
                # datetime attr already carries a real -04:00 offset (unlike
                # Mohawk's mislabeled Z) - to_dt truncates to the first 19
                # chars, which is already correct local wall time.
                "utc": False,
                "venue": html.unescape(venue_m.group(1)).strip() if venue_m else src["name"],
                "cost": "Free", "url": url,
                "image": urllib.parse.urljoin(src["url"], img_m.group(1)) if img_m else None,
            })
        if len(rows) < 8:          # short page - last one
            break
    return out


def parse_queensu_events(src, start, end):
    """Queen's University's campus-wide events calendar. Was a Drupal 7
    server-rendered month-grid (kind queensu_drupal) until it migrated to a
    React SPA sometime around 2026-08-26 - the SPA bundle exposes a clean
    JSON API instead (found by grepping the bundle's .js for "/api/"),
    which is strictly better than the old markup scrape: real venue and
    end-time fields, and the whole window in one request instead of a
    per-month walk."""
    # This is the university's ENTIRE campus calendar - most of it is
    # internal HR/staff programming (wellness classes, EFAP sessions,
    # TA-training series) that repeats under a dozen tags each and would
    # otherwise flood the practical stream at the MIN_SCORE floor. Drop it
    # by title marker; these are Queen's own recurring internal-program
    # brand names, not one-off titles, so the list is short and stable.
    SKIP_TITLE_MARKERS = ("thrive 365", "efap", "(ewpb)", "test event",
                           "teaching development series:", "on the agenda:",
                           "feedbackfruits", "course design series")
    base = src["url"].rstrip("/")
    q = urllib.parse.urlencode({"start": start.isoformat(), "end": end.isoformat()})
    data = fetch(base + "/api/events?" + q)
    out = []
    for e in data.get("data", []):
        title = html.unescape(e.get("title") or "").strip()
        if not title or any(marker in title.lower() for marker in SKIP_TITLE_MARKERS):
            continue
        loc = e.get("location") or {}
        venue = loc.get("name") or e.get("otherLocation") or "Queen's University"
        sdt, edt = e.get("startDatetime"), e.get("endDatetime")
        if not sdt:
            continue
        out.append({
            "source_id": src["id"], "source": src["name"],
            "title": title, "description": "",
            "start": sdt, "end": edt or sdt,
            # startDatetime/endDatetime already carry a real -04:00/-05:00
            # offset (not a lying Z) - to_dt truncates to the first 19
            # chars, which is already correct local wall time.
            "utc": False, "venue": venue, "cost": "Free",
            "url": src.get("home", base).rstrip("/") + "/calendar/events/" + e.get("slug", ""),
            "image": None,
        })
    return out


def parse_tourism_hamilton(src, start, end):
    """Custom WordPress theme with no REST route for its 'event' post type
    (not even listed in /wp-json/wp/v2/types), but Yoast's auto-generated
    /event-sitemap.xml enumerates every event page, and each page has plain
    regex-able info__date / info__description markup."""
    base = src["url"].rstrip("/")
    sitemap = fetch_text(base + "/event-sitemap.xml", accept="application/xml,text/xml,*/*")
    urls = re.findall(r"<loc>([^<]+)</loc>", sitemap)
    out = []
    for u in urls:
        try:
            page = fetch_text(u)
        except urllib.error.HTTPError:
            continue
        title_m = re.search(r'banner__title-content[^>]*>([^<]+)', page)
        date_m = re.search(r'class="info__date">\s*([^<]+?)\s*<', page)
        if not title_m or not date_m:
            continue
        sd, ed = _parse_mcmaster_date(html.unescape(date_m.group(1)).strip())
        if not sd:
            continue
        desc_idx = page.find('class="info__description"')
        description = strip_html(page[desc_idx:desc_idx + 3000])[:400] if desc_idx != -1 else ""
        img_m = re.search(r'class="event_image"[^>]*data-lazy-src="([^"]+)"', page)
        out.append({
            "source_id": src["id"], "source": src["name"],
            "title": html.unescape(title_m.group(1)).strip(), "description": description,
            "start": sd.strftime("%Y-%m-%d %H:%M:%S"),
            "end": (ed or sd).strftime("%Y-%m-%d %H:%M:%S"),
            "utc": False, "venue": "Hamilton", "cost": "Free",
            "url": u, "image": img_m.group(1) if img_m else None,
        })
    return out


def _unfold_ics(text):
    """RFC 5545 line unfolding: a line starting with a space/tab continues
    the previous line."""
    lines = text.replace("\r\n", "\n").split("\n")
    out = []
    for line in lines:
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _ics_unescape(s):
    return (s.replace("\\n", "\n").replace("\\N", "\n")
             .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\"))


def _ics_datetime(raw):
    """'20260819T130000Z' / '20260819T090000' / '20260819' -> ('Y-m-d H:M:S', is_utc)."""
    raw = raw.strip()
    is_utc = raw.endswith("Z")
    raw = raw.rstrip("Z")
    fmt = "%Y%m%dT%H%M%S" if "T" in raw else "%Y%m%d"
    d = dt.datetime.strptime(raw, fmt)
    return d.strftime("%Y-%m-%d %H:%M:%S"), is_utc


def parse_ical(src, start, end):
    """Generic .ics / webcal feed - unfolds lines and walks VEVENT blocks.
    Works for any calendar exposing a plain iCalendar export (church ChMS
    'subscribe' links, etc.) instead of a bespoke JSON API - no external
    icalendar library needed, the format is simple enough for stdlib."""
    text = fetch_text(src["url"], accept="text/calendar, */*")
    out, cur = [], None
    for line in _unfold_ics(text):
        if line.startswith("BEGIN:VEVENT"):
            cur = {}
        elif line.startswith("END:VEVENT"):
            if cur and cur.get("DTSTART") and cur.get("SUMMARY"):
                sd, sd_utc = _ics_datetime(cur["DTSTART"])
                ed, _ = _ics_datetime(cur["DTEND"]) if cur.get("DTEND") else (sd, sd_utc)
                out.append({
                    "source_id": src["id"], "source": src["name"],
                    "title": _ics_unescape(cur["SUMMARY"]),
                    "description": _ics_unescape(cur.get("DESCRIPTION", ""))[:400],
                    "start": sd, "end": ed, "utc": sd_utc,
                    "venue": _ics_unescape(cur.get("LOCATION", "")) or src.get("default_venue", src["name"]),
                    "cost": "Free", "url": cur.get("URL") or src.get("home"), "image": None,
                })
            cur = None
        elif cur is not None and ":" in line:
            key, val = line.split(":", 1)
            name = key.split(";")[0].strip().upper()
            if name in ("DTSTART", "DTEND", "SUMMARY", "DESCRIPTION", "LOCATION", "URL"):
                cur[name] = val
    return out


def parse_timely(src, start, end):
    """Time.ly (time.ly / timely.fun) hosted SaaS calendar - used by tourism
    boards and municipalities. The JSON events API is auth-walled (403 "not
    authorized" to anything but the calendar's own first-render XHR), but the
    `export?format=ics` endpoint is public and returns a plain, wide-window
    iCalendar feed.

    `src["url"]` is the public calendar page (a `calendar.time.ly/<slug>/...`
    URL, or the org's own page that embeds the widget). The numeric calendar
    id it needs is not the slug - it's pulled from the page (the widget's
    asset URLs carry it: `calendar.time.ly/images/<id>/` and
    `/api/calendars/<id>/`). Set `src["timely_id"]` to skip that lookup."""
    cal_id = str(src.get("timely_id") or "")
    if not cal_id:
        page = fetch_text(src["url"])
        m = (re.search(r"calendar\.time\.ly/images/(\d+)/", page)
             or re.search(r"/api/calendars/(\d+)[/?]", page))
        if not m:
            return []
        cal_id = m.group(1)

    ics_url = ("https://calendar.time.ly/api/calendars/%s/export?format=ics"
               "&start_date_utc=%d&end_date_utc=%d"
               % (cal_id,
                  int(dt.datetime(start.year, start.month, start.day).timestamp()),
                  int(dt.datetime(end.year, end.month, end.day).timestamp()) + 86399))
    text = fetch_text(ics_url, accept="text/calendar, */*")

    out, cur = [], None
    for line in _unfold_ics(text):
        if line.startswith("BEGIN:VEVENT"):
            cur = {}
        elif line.startswith("END:VEVENT"):
            if cur and cur.get("DTSTART") and cur.get("SUMMARY"):
                sd, sd_utc = _ics_datetime(cur["DTSTART"])
                ed, _ = _ics_datetime(cur["DTEND"]) if cur.get("DTEND") else (sd, sd_utc)
                if start <= dt.date.fromisoformat(sd[:10]) <= end:
                    out.append({
                        "source_id": src["id"], "source": src["name"],
                        "title": _ics_unescape(cur["SUMMARY"]),
                        "description": strip_html(_ics_unescape(cur.get("DESCRIPTION", "")))[:400],
                        "start": sd, "end": ed, "utc": sd_utc,
                        "venue": strip_html(_ics_unescape(cur.get("LOCATION", "")))
                                 or src.get("default_venue", src["name"]),
                        # Time.ly's X-COST-TYPE is unreliable on a general
                        # tourism calendar (Discover Sudbury has all 729 events
                        # marked "free", incl. ticketed concerts / hockey /
                        # symphony) - so default "Paid" like the other
                        # aggregators and let TICKETED_KINDS cap the flood.
                        "cost": "Paid", "url": cur.get("URL") or src.get("home"), "image": None,
                    })
            cur = None
        elif cur is not None and ":" in line:
            key, val = line.split(":", 1)
            name = key.split(";")[0].strip().upper()
            if name in ("DTSTART", "DTEND", "SUMMARY", "DESCRIPTION", "LOCATION", "URL"):
                cur[name] = val
    return out


# Engage "theme" -> our stream. Only the unambiguous ones force a category;
# everything else falls through to the keyword classifier + source default.
_ENGAGE_THEME_CAT = {
    "Spirituality": "spiritual",
    "ThoughtfulLearning": "practical",
    "GroupBusiness": "practical",
    "Social": "social",
    "Cultural": "social",
    "Athletics": "social",
}


def parse_campuslabs_engage(src, start, end):
    """Modern Campus Engage (formerly CampusLabs / "Anthology Engage") - the
    student-involvement platform behind hundreds of North-American campuses.
    Schools front it on a vanity domain (NAIT = ookslife.ca) but the
    discovery API is same-origin and unauthenticated:

        <base>/api/discovery/event/search?endsAfter=<ISO-Z>
               &orderByField=endsOn&orderByDirection=ascending
               &status=Approved&take=100&skip=<n>

    Results are already scoped to the one institution (the vanity host maps to
    one Engage community), so no per-school filter is needed - but a general
    student-life feed is noisy, so `src["org_filter"]` (optional, case-
    insensitive regex) keeps only events whose organisation *or* title matches
    it (NAIT: "immigration|international|intercultural|newcomer").

    `startsOn` / `endsOn` come back as UTC with a `+00:00`/`Z` suffix; the
    pipeline's `to_dt(utc=True)` only knows the Toronto offset, so we localise
    here instead using `src["tz_offset"]` (hours, e.g. -6 for Edmonton) and
    emit naive local time."""
    base = src["url"].rstrip("/")
    keep = re.compile(src["org_filter"], re.I) if src.get("org_filter") else None
    off = dt.timedelta(hours=src.get("tz_offset", 0))
    ends_after = dt.datetime(start.year, start.month, start.day).strftime("%Y-%m-%dT%H:%M:%SZ")
    out, skip = [], 0
    for _ in range(12):                     # <=1200 events; plenty for a 3-month window
        q = urllib.parse.urlencode({
            "endsAfter": ends_after, "orderByField": "endsOn",
            "orderByDirection": "ascending", "status": "Approved",
            "take": 100, "skip": skip,
        })
        data = fetch(base + "/api/discovery/event/search?" + q)
        rows = data.get("value") or []
        if not rows:
            break
        for e in rows:
            raw_start = (e.get("startsOn") or "")[:19]
            if not raw_start:
                continue
            try:
                sd = dt.datetime.strptime(raw_start, "%Y-%m-%dT%H:%M:%S") + off
            except ValueError:
                continue
            if sd.date() > end:
                continue                    # sorted by endsOn, not startsOn - keep scanning
            org = e.get("organizationName") or ""
            if keep and not (keep.search(org) or keep.search(e.get("name") or "")):
                continue
            ed_raw = (e.get("endsOn") or "")[:19]
            try:
                ed = dt.datetime.strptime(ed_raw, "%Y-%m-%dT%H:%M:%S") + off
            except ValueError:
                ed = sd
            loc = (e.get("location") or "").strip()
            img = e.get("imagePath")
            out.append({
                "source_id": src["id"], "source": src["name"],
                "title": (e.get("name") or "").strip(),
                "description": strip_html(e.get("description", ""))[:400],
                "start": sd.strftime("%Y-%m-%d %H:%M:%S"),
                "end": ed.strftime("%Y-%m-%d %H:%M:%S"), "utc": False,
                "venue": (src.get("default_venue", src["name"]) if loc.lower() == "online"
                          else loc or src.get("default_venue", src["name"])),
                "cost": "Free",
                "forced_category": _ENGAGE_THEME_CAT.get(e.get("theme")),
                "url": "%s/event/%s" % (base, e.get("id")),
                "image": ("https://se-images.campuslabs.ca/clink/images/%s?preset=med-w" % img
                          if img else None),
            })
        if len(rows) < 100:
            break
        skip += 100
    return out


def parse_supabase_events(src, start, end):
    """A community-events site built on Supabase, read straight off its public
    PostgREST API at `<project>.supabase.co/rest/v1/<table>`. The anon key is
    *meant* to be public - it ships in the site's own JS bundle and Supabase
    gates data with row-level security, not key secrecy - so committing it in
    `src["api_key"]` is fine (note it in the source's notes anyway).

    Shaped for a HalifaxEvents.ca-style `events` table: `start_date`/`end_date`
    (timestamptz), `title`, `short_description`/`description`, `venue_name`,
    `venue_address`, `is_free` (bool), `price_info`, `external_url`,
    `image_url` (a bare storage path), `status`. Overrides: `src["table"]`,
    `src["image_bucket"]` (prefixes `image_url` with the public storage URL).

    These aggregators scrape Eventbrite etc. and their event *times* are
    unreliable (a bar's music-bingo night stamped 02:00), so only the date is
    trusted - start time defaults to midnight. Cost is the `is_free` boolean;
    paid events then lean on `MIN_SCORE` rather than a blanket exemption, since
    a general what's-on feed is mostly concerts, not newcomer programming."""
    key = src["api_key"]
    base = src["url"].rstrip("/")
    q = urllib.parse.urlencode({
        "select": "title,short_description,description,venue_name,venue_address,"
                  "start_date,end_date,is_free,price_info,external_url,image_url,status,slug",
        "start_date": "gte." + start.isoformat(),
        "order": "start_date.asc",
    }) + "&start_date=lte." + end.isoformat()
    req = urllib.request.Request(
        "%s/rest/v1/%s?%s" % (base, src.get("table", "events"), q),
        headers={"User-Agent": UA, "Accept": "application/json",
                 "apikey": key, "Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=25) as r:
        rows = json.loads(r.read().decode("utf-8"))
    bucket = src.get("image_bucket")
    out = []
    for e in rows:
        if e.get("status") not in (None, "approved", "published", "active", "live"):
            continue
        raw = (e.get("start_date") or "")[:10]
        if not raw:
            continue
        loc = ", ".join(x for x in (e.get("venue_name"), e.get("venue_address")) if x)
        img = e.get("image_url")
        if img and bucket and not img.startswith("http"):
            img = "%s/storage/v1/object/public/%s/%s" % (base, bucket, img)
        out.append({
            "source_id": src["id"], "source": src["name"],
            "title": (e.get("title") or "").strip(),
            "description": strip_html(e.get("short_description") or e.get("description") or "")[:400],
            "start": raw + " 00:00:00",
            "end": ((e.get("end_date") or "")[:10] or raw) + " 00:00:00",
            "utc": False,
            "venue": loc or src.get("default_venue", src["name"]),
            "cost": "Free" if e.get("is_free") else "Paid",
            "url": e.get("external_url") or src.get("home"),
            "image": img if (img and img.startswith("http")) else None,
        })
    return out


def parse_libcal(src, start, end):
    """Springshare LibCal public 'list' ajax feed - no API key needed, it's
    the same same-origin endpoint the library's own JS calendar widget calls.
    `src["url"]` is the LibCal base (e.g. 'https://beinspired.libcal.com'),
    `src["cal_id"]` the numeric calendar id (the `?c=` on the calendar page).

    CORRECTED 2026-09-09: `date=` scopes results to events occurring ON that
    single day (multi-day events included via overlap) - it is NOT a month
    or range parameter, and the `monthly=true` flag changes nothing about the
    result set despite its name (verified empirically: identical `date`
    returns identical results with or without it, across two different
    LibCal tenants). The original version of this parser anchored on the 1st
    of each month and believed one call covered that whole month - it
    actually only ever saw whatever happened to be active on the 1st, a
    severe undercount. One call per day in the window is the only correct
    way to cover it; `perpage` is generously sized in case several branches
    have concurrent events on the same day."""
    # The console's copy of a source replaces the committed one wholesale, so
    # don't assume `cal_id` survived the round trip: fall back to a `?c=` on
    # the url (the calendar page's own address carries it).
    cal_id = src.get("cal_id") or (urllib.parse.parse_qs(
        urllib.parse.urlsplit(src["url"]).query).get("c") or [None])[0]
    if not cal_id:
        raise ValueError("libcal source has no cal_id (and no ?c= on its url)")
    base = src["url"].split("?")[0].rstrip("/")
    out, seen = [], set()
    day = start
    while day <= end:
        url = (base + "/ajax/calendar/list?" + urllib.parse.urlencode({
            "c": cal_id, "date": day.strftime("%Y-%m-%d"), "perpage": 50,
        }))
        data = fetch(url)
        for e in data.get("results") or []:
            key = e.get("id") or e.get("url") or (e.get("title"), e.get("startdt"))
            if key in seen or not e.get("startdt"):
                continue
            seen.add(key)
            out.append({
                "source_id": src["id"], "source": src["name"],
                "title": html.unescape(e.get("title", "")).strip(),
                "description": strip_html(e.get("shortdesc") or "")[:400],
                "start": e.get("startdt"), "end": e.get("enddt") or e.get("startdt"),
                "utc": False, "venue": e.get("location") or src.get("default_venue", src["name"]),
                "cost": "Free", "url": e.get("url"), "image": e.get("featured_image"),
            })
        day += dt.timedelta(days=1)
    return out


def parse_carleton_events(src, start, end):
    """Carleton University's custom Gutenberg/ACF event post type (cu_event) -
    exposed like any other WordPress post type at /wp-json/wp/v2/cu_event, a
    real REST API, just not the Tribe/Events-Calendar one this pipeline
    otherwise looks for. `src["url"]` is the department's own multisite
    wp-json collection URL, e.g.
    'https://carleton.ca/go-isso/wp-json/wp/v2/cu_event'. Cost, building and
    room come from ACF fields most sources don't have at all."""
    out = []
    for page_no in range(1, 8):
        page = fetch(src["url"] + "?per_page=100&page=%d" % page_no)
        if not page:
            break
        for e in page:
            acf = e.get("acf") or {}
            sd = acf.get("cu_event_start_date")
            if not sd:
                continue
            venue = ", ".join(x for x in [acf.get("cu_building"), acf.get("cu_event_meeting_room")] if x) \
                or src.get("default_venue", src["name"])
            out.append({
                "source_id": src["id"], "source": src["name"],
                "title": html.unescape((e.get("title") or {}).get("rendered", "")).strip(),
                "description": strip_html((e.get("content") or {}).get("rendered", ""))[:400],
                "start": sd, "end": acf.get("cu_event_end_date") or sd,
                "utc": False, "venue": venue,
                "cost": (acf.get("cu_event_cost") or "").strip() or "Free",
                "url": e.get("link"), "image": None,
            })
        if len(page) < 100:
            break
    return out


def parse_ottawa_tourism(src, start, end):
    """Drupal Views infinite-scroll card grid (same family as ymca_drupal /
    mohawk_drupal, different markup) - ?page=N, 18 cards/page. No time-of-day
    or cost is shown on the card, only a date - events land at midnight
    local, and cost defaults to 'Paid' (not 'Free') since this aggregates a
    real mix of ticketed and free events and an empty/free default would
    give ticketed shows a false free-cost credit (same reasoning as the
    ticketmaster parser)."""
    out = []
    for page_no in range(0, 40):
        sep = "&" if "?" in src["url"] else "?"
        page = fetch_text(src["url"] + sep + "page=%d" % page_no)
        cards = page.split('<div class="card h-100 pb-5"')[1:]
        if not cards:
            break
        for chunk in cards:
            m = re.search(r'<h5 class="card-title"><a href="([^"]+)">([^<]*)<a?', chunk)
            dm = re.search(r'next-date">.*?</i>&nbsp;&nbsp;([^<]+)<', chunk, re.S)
            if not m or not dm:
                continue
            try:
                sd = dt.datetime.strptime(dm.group(1).strip(), "%B %d, %Y")
            except ValueError:
                continue
            desc = re.search(r'field--name-body[^"]*field__item">\s*(.*?)\s*</div>', chunk, re.S)
            out.append({
                "source_id": src["id"], "source": src["name"],
                "title": html.unescape(m.group(2)).strip(),
                "description": strip_html(desc.group(1)) if desc else "",
                "start": sd.strftime("%Y-%m-%d %H:%M:%S"), "end": sd.strftime("%Y-%m-%d %H:%M:%S"),
                "utc": False, "venue": src.get("default_venue", "Ottawa"), "cost": "Paid",
                "url": urllib.parse.urljoin(src["home"], m.group(1)), "image": None,
            })
        if len(cards) < 18:
            break
    return out


def parse_mississauga_events(src, start, end):
    """City of Mississauga's custom Event Management System REST API
    (os-prod-api.mississauga.ca) - public, no key, found via the events-
    calendar page's own JS. Only the /Events/Featured/<n> endpoint is used;
    the plain /Events/Search endpoint exists but 500s on a bare GET and needs
    a POST body this pipeline hasn't reverse-engineered."""
    data = fetch(src["url"].rstrip("/") + "/Events/Featured/%d?groupId=%s" % (
        src.get("featured_count", 100), src.get("group_id", 4)))
    out = []
    for e in data:
        sd = e.get("startDate")
        if not sd:
            continue
        url = src.get("home", "").rstrip("/")
        if e.get("slug"):
            url += "/events/%s?eventdate=%s&schedule=%s" % (
                e["slug"], urllib.parse.quote(sd), e.get("scheduleId", ""))
        out.append({
            "source_id": src["id"], "source": src["name"],
            "title": html.unescape(e.get("name", "")).strip(),
            "description": strip_html(e.get("teaser") or e.get("subtitle") or "")[:400],
            "start": sd, "end": e.get("endDate") or sd,
            "utc": False, "venue": e.get("venueName") or src["name"],
            "cost": "Free", "url": url or None,
            "image": ("https://mississauga.ca/" + e["banner"]) if e.get("banner") else None,
        })
    return out


def parse_bibliocommons(src, start, end):
    """BiblioCommons v2 (a common public-library platform - Vancouver,
    Halifax, and others) - the events page embeds its full Redux state as
    SSR JSON in a <script data-iso-key="_0"> block (the LAST such block on
    the page carries the real search results; an earlier one is a lighter
    render pass). `src["url"]` is the library's own <slug>.bibliocommons.com
    base - found via a `https://<slug>.bibliocommons.com` link on the
    library's own (often WordPress) marketing site when the library's public
    domain isn't the bibliocommons.com host itself.

    No working date-range filter was found - a guessed `date_range=` query
    param is silently ignored (confirmed empirically: results are always
    global soonest-first regardless of the param). Paginates chronologically
    from page 1 and stops once a page's events are entirely past `end` - a
    library system can have 100+ pages total (thousands of events across
    every branch), but only a handful are needed for a 3-4 month window
    since results are date-sorted. Some library systems disable the Events
    feature entirely (a 403 page saying so) - that is a per-library
    configuration gap, not a platform or parser problem."""
    out = []
    for page_no in range(1, 60):
        page = fetch_text(src["url"].rstrip("/") + "/v2/events" + ("?page=%d" % page_no if page_no > 1 else ""))
        idx = page.rfind('data-iso-key="_0"')
        if idx == -1:
            break
        s = page.index(">", idx) + 1
        e = page.index("</script>", s)
        data = json.loads(page[s:e])
        entities = data.get("entities") or {}
        events, locations, audiences = (entities.get("events") or {}), (entities.get("locations") or {}), (entities.get("eventAudiences") or {})
        if not events:
            break
        all_past_end = True
        for ev in events.values():
            d = ev.get("definition") or {}
            sd_raw = d.get("start")
            if not sd_raw:
                continue
            try:
                sd_date = dt.datetime.strptime(sd_raw[:10], "%Y-%m-%d").date()
            except ValueError:
                continue
            if sd_date <= end:
                all_past_end = False
            if not (start <= sd_date <= end):
                continue
            # Structured audience names beat guessing from the title -
            # same reasoning as Communico's `ages` field.
            aud_names = [(audiences.get(a) or {}).get("name", "") for a in d.get("audienceIds") or []]
            if any(re.search(r"kid|preschool|baby|toddler|\bchild", a, re.I) for a in aud_names):
                continue
            branch = (locations.get(d.get("branchLocationId")) or {}).get("name")
            room = d.get("locationDetails")
            venue = ", ".join(x for x in [branch, room] if x) or src.get("default_venue", src["name"])
            out.append({
                "source_id": src["id"], "source": src["name"],
                "title": html.unescape(d.get("title", "")).strip(),
                "description": strip_html(d.get("description", ""))[:400],
                "start": sd_raw, "end": d.get("end") or sd_raw,
                "utc": False, "venue": venue, "cost": "Free",
                "url": src.get("home", src["url"]), "image": None,
            })
        if all_past_end:
            break
    return out


def parse_okanagan_events(src, start, end):
    """Okanagan College's Drupal JSON:API 'events' content type - a normal
    /jsonapi/node/events collection (real dates in field_event_date_range,
    venue in field_event_location_description). `src["url"]` is the full
    jsonapi query URL, already BETWEEN-filtered to upcoming events; follows
    `links.next.href` for pagination, the standard JSON:API mechanism."""
    out = []
    url = src["url"]
    for _ in range(10):
        data = fetch(url)
        for item in data.get("data") or []:
            attrs = item.get("attributes") or {}
            date_range = (attrs.get("field_event_date_range") or [None])[0]
            if not date_range or not date_range.get("value"):
                continue
            out.append({
                "source_id": src["id"], "source": src["name"],
                "title": html.unescape(attrs.get("title", "")).strip(),
                "description": strip_html((attrs.get("body") or {}).get("value", ""))[:400],
                "start": date_range["value"], "end": date_range.get("end_value") or date_range["value"],
                "utc": False,
                "venue": attrs.get("field_event_location_description") or src.get("default_venue", src["name"]),
                "cost": "Free",
                "url": urllib.parse.urljoin(src["home"], (attrs.get("path") or {}).get("alias") or ""),
                "image": None,
            })
        next_link = ((data.get("links") or {}).get("next") or {}).get("href")
        if not next_link:
            break
        url = next_link
    return out


# City-hall governance items that GovStack calendars list alongside real
# community events - useless to newcomers/students and, because the parser
# defaults them to Free with no description, they otherwise sail past the
# scorer. Kingston's calendar is ~75% this; Kitchener/Thunder Bay have a
# few. Matched case-insensitively against the title.
_GOVSTACK_NOISE = re.compile(
    r'^(proclamation|flag[ -](rais|lower)|illuminat|lighting of|light up )'
    r'|\bcommittee\b|\bcity council\b|\bcouncil meeting\b|\bcommittee of the whole\b'
    r'|\bpolice servic\w* board\b|\bpublic (meeting|hearing|notice)\b'
    r'|\bbudget (meeting|deliberation)', re.I)


def _fetch_govstack_page(url, home, retries=3):
    """fetch_text with a fuller browser-shaped header set and short retries
    on 403 - see the "2026-10-27" note on parse_govstack_calendar for why.
    Only used here, not swapped into fetch_text globally, so a platform
    that's already working elsewhere can't be regressed by a header this
    particular vendor's bot protection happens to dislike."""
    extra = {
        "Referer": home,
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Dest": "document",
        "DNT": "1",
    }
    last_err = None
    for attempt in range(retries):
        try:
            return fetch_text(url, extra_headers=extra)
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code != 403 or attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))   # 2s, 4s - a soft challenge/rate
                                             # limit often clears by then
    raise last_err


def parse_govstack_calendar(src, start, end):
    """eSolutionsGroup/GovStack (Umbraco-based) municipal calendar platform -
    seen on Brantford's library/city/tourism calendars, Thunder Bay's
    city/tourism calendars, and Waterloo/Kitchener's city calendars, all the
    same vendor (same appId across tenants). Server-rendered month grid at
    <base>/default/Month?StartDate=MM/DD/YYYY; no separate API needed. Each
    event's own detail link encodes its start date+time in the URL itself
    (/default/Detail/YYYY-MM-DD-HHMM-slug). The title comes from the link's
    `aria-label` ("View <title> on <date> <time>"), not its inner HTML -
    tenants render the inside of the <a> differently (Brantford has the
    title as plain text; Thunder Bay nests a <div data-title="..."> instead
    with no direct text), but aria-label's wording is consistent across all
    of them. No venue/description on the grid itself and no per-event page
    fetched here (would be one extra request per event).

    Path casing also varies by tenant - Milton renders `/Default/Detail/`
    (capital D) where Brantford/Thunder Bay/Kitchener use lowercase - so the
    detail-link match is case-insensitive; a case-sensitive match silently
    returns 0 raw events on a tenant that capitalizes it, same failure shape
    as the aria-label-vs-inner-text trap above.

    2026-10-27: every tenant on this platform (Brantford library, Thunder
    Bay, Kitchener) started 403ing the same week, despite "confirmed
    working" on 2026-09-09 - this is the eSolutionsGroup/GovStack bot
    protection catching GitHub Actions' well-known runner IPs and/or
    urllib's bare TLS fingerprint, not a parser or URL regression (a plain
    browser-style fetch of the same URL still loads fine). There's no
    stdlib way to spoof a TLS fingerprint, so this can't be made bulletproof
    without a third-party dependency this project deliberately avoids - but
    a fuller browser-shaped header set (Referer, Sec-Fetch-*, DNT) plus a
    couple of short retries clears a soft rate-limit/challenge noticeably
    more often than the bare request does, and still reports a clean
    "blocked HTTP 403" instead of hanging or silently returning nothing if
    the IP itself is hard-blocked."""
    out, seen = [], set()
    cursor = start.replace(day=1)
    home = src["url"].rstrip("/") + "/"
    for _ in range(6):
        if cursor > end:
            break
        url = (src["url"].rstrip("/") + "/default/Month?StartDate=%02d/01/%d"
               % (cursor.month, cursor.year))
        page = _fetch_govstack_page(url, home)
        for m in re.finditer(
            r'href="(/default/Detail/(\d{4})-(\d{2})-(\d{2})-(\d{2})(\d{2})-[^"]+)"'
            r'\s+aria-label="View (.+?) on \w+ \d{1,2}, \d{4} \d{1,2}:\d{2}\s*[ap]m"', page,
            re.IGNORECASE):
            href, y, mo, d, hh, mi, title = m.groups()
            if href in seen:
                continue
            seen.add(href)
            title = html.unescape(title).strip()
            if _GOVSTACK_NOISE.search(title):
                continue
            sd = "%s-%s-%s %s:%s:00" % (y, mo, d, hh, mi)
            out.append({
                "source_id": src["id"], "source": src["name"],
                "title": title, "description": "",
                "start": sd, "end": sd, "utc": False,
                "venue": src.get("default_venue", src["name"]), "cost": "Free",
                "url": src["url"].rstrip("/") + href, "image": None,
            })
        # advance one calendar month
        cursor = (cursor.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return out


def parse_ticketmaster(src, start, end):
    """Ticketmaster discovery page - events ship pre-rendered in __NEXT_DATA__."""
    out = []
    for page_no in range(0, 6):
        sep = "&" if "?" in src["url"] else "?"
        page = fetch_text(src["url"] + sep + "page=%d" % page_no)
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
        if not m:
            break
        data = json.loads(m.group(1))
        queries = data["props"]["pageProps"]["initialReduxState"]["api"]["queries"]
        ce = next((q for k, q in queries.items() if k.startswith("cityEvents(")), None)
        events = (ce or {}).get("data", {}).get("events", [])
        if not events:
            break
        for e in events:
            sd = (e.get("dates") or {}).get("startDate")
            if not sd:
                continue
            venue = e.get("venue") or {}
            venue_name = ", ".join(
                x for x in [venue.get("name"), venue.get("addressLineOne"), venue.get("city")] if x
            ) or "Hamilton"
            img = None
            for a in e.get("artists") or []:
                urls = (a.get("imageUrls") or {})
                img = next(iter(urls.values()), None)
                if img:
                    break
            out.append({
                "source_id": src["id"], "source": src["name"],
                "title": e.get("title", "").strip(), "description": "",
                "start": sd, "end": sd, "utc": True,
                "venue": venue_name, "cost": "Paid",
                "url": e.get("url"), "image": img,
            })
        if len(events) < 20:       # short page - last one
            break
    return out



def parse_audienceview(src, start, end):
    """AudienceView box offices (e.g. tickets.brampton.ca for The Rose). The
    listing page embeds a `searchResults : [ [...], ... ]` JS array, 10-12
    performances per page; `?BOset::WScontent::SearchResultsInfo::current_page=N`
    pages through without a session token."""
    base = src["url"].split("?")[0]
    root = re.match(r"https?://[^/]+", base).group(0)
    out, seen = [], set()
    for page in range(1, int(src.get("max_pages") or 10) + 1):
        url = base if page == 1 else base + "?BOset::WScontent::SearchResultsInfo::current_page=%d" % page
        text = fetch_text(url)
        rows = []
        for m in re.finditer(r'^\s*(\[ "[0-9A-F-]{36}", "P", .*\])\s*,?\s*$', text, re.M):
            raw = m.group(1).replace("\\'", "'")
            try:
                rows.append(json.loads(raw))
            except ValueError:
                continue
        new = [r for r in rows if r[0] not in seen]
        if not new:
            break
        for r in new:
            seen.add(r[0])
            try:
                d = dt.date(int(r[11]), int(r[10]) + 1, int(r[9]))     # month is 0-based
            except (ValueError, IndexError):
                continue
            hhmm = r[8] if re.match(r"^\d{1,2}:\d{2}$", r[8] or "") else "00:00"
            venue = ""
            if "Canada" in r:
                c = r.index("Canada")
                name = (r[c + 5] if len(r) > c + 5 else "") or ""
                venue = ", ".join(x for x in [re.sub(r"\s+", " ", name).strip(), r[c - 4], r[c - 3]] if x)
            elif len(r) > 31 and isinstance(r[30], str) and r[30]:     # "Venues Offsite"
                venue = ", ".join(x for x in [r[30], r[31]] if isinstance(x, str) and x)
            prices = [x for x in r if isinstance(x, str) and re.match(r"^\$\d", x)]
            cost = "Free" if prices and all(re.match(r"^\$0(\.00)?$", x) for x in prices) else (
                "From %s" % prices[0] if prices else "See website")
            link = r[18] if len(r) > 18 else ""
            out.append({
                "source_id": src["id"], "source": src["name"],
                "title": strip_html(r[6]),
                "description": strip_html(" ".join(x for x in [r[5], r[2], r[17]] if isinstance(x, str)))[:400],
                "start": "%s %s" % (d.isoformat(), hhmm), "end": "%s %s" % (d.isoformat(), hhmm),
                "utc": False, "venue": venue or src.get("default_venue", ""), "cost": cost,
                "url": urllib.parse.urljoin(base, link) if link else base,
                "image": urllib.parse.urljoin(root + "/", r[20]) if len(r) > 20 and r[20] else None,
            })
        if out and dt.date.fromisoformat(out[-1]["start"][:10]) > end:
            break
    return out


def parse_imis_events(src, start, end):
    """iMIS cloud 'Events_List.aspx' (e.g. PAMA): a RadGrid of
    title link + 'When: Month D, YYYY' (+ optional 'Where:'). No times."""
    text = fetch_text(src["url"])
    out = []
    for block in re.split(r'class="SearchResultSummary"', text)[1:]:
        a = re.search(r'<a[^>]+href="([^"]*Event\.aspx\?EventKey=[^"]+)"[^>]*>\s*<span[^>]*>(.*?)</span>', block, re.S)
        w = re.search(r"When:\s*</strong>\s*([A-Za-z]+ \d{1,2}, \d{4})(?:\s*-\s*([A-Za-z]+ \d{1,2}, \d{4}))?", block)
        if not a or not w:
            continue
        try:
            sd = dt.datetime.strptime(w.group(1), "%B %d, %Y").date()
            ed = dt.datetime.strptime(w.group(2), "%B %d, %Y").date() if w.group(2) else sd
        except ValueError:
            continue
        where = re.search(r"Where:\s*</strong>\s*([^<]+)", block)
        img = re.search(r'<img[^>]+src="([^"]+)"', block)
        out.append({
            "source_id": src["id"], "source": src["name"],
            "title": strip_html(a.group(2)), "description": "",
            "start": sd.isoformat(), "end": ed.isoformat(), "utc": False,
            "venue": src.get("default_venue") or (where.group(1).strip() if where else ""),
            "cost": src.get("default_cost", "See website"),
            "url": urllib.parse.urljoin(src["url"], html.unescape(a.group(1))),
            "image": urllib.parse.urljoin(src["url"], html.unescape(img.group(1)).split("?")[0]) if img else None,
        })
    return out


_SXA_MON = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _sxa_date(s):
    m = re.match(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),\s*(\d{4})", s.strip())
    return dt.date(int(m.group(3)), _SXA_MON[m.group(1).lower()], int(m.group(2))) if m else None


def _sxa_time(s):
    m = re.match(r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?", s.strip(), re.I)
    if not m:
        return None
    h = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "p" else 0)
    return "%02d:%s" % (h, m.group(2) or "00")


def parse_sitecore_sxa(src, start, end):
    """Sitecore SXA search results JSON (e.g. Sheridan College /events):
    {"Results":[{"Url":..., "Html":"<h3 class=field-eventname>..."}]}."""
    data = fetch(src["url"])
    root = re.match(r"https?://[^/]+", src["url"]).group(0)
    out = []
    for r in data.get("Results", []):
        h = r.get("Html") or ""
        a = re.search(r'field-eventname[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', h, re.S)
        if not a:
            continue
        date_block = re.search(r"search-result-event__date(.*?)</h4>", h, re.S)
        dates = [x for x in (_sxa_date(strip_html(s)) for s in
                             re.findall(r"<span>([^<]*\d{4})</span>", date_block.group(1) if date_block else ""))
                 if x]
        if not dates:
            continue
        times = [x for x in (_sxa_time(strip_html(s)) for s in
                             re.findall(r"<span>([^<]*[ap]\.m\.)</span>", h)) if x]
        loc = re.search(r"search-result-event__location[^>]*>(.*?)</span></p>", h, re.S)
        venue = strip_html(loc.group(1)).replace("Location:", "").strip() if loc else ""
        sd, ed = dates[0], dates[-1]
        # A long-running "Jun - Oct" series: show it from today rather than drop it.
        if sd < start <= ed:
            sd = start
        t1 = times[0] if times else "00:00"
        t2 = times[1] if len(times) > 1 else t1
        out.append({
            "source_id": src["id"], "source": src["name"],
            "title": strip_html(a.group(2)), "description": "",
            "start": "%s %s" % (sd.isoformat(), t1), "end": "%s %s" % (ed.isoformat(), t2),
            "utc": False, "venue": venue or src.get("default_venue", ""),
            "cost": src.get("default_cost", "Free"),
            "url": urllib.parse.urljoin(root + "/", a.group(1)), "image": None,
        })
    return out



def parse_drupal_fullcalendar(src, start, end):
    """Drupal 'FullCalendar View' pages (e.g. QUIC at Queen's): every event is
    embedded in the page's drupal-settings-json as
    fullCalendarView[n].calendar_options (a JSON string) -> events[]."""
    text = fetch_text(src["url"])
    m = re.search(r'<script type="application/json" data-drupal-selector="drupal-settings-json">(.*?)</script>',
                  text, re.S)
    if not m:
        raise ValueError("no drupal-settings-json on page")
    settings = json.loads(m.group(1))
    views = settings.get("fullCalendarView") or []
    if isinstance(views, dict):
        views = list(views.values())
    out, seen = [], set()
    for v in views:
        opts = v.get("calendar_options") or {}
        if isinstance(opts, str):
            opts = json.loads(opts)
        for e in opts.get("events") or []:
            s = (e.get("start") or "")[:16].replace("T", " ")
            if not s:
                continue
            title = strip_html(e.get("title"))
            key = (title.lower(), s)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "source_id": src["id"], "source": src["name"],
                "title": title, "description": "",
                "start": s, "end": (e.get("end") or e["start"])[:16].replace("T", " "),
                "utc": False, "venue": src.get("default_venue", ""),
                "cost": src.get("default_cost", "Free"),
                "url": urllib.parse.urljoin(src["url"], e.get("url") or ""), "image": None,
            })
    return out


_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1)}


def _vk_dates(text, fallback_start):
    """'October 22 - November 8, 2026 • 7:30 pm' / 'October 1 - 31, 2026' /
    'October 20, 2026 • 4:30 pm' -> (start_date, end_date, 'HH:MM'|None)."""
    when, _, tm = text.partition("•")
    m = re.match(r"\s*([A-Za-z]+)\s+(\d{1,2})(?:\s*-\s*(?:([A-Za-z]+)\s+)?(\d{1,2}))?,\s*(\d{4})", when)
    sd = ed = None
    if m:
        mon1, d1, mon2, d2, y = m.groups()
        y = int(y)
        try:
            sd = dt.date(y, _MONTHS[mon1.lower()], int(d1))
            ed = dt.date(y, _MONTHS[(mon2 or mon1).lower()], int(d2)) if d2 else sd
            if ed < sd:                      # "December 28 - January 3, 2027"
                sd = sd.replace(year=y - 1)
        except (KeyError, ValueError):
            sd = ed = None
    sd = sd or fallback_start
    ed = ed or sd
    hhmm = _sxa_time(tm) if tm.strip() else None
    return sd, ed, hhmm


def parse_visit_kingston(src, start, end):
    """Visit Kingston (Craft CMS): the events page fetches its listing from
    POST /ajax/event-results with a CSRF token from the page, returning HTML
    cards. Only the 'Special events' section is used - 'Recurring events'
    carry no dates and 'Top attractions' aren't events."""
    page_url = src.get("home") or "https://www.visitkingston.ca/events/"
    jar = urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    opener = urllib.request.build_opener(jar)
    hdr = {"User-Agent": UA, "Accept-Language": "en-CA,en;q=0.9"}
    with opener.open(urllib.request.Request(page_url, headers=hdr), timeout=30) as r:
        page = r.read().decode("utf-8", "replace")
    name = re.search(r'window\.csrfTokenName\s*=\s*"([^"]+)"', page)
    value = re.search(r'window\.csrfTokenValue\s*=\s*"((?:[^"\\]|\\.)*)"', page)
    if not (name and value):
        raise ValueError("no CSRF token on events page")
    body = {"fromDate": start.isoformat(), "toDate": end.isoformat(), "category": "all",
            "search": "", "language": "en", name.group(1): json.loads('"%s"' % value.group(1))}
    req = urllib.request.Request(src["url"], data=json.dumps(body).encode(), method="POST",
                                 headers=dict(hdr, **{"Content-Type": "application/json",
                                                      "Accept": "application/json",
                                                      "Referer": page_url}))
    with opener.open(req, timeout=40) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    results = (data.get("data") or {}).get("results") or ""
    sections = re.split(r"<h2\b[^>]*>", results)
    special = "".join(s for s in sections if re.match(r"\s*Special events", s, re.I)) or results
    out, seen = [], set()
    for href, card in re.findall(r'<a class="group[^"]*"\s+href="([^"]+)"[^>]*>(.*?)</a>', special, re.S):
        title_m = re.search(r'<p class="typo-h4[^"]*"[^>]*>(.*?)</p>', card, re.S)
        if not title_m:
            continue
        title = strip_html(title_m.group(1))
        tattr = re.search(r'<time datetime="(\d{4}-\d{2}-\d{2})"', card)
        fallback = dt.date.fromisoformat(tattr.group(1)) if tattr else None
        info = re.search(r'<p class="font-body[^"]*"[^>]*>(.*?)</p>', card, re.S)
        parts = re.split(r"<br\s*/?>", info.group(1)) if info else [""]
        sd, ed, hhmm = _vk_dates(strip_html(parts[0]), fallback)
        if not sd:
            continue
        if sd < start <= ed:
            sd = start
        key = (title.lower(), sd)
        if key in seen:
            continue
        seen.add(key)
        cat = re.search(r'typo-smallcaps[^>]*>(.*?)</p>', card, re.S)
        img = re.search(r'<img[^>]+src="([^"]+)"', card)
        t = hhmm or "00:00"
        out.append({
            "source_id": src["id"], "source": src["name"],
            "title": title, "description": strip_html(cat.group(1)) if cat else "",
            "start": "%s %s" % (sd.isoformat(), t), "end": "%s %s" % (ed.isoformat(), t),
            "utc": False, "venue": strip_html(parts[1]) if len(parts) > 1 else "",
            "cost": "See website", "url": html.unescape(href),
            "image": html.unescape(img.group(1)) if img else None,
        })
    return out


_JET_FIELD = re.compile(r'jet-listing-dynamic-field__content"\s*>([^<]*)</div>')


def _jet_section(html_doc, heading, next_headings):
    """Slice the HTML between an Elementor <h2>/<h3> heading with this exact
    text and whichever of `next_headings` comes first after it (or the end of
    the doc), then pull every JetEngine dynamic-field value out of that slice.
    Looking up the stable `jet-listing-dynamic-field__content` class instead
    of the surrounding `elementor-element-<hash>` wrapper is what makes this
    survive the hash regenerating every time CCI re-saves the page in the
    editor (see the cci-ottawa notes in cities/ottawa.json)."""
    m = re.search(r'>%s<' % re.escape(heading), html_doc)
    if not m:
        return []
    rest = html_doc[m.end():]
    cut = len(rest)
    for nh in next_headings:
        m2 = re.search(r'>%s<' % re.escape(nh), rest)
        if m2:
            cut = min(cut, m2.start())
    return [v.strip().lstrip("-").strip() for v in _JET_FIELD.findall(rest[:cut])]


def _jet_discover_links(src):
    """Enumerate an Elementor+JetEngine 'events' custom post type's own
    permalinks, for any site. Tries the plain WP REST route for the post type
    first (`/wp-json/wp/v2/<rest_type>` off `src["home"]`, default rest_type
    "events" - confirmed working as-is on both cciottawa.ca and
    downtownkelowna.com, same custom-post-type-not-tribe pattern already
    documented for carleton_events) since it's the most reliable: no feed
    config to go wrong. Falls back to the archive's own `/feed/` RSS (what
    this parser originally used) for a site where the CPT isn't REST-exposed
    but the archive still emits one - NOTE this feed can be a trap: a site
    that doesn't make the post type's own archive feed is default-enabled
    will have WordPress silently serve the unrelated *comments* feed instead
    (title literally "Comments on: Events") with zero items - harmless here
    since zero links just means zero events, but worth knowing if this ever
    returns suspiciously 0 on a site you can see real events on in a
    browser."""
    root = (src.get("home") or src["url"]).rstrip("/")
    rest_type = src.get("rest_type", "events")
    try:
        posts = fetch(root + "/wp-json/wp/v2/%s?per_page=100" % rest_type)
        links = [p["link"] for p in posts if p.get("link")]
        if links:
            return links
    except (urllib.error.URLError, json.JSONDecodeError, KeyError):
        pass
    try:
        feed = fetch_text(src["url"].rstrip("/") + "/feed/",
                          accept="application/rss+xml, application/xml, text/xml")
    except (urllib.error.URLError, UnicodeDecodeError):
        return []
    return [html.unescape(u) for u in re.findall(r"<link>(.*?)</link>", feed)[1:]]


def parse_downtown_kelowna_events(src, start, end):
    """Downtown Kelowna BIA's 'events' CPT - same Elementor+JetEngine family
    as `jet_events` but a different template: no Date:/Time:/Venue headings
    to anchor on, just a flat ordered run of `jet-listing-dynamic-field__content`
    values on each event's own page. A MULTI-day event gets 6 values
    ([start_date, start_dow, end_date, end_dow, start_time, end_time]); a
    single-day event gets only 4 ([date, dow, start_time, end_time]) with no
    separate end-date field at all - 31 of this site's 40 events are the
    4-value single-day shape (checked 2026-10-21), so treating 6 as the only
    valid length silently dropped 78% of real events here. Dates are
    'Mon D, YYYY' (not the full-month 'Month D, YYYY' cci-ottawa uses). No
    venue or price field exists anywhere on these pages - this BIA page is a
    generic downtown-entertainment listing (touring musicals etc., often
    6-12 months out), not newcomer/student programming, so cost defaults to
    'Ticketed' and needs the same `TICKETED_KINDS` MIN_SCORE exemption as
    ottawa_tourism/ticketmaster or everything here scores zero on cost alone
    despite being a real, major show worth knowing about."""
    out = []
    for link in _jet_discover_links(src):
        try:
            page = fetch_text(link)
        except (urllib.error.URLError, UnicodeDecodeError):
            continue
        tm = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
        if not tm:
            continue
        title = html.unescape(strip_html(tm.group(1)))
        # The title itself is an <h1 class="jet-listing-dynamic-field__content">,
        # not a <div> - _JET_FIELD (which requires a </div> close) skips it,
        # so this list starts directly at the date fields, not the title.
        vals = [v.strip() for v in _JET_FIELD.findall(page[page.find("<body"):])]
        if len(vals) >= 6:
            sd_raw, _, ed_raw, _, st_raw, et_raw = vals[:6]
        elif len(vals) == 4:
            sd_raw, _, st_raw, et_raw = vals[:4]
            ed_raw = sd_raw
        else:
            continue
        try:
            sd = dt.datetime.strptime(sd_raw + " " + st_raw, "%b %d, %Y %I:%M %p")
            ed = dt.datetime.strptime(ed_raw + " " + et_raw, "%b %d, %Y %I:%M %p")
        except ValueError:
            continue
        if not (start <= sd.date() <= end):
            continue
        out.append({
            "source_id": src["id"], "source": src["name"],
            "title": title, "description": "",
            "start": sd.strftime("%Y-%m-%d %H:%M:%S"),
            "end": ed.strftime("%Y-%m-%d %H:%M:%S"),
            "utc": False, "venue": src.get("default_venue", src["name"]),
            "cost": "Ticketed", "url": link, "image": None,
        })
    return out


def parse_jet_events(src, start, end):
    """WordPress + Elementor + JetEngine 'dynamic listing' events (the pattern
    documented as a dead end for CCI Ottawa on 2026-09-09/09-10: per-element
    classes are `elementor-element-<hash>` and regenerate every time the page
    is re-saved, too fragile to scrape directly). Found 2026-10-21 while
    testing a Firecrawl headless render against it: Firecrawl/Jina's rendered
    markdown wasn't actually doing anything a plain stdlib GET couldn't -
    this org's events ARE server-rendered - the real fix is reading the
    stable `jet-listing-dynamic-field__content` class JetEngine always wraps
    the actual value in, regardless of the hash, plus the plain WordPress
    `/feed/` RSS the events archive exposes for free to enumerate event
    permalinks (WordPress default: last 10 posts/events, by publish date -
    covers this org's ~monthly batch-of-4 publishing cadence with room to
    spare; may miss an event published far ahead of a 10-item window on a
    busier site).

    `src["url"]` is the events archive (e.g. 'https://cciottawa.ca/events/');
    `/feed/` is appended to it for discovery, then each entry's own page is
    fetched for the real date/time/venue. One request per event found, so
    keep this off very high-volume calendars."""
    links = _jet_discover_links(src)
    out = []
    for link in links:
        try:
            page = fetch_text(link)
        except (urllib.error.URLError, UnicodeDecodeError):
            continue
        tm = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
        if not tm:
            continue
        title = html.unescape(strip_html(tm.group(1)))

        dates = _jet_section(page, "Date:", ("Time:",))
        times = _jet_section(page, "Time:", ("Event Category:", "Venue"))
        if not dates or not times:
            continue
        sd_raw, ed_raw = dates[0], dates[-1]
        st_raw, et_raw = times[0], times[-1]
        try:
            sd = dt.datetime.strptime(sd_raw + " " + st_raw, "%B %d, %Y %I:%M %p")
            ed = dt.datetime.strptime(ed_raw + " " + et_raw, "%B %d, %Y %I:%M %p")
        except ValueError:
            continue
        if not (start <= sd.date() <= end):
            continue

        room_m = re.search(r">Venue<.*?<h3[^>]*>([^<]*)</h3>", page, re.S)
        addr = _jet_section(page, "Venue", ("Organizer", "Share:"))
        venue = ", ".join(x for x in [
            html.unescape(room_m.group(1)).strip() if room_m else None,
            addr[-1] if addr else None,
        ] if x) or src.get("default_venue", src["name"])

        desc_chunk = page[tm.end():page.find(">Event Details<")]
        # A handful of CCI's own posts have a typo'd AM start time for what
        # is obviously a daytime workshop (e.g. "3:00 AM - 4:30 PM") - surface
        # it rather than silently "fixing" it, since only the org can confirm
        # which side of the typo is wrong.
        verify = ("Site's own page lists a %s start for this event - "
                  "confirm before drafting, may be a typo." % st_raw) \
                 if sd.hour < 6 else None
        out.append({
            "source_id": src["id"], "source": src["name"],
            "title": title,
            "description": strip_html(desc_chunk)[:400],
            "start": sd.strftime("%Y-%m-%d %H:%M:%S"),
            "end": ed.strftime("%Y-%m-%d %H:%M:%S"),
            "utc": False, "venue": venue,
            "cost": src.get("default_cost", "Free"),
            "url": link, "image": None, "verify": verify,
        })
    return out


PARSERS = {"tribe": parse_tribe, "sqs": parse_sqs, "manual": parse_manual,
           "mohawk_drupal": parse_mohawk_drupal, "ticketmaster": parse_ticketmaster,
           "communico": parse_communico, "opl": parse_opl, "probe": parse_probe,
           "mcmaster_cards": parse_mcmaster_cards, "event_espresso": parse_event_espresso,
           "ymca_drupal": parse_ymca_drupal, "tourism_hamilton": parse_tourism_hamilton,
           "ical": parse_ical, "queensu_events": parse_queensu_events,
           "libcal": parse_libcal, "mississauga_events": parse_mississauga_events,
           "carleton_events": parse_carleton_events, "ottawa_tourism": parse_ottawa_tourism,
           "bibliocommons": parse_bibliocommons, "okanagan_events": parse_okanagan_events,
           "govstack_calendar": parse_govstack_calendar, "sqs_rss": parse_sqs_rss,
           "timely": parse_timely, "campuslabs_engage": parse_campuslabs_engage,
           "supabase_events": parse_supabase_events, "audienceview": parse_audienceview,
           "imis_events": parse_imis_events, "sitecore_sxa": parse_sitecore_sxa,
           "drupal_fullcalendar": parse_drupal_fullcalendar, "visit_kingston": parse_visit_kingston,
           "jet_events": parse_jet_events, "downtown_kelowna_events": parse_downtown_kelowna_events}


# ---------------------------------------------------------------- assembly

def to_dt(raw, is_utc):
    raw = (raw or "").strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            d = dt.datetime.strptime(raw[:19], fmt)
            if is_utc:                      # ACCES reports GMT; Toronto is UTC-4 in season
                d -= dt.timedelta(hours=4)
            return d
        except ValueError:
            continue
    return None


def collect(sources, start, end, log):
    events, status = [], {}
    for src in sources:
        if not src.get("enabled", True):
            status[src["id"]] = ("off", "disabled in registry")
            continue
        parser = PARSERS.get(src.get("kind"))
        if not parser:
            status[src["id"]] = ("dead", "unknown feed kind")
            continue
        if src.get("kind") == "probe":
            status[src["id"]] = ("blocked", "registered, no feed parser yet")
            log("  %-34s no parser yet" % src["name"])
            # A probe source has no live feed, but may still carry hand-typed
            # manual_events keyed to its source_id - those must not be lost.
            raw = _manual_events_for(src, start, end)
            if not raw:
                continue
        else:
            try:
                # Hand-typed dates keyed to a crawled source (e.g. a welcome
                # week the feed doesn't carry) ride along with the feed.
                raw = list(parser(src, start, end))
                if src.get("kind") != "manual":
                    raw += _manual_events_for(src, start, end)
            except urllib.error.HTTPError as e:
                status[src["id"]] = ("blocked", "HTTP %s" % e.code)
                log("  %-34s BLOCKED (HTTP %s)" % (src["name"], e.code))
                continue
            except Exception as e:                      # noqa: BLE001 - fail soft per source
                # Name the exception AND its message: a bare "KeyError" in the
                # crawl log doesn't say which field went missing.
                why = ("%s: %s" % (type(e).__name__, e))[:120]
                status[src["id"]] = ("dead", why)
                log("  %-34s FAILED (%s)" % (src["name"], why))
                continue

        kept = 0
        # Optional per-source venue regex: lets a region-wide feed (e.g. an
        # agency serving all of Peel/Halton/Toronto) contribute only what is
        # in this city, plus anything online.
        vf = re.compile(src["venue_filter"], re.I) if src.get("venue_filter") else None
        # Optional per-source title regex to drop standing one-to-one services
        # (e.g. daily "Drop-in Advising") that would otherwise flood the board.
        tx = re.compile(src["title_exclude"], re.I) if src.get("title_exclude") else None
        for e in raw:
            if vf and not vf.search(e["title"] if e.get("no_venue") else (e.get("venue") or "")):
                continue
            if tx and tx.search(e.get("title") or ""):
                continue
            sd = to_dt(e["start"], e.get("utc"))
            ed = to_dt(e["end"], e.get("utc")) or sd
            if not sd or not (start <= sd.date() <= end):
                continue
            cat = e.get("forced_category") or classify(
                e["title"], e.get("description", ""), src.get("default_category", "practical"))
            e.update({
                "sd": sd, "ed": ed, "category": cat, "source_kind": src.get("kind"),
                # Ticketed feeds, plus any source flagged "capped" (e.g. a museum
                # whose list has no prices), skip MIN_SCORE but share the
                # best-5-per-4-weeks cap below.
                "capped_feed": src.get("kind") in TICKETED_KINDS
                               or bool(src.get("capped", src.get("kind") in CAPPED_BY_DEFAULT)),
                "image": e.get("image") or src.get("image"),
            })
            e["score"] = score(e)
            if src.get("kind") == "manual" or e.get("hand_entered"):
                # A human already vetted these, so keyword scoring doesn't get a
                # veto — Nuit Blanche reads as "art", not "newcomer", and would
                # otherwise be dropped despite being one of the best free nights
                # of the year for someone new to the city.
                e["score"] = max(e["score"], 72)
            # Ticketed shows never clear MIN_SCORE on cost alone (see `score`),
            # but students still go to paid concerts/festivals - they're capped
            # to the best few per rolling 4-week window below instead of being
            # vetted out here.
            if e["score"] < MIN_SCORE and not e["capped_feed"]:
                continue
            events.append(e)
            kept += 1
        status[src["id"]] = ("live", "%d in window" % kept)
        log("  %-34s ok  %3d raw  %3d kept" % (src["name"], len(raw), kept))

    # de-dupe on url, then on title+date
    seen, unique = set(), []
    for e in sorted(events, key=lambda x: -x["score"]):
        # Key on url+date, not url alone: multi-day festivals share one page,
        # and the pipeline wants a separate dated draft for each day.
        k1 = ((e.get("url") or "").rstrip("/").lower(), e["sd"].date())
        k2 = (e["title"].lower(), e["sd"].date())
        if k1 in seen or k2 in seen:
            continue
        seen.add(k1)
        seen.add(k2)
        unique.append(e)

    # Collapse standing programs. Community centres list the same drop-in every
    # weekday; without this one source buries every other source in the month.
    runs, capped = {}, []
    for e in sorted(unique, key=lambda x: (x["sd"], -x["score"])):
        k = (e["source_id"], e["title"].strip().lower(), e["sd"].strftime("%Y-%m"))
        runs[k] = runs.get(k, 0) + 1
        if runs[k] > MAX_REPEATS:
            continue
        # Nudge later occurrences down so a single repeating programme can't
        # own the whole top five — variety is the point of a curation board.
        e["score"] = max(1, e["score"] - 12 * (runs[k] - 1))
        e["repeats"] = 0
        capped.append(e)
    for e in capped:
        k = (e["source_id"], e["title"].strip().lower(), e["sd"].strftime("%Y-%m"))
        e["repeats"] = runs[k]

    # Ticketed sources (Ticketmaster, Ottawa Tourism) get their own cadence:
    # best 5 per rolling 4-week window, not per calendar month/category - so a
    # handful of billed shows students would genuinely go to still surface,
    # without their near-zero cost score letting them crowd out the free
    # practical/social feed.
    tm_buckets = {}
    for e in capped:
        if not e.get("capped_feed"):
            continue
        idx = (e["sd"].date() - start).days // 28
        tm_buckets.setdefault(idx, []).append(e)
    tm_keep = set()
    for lst in tm_buckets.values():
        lst.sort(key=lambda x: -x["score"])
        tm_keep.update(id(e) for e in lst[:5])
    capped = [e for e in capped
              if not e.get("capped_feed") or id(e) in tm_keep]

    capped.sort(key=lambda x: x["sd"])
    return capped, status


def month_keys(start, months):
    keys, y, m = [], start.year, start.month
    for _ in range(months):
        keys.append("%04d-%02d" % (y, m))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return keys


def wix_title(e):
    return "%s: %d %s, %s" % (CITY["city"], e["sd"].day,
                              MONTH_ABBR[e["sd"].month - 1], e["title"])


def payload(events, sources, status, keys, top, start, end):
    def one(e, i):
        return {
            "id": "%s-e%d" % (CITY["slug"], i),
            "cat": e["category"],
            "date": e["sd"].strftime("%Y-%m-%d"),
            "month": e["sd"].strftime("%Y-%m"),
            "day": e["sd"].day,
            "dow": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][e["sd"].weekday()],
            # Midnight with no end time means the source only gave a date
            # (Ottawa Tourism cards, some church calendars) - showing "00:00"
            # reads as a midnight event, so leave the time blank instead.
            "time": ("" if (e["sd"].hour == 0 and e["sd"].minute == 0
                            and (not e["ed"] or e["ed"] == e["sd"]))
                     else e["sd"].strftime("%H:%M") + (
                "–" + e["ed"].strftime("%H:%M") if e["ed"] and e["ed"] != e["sd"] else "")),
            "title": e["title"],
            "wixTitle": wix_title(e),
            "venue": e["venue"], "cost": e["cost"], "source": e["source"],
            "url": e["url"], "score": e["score"],
            "live": bool(e.get("already_live")),
            "verify": e.get("verify"),
            "image": e.get("image") or "wix:" + CITY["default_images"][e["category"]],
            # where the picture came from: feed | event-page | web-search |
            # organizer | default - and, for openly licensed finds, the credit
            "imageSource": e.get("image_source") or ("feed" if e.get("image") else "default"),
            "imageCredit": e.get("image_credit"),
            # base can be null for a city whose base category doesn't exist yet
            "wixCategoryIds": [c for c in (CITY["wix"][e["category"]], CITY["wix"].get("base")) if c],
        }

    return {
        "generatedAt": dt.datetime.now().isoformat(timespec="seconds"),
        "city": CITY["city"],
        "slug": CITY["slug"],
        "timeZoneId": CITY.get("timezone", "America/Toronto"),
        "window": {"from": start.isoformat(), "to": end.isoformat(), "months": keys},
        "topPerCell": top,
        "site": {"name": "HIS Events", "siteId": CITY["wix"]["site_id"]},
        "months": [{"key": k, "label": MONTH_ABBR[int(k[5:]) - 1] + " " + k[:4],
                    "short": MONTH_ABBR[int(k[5:]) - 1]} for k in keys],
        "events": [one(e, i) for i, e in enumerate(events)],
        "sources": [{
            "id": s["id"], "name": s["name"], "url": s["url"],
            "cat": s.get("default_category", "practical"),
            "kind": s.get("kind"), "on": bool(s.get("enabled", True)),
            "status": status.get(s["id"], ("unknown", ""))[0],
            "detail": status.get(s["id"], ("", ""))[1],
            "notes": s.get("notes", ""),
        } for s in sources],
    }


def main():
    global MIN_SCORE, MAX_REPEATS
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=3)
    ap.add_argument("--from", dest="frm", default=None)
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--min-score", dest="min_score", type=int, default=MIN_SCORE)
    ap.add_argument("--max-repeats", dest="max_repeats", type=int, default=MAX_REPEATS)
    ap.add_argument("--city", default="toronto", help="which cities/<name>.json to build")
    ap.add_argument("--out", default=None)
    ap.add_argument("--json", default=None)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--no-images", action="store_true",
                    help="skip the picture search (offline / quick runs)")
    a = ap.parse_args()

    try:                                  # Windows consoles default to cp1252
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                     # noqa: BLE001 - older Python / odd streams
        pass

    def log(m):
        if not a.quiet:
            try:
                print(m)
            except UnicodeEncodeError:
                print(m.encode("ascii", "replace").decode("ascii"))

    MIN_SCORE, MAX_REPEATS = a.min_score, a.max_repeats

    start = dt.date.fromisoformat(a.frm) if a.frm else dt.date.today()
    # "the next 3 months" means 3 whole months ahead; the tail of the current
    # month rides along rather than eating one of the three columns.
    keys = month_keys(start, a.months + (1 if start.day > 1 else 0))
    last = keys[-1]
    ly, lm = int(last[:4]), int(last[5:])
    end = dt.date(ly + (lm == 12), 1 if lm == 12 else lm + 1, 1) - dt.timedelta(days=1)

    cfg_path = os.path.join(HERE, "cities", a.city.lower() + ".json")
    if not os.path.exists(cfg_path):
        have = sorted(f[:-5] for f in os.listdir(os.path.join(HERE, "cities"))
                      if f.endswith(".json"))
        sys.exit("No config for '%s'. Available: %s" % (a.city, ", ".join(have)))
    with open(cfg_path, encoding="utf-8") as f:
        CITY.update(json.load(f))
    sources = CITY["sources"]

    log("HIS %s board · %s → %s · top %d per stream per month"
        % (CITY["city"], start, end, a.top))
    events, status = collect(sources, start, end, log)
    log("  %s\n  %d events kept after de-dupe" % ("-" * 46, len(events)))

    picture_report = None
    # Most feeds carry no picture; look one up (event page -> openly licensed
    # web search -> organizer site) before falling back to the category art.
    if not a.no_images:
        try:
            from image_finder import fill_images
            picture_report = fill_images(events, sources, CITY["city"], log)
        except Exception as ex:                   # noqa: BLE001 - pictures never break a build
            log("  pictures: skipped (%s)" % type(ex).__name__)

    data = payload(events, sources, status, keys, a.top, start, end)
    if picture_report:
        data["pictureReport"] = picture_report     # coverage + search health, for checking

    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        log("  wrote %s" % a.json)

    tpl_path = os.path.join(HERE, "template.html")
    if not os.path.exists(tpl_path):
        sys.exit("template.html missing next to build_board.py")
    with open(tpl_path, encoding="utf-8") as f:
        tpl = f.read()

    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    out = tpl.replace("/*__DATA__*/null", blob).replace("__CITY__", CITY["city"])
    dest = a.out or os.path.join(HERE, "board-%s.html" % CITY["slug"])
    with open(dest, "w", encoding="utf-8") as f:
        f.write(out)
    log("  wrote %s" % dest)

    for c in CATEGORIES:
        row = []
        for k in keys:
            n = sum(1 for e in data["events"] if e["cat"] == c and e["month"] == k)
            row.append("%s %d" % (k[5:], n))
        log("  %-10s %s" % (c, "  ".join(row)))


if __name__ == "__main__":
    main()
