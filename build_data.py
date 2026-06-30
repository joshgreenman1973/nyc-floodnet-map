#!/usr/bin/env python3
"""
FloodNet data pipeline.

Pulls two NYC Open Data (Socrata) datasets:
  - Sensors:  kb2e-tjy3  (FloodNet: Sensor Deployment Metadata)
  - Events:   aq7i-eu5q  (FloodNet: Street Flooding Events Measured by FloodNet Sensors)

Joins events to sensors on sensor_id, computes per-sensor, per-borough,
seasonal, time-of-day, depth and duration aggregates, and a network-growth
normalized "events per active sensor-month" series.

Output: data/floodnet.json  (everything the frontend needs, one file)

Confidence: HIGH for raw counts/depths (taken directly from the published
sensor measurements). MEDIUM for the normalized exposure series, which depends
on our assumption that a sensor is "active" from its install date onward.
"""
import json, urllib.request, urllib.parse, datetime, collections, os, sys

DOMAIN = "https://data.cityofnewyork.us/resource"
SENSORS = "kb2e-tjy3"
EVENTS = "aq7i-eu5q"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "floodnet.json")


def fetch_all(dataset, select=None, order=None):
    rows, offset, page = [], 0, 50000
    while True:
        params = {"$limit": page, "$offset": offset}
        if select:
            params["$select"] = select
        if order:
            params["$order"] = order
        url = f"{DOMAIN}/{dataset}.json?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "floodnet-build/1.0"})
        with urllib.request.urlopen(req, timeout=120) as r:
            batch = json.load(r)
        rows.extend(batch)
        if len(batch) < page:
            break
        offset += page
    return rows


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def months_between(d0, d1):
    return (d1.year - d0.year) * 12 + (d1.month - d0.month)


print("Fetching sensors...", file=sys.stderr)
sensors_raw = fetch_all(SENSORS)
print(f"  {len(sensors_raw)} sensors", file=sys.stderr)
print("Fetching events...", file=sys.stderr)
events_raw = fetch_all(EVENTS, order="flood_start_time")
print(f"  {len(events_raw)} events", file=sys.stderr)

# Index sensors by id
sensors = {}
for s in sensors_raw:
    sid = s.get("sensor_id")
    if not sid:
        continue
    lat, lon = num(s.get("latitude")), num(s.get("longitude"))
    di = s.get("date_installed", "")[:10] or None
    sensors[sid] = {
        "id": sid,
        "name": s.get("sensor_name", sid),
        "boro": s.get("borough", ""),
        "nta": s.get("nta", ""),
        "street": s.get("street_name", ""),
        "zip": s.get("zipcode", ""),
        "cb": s.get("community_board", ""),
        "cd": s.get("council_district", ""),
        "tidal": (s.get("tidally_influenced", "") or "").strip().lower() == "yes",
        "installed": di,
        "lat": lat,
        "lon": lon,
        "n_events": 0,
        "max_depth": 0.0,
        "deepest_date": None,
        "total_flood_mins": 0.0,
    }

# Aggregators
by_year = collections.Counter()
by_month = collections.Counter()        # calendar month 1-12 (seasonality)
by_hour = collections.Counter()         # hour of day 0-23
by_yearmonth = collections.Counter()    # YYYY-MM for time series
depth_buckets = collections.Counter()   # depth class
dur_buckets = collections.Counter()
boro_events = collections.Counter()
boro_depth_sum = collections.Counter()
tidal_events = collections.Counter()    # tidal vs rainfall driven
events_clean = []
deepest = []

DEPTH_CLASSES = [(0, 4, "Under 4 in"), (4, 8, "4-8 in"), (8, 12, "8-12 in"),
                 (12, 24, "12-24 in"), (24, 1e9, "24+ in")]
DUR_CLASSES = [(0, 30, "Under 30 min"), (30, 60, "30-60 min"), (60, 120, "1-2 hr"),
               (120, 360, "2-6 hr"), (360, 1e9, "6+ hr")]


def classify(v, classes):
    for lo, hi, label in classes:
        if v is not None and lo <= v < hi:
            return label
    return None


for e in events_raw:
    sid = e.get("sensor_id")
    start = e.get("flood_start_time")
    if not start:
        continue
    try:
        dt = datetime.datetime.fromisoformat(start)
    except ValueError:
        continue
    depth = num(e.get("max_depth_inches"))
    dur = num(e.get("duration_mins"))
    s = sensors.get(sid)
    boro = s["boro"] if s else "Unknown"
    tidal = s["tidal"] if s else None

    by_year[dt.year] += 1
    by_month[dt.month] += 1
    by_hour[dt.hour] += 1
    by_yearmonth[f"{dt.year:04d}-{dt.month:02d}"] += 1
    boro_events[boro] += 1
    if depth is not None:
        boro_depth_sum[boro] += depth
        dc = classify(depth, DEPTH_CLASSES)
        if dc:
            depth_buckets[dc] += 1
    if dur is not None:
        dcu = classify(dur, DUR_CLASSES)
        if dcu:
            dur_buckets[dcu] += 1
    if tidal is True:
        tidal_events["Tidal"] += 1
    elif tidal is False:
        tidal_events["Rainfall"] += 1

    if s:
        s["n_events"] += 1
        if depth is not None and depth > s["max_depth"]:
            s["max_depth"] = depth
            s["deepest_date"] = start[:10]
        if dur is not None:
            s["total_flood_mins"] += dur

    deepest.append({
        "sensor": s["name"] if s else sid,
        "boro": boro,
        "date": start[:10],
        "depth": round(depth, 1) if depth is not None else None,
        "dur": int(dur) if dur is not None else None,
    })

