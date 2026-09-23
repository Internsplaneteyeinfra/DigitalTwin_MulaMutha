"""NadiTwin demo server — pure standard library (http.server), multi-river,
real-calendar time model, and live KML upload.

Endpoints (all JSON unless noted). Every GET endpoint below (except `/`,
`/api/rivers`) accepts an optional `?river=<name>` query param to pick
which river's engine to use (defaults to the first configured river), and
an optional `?at=<ISO8601 UTC datetime>` (e.g. `2026-06-15T13:00:00Z`) to
view a HISTORIC hour instead of live/current. Omitting `at` = live mode:
values are interpolated to the current minute and the endpoints intended
to auto-refresh should be polled roughly once a minute by the client.

  GET  /                       -> dashboard (HTML)
  GET  /api/rivers             -> list of loaded rivers [{name, reach_km}]
  GET  /api/timerange          -> {epoch_date, today_date, today_hour, now_iso}
                                   for populating the Live/Historic date picker
  GET  /api/meta               -> reach geometry, assets, landmarks, disclaimer
  GET  /api/state              -> discharge + WSE profile at `at` (or live)
  GET  /api/forecast/profile?lead=24
  GET  /api/hydrograph?cell=520
  GET  /api/margins            -> margin-to-threshold board
  GET  /api/alerts             -> active alerts
  GET  /api/scorecard          -> hindcast-scored metrics (synthetic)
  GET  /api/today?cell=&river=&at=
                                -> today's calendar day at one chainage cell:
                                   hours already passed = observed, hours
                                   still to come today = forecast (median +
                                   P10-P90), for both discharge and stage
  POST /api/upload_kml?name=<display name>
                                -> body = raw KML/XML text. Derives chainage
                                   (100 m stations), width, and bridge/locality
                                   landmarks from the KML, registers a brand
                                   new river, and returns an ingest summary.
                                   The new river is immediately selectable via
                                   `?river=<display name>` on every endpoint
                                   above, and survives a server restart.
                                   Single-slot: any PREVIOUSLY uploaded river
                                   (built-ins are never touched) is dropped
                                   and its on-disk folder deleted — so a new
                                   upload always REPLACES the last uploaded
                                   river rather than piling up in the list.
  POST /api/remove_river?name=<display name>
                                -> drops an uploaded river from the running
                                   server AND deletes its on-disk folder, so
                                   it's gone from the river list for good.
                                   Refuses on the two built-in rivers.
"""

import json
import os
import shutil
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

from .engine import TwinEngine, load_all_rivers, DEFAULT_RIVER, BUILTIN_RIVERS, parse_iso, EPOCH, utc_now
from . import kml_ingest

STATIC = os.path.join(os.path.dirname(__file__), "static")

ENGINES = {}        # river display name -> TwinEngine, filled in serve()
_SEED = 42
_GAUGE_CSV = None

MAX_UPLOAD_BYTES = 25 * 1024 * 1024   # 25 MB — generous for a hand-traced KML


def _pick_engine(q):
    name = q.get("river", [DEFAULT_RIVER])[0]
    return ENGINES.get(name, ENGINES.get(DEFAULT_RIVER) or next(iter(ENGINES.values())))


def _pick_dt(q):
    """?at=<ISO8601> -> aware UTC datetime (historic). Missing/empty -> None (live)."""
    raw = q.get("at", [None])[0]
    if not raw:
        return None
    try:
        return parse_iso(unquote(raw))
    except Exception:
        return None


