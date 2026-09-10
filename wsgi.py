"""WSGI entry for Gunicorn / Render.
Loads Flask app from server.py (NOT from a package named app/).
"""
from server import app, application  # noqa: F401

__all__ = ['app', 'application']
