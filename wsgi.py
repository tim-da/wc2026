"""WSGI entry point for production servers (gunicorn).

The Flask app lives in a hyphenated directory (`outputs/world-cup-tracker/`),
which is not a valid Python module path, so it is loaded by file path.

Run with:  gunicorn wsgi:app
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

SERVER_PATH = Path(__file__).resolve().parent / "outputs" / "world-cup-tracker" / "server.py"
_spec = importlib.util.spec_from_file_location("wc_server", SERVER_PATH)
_server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_server)

# gunicorn looks for `app` by default (gunicorn wsgi:app).
app = _server.APP
