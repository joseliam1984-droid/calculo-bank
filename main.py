import os
import csv
import io
import sqlite3
import statistics
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
from flask import (Flask, render_template, request, redirect,
                   url_for, Response, session, flash, jsonify)
import stripe

DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_POSTGRES  = bool(DATABASE_URL)
DB_PATH       = "calculo_bank.db"

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras

def get_conn():
    if USE_POSTGRES:
        return psycopg2.connect(DATABASE_URL)
    return sqlite3.connect(DB_PATH)

def PH(n=1):
    """Devuelve n placeholders: %s para Postgres, ? para SQLite."""
    p = "%s" if USE_POSTGRES else "?"
    return ", ".join([p] * n)

def now_sql():
    return "NOW()" if USE_POSTGRES else "datetime('now','localtime')"

def month_match_sql(col):
    if USE_POSTGRES:
        return f"TO_CHAR({col}, 'YYYY-MM') = TO_CHAR(NOW(), 'YYYY-MM')"
    return f"strftime('%Y-%m', {col}) = strftime('%Y-%m', 'now', 'localtime')"

def date_sql(col):
    return f"DATE({col})"

def serial_pk():
    return "SERIAL PRIMARY KEY" if USE_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"

def rows_to_dicts(cursor, rows):
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, r)) for r in rows]

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-in-prod")
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")

DEFAULT_NUMBERS = [14, 27, 8, 53, 31, 19, 42, 6, 75, 22]

# Límites del plan Free
FREE_CALCS_POR_MES = 30


# ── BASE DE DATOS ──────────────────────────────────────────────────────────────

def init_db():
    con = get_conn()
    cur = con.cursor()
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS usuarios (
            id            {serial_pk()},
            nombre        TEXT NOT NULL,
            email         TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            plan          TEXT DEFAULT 'free',
            stripe_customer_id TEXT DEFAULT '',
            fecha         TEXT DEFAULT ({now_sql()})
        )
    """)
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS calculos (
            id          {serial_pk()},
            usuario_id  INTEGER NOT NULL DEFAULT 0,
            etiqueta    TEXT DEFAULT '',
            categoria   TEXT DEFAULT '',
            notas       TEXT DEFAULT '',
            entrada     TEXT NOT NULL,
            cantidad    INTEGER NOT NULL,
            total       REAL NOT NULL,
            promedio    REAL NOT NULL,
            minimo      REAL NOT NULL,
            maximo      REAL NOT NULL,
            mediana     REAL NOT NULL,
            fecha       TEXT DEFAULT ({now_sql()})
        )
    """)
    con.commit()
    # Migracion segura para columnas faltantes
    migrations = [
        ("calculos", "usuario_id",  "INTEGER NOT NULL DEFAULT 0"),
        ("calculos", "etiqueta",    "TEXT DEFAULT ''"),
        ("calculos", "categoria",   "TEXT DEFAULT ''"),
        ("calculos", "notas",       "TEXT DEFAULT ''"),
        ("calculos", "minimo",      "REAL DEFAULT 0"),
        ("calculos", "maximo",      "REAL DEFAULT 0"),
        ("calculos", "mediana",     "REAL DEFAULT 0"),
        ("usuarios", "plan",        "TEXT DEFAULT 'free'"),
        ("usuarios", "stripe_customer_id", "TEXT DEFAULT ''"),
    ]
    for table, col, dfn in migrations:
        try:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {dfn}")
            con.commit()
        except Exception:
            con.rollback() if USE_POSTGRES else None
    cur.close()
    con.close()


# ── USUARIOS ───────────────────────────────────────────────────────────────────

def create_user(nombre, email, password):
    con = get_conn()
    cur = con.cursor()
    try:
        p = "%s" if USE_POSTGRES else "?"
        cur.execute(
            f"INSERT INTO usuarios (nombre, email, password_hash) VALUES ({p},{p},{p})",
            (nombre, email, generate_password_hash(password))
        )
        con.commit()
        return True, None
    except Exception:
        con.rollback() if USE_POSTGRES else None
        return False, "Ese correo ya tiene una cuenta registrada."
    finally:
        cur.close(); con.close()


