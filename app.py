"""
DivFund Tracker — แอปบันทึกกองทุนหุ้นปันผล (Flask single-file)

ฟีเจอร์หลัก
- บันทึกกองทุน: จำนวนหน่วย, ต้นทุนเฉลี่ย, NAV ปัจจุบัน, ปันผลต่อหน่วย/ปี, เดือนที่จ่าย
- คำนวณกำไร/ขาดทุน, ปันผลต่อเดือน (หลังหักภาษี 10%), Yield on Cost, ผลตอบแทนรวมปันผล
- วิเคราะห์อัตโนมัติ: สะสมเพิ่ม / ถือต่อ / ขายทำกำไรบางส่วน / เฝ้าระวัง / พิจารณาขาย
- บันทึกซื้อเพิ่ม/ขาย (คำนวณต้นทุนเฉลี่ยใหม่ + กำไรที่รับรู้แล้ว), บันทึกปันผลที่ได้รับ
- ปฏิทินปันผลคาดการณ์ 12 เดือน, อัปเดต NAV ทีละหลายกอง, วางแผนเป้าหมายปันผล, Export CSV

วิธีรัน
    pip install flask
    python app.py
แล้วเปิด http://127.0.0.1:5000
"""
import csv
import io
import os
import sqlite3
from datetime import date, datetime

from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, url_for)
from jinja2 import DictLoader

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "dividend_funds.db")
TAX_RATE = 0.10  # ภาษีหัก ณ ที่จ่ายเงินปันผลกองทุน 10%

TH_MONTHS = ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
             "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]
CATEGORIES = ["หุ้นไทยปันผล", "หุ้นต่างประเทศปันผล", "REITs/อสังหา",
              "โครงสร้างพื้นฐาน", "ผสม/Income", "หุ้นเติบโต", "อื่นๆ"]

REC_STYLES = {
    "ACCUMULATE": dict(label="สะสมเพิ่ม", icon="⬆", cls="bg-emerald-50 text-emerald-700 ring-emerald-600/20",
                       bar="bg-emerald-500",
                       summary="ปันผลแข็งแรงและพื้นฐานดี เหมาะกับการทยอยสะสมเพิ่ม (DCA) เพื่อเพิ่มกระแสเงินสด"),
    "HOLD": dict(label="ถือต่อ", icon="●", cls="bg-sky-50 text-sky-700 ring-sky-600/20",
                 bar="bg-sky-500",
                 summary="ยังสร้างกระแสเงินสดได้ดี ถือรับปันผลต่อ และติดตาม NAV กับนโยบายปันผลเป็นระยะ"),
    "TAKE_PROFIT": dict(label="ขายทำกำไรบางส่วน", icon="◎", cls="bg-violet-50 text-violet-700 ring-violet-600/20",
                        bar="bg-violet-500",
                        summary="ราคาขึ้นมามากแต่อัตราปันผลปัจจุบันต่ำลง พิจารณาขายทำกำไรบางส่วนแล้วย้ายไปกองที่ยีลด์สูงกว่า"),
    "WATCH": dict(label="เฝ้าระวัง", icon="⚠", cls="bg-amber-50 text-amber-700 ring-amber-600/20",
                  bar="bg-amber-500",
                  summary="มีสัญญาณที่ต้องจับตา ควรทบทวนนโยบายปันผล ผลการดำเนินงาน และค่าธรรมเนียม"),
    "SELL": dict(label="พิจารณาขาย", icon="⬇", cls="bg-rose-50 text-rose-700 ring-rose-600/20",
                 bar="bg-rose-500",
                 summary="ผลตอบแทนและปันผลไม่คุ้มความเสี่ยง พิจารณาขายหรือสับเปลี่ยนไปกองทุนที่ดีกว่า"),
}

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-divfund-secret-key")


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS funds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    amc TEXT DEFAULT '',
    category TEXT DEFAULT '',
    risk_level INTEGER DEFAULT 6,
    units REAL NOT NULL DEFAULT 0,
    avg_cost REAL NOT NULL DEFAULT 0,
    current_nav REAL NOT NULL DEFAULT 0,
    annual_div_per_unit REAL NOT NULL DEFAULT 0,
    pay_months TEXT DEFAULT '',
    expense_ratio REAL DEFAULT 0,
    notes TEXT DEFAULT '',
    nav_updated TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS dividends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_id INTEGER NOT NULL REFERENCES funds(id) ON DELETE CASCADE,
    pay_date TEXT NOT NULL,
    per_unit REAL NOT NULL,
    units REAL NOT NULL,
    gross REAL NOT NULL,
    tax REAL NOT NULL,
    net REAL NOT NULL,
    note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_id INTEGER NOT NULL REFERENCES funds(id) ON DELETE CASCADE,
    tx_date TEXT NOT NULL,
    tx_type TEXT NOT NULL,
    units REAL NOT NULL,
    nav REAL NOT NULL,
    amount REAL NOT NULL,
    realized_pl REAL DEFAULT 0
);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.executescript(SCHEMA)


def query(sql, args=(), one=False):
    rows = [dict(r) for r in get_db().execute(sql, args).fetchall()]
    return (rows[0] if rows else None) if one else rows


def execute(sql, args=()):
    db = get_db()
    cur = db.execute(sql, args)
    db.commit()
    return cur.lastrowid


def get_fund_or_404(fid):
    f = query("SELECT * FROM funds WHERE id = ?", (fid,), one=True)
    if f is None:
        abort(404)
    return f


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def fnum(name, default=0.0):
    raw = (request.form.get(name) or "").replace(",", "").strip()
    try:
        return float(raw)
    except ValueError:
        return default


def today_str():
    return date.today().isoformat()


def parse_months(text):
    months = []
    for part in (text or "").split(","):
        part = part.strip()
        if part.isdigit() and 1 <= int(part) <= 12:
            months.append(int(part))
    return sorted(set(months))


def month_label(y, m):
    return f"{TH_MONTHS[m - 1]} {str(y + 543)[-2:]}"


def last_n_months(n=12):
    t = date.today()
    y, m = t.year, t.month
    out = []
    for _ in range(n):
        out.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out[::-1]


def safe_back(default):
    nxt = request.form.get("next") or ""
    if nxt.startswith("/") and not nxt.startswith("//"):
        return redirect(nxt)
    return redirect(default)


@app.template_filter("money")
def money(v):
    return f"{(v or 0):,.2f}"


@app.template_filter("num0")
def num0(v):
    return f"{(v or 0):,.0f}"


@app.template_filter("units")
def units_fmt(v):
    return f"{(v or 0):,.4f}"


@app.template_filter("signed")
def signed(v):
    return f"{(v or 0):+,.2f}"


@app.template_filter("thdate")
def thdate(s):
    if isinstance(s, date):
        d = s
    else:
        try:
            d = datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
        except ValueError:
            return s or "-"
    return f"{d.day} {TH_MONTHS[d.month - 1]} {d.year + 543}"


@app.template_filter("months_th")
def months_th(text):
    ms = parse_months(text)
    return ", ".join(TH_MONTHS[m - 1] for m in ms) if ms else "ไม่ระบุ"


@app.context_processor
def inject_globals():
    return dict(today=date.today(), TH_MONTHS=TH_MONTHS, TAX_RATE=TAX_RATE)


# ---------------------------------------------------------------------------
# Analysis engine
# ---------------------------------------------------------------------------
def analyze_fund(f, divs, total_value):
    units, avg, nav, dpu = f["units"], f["avg_cost"], f["current_nav"], f["annual_div_per_unit"]
    cost = units * avg
    value = units * nav
    pl = value - cost
    pl_pct = pl / cost * 100 if cost else 0.0
    annual_div = units * dpu
    monthly_net = annual_div * (1 - TAX_RATE) / 12
    yoc = dpu / avg * 100 if avg else 0.0
    cur_yield = dpu / nav * 100 if nav else 0.0
    received = sum(d["net"] for d in divs)
    total_ret = pl + received
    total_ret_pct = total_ret / cost * 100 if cost else 0.0
    weight = value / total_value * 100 if total_value else 0.0

    trend, trend_pct = None, 0.0
    if len(divs) >= 2:
        s = sorted(divs, key=lambda d: d["pay_date"])
        last, prev = s[-1]["per_unit"], s[-2]["per_unit"]
        if prev > 0:
            trend_pct = (last - prev) / prev * 100
            trend = "up" if trend_pct > 5 else "down" if trend_pct < -5 else "flat"

    breakeven = (-pl / monthly_net) if pl < 0 and monthly_net > 0 else None

    reasons = []

    def add(pts, text):
        reasons.append(dict(pts=pts, text=text))

    # 1) อัตราปันผล
    if cur_yield >= 6:
        add(15, f"อัตราปันผลปัจจุบันสูง {cur_yield:.2f}% ต่อปี")
    elif cur_yield >= 4:
        add(8, f"อัตราปันผลอยู่ในเกณฑ์ดี {cur_yield:.2f}% ต่อปี")
    elif cur_yield >= 2:
        add(-3, f"อัตราปันผลค่อนข้างต่ำ {cur_yield:.2f}% ต่อปี")
    elif dpu > 0:
        add(-12, f"อัตราปันผลต่ำมาก {cur_yield:.2f}% ต่อปี ไม่เหมาะกับเป้าหมายกระแสเงินสด")
    else:
        add(-20, "ไม่มีข้อมูลปันผล / กองทุนไม่จ่ายปันผล")

    # 2) กำไร/ขาดทุน
    if pl_pct >= 30:
        add(5, f"กำไรสะสมสูง {pl_pct:.1f}% จากต้นทุน")
    elif pl_pct >= 0:
        add(5, f"อยู่ในแดนบวก {pl_pct:+.1f}%")
    elif pl_pct >= -15:
        add(-3, f"ขาดทุนเล็กน้อย {pl_pct:.1f}%")
    else:
        add(-10, f"ขาดทุนค่อนข้างลึก {pl_pct:.1f}%")

    if pl < 0 and total_ret >= 0 and received > 0:
        add(8, "เมื่อรวมปันผลที่ได้รับแล้ว ผลตอบแทนรวมยังเป็นบวก")

    # 3) แนวโน้มปันผล
    if trend == "up":
        add(10, f"ปันผลงวดล่าสุดเพิ่มขึ้น {trend_pct:+.1f}%")
    elif trend == "down":
        add(-15, f"ปันผลงวดล่าสุดลดลง {trend_pct:.1f}% ควรตรวจสอบสาเหตุ")
    elif trend == "flat":
        add(3, "จ่ายปันผลสม่ำเสมอ")

    # 4) ค่าธรรมเนียม
    er = f["expense_ratio"] or 0
    if er > 1.5:
        add(-8, f"ค่าธรรมเนียมรวมสูง {er:.2f}% ต่อปี กัดกินผลตอบแทน")
    elif 0 < er <= 0.8:
        add(4, f"ค่าธรรมเนียมต่ำ {er:.2f}% ต่อปี")

    # 5) การกระจุกตัว
    if weight > 40:
        add(-10, f"สัดส่วนในพอร์ตสูง {weight:.1f}% เสี่ยงกระจุกตัว")
    elif weight > 25:
        add(-4, f"สัดส่วนในพอร์ตค่อนข้างสูง {weight:.1f}%")

    # 6) ระยะเวลาคืนทุนด้วยปันผล
    if breakeven is not None:
        if breakeven > 60:
            add(-8, f"ต้องใช้ปันผลประมาณ {breakeven / 12:.1f} ปีจึงชดเชยส่วนที่ขาดทุนได้")
        else:
            add(2, f"ปันผลชดเชยส่วนที่ขาดทุนได้ในราว {breakeven:.0f} เดือน")

    if avg and yoc > cur_yield + 1:
        add(3, f"Yield on Cost {yoc:.2f}% สูงกว่ายีลด์ตลาด (ซื้อไว้ในราคาดี)")

    score = max(0, min(100, 50 + sum(r["pts"] for r in reasons)))
    if pl_pct >= 30 and cur_yield < 4:
        key = "TAKE_PROFIT"
    elif score >= 72:
        key = "ACCUMULATE"
    elif score >= 52:
        key = "HOLD"
    elif score >= 38:
        key = "WATCH"
    else:
        key = "SELL"

    return dict(cost=cost, value=value, pl=pl, pl_pct=pl_pct, annual_div=annual_div,
                monthly_net=monthly_net, yoc=yoc, cur_yield=cur_yield, received=received,
                total_ret=total_ret, total_ret_pct=total_ret_pct, weight=weight,
                trend=trend, trend_pct=trend_pct, breakeven=breakeven, score=score,
                reasons=sorted(reasons, key=lambda r: -r["pts"]),
                rec=dict(key=key, **REC_STYLES[key]), div_count=len(divs))


def load_portfolio():
    funds = query("SELECT * FROM funds ORDER BY code")
    divs = query("SELECT * FROM dividends ORDER BY pay_date")
    by_fund = {}
    for d in divs:
        by_fund.setdefault(d["fund_id"], []).append(d)
    total_value = sum(f["units"] * f["current_nav"] for f in funds)
    items = [{**f, **analyze_fund(f, by_fund.get(f["id"], []), total_value)} for f in funds]

    cost = sum(i["cost"] for i in items)
    value = sum(i["value"] for i in items)
    annual = sum(i["annual_div"] for i in items)
    received = sum(d["net"] for d in divs)
    year = str(date.today().year)
    realized = query("SELECT COALESCE(SUM(realized_pl), 0) AS v FROM transactions", one=True)["v"]
    health = sum(i["score"] * i["value"] for i in items) / value if value else 0
    s = dict(
        count=len(items), cost=cost, value=value, pl=value - cost,
        pl_pct=(value - cost) / cost * 100 if cost else 0,
        annual_gross=annual, annual_net=annual * (1 - TAX_RATE),
        monthly_net=annual * (1 - TAX_RATE) / 12,
        yoc=annual / cost * 100 if cost else 0,
        cur_yield=annual / value * 100 if value else 0,
        received=received, tax_paid=sum(d["tax"] for d in divs),
        received_ytd=sum(d["net"] for d in divs if d["pay_date"].startswith(year)),
        realized=realized,
        total_return=value - cost + received + realized,
        total_return_pct=(value - cost + received + realized) / cost * 100 if cost else 0,
        health=health,
        health_label="แข็งแรง" if health >= 70 else "ปานกลาง" if health >= 50 else "ควรปรับปรุง",
    )
    return items, divs, s


def upcoming_payouts(items):
    t = date.today()
    out = []
    for i in items:
        months = parse_months(i["pay_months"])
        if not months or i["annual_div"] <= 0:
            continue
        nxt = next((m for m in months if m >= t.month), None)
        year = t.year
        if nxt is None:
            nxt, year = months[0], t.year + 1
        out.append(dict(id=i["id"], code=i["code"], name=i["name"],
                        label=f"{TH_MONTHS[nxt - 1]} {year + 543}",
                        away=(year - t.year) * 12 + nxt - t.month,
                        amount=i["annual_div"] * (1 - TAX_RATE) / len(months)))
    return sorted(out, key=lambda x: x["away"])


def projected_calendar(items):
    """ปันผลสุทธิคาดการณ์รายเดือน (ม.ค.-ธ.ค.) แยกตามกองทุน"""
    series = []
    totals = [0.0] * 12
    for i in items:
        net = i["annual_div"] * (1 - TAX_RATE)
        if net <= 0:
            continue
        months = parse_months(i["pay_months"])
        row = [0.0] * 12
        if months:
            for m in months:
                row[m - 1] = net / len(months)
        else:
            row = [net / 12] * 12
        totals = [a + b for a, b in zip(totals, row)]
        series.append(dict(code=i["code"], data=[round(x, 2) for x in row]))
    return series, totals