# Network-growth normalization: events per 100 active sensor-months, by year.
# A sensor is "active" in a given month if installed on/before that month.
today = datetime.date(2026, 6, 30)
install_dates = []
for s in sensors.values():
    if s["installed"]:
        try:
            install_dates.append(datetime.date.fromisoformat(s["installed"]))
        except ValueError:
            pass

# active sensor-months per calendar year
active_sensor_months = collections.Counter()
for yr in range(2020, 2027):
    for mo in range(1, 13):
        month_start = datetime.date(yr, mo, 1)
        if month_start > today:
            continue
        cnt = sum(1 for d in install_dates if d <= month_start)
        active_sensor_months[yr] += cnt

normalized = []
for yr in sorted(by_year):
    asm = active_sensor_months.get(yr, 0)
    rate = (by_year[yr] / asm * 100) if asm else None
    normalized.append({
        "year": yr,
        "events": by_year[yr],
        "active_sensor_months": asm,
        "events_per_100_sensor_months": round(rate, 2) if rate is not None else None,
    })

# Deepest leaderboard (top 25 distinct events)
deepest_sorted = sorted([d for d in deepest if d["depth"] is not None],
                        key=lambda d: d["depth"], reverse=True)[:25]

# Sensor list for map (only geolocated)
sensor_list = []
for s in sensors.values():
    if s["lat"] is None or s["lon"] is None:
        continue
    sensor_list.append({
        "id": s["id"], "name": s["name"], "boro": s["boro"], "nta": s["nta"],
        "street": s["street"], "tidal": s["tidal"], "installed": s["installed"],
        "lat": round(s["lat"], 6), "lon": round(s["lon"], 6),
        "n": s["n_events"], "maxd": round(s["max_depth"], 1),
        "deepest_date": s["deepest_date"],
        "flood_hrs": round(s["total_flood_mins"] / 60, 1),
    })
sensor_list.sort(key=lambda s: s["n"], reverse=True)

MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

out = {
    "meta": {
        "generated": today.isoformat(),
        "n_sensors": len(sensors),
        "n_geolocated_sensors": len(sensor_list),
        "n_events": len(events_raw),
        "event_date_min": min((e.get("flood_start_time", "") for e in events_raw), default="")[:10],
        "event_date_max": max((e.get("flood_start_time", "") for e in events_raw), default="")[:10],
        "sources": {
            "sensors": f"{DOMAIN}/{SENSORS}",
            "events": f"{DOMAIN}/{EVENTS}",
        },
    },
    "sensors": sensor_list,
    "by_year": [{"year": y, "events": by_year[y]} for y in sorted(by_year)],
    "normalized": normalized,
    "by_month": [{"month": MONTH_NAMES[m - 1], "events": by_month[m]} for m in range(1, 13)],
    "by_hour": [{"hour": h, "events": by_hour.get(h, 0)} for h in range(24)],
    "by_yearmonth": [{"ym": k, "events": by_yearmonth[k]} for k in sorted(by_yearmonth)],
    "boro": [{"boro": b, "events": boro_events[b],
              "avg_depth": round(boro_depth_sum[b] / boro_events[b], 1) if boro_events[b] else None}
             for b in sorted(boro_events, key=lambda b: boro_events[b], reverse=True)],
    "depth_buckets": [{"label": lbl, "events": depth_buckets.get(lbl, 0)}
                      for _, _, lbl in DEPTH_CLASSES],
    "dur_buckets": [{"label": lbl, "events": dur_buckets.get(lbl, 0)}
                    for _, _, lbl in DUR_CLASSES],
    "tidal_split": [{"label": k, "events": v} for k, v in tidal_events.items()],
    "deepest": deepest_sorted,
}

os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w") as f:
    json.dump(out, f, separators=(",", ":"))
print(f"Wrote {OUT} ({os.path.getsize(OUT)//1024} KB)", file=sys.stderr)
print(f"Sensors with floods: {sum(1 for s in sensor_list if s['n']>0)}/{len(sensor_list)}", file=sys.stderr)
print(f"Tidal split: {dict(tidal_events)}", file=sys.stderr)