def get_user_by_email(email):
    con = get_conn()
    cur = con.cursor()
    p = "%s" if USE_POSTGRES else "?"
    cur.execute(
        f"SELECT id, nombre, email, password_hash FROM usuarios WHERE email = {p}", (email,)
    )
    row = cur.fetchone()
    cur.close(); con.close()
    return {"id": row[0], "nombre": row[1], "email": row[2], "password_hash": row[3]} if row else None


def get_user_by_id(user_id):
    con = get_conn()
    cur = con.cursor()
    p = "%s" if USE_POSTGRES else "?"
    cur.execute(f"SELECT id, nombre, email FROM usuarios WHERE id = {p}", (user_id,))
    row = cur.fetchone()
    cur.close(); con.close()
    return {"id": row[0], "nombre": row[1], "email": row[2]} if row else None


def get_user_plan(usuario_id):
    con = get_conn(); cur = con.cursor()
    p = "%s" if USE_POSTGRES else "?"
    cur.execute(f"SELECT plan FROM usuarios WHERE id={p}", (usuario_id,))
    row = cur.fetchone()
    cur.close(); con.close()
    return row[0] if row else "free"


def set_user_pro(usuario_id, stripe_customer_id=""):
    con = get_conn(); cur = con.cursor()
    p = "%s" if USE_POSTGRES else "?"
    cur.execute(f"UPDATE usuarios SET plan='pro', stripe_customer_id={p} WHERE id={p}",
                (stripe_customer_id, usuario_id))
    con.commit(); cur.close(); con.close()


def set_user_free(usuario_id):
    con = get_conn(); cur = con.cursor()
    p = "%s" if USE_POSTGRES else "?"
    cur.execute(f"UPDATE usuarios SET plan='free' WHERE id={p}", (usuario_id,))
    con.commit(); cur.close(); con.close()


def get_calculos_este_mes(usuario_id):
    con = get_conn(); cur = con.cursor()
    p = "%s" if USE_POSTGRES else "?"
    cur.execute(
        f"SELECT COUNT(*) FROM calculos WHERE usuario_id={p} AND {month_match_sql('fecha')}",
        (usuario_id,)
    )
    count = cur.fetchone()[0]
    cur.close(); con.close()
    return count


def update_user(user_id, nombre, email, new_password=None):
    con = get_conn(); cur = con.cursor()
    p = "%s" if USE_POSTGRES else "?"
    try:
        if new_password:
            cur.execute(
                f"UPDATE usuarios SET nombre={p}, email={p}, password_hash={p} WHERE id={p}",
                (nombre, email, generate_password_hash(new_password), user_id)
            )
        else:
            cur.execute(
                f"UPDATE usuarios SET nombre={p}, email={p} WHERE id={p}",
                (nombre, email, user_id)
            )
        con.commit()
        return True, None
    except Exception:
        con.rollback() if USE_POSTGRES else None
        return False, "Ese correo ya está en uso por otra cuenta."
    finally:
        cur.close(); con.close()


# ── CÁLCULOS ───────────────────────────────────────────────────────────────────

def save_calculo(usuario_id, etiqueta, categoria, notas, entrada,
                 cantidad, total, promedio, minimo, maximo, mediana):
    con = get_conn(); cur = con.cursor()
    ph = "%s" if USE_POSTGRES else "?"
    vals = ",".join([ph]*11)
    cur.execute(
        f"""INSERT INTO calculos
           (usuario_id,etiqueta,categoria,notas,entrada,cantidad,total,promedio,minimo,maximo,mediana)
           VALUES ({vals})""",
        (usuario_id, etiqueta, categoria, notas, entrada,
         cantidad, total, promedio, minimo, maximo, mediana)
    )
    con.commit(); cur.close(); con.close()


def _row_to_dict(r):
    return {
        "id": r[0], "etiqueta": r[1] or "Sin etiqueta",
        "categoria": r[2] or "General", "notas": r[3] or "",
        "input": r[4], "count": r[5], "total": r[6],
        "average": r[7], "min": r[8], "max": r[9], "median": r[10], "fecha": str(r[11]),
    }


def get_history(usuario_id, limit=20):
    con = get_conn(); cur = con.cursor()
    p = "%s" if USE_POSTGRES else "?"
    cur.execute(
        f"""SELECT id,etiqueta,categoria,notas,entrada,cantidad,total,
                  promedio,minimo,maximo,mediana,fecha
           FROM calculos WHERE usuario_id={p} ORDER BY id DESC LIMIT {p}""",
        (usuario_id, limit)
    )
    rows = cur.fetchall(); cur.close(); con.close()
    return [_row_to_dict(r) for r in rows]


