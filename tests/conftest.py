import subprocess

import pytest
import psycopg

from app.config import DATABASE_ADMIN_URL


@pytest.fixture(scope="session", autouse=True)
def seeded_database():
    subprocess.run(["python", "-m", "scripts.seed"], check=True)
    # Tests own the ephemeral Compose database and start from repeatable runtime state.
    with psycopg.connect(DATABASE_ADMIN_URL) as conn:
        conn.execute("TRUNCATE attempts, outbox, processed_jobs CASCADE")