class Handler(BaseHTTPRequestHandler):

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)

    def log_message(self, fmt, *args):  # quieter console
        pass

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html", "/dashboard"):
                with open(os.path.join(STATIC, "dashboard.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")

            if u.path == "/api/rivers":
                return self._send(200, [
                    {"name": name, "reach_km": eng.meta()["reach_km"],
                     "uploaded": name not in BUILTIN_RIVERS}
                    for name, eng in ENGINES.items()
                ])

            if u.path == "/api/timerange":
                now = utc_now()
                return self._send(200, {
                    "epoch_date": EPOCH.strftime("%Y-%m-%d"),
                    "today_date": now.strftime("%Y-%m-%d"),
                    "today_hour": now.hour,
                    "now_iso": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                })

            engine = _pick_engine(q)
            dt = _pick_dt(q)

            if u.path == "/api/meta":
                return self._send(200, engine.meta())
            if u.path == "/api/state":
                return self._send(200, engine.state_now(dt))
            if u.path == "/api/forecast/profile":
                lead = int(q.get("lead", ["24"])[0])
                return self._send(200, engine.forecast_profile(lead, dt))
            if u.path == "/api/hydrograph":
                n_cells = engine.river.n_cells
                cell = int(q.get("cell", [str(n_cells // 2)])[0])
                cell = max(0, min(n_cells - 1, cell))
                return self._send(200, {
                    "cell": cell,
                    "observed": engine.observed_hydrograph(cell, dt),
                    "forecast": engine.forecast_hydrograph(cell, dt),
                })
            if u.path == "/api/margins":
                return self._send(200, engine.margins(dt))
            if u.path == "/api/alerts":
                return self._send(200, engine.alerts(dt))
            if u.path == "/api/scorecard":
                return self._send(200, engine.scorecard(dt))
            if u.path == "/api/today":
                n_cells = engine.river.n_cells
                cell = int(q.get("cell", [str(n_cells // 2)])[0])
                cell = max(0, min(n_cells - 1, cell))
                try:
                    tz_offset_min = int(q.get("tz", ["0"])[0])
                except (TypeError, ValueError):
                    tz_offset_min = 0
                tz_offset_min = max(-720, min(840, tz_offset_min))  # clamp to real UTC offset range
                return self._send(200, engine.today_series(cell, dt, tz_offset_min))
            return self._send(404, {"error": "not found"})

        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            return self._send(500, {"error": str(e)})

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/api/upload_kml":
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0:
                    return self._send(400, {"error": "empty request body — POST the raw KML text"})
                if length > MAX_UPLOAD_BYTES:
                    return self._send(413, {"error": f"KML too large (> {MAX_UPLOAD_BYTES // (1024*1024)} MB)"})
                raw = self.rfile.read(length)
                try:
                    kml_text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    kml_text = raw.decode("utf-8", errors="replace")

                display_name = unquote(q.get("name", [""])[0]).strip()
                if not display_name:
                    display_name = f"Uploaded river {len(ENGINES)+1}"
                if display_name in BUILTIN_RIVERS:
                    return self._send(400, {"error": f'"{display_name}" is a built-in river name — pick a different name.'})

                try:
                    # re-uploading the same display name REPLACES it (same
                    # on-disk slug gets overwritten) instead of piling up
                    # near-duplicate rivers in the list.
                    summary = kml_ingest.build_river_from_kml(kml_text, display_name)
                except kml_ingest.KmlIngestError as e:
                    return self._send(400, {"error": str(e)})

                # single-slot uploads: any PREVIOUSLY uploaded river (built-ins
                # are never touched) is dropped now that the new KML has been
                # ingested successfully — so there's always at most one
                # uploaded river, and a fresh upload always replaces it in
                # place instead of piling up as a separate list entry.
                for old_name in [n for n in ENGINES if n not in BUILTIN_RIVERS and n != display_name]:
                    old_eng = ENGINES.pop(old_name, None)
                    if old_eng is not None:
                        old_dir = os.path.join(kml_ingest.UPLOADS_DIR, old_eng.river.key)
                        if os.path.isdir(old_dir):
                            shutil.rmtree(old_dir, ignore_errors=True)

                from .engine import RiverData
                rd = RiverData(
                    key=summary["key"], display_name=display_name,
                    product_label=(f"NadiTwin — {display_name} (uploaded KML, "
                                    f"{summary['landmarks_source']} landmarks)"),
                )
                ENGINES[display_name] = TwinEngine(rd, seed=_SEED, gauge_csv=_GAUGE_CSV)
                summary["display_name"] = display_name
                return self._send(200, summary)

            if u.path == "/api/remove_river":
                name = unquote(q.get("name", [""])[0]).strip()
                if not name:
                    return self._send(400, {"error": "missing ?name="})
                if name in BUILTIN_RIVERS:
                    return self._send(400, {"error": "cannot remove a built-in river"})
                eng = ENGINES.pop(name, None)
                if eng is None:
                    return self._send(404, {"error": f'river "{name}" not found'})
                slug = eng.river.key
                d = os.path.join(kml_ingest.UPLOADS_DIR, slug)
                if os.path.isdir(d):
                    shutil.rmtree(d, ignore_errors=True)
                return self._send(200, {"removed": name})

            return self._send(404, {"error": "not found"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            return self._send(500, {"error": str(e)})


def serve(port=8080, seed=42, gauge_csv=None):
    global ENGINES, _SEED, _GAUGE_CSV
    _SEED = seed
    _GAUGE_CSV = gauge_csv
    rivers = load_all_rivers()
    ENGINES = {name: TwinEngine(river, seed=seed, gauge_csv=gauge_csv) for name, river in rivers.items()}
    httpd = HTTPServer(("0.0.0.0", port), Handler)
    print(f"NadiTwin demo running at http://localhost:{port}  (Ctrl+C to stop)")
    print("Rivers loaded:", ", ".join(ENGINES.keys()))
    print("SYNTHETIC DEMONSTRATION DATA — not for real decisions.")
    httpd.serve_forever()
