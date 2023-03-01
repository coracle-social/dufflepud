from dufflepud.db.db import execute, transaction


with transaction():
    execute("""
    CREATE TABLE IF NOT EXISTS usage (
        name text NOT NULL,
        session text NOT NULL,
        created_at timestamp NOT NULL
    )
    """)


    execute("""
    CREATE TABLE IF NOT EXISTS quote (
        id uuid PRIMARY KEY,
        invoice text
    )
    """)

    execute("""
    CREATE TABLE IF NOT EXISTS upload (
        id uuid PRIMARY KEY,
        quote uuid NOT NULL,
        size int NOT NULL
    )
    """)
