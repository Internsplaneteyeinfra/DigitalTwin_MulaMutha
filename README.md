# NadiTwin — Multi-River, Any-KML, Real-Calendar River Twin

One dashboard, one server. Comes preloaded with two real reaches
(Mula-Mutha, Pune and Mithi, Mumbai) — and you can **upload any river
KML from the dashboard** to add a brand-new reach on the fly, with
chainage-wise geometry, bridge/landmark detection, and a full
discharge/stage/hydrograph/alert/margin board generated for it
automatically.

## Setup & run

`run.py` / `naditwin/engine.py` / `naditwin/server.py` use **only the
Python standard library** — nothing to install to just run the
dashboard:

```
python3 run.py
```
Opens `http://localhost:8080` automatically. **The dashboard starts
blank** — no river auto-loads. Pick one from the "Choose a river"
dropdown at the top, or upload a KML and click "Add river"; nothing
else on the page populates until you do. Options:
```
python3 run.py --port 9000                    # different port
python3 run.py --seed 7                       # different synthetic monsoon
python3 run.py --no-browser                   # don't auto-open a browser tab
python3 run.py --gauge data/your_gauge.csv    # plug in a real discharge/stage CSV
```

## Uploading your own river KML

On the dashboard, use **"Upload river KML"** (top bar) — pick a `.kml`
file (not `.kmz`) and click **Add river**. This runs
`naditwin/kml_ingest.py` live, in the server process, and:

- Derives a **centerline at 100 m chainage spacing** (configurable —
  see `DEFAULT_CHAINAGE_STEP_M` in `kml_ingest.py`) from either a
  `LineString` (used as-is) or a hand-traced bank `Polygon` (centerline
  derived by pairing the two bank arcs and taking midpoints — a fast,
  dependency-free approximation, weaker on tight meanders). The
  derived centerline is **guaranteed to stay inside the traced
  polygon** end to end — a safety net nudges any point that would
  otherwise land outside back in, iterating toward the polygon
  centroid and, as a last resort, blending toward the previous
  already-inside chainage station. Verified against deliberately
  meandering/S-curve synthetic polygons with zero containment
  failures across hundreds of stations.
- Computes the **real bank-to-bank width at every chainage station**
  from the KML geometry itself, not synthesized.
