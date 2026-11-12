"""Service-only interpreter bridge; the verified PDF component stays unchanged."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess

from .single_session_pdf import pdf_receipt, render_pdf


def render_service_pdf(source, target):
    interpreter = os.environ.get('TRAVEL_PDF_PYTHON')
    if not interpreter and importlib.util.find_spec('playwright') is not None:
        return render_pdf(source, target)
    interpreter = interpreter or shutil.which('python3')
    if not interpreter:
        raise RuntimeError('No PDF interpreter; configure TRAVEL_PDF_PYTHON')
    cli = Path(__file__).resolve().parents[2] / 'tools' / 'render_handbook_pdf.py'
    result = subprocess.run([interpreter, str(cli), str(source), str(target)],
                            capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise RuntimeError(f'PDF subprocess exit {result.returncode}: {result.stderr[-8000:]}')
    return pdf_receipt(source, target)