def received_by_month(divs, n=12):
    months = last_n_months(n)
    recv = {k: 0.0 for k in months}
    for d in divs:
        k = (int(d["pay_date"][:4]), int(d["pay_date"][5:7]))
        if k in recv:
            recv[k] += d["net"]
    return [month_label(y, m) for y, m in months], [round(recv[k], 2) for k in months]


def portfolio_insights(items, s):
    tips = []
    if not items:
        return tips
    if s["count"] < 3:
        tips.append(("warn", f"มีเพียง {s['count']} กองทุน ควรกระจายอย่างน้อย 3–5 กองในหลายสินทรัพย์"))
    top = max(items, key=lambda i: i["weight"])
    if top["weight"] > 40:
        tips.append(("warn", f"{top['code']} มีสัดส่วนสูงถึง {top['weight']:.1f}% ของพอร์ต เสี่ยงกระจุกตัว"))
    cats = {i["category"] for i in items if i["category"]}
    if len(cats) == 1 and s["count"] > 1:
        tips.append(("warn", f"ทุกกองอยู่ในหมวด “{next(iter(cats))}” ลองกระจายไปหมวดอื่น เช่น REITs หรือหุ้นต่างประเทศ"))
    elif len(cats) >= 3:
        tips.append(("good", f"กระจายการลงทุน {len(cats)} หมวดสินทรัพย์ ช่วยลดความผันผวนได้ดี"))
    if s["cur_yield"] >= 5:
        tips.append(("good", f"อัตราปันผลเฉลี่ยของพอร์ต {s['cur_yield']:.2f}% ต่อปี อยู่ในเกณฑ์ดีมาก"))
    elif s["cur_yield"] < 3:
        tips.append(("warn", f"อัตราปันผลเฉลี่ยของพอร์ตเพียง {s['cur_yield']:.2f}% ต่อปี ยังต่ำสำหรับพอร์ตเน้นปันผล"))
    losers = [i for i in items if i["pl"] < 0]
    if losers:
        tips.append(("info", f"กองที่ยังขาดทุน {len(losers)} กอง: " + ", ".join(i["code"] for i in losers)))
    cuts = [i for i in items if i["trend"] == "down"]
    if cuts:
        tips.append(("warn", "ปันผลงวดล่าสุดลดลง: " + ", ".join(f"{i['code']} ({i['trend_pct']:.0f}%)" for i in cuts)))
    if s["annual_gross"] > 0:
        best = max(items, key=lambda i: i["annual_div"])
        share = best["annual_div"] / s["annual_gross"] * 100
        tips.append(("info", f"{best['code']} สร้างปันผลมากที่สุด คิดเป็น {share:.0f}% ของปันผลทั้งพอร์ต"))
    sells = [i for i in items if i["rec"]["key"] in ("SELL", "TAKE_PROFIT")]
    if sells:
        tips.append(("warn", "กองที่ควรทบทวนการขาย: " + ", ".join(f"{i['code']} ({i['rec']['label']})" for i in sells)))
    high_fee = [i for i in items if (i["expense_ratio"] or 0) > 1.5]
    if high_fee:
        tips.append(("info", "ค่าธรรมเนียมสูงกว่า 1.5%: " + ", ".join(i["code"] for i in high_fee)))
    return tips


# ---------------------------------------------------------------------------
# Routes: dashboard / funds
# ---------------------------------------------------------------------------
@app.route("/")
def dashboard():
    items, divs, s = load_portfolio()
    recv_labels, recv_values = received_by_month(divs)
    chart = dict(
        alloc_labels=[i["code"] for i in items],
        alloc_values=[round(i["value"], 2) for i in items],
        pl_labels=[i["code"] for i in items],
        pl_values=[round(i["pl_pct"], 2) for i in items],
        recv_labels=recv_labels, recv_values=recv_values,
    )
    return render_template("dashboard.html", items=items, s=s, chart=chart,
                           upcoming=upcoming_payouts(items)[:5],
                           insights=portfolio_insights(items, s)[:4])


@app.route("/funds")
def funds_list():
    items, _divs, s = load_portfolio()
    return render_template("funds.html", items=items, s=s, rec_styles=REC_STYLES)


def fund_form_data():
    return dict(
        code=(request.form.get("code") or "").strip().upper(),
        name=(request.form.get("name") or "").strip(),
        amc=(request.form.get("amc") or "").strip(),
        category=(request.form.get("category") or "").strip(),
        risk_level=int(fnum("risk_level", 6)),
        units=max(0.0, fnum("units")),
        avg_cost=max(0.0, fnum("avg_cost")),
        current_nav=max(0.0, fnum("current_nav")),
        annual_div_per_unit=max(0.0, fnum("annual_div_per_unit")),
        pay_months=",".join(str(m) for m in parse_months(",".join(request.form.getlist("pay_months")))),
        expense_ratio=max(0.0, fnum("expense_ratio")),
        notes=(request.form.get("notes") or "").strip(),
    )


