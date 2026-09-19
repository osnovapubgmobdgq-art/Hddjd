import hmac
import re
import secrets
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import (Flask, abort, flash, redirect, render_template, request,
                   send_from_directory, session, url_for)
from markupsafe import Markup

import config
import db

STATUSES = {"new": "Новый", "paid": "Оплачен", "done": "Выполнен", "cancelled": "Отменён"}
BROADCAST_STATUSES = {"pending": "В очереди", "sending": "Отправляется",
                      "done": "Готово", "interrupted": "Прервана"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}

_login_fails: dict[str, list[float]] = {}


# ---------- вспомогательное ----------

def _secret_key() -> str:
    if config.SECRET_KEY:
        return config.SECRET_KEY
    path = config.DATA_DIR / "secret.key"
    if path.exists():
        return path.read_text().strip()
    key = secrets.token_hex(32)
    path.write_text(key)
    return key


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value):
    try:
        x = float(str(value).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def _price(value):
    x = _float(value)
    if x is None:
        return None
    x = round(x, 2)
    return x if 0 <= x <= 10_000_000 else None


def _looks_like_image(head: bytes) -> bool:
    return (head.startswith(b"\xff\xd8\xff") or head.startswith(b"\x89PNG\r\n\x1a\n")
            or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"))


def save_image(storage):
    """Сохраняет загруженную картинку, возвращает имя файла или None."""
    ext = Path(storage.filename or "").suffix.lower()
    if ext not in IMAGE_EXT:
        return None
    head = storage.stream.read(12)
    storage.stream.seek(0)
    if not _looks_like_image(head):
        return None
    name = uuid.uuid4().hex + ext
    storage.save(config.UPLOAD_DIR / name)
    return name


def delete_image(name):
    if name:
        try:
            (config.UPLOAD_DIR / Path(name).name).unlink(missing_ok=True)
        except OSError:
            pass


def _has_file(field: str) -> bool:
    f = request.files.get(field)
    return bool(f and f.filename)


def _too_many_fails(ip: str) -> bool:
    fresh = [t for t in _login_fails.get(ip, []) if t > time.time() - 300]
    _login_fails[ip] = fresh
    return len(fresh) >= 5


# ---------- приложение ----------

def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = _secret_key()
    app.config.update(
        MAX_CONTENT_LENGTH=12 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=config.WEB_URL.startswith("https://"),
        PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    )
    tz = timezone(timedelta(hours=config.TZ_OFFSET))

    @app.template_filter("money")
    def money_filter(v):
        return db.money(v)

    @app.template_filter("num")
    def num_filter(v):
        return f"{float(v or 0):.2f}".rstrip("0").rstrip(".")

    @app.template_filter("dt")
    def dt_filter(ts):
        return datetime.fromtimestamp(ts, tz).strftime("%d.%m.%Y %H:%M") if ts else "—"

    @app.context_processor
    def inject():
        def csrf_input():
            return Markup('<input type="hidden" name="csrf" value="%s">' % session.get("csrf", ""))
        return dict(csrf_input=csrf_input, STATUSES=STATUSES,
                    BROADCAST_STATUSES=BROADCAST_STATUSES)

    @app.before_request
    def guard():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        if request.endpoint in (None, "static", "login"):
            return None
        if not session.get("admin"):
            return redirect(url_for("login"))
        if request.method == "POST":
            if not hmac.compare_digest(request.form.get("csrf", ""), session.get("csrf", "")):
                abort(400, "Сессия устарела. Обновите страницу и повторите.")
        return None

    @app.after_request
    def headers(resp):
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.errorhandler(413)
    def too_big(_):
        return "Файл слишком большой: максимум 10 МБ.", 413

    # ----- вход -----

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if session.get("admin"):
            return redirect(url_for("dashboard"))
        password_enabled = bool(config.ADMIN_PASSWORD)
        ip = request.remote_addr or "?"
        if request.method == "POST":
            if _too_many_fails(ip):
                flash("Слишком много попыток. Подождите 5 минут.", "error")
                return render_template("login.html", token="", password_enabled=password_enabled), 429
            token = request.form.get("token", "")
            password = request.form.get("password", "")
            ok = False
            if token:
                ok = db.consume_login_token(token)
            elif password and password_enabled:
                ok = hmac.compare_digest(password.encode(), config.ADMIN_PASSWORD.encode())
            if ok:
                session.clear()
                session.permanent = True
                session["admin"] = True
                session["csrf"] = secrets.token_hex(16)
                return redirect(url_for("dashboard"))
            _login_fails.setdefault(ip, []).append(time.time())
            flash("Не удалось войти: ссылка устарела или пароль неверный. "
                  "Отправьте боту /admin и откройте новую ссылку.", "error")
            return render_template("login.html", token="", password_enabled=password_enabled)
        return render_template("login.html", token=request.args.get("token", ""),
                               password_enabled=password_enabled)

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # ----- сводка -----

    @app.route("/")
    def dashboard():
        stats = {
            "users": db.scalar("SELECT COUNT(*) FROM users"),
            "blocked": db.scalar("SELECT COUNT(*) FROM users WHERE blocked=1"),
            "products": db.scalar("SELECT COUNT(*) FROM products WHERE active=1"),
            "new_orders": db.scalar("SELECT COUNT(*) FROM orders WHERE status='new'"),
            "revenue": db.scalar("SELECT COALESCE(SUM(total),0) FROM orders WHERE status IN ('paid','done')"),
        }
        orders = db.q("SELECT * FROM orders ORDER BY id DESC LIMIT 5")
        return render_template("dashboard.html", s=stats, orders=orders)

    # ----- рассылка -----

    @app.route("/broadcast", methods=["GET", "POST"])
    def broadcast():
        active = db.scalar("SELECT COUNT(*) FROM broadcasts WHERE status IN ('pending','sending')")
        if request.method == "POST":
            text = request.form.get("text", "").strip()[:4000]
            if not text and not _has_file("image"):
                flash("Введите текст или добавьте картинку.", "error")
                return redirect(url_for("broadcast"))
            if active:
                flash("Дождитесь окончания предыдущей рассылки.", "error")
                return redirect(url_for("broadcast"))
            image = None
            if _has_file("image"):
                image = save_image(request.files["image"])
                if not image:
                    flash("Картинка должна быть в формате JPG, PNG или WEBP.", "error")
                    return redirect(url_for("broadcast"))
            db.run("INSERT INTO broadcasts(text, image, status, created_at) VALUES(?,?,?,?)",
                   (text, image, "pending", int(time.time())))
            flash("Рассылка поставлена в очередь и скоро начнётся.", "ok")
            return redirect(url_for("broadcast"))
        users = db.scalar("SELECT COUNT(*) FROM users WHERE blocked=0")
        history = db.q("SELECT * FROM broadcasts ORDER BY id DESC LIMIT 20")
        return render_template("broadcast.html", users=users, history=history, active=active)

    # ----- баннеры -----

    @app.route("/banners")
    def banners():
        return render_template("banners.html", rows=db.q("SELECT * FROM banners ORDER BY id DESC"))

    @app.post("/banners/add")
    def banner_add():
        if not _has_file("image"):
            flash("Выберите картинку для баннера.", "error")
        else:
            image = save_image(request.files["image"])
            if not image:
                flash("Картинка должна быть в формате JPG, PNG или WEBP.", "error")
            else:
                db.run("INSERT INTO banners(image, caption, active, created_at) VALUES(?,?,1,?)",
                       (image, request.form.get("caption", "").strip()[:900], int(time.time())))
                flash("Баннер добавлен.", "ok")
        return redirect(url_for("banners"))

    @app.post("/banners/<int:bid>/toggle")
    def banner_toggle(bid):
        db.run("UPDATE banners SET active = 1 - active WHERE id=?", (bid,))
        return redirect(url_for("banners"))

    @app.post("/banners/<int:bid>/delete")
    def banner_delete(bid):
        row = db.q("SELECT image FROM banners WHERE id=?", (bid,), one=True)
        if row:
            db.run("DELETE FROM banners WHERE id=?", (bid,))
            delete_image(row["image"])
            flash("Баннер удалён.", "ok")
        return redirect(url_for("banners"))

    # ----- категории -----

    @app.route("/categories")
    def categories():
        rows = db.q("SELECT c.*, (SELECT COUNT(*) FROM products p WHERE p.category_id=c.id) AS n "
                    "FROM categories c ORDER BY sort, id")
        return render_template("categories.html", rows=rows)

    @app.post("/categories/add")
    def category_add():
        name = request.form.get("name", "").strip()[:80]
        if not name:
            flash("Введите название категории.", "error")
        else:
            sort = _int(request.form.get("sort"))
            if sort is None:
                sort = (db.scalar("SELECT COALESCE(MAX(sort),0) FROM categories") or 0) + 1
            db.run("INSERT INTO categories(name, sort) VALUES(?,?)", (name, sort))
            flash("Категория добавлена.", "ok")
        return redirect(url_for("categories"))

    @app.post("/categories/<int:cid>/save")
    def category_save(cid):
        name = request.form.get("name", "").strip()[:80]
        if not name:
            flash("Название не может быть пустым.", "error")
        else:
            db.run("UPDATE categories SET name=?, sort=? WHERE id=?",
                   (name, _int(request.form.get("sort"), 0), cid))
            flash("Категория сохранена.", "ok")
        return redirect(url_for("categories"))

    @app.post("/categories/<int:cid>/delete")
    def category_delete(cid):
        if db.scalar("SELECT COUNT(*) FROM products WHERE category_id=?", (cid,)):
            flash("В категории есть товары. Сначала перенесите или удалите их.", "error")
        else:
            db.run("DELETE FROM categories WHERE id=?", (cid,))
            flash("Категория удалена.", "ok")
        return redirect(url_for("categories"))

    # ----- товары -----

    @app.route("/products")
    def products():
        cat = _int(request.args.get("cat"))
        sql = ("SELECT p.*, c.name AS cat_name FROM products p "
               "JOIN categories c ON c.id = p.category_id ")
        rows = (db.q(sql + "WHERE p.category_id=? ORDER BY p.id DESC", (cat,)) if cat
                else db.q(sql + "ORDER BY p.id DESC"))
        cats = db.q("SELECT * FROM categories ORDER BY sort, id")
        return render_template("products.html", rows=rows, cats=cats, cat=cat)

    @app.post("/products/add")
    def product_add():
        f = request.form
        name = f.get("name", "").strip()[:120]
        price = _price(f.get("price"))
        cid = _int(f.get("category_id"))
        if not name or price is None or not db.q("SELECT 1 FROM categories WHERE id=?", (cid,), one=True):
            flash("Укажите название, цену и категорию. Если категорий нет, сначала создайте её.", "error")
            return redirect(url_for("products"))
        image = None
        if _has_file("image"):
            image = save_image(request.files["image"])
            if not image:
                flash("Картинка должна быть в формате JPG, PNG или WEBP.", "error")
                return redirect(url_for("products"))
        db.run("INSERT INTO products(category_id, name, description, price, image, active, created_at) "
               "VALUES(?,?,?,?,?,?,?)",
               (cid, name, f.get("description", "").strip()[:900], price, image,
                1 if f.get("active") else 0, int(time.time())))
        flash("Товар добавлен.", "ok")
        return redirect(url_for("products", cat=cid))

    @app.post("/products/<int:pid>/price")
    def product_price(pid):
        price = _price(request.form.get("price"))
        if price is None:
            flash("Введите цену числом, например 1499 или 99.90.", "error")
        else:
            db.run("UPDATE products SET price=? WHERE id=?", (price, pid))
            flash("Цена обновлена.", "ok")
        return redirect(request.referrer or url_for("products"))

    @app.post("/products/bulk-price")
    def product_bulk_price():
        percent = _float(request.form.get("percent"))
        if percent is None or not -90 <= percent <= 500 or percent == 0:
            flash("Укажите процент от −90 до 500, например 10 или −5.", "error")
            return redirect(url_for("products"))
        factor = 1 + percent / 100
        expr = "ROUND(price * ?)" if request.form.get("round") else "ROUND(price * ?, 2)"
        cid = _int(request.form.get("category_id"))
        if cid:
            n = db.changed(f"UPDATE products SET price = {expr} WHERE category_id=?", (factor, cid))
        else:
            n = db.changed(f"UPDATE products SET price = {expr}", (factor,))
        flash(f"Цены изменены на {percent:g}% у товаров: {n}.", "ok")
        return redirect(url_for("products", cat=cid) if cid else url_for("products"))

    @app.post("/products/<int:pid>/toggle")
    def product_toggle(pid):
        db.run("UPDATE products SET active = 1 - active WHERE id=?", (pid,))
        return redirect(request.referrer or url_for("products"))

    @app.post("/products/<int:pid>/delete")
    def product_delete(pid):
        row = db.q("SELECT image FROM products WHERE id=?", (pid,), one=True)
        if row:
            db.run("DELETE FROM products WHERE id=?", (pid,))
            delete_image(row["image"])
            flash("Товар удалён.", "ok")
        return redirect(url_for("products"))

    @app.route("/products/<int:pid>/edit", methods=["GET", "POST"])
    def product_edit(pid):
        p = db.q("SELECT * FROM products WHERE id=?", (pid,), one=True)
        if not p:
            abort(404)
        if request.method == "POST":
            f = request.form
            name = f.get("name", "").strip()[:120]
            price = _price(f.get("price"))
            cid = _int(f.get("category_id"))
            if not name or price is None or not db.q("SELECT 1 FROM categories WHERE id=?", (cid,), one=True):
                flash("Укажите название, цену и категорию.", "error")
                return redirect(url_for("product_edit", pid=pid))
            image = p["image"]
            if _has_file("image"):
                new = save_image(request.files["image"])
                if not new:
                    flash("Картинка должна быть в формате JPG, PNG или WEBP.", "error")
                    return redirect(url_for("product_edit", pid=pid))
                delete_image(image)
                image = new
            elif f.get("remove_image"):
                delete_image(image)
                image = None
            db.run("UPDATE products SET category_id=?, name=?, description=?, price=?, image=?, active=? "
                   "WHERE id=?",
                   (cid, name, f.get("description", "").strip()[:900], price, image,
                    1 if f.get("active") else 0, pid))
            flash("Товар сохранён.", "ok")
            return redirect(url_for("products", cat=cid))
        cats = db.q("SELECT * FROM categories ORDER BY sort, id")
        return render_template("product_edit.html", p=p, cats=cats)

    # ----- промокоды -----

    @app.route("/promos")
    def promos():
        return render_template("promos.html", rows=db.q("SELECT * FROM promocodes ORDER BY id DESC"),
                               now=time.time())

    @app.post("/promos/add")
    def promo_add():
        f = request.form
        code = re.sub(r"[^A-Za-z0-9_-]", "", f.get("code", "")).upper()[:32]
        kind = f.get("kind")
        value = _float(f.get("value"))
        max_uses = max(_int(f.get("max_uses"), 0), 0)
        if (not code or kind not in ("percent", "fixed") or value is None or value <= 0
                or (kind == "percent" and value > 100)):
            flash("Проверьте промокод: код (латиница и цифры), тип скидки и размер "
                  "(для процентов — от 1 до 100).", "error")
            return redirect(url_for("promos"))
        expires_at = None
        if f.get("expires"):
            try:
                d = datetime.strptime(f["expires"], "%Y-%m-%d")
            except ValueError:
                flash("Дата окончания указана неверно.", "error")
                return redirect(url_for("promos"))
            expires_at = int((datetime(d.year, d.month, d.day, tzinfo=tz) + timedelta(days=1)).timestamp())
        try:
            db.run("INSERT INTO promocodes(code, kind, value, max_uses, expires_at, active, created_at) "
                   "VALUES(?,?,?,?,?,1,?)", (code, kind, value, max_uses, expires_at, int(time.time())))
            flash(f"Промокод {code} создан.", "ok")
        except sqlite3.IntegrityError:
            flash(f"Промокод {code} уже существует.", "error")
        return redirect(url_for("promos"))

    @app.post("/promos/<int:pid>/toggle")
    def promo_toggle(pid):
        db.run("UPDATE promocodes SET active = 1 - active WHERE id=?", (pid,))
        return redirect(url_for("promos"))

    @app.post("/promos/<int:pid>/delete")
    def promo_delete(pid):
        db.run("DELETE FROM promocodes WHERE id=?", (pid,))
        flash("Промокод удалён.", "ok")
        return redirect(url_for("promos"))

    # ----- заказы -----

    @app.route("/orders")
    def orders():
        status = request.args.get("status")
        if status in STATUSES:
            rows = db.q("SELECT * FROM orders WHERE status=? ORDER BY id DESC LIMIT 200", (status,))
        else:
            status = None
            rows = db.q("SELECT * FROM orders ORDER BY id DESC LIMIT 200")
        return render_template("orders.html", rows=rows, status=status)

    @app.post("/orders/<int:oid>/status")
    def order_status(oid):
        status = request.form.get("status")
        if status in STATUSES:
            db.run("UPDATE orders SET status=? WHERE id=?", (status, oid))
            flash(f"Заказ №{oid}: статус «{STATUSES[status]}».", "ok")
        return redirect(request.referrer or url_for("orders"))

    # ----- файлы -----

    @app.route("/uploads/<path:name>")
    def uploads(name):
        return send_from_directory(config.UPLOAD_DIR, name)

    return app
