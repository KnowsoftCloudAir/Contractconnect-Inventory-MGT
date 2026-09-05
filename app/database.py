from sqlmodel import SQLModel, create_engine, Session
from dotenv import load_dotenv
import os

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:////tmp/churchgate.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, echo=False, connect_args=connect_args, pool_pre_ping=True)

def create_db_and_tables():
    SQLModel.metadata.create_all(engine)
    # Best-effort columns for existing DBs (SQLite + Postgres)
    try:
        from sqlalchemy import text
        cols = [
            ("logo_url", "VARCHAR"),
            ("latitude", "FLOAT"),
            ("longitude", "FLOAT"),
            ("request_home_display", "BOOLEAN DEFAULT 0"),
            ("home_display_hours", "FLOAT"),
            ("home_display_starts_at", "TIMESTAMP"),
            ("home_display_ends_at", "TIMESTAMP"),
        ]
        with engine.begin() as conn:
            for col, typ in [
                ("logo_url", "VARCHAR"),
                ("latitude", "FLOAT"),
                ("longitude", "FLOAT"),
            ]:
                try:
                    conn.execute(text(f"ALTER TABLE churchunit ADD COLUMN {col} {typ}"))
                except Exception:
                    pass
            for col, typ in cols:
                try:
                    conn.execute(text(f"ALTER TABLE specialprogram ADD COLUMN {col} {typ}"))
                except Exception:
                    pass
    except Exception as e:
        print(f"migrate note: {e}")

def get_session():
    with Session(engine) as session:
        yield session
