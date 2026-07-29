#!/usr/bin/env python3
"""Rebuild every city in cities/ in one go.

    python build_all.py              # 3-month window from today
    python build_all.py --months 4   # any build_board.py flag passes through
"""

import datetime as dt
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CITIES = os.path.join(HERE, "cities")

INDEX = """<!doctype html>
<html lang="en"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta name="robots" content="noindex" />
<title>HIS Curation Boards</title>
<style>
  :root {{ color-scheme: light dark;
    --paper:#EDEEF1; --card:#fff; --ink:#14161F; --ink-2:#3A3F4E; --ink-3:#5F6575; --edge:#C9CDD8; }}
  @media (prefers-color-scheme: dark) {{ :root {{
    --paper:#15171E; --card:#1E212B; --ink:#F0F1F5; --ink-2:#BFC4D2; --ink-3:#9AA0B0; --edge:#343948; }} }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--paper); color:var(--ink); min-height:100vh;
    font:16px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; padding:56px 24px; }}
  .wrap {{ max-width:760px; margin:0 auto; }}
  h1 {{ font:400 clamp(34px,5vw,48px)/1.05 "Palatino Linotype",Palatino,Georgia,serif;
    letter-spacing:-.02em; margin:0 0 10px; }}
  p.lead {{ color:var(--ink-2); margin:0 0 36px; max-width:56ch; }}
  a.city {{ display:flex; align-items:baseline; gap:14px; text-decoration:none; color:inherit;
    background:var(--card); border:1px solid var(--edge); border-radius:12px;
    padding:20px 24px; margin-bottom:12px; }}
  a.city:hover {{ border-color:var(--ink-3); }}
  a.city b {{ font:400 24px/1 "Palatino Linotype",Palatino,Georgia,serif; }}
  a.city span {{ color:var(--ink-3); font-size:14px; margin-left:auto; }}
  footer {{ margin-top:36px; color:var(--ink-3); font-size:14px; }}
  code {{ font-family:ui-monospace,Consolas,monospace; font-size:13px; }}
</style></head><body><div class="wrap">
<h1>HIS curation boards</h1>
<p class="lead">Free, newcomer-friendly events ranked for international students.
Pick what should become a Wix draft, then export the work order. Nothing here publishes anything.</p>
{cards}
<footer>Rebuilt {stamp} · <code>python build_all.py --months 3</code></footer>
</div></body></html>
"""


def write_index(slugs):
    cards = []
    for s in slugs:
        n = ""
        path = os.path.join(HERE, "events-%s.json" % s)
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    n = "%d events" % len(json.load(f).get("events", []))
            except Exception:                      # noqa: BLE001 - label is cosmetic
                pass
        cards.append('<a class="city" href="board-{s}.html"><b>{t}</b><span>{n}</span></a>'
                     .format(s=s, t=s.title(), n=n))
    html = INDEX.format(cards="\n".join(cards),
                        stamp=dt.datetime.now().strftime("%d %b %Y, %H:%M"))
    with open(os.path.join(HERE, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    print("  wrote index.html")


def main():
    slugs = sorted(f[:-5] for f in os.listdir(CITIES) if f.endswith(".json"))
    if not slugs:
        sys.exit("No city configs in cities/")

    passthrough = sys.argv[1:] or ["--months", "3"]
    failed = []

    for slug in slugs:
        print("\n=== %s ===" % slug)
        # always emit the per-city JSON: CI reads it to summarise the run
        r = subprocess.run(
            [sys.executable, os.path.join(HERE, "build_board.py"), "--city", slug,
             "--json", os.path.join(HERE, "events-%s.json" % slug)] + passthrough,
            cwd=HERE)
        if r.returncode != 0:
            failed.append(slug)

    write_index([s for s in slugs if s not in failed])

    print("\n%d/%d built" % (len(slugs) - len(failed), len(slugs)))
    if failed:
        print("failed: " + ", ".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
