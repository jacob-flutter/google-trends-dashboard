#!/usr/bin/env python3
"""
Flutter UKI industry demand dashboard - Google Trends fetcher (United Kingdom only).

Pulls every group in config.json from Google Trends, merges it into
data/trends.json, and rebuilds data/trends.js + data/fixtures.js so the
dashboard (index.html) works on GitHub Pages, any static host, or straight
from disk (double-click index.html).

No third-party packages: standard-library Python 3.9+ only.

Usage
  python3 scripts/fetch_trends.py              # refresh whatever is stale
  python3 scripts/fetch_trends.py --force      # refresh everything
  python3 scripts/fetch_trends.py --only d1,d2 # refresh named groups
  python3 scripts/fetch_trends.py --build-only # no network, just rebuild the .js files

Environment (all optional)
  SERPAPI_KEY       use SerpApi's Google Trends engine as a fallback when
                    Google rate-limits us (not used for the year-on-year daily
                    pulls, which need Google's compare-time-periods mode)
  TRENDS_PROVIDER   "google" (default), "serpapi" (SerpApi only) or "auto"
                    (google first, SerpApi on block; this is the default when
                    SERPAPI_KEY is set)
  HTTPS_PROXY       standard proxy variable, honoured by urllib
  TRENDS_MIN_SLEEP / TRENDS_MAX_SLEEP   seconds between requests (default 6/14)

How it avoids getting blocked
  * one warm-up page load to collect Google's cookie, then reuse it
  * browser-like headers, a single session, no parallel requests
  * a randomised 6-14 s pause between every request
  * on HTTP 429 it backs off (60 s, 120 s, 240 s, with jitter) and retries
  * a circuit breaker: after 3 groups in a row are blocked it stops calling
    Google for the rest of the run (hammering a 429 is what gets IPs flagged)
  * each group has a time-to-live, so 5-year weekly data is only re-pulled
    about once a week and only the 90-day daily groups are pulled every day
    (about 20 requests on a normal day: 16 year-on-year terms + 2 daily groups)
  * a failed group never overwrites good data: the last good copy is kept
    and marked stale, and the dashboard shows which groups are stale
"""

from __future__ import annotations

import argparse
import datetime as dt
import http.cookiejar
import json
import re
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CONFIG = ROOT / "config.json"
TRENDS_JSON = DATA / "trends.json"
STATUS_JSON = DATA / "status.json"
FIXTURES_JSON = DATA / "fixtures.json"
MATCHES_JSON = DATA / "matches.json"
ESPN = "https://site.api.espn.com/apis/site/v2/sports/soccer/{code}/scoreboard?dates={ymd}"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
MIN_SLEEP = float(os.environ.get("TRENDS_MIN_SLEEP", "6"))
MAX_SLEEP = float(os.environ.get("TRENDS_MAX_SLEEP", "14"))
BACKOFF = [60, 120, 240]
BREAKER_LIMIT = 3


class Blocked(Exception):
    """Google answered 429 (or similar) after all retries."""


def log(msg: str) -> None:
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pause() -> None:
    time.sleep(random.uniform(MIN_SLEEP, MAX_SLEEP))


def strip_xssi(text: str) -> dict:
    """Trends prefixes JSON with )]}' to stop hijacking; drop it."""
    i = text.find("{")
    if i < 0:
        raise ValueError("no JSON object in response")
    return json.loads(text[i:])


