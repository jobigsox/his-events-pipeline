#!/usr/bin/env python3
"""Offline tests for the AudienceView, iMIS and Sitecore SXA parsers and the
per-source venue_filter, using trimmed copies of the real pages (Oct 2026).

    python test_new_parsers.py
"""

import datetime as dt
import json
import unittest

import build_board as B

AV_PAGE1 = r'''<script>
  searchResults : [
    [ "0E98A6EF-3F78-4B2B-9800-0477A98BE359", "B", "subscription", "", "26/27 Build-Your-Own Bundle", "26/27 Build-Your-Own Bundle", "26/27 Build-Your-Own Bundle & Save 20%", "", "", "", "", "", "", "", "S", "", "", "", "", "", "/content/Images/COB/2627TRP/Bundles2026-02.jpg", "" ],
    [ "6D7237F0-4659-4F32-A3EC-04F78EDA5253", "P", "Concert", "", "TIBSTROct2426", "This is Brampton<br />\ncurated by Ethan Berkeley Garcia", "STRIPPED: Behind the art", "Sat., Oct. 24, 2026 8:00 p.m.", "20:00", "24", "9", "2026", "", "", "S", "E", "74", "emerging artist,local artist,rock,This is Brampton", "default.asp?doWork::WScontent::loadArticle=Load&BOparam::WScontent::loadArticle::article_id=CAA0A7F4&BOparam::WScontent::loadArticle::context_id=6D7237F0", "", "/content/Images/COB/2627TRP/TIBSTROct2426web.jpg", "", "", "", "", "", "The Rose presents", "", "", "", "", "", "", "+fees/tx", "Concert", "", "", "", "", "", "", "0", "TIBSTROct2426", "", "", "", "", "", "", "", "", "", "The Rose presents", [], ["3","7"], "1 Theatre Lane", "Brampton", "ON", "L6V 0A3", "Canada", "", "", "7978EEF1-0E16-47FD-9F3C-4E130CCED80E", "Rose Studio", "Rose Studio", "Rose Studio", "", "", "general admission", "26/27 Studio", "$20.00", "$20.00", "Public" ],
    [ "9CE78114-6C87-404E-AD15-94EA7CB1A18D", "P", "", "", "BFCOct3026", "Presented by: Brampton Folk Club", "The Great Canadiana Song Swap", "Fri., Oct. 30, 2026 7:30 p.m.", "19:30", "30", "9", "2026", "", "", "S", "E", "283", "Concert,Folk,Offsite", "default.asp?x=1", "", "/content/Images/COB/2425Rentals/BFC2425.jpg", "", "", "", "", "", "Brampton Folk Club presents:", "", "with James Gordon and Ian Bell", "", "St. Paul\'s United Church", "30 Main St. S., Brampton", "", "+fees/tx", "", "", "", "", "", "", "", "0", "BFCOct3026", "", "", "", "", "", "", "", "", "", "Brampton Folk Club", [], ["3","6"], "", "", "", "", "", "", "", "58CA6E46-1DB2-47A2-A418-B9A85F4C85AD", "Venues Offsite", "Venues Offsite", "Venues Offsite", "general admission", "26/27 Rentals", "$22.00", "$22.00", "Public" ]
  ]
</script>'''

AV_PAGE2 = r'''searchResults : [
    [ "AAAAAAAA-0738-41F0-B92B-45BE939FDA3E", "P", "Concert", "", "X", "", "Winter Lights Free Concert", "Sat., Jan. 30, 2027 7:00 p.m.", "19:00", "30", "0", "2027", "", "", "S", "E", "1", "", "default.asp?y=2", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", [], [], "1 Theatre Lane", "Brampton", "ON", "L6V 0A3", "Canada", "", "", "FEB65D67-9830-4FC5-9C38-72309F140C5F", "Rose Mainstage Reserved", "Rose Mainstage", "Rose Mainstage", "reserved", "26/27", "$0.00", "$0.00", "Public" ]
  ]'''

IMIS = '''<tr class="rgRow"><td>
<div class="SearchResultSummary">
    <div class="ListItemTitle SearchResultLink">
        <a id="x" class="ClearFix" href="../../../Event.aspx?EventKey=102426BOO2&amp;WebsiteKey=7be0"><span id="y">Halloween at the Boo-seum</span></a>
    </div>
    <div class="SearchResult ClearFix"><div class="ImageSection"><a href="#"><img class="SearchResultAdditionalImage" src="../../../images/events/boo.jpg?width=250&amp;height=250" alt="" /></a></div>
        <div class="Description"><span class="PublishDate"></span>
            <span id="z"><strong translate='yes'>When: </strong>October 24, 2026<br/><strong translate='yes'>Where: </strong>L6W1Y1<br/></span>
        </div></div></div></td></tr>
<tr class="rgAltRow"><td>
<div class="SearchResultSummary">
    <div class="ListItemTitle SearchResultLink">
        <a id="x2" class="ClearFix" href="../../../Event.aspx?EventKey=122126WB&amp;WebsiteKey=7be0"><span id="y2">Winter break drop-ins</span></a>
    </div>
    <div class="Description"><span><strong translate='yes'>When: </strong>December 21, 2026<br/></span></div></div></td></tr>'''

