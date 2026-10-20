#!/usr/bin/env python3
"""Find a real picture for every event that comes back from its feed without one.

Most feeds (library calendars, university iCal, city calendars) carry no image,
so ~3 in 4 events used to fall back to the generic per-category art. This runs
after collect() and tries, in order, until one works:

  1. the event's own page          - og:image / twitter:image / JSON-LD image
  2. a web image search            - Pixabay first (free to use, no credit
                                     required; needs PIXABAY_API_KEY), then
                                     Openverse (CC0 / public domain only).
                                     Pixabay links expire, so the event keeps
                                     "pixabay:<id>" and the console fetches a
                                     fresh link when it sends the draft to Wix
  3. the organizer's home page     - its og:image
  4. (caller) per-category default - the existing "wix:<mediaId>" art

Every candidate is checked before it is used: it must load, be a real raster
image (jpeg/png/webp/gif), not be tiny, and not look like a logo/icon. A
picture that the same source reuses across several different events is treated
as a logo and skipped. Results are cached in .cache/images.json between runs
(the workflow persists that folder), so each weekly crawl only looks up what's
new and coverage keeps improving.

Standard library only, like the rest of the pipeline. Never raises: a missing
picture must never break a crawl.
"""

import concurrent.futures as cf
import html
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.environ.get("IMAGE_CACHE", os.path.join(HERE, ".cache", "images.json"))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

DAY = 86400
TTL = {"page": 30 * DAY, "ok": 14 * DAY, "web3": 60 * DAY, "px": 60 * DAY, "home": 30 * DAY}
NEG_TTL = 3 * DAY                      # retry failures sooner than successes

MAX_PAGE_FETCHES = int(os.environ.get("IMAGE_MAX_PAGES", "500"))   # per city
MAX_WEB_SEARCHES = int(os.environ.get("IMAGE_MAX_SEARCHES", "40"))   # per city, so later cities get a share
# Seconds per city for each step, so a slow first step can't starve the later
# ones (the web search used to get no turn at all in big cities).
PAGE_BUDGET = float(os.environ.get("IMAGE_PAGE_BUDGET", "50"))
SEARCH_BUDGET = float(os.environ.get("IMAGE_SEARCH_BUDGET", "60"))
HOME_BUDGET = float(os.environ.get("IMAGE_HOME_BUDGET", "15"))
WORKERS = 12
LOGO_REPEAT = 4          # same picture on >= this many different titles in one source

OPENVERSE = "https://api.openverse.org/v1/images/"
PIXABAY = "https://pixabay.com/api/"
MAX_PIXABAY = int(os.environ.get("IMAGE_MAX_PIXABAY", "60"))   # per city; API allows 100/min
OK_TYPES = ("image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif")
BAD_URL = re.compile(r"(logo|favicon|icon|sprite|placeholder|default[-_]?image|blank|spacer|"
                     r"avatar|gravatar|badge|banner-ad|pixel|1x1)", re.I)
BAD_EXT = re.compile(r"\.(svg|ico|bmp|tiff?)(\?|#|$)", re.I)

# Stand-in pictures must never show identifiable real people (politicians,
# officials, celebrities): skip news/encyclopedia sources and person-ish titles.
EXCLUDED_SOURCES = "wikimedia,wikimedia_audio,nasa,smithsonian_national_portrait_gallery"
PEOPLE_WORDS = re.compile(r"\b(president|prime minister|minister|secretary|senator|mayor|"
                          r"governor|king|queen|prince|princess|ambassador|general|"
                          r"chancellor|met with|meets|meeting with|portrait|headshot|"
                          r"selfie|celebrity|actor|actress|singer)\b", re.I)

STOP = set("""a an and the of for to in on at by with from or & - – — | : drop-in dropin
drop online virtual session sessions workshop workshops program programme class classes series
info information table meeting club circle hour hours am pm free weekly monthly daily
part new edition annual presented presents hosted host join us event events week
""".split())


def _now():
    return time.time()