# --------------------------------------------------------------------------- #
# Provider 1: Google Trends web endpoints (free)
# --------------------------------------------------------------------------- #
class GoogleTrends:
    BASE = "https://trends.google.com/trends/api"

    def __init__(self, hl: str, geo: str):
        self.hl = hl
        self.geo = geo
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )
        self.warmed = False

    def _get(self, url: str) -> str:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": UA,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": f"{self.hl},en;q=0.8",
                "Referer": f"https://trends.google.com/trends/explore?geo={self.geo}&hl={self.hl}",
            },
        )
        for attempt in range(len(BACKOFF) + 1):
            try:
                with self.opener.open(req, timeout=40) as r:
                    return r.read().decode("utf-8")
            except urllib.error.HTTPError as e:
                if e.code in (429, 503) and attempt < len(BACKOFF):
                    wait = BACKOFF[attempt] + random.uniform(0, 20)
                    log(f"  HTTP {e.code}, backing off {wait:.0f}s")
                    time.sleep(wait)
                    continue
                if e.code in (429, 503):
                    raise Blocked(f"HTTP {e.code}") from e
                raise
        raise Blocked("retries exhausted")

    def warm_up(self) -> None:
        if self.warmed:
            return
        try:
            self._get(f"https://trends.google.com/trends/explore?geo={self.geo}&hl={self.hl}")
        except Blocked:
            raise
        except Exception as e:  # cookie page failing is not fatal
            log(f"  warm-up page failed ({e}); continuing")
        self.warmed = True
        pause()

    def _explore(self, terms: list[str], time_range) -> list[dict]:
        times = time_range if isinstance(time_range, list) else [time_range] * len(terms)
        req = {
            "comparisonItem": [{"keyword": t, "geo": self.geo, "time": tm} for t, tm in zip(terms, times)],
            "category": 0,
            "property": "",
        }
        url = f"{self.BASE}/explore?hl={self.hl}&tz=0&req=" + urllib.parse.quote(json.dumps(req))
        return strip_xssi(self._get(url))["widgets"]

    def _widget(self, kind: str, widget: dict) -> dict:
        url = (
            f"{self.BASE}/widgetdata/{kind}?hl={self.hl}&tz=0&req="
            + urllib.parse.quote(json.dumps(widget["request"]))
            + "&token="
            + widget["token"]
        )
        return strip_xssi(self._get(url))

    def timeseries(self, terms: list[str], time_range: str) -> dict:
        self.warm_up()
        widgets = self._explore(terms, time_range)
        w = next(x for x in widgets if x["id"] == "TIMESERIES")
        pause()
        d = self._widget("multiline", w)
        rows = d["default"]["timelineData"]
        return {
            "t": [int(r["time"]) for r in rows],
            "v": [r["value"] for r in rows],
            "p": [1 if r.get("isPartial") else 0 for r in rows],
        }

    def multirange(self, term: str, ranges: list[str]) -> dict:
        """One term over several date ranges on ONE shared scale (Trends' compare-time-periods)."""
        self.warm_up()
        widgets = self._explore([term] * len(ranges), ranges)
        w = next(x for x in widgets if x["id"] == "TIMESERIES")
        pause()
        rows = self._widget("multirange", w)["default"]["timelineData"]
        n = len(ranges)
        return {
            "ranges": ranges,
            "t": [int(rows[0]["columnData"][i]["time"]) for i in range(n)],
            "v": [[r["columnData"][i]["value"] for r in rows] for i in range(n)],
            "p": [[1 if r["columnData"][i].get("isPartial") else 0 for r in rows] for i in range(n)],
        }

    def related(self, term: str, time_range: str) -> dict:
        self.warm_up()
        widgets = self._explore([term], time_range)
        out: dict = {}
        for w in widgets:
            if w["id"] == "RELATED_QUERIES":
                pause()
                d = self._widget("relatedsearches", w)
                lists = d["default"]["rankedList"]
                out["top"] = [[k["query"], k["value"]] for k in (lists[0]["rankedKeyword"] if lists else [])][:15]
                out["rising"] = [[k["query"], k["formattedValue"]] for k in (lists[1]["rankedKeyword"] if len(lists) > 1 else [])][:15]
            elif w["id"] == "GEO_MAP":
                pause()
                d = self._widget("comparedgeo", w)
                out["geo"] = [[g["geoName"], g["value"][0]] for g in d["default"]["geoMapData"]]
        return out


