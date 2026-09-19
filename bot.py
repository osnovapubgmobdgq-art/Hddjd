import asyncio
import logging
import random

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)

import config
import db

log = logging.getLogger("bot")
router = Router()


class Buy(StatesGroup):
    promo = State()


def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def image_path(name):
    if not name:
        return None
    path = config.UPLOAD_DIR / name
    return path if path.exists() else None


# ---------- каталог ----------

async def send_home(target: Message):
    cats = db.q("SELECT id, name FROM categories ORDER BY sort, id")
    markup = kb([[btn(c["name"], f"cat:{c['id']}")] for c in cats]) if cats else None
    text = "Выберите категорию:" if cats else "Каталог пока пуст. Загляните позже."

    banners = db.q("SELECT * FROM banners WHERE active = 1")
    random.shuffle(banners)
    for banner in banners:
        path = image_path(banner["image"])
        if path:
            caption = f"{banner['caption']}\n\n{text}" if banner["caption"] else text
            await target.answer_photo(FSInputFile(path), caption=caption[:1024], reply_markup=markup)
            return
    await target.answer(text, reply_markup=markup)


async def show(cb: CallbackQuery, text: str, markup, image: str | None = None):
    """Заменяет текущее сообщение новым (так работает и с фото, и с текстом)."""
    msg = cb.message
    try:
        await msg.delete()
    except TelegramBadRequest:
        pass
    path = image_path(image)
    if path:
        await msg.answer_photo(FSInputFile(path), caption=text[:1024], reply_markup=markup)
    else:
        await msg.answer(text, reply_markup=markup)
    await cb.answer()


@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    db.register_user(m.from_user)
    await send_home(m)


