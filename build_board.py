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
import json
import os
import re
import sys
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
EXCLUDE = ("seniors", "age 55", "0-6", "2-6", "kids", "children", "child care",
           "earlyon", "toddler", "parent", "caregiver", "disabilit", "13-17",
           "high school", "grade ")

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

def fetch_text(url, timeout=30, accept="text/html,application/xhtml+xml,*/*"):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": accept,
        "Accept-Language": "en-CA,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def fetch(url, timeout=25):
    return json.loads(fetch_text(url, timeout, "application/json, text/plain, */*"))


def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def parse_tribe(src, start, end):
    """The Events Calendar REST API."""
    out, page, pages = [], 1, 1
    while page <= pages and page <= 6:
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
            if v:
                venue = ", ".join(x for x in [v.get("venue"), v.get("address"), v.get("city")] if x)
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
                "venue": venue, "cost": cost,
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
        venue = ", ".join(x for x in [e.get("venue_name") or e.get("location"),
                                      e.get("venue_room")] if x) or e.get("library") or "Hamilton"
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


def parse_manual(src, start, end):
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
        })
    return out


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
                "utc": True, "venue": "Mohawk College", "cost": "Free",
                "url": url,
                "image": urllib.parse.urljoin(src["url"], im.group(1)) if im else None,
            })
        if len(articles) < 8:      # short page - last one
            break
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


PARSERS = {"tribe": parse_tribe, "sqs": parse_sqs, "manual": parse_manual,
           "mohawk_drupal": parse_mohawk_drupal, "ticketmaster": parse_ticketmaster,
           "communico": parse_communico, "opl": parse_opl, "probe": parse_probe,
           "mcmaster_cards": parse_mcmaster_cards}


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
            continue
        try:
            raw = parser(src, start, end)
        except urllib.error.HTTPError as e:
            status[src["id"]] = ("blocked", "HTTP %s" % e.code)
            log("  %-34s BLOCKED (HTTP %s)" % (src["name"], e.code))
            continue
        except Exception as e:                      # noqa: BLE001 - fail soft per source
            status[src["id"]] = ("dead", type(e).__name__)
            log("  %-34s FAILED (%s)" % (src["name"], type(e).__name__))
            continue

        kept = 0
        for e in raw:
            sd = to_dt(e["start"], e.get("utc"))
            ed = to_dt(e["end"], e.get("utc")) or sd
            if not sd or not (start <= sd.date() <= end):
                continue
            cat = e.get("forced_category") or classify(
                e["title"], e.get("description", ""), src.get("default_category", "practical"))
            e.update({
                "sd": sd, "ed": ed, "category": cat, "source_kind": src.get("kind"),
                "image": e.get("image") or src.get("image"),
            })
            e["score"] = score(e)
            if src.get("kind") == "manual":
                # A human already vetted these, so keyword scoring doesn't get a
                # veto — Nuit Blanche reads as "art", not "newcomer", and would
                # otherwise be dropped despite being one of the best free nights
                # of the year for someone new to the city.
                e["score"] = max(e["score"], 72)
            # Ticketed shows never clear MIN_SCORE on cost alone (see `score`),
            # but students still go to paid concerts/festivals - they're capped
            # to the best few per rolling 4-week window below instead of being
            # vetted out here.
            if e["score"] < MIN_SCORE and src.get("kind") != "ticketmaster":
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

    # Ticketed sources (Ticketmaster etc.) get their own cadence: best 5 per
    # rolling 4-week window, not per calendar month/category - so a handful of
    # billed shows students would genuinely go to still surface, without their
    # near-zero cost score letting them crowd out the free practical/social feed.
    tm_buckets = {}
    for e in capped:
        if e.get("source_kind") != "ticketmaster":
            continue
        idx = (e["sd"].date() - start).days // 28
        tm_buckets.setdefault(idx, []).append(e)
    tm_keep = set()
    for lst in tm_buckets.values():
        lst.sort(key=lambda x: -x["score"])
        tm_keep.update(id(e) for e in lst[:5])
    capped = [e for e in capped
              if e.get("source_kind") != "ticketmaster" or id(e) in tm_keep]

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
            "time": e["sd"].strftime("%H:%M") + (
                "–" + e["ed"].strftime("%H:%M") if e["ed"] and e["ed"] != e["sd"] else ""),
            "title": e["title"],
            "wixTitle": wix_title(e),
            "venue": e["venue"], "cost": e["cost"], "source": e["source"],
            "url": e["url"], "score": e["score"],
            "live": bool(e.get("already_live")),
            "verify": e.get("verify"),
            "image": e.get("image") or "wix:" + CITY["default_images"][e["category"]],
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

    data = payload(events, sources, status, keys, a.top, start, end)

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
