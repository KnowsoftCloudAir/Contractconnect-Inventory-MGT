# Fix Render: AppImportError (module 'app' has no attribute 'app')

## Root cause
Your GitHub repo contains **both**:
- `app.py` / `server.py` (Flask CONTRAconnect)
- **`app/` directory** (old FastAPI Churchgate package)

Python prefers the **`app/` package** over `app.py`. Gunicorn then loads the wrong module.

Render is also running `gunicorn app:app` from the **dashboard Start Command**, which overrides the Procfile.

## Fix (do all steps)

### 1. On GitHub — delete conflicting files
Delete these from the repo (they are a different product):
- entire folder **`app/`**
- **`main.py`** (FastAPI entry)

Keep only the CONTRAconnect layout:
```
server.py          ← Flask app (renamed so it never clashes with app/)
wsgi.py
Procfile
runtime.txt
requirements.txt
templates/
static/
README.md
```

### 2. On Render → Settings → Build & Deploy
**Start Command** (must set this explicitly):
```
gunicorn wsgi:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120
```

Do **not** use `gunicorn app:app`.

Optional: set Python to 3.12.x if the UI allows (runtime.txt is already python-3.12.8).

### 3. Redeploy
Push the cleaned repo → Manual Deploy.

## Verify
After deploy logs should show:
```
Booting worker with pid: ...
```
and no `AppImportError`.
