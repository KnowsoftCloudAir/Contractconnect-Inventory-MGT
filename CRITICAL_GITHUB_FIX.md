# CRITICAL: Why dashboards show no sample data

Your GitHub repo still contains the **old FastAPI package** and is **missing** the latest Flask files.

## Required GitHub layout (repo root)

DELETE these (they break the deploy):
- folder `app/`
- file `main.py`
- old `app.py` if present

KEEP / UPLOAD from the latest zip:
- `server.py`   ← full Flask app with sample data
- `wsgi.py`     ← `from server import app`
- `Procfile`
- `requirements.txt`
- `runtime.txt`
- `templates/`  (including admin_sample_data.html)
- `static/`

## Render Start Command
```
gunicorn wsgi:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120
```

## After deploy
1. Log in as admin / project manager
2. Open **Ops dashboard** → click **Load / refresh sample data**
   OR open **Report sample data** → **Load sample data for all reports**
3. Refresh Cost analytics, Expense requests, Variance report

Sample data will not appear until this code is what Render is running.
