"""Serve the existing single-session agent on a dedicated loopback port."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages"))
# Repo root as well: packages.* absolute imports (e.g. packages.atomizer)
# are a repo convention and must resolve under this entry point too.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from travel_agent.single_session_api import SessionHTTPServer, SessionService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True, help="private persistent session directory")
    parser.add_argument("--port", type=int, default=7392)
    parser.add_argument("--static-root", type=Path, help="optional prebuilt apps/v1 dist directory")
    args = parser.parse_args()
    if args.port == 7377:
        parser.error("7377 is reserved for the existing service")
    service = SessionService(args.directory)
    try:
        server = SessionHTTPServer(("127.0.0.1", args.port), service, args.static_root)
        print(f"Single-session API: http://127.0.0.1:{server.server_address[1]}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    finally:
        service.close()


if __name__ == "__main__":
    main()
