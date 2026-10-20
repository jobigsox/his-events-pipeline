#!/usr/bin/env python3
"""Sync city config and crawl results with the his-events-console app.

    python sync_to_console.py --export-configs   # BEFORE build_all.py (pushes, then merges)
    python sync_to_console.py --ingest-events    # AFTER build_all.py
    python sync_to_console.py --push-sources     # just the push, e.g. by hand

The source registry only ever GROWS. A source leaves the crawl in exactly one
way: someone clicks Remove in the console, which leaves a tombstone.

  --push-sources    adds every source / manual event in the committed
                    cities/*.json that the console doesn't have yet. Never
                    edits, deletes or revives anything in the console.
  --export-configs  MERGES the console's registry with the committed file:
                    console rows win on conflicts, file-only rows are kept,
                    and only tombstoned (explicitly removed) rows are dropped.
                    "Missing from the console" never means "delete".

Nothing here is allowed to break the crawl. If the console is unreachable the
committed cities/*.json is used as-is and the run continues -- a stale board is
far better than no board. Standard library only, like the rest of the pipeline.
"""

import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CITIES = os.path.join(HERE, "cities")
BASE = os.environ.get("CONSOLE_URL", "").rstrip("/")
SECRET = os.environ.get("INGEST_SECRET", "")


def call(method, path, body=None, timeout=30):
    req = urllib.request.Request(
        BASE + path,
        method=method,
        headers={"x-ingest-secret": SECRET, "Content-Type": "application/json"},
        data=json.dumps(body).encode("utf-8") if body is not None else None,
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def configured():
    if not BASE or not SECRET:
        print("::warning::CONSOLE_URL/INGEST_SECRET not set - skipping console sync")
        return False
    return True


def manual_key(e):
    """Must match manualKey() in his-events-console/src/lib/source-config.ts."""
    return "%s|%s|%s" % (e.get("source_id", ""), (e.get("title") or "").strip().lower(),
                         (e.get("start") or "")[:16])


SOURCE_LIST_KEYS = ("sources", "manual_events", "removed_sources", "removed_manual_events")


def merge_config(file_cfg, console_cfg):
    """Union of the committed file and the console's export.

    - Console version wins for any source id both have (console edits stick).
    - File-only sources are KEPT (the console may not have them yet, or the
      push failed this run) - this is what stops a city losing its sources the
      moment the console gains its first one.
    - Only ids the console lists in removed_sources are dropped.
    Returns (merged_cfg, stats).
    """
    removed = set(console_cfg.get("removed_sources") or [])
    removed_ev = {manual_key(e) for e in console_cfg.get("removed_manual_events") or []}

    merged = {k: v for k, v in file_cfg.items() if k not in SOURCE_LIST_KEYS}
    for k, v in console_cfg.items():
        if k not in SOURCE_LIST_KEYS and v not in (None, "", [], {}):
            merged[k] = v

    con = {s["id"]: s for s in console_cfg.get("sources") or [] if s.get("id")}
    out, used, file_only, dropped = [], set(), [], []
    for s in file_cfg.get("sources") or []:
        sid = s.get("id")
        if sid in removed:
            dropped.append(sid)
            continue
        if sid in con:
            out.append(con[sid])
            used.add(sid)
        else:
            out.append(s)
            file_only.append(sid)
    for sid, s in con.items():
        if sid not in used and sid not in removed:
            out.append(s)
    merged["sources"] = out

    live_ids = {s.get("id") for s in out}
    events, seen = [], set()
    for e in (console_cfg.get("manual_events") or []) + (file_cfg.get("manual_events") or []):
        k = manual_key(e)
        if k in seen or k in removed_ev or e.get("source_id") not in live_ids:
            continue
        seen.add(k)
        events.append(e)
    merged["manual_events"] = events

    return merged, {"sources": len(out), "file_only": file_only, "removed": dropped,
                    "console": len(con), "manual_events": len(events)}


def city_files():
    for fn in sorted(os.listdir(CITIES)):
        if fn.endswith(".json"):
            yield fn[:-5], os.path.join(CITIES, fn)


def push_sources():
    """Grow the console from the committed files. Add-only on the server side."""
    if not configured():
        return
    for slug, path in city_files():
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        body = {"sources": cfg.get("sources") or [], "manual_events": cfg.get("manual_events") or []}
        try:
            res = call("POST", "/api/cities/%s/sources/sync" % slug, body, timeout=120)
        except urllib.error.HTTPError as e:
            print("::warning::source push failed for %s (HTTP %s) - export merge still keeps "
                  "the file's sources" % (slug, e.code))
            continue
        except Exception as e:                      # noqa: BLE001 - never block the crawl
            print("::warning::source push failed for %s (%s)" % (slug, e))
            continue
        added = res.get("addedSources") or []
        kept = res.get("skippedRemoved") or []
        upgraded = res.get("upgradedSources") or []
        print("  pushed %s: +%d sources%s, +%d manual events%s%s"
              % (slug, len(added), (" (%s)" % ", ".join(added)) if added else "",
                 res.get("addedEvents", 0),
                 ("; parser now set for: %s" % ", ".join(upgraded)) if upgraded else "",
                 ("; left removed: %s" % ", ".join(kept)) if kept else ""))


def export_configs():
    """Rewrite each cities/<slug>.json as the MERGE of console + committed file.

    Pushes the file's new sources up first, so the console grows on every run
    even where the workflow only calls --export-configs.
    """
    if not configured():
        return
    push_sources()
    for slug, path in city_files():
        with open(path, encoding="utf-8") as f:
            file_cfg = json.load(f)
        try:
            console_cfg = call("GET", "/api/cities/%s/export" % slug)
        except Exception as e:                      # noqa: BLE001 - never block the crawl
            print("::warning::export failed for %s (%s) - using committed config" % (slug, e))
            continue
        merged, st = merge_config(file_cfg, console_cfg)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2, ensure_ascii=False)
        print("  merged cities/%s.json: %d sources (%d from console, %d file-only%s), %d manual events"
              % (slug, st["sources"], st["console"], len(st["file_only"]),
                 ("; removed in console: %s" % ", ".join(st["removed"])) if st["removed"] else "",
                 st["manual_events"]))
        if st["file_only"]:
            print("::notice::%s: %d source(s) not in the console yet, kept from the file: %s"
                  % (slug, len(st["file_only"]), ", ".join(st["file_only"])))