# --------------------------------------------------------------------------- #
# Provider 2: SerpApi (paid, optional) - same output shape
# Written to SerpApi's documented google_trends response format.
# --------------------------------------------------------------------------- #
class SerpApi:
    URL = "https://serpapi.com/search.json"

    def __init__(self, key: str, hl: str, geo: str):
        self.key, self.hl, self.geo = key, hl.split("-")[0], geo

    def _call(self, **params) -> dict:
        params.update({"engine": "google_trends", "api_key": self.key, "geo": self.geo, "hl": self.hl, "tz": "0"})
        url = self.URL + "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=60) as r:
            d = json.loads(r.read().decode("utf-8"))
        if "error" in d:
            raise RuntimeError(d["error"])
        return d

    def timeseries(self, terms: list[str], time_range: str) -> dict:
        d = self._call(q=",".join(terms), date=time_range, data_type="TIMESERIES")
        rows = d["interest_over_time"]["timeline_data"]
        return {
            "t": [int(r["timestamp"]) for r in rows],
            "v": [[int(x.get("extracted_value", 0)) for x in r["values"]] for r in rows],
            "p": [1 if r.get("partial_data") else 0 for r in rows],
        }

    def multirange(self, term: str, ranges: list[str]) -> dict:
        raise NotImplementedError("year-on-year daily comparison is Google-only")

    def related(self, term: str, time_range: str) -> dict:
        out: dict = {}
        d = self._call(q=term, date=time_range, data_type="RELATED_QUERIES")
        rq = d.get("related_queries", {})
        out["top"] = [[x["query"], x.get("extracted_value", 0)] for x in rq.get("top", [])][:15]
        out["rising"] = [[x["query"], x.get("value", "")] for x in rq.get("rising", [])][:15]
        g = self._call(q=term, date=time_range, data_type="GEO_MAP_0", region="REGION")
        out["geo"] = [[x["location"], int(x.get("extracted_value", 0))] for x in g.get("interest_by_region", [])]
        return out


# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Football fixtures and results (ESPN public scoreboard, no key)
# --------------------------------------------------------------------------- #
def _espn(code: str, day: dt.date) -> dict:
    url = ESPN.format(code=code, ymd=day.strftime("%Y%m%d"))
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_matches(cfg: dict) -> dict:
    """Fixtures/results for each league in config, past `days_back` to `days_ahead`,
    plus the same window `compare_years_back` years earlier (364-day steps) for
    year-on-year fixture comparisons. Finished days are cached for good."""
    store = load_json(MATCHES_JSON, {"days": {}, "matches": {}})
    today = dt.datetime.now(dt.timezone.utc).date()
    start, end = today - dt.timedelta(days=cfg["days_back"]), today + dt.timedelta(days=cfg["days_ahead"])
    windows = [(start - dt.timedelta(days=364 * k), end - dt.timedelta(days=364 * k)) for k in range(cfg.get("compare_years_back", 0) + 1)]
    home = set(cfg.get("home_nations", []))
    leagues = [(c, lg, False) for c, lg in cfg["leagues"].items()] + [(c, lg, True) for c, lg in cfg.get("international", {}).items()]
    recent = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).isoformat()
    calls = 0
    for code, lg, intl in leagues:
        for w0, w1 in windows:
            try:
                first = _espn(code, min(today, w1))
                calls += 1
            except Exception as e:  # noqa: BLE001
                log(f"  fixtures {code}: {e}")
                continue
            league = (first.get("leagues") or [{}])[0]
            cal = league.get("calendar", [])
            days = []
            if league.get("calendarType") == "day" and len(cal) > 3:
                days = sorted({dt.date.fromisoformat(s[:10]) for s in cal})
                days = [d for d in days if w0 <= d <= w1]
            if not days:
                midweek = lg.get("midweek_only", False)
                days = [w0 + dt.timedelta(days=i) for i in range((w1 - w0).days + 1)]
                days = [d for d in days if not midweek or d.weekday() in (1, 2, 3)]
            for d in days:
                key = f"{code}:{d.isoformat()}"
                seen = store["days"].get(key)
                if seen and d < today - dt.timedelta(days=3) and seen.get("final"):
                    continue
                if seen and d > today and seen.get("at", "") > recent:
                    continue
                try:
                    j = _espn(code, d)
                    calls += 1
                    time.sleep(0.2)
                except Exception as e:  # noqa: BLE001
                    log(f"  fixtures {code} {d}: {e}")
                    continue
                final = True
                for ev in j.get("events", []):
                    comp = ev["competitions"][0]["competitors"]
                    h = next((c for c in comp if c["homeAway"] == "home"), None)
                    a = next((c for c in comp if c["homeAway"] == "away"), None)
                    if not h or not a:
                        continue
                    hn, an = h["team"]["displayName"], a["team"]["displayName"]
                    if intl and hn not in home and an not in home:
                        continue
                    state = ev["status"]["type"]["state"]
                    final = final and state == "post"
                    store["matches"][ev["id"]] = [code, ev["date"], hn, an,
                                                  f'{h.get("score","")}-{a.get("score","")}' if state == "post" else "", state]
                store["days"][key] = {"at": now_iso(), "final": final}
    keep = lambda day: any((w0 - dt.timedelta(days=7)).isoformat() <= day <= (w1 + dt.timedelta(days=7)).isoformat() for w0, w1 in windows)  # noqa: E731
    store["matches"] = {k: v for k, v in store["matches"].items() if keep(v[1][:10])}
    store["days"] = {k: v for k, v in store["days"].items() if keep(k.split(":")[1])}
    store["updated"] = now_iso()
    MATCHES_JSON.write_text(json.dumps(store, separators=(",", ":"), ensure_ascii=False))
    log(f"fixtures: {len(store['matches'])} matches across {len(windows)} window(s) ({calls} ESPN calls)")
    return store


