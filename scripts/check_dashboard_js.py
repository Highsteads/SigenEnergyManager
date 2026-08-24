#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    check_dashboard_js.py
# Description: Runs the dashboard page's JavaScript against a payload in a stub DOM,
#              so a stale element reference fails here instead of blanking the page
# Author:      CliveS & Claude Opus 5
# Date:        24-08-2026
# Version:     1.0

"""Prove the dashboard page's JS still runs after the page has been edited.

`node --check` only parses. It cannot tell you that `update()` reaches for an
element somebody deleted three commits ago, and a browser will not tell you
either -- it throws mid-render and leaves half the cards blank, which looks like
a data problem rather than a code one.

So this builds a DOM stub whose `getElementById` returns null for any id NOT in
dashboard.html, exactly as a browser does, then runs the page's own `update()`
over a real `/api/status` payload. A stale reference throws. The only ids it is
allowed to miss are the ones the page creates at runtime.

This lives outside the unittest suite on purpose: it needs node, the suite is
defended as runnable with nothing installed, and a skipped test fails the build
here. Run it before any release that touched the page.

Usage:
    python3 scripts/check_dashboard_js.py                  # live server payload
    python3 scripts/check_dashboard_js.py path/to.json     # a saved payload

Exit codes: 0 clean, 1 the JS threw or reached for a missing element, 2 could
not run at all (no node, no payload) -- never a silent pass.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(REPO_ROOT, "SigenEnergyManager.indigoPlugin",
                    "Contents", "Server Plugin", "dashboard.html")
STATUS_URL = "http://127.0.0.1:8179/api/status"

# Ids the page builds at runtime, so a null lookup for them is correct.
RUNTIME_IDS = {"help-tip"}

HARNESS = """
const PAGE_IDS = new Set(%(ids)s);
const misses = [];
function mkEl(id) {
  return { id, textContent:'', innerHTML:'', className:'', dataset:{}, style:{},
           classList:{ add(){}, remove(){}, toggle(){}, contains(){return false} },
           setAttribute(){}, getAttribute(){return null}, appendChild(){},
           querySelector(){return mkEl('q')}, querySelectorAll(){return []},
           addEventListener(){}, getBoundingClientRect(){return {width:0,height:0,top:0,left:0}},
           closest(){return null}, remove(){} };
}
global.document = {
  body: mkEl('body'), documentElement: mkEl('html'),
  getElementById(id){ if(!PAGE_IDS.has(id)){ misses.push(id); return null; } return mkEl(id); },
  querySelector(){ return null }, querySelectorAll(){ return [] },
  createElement(t){ return mkEl(t) }, addEventListener(){},
};
global.window = { addEventListener(){}, scrollY:0,
                  matchMedia:()=>({matches:false,addEventListener(){}}) };
global.getComputedStyle = () => ({ getPropertyValue: () => '' });
global.requestAnimationFrame = (f) => { f(Date.now()+1000); return 1; };
global.performance = { now: () => Date.now() };
global.setInterval = () => 0;
global.setTimeout = () => 0;
global.fetch = async () => ({ ok:true, json: async () => ({ slots: [] }) });

%(js)s

let threw = null;
try { update(%(payload)s); } catch (e) { threw = e.stack || String(e); }
console.log(JSON.stringify({ threw, misses: [...new Set(misses)] }));
"""


def load_payload(argv):
    if argv:
        with open(argv[0], encoding="utf-8") as handle:
            return handle.read()
    try:
        with urllib.request.urlopen(STATUS_URL, timeout=8) as resp:
            return resp.read().decode("utf-8")
    except Exception as exc:
        print(f"NOT CHECKED - no payload: {STATUS_URL} unreachable ({exc}).")
        print("Start the plugin, or pass a saved /api/status JSON file.")
        return None


def main(argv):
    if not shutil.which("node"):
        print("NOT CHECKED - node is not installed.")
        return 2
    if not os.path.exists(PAGE):
        print(f"NOT CHECKED - {PAGE} not found.")
        return 2

    payload = load_payload(argv)
    if payload is None:
        return 2

    with open(PAGE, encoding="utf-8") as handle:
        html = handle.read()

    js = "\n".join(re.findall(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', html, re.S))
    ids = sorted(set(re.findall(r'id="([a-zA-Z0-9_-]+)"', html)))

    script = HARNESS % {"ids": json.dumps(ids), "js": js, "payload": payload}
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as handle:
        handle.write(script)
        path = handle.name
    try:
        result = subprocess.run(["node", path], capture_output=True, text=True)
    finally:
        os.unlink(path)

    if result.returncode != 0:
        print("NOT CHECKED - the harness itself failed to run:")
        print(result.stderr[:1000])
        return 2

    report = json.loads(result.stdout)
    unexpected = [m for m in report["misses"] if m not in RUNTIME_IDS]

    if report["threw"]:
        print("FAIL - update() threw against a real payload:\n")
        print(report["threw"][:1500])
        return 1
    if unexpected:
        print("FAIL - the page reaches for elements that do not exist:")
        for m in unexpected:
            print("   ", m)
        return 1

    print(f"OK - update() ran clean over {len(ids)} page elements.")
    if report["misses"]:
        print(f"     (runtime-created ids, as expected: {', '.join(report['misses'])})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