SXA = {"Results": [
    {"Url": "/events/2026/06/navigating-osap-fall", "Html": "<div><h3 class=\"field-eventname\"><a title=\"t\" href=\"/events/2026/06/navigating-osap-fall\" class=\"font-weight-bold\">Navigating OSAP: Your Guide to Applying for Financial Aid</a></h3><h4 class=\"search-result-event__date\"><span class=\"fas fa-calendar\"></span>\r\n    <span>Jun. 16, 2026</span><span>&nbsp;&ndash;&nbsp;</span><span>Oct. 27, 2026</span>\r\n</h4><p class=\"search-result-event__time mb-1\"><span class=\"fas fa-clock mr-2\"></span>\r\n  <span>3:00 p.m.</span><span> &ndash; </span><span>4:00 p.m.</span>\r\n</p><p><span class=\"fas fa-map-marked-alt mr-2\"></span><span class=\"search-result-event__location field-name\"><span>Location: </span>Online</span></p></div>"},
    {"Url": "/events/2026/11/fall-open-house", "Html": "<div><h3 class=\"field-eventname\"><a title=\"f\" href=\"/admissions/visit-sheridan/open-house\" class=\"font-weight-bold\">Fall Open House events: November 7 (Brampton, Oakville) &amp; November 12 (Mississauga)</a></h3><h4 class=\"search-result-event__date\"><span class=\"fas fa-calendar\"></span>\r\n    <span>Nov. 7, 2026</span><span>&nbsp;&ndash;&nbsp;</span><span>Nov. 12, 2026</span>\r\n</h4><p></p></div>"},
]}

START, END = dt.date(2026, 10, 20), dt.date(2027, 1, 20)

QUIC_EVENTS = [
    {"title": "Night for your Mind ", "url": "/night-your-mind-0", "start": "2026-10-22T17:30:00", "end": "2026-10-22T19:00:00"},
    {"title": "Drop-in Advising with the ISAs", "url": "/drop-advising-isas-808", "start": "2026-10-21T14:00:00", "end": "2026-10-21T15:00:00"},
    {"title": "International Education Week: Soup Lunch and Panel ", "url": "/iew-soup", "start": "2026-11-18T12:00:00", "end": "2026-11-18T13:30:00"},
    {"title": "Old thing", "url": "/old", "start": "2025-10-21T17:30:00", "end": "2025-10-21T19:00:00"},
]
QUIC_PAGE = ('<html><script type="application/json" data-drupal-selector="drupal-settings-json">'
             + json.dumps({"fullCalendarView": [{"calendar_options": json.dumps(
                 {"timeZone": "America/Toronto", "events": QUIC_EVENTS})}]}).replace("/", "\\/")
             + "</script></html>")

VK_PAGE = r'<script>window.csrfTokenName = "CRAFT_CSRF_TOKEN"; window.csrfTokenValue = "abc=";</script>'
VK_RESULTS = """<div><section><h2 class="typo-h3">Special events</h2>
<a class="group z-10 h-full focus:outline-none flex flex-col" href="https://www.visitkingston.ca/events/bees/">
<time datetime="2026-06-22" class="x"></time><img src="https://www.visitkingston.ca/media/bees.jpg" alt="">
<p class="typo-smallcaps text-magenta mb-8">History &amp; museums</p>
<p class="typo-h4 group-hover:underline" lang="en">BEES! At The PumpHouse Museum</p>
<p class="font-body text-14 text-dark m-0">June 22 - October 31, 2026<br>The PumpHouse Museum</p></a>
<a class="group z-10 h-full focus:outline-none flex flex-col" href="https://www.visitkingston.ca/events/goodlife/">
<time datetime="2026-10-22" class="x"></time>
<p class="typo-h4 group-hover:underline" lang="en">It&#8217;s a Good Life If You Don&#8217;t Weaken</p>
<p class="font-body text-14 text-dark m-0">October 22 - November 8, 2026<br>Kingston Grand Theatre</p></a>
<a class="group z-10 h-full focus:outline-none flex flex-col" href="https://www.visitkingston.ca/events/mariachi/">
<time datetime="2026-10-30" class="x"></time>
<p class="typo-h4" lang="en">Mariachi Herencia de México</p>
<p class="font-body text-14 text-dark m-0">October 30, 2026 • 7:30 pm<br>Isabel Bader Centre for the Performing Arts</p></a>
<a class="group z-10 h-full focus:outline-none flex flex-col" href="https://www.visitkingston.ca/events/mariachi-2/">
<time datetime="2026-10-30" class="x"></time>
<p class="typo-h4" lang="en">Mariachi Herencia de México</p>
<p class="font-body text-14 text-dark m-0">October 30, 2026 • 7:30 pm<br>Isabel Bader Centre</p></a>
</section><section><h2 class="typo-h3">Recurring events</h2>
<a class="group z-10 h-full flex flex-col" href="https://www.visitkingston.ca/events/trivia/">
<p class="typo-h4" lang="en">Trivia At Daft Brewing</p>
<p class="font-body text-14 text-dark m-0">Weekly on Wed • 7 pm<br>Daft</p></a></section></div>"""


