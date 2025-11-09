from datetime import datetime
import os
import csv
import io
from functools import wraps

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    Response,
    session,
    abort,
    make_response,
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
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
app.jinja_env.globals["getattr"] = getattr

# ---------- Model ----------
class Booking(db.Model):
    __tablename__ = "bookings"
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(200))
    phone = db.Column(db.String(50), nullable=False)

    service_type = db.Column(db.String(50), nullable=False)  # 'storefront_cleaning', etc.
    location = db.Column(db.String(300), nullable=False)
    preferred_date = db.Column(db.Date, nullable=True)

    options_photoreport = db.Column(db.Boolean, default=False)
    options_priority_visit = db.Column(db.Boolean, default=False)
    options_weekend_visit = db.Column(db.Boolean, default=False)
    options_extra_staff = db.Column(db.Boolean, default=False)
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
    "storefront_cleaning": 15000,
    "fixture_install": 26000,
    "event_support": 22000,
    "emergency_support": 14000,
    "office_move_light": 32000,
}

SERVICE_LABELS = {
    "storefront_cleaning": "店舗定期清掃ライト",
    "fixture_install": "什器・設備の搬入設置",
    "event_support": "イベント設営サポート",
    "emergency_support": "緊急トラブル一次対応",
    "office_move_light": "小規模オフィス移転ヘルプ",
}

OPTION_DEFINITIONS = [
    {
        "field": "options_photoreport",
        "label": "作業写真レポート（写真追加）",
        "description": "Before / After の写真と簡単な作業ログをメールで共有します。",
        "price": 1000,
    },
    {
        "field": "options_priority_visit",
        "label": "至急訪問（48時間以内）",
        "description": "最優先枠でスケジューリングし、48時間以内にスタッフが伺います。",
        "price": 4000,
    },
    {
        "field": "options_weekend_visit",
        "label": "土日祝日の訪問確約",
        "description": "土日祝の作業枠を事前確保し、追加料金なしで対応します。",
        "price": 2500,
    },
    {
        "field": "options_extra_staff",
        "label": "追加スタッフ帯同（2名体制）",
        "description": "大型什器や重量物の搬入時に、補助スタッフを1名追加します。",
        "price": 6000,
    },
]

OPTION_FIELDS = [item['field'] for item in OPTION_DEFINITIONS]
OPTION_PRICE = {item['field']: item['price'] for item in OPTION_DEFINITIONS}


def ensure_booking_schema():
    with app.app_context():
        db.create_all()
        pragma_rows = db.session.execute(text("PRAGMA table_info(bookings)")).mappings()
        existing_columns = {row["name"] for row in pragma_rows}
        missing_fields = [field for field in OPTION_FIELDS if field not in existing_columns]
        for field in missing_fields:
            db.session.execute(
                text(f"ALTER TABLE bookings ADD COLUMN {field} BOOLEAN DEFAULT 0")
            )
        if missing_fields:
            db.session.commit()
            print("Added booking columns:", ", ".join(missing_fields))


ensure_booking_schema()

STEP_SEQUENCE = ["contact", "service", "options", "confirm"]
STEP_TEMPLATES = {
    "contact": "booking_steps/contact.html",
    "service": "booking_steps/service.html",
    "options": "booking_steps/options.html",
    "confirm": "booking_steps/confirm.html",
}
STEP_LABELS = {
    "contact": "連絡先",
    "service": "サービス詳細",
    "options": "オプション",
    "confirm": "確認",
}
STEP_FIELDS = {
    "contact": ["name", "phone", "email"],
    "service": ["service_type", "location", "preferred_date"],
    "options": list(OPTION_FIELDS) + ["message"],
    "confirm": [],
}


def _default_progress():
    data = {
        "name": "",
        "email": "",
        "phone": "",
        "service_type": "",
        "location": "",
        "preferred_date": "",
        "message": "",
    }
    for field in OPTION_FIELDS:
        data[field] = False
    return data


def _normalize_progress(raw):
    progress = _default_progress()
    if raw:
        progress.update(raw)
    for field in OPTION_FIELDS:
        progress[field] = bool(progress.get(field))
    progress["preferred_date"] = progress.get("preferred_date") or ""
    return progress


def get_booking_progress():
    return _normalize_progress(session.get("booking_progress"))


def store_booking_progress(progress):
    session["booking_progress"] = _normalize_progress(progress)
    session.modified = True


def clear_booking_progress():
    session.pop("booking_progress", None)
    session.pop("booking_current_step", None)