@app.route("/funds/new", methods=["GET", "POST"])
def fund_new():
    if request.method == "POST":
        d = fund_form_data()
        if not d["code"] or not d["name"]:
            flash("กรุณากรอกรหัสและชื่อกองทุน", "error")
            return render_template("fund_form.html", f=d, categories=CATEGORIES, is_new=True)
        if d["current_nav"] <= 0:
            d["current_nav"] = d["avg_cost"]
        fid = execute(
            """INSERT INTO funds (code, name, amc, category, risk_level, units, avg_cost, current_nav,
               annual_div_per_unit, pay_months, expense_ratio, notes, nav_updated, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (d["code"], d["name"], d["amc"], d["category"], d["risk_level"], d["units"], d["avg_cost"],
             d["current_nav"], d["annual_div_per_unit"], d["pay_months"], d["expense_ratio"], d["notes"],
             today_str(), today_str()))
        if d["units"] > 0:
            execute("INSERT INTO transactions (fund_id, tx_date, tx_type, units, nav, amount) VALUES (?,?,?,?,?,?)",
                    (fid, today_str(), "BUY", d["units"], d["avg_cost"], d["units"] * d["avg_cost"]))
        flash(f"เพิ่มกองทุน {d['code']} เรียบร้อยแล้ว", "success")
        return redirect(url_for("fund_detail", fid=fid))
    blank = dict(code="", name="", amc="", category=CATEGORIES[0], risk_level=6, units="", avg_cost="",
                 current_nav="", annual_div_per_unit="", pay_months="", expense_ratio="", notes="")
    return render_template("fund_form.html", f=blank, categories=CATEGORIES, is_new=True)


@app.route("/funds/<int:fid>/edit", methods=["GET", "POST"])
def fund_edit(fid):
    f = get_fund_or_404(fid)
    if request.method == "POST":
        d = fund_form_data()
        if not d["code"] or not d["name"]:
            flash("กรุณากรอกรหัสและชื่อกองทุน", "error")
            return render_template("fund_form.html", f={**f, **d}, categories=CATEGORIES, is_new=False)
        nav_updated = today_str() if d["current_nav"] != f["current_nav"] else f["nav_updated"]
        execute(
            """UPDATE funds SET code=?, name=?, amc=?, category=?, risk_level=?, units=?, avg_cost=?,
               current_nav=?, annual_div_per_unit=?, pay_months=?, expense_ratio=?, notes=?, nav_updated=?
               WHERE id=?""",
            (d["code"], d["name"], d["amc"], d["category"], d["risk_level"], d["units"], d["avg_cost"],
             d["current_nav"], d["annual_div_per_unit"], d["pay_months"], d["expense_ratio"], d["notes"],
             nav_updated, fid))
        flash("บันทึกการแก้ไขเรียบร้อยแล้ว", "success")
        return redirect(url_for("fund_detail", fid=fid))
    return render_template("fund_form.html", f=f, categories=CATEGORIES, is_new=False)


@app.post("/funds/<int:fid>/delete")
def fund_delete(fid):
    f = get_fund_or_404(fid)
    execute("DELETE FROM funds WHERE id = ?", (fid,))
    flash(f"ลบกองทุน {f['code']} แล้ว", "success")
    return redirect(url_for("funds_list"))


@app.route("/funds/<int:fid>")
def fund_detail(fid):
    items, _divs, s = load_portfolio()
    f = next((i for i in items if i["id"] == fid), None)
    if f is None:
        abort(404)
    fdivs = query("SELECT * FROM dividends WHERE fund_id = ? ORDER BY pay_date DESC, id DESC", (fid,))
    txs = query("SELECT * FROM transactions WHERE fund_id = ? ORDER BY tx_date DESC, id DESC", (fid,))
    asc = list(reversed(fdivs))
    chart = dict(labels=[thdate(d["pay_date"]) for d in asc], values=[d["per_unit"] for d in asc])
    months = parse_months(f["pay_months"])
    per_payment = f["annual_div_per_unit"] / len(months) if months else f["annual_div_per_unit"]
    return render_template("fund_detail.html", f=f, s=s, divs=fdivs, txs=txs, chart=chart,
                           per_payment=per_payment)


@app.post("/funds/<int:fid>/nav")
def fund_nav(fid):
    get_fund_or_404(fid)
    nav = fnum("current_nav")
    if nav <= 0:
        flash("NAV ต้องมากกว่า 0", "error")
    else:
        execute("UPDATE funds SET current_nav = ?, nav_updated = ? WHERE id = ?",
                (nav, request.form.get("nav_date") or today_str(), fid))
        flash("อัปเดต NAV เรียบร้อยแล้ว", "success")
    return redirect(url_for("fund_detail", fid=fid))


@app.post("/funds/<int:fid>/trade")
def fund_trade(fid):
    f = get_fund_or_404(fid)
    tx_type = request.form.get("tx_type", "BUY")
    nav, units, amount = fnum("nav"), fnum("units"), fnum("amount")
    tx_date = request.form.get("tx_date") or today_str()
    if nav <= 0:
        flash("กรุณาระบุ NAV ต่อหน่วยที่มากกว่า 0", "error")
        return redirect(url_for("fund_detail", fid=fid))
    if units <= 0 and amount > 0:
        units = amount / nav
    if units <= 0:
        flash("กรุณาระบุจำนวนหน่วยหรือจำนวนเงิน", "error")
        return redirect(url_for("fund_detail", fid=fid))

    if tx_type == "BUY":
        new_units = f["units"] + units
        new_avg = (f["units"] * f["avg_cost"] + units * nav) / new_units
        execute("UPDATE funds SET units = ?, avg_cost = ? WHERE id = ?", (new_units, new_avg, fid))
        execute("INSERT INTO transactions (fund_id, tx_date, tx_type, units, nav, amount) VALUES (?,?,?,?,?,?)",
                (fid, tx_date, "BUY", units, nav, units * nav))
        flash(f"ซื้อเพิ่ม {units:,.4f} หน่วย ต้นทุนเฉลี่ยใหม่ {new_avg:,.4f} บาท/หน่วย", "success")
    else:
        if units > f["units"] + 1e-9:
            flash(f"จำนวนหน่วยที่ขายมากกว่าที่ถืออยู่ ({f['units']:,.4f} หน่วย)", "error")
            return redirect(url_for("fund_detail", fid=fid))
        realized = units * (nav - f["avg_cost"])
        remaining = f["units"] - units
        if remaining < 1e-6:
            remaining = 0.0
        execute("UPDATE funds SET units = ? WHERE id = ?", (remaining, fid))
        execute("""INSERT INTO transactions (fund_id, tx_date, tx_type, units, nav, amount, realized_pl)
                   VALUES (?,?,?,?,?,?,?)""", (fid, tx_date, "SELL", units, nav, units * nav, realized))
        flash(f"ขาย {units:,.4f} หน่วย {'กำไร' if realized >= 0 else 'ขาดทุน'} {abs(realized):,.2f} บาท", "success")

    if request.form.get("update_nav"):
        execute("UPDATE funds SET current_nav = ?, nav_updated = ? WHERE id = ?", (nav, tx_date, fid))
    return redirect(url_for("fund_detail", fid=fid))


# ---------------------------------------------------------------------------
# Routes: dividends
# ---------------------------------------------------------------------------
@app.route("/dividends")
def dividends():
    items, divs, s = load_portfolio()
    rows = query("""SELECT d.*, f.code, f.name FROM dividends d JOIN funds f ON f.id = d.fund_id
                    ORDER BY d.pay_date DESC, d.id DESC""")
    series, totals = projected_calendar(items)
    recv_labels, recv_values = received_by_month(divs)
    yearly = {}
    for d in divs:
        y = int(d["pay_date"][:4]) + 543
        yearly[y] = yearly.get(y, 0) + d["net"]
    chart = dict(cal_labels=TH_MONTHS, cal_series=series, recv_labels=recv_labels, recv_values=recv_values,
                 year_labels=[str(y) for y in sorted(yearly)], year_values=[round(yearly[y], 2) for y in sorted(yearly)])
    return render_template("dividends.html", items=items, s=s, rows=rows, chart=chart, totals=totals,
                           upcoming=upcoming_payouts(items))


@app.post("/dividends/add")
def dividend_add():
    fid = int(fnum("fund_id", 0))
    f = get_fund_or_404(fid)
    per_unit = fnum("per_unit")
    units = fnum("units") or f["units"]
    if per_unit <= 0 or units <= 0:
        flash("กรุณาระบุปันผลต่อหน่วยและจำนวนหน่วยให้ถูกต้อง", "error")
        return safe_back(url_for("fund_detail", fid=fid))
    gross = per_unit * units
    tax = gross * TAX_RATE if request.form.get("withhold") else 0.0
    execute("""INSERT INTO dividends (fund_id, pay_date, per_unit, units, gross, tax, net, note)
               VALUES (?,?,?,?,?,?,?,?)""",
            (fid, request.form.get("pay_date") or today_str(), per_unit, units, gross, tax, gross - tax,
             (request.form.get("note") or "").strip()))
    flash(f"บันทึกปันผล {f['code']} สุทธิ {gross - tax:,.2f} บาท", "success")
    return safe_back(url_for("fund_detail", fid=fid))


@app.post("/dividends/<int:did>/delete")
def dividend_delete(did):
    execute("DELETE FROM dividends WHERE id = ?", (did,))
    flash("ลบรายการปันผลแล้ว", "success")
    return safe_back(url_for("dividends"))


# ---------------------------------------------------------------------------
# Routes: analysis / NAV / planner / export / seed
# ---------------------------------------------------------------------------
@app.route("/analysis")
def analysis():
    items, _divs, s = load_portfolio()
    ranked = sorted(items, key=lambda i: -i["score"])
    counts = {k: sum(1 for i in items if i["rec"]["key"] == k) for k in REC_STYLES}
    return render_template("analysis.html", items=ranked, s=s, insights=portfolio_insights(items, s),
                           counts=counts, rec_styles=REC_STYLES)


@app.route("/nav", methods=["GET", "POST"])
def nav_update():
    if request.method == "POST":
        changed = 0
        nav_date = request.form.get("nav_date") or today_str()
        for f in query("SELECT id, current_nav FROM funds"):
            v = fnum(f"nav_{f['id']}")
            if v > 0 and abs(v - f["current_nav"]) > 1e-9:
                execute("UPDATE funds SET current_nav = ?, nav_updated = ? WHERE id = ?", (v, nav_date, f["id"]))
                changed += 1
        flash(f"อัปเดต NAV แล้ว {changed} กองทุน", "success")
        return redirect(url_for("dashboard"))
    items, _divs, s = load_portfolio()
    return render_template("nav.html", items=items, s=s)


@app.route("/planner")
def planner():
    _items, _divs, s = load_portfolio()
    return render_template("planner.html", s=s)


@app.route("/export.csv")
def export_csv():
    items, _divs, _s = load_portfolio()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["รหัส", "ชื่อกองทุน", "บลจ.", "หมวด", "จำนวนหน่วย", "ต้นทุนเฉลี่ย", "NAV", "ต้นทุนรวม",
                "มูลค่าปัจจุบัน", "กำไร/ขาดทุน", "%", "ปันผล/หน่วย/ปี", "ปันผลสุทธิ/เดือน", "Yield on Cost %",
                "Yield ปัจจุบัน %", "ปันผลรับแล้ว", "คะแนน", "คำแนะนำ"])
    for i in items:
        w.writerow([i["code"], i["name"], i["amc"], i["category"], round(i["units"], 4), round(i["avg_cost"], 4),
                    round(i["current_nav"], 4), round(i["cost"], 2), round(i["value"], 2), round(i["pl"], 2),
                    round(i["pl_pct"], 2), i["annual_div_per_unit"], round(i["monthly_net"], 2), round(i["yoc"], 2),
                    round(i["cur_yield"], 2), round(i["received"], 2), i["score"], i["rec"]["label"]])
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=divfund_{today_str()}.csv"})


@app.post("/seed")
def seed():
    if query("SELECT COUNT(*) AS c FROM funds", one=True)["c"]:
        flash("มีข้อมูลอยู่แล้ว จึงไม่โหลดข้อมูลตัวอย่าง", "error")
        return redirect(url_for("dashboard"))
    demo = [
        # code, name, amc, category, risk, units, avg, nav, dpu/yr, months, fee, dividend trend factor (old → new)
        ("THDIV-A", "กองทุนเปิดหุ้นไทยปันผล (ตัวอย่าง)", "บลจ.ตัวอย่าง A", "หุ้นไทยปันผล", 6,
         12000, 10.50, 11.90, 0.62, "4,10", 1.60, (1.0, 1.0)),
        ("GLBDIV-B", "กองทุนหุ้นปันผลทั่วโลก (ตัวอย่าง)", "บลจ.ตัวอย่าง B", "หุ้นต่างประเทศปันผล", 6,
         8000, 12.00, 13.40, 0.58, "3,6,9,12", 1.20, (0.92, 1.0)),
        ("REIT-C", "กองทุนอสังหาฯ และ REITs (ตัวอย่าง)", "บลจ.ตัวอย่าง C", "REITs/อสังหา", 8,
         15000, 9.80, 8.10, 0.52, "2,5,8,11", 1.00, (1.30, 1.0)),
        ("INFRA-D", "กองทุนโครงสร้างพื้นฐานปันผล (ตัวอย่าง)", "บลจ.ตัวอย่าง D", "โครงสร้างพื้นฐาน", 7,
         10000, 10.00, 10.20, 0.68, "1,4,7,10", 0.75, (0.85, 1.0)),
        ("GROWTH-E", "กองทุนหุ้นเติบโตจ่ายปันผลน้อย (ตัวอย่าง)", "บลจ.ตัวอย่าง E", "หุ้นเติบโต", 6,
         6000, 10.00, 14.20, 0.10, "12", 1.80, (1.0, 1.0)),
    ]
    t = date.today()
    start = date(t.year - 2, t.month, 1).isoformat()
    for code, name, amc, cat, risk, units, avg, nav, dpu, months, fee, (f_old, f_new) in demo:
        fid = execute(
            """INSERT INTO funds (code, name, amc, category, risk_level, units, avg_cost, current_nav,
               annual_div_per_unit, pay_months, expense_ratio, notes, nav_updated, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (code, name, amc, cat, risk, units, avg, nav, dpu, months, fee, "ข้อมูลตัวอย่างสำหรับทดลองใช้งาน",
             today_str(), start))
        execute("INSERT INTO transactions (fund_id, tx_date, tx_type, units, nav, amount) VALUES (?,?,?,?,?,?)",
                (fid, start, "BUY", units, avg, units * avg))
        ms = parse_months(months)
        payouts = [(y, m) for y, m in last_n_months(24) if m in ms and date(y, m, 15) <= t]
        for idx, (y, m) in enumerate(payouts):
            factor = f_new if idx == len(payouts) - 1 else f_old  # งวดล่าสุดเปลี่ยนแปลงเพื่อโชว์แนวโน้ม
            per_unit = round(dpu / len(ms) * factor, 4)
            gross = per_unit * units
            execute("""INSERT INTO dividends (fund_id, pay_date, per_unit, units, gross, tax, net, note)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (fid, date(y, m, 15).isoformat(), per_unit, units, gross, gross * TAX_RATE,
                     gross * (1 - TAX_RATE), "ตัวอย่าง"))
    flash("โหลดข้อมูลตัวอย่าง 5 กองทุนเรียบร้อยแล้ว", "success")
    return redirect(url_for("dashboard"))


@app.post("/reset")
def reset():
    execute("DELETE FROM funds")
    flash("ล้างข้อมูลทั้งหมดแล้ว", "success")
    return redirect(url_for("dashboard"))


@app.errorhandler(404)
def not_found(_e):
    return render_template("404.html"), 404


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
TEMPLATES = {}

TEMPLATES["macros.html"] = r"""
{% macro icon(name, cls='w-5 h-5') -%}
{% set paths = {
  'home': 'M2.25 12l8.954-8.955c.44-.439 1.152-.439 1.591 0L21.75 12M4.5 9.75v10.125c0 .621.504 1.125 1.125 1.125H9.75v-4.875c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125V21h4.125c.621 0 1.125-.504 1.125-1.125V9.75M8.25 21h8.25',
  'folder': 'M2.25 12.75V12A2.25 2.25 0 014.5 9.75h15A2.25 2.25 0 0121.75 12v.75m-8.69-6.44l-2.12-2.12a1.5 1.5 0 00-1.061-.44H4.5A2.25 2.25 0 002.25 6v12a2.25 2.25 0 002.25 2.25h15A2.25 2.25 0 0021.75 18V9a2.25 2.25 0 00-2.25-2.25h-5.379a1.5 1.5 0 01-1.06-.44z',
  'chart': 'M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 013 19.875v-6.75zM9.75 8.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125v11.25c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V8.625zM16.5 4.125c0-.621.504-1.125 1.125-1.125h2.25C20.496 3 21 3.504 21 4.125v15.75c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V4.125z',
  'coin': 'M12 6v12m-3-2.818l.879.659c1.171.879 3.07.879 4.242 0 1.172-.879 1.172-2.303 0-3.182C13.536 12.219 12.768 12 12 12c-.725 0-1.45-.22-2.003-.659-1.106-.879-1.106-2.303 0-3.182s2.9-.879 4.006 0l.415.33M21 12a9 9 0 11-18 0 9 9 0 0118 0z',
  'refresh': 'M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182m0-4.991v4.99',
  'target': 'M3 3v1.5M3 21v-6m0 0l2.77-.693a9 9 0 016.208.682l.108.054a9 9 0 006.086.71l3.114-.732a48.524 48.524 0 01-.005-10.499l-3.11.732a9 9 0 01-6.085-.711l-.108-.054a9 9 0 00-6.208-.682L3 4.5M3 15V4.5',
  'plus': 'M12 4.5v15m7.5-7.5h-15',
  'download': 'M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3',
  'pencil': 'M16.862 4.487l1.687-1.688a1.875 1.875 0 112.652 2.652L10.582 16.07a4.5 4.5 0 01-1.897 1.13L6 18l.8-2.685a4.5 4.5 0 011.13-1.897l8.932-8.931zm0 0L19.5 7.125M18 14v4.75A2.25 2.25 0 0115.75 21H5.25A2.25 2.25 0 013 18.75V8.25A2.25 2.25 0 015.25 6H10',
  'trash': 'M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0',
  'calendar': 'M6.75 3v2.25M17.25 3v2.25M3 18.75V7.5a2.25 2.25 0 012.25-2.25h13.5A2.25 2.25 0 0121 7.5v11.25m-18 0A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75m-18 0v-7.5A2.25 2.25 0 015.25 9h13.5A2.25 2.25 0 0121 11.25v7.5',
  'spark': 'M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z',
} %}
<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.6" stroke="currentColor" class="{{ cls }}"><path stroke-linecap="round" stroke-linejoin="round" d="{{ paths[name] }}"/></svg>
{%- endmacro %}

{% macro rec_badge(rec, big=false) -%}
<span class="inline-flex items-center gap-1.5 rounded-full ring-1 ring-inset font-semibold whitespace-nowrap {{ rec.cls }} {{ 'px-4 py-1.5 text-sm' if big else 'px-2.5 py-0.5 text-xs' }}">{{ rec.icon }} {{ rec.label }}</span>
{%- endmacro %}

{% macro plc(v) -%}{{ 'text-emerald-600' if v >= 0 else 'text-rose-600' }}{%- endmacro %}

{% macro kpi(title, value, sub='', tone='indigo', ic='chart') -%}
<div class="card p-5 flex items-start gap-4">
  <div class="shrink-0 rounded-xl p-2.5 bg-{{ tone }}-50 text-{{ tone }}-600">{{ icon(ic) }}</div>
  <div class="min-w-0">
    <p class="text-sm text-slate-500">{{ title }}</p>
    <p class="text-xl font-bold text-slate-900 num truncate">{{ value }}</p>
    {% if sub %}<p class="text-xs text-slate-400 mt-0.5">{{ sub|safe }}</p>{% endif %}
  </div>
</div>
{%- endmacro %}

{% macro risk_badge(level) -%}
{% set c = 'emerald' if level <= 4 else 'amber' if level <= 6 else 'rose' %}
<span class="chip bg-{{ c }}-50 text-{{ c }}-700">ความเสี่ยง {{ level }}</span>
{%- endmacro %}
"""

TEMPLATES["base.html"] = r"""<!doctype html>
<html lang="th">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}DivFund{% endblock %} · DivFund Tracker</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Thai:wght@300;400;500;600;700&display=swap" rel="stylesheet">
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = { theme: { extend: { fontFamily: { sans: ['"IBM Plex Sans Thai"', 'ui-sans-serif', 'system-ui', 'sans-serif'] } } } };
  </script>
  <style type="text/tailwindcss">
    @layer components {
      .card { @apply bg-white rounded-2xl border border-slate-200/70 shadow-sm; }
      .btn { @apply inline-flex items-center justify-center gap-2 rounded-xl px-4 py-2 text-sm font-semibold transition focus:outline-none focus:ring-2 focus:ring-offset-2; }
      .btn-primary { @apply btn bg-indigo-600 text-white hover:bg-indigo-500 focus:ring-indigo-500 shadow-sm shadow-indigo-600/20; }
      .btn-secondary { @apply btn bg-white text-slate-700 ring-1 ring-slate-200 hover:bg-slate-50 focus:ring-slate-300; }
      .btn-success { @apply btn bg-emerald-600 text-white hover:bg-emerald-500 focus:ring-emerald-500; }
      .btn-danger { @apply btn bg-rose-50 text-rose-700 ring-1 ring-rose-200 hover:bg-rose-100 focus:ring-rose-300; }
      .label { @apply block text-sm font-medium text-slate-700 mb-1; }
      .input { @apply block w-full rounded-xl border-0 bg-white px-3 py-2 text-sm text-slate-900 ring-1 ring-inset ring-slate-300 placeholder:text-slate-400 focus:ring-2 focus:ring-indigo-500; }
      .hint { @apply text-xs text-slate-400 mt-1; }
      .chip { @apply inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium; }
      .th { @apply px-4 py-3 text-left text-xs font-semibold uppercase tracking-wide text-slate-500 whitespace-nowrap; }
      .td { @apply px-4 py-3 text-sm whitespace-nowrap; }
      .num { font-variant-numeric: tabular-nums; }
      .section-title { @apply text-base font-semibold text-slate-900; }
    }
  </style>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.js"></script>
  <script>
    Chart.defaults.font.family = '"IBM Plex Sans Thai", sans-serif';
    Chart.defaults.color = '#64748b';
    Chart.defaults.plugins.legend.labels.usePointStyle = true;
    const PALETTE = ['#6366f1','#10b981','#f59e0b','#ec4899','#06b6d4','#8b5cf6','#84cc16','#f97316','#14b8a6','#ef4444'];
    const fmt = (v, d=2) => Number(v).toLocaleString('th-TH', {minimumFractionDigits: d, maximumFractionDigits: d});
  </script>
</head>
<body class="bg-slate-50 text-slate-700 font-sans antialiased">
{% from "macros.html" import icon %}
{% set nav = [
  ('dashboard', 'แดชบอร์ด', 'home'),
  ('funds_list', 'กองทุนของฉัน', 'folder'),
  ('analysis', 'วิเคราะห์ & คำแนะนำ', 'spark'),
  ('dividends', 'ปันผล & ปฏิทิน', 'calendar'),
  ('nav_update', 'อัปเดต NAV', 'refresh'),
  ('planner', 'วางแผนเป้าหมาย', 'target'),
] %}
{% set active = request.endpoint %}
<div class="min-h-screen lg:flex">
  <aside class="hidden lg:flex lg:flex-col w-64 shrink-0 h-screen sticky top-0 bg-gradient-to-b from-slate-900 via-slate-900 to-indigo-950 text-slate-300">
    <a href="{{ url_for('dashboard') }}" class="flex items-center gap-3 px-6 py-6">
      <div class="h-10 w-10 rounded-xl bg-gradient-to-br from-emerald-400 to-teal-500 grid place-items-center text-white font-bold shadow-lg shadow-emerald-500/30">฿</div>
      <div>
        <p class="text-white font-bold leading-tight">DivFund</p>
        <p class="text-xs text-slate-400">บันทึกกองทุนปันผล</p>
      </div>
    </a>
    <nav class="px-3 space-y-1 flex-1">
      {% for ep, label, ic in nav %}
      {% set on = active == ep or (ep == 'funds_list' and active in ['fund_detail','fund_edit','fund_new']) %}
      <a href="{{ url_for(ep) }}" class="flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition {{ 'bg-white/10 text-white' if on else 'hover:bg-white/5 hover:text-white' }}">
        {{ icon(ic) }} {{ label }}
      </a>
      {% endfor %}
      <div class="pt-4">
        <a href="{{ url_for('fund_new') }}" class="flex items-center justify-center gap-2 rounded-xl px-3 py-2.5 text-sm font-semibold bg-emerald-500 text-white hover:bg-emerald-400 transition">{{ icon('plus') }} เพิ่มกองทุน</a>
      </div>
    </nav>
    <div class="px-6 py-5 text-xs text-slate-500 border-t border-white/5">
      <a href="{{ url_for('export_csv') }}" class="flex items-center gap-2 hover:text-slate-300 mb-2">{{ icon('download', 'w-4 h-4') }} Export CSV</a>
      <form method="post" action="{{ url_for('reset') }}" onsubmit="return confirm('ล้างข้อมูลกองทุน ปันผล และรายการซื้อขายทั้งหมด?')" class="mb-3">
        <button class="flex items-center gap-2 hover:text-rose-300">{{ icon('trash', 'w-4 h-4') }} ล้างข้อมูลทั้งหมด</button>
      </form>
      ผลวิเคราะห์เป็นเกณฑ์อัตโนมัติ<br>ไม่ใช่คำแนะนำการลงทุน
    </div>
  </aside>

  <div class="flex-1 min-w-0">
    <header class="lg:hidden sticky top-0 z-20 bg-slate-900 text-white">
      <div class="flex items-center justify-between px-4 py-3">
        <a href="{{ url_for('dashboard') }}" class="flex items-center gap-2 font-bold">
          <span class="h-8 w-8 rounded-lg bg-gradient-to-br from-emerald-400 to-teal-500 grid place-items-center">฿</span> DivFund
        </a>
        <a href="{{ url_for('fund_new') }}" class="rounded-lg bg-emerald-500 px-3 py-1.5 text-sm font-semibold">+ เพิ่ม</a>
      </div>
      <nav class="flex gap-1 overflow-x-auto px-3 pb-3 text-sm">
        {% for ep, label, ic in nav %}
        <a href="{{ url_for(ep) }}" class="shrink-0 rounded-lg px-3 py-1.5 {{ 'bg-white/15 text-white' if active == ep else 'text-slate-300' }}">{{ label }}</a>
        {% endfor %}
      </nav>
    </header>

    <main class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6 lg:py-8">
      {% with msgs = get_flashed_messages(with_categories=true) %}
        {% for cat, m in msgs %}
        <div class="mb-4 flex items-start gap-3 rounded-xl px-4 py-3 text-sm ring-1 {{ 'bg-rose-50 text-rose-800 ring-rose-200' if cat == 'error' else 'bg-emerald-50 text-emerald-800 ring-emerald-200' }}" data-flash>
          <span class="font-bold">{{ '✕' if cat == 'error' else '✓' }}</span><span class="flex-1">{{ m }}</span>
          <button onclick="this.parentElement.remove()" class="opacity-60 hover:opacity-100">×</button>
        </div>
        {% endfor %}
      {% endwith %}
      {% block content %}{% endblock %}
      <footer class="mt-12 text-center text-xs text-slate-400">DivFund Tracker · ข้อมูลเก็บในเครื่องของคุณ (SQLite) · ภาษีปันผลคำนวณที่ {{ (TAX_RATE*100)|int }}%</footer>
    </main>
  </div>
</div>
<script>setTimeout(() => document.querySelectorAll('[data-flash]').forEach(e => e.remove()), 6000);</script>
{% block scripts %}{% endblock %}
</body>
</html>
"""

TEMPLATES["dashboard.html"] = r"""{% extends "base.html" %}
{% from "macros.html" import icon, rec_badge, plc, kpi %}
{% block title %}แดชบอร์ด{% endblock %}
{% block content %}
<div class="flex flex-col sm:flex-row sm:items-end sm:justify-between gap-4 mb-6">
  <div>
    <p class="text-sm text-slate-500">{{ today|thdate }}</p>
    <h1 class="text-2xl sm:text-3xl font-bold text-slate-900">ภาพรวมพอร์ตกองทุนปันผล</h1>
  </div>
  <div class="flex gap-2">
    <a href="{{ url_for('nav_update') }}" class="btn-secondary">{{ icon('refresh', 'w-4 h-4') }} อัปเดต NAV</a>
    <a href="{{ url_for('fund_new') }}" class="btn-primary">{{ icon('plus', 'w-4 h-4') }} เพิ่มกองทุน</a>
  </div>
</div>

{% if not items %}
<div class="card p-10 sm:p-16 text-center">
  <div class="mx-auto h-16 w-16 rounded-2xl bg-gradient-to-br from-emerald-400 to-teal-500 grid place-items-center text-3xl text-white font-bold shadow-lg shadow-emerald-500/30">฿</div>
  <h2 class="mt-6 text-xl font-bold text-slate-900">เริ่มบันทึกกองทุนปันผลของคุณ</h2>
  <p class="mt-2 text-slate-500 max-w-md mx-auto">เพิ่มกองทุนที่คุณถืออยู่ ระบบจะคำนวณกำไร/ขาดทุน ปันผลต่อเดือน และวิเคราะห์ว่าควรถือต่อหรือขายให้อัตโนมัติ</p>
  <div class="mt-6 flex flex-col sm:flex-row gap-3 justify-center">
    <a href="{{ url_for('fund_new') }}" class="btn-primary">{{ icon('plus', 'w-4 h-4') }} เพิ่มกองทุนแรก</a>
    <form method="post" action="{{ url_for('seed') }}"><button class="btn-secondary w-full">{{ icon('spark', 'w-4 h-4') }} ลองด้วยข้อมูลตัวอย่าง</button></form>
  </div>
</div>
{% else %}

<section class="grid lg:grid-cols-3 gap-4 mb-4">
  <div class="lg:col-span-2 relative overflow-hidden rounded-3xl p-6 sm:p-8 text-white bg-gradient-to-br from-indigo-950 via-indigo-800 to-violet-700 shadow-xl shadow-indigo-900/20">
    <div class="absolute -right-20 -top-20 h-72 w-72 rounded-full bg-white/5"></div>
    <div class="absolute right-24 -bottom-24 h-56 w-56 rounded-full bg-violet-400/10"></div>
    <div class="relative">
      <p class="text-indigo-200 text-sm">มูลค่าพอร์ตปัจจุบัน</p>
      <p class="mt-1 text-4xl sm:text-5xl font-bold num tracking-tight">฿{{ s.value|money }}</p>
      <div class="mt-3 inline-flex items-center gap-2 rounded-full px-3 py-1 text-sm font-semibold {{ 'bg-emerald-400/20 text-emerald-200' if s.pl >= 0 else 'bg-rose-400/20 text-rose-200' }}">
        {{ '▲' if s.pl >= 0 else '▼' }} {{ s.pl|signed }} บาท ({{ s.pl_pct|signed }}%)
      </div>
      <div class="mt-8 grid grid-cols-2 sm:grid-cols-4 gap-4 text-sm">
        <div><p class="text-indigo-200">ต้นทุนรวม</p><p class="text-lg font-semibold num">฿{{ s.cost|money }}</p></div>
        <div><p class="text-indigo-200">จำนวนกองทุน</p><p class="text-lg font-semibold">{{ s.count }} กอง</p></div>
        <div><p class="text-indigo-200">กำไรที่ขายแล้ว</p><p class="text-lg font-semibold num">{{ s.realized|signed }}</p></div>
        <div><p class="text-indigo-200">ผลตอบแทนรวมปันผล</p><p class="text-lg font-semibold num {{ 'text-emerald-300' if s.total_return >= 0 else 'text-rose-300' }}">{{ s.total_return_pct|signed }}%</p></div>
      </div>
    </div>
  </div>
  <div class="relative overflow-hidden rounded-3xl p-6 sm:p-8 text-white bg-gradient-to-br from-emerald-500 to-teal-600 shadow-xl shadow-emerald-900/10">
    <div class="absolute -right-10 -bottom-10 h-40 w-40 rounded-full bg-white/10"></div>
    <div class="relative">
      <p class="text-emerald-50 text-sm">ปันผลเฉลี่ยต่อเดือน (สุทธิ)</p>
      <p class="mt-1 text-4xl font-bold num tracking-tight">฿{{ s.monthly_net|money }}</p>
      <p class="text-sm text-emerald-50/90">≈ ฿{{ s.annual_net|money }} ต่อปี หลังหักภาษี {{ (TAX_RATE*100)|int }}%</p>
      <dl class="mt-6 space-y-2 text-sm">
        <div class="flex justify-between border-b border-white/15 pb-2"><dt class="text-emerald-50">Yield on Cost</dt><dd class="font-semibold num">{{ '%.2f'|format(s.yoc) }}%</dd></div>
        <div class="flex justify-between border-b border-white/15 pb-2"><dt class="text-emerald-50">Yield ปัจจุบัน</dt><dd class="font-semibold num">{{ '%.2f'|format(s.cur_yield) }}%</dd></div>
        <div class="flex justify-between"><dt class="text-emerald-50">รับแล้วปีนี้</dt><dd class="font-semibold num">฿{{ s.received_ytd|money }}</dd></div>
      </dl>
    </div>
  </div>
</section>

<section class="grid sm:grid-cols-2 xl:grid-cols-4 gap-4 mb-6">
  {{ kpi('ปันผลรับแล้วทั้งหมด (สุทธิ)', '฿' ~ (s.received|money), 'บันทึกจากรายการปันผลที่ได้รับ', 'emerald', 'coin') }}
  {{ kpi('ภาษีหัก ณ ที่จ่าย', '฿' ~ (s.tax_paid|money), 'ใช้เป็นเครดิตภาษีได้ถ้านำไปรวมยื่น', 'amber', 'calendar') }}
  {{ kpi('ผลตอบแทนรวม (รวมปันผล)', ('%+.2f'|format(s.total_return_pct)) ~ '%', '฿' ~ (s.total_return|signed), 'sky', 'chart') }}
  {{ kpi('คะแนนสุขภาพพอร์ต', ('%.0f'|format(s.health)) ~ ' / 100', s.health_label, 'violet', 'spark') }}
</section>

<section class="grid lg:grid-cols-3 gap-4 mb-6">
  <div class="card p-5 lg:col-span-2">
    <div class="flex items-center justify-between mb-4">
      <h2 class="section-title">ปันผลที่ได้รับ 12 เดือนล่าสุด</h2>
      <a href="{{ url_for('dividends') }}" class="text-sm text-indigo-600 hover:underline">ดูทั้งหมด →</a>
    </div>
    <div class="h-64"><canvas id="recvChart"></canvas></div>
  </div>
  <div class="card p-5">
    <h2 class="section-title mb-4">สัดส่วนพอร์ต</h2>
    <div class="h-64"><canvas id="allocChart"></canvas></div>
  </div>
</section>

<section class="grid lg:grid-cols-3 gap-4 mb-6">
  <div class="card p-5">
    <h2 class="section-title mb-4 flex items-center gap-2">{{ icon('calendar', 'w-5 h-5 text-emerald-600') }} ปันผลที่กำลังจะเข้า</h2>
    {% if upcoming %}
    <ul class="divide-y divide-slate-100">
      {% for u in upcoming %}
      <li class="py-3 flex items-center justify-between gap-3">
        <div class="min-w-0">
          <a href="{{ url_for('fund_detail', fid=u.id) }}" class="font-semibold text-slate-900 hover:text-indigo-600">{{ u.code }}</a>
          <p class="text-xs text-slate-500">{{ u.label }} · {{ 'เดือนนี้' if u.away == 0 else 'อีก ' ~ u.away ~ ' เดือน' }}</p>
        </div>
        <span class="font-semibold text-emerald-600 num">≈ ฿{{ u.amount|money }}</span>
      </li>
      {% endfor %}
    </ul>
    {% else %}<p class="text-sm text-slate-400">ระบุ “เดือนที่จ่ายปันผล” ในข้อมูลกองทุนเพื่อดูรายการนี้</p>{% endif %}
  </div>
  <div class="card p-5 lg:col-span-2">
    <h2 class="section-title mb-4">กำไร/ขาดทุนรายกองทุน (%)</h2>
    <div class="h-64"><canvas id="plChart"></canvas></div>
  </div>
</section>

{% if insights %}
<section class="card p-5 mb-6">
  <div class="flex items-center justify-between mb-3">
    <h2 class="section-title flex items-center gap-2">{{ icon('spark', 'w-5 h-5 text-violet-600') }} ข้อสังเกตของพอร์ต</h2>
    <a href="{{ url_for('analysis') }}" class="text-sm text-indigo-600 hover:underline">วิเคราะห์เต็ม →</a>
  </div>
  <div class="grid md:grid-cols-2 gap-3">
    {% for kind, text in insights %}
    <div class="flex gap-3 rounded-xl p-3 text-sm {{ {'good':'bg-emerald-50 text-emerald-800','warn':'bg-amber-50 text-amber-800','info':'bg-slate-50 text-slate-700'}[kind] }}">
      <span>{{ {'good':'✓','warn':'!','info':'i'}[kind] }}</span><span>{{ text }}</span>
    </div>
    {% endfor %}
  </div>
</section>
{% endif %}

<section class="card overflow-hidden">
  <div class="flex items-center justify-between p-5">
    <h2 class="section-title">กองทุนในพอร์ต</h2>
    <a href="{{ url_for('funds_list') }}" class="text-sm text-indigo-600 hover:underline">จัดการกองทุน →</a>
  </div>
  <div class="overflow-x-auto">
    <table class="min-w-full divide-y divide-slate-100">
      <thead class="bg-slate-50"><tr>
        <th class="th">กองทุน</th><th class="th text-right">ต้นทุน / NAV</th><th class="th text-right">มูลค่า</th>
        <th class="th text-right">กำไร/ขาดทุน</th><th class="th text-right">ปันผล/เดือน</th><th class="th text-right">Yield</th><th class="th">คำแนะนำ</th>
      </tr></thead>
      <tbody class="divide-y divide-slate-100">
        {% for i in items %}
        <tr class="hover:bg-slate-50/70 cursor-pointer" onclick="location.href='{{ url_for('fund_detail', fid=i.id) }}'">
          <td class="td"><p class="font-semibold text-slate-900">{{ i.code }}</p><p class="text-xs text-slate-500 max-w-[16rem] truncate">{{ i.name }}</p></td>
          <td class="td text-right num"><p>{{ '%.4f'|format(i.avg_cost) }}</p><p class="text-xs text-slate-500">{{ '%.4f'|format(i.current_nav) }}</p></td>
          <td class="td text-right num font-medium text-slate-900">฿{{ i.value|money }}<p class="text-xs text-slate-400">{{ '%.1f'|format(i.weight) }}% ของพอร์ต</p></td>
          <td class="td text-right num {{ plc(i.pl) }}"><p class="font-semibold">{{ i.pl|signed }}</p><p class="text-xs">{{ i.pl_pct|signed }}%</p></td>
          <td class="td text-right num font-semibold text-emerald-600">฿{{ i.monthly_net|money }}</td>
          <td class="td text-right num">{{ '%.2f'|format(i.cur_yield) }}%</td>
          <td class="td">{{ rec_badge(i.rec) }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</section>
{% endif %}
{% endblock %}

{% block scripts %}
{% if items %}
<script>
const C = {{ chart|tojson }};
new Chart(document.getElementById('recvChart'), {
  type: 'bar',
  data: { labels: C.recv_labels, datasets: [{ label: 'ปันผลสุทธิ (บาท)', data: C.recv_values, backgroundColor: '#10b981', borderRadius: 8, maxBarThickness: 36 }] },
  options: { maintainAspectRatio: false, plugins: { legend: { display: false }, tooltip: { callbacks: { label: c => ' ฿' + fmt(c.raw) } } },
             scales: { y: { beginAtZero: true, grid: { color: '#f1f5f9' }, ticks: { callback: v => '฿' + fmt(v, 0) } }, x: { grid: { display: false } } } }
});
new Chart(document.getElementById('allocChart'), {
  type: 'doughnut',
  data: { labels: C.alloc_labels, datasets: [{ data: C.alloc_values, backgroundColor: PALETTE, borderWidth: 3, borderColor: '#fff' }] },
  options: { maintainAspectRatio: false, cutout: '68%', plugins: { legend: { position: 'bottom' },
    tooltip: { callbacks: { label: c => { const t = c.dataset.data.reduce((a,b)=>a+b,0); return ` ฿${fmt(c.raw)} (${(c.raw/t*100).toFixed(1)}%)`; } } } } }
});
new Chart(document.getElementById('plChart'), {
  type: 'bar',
  data: { labels: C.pl_labels, datasets: [{ data: C.pl_values, borderRadius: 6, maxBarThickness: 28,
          backgroundColor: C.pl_values.map(v => v >= 0 ? '#10b981' : '#f43f5e') }] },
  options: { indexAxis: 'y', maintainAspectRatio: false, plugins: { legend: { display: false }, tooltip: { callbacks: { label: c => ` ${c.raw > 0 ? '+' : ''}${c.raw.toFixed(2)}%` } } },
             scales: { x: { grid: { color: '#f1f5f9' }, ticks: { callback: v => v + '%' } }, y: { grid: { display: false } } } }
});
</script>
{% endif %}
{% endblock %}
"""

TEMPLATES["funds.html"] = r"""{% extends "base.html" %}
{% from "macros.html" import icon, rec_badge, plc, risk_badge %}
{% block title %}กองทุนของฉัน{% endblock %}
{% block content %}
<div class="flex flex-col sm:flex-row sm:items-end sm:justify-between gap-4 mb-6">
  <div>
    <h1 class="text-2xl sm:text-3xl font-bold text-slate-900">กองทุนของฉัน</h1>
    <p class="text-slate-500 text-sm mt-1">{{ s.count }} กองทุน · มูลค่ารวม ฿{{ s.value|money }}</p>
  </div>
  <div class="flex gap-2">
    <a href="{{ url_for('export_csv') }}" class="btn-secondary">{{ icon('download', 'w-4 h-4') }} CSV</a>
    <a href="{{ url_for('fund_new') }}" class="btn-primary">{{ icon('plus', 'w-4 h-4') }} เพิ่มกองทุน</a>
  </div>
</div>

{% if items %}
<div class="card p-3 mb-5 flex flex-col md:flex-row gap-3">
  <input id="q" type="search" placeholder="ค้นหารหัส ชื่อ หรือ บลจ." class="input md:max-w-xs">
  <div class="flex flex-wrap gap-2" id="filters">
    <button data-f="" class="chip px-3 py-1.5 bg-slate-900 text-white">ทั้งหมด</button>
    {% for k, r in rec_styles.items() %}
    <button data-f="{{ k }}" class="chip px-3 py-1.5 ring-1 ring-inset {{ r.cls }}">{{ r.icon }} {{ r.label }}</button>
    {% endfor %}
  </div>
  <select id="sort" class="input md:max-w-[12rem] md:ml-auto">
    <option value="value">เรียงตามมูลค่า</option>
    <option value="pl">เรียงตาม % กำไร</option>
    <option value="yield">เรียงตาม Yield</option>
    <option value="div">เรียงตามปันผล/เดือน</option>
    <option value="score">เรียงตามคะแนน</option>
  </select>
</div>

<div id="grid" class="grid sm:grid-cols-2 xl:grid-cols-3 gap-4">
  {% for i in items %}
  <a href="{{ url_for('fund_detail', fid=i.id) }}" class="fund-card card p-5 hover:shadow-md hover:-translate-y-0.5 transition block"
     data-q="{{ (i.code ~ ' ' ~ i.name ~ ' ' ~ i.amc)|lower }}" data-rec="{{ i.rec.key }}"
     data-value="{{ i.value }}" data-pl="{{ i.pl_pct }}" data-yield="{{ i.cur_yield }}" data-div="{{ i.monthly_net }}" data-score="{{ i.score }}">
    <div class="flex items-start justify-between gap-3">
      <div class="min-w-0">
        <p class="text-lg font-bold text-slate-900">{{ i.code }}</p>
        <p class="text-sm text-slate-500 truncate">{{ i.name }}</p>
      </div>
      {{ rec_badge(i.rec) }}
    </div>
    <div class="mt-3 flex flex-wrap gap-1.5">
      {% if i.category %}<span class="chip bg-indigo-50 text-indigo-700">{{ i.category }}</span>{% endif %}
      {{ risk_badge(i.risk_level) }}
    </div>
    <div class="mt-4 grid grid-cols-2 gap-3 text-sm">
      <div><p class="text-slate-400 text-xs">มูลค่า</p><p class="font-semibold text-slate-900 num">฿{{ i.value|money }}</p></div>
      <div><p class="text-slate-400 text-xs">กำไร/ขาดทุน</p><p class="font-semibold num {{ plc(i.pl) }}">{{ i.pl|signed }} <span class="text-xs">({{ i.pl_pct|signed }}%)</span></p></div>
      <div><p class="text-slate-400 text-xs">ปันผล/เดือน (สุทธิ)</p><p class="font-semibold text-emerald-600 num">฿{{ i.monthly_net|money }}</p></div>
      <div><p class="text-slate-400 text-xs">Yield ปัจจุบัน</p><p class="font-semibold text-slate-900 num">{{ '%.2f'|format(i.cur_yield) }}%</p></div>
    </div>
    <div class="mt-4">
      <div class="flex justify-between text-xs text-slate-400 mb-1"><span>คะแนนวิเคราะห์</span><span>{{ i.score }}/100</span></div>
      <div class="h-1.5 rounded-full bg-slate-100 overflow-hidden"><div class="h-full rounded-full {{ i.rec.bar }}" style="width: {{ i.score }}%"></div></div>
    </div>
  </a>
  {% endfor %}
</div>
<p id="empty" class="hidden text-center text-slate-400 py-10">ไม่พบกองทุนที่ตรงกับเงื่อนไข</p>
{% else %}
<div class="card p-12 text-center">
  <p class="text-slate-500">ยังไม่มีกองทุน</p>
  <a href="{{ url_for('fund_new') }}" class="btn-primary mt-4">{{ icon('plus', 'w-4 h-4') }} เพิ่มกองทุนแรก</a>
</div>
{% endif %}
{% endblock %}

{% block scripts %}
<script>
(() => {
  const grid = document.getElementById('grid'); if (!grid) return;
  const cards = [...grid.querySelectorAll('.fund-card')];
  let rec = '';
  const apply = () => {
    const q = document.getElementById('q').value.trim().toLowerCase();
    const key = document.getElementById('sort').value;
    let shown = 0;
    cards.sort((a, b) => parseFloat(b.dataset[key]) - parseFloat(a.dataset[key])).forEach(c => {
      const ok = c.dataset.q.includes(q) && (!rec || c.dataset.rec === rec);
      c.style.display = ok ? '' : 'none'; if (ok) shown++;
      grid.appendChild(c);
    });
    document.getElementById('empty').classList.toggle('hidden', shown > 0);
  };
  document.getElementById('q').addEventListener('input', apply);
  document.getElementById('sort').addEventListener('change', apply);
  document.querySelectorAll('#filters button').forEach(b => b.addEventListener('click', () => {
    rec = b.dataset.f;
    document.querySelectorAll('#filters button').forEach(x => x.classList.toggle('outline', x === b));
    document.querySelectorAll('#filters button').forEach(x => x.classList.toggle('outline-2', x === b));
    apply();
  }));
  apply();
})();
</script>
{% endblock %}
"""

TEMPLATES["fund_form.html"] = r"""{% extends "base.html" %}
{% from "macros.html" import icon %}
{% block title %}{{ 'เพิ่มกองทุน' if is_new else 'แก้ไข ' ~ f.code }}{% endblock %}
{% block content %}
<div class="max-w-4xl mx-auto">
  <a href="{{ url_for('funds_list') if is_new else url_for('fund_detail', fid=f.id) }}" class="text-sm text-slate-500 hover:text-slate-800">← ย้อนกลับ</a>
  <h1 class="mt-2 text-2xl sm:text-3xl font-bold text-slate-900">{{ 'เพิ่มกองทุนใหม่' if is_new else 'แก้ไขกองทุน ' ~ f.code }}</h1>
  <p class="text-slate-500 text-sm mt-1">กรอกข้อมูลจากหนังสือชี้ชวน / Fund Fact Sheet และใบยืนยันการซื้อ</p>

  <form method="post" class="mt-6 space-y-5">
    <div class="card p-6">
      <h2 class="section-title mb-4">① ข้อมูลกองทุน</h2>
      <div class="grid sm:grid-cols-3 gap-4">
        <div><label class="label">รหัสกองทุน *</label><input name="code" value="{{ f.code }}" required class="input uppercase" placeholder="เช่น ABC-DIV"></div>
        <div class="sm:col-span-2"><label class="label">ชื่อกองทุน *</label><input name="name" value="{{ f.name }}" required class="input" placeholder="กองทุนเปิด..."></div>
        <div><label class="label">บลจ.</label><input name="amc" value="{{ f.amc or '' }}" class="input" placeholder="บริษัทจัดการ"></div>
        <div><label class="label">หมวดสินทรัพย์</label>
          <select name="category" class="input">{% for c in categories %}<option {{ 'selected' if c == f.category }}>{{ c }}</option>{% endfor %}</select></div>
        <div><label class="label">ระดับความเสี่ยง (1–8)</label>
          <select name="risk_level" class="input">{% for r in range(1, 9) %}<option value="{{ r }}" {{ 'selected' if r == (f.risk_level|int) }}>{{ r }}{{ ' – ต่ำ' if r <= 3 else ' – ปานกลาง' if r <= 5 else ' – สูง' if r <= 7 else ' – สูงมาก' }}</option>{% endfor %}</select></div>
      </div>
    </div>

    <div class="card p-6">
      <h2 class="section-title mb-1">② การถือครอง</h2>
      {% if not is_new %}<p class="text-xs text-amber-600 mb-4">แก้ไขจำนวนหน่วย/ต้นทุนตรงนี้จะไม่สร้างรายการซื้อขาย — หากซื้อเพิ่มหรือขาย แนะนำให้ใช้ฟอร์ม “ซื้อ/ขาย” ในหน้ากองทุน</p>{% else %}<p class="text-xs text-slate-400 mb-4">ระบบจะบันทึกเป็นรายการซื้อครั้งแรกให้อัตโนมัติ</p>{% endif %}
      <div class="grid sm:grid-cols-3 gap-4">
        <div><label class="label">จำนวนหน่วยที่ถือ</label><input name="units" id="units" value="{{ f.units }}" inputmode="decimal" class="input num" placeholder="0.0000"></div>
        <div><label class="label">ต้นทุนเฉลี่ย (บาท/หน่วย)</label><input name="avg_cost" id="avg_cost" value="{{ f.avg_cost }}" inputmode="decimal" class="input num" placeholder="10.0000"></div>
        <div><label class="label">NAV ปัจจุบัน (บาท/หน่วย)</label><input name="current_nav" id="current_nav" value="{{ f.current_nav }}" inputmode="decimal" class="input num" placeholder="เว้นว่าง = เท่าต้นทุน"></div>
      </div>
      <div id="preview" class="mt-4 grid grid-cols-3 gap-3 rounded-xl bg-slate-50 p-4 text-sm"></div>
    </div>

    <div class="card p-6">
      <h2 class="section-title mb-4">③ นโยบายปันผล</h2>
      <div class="grid sm:grid-cols-2 gap-4">
        <div><label class="label">ปันผลต่อหน่วยต่อปี (บาท)</label><input name="annual_div_per_unit" id="dpu" value="{{ f.annual_div_per_unit }}" inputmode="decimal" class="input num" placeholder="เช่น 0.60">
          <p class="hint">รวมทุกงวดใน 12 เดือนล่าสุด ดูได้จาก Fact Sheet</p></div>
        <div><label class="label">ค่าธรรมเนียมรวม (% ต่อปี)</label><input name="expense_ratio" value="{{ f.expense_ratio }}" inputmode="decimal" class="input num" placeholder="เช่น 1.25">
          <p class="hint">Total Expense Ratio (TER)</p></div>
      </div>
      <label class="label mt-4">เดือนที่จ่ายปันผล</label>
      {% set sel = (f.pay_months or '').split(',') %}
      <div class="grid grid-cols-4 sm:grid-cols-6 lg:grid-cols-12 gap-2">
        {% for m in TH_MONTHS %}
        <label class="cursor-pointer">
          <input type="checkbox" name="pay_months" value="{{ loop.index }}" class="peer sr-only" {{ 'checked' if loop.index|string in sel }}>
          <span class="block text-center rounded-lg py-2 text-sm ring-1 ring-slate-200 text-slate-600 peer-checked:bg-emerald-500 peer-checked:text-white peer-checked:ring-emerald-500 hover:bg-slate-50 transition">{{ m }}</span>
        </label>
        {% endfor %}
      </div>
      <p class="hint">ใช้สร้างปฏิทินปันผลและแจ้งงวดถัดไป</p>
    </div>

    <div class="card p-6">
      <h2 class="section-title mb-4">④ บันทึกเพิ่มเติม</h2>
      <textarea name="notes" rows="3" class="input" placeholder="เหตุผลที่ลงทุน, เป้าหมาย, จุดที่ต้องติดตาม...">{{ f.notes or '' }}</textarea>
    </div>

    <div class="flex justify-end gap-3">
      <a href="{{ url_for('funds_list') if is_new else url_for('fund_detail', fid=f.id) }}" class="btn-secondary">ยกเลิก</a>
      <button class="btn-primary px-6">{{ 'บันทึกกองทุน' if is_new else 'บันทึกการแก้ไข' }}</button>
    </div>
  </form>
</div>
{% endblock %}
{% block scripts %}
<script>
(() => {
  const n = id => parseFloat((document.getElementById(id).value || '').replace(/,/g, '')) || 0;
  const render = () => {
    const u = n('units'), a = n('avg_cost'), nav = n('current_nav') || a, d = n('dpu');
    const cost = u * a, val = u * nav, pl = val - cost, mo = u * d * 0.9 / 12;
    const cls = pl >= 0 ? 'text-emerald-600' : 'text-rose-600';
    document.getElementById('preview').innerHTML =
      `<div><p class="text-xs text-slate-400">ต้นทุนรวม</p><p class="font-semibold num">฿${fmt(cost)}</p></div>
       <div><p class="text-xs text-slate-400">กำไร/ขาดทุน</p><p class="font-semibold num ${cls}">${pl >= 0 ? '+' : ''}${fmt(pl)}</p></div>
       <div><p class="text-xs text-slate-400">ปันผล/เดือน (สุทธิ)</p><p class="font-semibold num text-emerald-600">฿${fmt(mo)}</p></div>`;
  };
  ['units', 'avg_cost', 'current_nav', 'dpu'].forEach(id => document.getElementById(id).addEventListener('input', render));
  render();
})();
</script>
{% endblock %}
"""

TEMPLATES["fund_detail.html"] = r"""{% extends "base.html" %}
{% from "macros.html" import icon, rec_badge, plc, risk_badge %}
{% block title %}{{ f.code }}{% endblock %}
{% block content %}
<a href="{{ url_for('funds_list') }}" class="text-sm text-slate-500 hover:text-slate-800">← กองทุนของฉัน</a>
<div class="mt-2 mb-6 flex flex-col md:flex-row md:items-start md:justify-between gap-4">
  <div>
    <div class="flex flex-wrap items-center gap-3">
      <h1 class="text-2xl sm:text-3xl font-bold text-slate-900">{{ f.code }}</h1>
      {{ rec_badge(f.rec, true) }}
    </div>
    <p class="text-slate-600 mt-1">{{ f.name }}</p>
    <div class="mt-2 flex flex-wrap gap-1.5">
      {% if f.amc %}<span class="chip bg-slate-100 text-slate-700">{{ f.amc }}</span>{% endif %}
      {% if f.category %}<span class="chip bg-indigo-50 text-indigo-700">{{ f.category }}</span>{% endif %}
      {{ risk_badge(f.risk_level) }}
      <span class="chip bg-emerald-50 text-emerald-700">จ่าย: {{ f.pay_months|months_th }}</span>
    </div>
  </div>
  <div class="flex gap-2">
    <a href="{{ url_for('fund_edit', fid=f.id) }}" class="btn-secondary">{{ icon('pencil', 'w-4 h-4') }} แก้ไข</a>
    <form method="post" action="{{ url_for('fund_delete', fid=f.id) }}" onsubmit="return confirm('ลบกองทุน {{ f.code }} และประวัติทั้งหมด?')">
      <button class="btn-danger">{{ icon('trash', 'w-4 h-4') }} ลบ</button>
    </form>
  </div>
</div>

<div class="grid lg:grid-cols-3 gap-5">
  <div class="lg:col-span-2 space-y-5">
    <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
      {% set stats = [
        ('มูลค่าปัจจุบัน', '฿' ~ (f.value|money), 'text-slate-900'),
        ('ต้นทุนรวม', '฿' ~ (f.cost|money), 'text-slate-900'),
        ('กำไร/ขาดทุน', (f.pl|signed) ~ ' (' ~ ('%+.2f'|format(f.pl_pct)) ~ '%)', plc(f.pl)),
        ('ปันผล/เดือน (สุทธิ)', '฿' ~ (f.monthly_net|money), 'text-emerald-600'),
        ('Yield on Cost', ('%.2f'|format(f.yoc)) ~ '%', 'text-slate-900'),
        ('Yield ปัจจุบัน', ('%.2f'|format(f.cur_yield)) ~ '%', 'text-slate-900'),
        ('ปันผลรับแล้ว', '฿' ~ (f.received|money), 'text-emerald-600'),
        ('ผลตอบแทนรวมปันผล', ('%+.2f'|format(f.total_ret_pct)) ~ '%', plc(f.total_ret)),
      ] %}
      {% for t, v, c in stats %}
      <div class="card p-4"><p class="text-xs text-slate-500">{{ t }}</p><p class="mt-1 font-bold num {{ c }} text-base sm:text-lg leading-tight">{{ v }}</p></div>
      {% endfor %}
    </div>

    <div class="card p-5">
      <div class="grid sm:grid-cols-4 gap-4 text-sm">
        <div><p class="text-slate-400 text-xs">จำนวนหน่วย</p><p class="font-semibold num">{{ f.units|units }}</p></div>
        <div><p class="text-slate-400 text-xs">ต้นทุนเฉลี่ย</p><p class="font-semibold num">{{ '%.4f'|format(f.avg_cost) }}</p></div>
        <div><p class="text-slate-400 text-xs">NAV ล่าสุด</p><p class="font-semibold num">{{ '%.4f'|format(f.current_nav) }} <span class="text-xs text-slate-400 font-normal">({{ f.nav_updated|thdate }})</span></p></div>
        <div><p class="text-slate-400 text-xs">ปันผล/หน่วย/ปี</p><p class="font-semibold num">{{ '%.4f'|format(f.annual_div_per_unit) }} <span class="text-xs text-slate-400 font-normal">· ค่าธรรมเนียม {{ '%.2f'|format(f.expense_ratio or 0) }}%</span></p></div>
      </div>
      {% if f.notes %}<p class="mt-4 rounded-xl bg-slate-50 p-3 text-sm text-slate-600">📝 {{ f.notes }}</p>{% endif %}
    </div>

    <div class="card p-5">
      <div class="flex items-center justify-between mb-4">
        <h2 class="section-title">ประวัติปันผลต่อหน่วย</h2>
        {% if f.trend %}<span class="chip {{ {'up':'bg-emerald-50 text-emerald-700','down':'bg-rose-50 text-rose-700','flat':'bg-slate-100 text-slate-600'}[f.trend] }}">
          {{ {'up':'▲ เพิ่มขึ้น','down':'▼ ลดลง','flat':'● คงที่'}[f.trend] }} {{ '%+.1f'|format(f.trend_pct) }}%</span>{% endif %}
      </div>
      {% if divs %}<div class="h-56"><canvas id="divChart"></canvas></div>
      {% else %}<p class="text-sm text-slate-400 py-8 text-center">ยังไม่มีบันทึกปันผล — บันทึกได้จากฟอร์มด้านขวา</p>{% endif %}
    </div>

    <div class="card overflow-hidden">
      <h2 class="section-title p-5 pb-3">รายการปันผลที่ได้รับ</h2>
      <div class="overflow-x-auto">
        <table class="min-w-full divide-y divide-slate-100">
          <thead class="bg-slate-50"><tr><th class="th">วันที่</th><th class="th text-right">ต่อหน่วย</th><th class="th text-right">หน่วย</th><th class="th text-right">ก่อนภาษี</th><th class="th text-right">ภาษี</th><th class="th text-right">สุทธิ</th><th class="th"></th></tr></thead>
          <tbody class="divide-y divide-slate-100">
            {% for d in divs %}
            <tr><td class="td">{{ d.pay_date|thdate }}</td><td class="td text-right num">{{ '%.4f'|format(d.per_unit) }}</td><td class="td text-right num">{{ d.units|units }}</td>
                <td class="td text-right num">{{ d.gross|money }}</td><td class="td text-right num text-amber-600">{{ d.tax|money }}</td><td class="td text-right num font-semibold text-emerald-600">{{ d.net|money }}</td>
                <td class="td text-right"><form method="post" action="{{ url_for('dividend_delete', did=d.id) }}" onsubmit="return confirm('ลบรายการนี้?')"><input type="hidden" name="next" value="{{ request.path }}"><button class="text-slate-400 hover:text-rose-600">{{ icon('trash', 'w-4 h-4') }}</button></form></td></tr>
            {% else %}<tr><td colspan="7" class="td text-center text-slate-400 py-6">ไม่มีรายการ</td></tr>{% endfor %}
          </tbody>
        </table>
      </div>
    </div>

    <div class="card overflow-hidden">
      <h2 class="section-title p-5 pb-3">ประวัติการซื้อ/ขาย</h2>
      <div class="overflow-x-auto">
        <table class="min-w-full divide-y divide-slate-100">
          <thead class="bg-slate-50"><tr><th class="th">วันที่</th><th class="th">ประเภท</th><th class="th text-right">หน่วย</th><th class="th text-right">NAV</th><th class="th text-right">จำนวนเงิน</th><th class="th text-right">กำไรที่รับรู้</th></tr></thead>
          <tbody class="divide-y divide-slate-100">
            {% for t in txs %}
            <tr><td class="td">{{ t.tx_date|thdate }}</td>
                <td class="td"><span class="chip {{ 'bg-indigo-50 text-indigo-700' if t.tx_type == 'BUY' else 'bg-orange-50 text-orange-700' }}">{{ 'ซื้อ' if t.tx_type == 'BUY' else 'ขาย' }}</span></td>
                <td class="td text-right num">{{ t.units|units }}</td><td class="td text-right num">{{ '%.4f'|format(t.nav) }}</td><td class="td text-right num">{{ t.amount|money }}</td>
                <td class="td text-right num {{ plc(t.realized_pl) if t.tx_type == 'SELL' else 'text-slate-300' }}">{{ t.realized_pl|signed if t.tx_type == 'SELL' else '–' }}</td></tr>
            {% else %}<tr><td colspan="6" class="td text-center text-slate-400 py-6">ไม่มีรายการ</td></tr>{% endfor %}
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <aside class="space-y-5">
    <div class="card p-5">
      <div class="flex items-center justify-between">
        <h2 class="section-title flex items-center gap-2">{{ icon('spark', 'w-5 h-5 text-violet-600') }} ผลวิเคราะห์</h2>
        <span class="text-2xl font-bold text-slate-900 num">{{ f.score }}<span class="text-sm text-slate-400 font-normal">/100</span></span>
      </div>
      <div class="mt-3 h-2 rounded-full bg-slate-100 overflow-hidden"><div class="h-full rounded-full {{ f.rec.bar }}" style="width: {{ f.score }}%"></div></div>
      <div class="mt-4 rounded-xl p-4 ring-1 ring-inset {{ f.rec.cls }}">
        <p class="font-bold">{{ f.rec.icon }} {{ f.rec.label }}</p>
        <p class="text-sm mt-1 opacity-90">{{ f.rec.summary }}</p>
      </div>
      <ul class="mt-4 space-y-2">
        {% for r in f.reasons %}
        <li class="flex items-start gap-2 text-sm">
          <span class="mt-0.5 shrink-0 w-10 text-center rounded-md text-xs font-semibold py-0.5 {{ 'bg-emerald-50 text-emerald-700' if r.pts > 0 else 'bg-rose-50 text-rose-700' if r.pts < 0 else 'bg-slate-100 text-slate-500' }}">{{ '%+d'|format(r.pts) }}</span>
          <span class="text-slate-600">{{ r.text }}</span>
        </li>
        {% endfor %}
      </ul>
      {% if f.breakeven %}<p class="mt-4 text-xs text-slate-500 rounded-lg bg-slate-50 p-3">⏱ ด้วยปันผลปัจจุบัน ต้องใช้เวลาราว <b>{{ '%.0f'|format(f.breakeven) }} เดือน</b> เพื่อชดเชยส่วนที่ขาดทุน ฿{{ (-f.pl)|money }}</p>{% endif %}
      <p class="mt-3 text-[11px] text-slate-400">* วิเคราะห์จากเกณฑ์อัตโนมัติ ไม่ใช่คำแนะนำการลงทุน</p>
    </div>

    <div class="card p-5">
      <div class="flex gap-1 rounded-xl bg-slate-100 p-1 text-sm font-medium mb-4" id="tabs">
        <button data-tab="trade" class="flex-1 rounded-lg py-1.5 bg-white shadow-sm text-slate-900">ซื้อ/ขาย</button>
        <button data-tab="div" class="flex-1 rounded-lg py-1.5 text-slate-500">บันทึกปันผล</button>
        <button data-tab="nav" class="flex-1 rounded-lg py-1.5 text-slate-500">NAV</button>
      </div>

      <form data-pane="trade" method="post" action="{{ url_for('fund_trade', fid=f.id) }}" class="space-y-3">
        <div class="grid grid-cols-2 gap-2">
          <label class="cursor-pointer"><input type="radio" name="tx_type" value="BUY" class="peer sr-only" checked><span class="block text-center rounded-xl py-2 text-sm font-semibold ring-1 ring-slate-200 peer-checked:bg-indigo-600 peer-checked:text-white peer-checked:ring-indigo-600">ซื้อเพิ่ม</span></label>
          <label class="cursor-pointer"><input type="radio" name="tx_type" value="SELL" class="peer sr-only"><span class="block text-center rounded-xl py-2 text-sm font-semibold ring-1 ring-slate-200 peer-checked:bg-orange-500 peer-checked:text-white peer-checked:ring-orange-500">ขาย</span></label>
        </div>
        <div class="grid grid-cols-2 gap-3">
          <div><label class="label">วันที่</label><input type="date" name="tx_date" value="{{ today.isoformat() }}" class="input"></div>
          <div><label class="label">NAV</label><input name="nav" value="{{ f.current_nav }}" inputmode="decimal" class="input num" required></div>
          <div><label class="label">จำนวนเงิน (บาท)</label><input name="amount" inputmode="decimal" class="input num" placeholder="สำหรับซื้อ"></div>
          <div><label class="label">หรือ จำนวนหน่วย</label><input name="units" inputmode="decimal" class="input num" placeholder="สูงสุด {{ '%.4f'|format(f.units) }}"></div>
        </div>
        <label class="flex items-center gap-2 text-sm text-slate-600"><input type="checkbox" name="update_nav" value="1" class="rounded"> อัปเดต NAV ปัจจุบันด้วยราคานี้</label>
        <button class="btn-primary w-full">บันทึกรายการ</button>
      </form>

      <form data-pane="div" method="post" action="{{ url_for('dividend_add') }}" class="space-y-3 hidden">
        <input type="hidden" name="fund_id" value="{{ f.id }}"><input type="hidden" name="next" value="{{ request.path }}">
        <div class="grid grid-cols-2 gap-3">
          <div><label class="label">วันที่จ่าย</label><input type="date" name="pay_date" value="{{ today.isoformat() }}" class="input"></div>
          <div><label class="label">ปันผล/หน่วย</label><input name="per_unit" value="{{ '%.4f'|format(per_payment) if per_payment else '' }}" inputmode="decimal" class="input num" required></div>
          <div class="col-span-2"><label class="label">จำนวนหน่วยที่ได้รับสิทธิ์</label><input name="units" value="{{ f.units }}" inputmode="decimal" class="input num"></div>
        </div>
        <label class="flex items-center gap-2 text-sm text-slate-600"><input type="checkbox" name="withhold" value="1" checked class="rounded"> หักภาษี ณ ที่จ่าย 10%</label>
        <input name="note" class="input" placeholder="หมายเหตุ (ถ้ามี)">
        <button class="btn-success w-full">บันทึกปันผล</button>
      </form>

      <form data-pane="nav" method="post" action="{{ url_for('fund_nav', fid=f.id) }}" class="space-y-3 hidden">
        <div class="grid grid-cols-2 gap-3">
          <div><label class="label">NAV ใหม่</label><input name="current_nav" value="{{ f.current_nav }}" inputmode="decimal" class="input num" required></div>
          <div><label class="label">ณ วันที่</label><input type="date" name="nav_date" value="{{ today.isoformat() }}" class="input"></div>
        </div>
        <button class="btn-primary w-full">อัปเดต NAV</button>
      </form>
    </div>

    <div class="card p-5">
      <h2 class="section-title mb-1">🧮 เครื่องคิดเลข What-if</h2>
      <p class="text-xs text-slate-400 mb-4">ทดลองตัวเลขโดยไม่บันทึกลงระบบ</p>
      <label class="label">ถ้าซื้อเพิ่ม (บาท) ที่ NAV ปัจจุบัน</label>
      <input id="wiBuy" inputmode="decimal" class="input num" placeholder="เช่น 10000">
      <div id="wiBuyOut" class="mt-2 text-sm text-slate-600 space-y-1"></div>
      <label class="label mt-4">ถ้า NAV เปลี่ยนเป็น</label>
      <input id="wiNav" type="range" min="{{ '%.4f'|format(f.current_nav * 0.5) }}" max="{{ '%.4f'|format(f.current_nav * 1.5) }}" step="0.01" value="{{ f.current_nav }}" class="w-full accent-indigo-600">
      <div id="wiNavOut" class="mt-1 text-sm"></div>
    </div>
  </aside>
</div>
{% endblock %}

{% block scripts %}
<script>
const F = {{ {'units': f.units, 'avg': f.avg_cost, 'nav': f.current_nav, 'dpu': f.annual_div_per_unit, 'tax': TAX_RATE}|tojson }};
const CH = {{ chart|tojson }};
if (document.getElementById('divChart')) {
  new Chart(document.getElementById('divChart'), {
    type: 'line',
    data: { labels: CH.labels, datasets: [{ label: 'ปันผล/หน่วย', data: CH.values, borderColor: '#10b981', backgroundColor: 'rgba(16,185,129,.12)', fill: true, tension: .35, pointRadius: 4, pointBackgroundColor: '#10b981' }] },
    options: { maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true, grid: { color: '#f1f5f9' } }, x: { grid: { display: false } } } }
  });
}
document.querySelectorAll('#tabs button').forEach(b => b.addEventListener('click', () => {
  document.querySelectorAll('#tabs button').forEach(x => { x.classList.toggle('bg-white', x === b); x.classList.toggle('shadow-sm', x === b); x.classList.toggle('text-slate-900', x === b); x.classList.toggle('text-slate-500', x !== b); });
  document.querySelectorAll('[data-pane]').forEach(p => p.classList.toggle('hidden', p.dataset.pane !== b.dataset.tab));
}));
const wiBuy = () => {
  const amt = parseFloat(document.getElementById('wiBuy').value.replace(/,/g, '')) || 0, out = document.getElementById('wiBuyOut');
  if (!amt || !F.nav) { out.innerHTML = ''; return; }
  const add = amt / F.nav, nu = F.units + add, navg = (F.units * F.avg + amt) / nu, mo = nu * F.dpu * (1 - F.tax) / 12;
  out.innerHTML = `<p>ได้เพิ่ม <b class="num">${fmt(add, 4)}</b> หน่วย</p>
    <p>ต้นทุนเฉลี่ยใหม่ <b class="num">${fmt(navg, 4)}</b> <span class="text-xs ${navg < F.avg ? 'text-emerald-600' : 'text-rose-600'}">(${navg < F.avg ? 'ลดลง' : 'เพิ่มขึ้น'} ${fmt(Math.abs(navg - F.avg), 4)})</span></p>
    <p>ปันผล/เดือนใหม่ <b class="num text-emerald-600">฿${fmt(mo)}</b></p>`;
};
const wiNav = () => {
  const nav = parseFloat(document.getElementById('wiNav').value), pl = F.units * (nav - F.avg), pct = F.avg ? (nav / F.avg - 1) * 100 : 0, y = nav ? F.dpu / nav * 100 : 0;
  document.getElementById('wiNavOut').innerHTML = `NAV <b class="num">${fmt(nav, 4)}</b> → <b class="num ${pl >= 0 ? 'text-emerald-600' : 'text-rose-600'}">${pl >= 0 ? '+' : ''}${fmt(pl)} บาท (${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%)</b> · Yield ${y.toFixed(2)}%`;
};
document.getElementById('wiBuy').addEventListener('input', wiBuy);
document.getElementById('wiNav').addEventListener('input', wiNav);
wiNav();
</script>
{% endblock %}
"""

TEMPLATES["dividends.html"] = r"""{% extends "base.html" %}
{% from "macros.html" import icon, kpi %}
{% block title %}ปันผล & ปฏิทิน{% endblock %}
{% block content %}
<div class="mb-6">
  <h1 class="text-2xl sm:text-3xl font-bold text-slate-900">ปันผล & ปฏิทิน</h1>
  <p class="text-slate-500 text-sm mt-1">ติดตามปันผลที่ได้รับจริง และคาดการณ์กระแสเงินสดรายเดือน</p>
