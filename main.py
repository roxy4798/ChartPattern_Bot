from __future__ import annotations
import asyncio
import logging
from bot.main import run

if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logging.getLogger("main").info("Terminated by KeyboardInterrupt.")


