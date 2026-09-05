# Knowsoft Churchgate

Church hierarchy, membership and growth analytics platform.

## Hierarchy
Global → Country → State → Group → District (primary data unit)

## Quick start
```bash
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

## Default General Admin
- URL: /ks-admin/login
- Email: admin@knowsoft.com
- Password: Admin@12345

## Render
- Build: `pip install -r requirements.txt`
- Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Env: `PYTHON_VERSION=3.12.8`, `SECRET_KEY=...`, `DATABASE_URL=...`
