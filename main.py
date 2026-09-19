import asyncio
import logging
import threading

import bot as bot_module
import config
import db
from web import create_app


def run_web():
    from waitress import serve

    serve(create_app(), host="0.0.0.0", port=config.PORT, threads=4)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not config.BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN (см. .env.example)")
    if not config.ADMIN_IDS:
        logging.warning("ADMIN_IDS пуст: команда /admin никому не будет доступна")

    db.init_db()
    threading.Thread(target=run_web, daemon=True, name="web").start()
    logging.info("Админ-панель запущена на порту %s", config.PORT)

    bot, dp = bot_module.build()
    worker = asyncio.create_task(bot_module.broadcast_worker(bot))
    try:
        await dp.start_polling(bot)
    finally:
        worker.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
