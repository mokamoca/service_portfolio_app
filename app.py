from datetime import datetime
import os
import csv
import io
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for, flash, Response
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import validates

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "app.db")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB_PATH}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# ---------- Model ----------
class Booking(db.Model):
    __tablename__ = "bookings"
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(200))
    phone = db.Column(db.String(50), nullable=False)

    service_type = db.Column(db.String(50), nullable=False)  # 'grave_cleaning', etc.
    location = db.Column(db.String(300), nullable=False)
    preferred_date = db.Column(db.Date, nullable=True)

    options_photoreport = db.Column(db.Boolean, default=False)
    message = db.Column(db.Text)

    est_price = db.Column(db.Integer, default=0)

    status = db.Column(db.String(20), default="new")  # new, confirmed, done, canceled
    admin_note = db.Column(db.Text)

    @validates("email")
    def validate_email(self, key, value):
        if value and ("@" not in value or "." not in value):
            raise ValueError("メール形式が不正です")
        return value

# ---------- Estimate Logic ----------
SERVICE_PRICE_TABLE = {
    "grave_cleaning": 6000,
    "appliance_setup": 8000,
    "errand": 4000,
}

OPTION_PRICE = {
    "photoreport": 1000,
}

def calc_estimate(service_type: str, options: dict) -> int:
    base = SERVICE_PRICE_TABLE.get(service_type, 0)
    extra = OPTION_PRICE["photoreport"] if options.get("photoreport") else 0
    return base + extra

# ---------- Basic Auth (Admin) ----------
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "changeme")

def check_auth(username, password):
    return username == ADMIN_USER and password == ADMIN_PASS

def authenticate():
    return Response("認証が必要です", 401, {"WWW-Authenticate": 'Basic realm="Admin Area"'})

def requires_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return wrapper

# ---------- Public ----------
# 年をテンプレに常に渡す
@app.context_processor
def inject_globals():
    from datetime import datetime
    return dict(current_year=datetime.now().year)


@app.route("/")
def index():
    return render_template(
    "booking_form.html",
    service_price_table=SERVICE_PRICE_TABLE,
    photo_option_price=OPTION_PRICE["photoreport"],
)


@app.route("/estimate", methods=["POST"])
def estimate():
    service_type = request.form.get("service_type")
    photoreport = request.form.get("options_photoreport") == "on"
    price = calc_estimate(service_type, {"photoreport": photoreport})
    return render_template("_estimate_fragment.html", price=price)

@app.route("/book", methods=["POST"])
def book():
    try:
        name = (request.form.get("name") or "").strip()
        email = (request.form.get("email") or "").strip() or None
        phone = (request.form.get("phone") or "").strip()
        service_type = request.form.get("service_type")
        location = (request.form.get("location") or "").strip()
        preferred_date_raw = request.form.get("preferred_date")
        preferred_date = (
            datetime.strptime(preferred_date_raw, "%Y-%m-%d").date()
            if preferred_date_raw else None
        )
        options_photoreport = request.form.get("options_photoreport") == "on"
        message = (request.form.get("message") or "").strip() or None

        est_price = calc_estimate(service_type, {"photoreport": options_photoreport})

        if not name or not phone or not service_type or not location:
            flash("必須項目が未入力です", "error")
            return redirect(url_for("index"))

        b = Booking(
            name=name,
            email=email,
            phone=phone,
            service_type=service_type,
            location=location,
            preferred_date=preferred_date,
            options_photoreport=options_photoreport,
            message=message,
            est_price=est_price,
        )
        db.session.add(b)
        db.session.commit()
        return render_template("thanks.html", booking=b)
    except Exception as e:
        app.logger.exception(e)
        flash(f"保存時にエラー: {e}", "error")
        return redirect(url_for("index"))

# ---------- Admin ----------
from sqlalchemy import or_

@app.route("/admin")
@requires_auth
def admin_list():
    q = request.args.get("q", "")
    status = request.args.get("status", "")

    query = Booking.query
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Booking.name.like(like),
                                 Booking.phone.like(like),
                                 Booking.location.like(like)))
    if status:
        query = query.filter(Booking.status == status)

    bookings = query.order_by(Booking.created_at.desc()).all()
    return render_template("admin_list.html", bookings=bookings, q=q, status=status)

@app.route("/admin/<int:booking_id>")
@requires_auth
def admin_detail(booking_id):
    b = Booking.query.get_or_404(booking_id)
    return render_template("admin_detail.html", b=b)

@app.route("/admin/<int:booking_id>/update", methods=["POST"])
@requires_auth
def admin_update(booking_id):
    b = Booking.query.get_or_404(booking_id)
    b.status = request.form.get("status", b.status)
    b.admin_note = request.form.get("admin_note", b.admin_note)
    db.session.commit()
    flash("更新しました", "success")
    return redirect(url_for("admin_detail", booking_id=b.id))

@app.route("/admin/export.csv")
@requires_auth
def admin_export_csv():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID","作成日時","名前","電話","メール","サービス","住所/場所",
                     "希望日","写真レポ","見積","ステータス","管理メモ"])
    for b in Booking.query.order_by(Booking.id).all():
        writer.writerow([
            b.id,
            b.created_at.strftime("%Y-%m-%d %H:%M"),
            b.name,
            b.phone,
            b.email or "",
            b.service_type,
            b.location,
            b.preferred_date.isoformat() if b.preferred_date else "",
            "Yes" if b.options_photoreport else "No",
            b.est_price,
            b.status,
            (b.admin_note or "").replace("\n", " "),
        ])
    content = output.getvalue()
    return Response(
        content,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=bookings.csv"}
    )

# ---------- CLI ----------
@app.cli.command("init-db")
def init_db():
    db.create_all()
    print("DB initialized at", DB_PATH)

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True)
