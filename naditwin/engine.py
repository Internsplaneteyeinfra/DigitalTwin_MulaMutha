"""
NadiTwin engine — multi-river, real-calendar capable.

Each river is a self-contained data folder under naditwin/rivers/<key>/:
    chainage_profile.json   REAL centerline stations (100 m spacing by
                             default) + width, extracted from that river's
                             KML polygon.
    reach_polygon.json      REAL traced polygon (lon/lat), for the map.
    landmarks.json          REAL localities/bridges/confluences snapped
                             onto the real chainage (or auto-placed if the
                             KML had no named points — tagged source:"auto").
    depth_profile.json      OPTIONAL. REAL per-station water depth, from a
                             bathymetry survey grid. Where absent, bed
                             elevation falls back to a synthetic toy formula.
    meta.json                OPTIONAL. Present for rivers added via KML
                             upload (naditwin.kml_ingest) — records the
                             display name so the server can rediscover the
                             river on restart.

What is REAL for every river: centerline, chainage, cross-section width
(from the KML), and locality/bridge names & positions (snapped to that
chainage, or honestly tagged "auto" when the KML carried no named points).

What is STILL SYNTHETIC for every river: absolute elevation datum,
downstream slope, embankment/bank crest height, discharge (Q), river
stage time series, hydrograph, forecasts, margins, and alerts — none of
these have a real DEM or real gauge/sensor feed behind them yet. Plug a
real discharge/stage CSV in via `--gauge your_file.csv`.

TIME MODEL — real calendar, not an abstract "sim hour":
  Every hourly value (past AND forecast) is indexed against a real UTC
  clock starting at EPOCH (2026-01-01 00:00 UTC) through "today" plus a
  runway of FUTURE_BUFFER_H hours. There is no more a mutable "now_idx"
  simulation clock — every query method takes an explicit `dt` (a
  timezone-aware datetime); passing dt=None means "right now" (live,
  interpolated to the current minute). Passing an explicit historic dt
  (any hour from EPOCH up to the current real hour) returns exactly what
  that hour's board looked like — past hourly, forecast-from-there, and
  everything else consistently anchored to that moment. This makes the
  engine itself stateless/request-scoped: two requests for two different
  `dt` values never interfere with each other.
"""

import json
import math
import random
import csv
import os
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(__file__)
RIVERS_DIR = os.path.join(HERE, "rivers")

EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)   # "past data from Jan 2026 to date"
PAST_WINDOW_H = 72        # default look-back window for the observed hydrograph
FORECAST_HOURS = 72
N_MEMBERS = 50
FUTURE_BUFFER_H = 24 * 45  # ~45 days of runway past "real now" — avoids having
                            # to extend the truth series mid-demo

# --- synthetic-only knobs (no real datum / gauge available for any river yet) ---
WSE_DATUM_M = 0.0        # ARBITRARY relative datum: 0 m = assumed water surface
                          # at survey time. Real MSL elevation needs a DEM.
TOY_SLOPE = 0.0009        # synthetic downstream slope, m/m — only so the toy
                          # hydraulics has a flow direction; not measured.
DEPTH_COEF = 0.10         # hydraulic-geometry style depth = c * Q^0.6 * width_factor
DEPTH_EXP = 0.6
MONSOON_MONTHS = (6, 7, 8, 9)   # Jun-Sep — used only to season the SYNTHETIC
SHOULDER_MONTHS = (5, 10)       # discharge curve; real gauge data overrides this.


def utc_now():
    return datetime.now(timezone.utc)


