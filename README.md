# Where the streets flood — New York City's flood-sensor network

An interactive map and chart set tracking measured street flooding across New York City, drawn from the FloodNet sensor network published on NYC Open Data.

## Live page: is it flooding right now?

`now.html` is a companion live view. It queries FloodNet's public API (early-access beta) directly from the browser — no server, no build:

- `GET https://api.floodnet.nyc/api/rest/deployments/flood` for active sensor locations.
- `POST https://api.floodnet.nyc/v1/graphql` for depth readings (`depth_data` table, `depth_proc_mm`): one query fetching all readings in the trailing 15 minutes (the "right now" state) plus all wet readings (`depth_proc_mm > 0`) in the trailing 24 hours.
- `GET .../deployments/flood/{deployment_id}/depth?start_time=&end_time=` for the per-sensor six-hour sparkline on click.

The page re-queries every 60 seconds. "Wet" = latest processed reading above zero within the last 15 minutes. All depths shown are FloodNet's own `depth_proc_mm`, converted to inches, unmodified. Sensors silent for more than 15 minutes render hollow. The API endpoints were taken from FloodNet's public access guide (linked from github.com/floodnet-nyc/floodnet-data); the data license is CC BY-NC-SA 4.0.

## What it shows

- A map of all 453 FloodNet sensors, sized and colored by how often each has recorded flooding (or by the deepest flood reached), with a tidal-vs-rainfall filter and per-sensor flood histories.
- Charts for flood events per year (raw and network-growth normalized), tidal vs rainfall split, borough comparison, seasonality, time of day, and depth and duration distributions.
- A leaderboard of the deepest single floods on record.

## Data sources

| Dataset | NYC Open Data ID | Used for |
|---|---|---|
| FloodNet: Street Flooding Events Measured by FloodNet Sensors | `aq7i-eu5q` | 2,448 measured flood events (depth, onset, drain, duration) |
| FloodNet: Sensor Deployment Metadata | `kb2e-tjy3` | 453 sensor locations, borough, tidal flag, install date |

FloodNet is a research collaboration of CUNY, NYU and New York City agencies. Coverage runs Nov 2020 → Jun 2026.

## Method and confidence

- Events are joined to sensors on `sensor_id`. Every value shown is a published measurement — nothing is modeled or estimated.
- **Network-growth caveat:** raw yearly event counts rise largely because the sensor network grew. The "per 100 active sensor-months" series normalizes by exposure (a sensor counts as active from its install date forward). Confidence: **high** for raw counts and depths; **medium** for the normalized series.
- Sensors are deliberately placed at known flood hot spots, so these numbers describe flooding where the city expects it, not a random street sample. Absence of a sensor is not evidence of no flooding.

## Build

```bash
python3 build_data.py     # pulls both datasets, writes data/floodnet.json
python3 -m http.server 8731   # then open http://localhost:8731
```

No build dependencies beyond Python 3 standard library. The page is fully static (Leaflet + CARTO basemap from CDN) and deploys as-is to any static host.

Built with AI assistance (Claude); all figures computed by `build_data.py` directly from the sources above. Independent civic-data project — not affiliated with FloodNet or the City of New York.
