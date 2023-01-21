from dufflepud.db.db import execute, transaction


with transaction():
    execute("""
    CREATE TABLE IF NOT EXISTS usage (
        name text NOT NULL,
        session text NOT NULL,
        created_at timestamp NOT NULL
    )
    """)
