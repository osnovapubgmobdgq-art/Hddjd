"""Общая база SQLite: её используют и бот, и сайт админки."""
import sqlite3
import time
import secrets

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    joined_at INTEGER,
    blocked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    sort INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    price REAL NOT NULL,
    image TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS banners (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    image TEXT NOT NULL,
    caption TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS promocodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE COLLATE NOCASE,
    kind TEXT NOT NULL,              -- 'percent' или 'fixed'
    value REAL NOT NULL,
    max_uses INTEGER NOT NULL DEFAULT 0,   -- 0 = без лимита
    used INTEGER NOT NULL DEFAULT 0,
    expires_at INTEGER,              -- unix-время, NULL = бессрочно
    active INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    username TEXT,
    product_id INTEGER,
    product_name TEXT,
    price REAL,
    promo_code TEXT,
    discount REAL NOT NULL DEFAULT 0,
    total REAL,
    status TEXT NOT NULL DEFAULT 'new',
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS broadcasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL DEFAULT '',
    image TEXT,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending / sending / done / interrupted
    total INTEGER NOT NULL DEFAULT 0,
    sent INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS login_tokens (
    token TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL,
    used INTEGER NOT NULL DEFAULT 0
);
"""

LOGIN_TOKEN_TTL = 600  # секунд


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        # Рассылки, прерванные перезапуском, повторно не отправляем
        conn.execute("UPDATE broadcasts SET status='interrupted' WHERE status='sending'")
        conn.execute("DELETE FROM login_tokens WHERE created_at < ?", (int(time.time()) - 86400,))
        conn.commit()
    finally:
        conn.close()


def q(sql: str, params=(), one: bool = False):
    conn = get_conn()
    try:
        cur = conn.execute(sql, params)
        return cur.fetchone() if one else cur.fetchall()
    finally:
        conn.close()


def scalar(sql: str, params=()):
    row = q(sql, params, one=True)
    return row[0] if row else None


def run(sql: str, params=()) -> int:
    """Выполняет запрос и возвращает id новой строки."""
    conn = get_conn()
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def changed(sql: str, params=()) -> int:
    """Выполняет запрос и возвращает число изменённых строк."""
    conn = get_conn()
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def money(value) -> str:
    value = float(value or 0)
    text = f"{value:,.2f}".replace(",", "\u00a0")
    if text.endswith(".00"):
        text = text[:-3]
    return f"{text}\u00a0{config.CURRENCY}"


# ---------- пользователи ----------

def register_user(user) -> None:
    run(
        "INSERT INTO users(id, username, first_name, joined_at) VALUES(?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET username=excluded.username, "
        "first_name=excluded.first_name, blocked=0",
        (user.id, user.username, user.first_name, int(time.time())),
    )


# ---------- вход в админку ----------

def create_login_token() -> str:
    token = secrets.token_urlsafe(32)
    run("INSERT INTO login_tokens(token, created_at) VALUES(?,?)", (token, int(time.time())))
    return token


def consume_login_token(token: str) -> bool:
    if not token:
        return False
    return changed(
        "UPDATE login_tokens SET used=1 WHERE token=? AND used=0 AND created_at>?",
        (token, int(time.time()) - LOGIN_TOKEN_TTL),
    ) == 1


# ---------- промокоды и заказы ----------

def _evaluate_promo(promo, price: float):
    """Возвращает (промокод, скидка, ошибка)."""
    if promo is None or not promo["active"]:
        return None, 0.0, "Такой промокод не найден."
    if promo["expires_at"] and promo["expires_at"] <= time.time():
        return None, 0.0, "Срок действия промокода истёк."
    if promo["max_uses"] and promo["used"] >= promo["max_uses"]:
        return None, 0.0, "Промокод уже использован максимальное число раз."
    if promo["kind"] == "percent":
        discount = price * promo["value"] / 100
    else:
        discount = promo["value"]
    discount = round(min(discount, price), 2)
    return promo, discount, None


def check_promo(code: str, price: float):
    promo = q("SELECT * FROM promocodes WHERE code = ?", ((code or "").strip(),), one=True)
    return _evaluate_promo(promo, price)


def create_order(user, product_id: int, promo_code: str | None = None):
    """Создаёт заказ по текущей цене товара. Возвращает (заказ, ошибка)."""
    conn = get_conn()
    try:
        product = conn.execute(
            "SELECT * FROM products WHERE id=? AND active=1", (product_id,)
        ).fetchone()
        if not product:
            return None, "Этот товар сейчас недоступен."
        discount, code = 0.0, None
        if promo_code:
            row = conn.execute(
                "SELECT * FROM promocodes WHERE code = ?", (promo_code.strip(),)
            ).fetchone()
            promo, discount, error = _evaluate_promo(row, product["price"])
            if error:
                return None, error
            cur = conn.execute(
                "UPDATE promocodes SET used = used + 1 "
                "WHERE id=? AND (max_uses=0 OR used < max_uses)",
                (promo["id"],),
            )
            if cur.rowcount != 1:
                return None, "Промокод уже использован максимальное число раз."
            code = promo["code"]
        total = round(product["price"] - discount, 2)
        cur = conn.execute(
            "INSERT INTO orders(user_id, username, product_id, product_name, price, "
            "promo_code, discount, total, status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (user.id, user.username, product["id"], product["name"], product["price"],
             code, discount, total, "new", int(time.time())),
        )
        conn.commit()
        order = conn.execute("SELECT * FROM orders WHERE id=?", (cur.lastrowid,)).fetchone()
        return order, None
    finally:
        conn.close()