</div>

<section class="grid sm:grid-cols-2 xl:grid-cols-4 gap-4 mb-6">
  {{ kpi('ปันผลคาดการณ์/เดือน', '฿' ~ (s.monthly_net|money), 'สุทธิหลังภาษี', 'emerald', 'coin') }}
  {{ kpi('ปันผลคาดการณ์/ปี', '฿' ~ (s.annual_net|money), 'ก่อนภาษี ฿' ~ (s.annual_gross|money), 'teal', 'calendar') }}
  {{ kpi('รับแล้วปี ' ~ (today.year + 543), '฿' ~ (s.received_ytd|money), 'สุทธิ', 'indigo', 'chart') }}
  {{ kpi('รับแล้วทั้งหมด', '฿' ~ (s.received|money), 'ภาษีที่ถูกหัก ฿' ~ (s.tax_paid|money), 'violet', 'spark') }}
</section>

<section class="card p-5 mb-6">
  <div class="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2 mb-4">
    <h2 class="section-title">ปฏิทินปันผลคาดการณ์ (สุทธิ) ม.ค.–ธ.ค.</h2>
    <p class="text-xs text-slate-400">คำนวณจากปันผล/หน่วย/ปี × หน่วยที่ถือ แบ่งตามเดือนที่จ่าย</p>
  </div>
  {% if chart.cal_series %}
  <div class="h-72"><canvas id="calChart"></canvas></div>
  <div class="mt-4 grid grid-cols-3 sm:grid-cols-6 lg:grid-cols-12 gap-2">
    {% for m in TH_MONTHS %}
    {% set v = totals[loop.index0] %}
    <div class="rounded-xl p-2 text-center {{ 'bg-emerald-50 ring-1 ring-emerald-200' if loop.index == today.month else 'bg-slate-50' }}">
      <p class="text-xs text-slate-500">{{ m }}</p><p class="text-sm font-semibold num {{ 'text-emerald-700' if v > 0 else 'text-slate-300' }}">{{ v|num0 }}</p>
    </div>
    {% endfor %}
  </div>
  {% else %}<p class="text-sm text-slate-400 py-8 text-center">ยังไม่มีกองทุนที่ระบุปันผล</p>{% endif %}
