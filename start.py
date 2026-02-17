"""Launcher for the ADS-B server.

Receives raw hex ADS-B messages from remote collectors via TCP,
decodes them, and serves the web UI and REST API.

Usage:
  python start.py
  python start.py --host 0.0.0.0 --port 8080

Press Ctrl+C to shut down.
"""

import argparse
import json
import os
import sys


def main():
    # Load config to get defaults
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    config = {
        "http_port": 8080,
        "collector_port": 4002,
        "latitude": 38.85596396471333,
        "longitude": -77.04951658878798,
    }
    if os.path.isfile(config_path):
        try:
            with open(config_path, "r") as f:
                config.update(json.load(f))
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="ADS-B Server")
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="HTTP bind address (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=config.get("http_port", 8080),
        help=f"HTTP bind port (default: {config.get('http_port', 8080)})",
    )
    args = parser.parse_args()

    collector_port = config.get("collector_port", 4002)

    print()
    print("[server] ADS-B Server")
    print(f"[server] HTTP server:     http://{args.host}:{args.port}")
    print(f"[server] Collector port:  {collector_port} (TCP raw hex)")
    print(f"[server] Map UI:          http://{args.host}:{args.port}/")
    print(f"[server] Aircraft API:    http://{args.host}:{args.port}/api/aircraft")
    print(f"[server] Stats API:       http://{args.host}:{args.port}/api/stats")
    print(f"[server] Collectors API:  http://{args.host}:{args.port}/api/collectors")
    print()

    try:
        import uvicorn
        uvicorn.run(
            "app.main:app",
            host=args.host,
            port=args.port,
            log_level="info",
        )
    except KeyboardInterrupt:
        print("\n[server] Shutting down...")

    print("[server] Done.")


if __name__ == "__main__":
    main()