class Parsers(unittest.TestCase):
    def setUp(self):
        self._ft, self._f = B.fetch_text, B.fetch

    def tearDown(self):
        B.fetch_text, B.fetch = self._ft, self._f

    def test_audienceview(self):
        pages = {1: AV_PAGE1, 2: AV_PAGE2, 3: AV_PAGE2}
        calls = []

        def fake(url, *a, **k):
            n = int(url.split("current_page=")[1]) if "current_page=" in url else 1
            calls.append(n)
            return pages.get(n, "")
        B.fetch_text = fake
        out = B.parse_audienceview({"id": "rose", "name": "The Rose",
                                    "url": "https://tickets.brampton.ca/online/"}, START, END)
        self.assertEqual([e["title"] for e in out],
                         ["STRIPPED: Behind the art", "The Great Canadiana Song Swap",
                          "Winter Lights Free Concert"])
        a, b, c = out
        self.assertEqual(a["start"], "2026-10-24 20:00")
        self.assertEqual(a["venue"], "Rose Studio, 1 Theatre Lane, Brampton")
        self.assertEqual(a["cost"], "From $20.00")
        self.assertTrue(a["url"].startswith("https://tickets.brampton.ca/online/default.asp?"))
        self.assertEqual(a["image"], "https://tickets.brampton.ca/content/Images/COB/2627TRP/TIBSTROct2426web.jpg")
        self.assertEqual(b["venue"], "St. Paul's United Church, 30 Main St. S., Brampton")
        self.assertEqual(c["start"], "2027-01-30 19:00")       # month index 0 = January
        self.assertEqual(c["cost"], "Free")
        self.assertEqual(calls, [1, 2], "stops once past the window")

    def test_imis(self):
        B.fetch_text = lambda url, *a, **k: IMIS
        out = B.parse_imis_events({"id": "pama", "name": "PAMA", "default_venue": "PAMA, Brampton",
                                   "url": "https://pama.imiscloud.com/PAMA/PAMA/Events/Events_List.aspx"},
                                  START, END)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["title"], "Halloween at the Boo-seum")
        self.assertEqual(out[0]["start"], "2026-10-24")
        self.assertEqual(out[0]["url"], "https://pama.imiscloud.com/Event.aspx?EventKey=102426BOO2&WebsiteKey=7be0")
        self.assertEqual(out[0]["image"], "https://pama.imiscloud.com/images/events/boo.jpg")
        self.assertEqual(out[1]["venue"], "PAMA, Brampton")

    def test_sitecore_sxa(self):
        B.fetch = lambda url, *a, **k: json.loads(json.dumps(SXA))
        out = B.parse_sitecore_sxa({"id": "sh", "name": "Sheridan",
                                    "url": "https://www.sheridancollege.ca//sxa/search/results/?s=x&p=50"},
                                   START, END)
        osap, oh = out
        self.assertEqual(osap["start"], "2026-10-20 15:00")     # series clipped to today
        self.assertEqual(osap["end"], "2026-10-27 16:00")
        self.assertEqual(osap["venue"], "Online")
        self.assertEqual(oh["start"], "2026-11-07 00:00")
        self.assertEqual(oh["url"], "https://www.sheridancollege.ca/admissions/visit-sheridan/open-house")
        self.assertIn("&", oh["title"])

    def test_venue_filter_and_page_cap(self):
        seen = []

        def fake_fetch(url, *a, **k):
            seen.append(url)
            ev = lambda t, city: {"title": t, "start_date": "2026-11-02 10:00:00",
                                  "end_date": "2026-11-02 11:00:00", "url": "https://a/" + t,
                                  "venue": {"venue": "Centre", "city": city} if city else []}
            return {"total_pages": 30, "events": [ev("Job fair", "Brampton"), ev("Resume Webinar", None),
                                                  ev("Toronto thing", "Toronto"),
                                                  ev("Pioneer Village Tour: In-Person", None)]}
        B.fetch = fake_fetch
        B.CITY = {"city": "Brampton", "manual_events": []}
        src = {"id": "achev", "name": "Achev", "kind": "tribe", "max_pages": 3,
               "url": "https://achev.ca/wp-json/tribe/events/v1/events",
               "venue_filter": "brampton|online", "default_category": "practical"}
        events, status = B.collect([src], START, END, log=lambda m: None)
        self.assertEqual(len(seen), 3)
        titles = {e["title"] for e in events}
        self.assertNotIn("Toronto thing", titles)
        self.assertNotIn("Pioneer Village Tour: In-Person", titles)   # no venue, not online
        self.assertTrue(titles <= {"Job fair", "Resume Webinar"})

    def test_capped_source_skips_min_score(self):
        B.fetch_text = lambda url, *a, **k: IMIS
        B.CITY = {"city": "Brampton", "manual_events": []}
        src = {"id": "pama", "name": "PAMA", "kind": "imis_events",          # capped by default
               "default_category": "social", "default_cost": "See website",
               "url": "https://pama.imiscloud.com/PAMA/PAMA/Events/Events_List.aspx"}
        events, _ = B.collect([src], START, END, log=lambda m: None)
        self.assertEqual(len(events), 2)
        self.assertTrue(all(e["capped_feed"] for e in events))
        events, _ = B.collect([dict(src, capped=False)], START, END, log=lambda m: None)
        self.assertEqual(events, [], "without the flag, unpriced museum events fall under MIN_SCORE")

    def test_drupal_fullcalendar_and_title_exclude(self):
        B.fetch_text = lambda url, *a, **k: QUIC_PAGE
        B.CITY = {"city": "Kingston", "manual_events": []}
        src = {"id": "quic", "name": "QUIC", "kind": "drupal_fullcalendar",
               "url": "https://quic.queensu.ca/event-calendar", "default_category": "practical",
               "default_venue": "QUIC, Queen's University", "title_exclude": "drop-in advising"}
        raw = B.parse_drupal_fullcalendar(src, START, END)
        self.assertEqual(raw[0]["title"], "Night for your Mind")
        self.assertEqual(raw[0]["start"], "2026-10-22 17:30")
        self.assertEqual(raw[0]["url"], "https://quic.queensu.ca/night-your-mind-0")
        events, _ = B.collect([src], START, END, log=lambda m: None)
        titles = {e["title"] for e in events}
        self.assertNotIn("Drop-in Advising with the ISAs", titles)
        self.assertNotIn("Old thing", titles)

    def test_visit_kingston(self):
        sent = {}

        class Resp:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return self.body.encode()

        class Opener:
            def open(self, req, timeout=None):
                if req.get_method() == "POST":
                    sent["body"] = json.loads(req.data)
                    return Resp(json.dumps({"data": {"results": VK_RESULTS}}))
                return Resp(VK_PAGE)
        orig = B.urllib.request.build_opener
        B.urllib.request.build_opener = lambda *a: Opener()
        try:
            out = B.parse_visit_kingston({"id": "vk", "name": "Visit Kingston",
                                          "url": "https://www.visitkingston.ca/ajax/event-results",
                                          "home": "https://www.visitkingston.ca/events/"}, START, END)
        finally:
            B.urllib.request.build_opener = orig
        self.assertEqual(sent["body"]["CRAFT_CSRF_TOKEN"], "abc=")
        self.assertEqual(sent["body"]["fromDate"], "2026-10-20")
        self.assertEqual([e["title"] for e in out],
                         ["BEES! At The PumpHouse Museum", "It’s a Good Life If You Don’t Weaken",
                          "Mariachi Herencia de México"])          # duplicate + recurring dropped
        bees, play, mar = out
        self.assertEqual(bees["start"], "2026-10-20 00:00")              # clipped to window
        self.assertEqual(bees["end"], "2026-10-31 00:00")
        self.assertEqual(play["end"][:10], "2026-11-08")
        self.assertEqual(mar["start"], "2026-10-30 19:30")
        self.assertEqual(mar["venue"], "Isabel Bader Centre for the Performing Arts")
        self.assertEqual(mar["cost"], "See website")


if __name__ == "__main__":
    unittest.main()
