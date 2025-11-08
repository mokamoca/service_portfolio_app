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

import re
from datetime import date


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

# --- Bookingモデル定義の直後に追加/移動 ---
with app.app_context():
    db.create_all()
    print("DB ready at", DB_PATH)
# -----------------------------------------


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

# ---------- Validation ----------
import re
from datetime import date

ALLOWED_SERVICE_TYPES = set(SERVICE_PRICE_TABLE.keys())
PHONE_RE = re.compile(r"^[0-9+\-() ]{9,16}$")

def validate_and_clean(form):
    """フォーム値を整形しつつ検証し、(cleaned_data, errors) を返す"""
    # 1) まず整形（stripや型変換）
    data = {
        "name": (form.get("name") or "").strip(),
        "email": (form.get("email") or "").strip(),
        "phone": (form.get("phone") or "").strip(),
        "service_type": form.get("service_type") or "",
        "location": (form.get("location") or "").strip(),
        "preferred_date": form.get("preferred_date") or "",
        "options_photoreport": (form.get("options_photoreport") == "on"),
        "message": (form.get("message") or "").strip(),
    }
    errors = {}

    # 2) 必須
    for key in ["name", "phone", "service_type", "location"]:
        if not data[key]:
            errors[key] = "必須項目です"

    # 3) サービス種別
    if data["service_type"] and data["service_type"] not in ALLOWED_SERVICE_TYPES:
        errors["service_type"] = "不正な選択です"

    # 4) 電話番号
    if data["phone"] and not PHONE_RE.match(data["phone"]):
        errors["phone"] = "数字・+ - () とスペースのみ、9〜16桁で入力してください"

    # 5) メール（任意）
    if data["email"] and ("@" not in data["email"] or "." not in data["email"] or len(data["email"]) > 200):
        errors["email"] = "メール形式が不正です"

    # 6) 文字数
    if len(data["name"]) > 120:
        errors["name"] = "120文字以内で入力してください"
    if len(data["location"]) > 300:
        errors["location"] = "300文字以内で入力してください"
    if len(data["message"]) > 500:
        errors["message"] = "500文字以内で入力してください"

    # 7) 日付（任意）：過去不可、半年先まで
    if data["preferred_date"]:
        try:
            d = datetime.strptime(data["preferred_date"], "%Y-%m-%d").date()
        except ValueError:
            errors["preferred_date"] = "日付の形式が不正です"
        else:
            if d < date.today():
                errors["preferred_date"] = "過去の日付は選べません"
            elif (d - date.today()).days > 180:
                errors["preferred_date"] = "半年より先の予約はできません"

    return data, errors


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
        form_data={},        # ← 追加
        errors={},           # ← 追加
        price=0,             # ← 初期の概算価格
    )

@app.route("/estimate", methods=["POST"])
def estimate():
    service_type = request.form.get("service_type")
    photoreport = request.form.get("options_photoreport") == "on"
    # 不正 service_type は 0 で返す（安全化）
    if service_type not in SERVICE_PRICE_TABLE:
        return render_template("_estimate_fragment.html", price=0)
    price = calc_estimate(service_type, {"photoreport": photoreport})
    return render_template("_estimate_fragment.html", price=price)

@app.route("/book", methods=["POST"])
def book():
    data, errors = validate_and_clean(request.form)
    if errors:
        # 入力値を保持したまま、その場で再描画（400を返すとブラウザもエラー扱いするが問題なし）
        price = calc_estimate(
            data["service_type"], {"photoreport": data["options_photoreport"]}
        ) if data["service_type"] in SERVICE_PRICE_TABLE else 0
        return render_template(
            "booking_form.html",
            service_price_table=SERVICE_PRICE_TABLE,
            photo_option_price=OPTION_PRICE["photoreport"],
            form_data=data,
            errors=errors,
            price=price,
        ), 400

    # ここからは保存（サーバ側で価格を再計算）
    preferred_date = (
        datetime.strptime(data["preferred_date"], "%Y-%m-%d").date()
        if data["preferred_date"] else None
    )
    est_price = calc_estimate(data["service_type"], {"photoreport": data["options_photoreport"]})

    b = Booking(
        name=data["name"],
        email=(data["email"] or None),
        phone=data["phone"],
        service_type=data["service_type"],
        location=data["location"],
        preferred_date=preferred_date,
        options_photoreport=data["options_photoreport"],
        message=(data["message"] or None),
        est_price=est_price,
    )
    db.session.add(b)
    db.session.commit()
    return render_template("thanks.html", booking=b)


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
