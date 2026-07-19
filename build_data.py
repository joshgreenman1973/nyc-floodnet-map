#!/usr/bin/env python3
"""
FloodNet data pipeline v2 — severity-first, with honest growth accounting.

Sources (NYC Open Data / Socrata):
  - Sensors:  kb2e-tjy3  (FloodNet: Sensor Deployment Metadata)
  - Events:   aq7i-eu5q  (FloodNet: Street Flooding Events Measured by FloodNet Sensors)

This version goes beyond raw counts:
  * SEVERITY. Each event is classed by peak depth (nuisance -> extreme) and we
    use the published time-above-threshold fields to measure DEEP-FLOOD-HOURS
    (hours of standing water above 12 inches) — a far better proxy for "serious"
    flooding than counting events.
  * HONEST TRENDS. The sensor network grew ~30x (2 sensors in late 2020 -> 453
    now), so raw yearly counts are mostly deployment, not weather. We compute
    events per active sensor, and — crucially — a STABLE COHORT of sensors that
    were already live before 2023 and track only them across 2023-2025, holding
    the sensor set fixed. That isolates whether flooding itself changed.
  * Partial years (2020 = 6 weeks; 2026 = through ~June) are flagged everywhere.

Output: data/floodnet.json
Confidence: HIGH for raw measurements; MEDIUM for normalized/cohort trends
(they depend on install-date-as-activation and a small early cohort).
"""
import json, urllib.request, urllib.parse, datetime, collections, os, re, sys

DOMAIN = "https://data.cityofnewyork.us/resource"
SENSORS, EVENTS = "kb2e-tjy3", "aq7i-eu5q"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "floodnet.json")
TODAY = datetime.date(2026, 6, 30)


def fetch_all(dataset, order=None):
    rows, offset, page = [], 0, 50000
    while True:
        params = {"$limit": page, "$offset": offset}
        if order:
            params["$order"] = order
        url = f"{DOMAIN}/{dataset}.json?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "floodnet-build/2.0"})
        with urllib.request.urlopen(req, timeout=120) as r:
            batch = json.load(r)
        rows.extend(batch)
        if len(batch) < page:
            break
        offset += page
    return rows


def _norm(x):
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (x or "").lower().replace("&", "and")).split())


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


print("Fetching sensors + events...", file=sys.stderr)
sensors_raw = fetch_all(SENSORS)
events_raw = fetch_all(EVENTS, order="flood_start_time")
print(f"  {len(sensors_raw)} sensors, {len(events_raw)} events", file=sys.stderr)

# ---- sensor index ----
sensors = {}
for s in sensors_raw:
    sid = s.get("sensor_id")
    if not sid:
        continue
    di = (s.get("date_installed", "") or "")[:10] or None
    inst = None
    if di:
        try:
            inst = datetime.date.fromisoformat(di)
        except ValueError:
            inst = None
    sensors[sid] = {
        "id": sid, "name": s.get("sensor_name", sid), "boro": s.get("borough", ""),
        "nta": s.get("nta", ""), "street": s.get("street_name", ""),
        "tidal": (s.get("tidally_influenced", "") or "").strip().lower() == "yes",
        "installed": di, "inst_date": inst,
        "lat": num(s.get("latitude")), "lon": num(s.get("longitude")),
        # severity accumulators
        "n": 0, "serious": 0, "severe": 0, "max_depth": 0.0, "deepest_date": None,
        "deep_hours": 0.0,    # hours above 12"
        "flood_hours": 0.0,   # total hours flooded
        "depth_sum": 0.0,     # for mean peak depth
    }

# depth severity tiers (inches)
def depth_tier(d):
    if d is None:        return None
    if d < 4:            return "Nuisance"     # ankle / curb film
    if d < 8:            return "Notable"      # impassable on foot in spots
    if d < 12:           return "Serious"      # most cars can't pass
    if d < 24:           return "Severe"       # vehicles stall / float
    return "Extreme"                            # life-threatening
TIERS = ["Nuisance", "Notable", "Serious", "Severe", "Extreme"]
TIER_SERIOUS = {"Serious", "Severe", "Extreme"}   # >= 8"

# ---- per-year accumulators ----
yr_events = collections.Counter()
yr_serious = collections.Counter()
yr_deep_hours = collections.Counter()
yr_depth_sum = collections.Counter()
yr_depth_n = collections.Counter()
by_month = collections.Counter()
by_hour = collections.Counter()
tier_counts = collections.Counter()
tidal_split = collections.Counter()
boro_events = collections.Counter()
boro_serious = collections.Counter()
boro_deep_hours = collections.Counter()
deepest = []

# Stable cohort: sensors in the ground before 2023 AND STILL IN THE GROUND.
#
# An earlier version divided by every sensor installed before 2023. Eleven of
# those 34 have since been retired, and a sensor that is gone records no floods,
# so leaving it in the denominator manufactures a decline. Survivorship has to
# be checked, not assumed: the corrected per-sensor figures are roughly 40%
# higher than the ones that flaw produced.
#
# "Still in the ground" is taken from FloodNet's live deployment list (a sensor
# absent from it, or carrying date_down, is no longer reporting). This is a
# proxy for uptime, not proof of it: a surviving sensor could still have been
# offline for stretches, which the published event record cannot reveal.
COHORT_CUT = datetime.date(2023, 1, 1)
COHORT_YEARS = [2023, 2024, 2025]