def ingest_events():
    """POST each events-<slug>.json produced by build_all.py to the console."""
    if not configured():
        return
    failed = []
    for fn in sorted(os.listdir(HERE)):
        if not (fn.startswith("events-") and fn.endswith(".json")):
            continue
        slug = fn[len("events-"):-len(".json")]
        with open(os.path.join(HERE, fn), encoding="utf-8") as f:
            payload = json.load(f)
        try:
            res = call("POST", "/api/cities/%s/ingest" % slug, payload, timeout=120)
            print("  ingested %s: %d events, %d retired"
                  % (slug, res.get("eventsIngested", 0), res.get("retired", 0)))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            print("::warning::ingest failed for %s (HTTP %s: %s)" % (slug, e.code, detail))
            failed.append(slug)
        except Exception as e:                      # noqa: BLE001 - report, don't fail the job
            print("::warning::ingest failed for %s (%s)" % (slug, e))
            failed.append(slug)
    if failed:
        print("::warning::console is stale until the next run for: %s" % ", ".join(failed))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                               # noqa: BLE001 - older Python / odd streams
        pass

    if "--push-sources" in sys.argv:
        push_sources()
    elif "--export-configs" in sys.argv:
        export_configs()
    elif "--ingest-events" in sys.argv:
        ingest_events()
    else:
        sys.exit("specify --push-sources, --export-configs or --ingest-events")