</section>

<section class="grid lg:grid-cols-3 gap-4 mb-6">
  <div class="card p-5 lg:col-span-2">
    <h2 class="section-title mb-4">ปันผลที่ได้รับจริง 12 เดือนล่าสุด</h2>
    <div class="h-64"><canvas id="recvChart"></canvas></div>
  </div>
  <div class="card p-5">
    <h2 class="section-title mb-4">บันทึกปันผลที่ได้รับ</h2>
    {% if items %}
    <form method="post" action="{{ url_for('dividend_add') }}" class="space-y-3">
      <input type="hidden" name="next" value="{{ request.path }}">
      <div><label class="label">กองทุน</label>
        <select name="fund_id" id="dFund" class="input">
          {% for i in items %}<option value="{{ i.id }}" data-units="{{ i.units }}" data-dpu="{{ i.annual_div_per_unit }}" data-n="{{ (i.pay_months or '').split(',')|select|list|length or 1 }}">{{ i.code }} — {{ i.name }}</option>{% endfor %}
        </select></div>
      <div class="grid grid-cols-2 gap-3">
        <div><label class="label">วันที่จ่าย</label><input type="date" name="pay_date" value="{{ today.isoformat() }}" class="input"></div>
        <div><label class="label">ปันผล/หน่วย</label><input name="per_unit" id="dPer" inputmode="decimal" class="input num" required></div>
      </div>
      <div><label class="label">จำนวนหน่วย</label><input name="units" id="dUnits" inputmode="decimal" class="input num"></div>
      <label class="flex items-center gap-2 text-sm text-slate-600"><input type="checkbox" name="withhold" value="1" checked class="rounded"> หักภาษี ณ ที่จ่าย 10%</label>
      <p id="dPreview" class="text-sm text-slate-500"></p>
      <button class="btn-success w-full">บันทึกปันผล</button>
    </form>
    {% else %}<p class="text-sm text-slate-400">เพิ่มกองทุนก่อนเพื่อบันทึกปันผล</p>{% endif %}
  </div>
