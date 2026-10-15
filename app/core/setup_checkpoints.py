"""Creates or updates LangGraph's checkpoint tables: `python -m app.core.setup_checkpoints`.

They live in their own schema and aren't Alembic's (`alembic/env.py` leaves the schema out), so the deploy runs
this next to `alembic upgrade head`. Safe to run again.
"""

import asyncio

from app.core.client import close_connection, setup_checkpointer
from app.core.config import DatabaseSettings


async def main() -> None:
    try:
        await setup_checkpointer(DatabaseSettings())
    finally:
        await close_connection()


if __name__ == "__main__":
    asyncio.run(main())