@router.message(Command("admin"))
async def cmd_admin(m: Message, state: FSMContext):
    await state.clear()
    if m.from_user.id not in config.ADMIN_IDS:
        return  # обычным пользователям бот не отвечает
    if not config.WEB_URL:
        await m.answer("В настройках не указан WEB_URL — не могу собрать ссылку на админ-панель.")
        return
    token = db.create_login_token()
    await m.answer(
        f"Админ-панель:\n{config.WEB_URL}/login?token={token}\n\n"
        "Ссылка одноразовая и действует 10 минут. Никому её не пересылайте.",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


@router.callback_query(F.data == "home")
async def cb_home(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await cb.message.delete()
    except TelegramBadRequest:
        pass
    await send_home(cb.message)
    await cb.answer()


@router.callback_query(F.data.startswith("cat:"))
async def cb_category(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    cat = db.q("SELECT * FROM categories WHERE id=?", (int(cb.data[4:]),), one=True)
    if not cat:
        await cb.answer("Категория не найдена", show_alert=True)
        return
    products = db.q(
        "SELECT id, name, price FROM products WHERE category_id=? AND active=1 ORDER BY id",
        (cat["id"],),
    )
    rows = [[btn(f"{p['name']} — {db.money(p['price'])}", f"prod:{p['id']}")] for p in products]
    rows.append([btn("‹ Назад", "home")])
    text = cat["name"] if products else f"{cat['name']}\n\nВ этой категории пока нет товаров."
    await show(cb, text, kb(rows))


@router.callback_query(F.data.startswith("prod:"))
async def cb_product(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    p = db.q("SELECT * FROM products WHERE id=? AND active=1", (int(cb.data[5:]),), one=True)
    if not p:
        await cb.answer("Товар недоступен", show_alert=True)
        return
    parts = [p["name"]]
    if p["description"]:
        parts.append(p["description"])
    parts.append(f"Цена: {db.money(p['price'])}")
    markup = kb([
        [btn("Купить", f"buy:{p['id']}")],
        [btn("‹ Назад", f"cat:{p['category_id']}")],
    ])
    await show(cb, "\n\n".join(parts), markup, p["image"])


# ---------- покупка ----------

@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(cb: CallbackQuery, state: FSMContext):
    pid = int(cb.data[4:])
    if not db.q("SELECT 1 FROM products WHERE id=? AND active=1", (pid,), one=True):
        await cb.answer("Товар недоступен", show_alert=True)
        return
    await state.set_state(Buy.promo)
    await state.update_data(product_id=pid)
    await cb.message.answer(
        "Есть промокод? Отправьте его сообщением или нажмите «Без промокода».",
        reply_markup=kb([[btn("Без промокода", "nopromo")], [btn("Отмена", f"prod:{pid}")]]),
    )
    await cb.answer()


@router.callback_query(Buy.promo, F.data == "nopromo")
async def cb_nopromo(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    await finish_order(cb.message, cb.from_user, data["product_id"], None)
    await cb.answer()


@router.message(Buy.promo, F.text)
async def msg_promo(m: Message, state: FSMContext):
    data = await state.get_data()
    product = db.q("SELECT * FROM products WHERE id=? AND active=1", (data["product_id"],), one=True)
    if not product:
        await state.clear()
        await m.answer("Этот товар сейчас недоступен.", reply_markup=kb([[btn("В каталог", "home")]]))
        return
    _, _, error = db.check_promo(m.text, product["price"])
    if error:
        await m.answer(
            f"{error} Отправьте другой код или нажмите «Без промокода».",
            reply_markup=kb([[btn("Без промокода", "nopromo")], [btn("Отмена", f"prod:{product['id']}")]]),
        )
        return
    await state.clear()
    await finish_order(m, m.from_user, product["id"], m.text)


async def finish_order(msg: Message, user, product_id: int, promo_code):
    db.register_user(user)
    order, error = db.create_order(user, product_id, promo_code)
    home = kb([[btn("В каталог", "home")]])
    if error:
        await msg.answer(error, reply_markup=home)
        return

    lines = [f"Заказ №{order['id']} оформлен.", f"Товар: {order['product_name']}",
             f"Цена: {db.money(order['price'])}"]
    if order["discount"]:
        lines.append(f"Скидка по промокоду {order['promo_code']}: −{db.money(order['discount'])}")
    lines.append(f"Итого: {db.money(order['total'])}")
    lines.append("Мы свяжемся с вами в ближайшее время.")
    await msg.answer("\n".join(lines), reply_markup=home)

    who = f"@{user.username}" if user.username else user.full_name
    note = (f"Новый заказ №{order['id']}\n{order['product_name']}\n"
            f"Итого: {db.money(order['total'])}"
            + (f" (промокод {order['promo_code']})" if order["promo_code"] else "")
            + f"\nПокупатель: {who}, id {user.id}")
    for admin_id in config.ADMIN_IDS:
        try:
            await msg.bot.send_message(admin_id, note)
        except Exception:
            log.warning("Не удалось уведомить админа %s", admin_id)


# ---------- рассылки из админ-панели ----------

async def _send_text(bot: Bot, uid: int, text: str):
    try:
        await bot.send_message(uid, text, parse_mode="HTML")
    except TelegramBadRequest as e:
        if "parse" in str(e).lower():
            await bot.send_message(uid, text, parse_mode=None)
        else:
            raise


async def send_one(bot: Bot, uid: int, text: str, path):
    if not path:
        await _send_text(bot, uid, text)
        return
    caption = text if text and len(text) <= 1024 else None
    try:
        await bot.send_photo(uid, FSInputFile(path), caption=caption, parse_mode="HTML")
    except TelegramBadRequest as e:
        if caption and "parse" in str(e).lower():
            await bot.send_photo(uid, FSInputFile(path), caption=caption, parse_mode=None)
        else:
            raise
    if text and not caption:
        await _send_text(bot, uid, text)


async def run_broadcast(bot: Bot, job):
    db.run("UPDATE broadcasts SET status='sending' WHERE id=?", (job["id"],))
    users = [r["id"] for r in db.q("SELECT id FROM users WHERE blocked=0")]
    db.run("UPDATE broadcasts SET total=? WHERE id=?", (len(users), job["id"]))
    path = image_path(job["image"])
    sent = failed = 0
    for i, uid in enumerate(users, 1):
        for _ in range(2):
            try:
                await send_one(bot, uid, job["text"] or "", path)
                sent += 1
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramForbiddenError:
                db.run("UPDATE users SET blocked=1 WHERE id=?", (uid,))
                failed += 1
                break
            except Exception as e:
                log.warning("Рассылка: не отправлено %s: %s", uid, e)
                failed += 1
                break
        else:
            failed += 1
        if i % 20 == 0:
            db.run("UPDATE broadcasts SET sent=?, failed=? WHERE id=?", (sent, failed, job["id"]))
        await asyncio.sleep(0.05)  # ~20 сообщений в секунду, в пределах лимитов Telegram
    db.run("UPDATE broadcasts SET sent=?, failed=?, status='done' WHERE id=?", (sent, failed, job["id"]))
    log.info("Рассылка #%s завершена: %s доставлено, %s ошибок", job["id"], sent, failed)


async def broadcast_worker(bot: Bot):
    while True:
        try:
            job = db.q("SELECT * FROM broadcasts WHERE status='pending' ORDER BY id LIMIT 1", one=True)
            if job:
                await run_broadcast(bot, job)
            else:
                await asyncio.sleep(3)
        except Exception:
            log.exception("Ошибка в обработчике рассылок")
            await asyncio.sleep(5)


def build():
    bot = Bot(config.BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    return bot, dp
