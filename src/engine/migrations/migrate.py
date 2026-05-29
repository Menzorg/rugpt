"""
Database Migration Runner

Tracks applied migrations in schema_migrations table.
Only runs new migrations, skips already applied ones.
"""
import asyncio
import asyncpg
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from src.engine.config import Config


async def run_migrations():
    """Run only new SQL migrations"""
    migrations_dir = Path(__file__).parent
    dsn = Config.get_postgres_dsn()

    print(f"Connecting to database...")
    print(f"DSN: {dsn}")

    try:
        conn = await asyncpg.connect(dsn)
        print("Connected successfully!")

        # Create tracking table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename VARCHAR(255) PRIMARY KEY,
                applied_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
            )
        """)

        # Get already applied
        rows = await conn.fetch("SELECT filename FROM schema_migrations")
        applied = {row["filename"] for row in rows}

        # Get all SQL files sorted by name
        sql_files = sorted(migrations_dir.glob("*.sql"))

        new_count = 0
        for sql_file in sql_files:
            if sql_file.name in applied:
                print(f"  - {sql_file.name} (skip)")
                continue

            print(f"\nRunning migration: {sql_file.name}")

            with open(sql_file, "r") as f:
                sql = f.read()

            try:
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (filename) VALUES ($1)",
                        sql_file.name,
                    )
                print(f"  ✓ {sql_file.name} completed")
                new_count += 1
            except asyncpg.PostgresError as e:
                print(f"  ✗ Error in {sql_file.name}: {e}")

        await conn.close()

        if new_count == 0:
            print("\nAll migrations already applied.")
        else:
            print(f"\n{new_count} new migration(s) applied.")

    except Exception as e:
        print(f"Connection failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(run_migrations())