def get_history_filtered(usuario_id, buscar="", categoria="", limit=20):
    con = get_conn(); cur = con.cursor()
    p = "%s" if USE_POSTGRES else "?"
    sql = f"""SELECT id,etiqueta,categoria,notas,entrada,cantidad,total,
                    promedio,minimo,maximo,mediana,fecha
             FROM calculos WHERE usuario_id={p}"""
    params = [usuario_id]
    if buscar:
        sql += f" AND (etiqueta LIKE {p} OR notas LIKE {p})"
        params += [f"%{buscar}%", f"%{buscar}%"]
    if categoria:
        sql += f" AND categoria = {p}"
        params.append(categoria)
    sql += f" ORDER BY id DESC LIMIT {p}"
    params.append(limit)
    cur.execute(sql, params)
    rows = cur.fetchall(); cur.close(); con.close()
    return [_row_to_dict(r) for r in rows]


def get_categorias(usuario_id):
    con = get_conn(); cur = con.cursor()
    p = "%s" if USE_POSTGRES else "?"
    cur.execute(
        f"SELECT DISTINCT categoria FROM calculos WHERE usuario_id={p} AND categoria!='' ORDER BY categoria",
        (usuario_id,)
    )
    rows = cur.fetchall(); cur.close(); con.close()
    return [r[0] for r in rows]


def clear_history(usuario_id):
    con = get_conn(); cur = con.cursor()
    p = "%s" if USE_POSTGRES else "?"
    cur.execute(f"DELETE FROM calculos WHERE usuario_id={p}", (usuario_id,))
    con.commit(); cur.close(); con.close()


def delete_one(record_id, usuario_id):
    con = get_conn(); cur = con.cursor()
    p = "%s" if USE_POSTGRES else "?"
    cur.execute(f"DELETE FROM calculos WHERE id={p} AND usuario_id={p}", (record_id, usuario_id))
    con.commit(); cur.close(); con.close()


def get_metrics(usuario_id):
    con = get_conn(); cur = con.cursor()
    p = "%s" if USE_POSTGRES else "?"

    cur.execute(f"SELECT COUNT(*) FROM calculos WHERE usuario_id={p}", (usuario_id,))
    total_calcs = cur.fetchone()[0]

    cur.execute(f"SELECT AVG(promedio) FROM calculos WHERE usuario_id={p}", (usuario_id,))
    avg_of_avgs = cur.fetchone()[0] or 0

    cur.execute(
        f"""SELECT COALESCE(NULLIF(categoria,''),'General'), COUNT(*), ROUND(AVG(promedio)::numeric,2)
           FROM calculos WHERE usuario_id={p}
           GROUP BY categoria ORDER BY COUNT(*) DESC""" if USE_POSTGRES else
        f"""SELECT COALESCE(NULLIF(categoria,''),'General'), COUNT(*), ROUND(AVG(promedio),2)
           FROM calculos WHERE usuario_id={p}
           GROUP BY categoria ORDER BY COUNT(*) DESC""",
        (usuario_id,)
    )
    by_cat = cur.fetchall()

    cur.execute(
        f"""SELECT DATE(fecha::timestamp), ROUND(AVG(promedio)::numeric,2), COUNT(*)
           FROM calculos WHERE usuario_id={p}
           GROUP BY DATE(fecha::timestamp) ORDER BY DATE(fecha::timestamp) ASC LIMIT 30""" if USE_POSTGRES else
        f"""SELECT DATE(fecha), ROUND(AVG(promedio),2), COUNT(*)
           FROM calculos WHERE usuario_id={p}
           GROUP BY DATE(fecha) ORDER BY DATE(fecha) ASC LIMIT 30""",
        (usuario_id,)
    )
    evolution = cur.fetchall()

    cur.execute(f"SELECT etiqueta, promedio FROM calculos WHERE usuario_id={p} ORDER BY promedio DESC LIMIT 1", (usuario_id,))
    best = cur.fetchone()
    cur.execute(f"SELECT etiqueta, promedio FROM calculos WHERE usuario_id={p} ORDER BY promedio ASC LIMIT 1", (usuario_id,))
    worst = cur.fetchone()

    cur.close(); con.close()
    return {
        "total_calcs": total_calcs,
        "avg_of_avgs": round(float(avg_of_avgs), 2),
        "by_cat": [{"cat": r[0], "count": r[1], "avg": float(r[2])} for r in by_cat],
        "evolution": [{"fecha": str(r[0]), "avg": float(r[1]), "count": r[2]} for r in evolution],
        "best":  {"etiqueta": best[0] or "Sin etiqueta",  "promedio": best[1]}  if best  else None,
        "worst": {"etiqueta": worst[0] or "Sin etiqueta", "promedio": worst[1]} if worst else None,
    }


