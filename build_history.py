#!/usr/bin/env python3
"""
Per-sensor historical baselines for the live page (now.html).

The live FloodNet API tells you what is happening this minute. It does not tell
you whether this minute is unusual. This builds the yardstick.

The only defensible comparison for a sensor is AGAINST ITSELF. Sensors sit where
FloodNet chose to put them (known hot spots first), so comparing one corner to
another, or today's network to a smaller network two years ago, measures
deployment decisions as much as weather. Comparing a sensor to its own record
holds location, instrument and processing fixed.

Two baselines, both apples-to-apples:

  A. PER SENSOR. Every historical flood event peak depth at that exact sensor.
     Lets the page say "this is that corner's 3rd-deepest flood on record"
     instead of the meaningless "3rd deepest in the city."

  B. CITYWIDE COHORT. Daily counts of how many sensors recorded a flood, but
     restricted to a FIXED cohort of sensors installed before the baseline
     window opens. Same sensors every day, so today's count can be ranked
     against the same-sized network on every prior day.

Source: NYC Open Data aq7i-eu5q (flood events) + kb2e-tjy3 (sensor metadata).
Note the events dataset lags roughly a week, so it is the BASELINE only; today's
readings come live from api.floodnet.nyc. That boundary is stated on the page.

Join key: sensor name, normalized. The live API and Open Data use different IDs
(deployment_id vs sensor_id) but share human-readable names.

Output: data/sensor_history.json
"""
import json, urllib.request, urllib.parse, datetime, collections, os, re, sys

DOMAIN = "https://data.cityofnewyork.us/resource"
SENSORS, EVENTS = "kb2e-tjy3", "aq7i-eu5q"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "sensor_history.json")
BASELINE_DAYS = 365          # trailing window for the cohort comparison


def fetch_all(dataset, order=None):
    rows, offset, page = [], 0, 50000
    while True:
        params = {"$limit": page, "$offset": offset}
        if order:
            params["$order"] = order
        url = f"{DOMAIN}/{dataset}.json?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "floodnet-history/1.0"})
        with urllib.request.urlopen(req, timeout=120) as r:
            batch = json.load(r)
        rows.extend(batch)
        if len(batch) < page:
            break
        offset += page
    return rows


def norm(name):
    """Normalize a sensor name for joining across the two data sources."""
    s = (name or "").lower()
    s = s.replace("&", "and")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


print("Fetching sensors + events from NYC Open Data...", file=sys.stderr)
sensors_raw = fetch_all(SENSORS)
events_raw = fetch_all(EVENTS, order="flood_start_time")
print(f"  {len(sensors_raw)} sensors, {len(events_raw)} events", file=sys.stderr)

# ---- sensor index: id -> (normalized name, install date) ----
sid_name, sid_install, sid_meta = {}, {}, {}
for s in sensors_raw:
    sid = s.get("sensor_id")
    if not sid:
        continue
    sid_name[sid] = norm(s.get("sensor_name", ""))
    di = (s.get("date_installed", "") or "")[:10]
    if di:
        try:
            sid_install[sid] = datetime.date.fromisoformat(di)
        except ValueError:
            pass
    lat, lon = num(s.get("latitude")), num(s.get("longitude"))
    if lat is not None and lon is not None:
        sid_meta[sid] = {
            "name": s.get("sensor_name", ""),
            "lat": round(lat, 5), "lon": round(lon, 5),
            "boro": s.get("borough", ""),
            "tidal": (s.get("tidally_influenced", "") or "").strip().lower() == "yes",
            "installed": di,
        }

# ---- per-sensor event peaks, keyed by normalized NAME ----
peaks = collections.defaultdict(list)      # name -> [{d: inches, t: iso date}]
day_sensors = collections.defaultdict(set) # date -> {sensor_id with an event}
sid_events = collections.defaultdict(list) # sensor_id -> [(date, inches)]
latest_event = None

