# HIS Events Pipeline

Finds free, newcomer-friendly events in Canadian cities, ranks them for international
students, and turns the good ones into **draft** events on the Helping International
Students Wix site. Nothing it produces is ever published automatically.

Three cities are live: **Toronto**, **Hamilton**, **Ottawa**.

## How it fits together

```
cities/<slug>.json ─┐
                    ├─► build_board.py ──► board-<slug>.html ──► published artifact
template.html ──────┘         │                                        │
                              └─► events-<slug>.json                   │
                                                                       ▼
                                                          you pick events, export
                                                          a work order, and hand it
                                                          to the /his-event-drafter
                                                          skill, which writes the
                                                          Wix drafts
```

The board **cannot reach Wix or crawl sources itself** — a published artifact is
sandboxed from every other host. It is a decision surface: you choose events, it
hands you a work order, and the drafting happens back in Claude Code.

## Running it

```bash
python build_board.py --city toronto --months 3
```

Standard library only, Python 3.8+. No install step.

| Flag | Default | What it does |
|---|---|---|
| `--city` | `toronto` | Which `cities/<name>.json` to build |
| `--months` | `3` | Whole months ahead; the rest of the current month rides along |
| `--top` | `5` | Rows kept per stream per month |
| `--min-score` | `55` | Below this an event never reaches you |
| `--max-repeats` | `2` | Occurrences kept per identical title/source/month |
| `--from` | today | Start the window on a specific date |
| `--json` | – | Also dump the normalized events |
| `--out` | `board-<slug>.html` | Override the output path |

Rebuild everything:

```bash
for c in toronto hamilton ottawa; do python build_board.py --city $c --months 3; done
```

The window **rolls forward from the day you run it** — nothing is pinned to a quarter.

## Files

| Path | Role |
|---|---|
| `build_board.py` | Crawler, scorer, generator. Shared by every city — do not fork it. |
| `cities/<slug>.json` | Everything city-specific: Wix category IDs, sources, manual events, local venue words. |
| `template.html` | The board UI. Contains `/*__DATA__*/null` and `__CITY__`, both replaced at build. |
| `board-<slug>.html` | **Generated.** Do not edit — the next build overwrites it. |
| `events-<slug>.json` | **Generated.** The normalized event set, handy for debugging. |

To change how the board looks or behaves, edit `template.html` — all three cities
get it. To change what appears on it, edit the city's JSON.

## Adding a city

Use the `his-create-city` skill (`~/.claude/skills/his-create-city/`). Its
`references/city-onboarding.md` carries the config schema, the feed-probe playbook
and the traps. In short: find the Wix category IDs, hunt 6–12 local sources, write
`cities/<slug>.json`, build, sanity-check, publish as its own artifact.

## Scoring

100 points: cost 25 · fits a newcomer 25 · fits a student 25 · how reachable it is 15 ·
whether it helps someone meet people 10. Anything under `--min-score` is dropped
before it reaches you. Hand-entered (`manual`) events get a floor of 72 because a
human already vetted them — without it, marquee events like Nuit Blanche score as
"art" rather than "newcomer" and vanish.

## Source kinds

| `kind` | What it means |
|---|---|
| `tribe` | The Events Calendar REST — most WordPress orgs |
| `sqs` | Squarespace `?format=json` (dates are epoch ms) |
| `communico` | Communico / libnet library calendars |
| `opl` | Ottawa Public Library's Drupal 7 booking site (HTML scrape) |
| `manual` | Dated events written by hand into the city file |
| `probe` | Registered but not yet parseable — reported as blocked every run |

`probe` is deliberate. A source we cannot read yet stays visible as a to-do rather
than quietly vanishing.

## Things that will bite you

- **Retry a 403 with browser headers before writing a source off.** CultureLink was
  recorded as permanently blocked for months; it only ever needed a User-Agent.
- **Dedup on url + date, never url alone.** Multi-day festivals share one page and
  each day has to survive as its own draft.
- **Classify from the title, not the description.** Matching "service" anywhere filed
  "Employment Services Intake" under Spiritual.
- **OPL's pager is `page=0,N`, not `page=N`.** A plain `page=N` silently re-serves
  page one, which looks exactly like "this library only has 20 events".
- **Publish artifacts with `capabilities={}`.** Declaring any capability — even
  `downloads` — makes the page unshareable. The export uses a selectable textarea
  because a shared artifact sandbox blocks downloads, `navigator.clipboard` *and*
  `execCommand("copy")`.
- **One artifact per city**, never redeploy one city over another's URL. Saved picks
  are namespaced `his-board-<slug>-<date>` and event ids are `<slug>-e<n>` for the
  same reason.
- **Spiritual will be thin.** Churches publish standing weekly programmes, not dated
  one-offs. Register them as `manual` and hand-enter real gatherings; never invent dates.

## Rules for the Wix drafts

Enforced by the `his-event-drafter` skill, which is the **only** thing that should
write to Wix:

1. Title is `<City>: <D Mon>, <Title>`.
2. One dated event per date. Never Wix recurring events.
3. Registration is EXTERNAL, pointing at the organizer's page.
4. Categories are base `<City>` + the type. **Never `<City> - HIS`.**
5. Times in the city's timezone — check whether the feed reports local or UTC.
6. Images: the event's own photo → the org's homepage photo → the category default.
7. Dedup against existing Wix events, including drafts, before creating anything.

## Known gaps

- **Ottawa has no base `Ottawa` category** on the Wix site, so its work orders carry
  only the type category. Awaiting a decision on creating it.
- **Ottawa spiritual is empty.** Two ministries HIS already runs there (P2C English
  Corner, Gospel Hall English classes) have no feeds and need hand-entered dates.
- **uOttawa International Office** is the highest-value unparsed source; it needs a
  scraper. Carleton, Algonquin, OCISO, CCI and World Skills are also pending.
- **McMaster Student Success Centre** (Hamilton) has 575 events and no feed endpoint.