# ── LÓGICA ─────────────────────────────────────────────────────────────────────

def calculate_stats(numbers):
    if not numbers:
        return 0, 0, 0, 0, 0
    return (
        round(sum(numbers) / len(numbers), 4),
        round(min(numbers), 4),
        round(max(numbers), 4),
        round(statistics.median(numbers), 4),
        round(sum(numbers), 4),
    )


def parse_numbers(raw):
    results, errors = [], []
    for token in raw.replace(",", " ").split():
        try:
            results.append(float(token))
        except ValueError:
            errors.append(token)
    return results, errors


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            flash("Inicia sesión para continuar.", "error")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ── AUTENTICACIÓN ──────────────────────────────────────────────────────────────

@app.route("/registro", methods=["GET", "POST"])
def registro():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        nombre   = request.form.get("nombre", "").strip()
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm", "")
        if not nombre or not email or not password:
            flash("Todos los campos son obligatorios.", "error")
        elif password != confirm:
            flash("Las contraseñas no coinciden.", "error")
        elif len(password) < 6:
            flash("La contraseña debe tener al menos 6 caracteres.", "error")
        else:
            ok, err = create_user(nombre, email, password)
            if ok:
                flash("Cuenta creada. Ya puedes iniciar sesión.", "success")
                return redirect(url_for("login"))
            else:
                flash(err, "error")
    return render_template("registro.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user     = get_user_by_email(email)
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"]   = user["id"]
            session["user_name"] = user["nombre"]
            return redirect(url_for("dashboard"))
        flash("Correo o contraseña incorrectos.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── RUTAS PRINCIPALES ──────────────────────────────────────────────────────────

@app.route("/", methods=["GET", "POST"])
@login_required
def dashboard():
    user_id      = session["user_id"]
    numbers      = DEFAULT_NUMBERS
    raw_input    = ""
    etiqueta     = ""
    categoria    = ""
    notas        = ""
    parse_errors = []
    user_submitted = False

    plan           = get_user_plan(user_id)
    calcs_mes      = get_calculos_este_mes(user_id)
    limite_alcanzado = plan == "free" and calcs_mes >= FREE_CALCS_POR_MES

    if request.method == "POST":
        if request.form.get("action") == "clear":
            clear_history(user_id)
            return redirect(url_for("dashboard"))

        raw_input = request.form.get("numbers",   "").strip()
        etiqueta  = request.form.get("etiqueta",  "").strip()
        categoria = request.form.get("categoria", "").strip()
        notas     = request.form.get("notas",     "").strip()
        parsed, parse_errors = parse_numbers(raw_input)

        if parsed:
            if limite_alcanzado:
                flash(f"Alcanzaste los {FREE_CALCS_POR_MES} cálculos del plan Free este mes. Actualiza a Pro para continuar.", "upgrade")
            else:
                numbers = parsed
                user_submitted = True
                avg, minimo, maximo, mediana, total = calculate_stats(numbers)
                save_calculo(
                    usuario_id=user_id, etiqueta=etiqueta, categoria=categoria,
                    notas=notas, entrada=raw_input, cantidad=len(numbers),
                    total=total, promedio=avg, minimo=minimo, maximo=maximo, mediana=mediana,
                )
                calcs_mes += 1

    buscar_q   = request.args.get("buscar", "").strip()
    cat_filter = request.args.get("categoria", "").strip()

    avg, minimo, maximo, mediana, total = calculate_stats(numbers)
    history    = get_history_filtered(user_id, buscar=buscar_q, categoria=cat_filter)
    categorias = get_categorias(user_id)

    chart_labels   = [h["etiqueta"] if h["etiqueta"] != "Sin etiqueta" else f"#{h['id']}"
                      for h in reversed(history)]
    chart_averages = [h["average"] for h in reversed(history)]

    return render_template("index.html",
        numbers=numbers, average=avg, minimo=minimo, maximo=maximo,
        mediana=mediana, count=len(numbers), total=total,
        raw_input=raw_input, etiqueta=etiqueta, categoria=categoria, notas=notas,
        parse_errors=parse_errors, user_submitted=user_submitted,
        history=history, categorias=categorias,
        buscar_q=buscar_q, cat_filter=cat_filter,
        chart_labels=chart_labels, chart_averages=chart_averages,
        user_name=session.get("user_name", ""),
        plan=plan, calcs_mes=calcs_mes,
        limite_alcanzado=limite_alcanzado,
        free_limit=FREE_CALCS_POR_MES,
    )


@app.route("/metricas")
@login_required
def metricas():
    m = get_metrics(session["user_id"])
    return render_template("metricas.html", m=m, user_name=session.get("user_name", ""))


@app.route("/importar", methods=["GET", "POST"])
@login_required
def importar():
    if request.method == "POST":
        archivo   = request.files.get("archivo")
        etiqueta  = request.form.get("etiqueta",  "").strip()
        categoria = request.form.get("categoria", "").strip()
        notas     = request.form.get("notas",     "").strip()

        if not archivo or archivo.filename == "":
            flash("Selecciona un archivo CSV.", "error")
            return render_template("importar.html", user_name=session.get("user_name", ""))

        try:
            content = archivo.read().decode("utf-8-sig")
            reader  = csv.reader(io.StringIO(content))
            numbers = []
            for row in reader:
                for cell in row:
                    try:
                        numbers.append(float(cell.strip()))
                    except ValueError:
                        pass

            if not numbers:
                flash("No se encontraron números válidos en el archivo.", "error")
                return render_template("importar.html", user_name=session.get("user_name", ""))

            avg, minimo, maximo, mediana, total = calculate_stats(numbers)
            entrada_preview = " ".join(str(n) for n in numbers[:30])
            if len(numbers) > 30:
                entrada_preview += f" ... (+{len(numbers)-30} más)"

            save_calculo(
                usuario_id=session["user_id"],
                etiqueta=etiqueta or archivo.filename,
                categoria=categoria,
                notas=notas or f"Importado desde {archivo.filename}",
                entrada=entrada_preview,
                cantidad=len(numbers),
                total=total, promedio=avg, minimo=minimo, maximo=maximo, mediana=mediana,
            )
            flash(f"Importados {len(numbers)} números. Promedio: {avg}", "success")
            return redirect(url_for("dashboard"))
        except Exception as e:
            flash(f"Error al procesar el archivo: {e}", "error")

    return render_template("importar.html", user_name=session.get("user_name", ""))


@app.route("/perfil", methods=["GET", "POST"])
@login_required
def perfil():
    user = get_user_by_id(session["user_id"])
    if request.method == "POST":
        nombre       = request.form.get("nombre", "").strip()
        email        = request.form.get("email",  "").strip().lower()
        new_password = request.form.get("new_password", "")
        confirm      = request.form.get("confirm", "")
        current_pw   = request.form.get("current_password", "")

        if not check_password_hash(user["password_hash"] if hasattr(user, "get") else
                                   get_user_by_id(session["user_id"])["password_hash"] or "",
                                   current_pw):
            pass  # handled below via re-fetch

        user_full = get_user_by_email(user["email"])
        if not check_password_hash(user_full["password_hash"], current_pw):
            flash("Contraseña actual incorrecta.", "error")
        elif new_password and new_password != confirm:
            flash("Las contraseñas nuevas no coinciden.", "error")
        elif new_password and len(new_password) < 6:
            flash("La nueva contraseña debe tener al menos 6 caracteres.", "error")
        else:
            ok, err = update_user(session["user_id"], nombre, email,
                                  new_password if new_password else None)
            if ok:
                session["user_name"] = nombre
                flash("Perfil actualizado correctamente.", "success")
                return redirect(url_for("perfil"))
            else:
                flash(err, "error")

    con = get_conn(); cur = con.cursor()
    p = "%s" if USE_POSTGRES else "?"
    cur.execute(f"SELECT COUNT(*), MIN(fecha) FROM calculos WHERE usuario_id={p}", (session["user_id"],))
    total_calcs = cur.fetchone()
    cur.close(); con.close()

    return render_template("perfil.html",
        user=user, user_name=session.get("user_name", ""),
        total_calcs=total_calcs[0], miembro_desde=total_calcs[1] or "—"
    )


@app.route("/eliminar/<int:record_id>", methods=["POST"])
@login_required
def eliminar(record_id):
    delete_one(record_id, session["user_id"])
    return redirect(url_for("dashboard"))


@app.route("/exportar")
@login_required
def exportar():
    history = get_history(session["user_id"], limit=10000)
    output  = io.StringIO()
    writer  = csv.DictWriter(output,
        fieldnames=["id","etiqueta","categoria","notas","input","count",
                    "total","average","min","max","median","fecha"])
    writer.writeheader()
    writer.writerows(history)
    resp = Response(output.getvalue(), mimetype="text/csv")
    resp.headers["Content-Disposition"] = "attachment; filename=calculo_bank.csv"
    return resp


@app.route("/precios")
def precios():
    plan = get_user_plan(session["user_id"]) if "user_id" in session else "free"
    return render_template("precios.html",
        user_name=session.get("user_name", ""),
        plan=plan,
        stripe_configured=bool(stripe.api_key),
    )


@app.route("/upgrade")
@login_required
def upgrade():
    if not stripe.api_key:
        flash("Los pagos no están configurados todavía.", "error")
        return redirect(url_for("precios"))

    price_id = os.environ.get("STRIPE_PRICE_ID", "")
    if not price_id:
        flash("STRIPE_PRICE_ID no configurado.", "error")
        return redirect(url_for("precios"))

    user = get_user_by_id(session["user_id"])
    if not user:
        session.clear()
        flash("Sesión expirada. Inicia sesión de nuevo.", "error")
        return redirect(url_for("login"))

    checkout = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{"price": price_id, "quantity": 1}],
        mode="subscription",
        success_url=url_for("dashboard", _external=True) + "?upgrade=success",
        cancel_url=url_for("precios", _external=True),
        client_reference_id=str(session["user_id"]),
        customer_email=user["email"],
    )
    return redirect(checkout.url)


