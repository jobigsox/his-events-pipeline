---
name: his-fix-city-lists
description: Fix a city page on events.helpinginternationalstudents.com whose Social / Practical / Spiritual event lists are empty ("No events at the moment") while other cities like Barrie show events - e.g. "make Halton look like Barrie", "events aren't showing on the <city> page", "fix the <city> lists". Diagnoses uncategorised Wix events and blank city category IDs, assigns the existing events, and fixes the cause so future drafts land in the right list.
---

# Fix a city page's empty Social / Practical / Spiritual lists

Site: HIS Events Wix site `a8184ab6-cb25-4a52-8c23-1eb37f03bc6b` (use the HIS Wix
connector, `CallWixSiteAPI` / `ExecuteWixAPI`). Repos: `his-events-pipeline`
(crawler, `cities/<slug>.json`) and `his-events-console` (Vercel app).

## How the lists work

Every city page has, under **Upcoming Events**, an "Event hosted by HIS" box (see
the `his-feature-event` skill) and then three lists: **Spiritual, Practical,
Social**. Each list shows the published events in one Wix category:

| Page | Categories the lists read |
|---|---|
| Barrie | `Barrie - Social` / `- Practical` / `- Spiritual` |
| Halton (`/halton`) | `Halton Region - Social` `18bf74df-ac36-44a6-9dc8-e00c39b230fb` / `- Practical` `617d6110-e81c-4d4f-bfa3-1a8d042f01c9` / `- Spiritual` `2fea3459-c8bc-444e-91b1-5ba18b12d569` |

Category names don't always match the console slug (console `halton-peel` →
Wix "Halton Region"; page `/halton`).

Events get their category when the console sends a draft to Wix: it uses the
event's `wixCategoryIds`, which the crawler builds from the city's
`wix.social|practical|spiritual` IDs. **If those IDs are blank, every draft goes
to Wix uncategorised and never shows in any list** - this is what broke Halton
(its console row was created while the site was at the Wix category cap).

## Diagnose

1. **Is the page laid out like Barrie?** Scrape `https://events.helpinginternationalstudents.com/<page>`
   (Firecrawl, `formats: ["rawHtml"]`, `maxAge: 0`) and list headings and
   `"widgetcomp-…":{"events":{"events":[…]` blocks in order. You should see
   Upcoming Events → Event hosted by HIS → widget → Spiritual → widget →
   Practical → widget → Social → widget. If a section is missing, that's an
   Editor job (copy it from Barrie's page), not this skill.

2. **Do the city's categories hold events?**
   `POST https://www.wixapis.com/events/v1/categories/query`
   ```json
   {"query": {"filter": {"name": {"$startsWith": "<Wix city name>"}}, "paging": {"limit": 20}},
    "fieldset": ["COUNTS"]}
   ```
   No `assignedEventsCount` = 0 events. Compare with `Barrie - Social` (~20).

3. **Are there published events with no category?**
   `POST https://www.wixapis.com/events/v3/events/query`
   ```json
   {"query": {"filter": {"title": {"$startsWith": "<Console city name>:"}}, "paging": {"limit": 100}},
    "fields": ["CATEGORIES"], "includeDrafts": true}
   ```
   Drafted titles start with the console city name (e.g. `Halton-Peel: 5 Nov, …`).
   Events with no `categories` block are the ones missing from the page.

4. **Are the city's IDs blank?** Check `cities/<slug>.json` → `wix.social /
   practical / spiritual`, and the crawl output: every event in
   `events-<slug>.json` should have non-empty `wixCategoryIds`.

## Fix the existing events

1. Get each event's type from the latest crawl output (it has `cat` and
   `wixTitle` for every event):
   ```bash
   gh run download <run-id> --repo jobigsox/his-events-pipeline -n boards -D <scratch dir>
   ```
   (`gh run list --repo jobigsox/his-events-pipeline` for a recent successful run.)
2. Match each uncategorised Wix event's `title` to a crawl event's `wixTitle`
   and group the Wix event ids by `cat`. Every event should match; ask before
   guessing for any that don't.
3. Assign each group (up to 100 ids per call):
   `POST https://www.wixapis.com/events/v1/categories/<categoryId>/events`
   ```json
   {"eventId": ["<id>", "<id>"]}
   ```
4. Re-run Diagnose step 2: the counts should rise by the number assigned.
5. Re-check the live page. Wix's page cache takes **5-15 minutes** (Halton took
   ~15). Don't reassign while waiting.

Do not publish, unpublish, edit or delete the events themselves - only add
categories. Publishing is a human step in the Wix dashboard.

## Fix the cause (future drafts)

1. Put the real IDs in `cities/<slug>.json` → `wix.social|practical|spiritual`
   (and update any stale `_wix_note`).
2. The console wins over the file for city settings, **except** that the source
   sync fills a console ID that is blank or not a UUID from the file
   (`his-events-console` `src/app/api/cities/[slug]/sources/sync/route.ts`,
   `wixFilled` in the response; `sync_to_console.py` logs
   `::notice::<slug>: filled blank Wix category IDs`). A real ID already in the
   console is never overwritten.
3. Commit and push the city file to `main`; the push starts a crawl whose sync
   fills the console. Check the run log for the notice. From then on the
   crawl's `wixCategoryIds` are set and new drafts land in the right list.

## Gotchas

- An empty **Spiritual** list can be correct: Halton-Peel's crawl currently has
  no spiritual events at all (302 = 264 practical + 38 social).
- If categories hold events and the cache has had 15+ minutes but a list is
  still empty, the list widget itself isn't filtering by that category. Fix in
  the Wix Editor: click the list → **Display Events** → pick the matching
  `<City> - Social|Practical|Spiritual` category (as on Barrie) → **Publish**.
- A city whose Wix categories don't exist yet: creating categories is capped
  (the site has hit the 100-category limit before) - check with the user before
  creating any.
