#!/usr/bin/env python3
"""NadiTwin demo — plug-and-play launcher.

Usage:
    python3 run.py                 # http://localhost:8003
    python3 run.py --port 9000
    python3 run.py --seed 7        # different synthetic monsoon
    python3 run.py --gauge data/my_gauge.csv   # plug in your own discharge CSV
                                   # (one value per hour, last column numeric)

No external dependencies. Python 3.8+.
SYNTHETIC DEMONSTRATION DATA — not for real-world decisions.
"""
import argparse
import threading
import webbrowser

from naditwin.server import serve


def main():
    p = argparse.ArgumentParser(description="NadiTwin demo server")
    p.add_argument("--port", type=int, default=8003)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--gauge", type=str, default=None,
                   help="optional CSV of hourly discharge values (plug-in point)")
    p.add_argument("--no-browser", action="store_true")
    a = p.parse_args()

    if not a.no_browser:
        threading.Timer(
            1.0, lambda: webbrowser.open(f"http://localhost:{a.port}")
        ).start()
    serve(port=a.port, seed=a.seed, gauge_csv=a.gauge)


if __name__ == "__main__":
    main()