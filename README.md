# CONTRAconnect – Commodity & Cost Analytics Platform

Web-based multi-tenant application for managing contraceptive commodity supply from a central Admin system to service providers (kiosks, PHCs, other outlets), capturing client encounters, and computing **true operational costs per client / method / kiosk**.

## Key Capabilities

- **Admin**: Facility registry & activation, product catalogue (with admin-defined unit costs), stock dispatch, replenishment request approval, digital accounting ledgers, cost-allocation engine, method-uptake & cost analytics dashboards with charts.
- **Service Provider**: Registration → activation, personal dashboard (stock received / issued / balance / open requests), stock request form, client encounter form (method, demographics, coded refusal/discontinuation reasons, observation checklist), automatic stock deduction on dispensation.
- **Cost Engine** (answers the learning question):
  - Unit cost per client served = Total operational cost ÷ Total clients served
  - Operating cost per kiosk / facility
  - Platform build costs tracked **separately** and excluded from unit-cost calculations
  - Method-level commodity cost from inventory issues
  - Graphical dashboards (Chart.js)

## Quick Start

```bash
cd contraconnect
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000

### Demo Accounts

| Role     | Email                          | Password    |
|----------|--------------------------------|-------------|
| Admin    | admin@contraconnect.local      | admin123    |
| Kiosk    | kiosk1@contraconnect.local     | provider123 |
| PHC      | phc1@contraconnect.local       | provider123 |

Database is auto-created and seeded on first run (SQLite in `instance/contraconnect.db`).

## Project Structure

```
contraconnect/
├── app.py                 # Full application (models, routes, cost engine)
├── requirements.txt
├── README.md
├── instance/              # SQLite DB (auto-created)
└── templates/             # Jinja2 + Bootstrap 5 templates
    ├── base.html
    ├── login.html
    ├── register.html
    ├── admin_*.html
    └── provider_*.html
```

## Cost Allocation Logic (summary)

1. Operational expenditures = all ledger entries where `is_platform_cost = False`.
2. Platform build costs are summed separately and **not** included in unit-cost denominator.
3. Clients served = encounters with outcome `accepted` or `discontinued` in the period.
4. Per-facility operating cost = direct facility expenditures + equal share of unallocated (central) operational costs.
5. Unit cost per facility = facility operating cost ÷ facility clients.
6. Commodity cost by method derived from stock `issue` transactions × unit cost.

## Extending

- Split `app.py` into blueprints (`auth`, `admin`, `provider`, `api`).
- Replace SQLite with PostgreSQL for production.
- Add offline-capable PWA for kiosk devices.
- Volume-weighted allocation of central costs instead of equal share.
- Export to Excel / national HMIS.

## Licence

Provided as a reference implementation for programme design and pilot use.
