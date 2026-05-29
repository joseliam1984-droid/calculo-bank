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

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-in-prod")
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")

DEFAULT_NUMBERS = [14, 27, 8, 53, 31, 19, 42, 6, 75, 22]
DB_PATH = "calculo_bank.db"

# Límites del plan Free
FREE_CALCS_POR_MES = 30


# ── BASE DE DATOS ──────────────────────────────────────────────────────────────

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre        TEXT NOT NULL,
            email         TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            fecha         TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS calculos (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
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
            fecha       TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
        )
    """)
    con.commit()
    for col, dfn in [
        ("usuario_id",         "INTEGER NOT NULL DEFAULT 0"),
        ("etiqueta",           "TEXT DEFAULT ''"),
        ("categoria",          "TEXT DEFAULT ''"),
        ("notas",              "TEXT DEFAULT ''"),
        ("minimo",             "REAL DEFAULT 0"),
        ("maximo",             "REAL DEFAULT 0"),
        ("mediana",            "REAL DEFAULT 0"),
    ]:
        try:
            con.execute(f"ALTER TABLE calculos ADD COLUMN {col} {dfn}")
            con.commit()
        except sqlite3.OperationalError:
            pass

    for col, dfn in [
        ("plan",               "TEXT DEFAULT 'free'"),
        ("stripe_customer_id", "TEXT DEFAULT ''"),
    ]:
        try:
            con.execute(f"ALTER TABLE usuarios ADD COLUMN {col} {dfn}")
            con.commit()
        except sqlite3.OperationalError:
            pass

    con.close()


# ── USUARIOS ───────────────────────────────────────────────────────────────────

def create_user(nombre, email, password):
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute(
            "INSERT INTO usuarios (nombre, email, password_hash) VALUES (?,?,?)",
            (nombre, email, generate_password_hash(password))
        )
        con.commit()
        return True, None
    except sqlite3.IntegrityError:
        return False, "Ese correo ya tiene una cuenta registrada."
    finally:
        con.close()


def get_user_by_email(email):
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT id, nombre, email, password_hash FROM usuarios WHERE email = ?", (email,)
    ).fetchone()
    con.close()
    return {"id": row[0], "nombre": row[1], "email": row[2], "password_hash": row[3]} if row else None


def get_user_by_id(user_id):
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT id, nombre, email FROM usuarios WHERE id = ?", (user_id,)
    ).fetchone()
    con.close()
    return {"id": row[0], "nombre": row[1], "email": row[2]} if row else None


def get_user_plan(usuario_id):
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT plan FROM usuarios WHERE id=?", (usuario_id,)).fetchone()
    con.close()
    return row[0] if row else "free"


def set_user_pro(usuario_id, stripe_customer_id=""):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "UPDATE usuarios SET plan='pro', stripe_customer_id=? WHERE id=?",
        (stripe_customer_id, usuario_id)
    )
    con.commit()
    con.close()


def set_user_free(usuario_id):
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE usuarios SET plan='free' WHERE id=?", (usuario_id,))
    con.commit()
    con.close()


def get_calculos_este_mes(usuario_id):
    con = sqlite3.connect(DB_PATH)
    count = con.execute(
        """SELECT COUNT(*) FROM calculos
           WHERE usuario_id=?
           AND strftime('%Y-%m', fecha) = strftime('%Y-%m', 'now', 'localtime')""",
        (usuario_id,)
    ).fetchone()[0]
    con.close()
    return count


def update_user(user_id, nombre, email, new_password=None):
    con = sqlite3.connect(DB_PATH)
    try:
        if new_password:
            con.execute(
                "UPDATE usuarios SET nombre=?, email=?, password_hash=? WHERE id=?",
                (nombre, email, generate_password_hash(new_password), user_id)
            )
        else:
            con.execute(
                "UPDATE usuarios SET nombre=?, email=? WHERE id=?",
                (nombre, email, user_id)
            )
        con.commit()
        return True, None
    except sqlite3.IntegrityError:
        return False, "Ese correo ya está en uso por otra cuenta."
    finally:
        con.close()


# ── CÁLCULOS ───────────────────────────────────────────────────────────────────

def save_calculo(usuario_id, etiqueta, categoria, notas, entrada,
                 cantidad, total, promedio, minimo, maximo, mediana):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        """INSERT INTO calculos
           (usuario_id,etiqueta,categoria,notas,entrada,cantidad,total,promedio,minimo,maximo,mediana)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (usuario_id, etiqueta, categoria, notas, entrada,
         cantidad, total, promedio, minimo, maximo, mediana)
    )
    con.commit()
    con.close()


