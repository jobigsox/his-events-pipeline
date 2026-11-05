---
name: his-feature-event
description: Put a specific Wix event (e.g. the Friendship Families wait list, a community dinner) into a city page's "Event hosted by HIS" section on events.helpinginternationalstudents.com. Use when asked to add, feature, show or pin an event "under Upcoming Events / Event hosted by HIS" on a city page (Barrie, Hamilton, Edmonton, ...), or to do "the same as Hamilton" for another city.
---

# Feature an event in a city's "Event hosted by HIS" section

Every city page on the HIS Events Wix site (site id `a8184ab6-cb25-4a52-8c23-1eb37f03bc6b`)
has an **Upcoming Events → Event hosted by HIS** block. That block is a Wix Events
list widget set to **Custom** ("show specific events"), separate from the
Social / Practical / Spiritual widgets below it, which filter by the
`<City> - Social|Practical|Spiritual` categories.

A Custom widget stores its picked events as a hidden Wix Events category
named after the widget's component id, e.g. `comp-mp1n1l8n` (state `COMPONENT`).
Ticking an event in the Editor and adding the event to that category are the
same thing.

You need: the event's link (`https://events.helpinginternationalstudents.com/event-details/<slug>`)
and the city.

## Route A: Wix API (preferred when Claude does it; no publish needed)

Use the HIS Wix connector (`CallWixSiteAPI` / `ExecuteWixAPI`, siteId above).

1. **Find the event.** `POST https://www.wixapis.com/events/v3/events/query`
   ```json
   {"query": {"filter": {"slug": {"$eq": "<slug from the link>"}}, "paging": {"limit": 10}},
    "fields": ["DETAILS", "CATEGORIES"]}
   ```
   Take `events[0].id`. Its `categories` list shows which `comp-…` widgets already
   feature it.

2. **Find the city's HIS widget id.** Scrape the city page's raw HTML
   (`https://events.helpinginternationalstudents.com/<city-slug>`, e.g. with
   Firecrawl `formats: ["rawHtml"]`, `maxAge: 0`) and:
   - find `hosted by HIS`, then the first `id="comp-…"` that is an events widget
     after it, **and**
   - confirm in the page data that `"widgetcomp-<id>"` has `"filterType":2`
     (Custom). The Social/Practical/Spiritual widgets come after it.

   Known widgets (re-check if a page was rebuilt):

   | City | Widget | Category id |
   |---|---|---|
   | Barrie | `comp-mp1n1l8n` | `0d74464c-efc9-4c42-9491-378e3cb5a61c` |
   | Hamilton | `comp-mg5c6kup` | `4979417c-492c-4027-bdc1-a32c009cb255` |

3. **Get the widget's category id** (if not in the table):
   `POST https://www.wixapis.com/events/v1/categories/query`
   ```json
   {"query": {"filter": {"name": {"$eq": "comp-<id>"}}, "paging": {"limit": 5}}, "fieldset": ["COUNTS"]}
   ```

4. **Add the event.** `POST https://www.wixapis.com/events/v1/categories/<categoryId>/events`
   ```json
   {"eventId": ["<event id>"]}
   ```
   Re-run step 3: `assignedEventsCount` should go up by one.

5. **Verify the live page.** It shows within a few minutes (Wix page cache;
   Barrie took ~5). Re-scrape the city page and check the event title appears
   right after "Event hosted by HIS" instead of "No events at the moment".

To remove an event later:
`DELETE https://www.wixapis.com/events/v1/categories/<categoryId>/events?eventId=<event id>`
(confirm with the user first).

## Route B: Wix Editor (what a person does by hand)

1. Wix Editor → **Page:** dropdown (top left) → pick the city (e.g. Edmonton).
2. Scroll to **Upcoming Events → Event hosted by HIS** and click the events block
   under the heading (it's labelled **Wix Events**).
3. Right-click it / use the floating buttons → **Display Events**.
4. Choose **Custom** → **Select event**, search (e.g. `wait`), tick the right
   occurrence → **Confirm**.
5. **Publish** (top right). Editor changes are not live until published.

## Gotchas

- **Recurring events list several dates.** The Friendship Families wait list has
  occurrences on e.g. Oct 8 2026, Dec 22 2026 and Mar 25 2027, each with its own
  slug (`…-2026-12-22-07-00`). Use the slug from the link you were given; in the
  Editor tick the matching date.
- **An empty Custom widget shows Wix demo events in the Editor** (e.g. "Soul
  Concert", "Modern Ballet"). On the live site it says "No events at the moment".
  Neither means it's misconfigured.
- If the widget's `filterType` is **not** 2 (it filters by category instead of
  Custom), adding to its `comp-…` category does nothing visible. Use Route B and
  switch Display Events to Custom first.
- Never put an event in a `<City> - HIS` category and never touch `RECURRING_EVENT`
  categories. The `comp-…` category is the only one this task changes.
- Don't create or edit the event itself here; that's the drafting flow
  (`his-events-console` "Send to Wix"), and drafts must stay drafts.
