# HIS Events — custom instructions for a claude.ai Project

Paste this whole file into the Project's **custom instructions** box (or add it as the
first knowledge file). It replaces the local Claude Code memory and skills, which do
not travel to claude.ai.

---

## What this project is

I curate free, newcomer-friendly events in Canadian cities for international students,
and create them as **draft** events in the Helping International Students Wix site.

**Site:** HIS Events · `siteId a8184ab6-cb25-4a52-8c23-1eb37f03bc6b`
Live cities: Toronto, Hamilton, Ottawa.

## Absolute rules

1. **Never publish an event. Ever.** Everything is created with `draft: true` and a
   human publishes it. Publishing a draft is irreversible.
2. **Confirm with me before any write to Wix** — creates, updates, deletes, image
   uploads. Reads never need confirmation.
3. **Never assign the `<City> - HIS` category.** Standing instruction.
4. **Dedup before creating.** Query existing Wix events for the window across all
   statuses including DRAFT, and skip anything whose external URL, or title+date,
   already exists.

## Draft format

1. **Title:** `<City>: <D Mon>, <Title>` — abbreviated month, comma after the date.
   e.g. `Toronto: 7 Aug, Habari Africa Festival`. If the source title is generic, add
   the distinguishing detail: `Noon Hour Concert: Basement Revolver`, `Free Flicks: Chicago`.
2. **One dated event per date. Never use Wix recurring events** — they share one name
   across occurrences, so later dates would show the wrong date in the title.
3. **Registration = EXTERNAL**, pointing at the organizer's own event page.
4. **Categories:** base `<City>` + the type (`<City> - Social` / `- Practical` /
   `- Spiritual`). Never `<City> - HIS`.
5. **Times in America/Toronto.** Check whether the source reports local or UTC —
   ACCES Employment reports GMT, most others report local.
6. **Images:** the event's own photo → the organizer's homepage photo → the
   per-category default. Skip a logo-only homepage; the default banner looks better.

## Wix IDs

Default category images (site-wide):
- social `ff627a_27c471962388496492eb61568dda0041~mv2.png`
- practical `ff627a_f1cf2e1967bd4b6cab2f85f86f661387~mv2.png`
- spiritual `ff627a_702a37714e4c46acbae72e93182651b1~mv2.png`

| City | base | Social | Practical | Spiritual |
|---|---|---|---|---|
| Toronto | `91005a8a-7c01-4cb7-8c3f-f80686b0db78` | `02525157-3ee2-4f35-94ef-4b324ca39c83` | `ac1c5c03-b6c8-47b6-bb03-1c6ea7ae81e5` | `697a1913-d42e-4366-a7d1-18d8f1e5041d` |
| Hamilton | `073779bb-8dab-4f4c-9647-272c6eac12ad` | `dd96132e-7503-410c-9584-462f17868c17` | `5e9144f6-0c5e-4cd3-8905-0de4eb521609` | `919e0652-b7fe-4a4d-a531-e15a88a56061` |
| Ottawa | **none exists** | `a80505a4-a8d0-42fd-b2ea-01bbbb201533` | `174ca97a-4015-4954-b73b-54157628efe8` | `208e0e3d-0b8e-47f4-9e8a-03fc9a7472ee` |

Never use: Toronto - HIS `d5aef93e-…`, Hamilton - HIS `db926ff8-…`, Ottawa - HIS `6bb02147-…`.

The site has ~178 real categories across 23 cities, plus `comp-*` UI junk. The
category query caps at 100 per page — **paginate**, or a city will look like it has
no categories at all.

## Wix Events v3 recipes

Create a draft — `POST https://www.wixapis.com/events/v3/events`:

```json
{ "event": {
    "title": "Toronto: 7 Aug, Habari Africa Festival",
    "location": { "name": "Harbourfront Centre", "type": "VENUE",
      "address": { "country":"CA", "city":"Toronto",
        "streetAddress": { "number":"235", "name":"Queens Quay West" } } },
    "dateAndTimeSettings": { "startDate":"2026-08-07T16:00:00Z",
      "endDate":"2026-08-07T22:00:00Z", "timeZoneId":"America/Toronto" },
    "shortDescription": "one-line teaser",
    "description": { "nodes": [ "…Ricos nodes…" ] },
    "registration": { "initialType":"RSVP", "rsvp": { "responseType":"YES_AND_NO" } },
    "eventDisplaySettings": { "hideEventDetailsPage": false }
  },
  "draft": true }
```

Then make registration EXTERNAL — `PATCH /events/v3/events/{id}`:

```json
{ "event": { "id":"{id}", "revision":"{current}",
    "dateAndTimeSettings": { "…resend unchanged…" },
    "registration": { "initialType":"RSVP", "type":"EXTERNAL",
      "external": { "url":"https://organizer/event" }, "registrationDisabled": false },
    "mainImage": { "id":"…", "url":"…", "width":400, "height":400, "altText":"…" } } }
```

Then categories — `POST /events/v1/bulk/categories/events`:
`{ "categoryId": ["<type id>", "<base id>"], "eventId": ["<one event id>"] }`

### Gotchas that cause 400s

- You **cannot create** an event with `type: EXTERNAL` or `NONE`. Create with
  `initialType: RSVP`, then PATCH.
- **Always resend `dateAndTimeSettings` on every PATCH.** Update revalidates the whole
  event; omit the dates and it 400s with "startDate.isDefined must be true".
- Refresh `revision` before each PATCH.
- The bulk-categories endpoint takes many categories but **exactly one event** — loop.
- `location.name` max length is **50 characters**.
- `GET /events/v3/events/{id}?fields=REGISTRATION,CATEGORIES` (combined) 400s. Ask for
  one field, or list categories separately.
- Image upload needs a `file_id` string per image.

## Curation criteria

Free or cheap, easy to reach by transit, open to drop-ins, and either socially
connecting (festivals, meetups, outings), practically useful (immigration, health,
jobs, finance, settlement, ESL) or spiritually welcoming (young-adult groups, ESL
cafés). When torn between Social and Practical, pick by primary purpose — connect
vs. get something done. Church and faith events are Spiritual.

Scoring used by the generator, out of 100: cost 25 · fits a newcomer 25 · fits a
student 25 · reachability 15 · helps someone meet people 10.

## The workflow in claude.ai

The crawler (`build_board.py`) needs a Python runtime and cannot run here. So:

1. The board is rebuilt in Claude Code (or wherever Python runs) and published.
2. On the board, pick events and hit **Export work order** — it gives JSON.
3. Paste that JSON here and say: *create these as Wix drafts.*
4. I dedup, confirm the batch with you, then create each as a draft, set EXTERNAL
   registration, assign categories, set the image, and verify each result.

Work order shape: `{ site, window, approved: [ { wixTitle, date, time, timeZoneId,
venue, cost, category, wixCategoryIds, registration: {type, url}, mainImage, draft } ] }`

## Known gaps

- Ottawa has no base category, so its drafts carry only the type category.
- Ottawa Spiritual is empty — P2C English Corner and the Gospel Hall English classes
  have no feeds and need hand-entered dates.
- Unparsed sources: uOttawa International Office (highest value), Carleton ISSO,
  Algonquin, OCISO, Catholic Centre for Immigrants, World Skills, McMaster SSC.
- Spiritual is thin everywhere — churches publish standing weekly programmes, not
  dated one-offs. Never invent dates for a standing programme.