def _live_sensor_names():
    try:
        req = urllib.request.Request("https://api.floodnet.nyc/api/rest/deployments/flood",
                                     headers={"User-Agent": "floodnet-build/2.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.load(r)
        deps = d.get("deployments", d)
        return {_norm(x.get("name")) for x in deps if not x.get("date_down")}
    except Exception as exc:
        print(f"WARNING: could not reach the deployment list ({exc}); "
              f"cohort falls back to all pre-2023 sensors and will understate "
              f"per-sensor rates in later years", file=sys.stderr)
        return None

_live_names = _live_sensor_names()
_pre2023 = {sid for sid, s in sensors.items() if s["inst_date"] and s["inst_date"] < COHORT_CUT}
if _live_names is None:
    cohort_ids = _pre2023
    cohort_verified = False
else:
    cohort_ids = {sid for sid in _pre2023 if _norm(sensors[sid]["name"]) in _live_names}
    cohort_verified = True
cohort_retired = len(_pre2023) - len(cohort_ids)
cohort_yr_events = collections.Counter()
cohort_yr_serious = collections.Counter()
cohort_yr_deep = collections.Counter()
cohort_yr_reporting = collections.defaultdict(set)   # sensors with >=1 event that year

for e in events_raw:
    sid = e.get("sensor_id")
    start = e.get("flood_start_time")
    if not start:
        continue
    try:
        dt = datetime.datetime.fromisoformat(start)
    except ValueError:
        continue
    yr = dt.year
    depth = num(e.get("max_depth_inches"))
    dur = num(e.get("duration_mins"))
    above12 = num(e.get("duration_above_12_inches_mins")) or 0.0
    s = sensors.get(sid)
    boro = s["boro"] if s else "Unknown"
    tier = depth_tier(depth)
    serious = tier in TIER_SERIOUS

    yr_events[yr] += 1
    by_month[dt.month] += 1
    by_hour[dt.hour] += 1
    if tier:
        tier_counts[tier] += 1
    if serious:
        yr_serious[yr] += 1
        boro_serious[boro] += 1
    if depth is not None:
        yr_depth_sum[yr] += depth
        yr_depth_n[yr] += 1
    yr_deep_hours[yr] += above12 / 60.0
    boro_events[boro] += 1
    boro_deep_hours[boro] += above12 / 60.0
    if s and s["tidal"]:
        tidal_split["Tidal"] += 1
    elif s:
        tidal_split["Rainfall"] += 1

    if sid in cohort_ids:
        cohort_yr_events[yr] += 1
        cohort_yr_reporting[yr].add(sid)
        if serious:
            cohort_yr_serious[yr] += 1
        cohort_yr_deep[yr] += above12 / 60.0

    if s:
        s["n"] += 1
        if serious:
            s["serious"] += 1
        if tier in ("Severe", "Extreme"):
            s["severe"] += 1
        if depth is not None:
            s["depth_sum"] += depth
            if depth > s["max_depth"]:
                s["max_depth"] = depth
                s["deepest_date"] = start[:10]
        s["deep_hours"] += above12 / 60.0
        if dur is not None:
            s["flood_hours"] += dur / 60.0

    deepest.append({"sensor": s["name"] if s else sid, "boro": boro, "date": start[:10],
                    "depth": round(depth, 1) if depth is not None else None,
                    "deep_hrs": round(above12 / 60.0, 1),
                    "dur": round(dur) if dur is not None else None,
                    "tier": tier})

# ---- active sensors per year (mid-year census) ----
def active_on(d):
    return sum(1 for s in sensors.values() if s["inst_date"] and s["inst_date"] <= d)

active_year = {}
for yr in range(2020, 2027):
    mid = datetime.date(yr, 7, 1)
    if mid > TODAY:
        mid = TODAY
    active_year[yr] = active_on(mid)

# fraction of the year covered (for partial-year honesty)
def year_fraction(yr):
    start = datetime.date(yr, 1, 1)
    end = datetime.date(yr, 12, 31)
    if yr == 2020:  # network began Nov 16
        start = datetime.date(2020, 11, 16)
    if end > TODAY:
        end = TODAY
    days = (end - start).days + 1
    return round(days / 365.0, 3)

years_present = sorted(yr_events)
trend = []
for yr in years_present:
    act = active_year.get(yr, 0)
    frac = year_fraction(yr)
    # annualized events per active sensor (per-sensor exposure, scaled to full year)
    eps = (yr_events[yr] / act / frac) if act and frac else None
    sps = (yr_serious[yr] / act / frac) if act and frac else None
    dhs = (yr_deep_hours[yr] / act / frac) if act and frac else None
    trend.append({
        "year": yr, "events": yr_events[yr], "serious": yr_serious[yr],
        "deep_hours": round(yr_deep_hours[yr], 1),
        "active_sensors": act, "year_fraction": frac, "partial": frac < 0.97,
        "events_per_sensor": round(eps, 2) if eps is not None else None,
        "serious_per_sensor": round(sps, 2) if sps is not None else None,
        "deep_hours_per_sensor": round(dhs, 2) if dhs is not None else None,
        "mean_depth": round(yr_depth_sum[yr] / yr_depth_n[yr], 1) if yr_depth_n[yr] else None,
        "share_serious": round(100 * yr_serious[yr] / yr_events[yr], 1) if yr_events[yr] else None,
    })

cohort = [{
    "year": yr, "events": cohort_yr_events.get(yr, 0),
    "serious": cohort_yr_serious.get(yr, 0),
    "deep_hours": round(cohort_yr_deep.get(yr, 0.0), 1),
    "reporting": len(cohort_yr_reporting.get(yr, ())),
    "deep_hours_per_sensor": round(cohort_yr_deep.get(yr, 0.0) / len(cohort_ids), 2) if cohort_ids else None,
    "per_sensor": round(cohort_yr_events.get(yr, 0) / len(cohort_ids), 2) if cohort_ids else None,
} for yr in COHORT_YEARS]

# ---- sensor list for the map ----
sensor_list = []
for s in sensors.values():
    if s["lat"] is None or s["lon"] is None:
        continue
    sensor_list.append({
        "id": s["id"], "name": s["name"], "boro": s["boro"], "street": s["street"],
        "tidal": s["tidal"], "installed": s["installed"],
        "lat": round(s["lat"], 6), "lon": round(s["lon"], 6),
        "n": s["n"], "serious": s["serious"], "severe": s["severe"],
        "maxd": round(s["max_depth"], 1), "deepest_date": s["deepest_date"],
        "deep_hrs": round(s["deep_hours"], 1), "flood_hrs": round(s["flood_hours"], 1),
        "mean_depth": round(s["depth_sum"] / s["n"], 1) if s["n"] else 0,
        "in_cohort": s["id"] in cohort_ids,
    })
sensor_list.sort(key=lambda s: s["deep_hrs"], reverse=True)

deepest_sorted = sorted([d for d in deepest if d["depth"] is not None],
                        key=lambda d: d["depth"], reverse=True)[:30]
# worst by duration of deep water
longest_deep = sorted([d for d in deepest if d["deep_hrs"] > 0],
                      key=lambda d: d["deep_hrs"], reverse=True)[:15]

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

out = {
    "meta": {
        "generated": TODAY.isoformat(),
        "n_sensors": len(sensors), "n_geo": len(sensor_list), "n_events": len(events_raw),
        "n_serious": sum(yr_serious.values()),
        "total_deep_hours": round(sum(yr_deep_hours.values()), 1),
        "event_date_min": (min((e.get("flood_start_time", "") for e in events_raw), default=""))[:10],
        "event_date_max": (max((e.get("flood_start_time", "") for e in events_raw), default=""))[:10],
        "cohort_size": len(cohort_ids), "cohort_years": COHORT_YEARS,
        "cohort_retired_excluded": cohort_retired, "cohort_verified_active": cohort_verified,
        "sources": {"sensors": f"{DOMAIN}/{SENSORS}", "events": f"{DOMAIN}/{EVENTS}"},
    },
    "sensors": sensor_list,
    "trend": trend,
    "cohort": cohort,
    "tiers": [{"tier": t, "events": tier_counts.get(t, 0)} for t in TIERS],
    "by_month": [{"month": MONTHS[m - 1], "events": by_month.get(m, 0)} for m in range(1, 13)],
    "by_hour": [{"hour": h, "events": by_hour.get(h, 0)} for h in range(24)],
    "boro": [{"boro": b, "events": boro_events[b], "serious": boro_serious.get(b, 0),
              "deep_hours": round(boro_deep_hours.get(b, 0.0), 1)}
             for b in sorted(boro_events, key=lambda b: boro_deep_hours.get(b, 0), reverse=True)],
    "tidal_split": [{"label": k, "events": v} for k, v in tidal_split.items()],
    "deepest": deepest_sorted,
    "longest_deep": longest_deep,
}

os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w") as f:
    json.dump(out, f, separators=(",", ":"))
print(f"Wrote {OUT} ({os.path.getsize(OUT)//1024} KB)", file=sys.stderr)
print(f"Serious floods (>=8\"): {sum(yr_serious.values())}  | total deep-hours: {round(sum(yr_deep_hours.values()),1)}", file=sys.stderr)
print(f"Stable cohort: {len(cohort_ids)} sensors live pre-2023", file=sys.stderr)
print(f"Cohort events/sensor: " + ", ".join(f"{c['year']}={c['per_sensor']}" for c in cohort), file=sys.stderr)
print(f"Trend events/sensor: " + ", ".join(f"{t['year']}={t['events_per_sensor']}" for t in trend), file=sys.stderr)