def parse_iso(s):
    """Parse an ISO-ish datetime string (from the frontend's <input type=date>
    + hour dropdown, joined as 'YYYY-MM-DDTHH:00:00Z') into an aware UTC dt."""
    if s is None:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def hours_since_epoch(dt):
    return int((dt - EPOCH).total_seconds() // 3600)


# ------------------------------ river registry ------------------------------

class RiverData:
    """Loads and holds the REAL, pre-computed geometry layers for one river."""

    def __init__(self, key, display_name, product_label, data_dir=None):
        self.key = key
        self.display_name = display_name
        self.product_label = product_label
        d = os.path.join(data_dir or RIVERS_DIR, key)

        with open(os.path.join(d, "chainage_profile.json")) as f:
            stations = json.load(f)
        with open(os.path.join(d, "reach_polygon.json")) as f:
            self.reach_polygon_lonlat = json.load(f)
        lm_path = os.path.join(d, "landmarks.json")
        if os.path.exists(lm_path):
            with open(lm_path) as f:
                self.landmarks = json.load(f)
        else:
            self.landmarks = []

        self.n_cells = len(stations)
        self.chainage_m = [s["chainage_m"] for s in stations]
        self.lon = [s["lon"] for s in stations]
        self.lat = [s["lat"] for s in stations]
        self.real_width_m = [s["width_m"] for s in stations]
        self.reach_len_m = self.chainage_m[-1]
        self.mean_width = sum(self.real_width_m) / len(self.real_width_m)

        depth_path = os.path.join(d, "depth_profile.json")
        if os.path.exists(depth_path):
            with open(depth_path) as f:
                depth_stations = json.load(f)
            self.has_real_depth = True
            self.real_depth_m = [ds["depth_m"] for ds in depth_stations]
            self.depth_flagged = [bool(ds["flagged"]) for ds in depth_stations]
            self.mean_depth = sum(self.real_depth_m) / len(self.real_depth_m)
        else:
            self.has_real_depth = False
            self.real_depth_m = [0.0] * self.n_cells
            self.depth_flagged = [False] * self.n_cells
            self.mean_depth = 0.0


BUILTIN_RIVERS = {
    "Mula-Mutha": dict(
        key="mula_mutha",
        product_label="NadiTwin — Mula-Mutha reach, Pune (real KML geometry + real depth survey)",
    ),
    "Mithi": dict(
        key="mithi",
        product_label="NadiTwin — Mithi River reach, Mumbai (real KML geometry)",
    ),
}
DEFAULT_RIVER = "Mula-Mutha"


def load_builtin_rivers():
    return {
        name: RiverData(key=cfg["key"], display_name=name, product_label=cfg["product_label"])
        for name, cfg in BUILTIN_RIVERS.items()
    }


def discover_uploaded_rivers(existing_keys, data_dir=RIVERS_DIR):
    """Scan naditwin/rivers/ for folders carrying a meta.json (written by
    kml_ingest.build_river_from_kml) that aren't one of the built-ins — i.e.
    rivers added via the /api/upload_kml endpoint in an earlier run. Lets an
    uploaded river survive a server restart without re-uploading the KML."""
    out = {}
    if not os.path.isdir(data_dir):
        return out
    for slug in sorted(os.listdir(data_dir)):
        d = os.path.join(data_dir, slug)
        meta_path = os.path.join(d, "meta.json")
        if slug in existing_keys or not os.path.isfile(meta_path):
            continue
        try:
            with open(meta_path) as f:
                m = json.load(f)
            display_name = m.get("display_name") or slug.replace("_", " ").title()
            rd = RiverData(
                key=slug,
                display_name=display_name,
                product_label=(
                    f"NadiTwin — {display_name} (uploaded KML, "
                    f"{m.get('landmarks_source', 'auto')} landmarks)"
                ),
                data_dir=data_dir,
            )
            out[display_name] = rd
        except Exception:
            continue
    return out


def load_all_rivers():
    rivers = load_builtin_rivers()
    existing_keys = {cfg["key"] for cfg in BUILTIN_RIVERS.values()}
    upload_dir = os.path.join("/tmp", "naditwin", "rivers") if os.environ.get("VERCEL") else RIVERS_DIR
    rivers.update(discover_uploaded_rivers(existing_keys, upload_dir))
    return rivers


# --------------------------------- engine ---------------------------------

class TwinEngine:
    def __init__(self, river: RiverData, seed=42, gauge_csv=None):
        self.river = river
        self.seed = seed
        self._build_geometry()
        self._build_truth_discharge(gauge_csv)
        self._build_assets()
        self._members_cache = {}

    # ------------------------- geometry / bathymetry -------------------------

    def _build_geometry(self):
        """bed = ARBITRARY relative datum MINUS the REAL measured depth at
        that station where a real depth survey was supplied, plus a small
        SYNTHETIC downstream slope so the toy hydraulics has a flow
        direction. Where no real depth survey exists, bed falls back fully
        to a synthetic toy formula. crest and sigma_bed remain
        synthetic/toy for every river, pending a real DEM / structural
        survey."""
        r = self.river
        rng = random.Random(self.seed + 1)
        self.bed = []
        self.crest = []
        self.width_factor = []
        self.sigma_bed = []
        for i in range(r.n_cells):
            x = r.chainage_m[i]
            slope_component = WSE_DATUM_M - TOY_SLOPE * x   # synthetic trend only

            if r.has_real_depth:
                relief_component = -(r.real_depth_m[i] - r.mean_depth)   # REAL, from survey
                bed = slope_component + relief_component
            else:
                pools = 0.6 * math.sin(2 * math.pi * x / 500.0) \
                      + 0.3 * math.sin(2 * math.pi * x / 140.0 + 1.3)
                noise = rng.gauss(0, 0.12)
                bed = slope_component + pools + noise
            self.bed.append(bed)

            crest = bed + 6.0 + 0.5 * math.sin(2 * math.pi * x / 700.0 + 0.7)
            if r.real_width_m[i] < r.mean_width * 0.55:
                crest -= 1.4
            self.crest.append(crest)

            wf = r.real_width_m[i] / r.mean_width
            self.width_factor.append(max(0.15, min(2.2, wf)))

            edge_frac = min(x, r.reach_len_m - x) / (r.reach_len_m / 2.0)
            s = 0.6 - 0.4 * edge_frac
            if r.has_real_depth and r.depth_flagged[i]:
                s = max(s, 0.5)
            self.sigma_bed.append(round(max(0.12, s), 3))

    # ------------------------- discharge truth series -------------------------

    def _build_truth_discharge(self, gauge_csv):
        now_idx = hours_since_epoch(utc_now())
        truth_hours = max(now_idx + FUTURE_BUFFER_H, PAST_WINDOW_H + FORECAST_HOURS + 4)
        self.truth_hours = truth_hours
        self.q_truth = None

        if gauge_csv and os.path.exists(gauge_csv):
            try:
                vals = []
                with open(gauge_csv, newline="") as f:
                    for row in csv.reader(f):
                        if not row:
                            continue
                        try:
                            vals.append(float(row[-1]))
                        except ValueError:
                            continue
                if len(vals) >= 24:
                    self.q_truth = vals[:truth_hours]
                    fill = self.q_truth[-1] if self.q_truth else 150.0
                    while len(self.q_truth) < truth_hours:
                        self.q_truth.append(fill)
                    self.q_source = "user_csv:" + os.path.basename(gauge_csv)
            except Exception:
                self.q_truth = None

        if self.q_truth is None:
            rng = random.Random(self.seed + 2)
            base = 180.0
            pulses = []
            t = 20
            while t < truth_hours:
                amp = rng.uniform(200, 1400)
                width = rng.uniform(6, 20)
                pulses.append((t, amp, width))
                t += int(rng.uniform(40, 110))
            q = []
            for h in range(truth_hours):
                month = (EPOCH + timedelta(hours=h)).month
                if month in MONSOON_MONTHS:
                    season = 2.1
                elif month in SHOULDER_MONTHS:
                    season = 1.35
                else:
                    season = 0.75
                v = base * season + 30 * math.sin(2 * math.pi * h / 240.0)
                for (pt, amp, w) in pulses:
                    pmonth = (EPOCH + timedelta(hours=pt)).month
                    pseason = 1.7 if pmonth in MONSOON_MONTHS else 0.55
                    v += amp * pseason * math.exp(-0.5 * ((h - pt) / w) ** 2)
                v *= (1 + rng.gauss(0, 0.015))
                q.append(max(40.0, v))
            self.q_truth = q
            self.q_source = ("synthetic_demo (no gauge/sensor feed supplied — "
                              "seasonal monsoon-shaped synthetic series, Jan 2026 to date)")

    # ------------------------------- assets -------------------------------

    def _build_assets(self):
        r = self.river

        def crest_at(x_m):
            return self.crest[self._cell_for_chainage(x_m)]

        self.assets = []
        if r.landmarks:
            ordered = sorted(r.landmarks, key=lambda lm: lm["chainage_m"])
            for i, lm in enumerate(ordered):
                x = lm["chainage_m"]
                cell = self._cell_for_chainage(x)
                aid = f"A{i+1}"
                self.assets.append({
                    "id": aid,
                    "name": lm["name"],
                    "locality": lm["name"],
                    "type": lm.get("type", "locality"),
                    "source": lm.get("source", "auto"),
                    "chainage_m": round(x, 1),
                    "lon": r.lon[cell], "lat": r.lat[cell],
                    "threshold": round(crest_at(x) - 0.8, 2),
                })
        else:
            picks = [
                ("A1", "Upstream reach", r.reach_len_m * 0.10),
                ("A2", "Narrow section (left bank)", r.reach_len_m * 0.30),
                ("A3", "Mid-reach crossing", r.reach_len_m * 0.50),
                ("A4", "Narrow section (right bank)", r.reach_len_m * 0.70),
                ("A5", "Downstream reach", r.reach_len_m * 0.90),
            ]
            for aid, name, x in picks:
                cell = self._cell_for_chainage(x)
                self.assets.append({
                    "id": aid, "name": name, "type": "locality", "source": "auto",
                    "chainage_m": round(x, 1),
                    "lon": r.lon[cell], "lat": r.lat[cell],
                    "threshold": round(crest_at(x) - 0.8, 2),
                })

    def _cell_for_chainage(self, x_m):
        r = self.river
        best = 0
        best_d = abs(r.chainage_m[0] - x_m)
        for i, c in enumerate(r.chainage_m):
            d = abs(c - x_m)
            if d < best_d:
                best, best_d = i, d
        return best

    # --------------------------- hydraulic mapping ---------------------------

    def _depth_from_q(self, q, cell):
        return DEPTH_COEF * (q ** DEPTH_EXP) / max(0.4, self.width_factor[cell])

    def wse_profile(self, q):
        r = self.river
        prof = []
        for i in range(r.n_cells):
            att = 1.0 - 0.08 * (i / r.n_cells)
            d = self._depth_from_q(q * att, i)
            prof.append(self.bed[i] + d)
        return prof

    def wse_at(self, q, cell):
        att = 1.0 - 0.08 * (cell / self.river.n_cells)
        return self.bed[cell] + self._depth_from_q(q * att, cell)

    # -------------------------------- time index --------------------------------

    def idx_for(self, dt=None):
        """Whole-hour index into q_truth for a given (or, if None, current) dt."""
        if dt is None:
            dt = utc_now()
        idx = hours_since_epoch(dt)
        return max(0, min(len(self.q_truth) - FORECAST_HOURS - 2, idx))

    def live_idx_and_frac(self):
        """Live mode: fractional-hour position so discharge/stage move
        smoothly minute-by-minute between the underlying hourly points."""
        now = utc_now()
        total_h = (now - EPOCH).total_seconds() / 3600.0
        max_idx = len(self.q_truth) - FORECAST_HOURS - 2
        idx = max(0, min(max_idx, int(total_h)))
        frac = 0.0 if idx == max_idx else max(0.0, min(1.0, total_h - int(total_h)))
        return idx, frac

    def q_at_time(self, dt=None):
        """Returns (q, hour_idx). dt=None => live, interpolated to the
        current minute. Explicit dt => that historic hour, exact (no
        interpolation, since the user picked a specific hour)."""
        if dt is None:
            idx, frac = self.live_idx_and_frac()
            q0 = self.q_truth[idx]
            q1 = self.q_truth[min(idx + 1, len(self.q_truth) - 1)]
            return q0 + (q1 - q0) * frac, idx
        idx = self.idx_for(dt)
        return self.q_truth[idx], idx

    # ------------------------------- forecasting ------------------------------

    def _members(self, idx):
        cached = self._members_cache.get(idx)
        if cached is not None:
            return cached
        members = []
        for m in range(N_MEMBERS):
            rng = random.Random(self.seed * 1000 + idx * 7 + m)
            phase = rng.gauss(0, 2.5)
            bias = rng.gauss(0, 0.04)
            series = []
            noise = 0.0
            for k in range(1, FORECAST_HOURS + 1):
                noise += rng.gauss(0, 0.012)
                pos = idx + k + phase
                i0 = max(0, min(len(self.q_truth) - 2, int(pos)))
                frac = min(1.0, max(0.0, pos - i0))
                q = self.q_truth[i0] * (1 - frac) + self.q_truth[i0 + 1] * frac
                series.append(max(30.0, q * (1 + bias + noise)))
            members.append(series)
        if len(self._members_cache) > 12:
            self._members_cache.clear()
        self._members_cache[idx] = members
        return members

    def q_quantiles(self, idx, k, qs=(0.1, 0.5, 0.9)):
        vals = sorted(m[k] for m in self._members(idx))
        out = []
        for q in qs:
            pos = q * (len(vals) - 1)
            lo = int(pos)
            hi = min(lo + 1, len(vals) - 1)
            out.append(vals[lo] + (vals[hi] - vals[lo]) * (pos - lo))
        return out

    # ------------------------------- analytics -------------------------------

    def state_now(self, dt=None):
        q, idx = self.q_at_time(dt)
        at = dt or utc_now()
        return {
            "q_now": round(q, 1),
            "wse": [round(v, 3) for v in self.wse_profile(q)],
            "at": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "hour_index": idx,
            "live": dt is None,
        }

    def observed_hydrograph(self, cell, dt=None, window=PAST_WINDOW_H):
        idx = self.idx_for(dt)
        lo = max(0, idx - window + 1)
        return [round(self.wse_at(self.q_truth[h], cell), 3) for h in range(lo, idx + 1)]

    def forecast_hydrograph(self, cell, dt=None):
        idx = self.idx_for(dt)
        med, p10, p90 = [], [], []
        for k in range(FORECAST_HOURS):
            q10, q50, q90 = self.q_quantiles(idx, k)
            p10.append(round(self.wse_at(q10, cell), 3))
            med.append(round(self.wse_at(q50, cell), 3))
            p90.append(round(self.wse_at(q90, cell), 3))
        return {"median": med, "p10": p10, "p90": p90}

    def forecast_profile(self, lead_h, dt=None):
        idx = self.idx_for(dt)
        k = max(0, min(FORECAST_HOURS - 1, lead_h - 1))
        q10, q50, q90 = self.q_quantiles(idx, k)
        return {
            "lead_h": lead_h,
            "median": [round(v, 3) for v in self.wse_profile(q50)],
            "p10": [round(v, 3) for v in self.wse_profile(q10)],
            "p90": [round(v, 3) for v in self.wse_profile(q90)],
        }

    def today_series(self, cell, dt=None, tz_offset_min=0):
        """Calendar-day view (00:00-23:00 of the given/live date, in the
        caller's local timezone if tz_offset_min is given): hours up to
        'now' (or the historic dt) are OBSERVED from the truth series;
        hours later in the same day are the FORECAST (median + P10-P90)
        generated from 'now'/dt forward — exactly what the person asked
        for: today so far, plus what the rest of today could look like.

        tz_offset_min is minutes EAST of UTC (e.g. +330 for IST). All
        underlying indexing into q_truth stays in UTC (the source of
        truth); only the calendar-day boundary and the displayed date
        are shifted, so 'today' and 'now' match the caller's wall clock
        instead of always landing on the UTC day/hour."""
        at = dt or utc_now()
        tz_delta = timedelta(minutes=tz_offset_min)
        at_local = at + tz_delta  # wall-clock reading, kept as a UTC-tagged dt for arithmetic
        day_start_local = at_local.replace(hour=0, minute=0, second=0, microsecond=0)
        day_start_utc = day_start_local - tz_delta  # the real UTC instant that local midnight falls on
        idx0 = self.idx_for(day_start_utc)
        now_idx = self.idx_for(at)
        # cur_hour is the local wall-clock hour itself (always exact). idx0 is
        # only floored to the nearest whole UTC hour, which for a non-whole-hour
        # offset like IST (+5:30) can land up to ~30min early — fine for the
        # other 23 hourly-average buckets, but the "now" bucket below uses
        # now_idx directly so its value isn't blurred by that rounding.
        cur_hour = max(0, min(23, at_local.hour))
        hours = list(range(24))
        q_obs, q_med, q_p10, q_p90 = [], [], [], []
        wse_obs, wse_med, wse_p10, wse_p90 = [], [], [], []
        for h in hours:
            idx = now_idx if h == cur_hour else idx0 + h
            if h <= cur_hour:
                q = self.q_truth[min(idx, len(self.q_truth) - 1)]
                q_obs.append(round(q, 1)); q_med.append(None); q_p10.append(None); q_p90.append(None)
                wse_obs.append(round(self.wse_at(q, cell), 3))
                wse_med.append(None); wse_p10.append(None); wse_p90.append(None)
            else:
                k = h - cur_hour - 1
                q10, q50, q90 = self.q_quantiles(now_idx, k)
                q_obs.append(None); q_med.append(round(q50, 1)); q_p10.append(round(q10, 1)); q_p90.append(round(q90, 1))
                wse_obs.append(None)
                wse_med.append(round(self.wse_at(q50, cell), 3))
                wse_p10.append(round(self.wse_at(q10, cell), 3))
                wse_p90.append(round(self.wse_at(q90, cell), 3))
        return {
            "date": day_start_local.strftime("%Y-%m-%d"),
            "hours": hours,
            "current_hour": cur_hour,
            "tz_offset_min": tz_offset_min,
            "q_observed": q_obs, "q_forecast_median": q_med, "q_forecast_p10": q_p10, "q_forecast_p90": q_p90,
            "wse_observed": wse_obs, "wse_forecast_median": wse_med, "wse_forecast_p10": wse_p10, "wse_forecast_p90": wse_p90,
        }

    def margins(self, dt=None):
        idx = self.idx_for(dt)
        q_now = self.q_truth[idx]
        members = self._members(idx)
        out = []
        for a in self.assets:
            cell = self._cell_for_chainage(a["chainage_m"])
            wse_now = self.wse_at(q_now, cell)
            margin_now = a["threshold"] - wse_now
            exceed_members = 0
            tth = None
            min_margin_med = margin_now
            for k in range(FORECAST_HOURS):
                q10, q50, q90 = self.q_quantiles(idx, k)
                m_med = a["threshold"] - self.wse_at(q50, cell)
                min_margin_med = min(min_margin_med, m_med)
                if tth is None and m_med <= 0:
                    tth = k + 1
            for m in members:
                if any(self.wse_at(m[k], cell) >= a["threshold"] for k in range(FORECAST_HOURS)):
                    exceed_members += 1
            p = exceed_members / N_MEMBERS
            if p >= 0.6 or margin_now <= 0:
                status = "DANGER"
            elif p >= 0.3:
                status = "WARNING"
            elif p >= 0.1:
                status = "WATCH"
            else:
                status = "SAFE"
            out.append({
                "id": a["id"], "name": a["name"], "type": a.get("type", "locality"),
                "chainage_m": a["chainage_m"], "lon": a["lon"], "lat": a["lat"],
                "threshold": a["threshold"],
                "wse_now": round(wse_now, 2),
                "margin_now_m": round(margin_now, 2),
                "min_margin_median_m": round(min_margin_med, 2),
                "p_exceed_72h": round(p, 2),
                "time_to_threshold_h": tth,
                "status": status,
            })
        return out

    def alerts(self, dt=None):
        out = []
        for m in self.margins(dt):
            if m["status"] in ("WATCH", "WARNING", "DANGER"):
                msg = (f"{m['name']}: probability of threshold exceedance in next "
                       f"72 h is {int(m['p_exceed_72h']*100)}%.")
                if m["time_to_threshold_h"]:
                    msg += (f" Median forecast crosses threshold in "
                            f"~{m['time_to_threshold_h']} h.")
                msg += f" Current margin {m['margin_now_m']} m."
                out.append({"severity": m["status"], "asset": m["id"], "message": msg})
        return out

    def scorecard(self, dt=None):
        idx = self.idx_for(dt)
        if idx - 24 < 0:
            return {"note": "insufficient history yet", "rows": []}
        past_idx = idx - 24
        cell = self._cell_for_chainage(self.assets[0]["chainage_m"])
        errs = []
        hits = misses = false_alarms = 0
        thr = self.assets[0]["threshold"]
        for k in range(24):
            q10, q50, q90 = self.q_quantiles(past_idx, k)
            pred = self.wse_at(q50, cell)
            truth = self.wse_at(self.q_truth[past_idx + 1 + k], cell)
            errs.append((pred - truth) ** 2)
            pe = pred >= thr
            te = truth >= thr
            if pe and te:
                hits += 1
            elif te and not pe:
                misses += 1
            elif pe and not te:
                false_alarms += 1
        rmse = math.sqrt(sum(errs) / len(errs))
        return {
            "note": "",
            "rows": [
                {"metric": f"WSE 24 h forecast RMSE at {self.assets[0]['name']}",
                 "value": f"{rmse:.2f} m"},
                {"metric": "Threshold-exceedance hits (24 h)", "value": str(hits)},
                {"metric": "Misses", "value": str(misses)},
                {"metric": "False alarms", "value": str(false_alarms)},
                {"metric": "Ensemble size", "value": str(N_MEMBERS)},
                {"metric": "Data source", "value": self.q_source},
            ],
        }

    def meta(self):
        r = self.river
        now = utc_now()
        n_bridges = sum(1 for lm in r.landmarks if lm.get("type") == "bridge")
        n_confluences = sum(1 for lm in r.landmarks if lm.get("type") == "confluence")
        span = f"{r.landmarks[0]['name']} \u2192 {r.landmarks[-1]['name']}" if r.landmarks else ""
        max_idx = len(self.q_truth) - FORECAST_HOURS - 2
        return {
            "product": r.product_label,
            "river": r.display_name,
            "disclaimer": (
                "REAL layers: centerline/chainage/width from the KML polygon"
                + (", local bed relief (relative depth variation) from a real bathymetry survey grid"
                   if r.has_real_depth else "")
                + ". STILL SYNTHETIC: absolute elevation datum and downstream "
                  "slope (no DEM supplied), embankment crest (no structural "
                  "survey)"
                + ("" if r.has_real_depth else ", and bed elevation itself (no depth survey supplied for this river)")
                + ", and discharge / river stage / hydrograph / forecasts / "
                  "margins / alerts (no gauge or sensor feed supplied — plug "
                  "one in via `--gauge your_file.csv`). Do not use for any "
                  "real decision until the DEM and gauge layers are supplied."
            ),
            "has_real_depth": r.has_real_depth,
            "reach_km": round(r.reach_len_m / 1000.0, 3),
            "n_cells": r.n_cells,
            "chainage_step_m": round((r.reach_len_m / max(1, r.n_cells - 1)), 1),
            "past_window_hours": PAST_WINDOW_H,
            "forecast_hours": FORECAST_HOURS,
            "members": N_MEMBERS,
            "q_source": self.q_source,
            "assets": self.assets,
            "n_bridges": n_bridges,
            "n_confluences": n_confluences,
            "n_landmarks": len(r.landmarks),
            "reach_span": span,
            "landmarks": r.landmarks,
            "chainage_km": [round(v / 1000.0, 4) for v in r.chainage_m],
            "lon": r.lon,
            "lat": r.lat,
            "real_width_m": r.real_width_m,
            "real_depth_m": r.real_depth_m,
            "depth_flagged": r.depth_flagged,
            "reach_polygon_lonlat": r.reach_polygon_lonlat,
            "bed": [round(v, 3) for v in self.bed],
            "crest": [round(v, 3) for v in self.crest],
            "sigma_bed": self.sigma_bed,
            # time range for the frontend's Live/Historic calendar picker
            "epoch_date": EPOCH.strftime("%Y-%m-%d"),
            "now_iso": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "today_date": now.strftime("%Y-%m-%d"),
            "today_hour": now.hour,
            "max_index_date": (EPOCH + timedelta(hours=max_idx)).strftime("%Y-%m-%d"),
        }