</section>

<section class="grid lg:grid-cols-3 gap-4 mb-6">
  <div class="card p-5">
    <h2 class="section-title mb-4">งวดถัดไปของแต่ละกอง</h2>
    <ul class="divide-y divide-slate-100">
      {% for u in upcoming %}
      <li class="py-2.5 flex justify-between text-sm"><span><b class="text-slate-900">{{ u.code }}</b> <span class="text-slate-400">· {{ u.label }}</span></span><span class="num font-semibold text-emerald-600">฿{{ u.amount|money }}</span></li>
      {% else %}<li class="text-sm text-slate-400">ไม่มีข้อมูล</li>{% endfor %}
    </ul>
  </div>
  <div class="card p-5 lg:col-span-2">
    <h2 class="section-title mb-4">ปันผลสุทธิรายปี (พ.ศ.)</h2>
    <div class="h-56"><canvas id="yearChart"></canvas></div>
  </div>
</section>

<section class="card overflow-hidden">
  <h2 class="section-title p-5 pb-3">ประวัติปันผลทั้งหมด ({{ rows|length }} รายการ)</h2>
  <div class="overflow-x-auto">
    <table class="min-w-full divide-y divide-slate-100">
      <thead class="bg-slate-50"><tr><th class="th">วันที่</th><th class="th">กองทุน</th><th class="th text-right">ต่อหน่วย</th><th class="th text-right">หน่วย</th><th class="th text-right">ก่อนภาษี</th><th class="th text-right">ภาษี</th><th class="th text-right">สุทธิ</th><th class="th"></th></tr></thead>
      <tbody class="divide-y divide-slate-100">
        {% for d in rows %}
        <tr class="hover:bg-slate-50/70">
          <td class="td">{{ d.pay_date|thdate }}</td>
          <td class="td"><a href="{{ url_for('fund_detail', fid=d.fund_id) }}" class="font-semibold text-slate-900 hover:text-indigo-600">{{ d.code }}</a>{% if d.note %}<span class="text-xs text-slate-400"> · {{ d.note }}</span>{% endif %}</td>
          <td class="td text-right num">{{ '%.4f'|format(d.per_unit) }}</td><td class="td text-right num">{{ d.units|units }}</td>
          <td class="td text-right num">{{ d.gross|money }}</td><td class="td text-right num text-amber-600">{{ d.tax|money }}</td>
          <td class="td text-right num font-semibold text-emerald-600">{{ d.net|money }}</td>
          <td class="td text-right"><form method="post" action="{{ url_for('dividend_delete', did=d.id) }}" onsubmit="return confirm('ลบรายการนี้?')"><input type="hidden" name="next" value="{{ request.path }}"><button class="text-slate-400 hover:text-rose-600">{{ icon('trash', 'w-4 h-4') }}</button></form></td>
        </tr>
        {% else %}<tr><td colspan="8" class="td text-center text-slate-400 py-8">ยังไม่มีรายการปันผล</td></tr>{% endfor %}
      </tbody>
    </table>
  </div>
