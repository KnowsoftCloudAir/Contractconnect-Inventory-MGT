"""WSGI entrypoint for Gunicorn / Render.

IMPORTANT: Do not name a package folder 'app/' in this repo — it shadows modules.
This loads the Flask application from server.py.
"""
from server import app, application  # noqa: F401

__all__ = ['app', 'application']
