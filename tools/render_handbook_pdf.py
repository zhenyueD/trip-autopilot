#!/usr/bin/env python3
"""Ops-owned host entry point; same implementation as SessionService, no model calls.

Usage: python3 tools/render_handbook_pdf.py /absolute/document.html /absolute/document.pdf
Run on the host when sandbox Chromium is unavailable. No service restart required.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packages.travel_agent.single_session_pdf import render_pdf

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('html', type=Path)
    parser.add_argument('pdf', type=Path)
    parser.add_argument('--offline', action='store_true', help='deny all external image requests')
    args = parser.parse_args()
    print(json.dumps(render_pdf(args.html, args.pdf, allow_remote_images=not args.offline), ensure_ascii=False, indent=2))