def _row_to_dict(r):
    return {
        "id": r[0], "etiqueta": r[1] or "Sin etiqueta",
        "categoria": r[2] or "General", "notas": r[3] or "",
        "input": r[4], "count": r[5], "total": r[6],
        "average": r[7], "min": r[8], "max": r[9], "median": r[10], "fecha": r[11],
    }


def get_history(usuario_id, limit=20):
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        """SELECT id,etiqueta,categoria,notas,entrada,cantidad,total,
                  promedio,minimo,maximo,mediana,fecha
           FROM calculos WHERE usuario_id=? ORDER BY id DESC LIMIT ?""",
        (usuario_id, limit)
    ).fetchall()
    con.close()
    return [_row_to_dict(r) for r in rows]


def get_history_filtered(usuario_id, buscar="", categoria="", limit=20):
    con = sqlite3.connect(DB_PATH)
    sql = """SELECT id,etiqueta,categoria,notas,entrada,cantidad,total,
                    promedio,minimo,maximo,mediana,fecha
             FROM calculos WHERE usuario_id=?"""
    params = [usuario_id]
    if buscar:
        sql += " AND (etiqueta LIKE ? OR notas LIKE ?)"
        params += [f"%{buscar}%", f"%{buscar}%"]
    if categoria:
        sql += " AND categoria = ?"
        params.append(categoria)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = con.execute(sql, params).fetchall()
    con.close()
    return [_row_to_dict(r) for r in rows]


def get_categorias(usuario_id):
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT DISTINCT categoria FROM calculos WHERE usuario_id=? AND categoria!='' ORDER BY categoria",
        (usuario_id,)
    ).fetchall()
    con.close()
    return [r[0] for r in rows]


def clear_history(usuario_id):
    con = sqlite3.connect(DB_PATH)
    con.execute("DELETE FROM calculos WHERE usuario_id=?", (usuario_id,))
    con.commit()
    con.close()


def delete_one(record_id, usuario_id):
    con = sqlite3.connect(DB_PATH)
    con.execute("DELETE FROM calculos WHERE id=? AND usuario_id=?", (record_id, usuario_id))
    con.commit()
    con.close()


def get_metrics(usuario_id):
    con = sqlite3.connect(DB_PATH)

    total_calcs = con.execute(
        "SELECT COUNT(*) FROM calculos WHERE usuario_id=?", (usuario_id,)
    ).fetchone()[0]

    avg_of_avgs = con.execute(
        "SELECT AVG(promedio) FROM calculos WHERE usuario_id=?", (usuario_id,)
    ).fetchone()[0] or 0

    by_cat = con.execute(
        """SELECT COALESCE(NULLIF(categoria,''),'General'), COUNT(*), ROUND(AVG(promedio),2)
           FROM calculos WHERE usuario_id=?
           GROUP BY categoria ORDER BY COUNT(*) DESC""",
        (usuario_id,)
    ).fetchall()

    evolution = con.execute(
        """SELECT DATE(fecha), ROUND(AVG(promedio),2), COUNT(*)
           FROM calculos WHERE usuario_id=?
           GROUP BY DATE(fecha) ORDER BY DATE(fecha) ASC LIMIT 30""",
        (usuario_id,)
    ).fetchall()

    best = con.execute(
        "SELECT etiqueta, promedio FROM calculos WHERE usuario_id=? ORDER BY promedio DESC LIMIT 1",
        (usuario_id,)
    ).fetchone()

    worst = con.execute(
        "SELECT etiqueta, promedio FROM calculos WHERE usuario_id=? ORDER BY promedio ASC LIMIT 1",
        (usuario_id,)
    ).fetchone()

    con.close()
    return {
        "total_calcs": total_calcs,
        "avg_of_avgs": round(avg_of_avgs, 2),
        "by_cat": [{"cat": r[0], "count": r[1], "avg": r[2]} for r in by_cat],
        "evolution": [{"fecha": r[0], "avg": r[1], "count": r[2]} for r in evolution],
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

    con = sqlite3.connect(DB_PATH)
    total_calcs = con.execute(
        "SELECT COUNT(*), MIN(fecha) FROM calculos WHERE usuario_id=?",
        (session["user_id"],)
    ).fetchone()
    con.close()

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

    checkout = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{"price": price_id, "quantity": 1}],
        mode="subscription",
        success_url=url_for("dashboard", _external=True) + "?upgrade=success",
        cancel_url=url_for("precios", _external=True),
        client_reference_id=str(session["user_id"]),
        customer_email=get_user_by_id(session["user_id"])["email"],
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
            con = sqlite3.connect(DB_PATH)
            row = con.execute(
                "SELECT id FROM usuarios WHERE stripe_customer_id=?", (customer_id,)
            ).fetchone()
            con.close()
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