def looks_valid(series: dict, n_terms: int) -> bool:
    v = series.get("v") or []
    if len(v) < 20 or any(len(row) != n_terms for row in v):
        return False
    return any(any(x > 0 for x in row) for row in v)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def is_stale(status: dict, key: str, ttl_hours: float) -> bool:
    s = status.get(key, {})
    if not s.get("ok_at"):
        return True
    last = dt.datetime.strptime(s["ok_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    return dt.datetime.now(dt.timezone.utc) - last > dt.timedelta(hours=ttl_hours)


def yoy_ranges(days: int, years_back: int) -> list[str]:
    """Last `days` complete days, plus the same weekdays 1..years_back years earlier (364-day steps)."""
    end = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
    out = []
    for k in range(years_back + 1):
        e = end - dt.timedelta(days=364 * k)
        out.append(f"{(e - dt.timedelta(days=days - 1)).isoformat()} {e.isoformat()}")
    return out


def all_yoy_terms(config: dict) -> list[str]:
    out: list[str] = []
    for v in config.get("verticals", {}).values():
        out += v.get("category_terms", []) + v.get("brand_terms", [])
    out += config.get("tv", {}).get("yoy_terms", [])
    return list(dict.fromkeys(out))


def all_related_terms(config: dict) -> list[str]:
    out: list[str] = []
    for v in config.get("verticals", {}).values():
        out += v.get("related_terms", [])
    return list(dict.fromkeys(out))


def build_js(trends: dict, status: dict, config: dict) -> None:
    payload = dict(trends)
    payload["status"] = status
    payload["config"] = {k: config[k] for k in ("geo", "title", "verticals", "tv", "fixture_popularity", "brand_domains", "ambiguous_terms", "groups", "yoy_daily", "search_console") if k in config}
    (DATA / "trends.js").write_text("window.TRENDS = " + json.dumps(payload, separators=(",", ":")) + ";\n")
    fixtures = load_json(FIXTURES_JSON, {"events": []})
    m = load_json(MATCHES_JSON, {"matches": {}})
    ffc = config.get("football_feed", {})
    leagues = {**ffc.get("leagues", {}), **ffc.get("international", {})}
    uniq = {}
    for v in m.get("matches", {}).values():  # one row per fixture, preferring the one with a result
        k = (v[0], v[1][:10], v[2], v[3])
        if k not in uniq or (v[4] and not uniq[k][4]):
            uniq[k] = [v[0], v[1], v[2], v[3], v[4]]
    fixtures["matches"] = sorted(uniq.values(), key=lambda r: r[1])
    fixtures["leagues"] = {k: v["name"] for k, v in leagues.items()}
    fixtures["matches_updated"] = m.get("updated")
    (DATA / "fixtures.js").write_text("window.FIXTURES = " + json.dumps(fixtures, separators=(",", ":")) + ";\n")
    # Search Console CSVs committed to data/gsc/<property>/*.csv (shared with everyone who opens the dashboard)
    files = []
    gsc_dir = DATA / "gsc"
    if gsc_dir.is_dir():
        for f in sorted(gsc_dir.glob("*/*.csv")):
            try:
                files.append({"property": f.parent.name, "name": f.name, "text": f.read_text(encoding="utf-8-sig")})
            except Exception as e:  # noqa: BLE001
                log(f"  skipped {f}: {e}")
    social = load_json(DATA / "social.json", {"posts": []})
    (DATA / "social.js").write_text("window.SOCIAL = " + json.dumps(social.get("posts", []), separators=(",", ":")) + ";\n")
    (DATA / "gsc.js").write_text("window.GSC_FILES = " + json.dumps(files, separators=(",", ":")) + ";\n")
    # One self-contained file (data inlined) that can be emailed or opened anywhere.
    try:
        page = (ROOT / "index.html").read_text()
        lib = ROOT / "vendor" / "chart.umd.js"  # chart library built in, so the file works with no internet
        if lib.exists():
            page = re.sub(r'<script src="https://cdnjs[^"]*chart[^"]*"></script>', lambda _: "<script>" + lib.read_text().replace("</script", "<\\/script") + "</script>", page, flags=re.I)
        page = page.replace("<head>", "<head>\n<script>window.SNAPSHOT=true;</script>", 1)
        for name in ("trends.js", "fixtures.js", "gsc.js", "social.js"):
            tag = f'<script src="data/{name}"></script>'
            if tag in page:
                page = page.replace(tag, "<script>\n" + (DATA / name).read_text().replace("</", "<\\/") + "\n</script>")
        (ROOT / "flutter-uki-industry-demand-dashboard.html").write_text(page)
    except Exception as e:  # noqa: BLE001
        log(f"  single-file build skipped: {e}")
    log(f"rebuilt data/trends.js, data/fixtures.js and data/gsc.js ({len(files)} Search Console file(s))")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--build-only", action="store_true")
    args = ap.parse_args()

    config = json.loads(CONFIG.read_text())
    if config.get("geo") != "GB":
        log("config.json geo must be GB (United Kingdom); this dashboard is UK-only")
        return 2
    trends = load_json(TRENDS_JSON, {"groups": {}, "related": {}})
    trends.setdefault("groups", {})
    trends.setdefault("related", {})
    status = load_json(STATUS_JSON, {})

    if args.build_only:
        build_js(trends, status, config)
        return 0

    key = os.environ.get("SERPAPI_KEY", "").strip()
    mode = os.environ.get("TRENDS_PROVIDER", "auto" if key else "google").lower()
    google = GoogleTrends(config["hl"], config["geo"]) if mode in ("google", "auto") else None
    serp = SerpApi(key, config["hl"], config["geo"]) if key and mode in ("serpapi", "auto") else None
    if not google and not serp:
        log("TRENDS_PROVIDER=serpapi needs SERPAPI_KEY")
        return 2

    only = {x.strip() for x in args.only.split(",") if x.strip()}
    jobs = []
    for gid, g in config["groups"].items():
        if only and gid not in only:
            continue
        if args.force or only or is_stale(status, gid, g["ttl_hours"]):
            jobs.append(("ts", gid, g))
    yd = config.get("yoy_daily")
    ranges = yoy_ranges(yd["days"], yd["years_back"]) if yd else []
    yoy_terms = all_yoy_terms(config)
    for term in yoy_terms if yd else []:
        k = "yoy:" + term
        if (not only and (args.force or is_stale(status, k, yd["ttl_hours"]))) or k in only:
            jobs.append(("yoy", term, yd))
    rel = config["related"]
    for term in all_related_terms(config):
        k = "related:" + term
        if (not only and (args.force or is_stale(status, k, rel["ttl_hours"]))) or k in only:
            jobs.append(("rel", term, rel))

    ff = config.get("football_feed")
    if ff and (args.force or not only):
        try:
            fetch_matches(ff)
            status["fixtures:espn"] = {"ok_at": now_iso(), "source": "espn scoreboard", "error": None, "tried_at": now_iso()}
        except Exception as e:  # noqa: BLE001
            status.setdefault("fixtures:espn", {})["error"] = f"{type(e).__name__}: {e}"
            log(f"fixtures feed failed: {e}")
    log(f"{len(jobs)} job(s) to run; provider mode = {mode}")
    blocked_in_a_row = 0
    google_off = False
    changed = 0

    for kind, ident, spec in jobs:
        skey = ident if kind == "ts" else ("yoy:" if kind == "yoy" else "related:") + ident
        st = status.setdefault(skey, {})
        st["tried_at"] = now_iso()
        result, source, err = None, None, None
        providers = []
        if google and not google_off:
            providers.append(("google", google))
        if serp:
            providers.append(("serpapi", serp))
        for name, prov in providers:
            try:
                log(f"{kind} {ident} via {name}")
                if kind == "ts":
                    r = prov.timeseries(spec["terms"], spec["time"])
                    if not looks_valid(r, len(spec["terms"])):
                        raise ValueError("response failed sanity checks (empty / all-zero / wrong width)")
                elif kind == "yoy":
                    r = prov.multirange(ident, ranges)
                    if len(r["v"][0]) < 28 or not any(r["v"][0]):
                        raise ValueError("year-on-year response failed sanity checks")
                else:
                    r = prov.related(ident, spec["time"])
                result, source = r, name
                if name == "google":
                    blocked_in_a_row = 0
                break
            except Blocked as e:
                err = f"{name}: blocked ({e})"
                log("  " + err)
                if name == "google":
                    blocked_in_a_row += 1
                    if blocked_in_a_row >= BREAKER_LIMIT:
                        google_off = True
                        log("  circuit breaker: no more Google calls this run")
            except Exception as e:  # noqa: BLE001
                err = f"{name}: {type(e).__name__}: {e}"
                log("  " + err)
        if result is not None:
            if kind == "ts":
                trends["groups"][ident] = {"terms": spec["terms"], "time": spec["time"], **result}
            elif kind == "yoy":
                trends.setdefault("yoy", {})[ident] = result
            else:
                trends["related"][ident] = result
            st.update({"ok_at": now_iso(), "source": source, "error": None})
            changed += 1
        else:
            st["error"] = err or "no provider available"
        if google and not google_off:
            pause()

    # Drop groups that were removed from config.
    for gid in list(trends["groups"]):
        if gid not in config["groups"]:
            trends["groups"].pop(gid)
    keep = set(yoy_terms if yd else [])
    rel_keep = set(all_related_terms(config))
    for t in list(trends.get("related", {})):
        if t not in rel_keep:
            trends["related"].pop(t)
    for t in list(trends.get("yoy", {})):
        if t not in keep:
            trends["yoy"].pop(t)
    trends["geo"] = config["geo"]

    trends["pulled"] = now_iso()
    DATA.mkdir(exist_ok=True)
    TRENDS_JSON.write_text(json.dumps(trends, separators=(",", ":")))
    STATUS_JSON.write_text(json.dumps(status, indent=1, sort_keys=True))
    build_js(trends, status, config)

    failed = [k for k, v in status.items() if v.get("error")]
    log(f"done: {changed} updated, {len(failed)} with errors {failed if failed else ''}")
    # Exit 0 even with some failures so the workflow still publishes the last good data.
    return 0


if __name__ == "__main__":
    sys.exit(main())
