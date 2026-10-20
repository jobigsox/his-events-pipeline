#!/usr/bin/env python3
"""Tests for image_finder.py against a local fake web (no internet needed).

    python test_image_finder.py
"""

import http.server
import json
import os
import struct
import tempfile
import threading
import unittest
import zlib

import image_finder as F


def png(w=900, h=600, pad=20_000):
    """A valid PNG header plus padding, big enough to pass the size check."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\0" * pad)) + b"\0" * pad)


PAGES = {}
IMAGES = {}
SEARCH = {}
PIXA = {}
FAIL = set()
HITS = []


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        HITS.append(self.path)
        path = self.path.split("?")[0]
        if path.startswith("/pixabay"):
            q = self.path.split("q=")[1].split("&")[0].replace("+", " ")
            body = json.dumps({"hits": PIXA.get(q, [])}).encode()
            return self._send(200, "application/json", body)
        if path.startswith("/search"):
            q = self.path.split("q=")[1].split("&")[0].replace("+", " ")
            if q in FAIL:
                return self._send(503, "text/plain", b"busy")
            body = json.dumps({"results": SEARCH.get(q, [])}).encode()
            return self._send(200, "application/json", body)
        if path in IMAGES:
            ctype, body = IMAGES[path]
            return self._send(200, ctype, body)
        if path in PAGES:
            return self._send(200, "text/html; charset=utf-8", PAGES[path].encode())
        self._send(404, "text/plain", b"nope")

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FinderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_port
        os.environ["NO_PROXY"] = "127.0.0.1,localhost"
        os.environ.pop("NO_IMAGE_SEARCH", None)
        os.environ["IMAGE_FINDER_QUIET"] = "1"   # keep CI annotations for real cities
        F.OPENVERSE = cls.base + "/search"
        F.PIXABAY = cls.base + "/pixabay"
        b = cls.base
        IMAGES.update({
            "/img/parade.jpg": ("image/jpeg", png()),
            "/img/logo-lib.png": ("image/png", png()),
            "/img/libbanner.png": ("image/png", png()),
            "/img/tiny.png": ("image/png", b"\x89PNG small"),
            "/img/scrabble.jpg": ("image/jpeg", png()),
            "/img/org.jpg": ("image/jpeg", png()),
            "/img/page.svg": ("image/svg+xml", b"<svg/>" * 5000),
        })
        PAGES.update({
            "/parade": '<html><head><meta property="og:image" content="/img/parade.jpg"></head></html>',
            "/ld": '<script type="application/ld+json">{"@type":"Event","image":{"url":"%s/img/parade.jpg"}}</script>' % b,
            "/broken": '<meta property="og:image" content="/img/missing.jpg">',
            "/small": '<meta name="twitter:image" content="/img/tiny.png">',
            "/svg": '<meta property="og:image" content="/img/page.svg">',
            "/org": '<meta property="og:image" content="/img/org.jpg">',
        })
        for i in range(5):
            PAGES["/lib%d" % i] = '<meta property="og:image" content="/img/libbanner.png">'
        SEARCH["social scrabble"] = [
            {"url": b + "/img/scrabble.jpg", "width": 1200, "height": 800,
             "creator": "Jane", "license": "cc0"}]

    def setUp(self):
        HITS.clear()
        self.tmp = tempfile.mkdtemp()
        self.cache = os.path.join(self.tmp, "images.json")

    def ev(self, title, path, source="s1", score=60, image=None):
        return {"title": title, "url": self.base + path if path else None, "source_id": source,
                "score": score, "image": image}

    def run_fill(self, events, sources=None):
        sources = sources or [{"id": "s1", "home": self.base + "/org", "url": self.base + "/feed"},
                              {"id": "s2", "home": self.base + "/nowhere", "url": self.base + "/feed2"}]
        return F.fill_images(events, sources, "Hamilton", log=lambda m: None, cache_path=self.cache)

    def test_og_image_from_event_page(self):
        e = self.ev("Santa Claus Parade", "/parade")
        self.run_fill([e])
        self.assertEqual(e["image"], self.base + "/img/parade.jpg")
        self.assertEqual(e["image_source"], "event-page")

    def test_json_ld_image(self):
        e = self.ev("Parade", "/ld")
        self.run_fill([e])
        self.assertEqual(e["image_source"], "event-page")

    def test_logo_reused_across_events_is_skipped(self):
        evs = [self.ev("Library program %d" % i, "/lib%d" % i, source="s2") for i in range(5)]
        self.run_fill(evs)
        self.assertTrue(all(e["image_source"] != "event-page" for e in evs))

    def test_broken_small_and_svg_pictures_fall_through_to_web_search(self):
        evs = [self.ev("Social Scrabble", "/broken", source="s2"),
               self.ev("Social Scrabble (Central)", "/small", source="s2"),
               self.ev("Social Scrabble Hamilton", "/svg", source="s2")]
        self.run_fill(evs)
        for e in evs:
            self.assertEqual(e["image_source"], "web-search", e["title"])
            self.assertEqual(e["image"], self.base + "/img/scrabble.jpg")
            self.assertIn("CC0", e["image_credit"])

    def test_organizer_site_when_search_finds_nothing(self):
        e = self.ev("Tax Clinic", "/broken", source="s1")
        self.run_fill([e])
        self.assertEqual(e["image_source"], "organizer")
        self.assertEqual(e["image"], self.base + "/img/org.jpg")

    def test_default_when_nothing_found(self):
        e = self.ev("Tax Clinic", "/broken", source="s2")
        self.run_fill([e])
        self.assertIsNone(e["image"])
        self.assertEqual(e["image_source"], "default")

    def test_working_feed_image_is_kept_and_broken_one_replaced(self):
        good = self.ev("Parade", "/parade", image=self.base + "/img/org.jpg")
        bad = self.ev("Santa Claus Parade", "/parade", image=self.base + "/img/missing.jpg")
        self.run_fill([good, bad])
        self.assertEqual(good["image_source"], "feed")
        self.assertEqual(bad["image_source"], "event-page")

    def test_second_run_uses_the_cache(self):
        self.run_fill([self.ev("Santa Claus Parade", "/parade")])
        HITS.clear()
        e = self.ev("Santa Claus Parade", "/parade")
        self.run_fill([e])
        self.assertEqual(e["image_source"], "event-page")
        self.assertEqual(HITS, [], "everything should come from the cache")

    def test_search_errors_are_not_cached(self):
        """A slow/failed search must be retried next run, not remembered as 'nothing'."""
        FAIL.add("pottery wheel")
        e = self.ev("Pottery Wheel", "/broken", source="s2")
        self.run_fill([e])
        self.assertEqual(e["image_source"], "default")
        FAIL.discard("pottery wheel")
        SEARCH["pottery wheel"] = [{"url": self.base + "/img/scrabble.jpg", "width": None,
                                    "height": None, "creator": "A", "license": "pdm"}]
        e2 = self.ev("Pottery Wheel", "/broken", source="s2")
        self.run_fill([e2])
        self.assertEqual(e2["image_source"], "web-search")

    def test_photos_of_real_people_are_skipped(self):
        SEARCH["military leadership"] = [
            {"url": self.base + "/img/parade.jpg", "width": 1200, "height": 800, "creator": "DoD",
             "license": "pdm", "title": "President met with the Secretary of Defense"},
            {"url": self.base + "/img/scrabble.jpg", "width": 1200, "height": 800, "creator": "B",
             "license": "cc0", "title": "Compass on a map"}]
        e = self.ev("Military Leadership", "/broken", source="s2")
        self.run_fill([e])
        self.assertEqual(e["image"], self.base + "/img/scrabble.jpg")

    def test_pixabay_first_and_keeps_only_the_id(self):
        PIXA["bike repair"] = [
            {"id": 111, "largeImageURL": self.base + "/img/parade.jpg", "tags": "portrait, man",
             "user": "x"},
            {"id": 222, "largeImageURL": self.base + "/img/scrabble.jpg", "tags": "bicycle, tools",
             "user": "Jane"}]
        SEARCH["bike repair"] = [{"url": self.base + "/img/org.jpg", "width": 1200, "height": 800,
                                  "creator": "B", "license": "cc0", "title": "bike"}]
        os.environ["PIXABAY_API_KEY"] = "test"
        try:
            e = self.ev("Bike Repair", "/broken", source="s2")
            self.run_fill([e])
        finally:
            del os.environ["PIXABAY_API_KEY"]
        self.assertEqual(e["image"], "pixabay:222")          # person photo 111 skipped
        self.assertEqual(e["image_source"], "web-search")
        self.assertIn("Pixabay / Jane", e["image_credit"])

    def test_without_pixabay_key_openverse_is_used(self):
        SEARCH["kite flying"] = [{"url": self.base + "/img/org.jpg", "width": 1200, "height": 800,
                                  "creator": "B", "license": "cc0", "title": "kite"}]
        e = self.ev("Kite Flying", "/broken", source="s2")
        self.run_fill([e])
        self.assertEqual(e["image"], self.base + "/img/org.jpg")

    def test_search_query_cleanup(self):
        self.assertEqual(F.search_query("Social Scrabble (Online) - Hamilton 2026", "Hamilton"),
                         "social scrabble")
        self.assertEqual(F.search_query("Drop-In Knitting and Crochet", "Hamilton"), "knitting crochet")

    def test_disabled_by_env(self):
        os.environ["NO_IMAGE_SEARCH"] = "1"
        try:
            e = self.ev("Santa Claus Parade", "/parade")
            self.assertEqual(self.run_fill([e]), {"skipped": True})
        finally:
            del os.environ["NO_IMAGE_SEARCH"]


if __name__ == "__main__":
    unittest.main()