@app.route("/webhook", methods=["POST"])
def stripe_webhook():
    payload        = request.get_data()
    sig_header     = request.headers.get("Stripe-Signature", "")
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except Exception:
        return "Invalid signature", 400

    if event["type"] in ("checkout.session.completed", "invoice.paid"):
        obj     = event["data"]["object"]
        user_id = int(obj.get("client_reference_id") or 0)
        if user_id:
            set_user_pro(user_id, obj.get("customer", ""))

    elif event["type"] in ("customer.subscription.deleted", "invoice.payment_failed"):
        customer_id = event["data"]["object"].get("customer", "")
        if customer_id:
            con = get_conn(); cur = con.cursor()
            p = "%s" if USE_POSTGRES else "?"
            cur.execute(f"SELECT id FROM usuarios WHERE stripe_customer_id={p}", (customer_id,))
            row = cur.fetchone()
            cur.close(); con.close()
            if row:
                set_user_free(row[0])

    return "ok", 200


@app.route("/api/calculos")
@login_required
def api_calculos():
    buscar   = request.args.get("buscar", "")
    categoria = request.args.get("categoria", "")
    limit    = min(int(request.args.get("limit", 100)), 1000)
    data     = get_history_filtered(session["user_id"], buscar=buscar,
                                    categoria=categoria, limit=limit)
    return jsonify({"status": "ok", "count": len(data), "data": data})


@app.route("/admin/activar-pro/<token>")
def admin_activar_pro(token):
    admin_token = os.environ.get("ADMIN_TOKEN", "").strip()
    if not admin_token or token.strip() != admin_token:
        return "No autorizado", 403
    email = request.args.get("email", "").strip()
    if not email:
        return "Falta el parametro email", 400
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE usuarios SET plan='pro' WHERE email=?", (email,))
    con.commit()
    affected = con.execute("SELECT changes()").fetchone()[0]
    con.close()
    if affected:
        return f"Plan Pro activado para {email}", 200
    return f"Usuario {email} no encontrado", 404


init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port, debug=True)