def get_current_step():
    step = session.get("booking_current_step")
    if step not in STEP_SEQUENCE:
        step = STEP_SEQUENCE[0]
    return step


def set_current_step(step):
    session["booking_current_step"] = step if step in STEP_SEQUENCE else STEP_SEQUENCE[0]
    session.modified = True


def get_adjacent_steps(step):
    if step not in STEP_SEQUENCE:
        return None, None
    idx = STEP_SEQUENCE.index(step)
    prev_step = STEP_SEQUENCE[idx - 1] if idx > 0 else None
    next_step = STEP_SEQUENCE[idx + 1] if idx + 1 < len(STEP_SEQUENCE) else None
    return prev_step, next_step


def render_step_template(step, data, errors=None):
    if step not in STEP_TEMPLATES:
        abort(404)
    payload = _normalize_progress(data)
    errors = errors or {}
    prev_step, next_step = get_adjacent_steps(step)
    option_flags = {field: payload.get(field, False) for field in OPTION_FIELDS}
    price = 0
    if payload["service_type"] in SERVICE_PRICE_TABLE:
        price = calc_estimate(
            payload["service_type"],
            option_flags,
        )
    return render_template(
        STEP_TEMPLATES[step],
        data=payload,
        errors=errors,
        price=price,
        option_catalog=OPTION_DEFINITIONS,
        service_price_table=SERVICE_PRICE_TABLE,
        service_labels=SERVICE_LABELS,
        step=step,
        prev_step=prev_step,
        next_step=next_step,
        step_sequence=STEP_SEQUENCE,
        step_labels=STEP_LABELS,
    )


def calc_estimate(service_type: str, options: dict) -> int:
    base = SERVICE_PRICE_TABLE.get(service_type, 0)
    extra = sum(OPTION_PRICE.get(field, 0) for field, enabled in options.items() if enabled)
    return base + extra

# ---------- Validation ----------
import re
from datetime import date

ALLOWED_SERVICE_TYPES = set(SERVICE_PRICE_TABLE.keys())
PHONE_RE = re.compile(r"^[0-9+\-() ]{9,16}$")

def validate_and_clean(form, *, partial=False, fields=None):
    """フォーム値を整形しつつ検証し、(cleaned_data, errors) を返す"""
    # 1) まず整形（stripや型変換）
    data = {
        "name": (form.get("name") or "").strip(),
        "email": (form.get("email") or "").strip(),
        "phone": (form.get("phone") or "").strip(),
        "service_type": form.get("service_type") or "",
        "location": (form.get("location") or "").strip(),
        "preferred_date": form.get("preferred_date") or "",
        "message": (form.get("message") or "").strip(),
    }
    for field in OPTION_FIELDS:
        raw_value = form.get(field)
        if isinstance(raw_value, bool):
            data[field] = raw_value
        else:
            data[field] = raw_value == "on"
    errors = {}

    fields_set = set(fields or data.keys())

    # 2) 必須
    for key in ["name", "phone", "service_type", "location"]:
        if not data[key] and (not partial or key in fields_set):
            errors[key] = "必須項目です"

    # 3) サービス種別
    if (
        data["service_type"]
        and data["service_type"] not in ALLOWED_SERVICE_TYPES
        and (not partial or "service_type" in fields_set)
    ):
        errors["service_type"] = "不正な選択です"

    # 4) 電話番号
    if (
        data["phone"]
        and not PHONE_RE.match(data["phone"])
        and (not partial or "phone" in fields_set)
    ):
        errors["phone"] = "数字・+ - () とスペースのみ、9〜16桁で入力してください"

    # 5) メール（任意）
    if (
        data["email"]
        and ("@" not in data["email"] or "." not in data["email"] or len(data["email"]) > 200)
        and (not partial or "email" in fields_set)
    ):
        errors["email"] = "メール形式が不正です"

    # 6) 文字数
    if len(data["name"]) > 120 and (not partial or "name" in fields_set):
        errors["name"] = "120文字以内で入力してください"
    if len(data["location"]) > 300 and (not partial or "location" in fields_set):
        errors["location"] = "300文字以内で入力してください"
    if len(data["message"]) > 500 and (not partial or "message" in fields_set):
        errors["message"] = "500文字以内で入力してください"

    # 7) 日付（任意）：過去不可、半年先まで
    if data["preferred_date"] and (not partial or "preferred_date" in fields_set):
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
    return dict(
        current_year=datetime.now().year,
        service_labels=SERVICE_LABELS,
        option_catalog=OPTION_DEFINITIONS,
    )


