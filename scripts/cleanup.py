"""Delete at most 200 expired demo session rows using the configured authenticated store."""

import asyncio

from gpt_live_python.config import settings
from gpt_live_python.remote import Remote


async def main():
    result = await Remote(settings()).cleanup()
    print(f"Deleted {result['deleted']} expired demo session rows")


if __name__ == "__main__":
    asyncio.run(main())
