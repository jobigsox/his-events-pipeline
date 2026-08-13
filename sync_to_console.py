#!/usr/bin/env python3
"""Sync city config and crawl results with the his-events-console app.

    python sync_to_console.py --export-configs   # BEFORE build_all.py
    python sync_to_console.py --ingest-events    # AFTER build_all.py

The console holds the live source registry; this pulls it down so edits made
there are what actually get crawled, then pushes the results back.

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


def export_configs():
    """Overwrite each cities/<slug>.json with the console's copy, on success only."""
    if not configured():
        return
    for fn in sorted(os.listdir(CITIES)):
        if not fn.endswith(".json"):
            continue
        slug = fn[:-5]
        try:
            cfg = call("GET", "/api/cities/%s/export" % slug)
        except Exception as e:                      # noqa: BLE001 - never block the crawl
            print("::warning::export failed for %s (%s) - using committed config" % (slug, e))
            continue
        if not cfg.get("sources"):
            print("::warning::%s came back with no sources - keeping committed config" % slug)
            continue
        with open(os.path.join(CITIES, fn), "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        print("  synced cities/%s (%d sources)" % (fn, len(cfg["sources"])))


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

    if "--export-configs" in sys.argv:
        export_configs()
    elif "--ingest-events" in sys.argv:
        ingest_events()
    else:
        sys.exit("specify --export-configs or --ingest-events")