class _Cache:
    """Tiny JSON cache shared by the worker threads."""

    def __init__(self, path):
        self.path, self.lock, self.data = path, threading.Lock(), {}
        try:
            with open(path, encoding="utf-8") as f:
                self.data = json.load(f)
        except Exception:                          # noqa: BLE001 - empty/corrupt cache is fine
            self.data = {}

    def get(self, kind, key):
        with self.lock:
            hit = self.data.get(kind + ":" + key)
        if not hit:
            return None
        age = _now() - hit.get("t", 0)
        ttl = TTL[kind] if hit.get("v") not in (None, "", [], False) else NEG_TTL
        return hit if age < ttl else None

    def put(self, kind, key, value):
        with self.lock:
            self.data[kind + ":" + key] = {"v": value, "t": int(_now())}

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            # Drop entries far past every TTL so the file can't grow forever.
            cutoff = _now() - 120 * DAY
            keep = {k: v for k, v in self.data.items() if v.get("t", 0) > cutoff}
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(keep, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except Exception:                          # noqa: BLE001
            pass


def _get(url, timeout=8, max_bytes=600_000, accept="text/html,*/*", headers=None):
    req = urllib.request.Request(url, headers=dict({
        "User-Agent": UA, "Accept": accept, "Accept-Language": "en-CA,en;q=0.9"}, **(headers or {})))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.headers, r.read(max_bytes)


# --------------------------------------------------------------------------- pages
_META = re.compile(r"<meta\b[^>]*>", re.I)
_ATTR = re.compile(r'([a-zA-Z:_-]+)\s*=\s*("([^"]*)"|\'([^\']*)\')')
_LDJSON = re.compile(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.I | re.S)
_IMGSRC = re.compile(r'<link\b[^>]*rel=["\']image_src["\'][^>]*>', re.I)


def page_images(markup, base):
    """Candidate pictures a page advertises for itself, best first."""
    found = []

    def add(u):
        if not u:
            return
        u = html.unescape(u.strip())
        if u.startswith("//"):
            u = "https:" + u
        u = urllib.parse.urljoin(base, u)
        if u.startswith("http") and u not in found:
            found.append(u)

    metas = {}
    for tag in _META.findall(markup):
        attrs = {m.group(1).lower(): (m.group(3) if m.group(3) is not None else m.group(4))
                 for m in _ATTR.finditer(tag)}
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        if key and attrs.get("content"):
            metas.setdefault(key, attrs["content"])
    for key in ("og:image:secure_url", "og:image", "og:image:url", "twitter:image",
                "twitter:image:src"):
        add(metas.get(key))

    for blob in _LDJSON.findall(markup):
        try:
            data = json.loads(blob.strip())
        except Exception:                          # noqa: BLE001
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop(0)
            if isinstance(node, dict):
                img = node.get("image")
                if isinstance(img, str):
                    add(img)
                elif isinstance(img, dict):
                    add(img.get("url"))
                elif isinstance(img, list):
                    for i in img:
                        add(i if isinstance(i, str) else (i or {}).get("url"))
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
            elif isinstance(node, list):
                stack.extend(node)

    for tag in _IMGSRC.findall(markup):
        m = re.search(r'href=["\']([^"\']+)', tag)
        add(m.group(1) if m else None)
    return found


def looks_wrong(url):
    path = urllib.parse.urlparse(url).path
    return bool(BAD_EXT.search(path) or BAD_URL.search(path))


def check_image(url, cache):
    """True if url loads as a real, reasonably sized raster image."""
    if not url or looks_wrong(url):
        return False
    hit = cache.get("ok", url)
    if hit is not None:
        return bool(hit["v"])
    ok = False
    try:
        status, headers, body = _get(url, timeout=12, max_bytes=65_536,
                                     accept="image/avif,image/webp,image/*,*/*;q=0.8",
                                     headers={"Range": "bytes=0-65535"})
        ctype = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
        size = headers.get("Content-Range", "").rpartition("/")[2] or headers.get("Content-Length")
        size = int(size) if size and size.isdigit() else len(body)
        ok = status in (200, 206) and ctype in OK_TYPES and size >= 12_000
    except urllib.error.HTTPError:
        ok = False                                 # a real "no": cache it
    except Exception:                              # noqa: BLE001 - timeout etc: retry next run
        return False
    cache.put("ok", url, ok)
    return ok


def fetch_page_images(url, cache):
    hit = cache.get("page", url)
    if hit is not None:
        return hit["v"] or []
    imgs = []
    try:
        status, headers, body = _get(url)
        ctype = (headers.get("Content-Type") or "").lower()
        if status == 200 and "html" in ctype:
            charset = headers.get_content_charset() or "utf-8"
            imgs = page_images(body.decode(charset, "replace"), url)[:4]
    except urllib.error.HTTPError:
        imgs = []                                  # a real answer (404, 403...): cache it
    except Exception:                              # noqa: BLE001 - timeout etc: retry next run
        return []
    cache.put("page", url, imgs)
    return imgs


# --------------------------------------------------------------------------- web search
def search_query(title, city):
    """Turn an event title into a short picture-search query."""
    t = re.sub(r"\(.*?\)|\[.*?\]", " ", title)
    t = re.sub(r"\b%s\b" % re.escape(city), " ", t, flags=re.I)
    t = re.sub(r"\b\d{1,4}(st|nd|rd|th)?\b", " ", t)
    words = [w for w in re.findall(r"[A-Za-zÀ-ÿ'’]+", t) if w.lower() not in STOP and len(w) > 2]
    return " ".join(words[:4]).lower()


_token = {"v": None, "exp": 0}


def _openverse_headers():
    """Optional registered credentials lift the anonymous rate limit."""
    cid, secret = os.environ.get("OPENVERSE_CLIENT_ID"), os.environ.get("OPENVERSE_CLIENT_SECRET")
    if not (cid and secret):
        return {}
    if _token["v"] and _token["exp"] > _now() + 60:
        return {"Authorization": "Bearer " + _token["v"]}
    try:
        body = urllib.parse.urlencode({"client_id": cid, "client_secret": secret,
                                       "grant_type": "client_credentials"}).encode()
        req = urllib.request.Request("https://api.openverse.org/v1/auth_tokens/token/", data=body,
                                     headers={"User-Agent": UA,
                                              "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=10) as r:
            tok = json.loads(r.read().decode())
        _token.update(v=tok["access_token"], exp=_now() + int(tok.get("expires_in", 3600)))
        return {"Authorization": "Bearer " + _token["v"]}
    except urllib.error.HTTPError as e:
        _token["err"] = "HTTP %s" % e.code      # 401 = wrong id/secret, or email not confirmed
        return {}
    except Exception as e:                         # noqa: BLE001
        _token["err"] = type(e).__name__
        return {}


def pixabay_search(query, cache, state):
    """Pixabay photo for `query` -> ("pixabay:<id>", credit), or None.

    Pixabay forbids permanent hotlinking and its image URLs expire, so only the
    stable id is kept; the console asks Pixabay for a fresh URL at draft time
    and Wix stores its own copy. Results are cached (Pixabay asks for >= 24h).
    """
    key = os.environ.get("PIXABAY_API_KEY")
    if not (key and query):
        return None
    hit = cache.get("px", query)
    if hit is not None:
        return tuple(hit["v"]) if hit["v"] else None
    with state["lock"]:
        if state["px"] >= MAX_PIXABAY or state["px_throttled"]:
            return None
        state["px"] += 1
    try:
        qs = urllib.parse.urlencode({"key": key, "q": query[:100], "image_type": "photo",
                                     "orientation": "horizontal", "safesearch": "true",
                                     "per_page": 20, "min_width": 1000})
        status, headers, body = _get(PIXABAY + "?" + qs, timeout=20, max_bytes=800_000,
                                     accept="application/json")
        hits = json.loads(body.decode("utf-8")).get("hits", [])
    except urllib.error.HTTPError as e:
        with state["lock"]:
            if e.code == 429:
                state["px_throttled"] = True
            k = "pixabay HTTP %s" % e.code
            state["errkinds"][k] = state["errkinds"].get(k, 0) + 1
            state["errors"] += 1
        return None
    except Exception as e:                         # noqa: BLE001 - retry next run
        with state["lock"]:
            k = "pixabay " + type(e).__name__
            state["errkinds"][k] = state["errkinds"].get(k, 0) + 1
            state["errors"] += 1
        return None
    result = None
    for h in hits:
        if PEOPLE_WORDS.search(str(h.get("tags") or "")):
            continue
        url, pid = h.get("largeImageURL") or h.get("webformatURL"), h.get("id")
        if not (url and pid):
            continue
        # The URL is fresh right now, so it can be checked; only the id is kept.
        if check_image(url, cache):
            result = ("pixabay:%s" % pid, "Pixabay / %s" % (h.get("user") or "contributor"))
            break
    cache.put("px", query, list(result) if result else "")
    return result


def web_search(query, cache, state):
    """Openly licensed picture for `query`, or None. Returns (url, credit).

    Only a real answer is cached ("found X" or "nothing suitable"). Timeouts
    and errors are NOT cached: an uncached Openverse query can take 10s+, and
    caching those failures as "no picture" used to silence the search for days.
    """
    if not query:
        return None
    hit = cache.get("web3", query)
    if hit is not None:
        return tuple(hit["v"]) if hit["v"] else None
    with state["lock"]:
        if state["searches"] >= MAX_WEB_SEARCHES or state["throttled"]:
            return None
        state["searches"] += 1
    try:
        qs = urllib.parse.urlencode({"q": query, "license": "cc0,pdm", "page_size": 20,
                                     "mature": "false", "category": "photograph,digitized_artwork",
                                     "excluded_source": EXCLUDED_SOURCES})
        status, headers, body = _get(OPENVERSE + "?" + qs, timeout=30, max_bytes=800_000,
                                     accept="application/json", headers=_openverse_headers())
        results = json.loads(body.decode("utf-8")).get("results", [])
    except urllib.error.HTTPError as e:
        if e.code == 429:
            state["throttled"] = True        # stop for this run; cache keeps what we have
        with state["lock"]:
            state["errors"] += 1
            k = "HTTP %s" % e.code
            state["errkinds"][k] = state["errkinds"].get(k, 0) + 1
        return None
    except Exception as e:                         # noqa: BLE001 - timeout etc: retry next run
        with state["lock"]:
            state["errors"] += 1
            k = type(e).__name__
            state["errkinds"][k] = state["errkinds"].get(k, 0) + 1
            # Openverse doesn't answer from some cloud servers (GitHub Actions):
            # after a few timeouts stop waiting on it, so the time goes to Pixabay.
            if state["errkinds"].get("TimeoutError", 0) >= 3:
                state["throttled"] = True
        return None
    result = None
    for r in results:
        w, h, url = r.get("width"), r.get("height"), r.get("url")
        if not url or looks_wrong(url) or "wikimedia.org" in url:
            continue
        if PEOPLE_WORDS.search(" ".join(str(r.get(k) or "") for k in ("title", "creator"))
                               + " " + " ".join(str(x.get("name", "")) for x in r.get("tags") or [])):
            continue
        if w and w < 640:                          # sizes are sometimes unknown: allow those
            continue
        if w and h and w / h < 1.1:                # prefer landscape for event banners
            continue
        credit = "%s (%s, via Openverse)" % (r.get("creator") or "unknown",
                                            (r.get("license") or "").upper())
        result = (url, credit)
        break
    cache.put("web3", query, list(result) if result else "")
    return result


# --------------------------------------------------------------------------- driver
def fill_images(events, sources, city_name, log=print, cache_path=None):
    """Give every event in `events` the best picture we can find, in place.

    Sets e["image"] (only when a verified picture is found) and
    e["image_source"] in {"feed", "event-page", "web-search", "organizer",
    "default"}. Returns a summary dict.
    """
    if os.environ.get("NO_IMAGE_SEARCH"):
        return {"skipped": True}
    t0 = _now()
    cache = _Cache(cache_path or CACHE_PATH)
    by_src = {s["id"]: s for s in sources}
    state = {"searches": 0, "throttled": False, "errors": 0, "errkinds": {}, "lock": threading.Lock(),
             "px": 0, "px_throttled": False}
    clock = {"deadline": _now() + PAGE_BUDGET}
    out_of_time = lambda: _now() > clock["deadline"]       # noqa: E731

    def stage(seconds):
        clock["deadline"] = _now() + seconds

    def pool_map(fn, items):
        items = list(items)
        res = {}
        if not items:
            return res
        with cf.ThreadPoolExecutor(WORKERS) as ex:
            # Each task re-checks the clock, so a slow site can't blow the budget.
            futs = {ex.submit(lambda it: None if out_of_time() else fn(it), it): it for it in items}
            for f in cf.as_completed(futs):
                try:
                    res[futs[f]] = f.result()
                except Exception:                  # noqa: BLE001
                    res[futs[f]] = None
        return res

    # 0. Feed images: keep them, but only if they actually load.
    feed = {e["image"] for e in events if e.get("image") and str(e["image"]).startswith("http")}
    ok = pool_map(lambda u: check_image(u, cache), feed)
    for e in events:
        img = e.get("image")
        if img and str(img).startswith("http"):
            if ok.get(img):
                e["image_source"] = "feed"
            else:
                e["image"] = None

    # 1. The event's own page.
    need = [e for e in events if not e.get("image") and e.get("url")]
    page_urls = []
    for e in need:
        src = by_src.get(e.get("source_id")) or {}
        u = e["url"]
        if u not in page_urls and u.rstrip("/") not in {(src.get("home") or "").rstrip("/"),
                                                       (src.get("url") or "").rstrip("/")}:
            page_urls.append(u)
    page_urls = page_urls[:MAX_PAGE_FETCHES]
    pages = pool_map(lambda u: fetch_page_images(u, cache), page_urls) if not out_of_time() else {}

    # A picture the same source attaches to several *different* events is its
    # logo or a stock banner, not the event's own photo - skip those.
    seen = {}
    for e in need:
        for img in (pages.get(e["url"]) or [])[:1]:
            seen.setdefault((e.get("source_id"), img), set()).add(e["title"].strip().lower())
    logos = {k[1] for k, titles in seen.items() if len(titles) >= LOGO_REPEAT}

    cands = {}
    for e in need:
        for img in pages.get(e["url"]) or []:
            if img not in logos:
                cands[id(e)] = img
                break
    ok = pool_map(lambda u: check_image(u, cache), set(cands.values())) if not out_of_time() else {}
    for e in need:
        img = cands.get(id(e))
        if img and ok.get(img):
            e["image"], e["image_source"] = img, "event-page"

    # 2. Openly licensed web search on the event title (best-scored events first).
    stage(SEARCH_BUDGET)
    need = sorted((e for e in events if not e.get("image")), key=lambda x: -x.get("score", 0))
    queries = {}
    for e in need:
        q = search_query(e["title"], city_name)
        if q:
            queries.setdefault(q, []).append(e)
    def search_one(q):
        found = pixabay_search(q, cache, state)       # already verified, keeps only the id
        if found:
            return found
        found = web_search(q, cache, state)
        return found if found and check_image(found[0], cache) else None

    # A few searches at a time: the services can be slow, but don't hammer them.
    found_by_q = {}
    with cf.ThreadPoolExecutor(4) as ex:
        futs = {ex.submit(lambda q: None if out_of_time() else search_one(q), q): q for q in queries}
        for f in cf.as_completed(futs):
            try:
                found_by_q[futs[f]] = f.result()
            except Exception:                      # noqa: BLE001
                pass
    for q, evs in queries.items():
        found = found_by_q.get(q)
        if found:
            for e in evs:
                e["image"], e["image_source"], e["image_credit"] = found[0], "web-search", found[1]

    # 3. The organizer's own site picture (one lookup per source).
    stage(HOME_BUDGET)
    need = [e for e in events if not e.get("image")]
    homes = {}
    for e in need:
        src = by_src.get(e.get("source_id")) or {}
        h = src.get("home") or src.get("url")
        if h and h.startswith("http"):
            homes[e.get("source_id")] = h
    if homes and not out_of_time():
        imgs = pool_map(lambda h: fetch_page_images(h, cache), set(homes.values()))
        best = {}
        for sid, h in homes.items():
            for img in imgs.get(h) or []:
                if check_image(img, cache):
                    best[sid] = img
                    break
        for e in need:
            img = best.get(e.get("source_id"))
            if img:
                e["image"], e["image_source"] = img, "organizer"

    for e in events:
        e.setdefault("image_source", "default" if not e.get("image") else "feed")

    cache.save()
    counts = {}
    for e in events:
        counts[e["image_source"]] = counts.get(e["image_source"], 0) + 1
    if os.environ.get("OPENVERSE_CLIENT_ID") and os.environ.get("OPENVERSE_CLIENT_SECRET"):
        auth = ("registered key OK" if _token["v"] else
                "registered key NOT accepted (%s) - check the GitHub secrets / confirm the email"
                % _token.get("err", "no searches tried"))
    else:
        auth = "no key (anonymous)"
    report = {"counts": counts, "webSearches": state["searches"], "pixabaySearches": state["px"],
              "pixabay": "key set" if os.environ.get("PIXABAY_API_KEY") else "no key",
              "searchErrors": state["errkinds"],
              "rateLimited": state["throttled"], "searchAuth": auth,
              "seconds": round(_now() - t0)}
    if not os.environ.get("IMAGE_FINDER_QUIET"):
        print("::notice title=pictures %s::%s, pixabay %d (%s), openverse %d (%s)%s%s" % (
        city_name, ", ".join("%s %d" % kv for kv in sorted(counts.items())), state["px"],
        report["pixabay"], state["searches"], auth,
        ", errors %s" % state["errkinds"] if state["errors"] else "",
        ", rate-limited" if state["throttled"] else ""))
    log("  pictures: %s%s (%.0fs, %d web searches%s)" % (
        ", ".join("%s %d" % kv for kv in sorted(counts.items())),
        "", _now() - t0, state["searches"],
        (", %d search errors" % state["errors"] if state["errors"] else "")
        + (", search rate-limited" if state["throttled"] else "")))
    return report