for e in events_raw:
    sid = e.get("sensor_id")
    depth = num(e.get("max_depth_inches"))
    start = e.get("flood_start_time")
    if not sid or depth is None or not start:
        continue
    nm = sid_name.get(sid)
    if not nm:
        continue
    day = start[:10]
    dur = num(e.get("duration_mins")) or 0
    peaks[nm].append({"d": round(depth, 2), "t": day, "m": round(dur)})
    day_sensors[day].add(sid)
    sid_events[sid].append((day, round(depth, 2), round(dur)))
    if latest_event is None or start > latest_event:
        latest_event = start

# ---- A. per-sensor distributions ----
per_sensor = {}
for nm, evs in peaks.items():
    ds = sorted((e["d"] for e in evs), reverse=True)
    top = sorted(evs, key=lambda e: -e["d"])[:3]
    n = len(ds)
    # How long water sits is its own question, and often the one that matters
    # more to a block than how deep it got. FloodNet publishes a duration for
    # every event, so each corner can carry a duration yardstick of its own
    # exactly as it carries a depth one.
    ms = sorted((e["m"] for e in evs), reverse=True)
    longest = max(evs, key=lambda e: e["m"])
    per_sensor[nm] = {
        "n": n,                                   # total recorded flood events here
        "max": ds[0],                             # deepest ever recorded here
        "max_date": top[0]["t"],
        "median": ds[n // 2],                     # typical flood at this corner
        "p90": ds[max(0, int(n * 0.10))],         # a bad one for this corner
        "top": [{"d": t["d"], "t": t["t"]} for t in top],
        "first": min(e["t"] for e in evs),        # start of this sensor's record
        "dur_median": ms[n // 2],                 # typical minutes underwater here
        "dur_max": ms[0],                         # longest it has ever stayed wet
        "dur_max_date": longest["t"],
        "dur_total": sum(ms),                     # lifetime minutes above the floor
    }

# ---- B. fixed-cohort daily counts ----
today = datetime.date.today()
window_start = today - datetime.timedelta(days=BASELINE_DAYS)
# cohort: sensors installed before the window opened, so every day in the window
# had the chance to hear from all of them
cohort = {sid for sid, d in sid_install.items() if d < window_start}

daily = []
d = window_start
while d < today:
    key = d.isoformat()
    hits = day_sensors.get(key, set()) & cohort
    daily.append(len(hits))
    d += datetime.timedelta(days=1)

daily_sorted = sorted(daily)
cohort_names = sorted({sid_name[s] for s in cohort if s in sid_name})

# ---- matched threshold ----
# The live API streams raw per-minute depths; the historical dataset contains
# only DETECTED EVENTS. Counting "any reading above zero" live and comparing it
# to detected events would compare a loose definition to a strict one and
# manufacture a trend. So we read the effective floor out of the published
# events themselves and apply that same floor to the live readings.
all_depths = [num(e.get("max_depth_inches")) for e in events_raw]
all_durs = [num(e.get("duration_mins")) for e in events_raw]
min_depth = min(d for d in all_depths if d is not None)
min_dur = min(d for d in all_durs if d is not None)

# ---- B2. true concurrent peak, for the 24-hour pulse chart ----
# The pulse chart counts how many sensors are in flood AT THE SAME MOMENT, in
# 10-minute bins. The daily cohort counts above are a different quantity: how
# many distinct sensors flooded at any point across a whole day. A day's total
# nearly always exceeds any single moment, so using it as the chart's ceiling
# compares two different things and overstates the bar. This reconstructs the
# genuinely comparable number by walking each event's own interval (start plus
# duration) across the same 10-minute grid and taking the busiest single bin.
BIN_MIN = 10
concurrent = collections.defaultdict(set)
for e in events_raw:
    sid = e.get("sensor_id")
    start = e.get("flood_start_time")
    dur = num(e.get("duration_mins"))
    if sid not in cohort or not start or dur is None:
        continue
    try:
        t0 = datetime.datetime.fromisoformat(start.replace("Z", ""))
    except ValueError:
        continue
    if t0.date() < window_start:
        continue
    first = int(t0.timestamp() // (BIN_MIN * 60))
    last = int((t0 + datetime.timedelta(minutes=max(dur, 1))).timestamp() // (BIN_MIN * 60))
    for b in range(first, last + 1):
        concurrent[b].add(sid)

peak_bin, peak_n = None, 0
for b, s in concurrent.items():
    if len(s) > peak_n:
        peak_bin, peak_n = b, len(s)
peak_at = (datetime.datetime.fromtimestamp(peak_bin * BIN_MIN * 60).isoformat(timespec="minutes")
           if peak_bin is not None else None)

# ---- C. map layer: every sensor's record over the trailing year ----
# One entry per sensor that was in the ground for the whole window, whether or
# not it ever flooded. A sensor that watched a year of storms and recorded
# nothing is a real finding and has to be on the map, otherwise the map only
# shows places that flood and reads as if nowhere else was watched.
win_start = window_start.isoformat()
year_sensors = []
for sid, m in sid_meta.items():
    inst = sid_install.get(sid)
    evs = [(d, x, mins) for d, x, mins in sid_events.get(sid, []) if d >= win_start]
    evs.sort(key=lambda e: -e[1])
    year_sensors.append({
        **m,
        "n": len(evs),                                  # floods in the window
        "max": evs[0][1] if evs else 0,                 # deepest in the window
        "hrs": round(sum(e[2] for e in evs) / 60, 1),   # hours underwater in the window
        "last": max((d for d, _, _ in evs), default=None), # most recent flood
        # full watch = in the ground before the window opened, so its zero
        # means "watched and stayed dry" rather than "arrived late"
        "full": bool(inst and inst < window_start),
    })
year_sensors.sort(key=lambda s: -s["n"])

out = {
    "generated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    "year_sensors": year_sensors,
    "baseline_source": "NYC Open Data aq7i-eu5q (FloodNet flood events) + kb2e-tjy3",
    "baseline_through": (latest_event or "")[:10],
    "baseline_days": BASELINE_DAYS,
    "event_count": sum(len(v) for v in peaks.values()),
    "matched": {
        "min_depth_inches": min_depth,
        "min_duration_mins": min_dur,
        "note": ("Effective detection floor observed in FloodNet's own published "
                 "event dataset. The live page applies this same floor to raw "
                 "readings so today is counted the way history was counted."),
    },
    "per_sensor": per_sensor,
    "cohort": {
        "size": len(cohort),
        "names": cohort_names,
        "window_start": window_start.isoformat(),
        "daily_counts": daily,
        "mean": round(sum(daily) / len(daily), 2) if daily else 0,
        "median": daily_sorted[len(daily_sorted) // 2] if daily_sorted else 0,
        "p90": daily_sorted[int(len(daily_sorted) * 0.90)] if daily_sorted else 0,
        "max": daily_sorted[-1] if daily_sorted else 0,
        "days_with_any": sum(1 for x in daily if x > 0),
        # busiest single 10-minute moment, directly comparable to the pulse chart
        "peak_concurrent": peak_n,
        "peak_concurrent_at": peak_at,
        "peak_concurrent_bin_mins": BIN_MIN,
    },
}

os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w") as f:
    json.dump(out, f, separators=(",", ":"))

print(f"  wrote {OUT}", file=sys.stderr)
print(f"  per-sensor histories: {len(per_sensor)}", file=sys.stderr)
print(f"  baseline runs through: {out['baseline_through']}", file=sys.stderr)
print(f"  fixed cohort: {len(cohort)} sensors installed before {window_start}", file=sys.stderr)
print(f"  cohort daily sensor-with-flood counts over {BASELINE_DAYS}d: "
      f"median {out['cohort']['median']}, p90 {out['cohort']['p90']}, max {out['cohort']['max']}, "
      f"{out['cohort']['days_with_any']} days with any", file=sys.stderr)
