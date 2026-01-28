"""
Database Migration Runner

Simple migration runner for RuGPT database.
"""
import asyncio
import asyncpg
import os
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from src.engine.config import Config


async def run_migrations():
    """Run all SQL migrations in order"""
    migrations_dir = Path(__file__).parent
    dsn = Config.get_postgres_dsn()

    print(f"Connecting to database...")
    print(f"DSN: {dsn}")

    try:
        conn = await asyncpg.connect(dsn)
        print("Connected successfully!")

        # Get all SQL files sorted by name
        sql_files = sorted(migrations_dir.glob("*.sql"))

        for sql_file in sql_files:
            print(f"\nRunning migration: {sql_file.name}")

            with open(sql_file, "r") as f:
                sql = f.read()

            try:
                await conn.execute(sql)
                print(f"  ✓ {sql_file.name} completed")
            except asyncpg.PostgresError as e:
                print(f"  ✗ Error in {sql_file.name}: {e}")
                # Continue with other migrations

        await conn.close()
        print("\nMigrations complete!")

    except Exception as e:
        print(f"Connection failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(run_migrations())