@app.route("/")
def index():
    progress = get_booking_progress()
    store_booking_progress(progress)
    current_step = get_current_step()
    set_current_step(current_step)
    step_html = render_step_template(current_step, progress)
    return render_template(
        "booking_form.html",
        current_step=current_step,
        step_sequence=STEP_SEQUENCE,
        step_labels=STEP_LABELS,
        initial_step_html=step_html,
    )

@app.route("/booking/step/<step>", methods=["GET", "POST"])
def booking_step(step):
    if step not in STEP_SEQUENCE:
        abort(404)
    progress = get_booking_progress()
    if request.method == "GET":
        set_current_step(step)
        return render_step_template(step, progress)

    incoming = request.form.to_dict(flat=True)
    validate_only = incoming.pop("_validate", "") == "1"

    if step == "options":
        for field in OPTION_FIELDS:
            incoming[field] = "on" if request.form.get(field) else ""

    combined = {**progress, **incoming}
    cleaned, errors = validate_and_clean(
        combined, partial=True, fields=STEP_FIELDS.get(step)
    )
    step_errors = {k: v for k, v in errors.items() if k in STEP_FIELDS.get(step, [])}

    store_booking_progress(cleaned)

    if step_errors:
        set_current_step(step)
        return render_step_template(step, cleaned, errors=step_errors)

    if validate_only:
        set_current_step(step)
        return render_step_template(step, cleaned)

    _, next_step = get_adjacent_steps(step)
    if next_step:
        set_current_step(next_step)
        return render_step_template(next_step, cleaned)

    set_current_step(step)
    return render_step_template(step, cleaned)


@app.route("/estimate", methods=["POST"])
def estimate():
    progress = get_booking_progress()
    incoming = request.form.to_dict(flat=True)
    if incoming:
        for field in OPTION_FIELDS:
            incoming[field] = "on" if request.form.get(field) else ""
    combined = {**progress, **incoming}
    cleaned, _ = validate_and_clean(
        combined,
        partial=True,
        fields=["service_type", *OPTION_FIELDS],
    )
    store_booking_progress(cleaned)
    service_type = cleaned.get("service_type")
    price = 0
    if service_type in SERVICE_PRICE_TABLE:
        price = calc_estimate(
            service_type, {field: cleaned.get(field, False) for field in OPTION_FIELDS}
        )
    return render_template("_estimate_fragment.html", price=price)

@app.route("/book", methods=["POST"])
def book():
    payload = request.form.to_dict(flat=True)
    for field in OPTION_FIELDS:
        payload.setdefault(field, "")
    data, errors = validate_and_clean(payload)
    if errors:
        store_booking_progress(data)
        set_current_step("confirm")
        html = render_step_template("confirm", data, errors)
        response = make_response(html, 400)
        return response

    # ここからは保存（サーバ側で価格を再計算）
    preferred_date = (
        datetime.strptime(data["preferred_date"], "%Y-%m-%d").date()
        if data["preferred_date"] else None
    )
    option_flags = {field: data.get(field, False) for field in OPTION_FIELDS}
    est_price = calc_estimate(data["service_type"], option_flags)

    b = Booking(
        name=data["name"],
        email=(data["email"] or None),
        phone=data["phone"],
        service_type=data["service_type"],
        location=data["location"],
        preferred_date=preferred_date,
        **{field: data.get(field, False) for field in OPTION_FIELDS},
        message=(data["message"] or None),
        est_price=est_price,
    )
    db.session.add(b)
    db.session.commit()
    clear_booking_progress()

    if request.headers.get("HX-Request"):
        response = make_response("", 204)
        response.headers["HX-Redirect"] = url_for("booking_thanks", booking_id=b.id)
        return response

    return redirect(url_for("booking_thanks", booking_id=b.id))


@app.route("/thanks/<int:booking_id>")
def booking_thanks(booking_id):
    booking = Booking.query.get_or_404(booking_id)
    return render_template("thanks.html", booking=booking)


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
    writer.writerow([
        "ID","作成日時","名前","電話","メール","サービス","住所/場所",
        "希望日","写真レポ","至急訪問","土日祝訪問","追加スタッフ","見積","ステータス","管理メモ"
    ])
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
            "Yes" if b.options_priority_visit else "No",
            "Yes" if b.options_weekend_visit else "No",
            "Yes" if b.options_extra_staff else "No",
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
