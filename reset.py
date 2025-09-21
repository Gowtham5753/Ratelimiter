import asyncio
from database import db_manager

async def reset():
    print("Dropping old tables...")
    await db_manager.drop_tables()
    print("Creating new tables...")
    await db_manager.create_tables()
    print("✅ Database reset complete")

if __name__ == "__main__":
    asyncio.run(reset())