</section>
{% endblock %}

{% block scripts %}
<script>
const C = {{ chart|tojson }};
const moneyTip = { callbacks: { label: c => ` ${c.dataset.label ? c.dataset.label + ': ' : ''}฿${fmt(c.raw)}` } };
const yTicks = { beginAtZero: true, grid: { color: '#f1f5f9' }, ticks: { callback: v => '฿' + fmt(v, 0) } };
if (document.getElementById('calChart')) new Chart(document.getElementById('calChart'), {
  type: 'bar',
  data: { labels: C.cal_labels, datasets: C.cal_series.map((s, i) => ({ label: s.code, data: s.data, backgroundColor: PALETTE[i % PALETTE.length], borderRadius: 4, maxBarThickness: 40 })) },
  options: { maintainAspectRatio: false, plugins: { tooltip: moneyTip }, scales: { x: { stacked: true, grid: { display: false } }, y: { ...yTicks, stacked: true } } }
});
new Chart(document.getElementById('recvChart'), {
  type: 'bar',
  data: { labels: C.recv_labels, datasets: [{ label: '', data: C.recv_values, backgroundColor: '#10b981', borderRadius: 8, maxBarThickness: 36 }] },
  options: { maintainAspectRatio: false, plugins: { legend: { display: false }, tooltip: moneyTip }, scales: { y: yTicks, x: { grid: { display: false } } } }
});
new Chart(document.getElementById('yearChart'), {
  type: 'bar',
  data: { labels: C.year_labels, datasets: [{ label: '', data: C.year_values, backgroundColor: '#6366f1', borderRadius: 8, maxBarThickness: 56 }] },
  options: { maintainAspectRatio: false, plugins: { legend: { display: false }, tooltip: moneyTip }, scales: { y: yTicks, x: { grid: { display: false } } } }
});
(() => {
  const sel = document.getElementById('dFund'); if (!sel) return;
  const per = document.getElementById('dPer'), un = document.getElementById('dUnits'), pv = document.getElementById('dPreview');
  const wh = sel.form.querySelector('[name=withhold]');
  const fill = () => { const o = sel.selectedOptions[0]; un.value = o.dataset.units; per.value = (parseFloat(o.dataset.dpu) / parseInt(o.dataset.n)).toFixed(4); calc(); };
  const calc = () => { const g = (parseFloat(per.value) || 0) * (parseFloat(un.value) || 0), n = wh.checked ? g * 0.9 : g;
    pv.innerHTML = g ? `ก่อนภาษี ฿${fmt(g)} → สุทธิ <b class="text-emerald-600">฿${fmt(n)}</b>` : ''; };
  sel.addEventListener('change', fill); [per, un, wh].forEach(e => e.addEventListener('input', calc));
  fill();
})();
</script>
{% endblock %}
"""

TEMPLATES["analysis.html"] = r"""{% extends "base.html" %}
{% from "macros.html" import icon, rec_badge, plc %}
{% block title %}วิเคราะห์ & คำแนะนำ{% endblock %}
{% block content %}
<div class="mb-6">
  <h1 class="text-2xl sm:text-3xl font-bold text-slate-900">วิเคราะห์ & คำแนะนำ</h1>
  <p class="text-slate-500 text-sm mt-1">ให้คะแนนแต่ละกองจากอัตราปันผล กำไร/ขาดทุน แนวโน้มปันผล ค่าธรรมเนียม และการกระจุกตัว</p>
</div>

{% if items %}
<section class="grid lg:grid-cols-3 gap-4 mb-6">
  <div class="card p-6 flex items-center gap-6">
    {% set h = s.health|round|int %}
    {% set hc = '#10b981' if h >= 70 else '#0ea5e9' if h >= 50 else '#f59e0b' %}
    <div class="relative h-28 w-28 shrink-0 rounded-full grid place-items-center" style="background: conic-gradient({{ hc }} {{ h * 3.6 }}deg, #f1f5f9 0)">
      <div class="h-[5.5rem] w-[5.5rem] rounded-full bg-white grid place-items-center">
        <span class="text-3xl font-bold text-slate-900 num">{{ h }}</span>
      </div>
    </div>
    <div>
      <p class="text-sm text-slate-500">คะแนนสุขภาพพอร์ต</p>
      <p class="text-xl font-bold" style="color: {{ hc }}">{{ s.health_label }}</p>
      <p class="text-xs text-slate-400 mt-1">ค่าเฉลี่ยถ่วงน้ำหนักตามมูลค่าแต่ละกอง</p>
    </div>
  </div>
  <div class="card p-6 lg:col-span-2">
    <p class="text-sm text-slate-500 mb-3">สรุปคำแนะนำ</p>
    <div class="grid grid-cols-2 sm:grid-cols-5 gap-3">
      {% for k, r in rec_styles.items() %}
      <div class="rounded-xl p-3 ring-1 ring-inset {{ r.cls }}">
        <p class="text-2xl font-bold">{{ counts[k] }}</p><p class="text-xs font-medium">{{ r.icon }} {{ r.label }}</p>
      </div>
      {% endfor %}
    </div>
  </div>
</section>

<section class="card p-5 mb-6">
  <h2 class="section-title mb-3 flex items-center gap-2">{{ icon('spark', 'w-5 h-5 text-violet-600') }} ข้อสังเกตของพอร์ต</h2>
  <div class="grid md:grid-cols-2 gap-3">
    {% for kind, text in insights %}
    <div class="flex gap-3 rounded-xl p-3 text-sm {{ {'good':'bg-emerald-50 text-emerald-800','warn':'bg-amber-50 text-amber-800','info':'bg-slate-50 text-slate-700'}[kind] }}">
      <span class="font-bold">{{ {'good':'✓','warn':'!','info':'i'}[kind] }}</span><span>{{ text }}</span>
    </div>
    {% endfor %}
  </div>
</section>

<section class="space-y-3">
  {% for i in items %}
  <details class="card group" {{ 'open' if loop.first }}>
    <summary class="list-none cursor-pointer p-5 flex flex-col md:flex-row md:items-center gap-4">
      <div class="flex items-center gap-4 md:w-72 shrink-0">
        <span class="h-10 w-10 shrink-0 rounded-xl bg-slate-100 grid place-items-center text-sm font-bold text-slate-500">#{{ loop.index }}</span>
        <div class="min-w-0"><p class="font-bold text-slate-900">{{ i.code }}</p><p class="text-xs text-slate-500 truncate">{{ i.name }}</p></div>
      </div>
      <div class="flex-1 grid grid-cols-3 gap-3 text-sm">
        <div><p class="text-xs text-slate-400">กำไร/ขาดทุน</p><p class="font-semibold num {{ plc(i.pl) }}">{{ i.pl_pct|signed }}%</p></div>
        <div><p class="text-xs text-slate-400">Yield</p><p class="font-semibold num">{{ '%.2f'|format(i.cur_yield) }}%</p></div>
        <div><p class="text-xs text-slate-400">ปันผล/เดือน</p><p class="font-semibold num text-emerald-600">฿{{ i.monthly_net|money }}</p></div>
      </div>
      <div class="flex items-center gap-4 md:w-80 shrink-0">
        <div class="flex-1">
          <div class="flex justify-between text-xs text-slate-400 mb-1"><span>คะแนน</span><span class="font-semibold text-slate-700">{{ i.score }}</span></div>
          <div class="h-2 rounded-full bg-slate-100 overflow-hidden"><div class="h-full rounded-full {{ i.rec.bar }}" style="width: {{ i.score }}%"></div></div>
        </div>
        {{ rec_badge(i.rec) }}
        <span class="text-slate-400 transition group-open:rotate-180">▾</span>
      </div>
    </summary>
    <div class="px-5 pb-5 border-t border-slate-100 pt-4 grid md:grid-cols-2 gap-5">
      <div>
        <p class="text-sm font-semibold text-slate-900 mb-2">เหตุผลประกอบ</p>
        <ul class="space-y-1.5">
          {% for r in i.reasons %}
          <li class="flex items-start gap-2 text-sm"><span class="shrink-0 w-10 text-center rounded-md text-xs font-semibold py-0.5 {{ 'bg-emerald-50 text-emerald-700' if r.pts > 0 else 'bg-rose-50 text-rose-700' if r.pts < 0 else 'bg-slate-100 text-slate-500' }}">{{ '%+d'|format(r.pts) }}</span><span class="text-slate-600">{{ r.text }}</span></li>
          {% endfor %}
        </ul>
      </div>
      <div class="space-y-3">
        <div class="rounded-xl p-4 ring-1 ring-inset {{ i.rec.cls }}"><p class="font-bold">{{ i.rec.icon }} {{ i.rec.label }}</p><p class="text-sm mt-1">{{ i.rec.summary }}</p></div>
        <div class="grid grid-cols-2 gap-2 text-sm">
          <div class="rounded-xl bg-slate-50 p-3"><p class="text-xs text-slate-400">สัดส่วนในพอร์ต</p><p class="font-semibold num">{{ '%.1f'|format(i.weight) }}%</p></div>
          <div class="rounded-xl bg-slate-50 p-3"><p class="text-xs text-slate-400">ผลตอบแทนรวมปันผล</p><p class="font-semibold num {{ plc(i.total_ret) }}">{{ i.total_ret_pct|signed }}%</p></div>
        </div>
        <a href="{{ url_for('fund_detail', fid=i.id) }}" class="btn-secondary w-full">ดูรายละเอียดกองทุน →</a>
      </div>
    </div>
  </details>
  {% endfor %}