- Picks up any **named `Point` placemarks** in the KML (e.g. "XYZ
  Bridge", "Sangam") as real landmarks, classifies them
  bridge / confluence / locality by name keywords, and snaps them to
  the nearest chainage station. If the KML has **no named points at
  all**, it auto-places a plausible number of bridge markers along the
  reach, named simply **B-1, B-2, B-3, ...** in upstream-to-downstream
  order, so the margin board and alerts have assets to attach to —
  these are clearly tagged `"source": "auto"` everywhere (JSON and
  dashboard UI), never presented as real bridge locations.
- Writes `naditwin/rivers/<slug>/*.json` + a small `meta.json`, so the
  new river **survives a server restart** without re-uploading.
  **Re-uploading the same display name replaces it** (same on-disk
  slug gets overwritten) instead of piling up near-duplicate entries
  in the river list.
- Immediately wires up a full synthetic discharge/stage/hydrograph/
  forecast/margin/alert engine for it (see "Time model" below) — no
  extra step needed.

On Vercel, uploaded river files are written to the function's temporary
`/tmp` storage because the deployed application bundle is read-only. That
storage is ephemeral and is not shared across cold starts, so uploaded
rivers should be treated as session data in the serverless deployment.

Every bridge/locality/confluence gets a **persistent name label** next
to its marker on the map (toggle with the "Area names" button, same
idea as the chainage-number toggle), so you can see at a glance which
named area has which discharge/stage/alert/margin status, area-wise,
without clicking through each marker — the map pins for a river show
exactly however many bridges/landmarks that river actually has.

Uploaded (non-built-in) rivers can be removed with the **"✕ Remove"**
button next to the river selector — deletes both the running engine
and its on-disk folder, so it's gone from the list for good. The two
built-in rivers (Mula-Mutha, Mithi) can't be removed this way.

### One shared "Bridge / area" selector drives three charts

A single dropdown ("Bridge / area", top stats row) picks which
bridge/locality is in focus, and three charts follow it together:

- **River stage** — shows only the *segment* from the selected bridge
  to the next one downstream (e.g. "B-1 → B-2"), not the whole reach —
  so you can zoom into exactly the stretch you care about. The last
  bridge shows "→ end of reach".
- **Hydrograph** — the observed/forecast time series *at* the selected
  bridge (a single point, since a hydrograph is inherently
  location-specific).
- **Discharge — today** — labelled with the same segment, using the
  selected bridge's chainage cell. Note: discharge (Q) itself is a
  single upstream-input value for the whole reach (not something
  that's independently different per location in this model), so the
  line doesn't change shape by bridge — only the label does — while
  river stage genuinely does vary by location and the profile chart
  reflects that.

## Time model — Live vs Historic, real calendar dates

Every hourly value (past **and** forecast) is now indexed against a
**real UTC clock**, not an abstract "sim hour": the synthetic
discharge series runs from **1 Jan 2026** through today (and a ~45-day
forecast runway beyond), seasoned so discharge is higher in the
Jun–Sep monsoon months and lower otherwise — still 100% synthetic
(no real gauge feed), but calendar-shaped instead of a flat random
walk. A ticking **"Simulation clock"** in the top bar shows the real
UTC time in Live mode, or the selected hour in Historic mode.

Two modes, both in the top bar:

- **🔴 Live** (default) — every panel (discharge, stage profile,
  hydrograph, margins, alerts, and the Discharge-today chart below)
  reflects *right now*, interpolated to the current minute between
  the underlying hourly points, and **auto-refreshes every 1 minute**
  automatically — no button to press.
- **🕐 Historic** — pick any **date (1 Jan 2026 → today)** and **hour**
  from the dropdowns, or use the **"Manual step"** ± buttons next to
  the simulation clock to nudge the currently-viewed hour forward or
  back by a configurable number of hours. Every panel re-renders for
  exactly that hour: the observed hydrograph shows the 72 h *before*
  it, the forecast panels show the 72 h ensemble forecast generated
  *from* that point, and the margin board / alerts reflect the state
  at that historic hour. Click **"Jump to now"** to snap back to Live.

Every `GET /api/*` endpoint accepts this the same way: omit `?at=` for
live, or pass `?at=2026-06-15T13:00:00Z` for a specific historic hour
— the engine is now fully stateless per request (no more a
server-side "sim clock" that mutates and drifts between requests).

### Discharge — today, with a scrub slider

A dedicated card shows, for any selected area/asset, **today's
discharge hour-by-hour**: hours already passed (00:00 → now) drawn as
observed, and the remaining hours of the same day drawn as the
forecast ensemble (median + P10–P90) generated from right now forward
— e.g. if it's 18:00 now, hours 00–18 are observed and 19–23 are
forecast, and that split moves forward automatically every minute in
Live mode. A slider under the chart tracks the current hour live, and
can be dragged to scrub through any hour of that day — dragging it
switches into Historic mode at that hour and updates every other
panel to match. This is `GET /api/today?cell=&river=&at=`.

## What's REAL vs SYNTHETIC, per river

| Layer | Mula-Mutha | Mithi | Any uploaded KML |
|---|---|---|---|
| Centerline, chainage, width | ✅ real (KML) | ✅ real (KML) | ✅ real (from the uploaded KML) |
| Bridge/locality/confluence names + positions | ✅ real (snapped) | ✅ real (snapped) | ✅ real if the KML has named Points, else honestly tagged `"auto"` |
| Bed **relief** (relative depth shape) | ✅ real (11,580-pt depth survey) | ❌ synthetic | ❌ synthetic (no depth survey) |
| Absolute elevation datum / downstream slope | ❌ synthetic (no DEM) | ❌ synthetic | ❌ synthetic |
| Embankment / bank crest height | ❌ synthetic | ❌ synthetic | ❌ synthetic |
| Discharge (Q), stage, hydrograph, forecasts, margins, alerts | ❌ synthetic, monsoon-seasoned, Jan 2026–date | same | same |

I still can't fetch a DEM or a real gauge/sensor feed myself in this
sandbox (no internet access). Upload a Copernicus GLO-30 `.tif`, or an
hourly gauge/discharge CSV (`python3 run.py --gauge data/your_file.csv`),
and I'll wire it in the same way documented before.

## Layout

```
run.py                                  launcher
requirements.txt                        deps for dev-tool scripts only (app itself needs none)
data/
  mula_mutha_river.kml                  source KML (real)
  mula_mutha_water_depth.xlsx           source depth survey (real)
  mithi_river.kml                       source KML (real)
naditwin/
  engine.py                             multi-river engine (RiverData + TwinEngine, real-calendar time model)
  kml_ingest.py                         turns ANY uploaded river KML into chainage/width/landmarks JSON, stdlib-only
  server.py                             stdlib HTTP server, ?river=&at= routing, /api/upload_kml
  static/dashboard.html                 dashboard (river dropdown, KML upload, Live/Historic time control)
  rivers/
    mula_mutha/  chainage_profile.json, reach_polygon.json, landmarks.json, depth_profile.json  (real)
    mithi/       chainage_profile.json, reach_polygon.json, landmarks.json                       (real)
    <slug>/      created automatically for every river you upload via the dashboard
extract_centerline_mula_mutha.py        dev tool: KML -> chainage/width (Mula-Mutha, one-off/offline)
extract_centerline_mithi.py             dev tool: KML -> chainage/width (Mithi, one-off/offline)
reverse_and_tag_landmarks_mula_mutha.py dev tool: flip chainage + snap localities (Mula-Mutha)
tag_mithi_landmarks.py                  dev tool: snap localities (Mithi)
build_depth_profile_mula_mutha.py       dev tool: depth survey -> per-station depth (Mula-Mutha)
```

The offline dev-tool scripts (for re-extracting geometry from a new/
updated KML for the two *built-in* rivers specifically) need
`shapely`, `pyproj`, `scipy`, `networkx`, `pandas`, `openpyxl`
(`requirements.txt`). They are **not** what runs when you upload a KML
from the dashboard — that path uses `naditwin/kml_ingest.py`, which is
standard-library only and runs live inside the server.

## API

`/api/rivers`, `/api/timerange`, `/api/meta?river=`,
`/api/state?river=&at=`, `/api/forecast/profile?lead=&river=&at=`,
`/api/hydrograph?cell=&river=&at=`, `/api/today?cell=&river=&at=`,
`/api/margins?river=&at=`, `/api/alerts?river=&at=`,
`/api/scorecard?river=&at=`,
`POST /api/upload_kml?name=<display name>` (body = raw KML text;
re-uploading the same name replaces it),
`POST /api/remove_river?name=<display name>` (uploaded rivers only).
`river` is optional everywhere except `/api/rivers`/`/api/timerange`
(defaults to Mula-Mutha). `at` is optional everywhere it's accepted;
omitting it means live/current time.
#   D i g i t a l T w i n _ M u l a M u t h a 
 
 