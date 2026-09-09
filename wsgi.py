"""WSGI entrypoint for Gunicorn / Render."""
from app import app, application  # noqa: F401

# Explicit export for gunicorn wsgi:app
__all__ = ['app', 'application']