</section>

<div class="card p-5 mt-6 text-sm text-slate-500">
  <p class="font-semibold text-slate-700 mb-2">เกณฑ์การให้คำแนะนำ</p>
  <ul class="grid sm:grid-cols-2 gap-x-6 gap-y-1 list-disc pl-5">
    <li>คะแนนเริ่มต้น 50 แล้วบวก/ลบตามปัจจัยต่างๆ (0–100)</li>
    <li><b>สะสมเพิ่ม</b> ≥ 72 · <b>ถือต่อ</b> 52–71 · <b>เฝ้าระวัง</b> 38–51 · <b>พิจารณาขาย</b> &lt; 38</li>
    <li><b>ขายทำกำไรบางส่วน</b>: กำไร ≥ 30% แต่ยีลด์ปัจจุบัน &lt; 4%</li>
    <li>แนวโน้มปันผลเทียบงวดล่าสุดกับงวดก่อนหน้า (±5%)</li>
  </ul>
  <p class="mt-3 text-xs text-slate-400">* ผลวิเคราะห์เป็นเกณฑ์อัตโนมัติเพื่อช่วยตัดสินใจเบื้องต้น ไม่ใช่คำแนะนำการลงทุน ควรศึกษาหนังสือชี้ชวนก่อนตัดสินใจ</p>
</div>
{% else %}
<div class="card p-12 text-center text-slate-500">ยังไม่มีกองทุนให้วิเคราะห์ <a href="{{ url_for('fund_new') }}" class="text-indigo-600 hover:underline">เพิ่มกองทุน</a></div>
{% endif %}
{% endblock %}
"""

TEMPLATES["nav.html"] = r"""{% extends "base.html" %}
{% block title %}อัปเดต NAV{% endblock %}
{% block content %}
<div class="max-w-4xl mx-auto">
  <h1 class="text-2xl sm:text-3xl font-bold text-slate-900">อัปเดต NAV</h1>
  <p class="text-slate-500 text-sm mt-1">กรอก NAV ล่าสุดของแต่ละกองทุนพร้อมกันทีเดียว ระบบจะคำนวณกำไร/ขาดทุนใหม่ทันที</p>
  {% if items %}
  <form method="post" class="mt-6 card overflow-hidden">
    <div class="p-5 flex flex-col sm:flex-row sm:items-end gap-3 border-b border-slate-100">
      <div><label class="label">NAV ณ วันที่</label><input type="date" name="nav_date" value="{{ today.isoformat() }}" class="input"></div>
      <p class="text-xs text-slate-400 sm:ml-auto">💡 ดู NAV ได้จากเว็บไซต์ บลจ. หรือแอปที่ใช้ซื้อกองทุน</p>
    </div>
    <div class="overflow-x-auto">
      <table class="min-w-full divide-y divide-slate-100">
        <thead class="bg-slate-50"><tr><th class="th">กองทุน</th><th class="th text-right">NAV เดิม</th><th class="th">NAV ใหม่</th><th class="th text-right">เปลี่ยนแปลง</th><th class="th text-right">กำไร/ขาดทุนใหม่</th></tr></thead>
        <tbody class="divide-y divide-slate-100">
          {% for i in items %}
          <tr data-units="{{ i.units }}" data-avg="{{ i.avg_cost }}" data-old="{{ i.current_nav }}">
            <td class="td"><p class="font-semibold text-slate-900">{{ i.code }}</p><p class="text-xs text-slate-400">อัปเดตล่าสุด {{ i.nav_updated|thdate }}</p></td>
            <td class="td text-right num">{{ '%.4f'|format(i.current_nav) }}</td>
            <td class="td"><input name="nav_{{ i.id }}" value="{{ i.current_nav }}" inputmode="decimal" class="input num w-32"></td>
            <td class="td text-right num chg">–</td>
            <td class="td text-right num font-semibold newpl">–</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
    <div class="p-5 flex justify-end gap-3 border-t border-slate-100">
      <a href="{{ url_for('dashboard') }}" class="btn-secondary">ยกเลิก</a>
      <button class="btn-primary px-6">บันทึก NAV ทั้งหมด</button>
    </div>
  </form>
  {% else %}<div class="card p-12 text-center text-slate-500 mt-6">ยังไม่มีกองทุน</div>{% endif %}
</div>
{% endblock %}
{% block scripts %}
<script>
document.querySelectorAll('tr[data-units]').forEach(tr => {
  const inp = tr.querySelector('input'), u = +tr.dataset.units, a = +tr.dataset.avg, o = +tr.dataset.old;
  const calc = () => {
    const n = parseFloat(inp.value) || 0, ch = o ? (n / o - 1) * 100 : 0, pl = u * (n - a);
    const c = tr.querySelector('.chg'), p = tr.querySelector('.newpl');
    c.textContent = (ch >= 0 ? '+' : '') + ch.toFixed(2) + '%'; c.className = 'td text-right num chg ' + (ch > 0 ? 'text-emerald-600' : ch < 0 ? 'text-rose-600' : 'text-slate-400');
    p.textContent = (pl >= 0 ? '+' : '') + fmt(pl); p.className = 'td text-right num font-semibold newpl ' + (pl >= 0 ? 'text-emerald-600' : 'text-rose-600');
  };
  inp.addEventListener('input', calc); calc();
});
</script>
{% endblock %}
"""

TEMPLATES["planner.html"] = r"""{% extends "base.html" %}
{% from "macros.html" import icon %}
{% block title %}วางแผนเป้าหมาย{% endblock %}
{% block content %}
<div class="mb-6">
  <h1 class="text-2xl sm:text-3xl font-bold text-slate-900">วางแผนเป้าหมายปันผล</h1>
  <p class="text-slate-500 text-sm mt-1">อยากมีรายได้จากปันผลเดือนละเท่าไหร่? ต้องลงทุนเพิ่มอีกเท่าไหร่ และใช้เวลากี่ปี</p>
</div>

<div class="grid lg:grid-cols-3 gap-5">
  <div class="card p-6 space-y-4">
    <h2 class="section-title">ตั้งค่า</h2>
    <div><label class="label">เป้าหมายปันผลสุทธิ/เดือน (บาท)</label><input id="pTarget" type="number" value="10000" min="0" class="input num"></div>
    <div><label class="label">มูลค่าพอร์ตเริ่มต้น (บาท)</label><input id="pStart" type="number" value="{{ '%.0f'|format(s.value) }}" min="0" class="input num"></div>
    <div><label class="label">อัตราปันผลเฉลี่ย (% ต่อปี)</label><input id="pYield" type="number" step="0.1" value="{{ '%.2f'|format(s.cur_yield if s.cur_yield else 5) }}" class="input num"><p class="hint">ค่าเริ่มต้นจาก Yield ปัจจุบันของพอร์ต</p></div>
    <div><label class="label">ลงทุนเพิ่มต่อเดือน (DCA)</label><input id="pDca" type="number" value="5000" min="0" class="input num"></div>
    <div><label class="label">การเติบโตของ NAV (% ต่อปี)</label><input id="pGrowth" type="number" step="0.5" value="2" class="input num"></div>
    <div><label class="label">ระยะเวลาจำลอง: <span id="pYearsLbl" class="font-bold text-indigo-600">20</span> ปี</label><input id="pYears" type="range" min="1" max="40" value="20" class="w-full accent-indigo-600"></div>
    <label class="flex items-center gap-2 text-sm text-slate-600"><input id="pDrip" type="checkbox" checked class="rounded"> นำปันผลไปลงทุนต่อ (ทบต้น)</label>
  </div>

  <div class="lg:col-span-2 space-y-5">
    <div class="grid sm:grid-cols-3 gap-4">
      <div class="rounded-2xl p-5 text-white bg-gradient-to-br from-indigo-600 to-violet-600 shadow-lg shadow-indigo-600/20">
        <p class="text-sm text-indigo-100">เงินลงทุนที่ต้องมี</p><p id="rNeed" class="text-2xl font-bold num mt-1">–</p><p class="text-xs text-indigo-100 mt-1">เพื่อได้ปันผลตามเป้า</p>
      </div>
      <div class="card p-5"><p class="text-sm text-slate-500">ยังขาดอีก</p><p id="rGap" class="text-2xl font-bold num mt-1 text-slate-900">–</p><p id="rNow" class="text-xs text-slate-400 mt-1"></p></div>
      <div class="card p-5"><p class="text-sm text-slate-500">ถึงเป้าหมายในอีก</p><p id="rYears" class="text-2xl font-bold mt-1 text-emerald-600">–</p><p class="text-xs text-slate-400 mt-1">ตามสมมติฐานด้านซ้าย</p></div>
    </div>
    <div class="card p-5">
      <div class="flex justify-between text-sm mb-2"><span class="font-semibold text-slate-900">ความคืบหน้าสู่เป้าหมาย</span><span id="rPct" class="font-semibold text-indigo-600">0%</span></div>
      <div class="h-3 rounded-full bg-slate-100 overflow-hidden"><div id="rBar" class="h-full rounded-full bg-gradient-to-r from-emerald-400 to-teal-500 transition-all" style="width:0%"></div></div>
    </div>
    <div class="card p-5">
      <h2 class="section-title mb-4">จำลองการเติบโตของพอร์ตและปันผล</h2>
      <div class="h-80"><canvas id="simChart"></canvas></div>
    </div>
    <div class="card overflow-hidden">
      <div class="overflow-x-auto max-h-80">
        <table class="min-w-full divide-y divide-slate-100">
          <thead class="bg-slate-50 sticky top-0"><tr><th class="th">ปีที่</th><th class="th text-right">เงินลงทุนสะสม</th><th class="th text-right">มูลค่าพอร์ต</th><th class="th text-right">ปันผล/เดือน (สุทธิ)</th></tr></thead>
          <tbody id="simTable" class="divide-y divide-slate-100"></tbody>
        </table>
      </div>
    </div>
  </div>
</div>
{% endblock %}
{% block scripts %}
<script>
(() => {
  const $ = id => document.getElementById(id), TAX = {{ TAX_RATE }};
  const chart = new Chart($('simChart'), {
    data: { labels: [], datasets: [
      { type: 'line', label: 'มูลค่าพอร์ต', data: [], borderColor: '#6366f1', backgroundColor: 'rgba(99,102,241,.1)', fill: true, tension: .3, yAxisID: 'y', pointRadius: 0 },
      { type: 'line', label: 'เงินลงทุนสะสม', data: [], borderColor: '#94a3b8', borderDash: [6, 4], tension: .3, yAxisID: 'y', pointRadius: 0 },
      { type: 'bar', label: 'ปันผล/เดือน', data: [], backgroundColor: '#10b981', borderRadius: 4, yAxisID: 'y1', maxBarThickness: 18 },
    ] },
    options: { maintainAspectRatio: false, interaction: { mode: 'index', intersect: false },
      plugins: { tooltip: { callbacks: { label: c => ` ${c.dataset.label}: ฿${fmt(c.raw, 0)}` } } },
      scales: { y: { position: 'left', grid: { color: '#f1f5f9' }, ticks: { callback: v => '฿' + (v >= 1e6 ? (v / 1e6).toFixed(1) + 'M' : fmt(v, 0)) } },
                y1: { position: 'right', grid: { display: false }, ticks: { callback: v => '฿' + fmt(v, 0) } }, x: { grid: { display: false } } } }
  });
  const run = () => {
    const target = +$('pTarget').value || 0, start = +$('pStart').value || 0, y = (+$('pYield').value || 0) / 100;
    const dca = +$('pDca').value || 0, gr = (+$('pGrowth').value || 0) / 100, years = +$('pYears').value, drip = $('pDrip').checked;
    $('pYearsLbl').textContent = years;
    const need = y > 0 ? target * 12 / (y * (1 - TAX)) : 0, nowMo = start * y * (1 - TAX) / 12;
    $('rNeed').textContent = y > 0 ? '฿' + fmt(need, 0) : 'ระบุ Yield';
    $('rGap').textContent = '฿' + fmt(Math.max(0, need - start), 0);
    $('rNow').textContent = `ตอนนี้ได้ประมาณ ฿${fmt(nowMo)}/เดือน`;
    const pct = target ? Math.min(100, nowMo / target * 100) : 0;
    $('rPct').textContent = pct.toFixed(1) + '%'; $('rBar').style.width = pct + '%';
    let v = start, inv = start, hit = null; const L = [], V = [], I = [], D = []; let rows = '';
    const mg = Math.pow(1 + gr, 1 / 12);
    for (let yr = 1; yr <= years; yr++) {
      for (let m = 0; m < 12; m++) {
        v += dca; inv += dca;
        const div = v * y / 12 * (1 - TAX);
        if (drip) v += div;
        v *= mg;
        if (hit === null && v * y * (1 - TAX) / 12 >= target) hit = yr - 1 + (m + 1) / 12;
      }
      const mo = v * y * (1 - TAX) / 12;
      L.push('ปี ' + yr); V.push(v); I.push(inv); D.push(mo);
      rows += `<tr class="${mo >= target ? 'bg-emerald-50/60' : ''}"><td class="td">${yr}</td><td class="td text-right num">฿${fmt(inv, 0)}</td><td class="td text-right num font-medium">฿${fmt(v, 0)}</td><td class="td text-right num font-semibold text-emerald-600">฿${fmt(mo)}</td></tr>`;
    }
    if (nowMo >= target && target > 0) hit = 0;
    $('rYears').textContent = hit === null ? `เกิน ${years} ปี` : hit === 0 ? 'ถึงแล้ว 🎉' : `${hit.toFixed(1)} ปี`;
    $('simTable').innerHTML = rows;
    chart.data.labels = L; chart.data.datasets[0].data = V; chart.data.datasets[1].data = I; chart.data.datasets[2].data = D; chart.update();
  };
  document.querySelectorAll('input').forEach(e => e.addEventListener('input', run));
  run();
})();
</script>
{% endblock %}
"""

TEMPLATES["404.html"] = r"""{% extends "base.html" %}
{% block title %}ไม่พบหน้า{% endblock %}
{% block content %}
<div class="card p-16 text-center">
  <p class="text-6xl font-bold text-slate-200">404</p>
  <p class="mt-4 text-slate-500">ไม่พบหน้าหรือกองทุนที่ต้องการ</p>
  <a href="{{ url_for('dashboard') }}" class="btn-primary mt-6">กลับหน้าแดชบอร์ด</a>
</div>
{% endblock %}
"""

app.jinja_loader = DictLoader(TEMPLATES)
init_db()

if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
