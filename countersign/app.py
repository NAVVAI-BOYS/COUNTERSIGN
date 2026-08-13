"""
Countersign — v1 week one: the countersign loop, end to end.

Concierge model: dg enters vendors and claims via the admin panel.
The named client confirms via magic link. Nothing publishes unless they do.

Env vars:
  ADMIN_KEY        required — admin panel access
  DATABASE_URL     Render Postgres URL (falls back to local SQLite for dev)
  POSTMARK_TOKEN   optional — enables the "send via Countersign" fallback email
  FROM_EMAIL       sender address for the fallback email
  BASE_URL         public URL, e.g. https://countersign-demo.onrender.com
  BRAND_NAME       defaults to "Countersign" (rename costs one env var)
"""

import os
import secrets
import json
import urllib.request
from datetime import datetime, timezone
from functools import wraps

from flask import (Flask, request, redirect, render_template, url_for,
                   session, abort, flash)
from flask_sqlalchemy import SQLAlchemy

# ---------------------------------------------------------------- config
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

db_url = os.environ.get("DATABASE_URL", "sqlite:///countersign.db")
if db_url.startswith("postgres://"):  # Render's older URL scheme
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

BRAND = os.environ.get("BRAND_NAME", "Countersign")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000").rstrip("/")
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
POSTMARK_TOKEN = os.environ.get("POSTMARK_TOKEN", "")
FROM_EMAIL = os.environ.get("FROM_EMAIL", "")

db = SQLAlchemy(app)

GRADES = {
    "client_confirmed": "Client Confirmed",
    "evidence_verified": "Evidence Verified",
    "fully_verified": "Fully Verified",
}

# ---------------------------------------------------------------- models
def now():
    return datetime.now(timezone.utc)


class Vendor(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    record_no = db.Column(db.String(12), unique=True, nullable=False)  # CS-0001
    name = db.Column(db.String(200), nullable=False)
    slug = db.Column(db.String(80), unique=True, nullable=False)
    website = db.Column(db.String(300), default="")
    company_no = db.Column(db.String(60), default="")
    blurb = db.Column(db.String(400), default="")
    contact_email = db.Column(db.String(200), default="")
    login_token = db.Column(db.String(64), unique=True)
    onboard_token = db.Column(db.String(64), unique=True)
    onboarded_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), default=now)
    claims = db.relationship("Claim", backref="vendor", lazy=True)


class Claim(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendor.id"), nullable=False)
    claim_no = db.Column(db.String(16), unique=True, nullable=False)  # CS-0001-01
    text = db.Column(db.Text, nullable=False)              # the claim as stated
    client_company = db.Column(db.String(200), nullable=False)
    relationship_line = db.Column(db.String(300), default="")   # "Client since Feb 2025 · 18 months · renewed once"
    scope_line = db.Column(db.String(300), default="")
    status_line = db.Column(db.String(120), default="")         # Ongoing / Completed
    grade = db.Column(db.String(30), default="client_confirmed")
    evidence_checked = db.Column(db.Text, default="")           # one item per line; empty = none provided
    evidence_notes = db.Column(db.Text, default="")             # what the vendor SAYS they have (self serve intake)
    # countersign
    confirmer_email = db.Column(db.String(200), nullable=False)
    confirmer_name = db.Column(db.String(200), default="")      # filled at confirmation
    confirmer_role = db.Column(db.String(200), default="")
    confirmer_linkedin = db.Column(db.String(300), default="")
    show_confirmer = db.Column(db.Boolean, default=True)        # private confirmation if False
    anon_descriptor = db.Column(db.String(200), default="")     # e.g. "a global technology company" — shown when private
    token = db.Column(db.String(64), unique=True, nullable=False)
    state = db.Column(db.String(20), default="draft")  # submitted|draft|sent|confirmed|corrected|declined
    sent_at = db.Column(db.DateTime(timezone=True))
    resolved_at = db.Column(db.DateTime(timezone=True))
    correction_text = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime(timezone=True), default=now)

    @property
    def grade_label(self):
        return GRADES.get(self.grade, self.grade)

    @property
    def evidence_items(self):
        return [l.strip() for l in (self.evidence_checked or "").splitlines() if l.strip()]

    @property
    def confirm_url(self):
        return f"{BASE_URL}/confirm/{self.token}"


class AuditEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    claim_id = db.Column(db.Integer, db.ForeignKey("claim.id"))
    at = db.Column(db.DateTime(timezone=True), default=now)
    event = db.Column(db.String(300), nullable=False)


def log_event(claim_id, event):
    db.session.add(AuditEvent(claim_id=claim_id, event=event))


class Dispute(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    claim_no = db.Column(db.String(16), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(200), nullable=False)
    company = db.Column(db.String(200), default="")
    detail = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=now)
    handled = db.Column(db.Boolean, default=False)


class InviteRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company = db.Column(db.String(200), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(200), nullable=False)
    sells = db.Column(db.String(400), default="")
    created_at = db.Column(db.DateTime(timezone=True), default=now)
    handled = db.Column(db.Boolean, default=False)


# ---------------------------------------------------------------- helpers
def next_record_no():
    n = db.session.query(db.func.count(Vendor.id)).scalar() + 1
    return f"CS-{n:04d}"


def next_claim_no(vendor):
    n = db.session.query(db.func.count(Claim.id)).filter(
        Claim.vendor_id == vendor.id).scalar() + 1
    return f"{vendor.record_no}-{n:02d}"


def slugify(name):
    s = "".join(c.lower() if c.isalnum() else "-" for c in name)
    while "--" in s:
        s = s.replace("--", "-")
    return s.strip("-")[:80]


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not ADMIN_KEY:
            abort(503)
        if session.get("admin") is True:
            return f(*args, **kwargs)
        key = request.args.get("key") or request.form.get("key")
        if key and secrets.compare_digest(key, ADMIN_KEY):
            session["admin"] = True
            return f(*args, **kwargs)
        return render_template("admin_login.html", brand=BRAND), 401
    return wrapper


def countersign_email_text(claim):
    """The email the vendor sends from their own inbox (primary flow)."""
    vendor = claim.vendor
    return f"""Subject: Can you confirm the facts of our work together? 30 seconds

Hi [first name],

We're putting our track record on the record, literally. We've listed the facts of our work together on {BRAND}, an independent register, and nothing publishes unless you confirm it's accurate.

It takes about 30 seconds, no account needed:

{claim.confirm_url}

You'll see exactly what we've stated. If anything's wrong you can correct it right there.

Thanks,
[your name]
{vendor.name}"""


def onboarding_email_text(vendor):
    return f"""Subject: Your {BRAND} founding onboarding, five minutes

Hi [first name],

Great to have {vendor.name} in the founding fifty. Here's your onboarding link:

{BASE_URL}/onboard/{vendor.onboard_token}

It takes about five minutes: your company details, then the two or three claims
you actually use in sales, and who at each client would confirm them.

We review everything before anything moves. Nothing publishes until your client
has confirmed it, and nothing negative can ever be recorded.

Best,
dg
{BRAND}"""


def send_via_postmark(claim):
    """Fallback: the DocuSign-style email from the register itself."""
    if not (POSTMARK_TOKEN and FROM_EMAIL):
        return False, "Postmark not configured (POSTMARK_TOKEN / FROM_EMAIL)"
    vendor = claim.vendor
    body = {
        "From": FROM_EMAIL,
        "To": claim.confirmer_email,
        "Subject": f"{vendor.name} has asked you to confirm the facts of your work together",
        "TextBody": (
            f"{vendor.name} has listed the facts of your working relationship on {BRAND}, "
            f"an independent register. Nothing publishes unless you confirm it is accurate.\n\n"
            f"Review and confirm (about 30 seconds, no account needed):\n{claim.confirm_url}\n\n"
            f"If anything is wrong you can correct it on the same page.\n\n"
            f"{BRAND} · facts only, never opinions · nothing negative is ever recorded"
        ),
        "MessageStream": "outbound",
    }
    req = urllib.request.Request(
        "https://api.postmarkapp.com/email",
        data=json.dumps(body).encode(),
        headers={"Accept": "application/json",
                 "Content-Type": "application/json",
                 "X-Postmark-Server-Token": POSTMARK_TOKEN},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200, f"Postmark status {r.status}"
    except Exception as e:
        return False, f"Postmark error: {e}"


# ---------------------------------------------------------------- public
@app.get("/")
def home():
    confirmed = Claim.query.filter_by(state="confirmed").count()
    vendors = db.session.query(db.func.count(Vendor.id)).scalar()
    return render_template("home.html", brand=BRAND,
                           confirmed=confirmed, vendors=vendors)


@app.get("/registry")
def registry():
    vendors = Vendor.query.order_by(Vendor.created_at.desc()).all()
    visible = [v for v in vendors if any(c.state == "confirmed" for c in v.claims)]
    return render_template("registry.html", brand=BRAND, vendors=visible)


@app.get("/standard")
def standard():
    return render_template("standard.html", brand=BRAND)


@app.get("/invite")
def invite_redirect():
    return redirect(url_for("signup"))


@app.get("/signup")
def signup():
    return render_template("invite.html", brand=BRAND)


@app.post("/signup")
def signup_submit():
    company = (request.form.get("company") or "").strip()
    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip()
    if not (company and name and email):
        flash("Company, name and email are all needed.")
        return render_template("invite.html", brand=BRAND), 400
    db.session.add(InviteRequest(company=company, name=name, email=email,
                                 sells=(request.form.get("sells") or "").strip()))
    db.session.commit()
    return render_template("invite_thanks.html", brand=BRAND, company=company)


@app.get("/r/<slug>")
def proof_page(slug):
    vendor = Vendor.query.filter_by(slug=slug).first_or_404()
    claims = [c for c in vendor.claims if c.state == "confirmed"]
    claims.sort(key=lambda c: c.resolved_at or c.created_at, reverse=True)
    if not claims:
        abort(404)
    return render_template("proof.html", brand=BRAND, vendor=vendor,
                           claims=claims, grades=GRADES)


@app.get("/check")
def check():
    q = (request.args.get("q") or "").strip().upper()
    result = None
    searched = False
    if q:
        searched = True
        vendor = Vendor.query.filter_by(record_no=q).first()
        if not vendor and "-" in q:
            claim = Claim.query.filter_by(claim_no=q).first()
            vendor = claim.vendor if claim and claim.state == "confirmed" else None
        if vendor and any(c.state == "confirmed" for c in vendor.claims):
            result = vendor
    return render_template("check.html", brand=BRAND, q=q,
                           searched=searched, result=result)


# ---------------------------------------------------------------- sample
@app.get("/sample")
def sample():
    from types import SimpleNamespace as NS
    from datetime import datetime as dt
    vendor = NS(record_no="CS-0000", name="Meridian Data Services",
                slug="_sample", website="https://example.com",
                company_no="00000000",
                blurb="Data engineering consultancy, specimen record")
    claims = [
        NS(claim_no="CS-0000-01",
           text="Meridian has run the data platform migration and ongoing pipeline operations for Harbourline Logistics since January 2024.",
           client_company="Harbourline Logistics",
           relationship_line="Client since January 2024 · 19 months · renewed once",
           scope_line="Data platform migration, pipeline operations",
           status_line="Ongoing", grade="fully_verified",
           grade_label="Fully Verified",
           evidence_items=["Signed master services agreement, dated January 2024",
                           "Invoice history showing 19 consecutive months of payments",
                           "Renewal recorded January 2025"],
           show_confirmer=True, anon_descriptor="",
           confirmer_name="Sarah Whitmore", confirmer_role="Chief Operating Officer",
           confirmer_linkedin="", resolved_at=dt(2026, 8, 1)),
        NS(claim_no="CS-0000-02",
           text="Meridian delivered a reporting automation project that the client operates independently today.",
           client_company="",
           relationship_line="Project completed March 2026",
           scope_line="Reporting automation", status_line="Completed",
           grade="evidence_verified", grade_label="Evidence Verified",
           evidence_items=["Statement of work, dated November 2025",
                           "Project completion sign off, March 2026"],
           show_confirmer=False, anon_descriptor="a national retail group",
           confirmer_name="", confirmer_role="", confirmer_linkedin="",
           resolved_at=dt(2026, 7, 14)),
        NS(claim_no="CS-0000-03",
           text="Meridian provides ad hoc data advisory to Bright & Co Accountants.",
           client_company="Bright & Co Accountants",
           relationship_line="Client since May 2026",
           scope_line="Data advisory", status_line="Ongoing",
           grade="client_confirmed", grade_label="Client Confirmed",
           evidence_items=[],
           show_confirmer=True, anon_descriptor="",
           confirmer_name="James Bright", confirmer_role="Managing Partner",
           confirmer_linkedin="", resolved_at=dt(2026, 8, 9)),
    ]
    return render_template("proof.html", brand=BRAND, vendor=vendor,
                           claims=claims, grades=GRADES, specimen=True)


# ------------------------------------------------------- badge + machine
@app.get("/badge/<record_no>.svg")
def badge(record_no):
    vendor = Vendor.query.filter_by(record_no=record_no.upper()).first_or_404()
    claims = [c for c in vendor.claims if c.state == "confirmed"]
    if not claims:
        abort(404)
    best = min(c.grade for c in claims)  # alphabetical luck: client< evidence< fully — fix properly:
    order = {"fully_verified": 0, "evidence_verified": 1, "client_confirmed": 2}
    top = sorted(claims, key=lambda c: order.get(c.grade, 3))[0]
    label = GRADES[top.grade].upper()
    svg = f"""<svg xmlns='http://www.w3.org/2000/svg' width='236' height='44' role='img' aria-label='{BRAND} {label}'>
<rect width='236' height='44' fill='#16254E'/>
<rect x='0' y='0' width='4' height='44' fill='#6C1D45'/>
<g transform='translate(14,10)'>
  <circle cx='12' cy='12' r='10.5' stroke='#F5F7FC' stroke-width='1.4' fill='none'/>
  <path d='M8.2 12.3l2.5 2.5 5-5.4' stroke='#F5F7FC' stroke-width='1.7' fill='none' stroke-linecap='round' stroke-linejoin='round'/>
</g>
<text x='46' y='19' font-family='Helvetica,Arial,sans-serif' font-size='11' font-weight='700' fill='#F5F7FC' letter-spacing='1.5'>{BRAND.upper()}</text>
<text x='46' y='33' font-family='Helvetica,Arial,sans-serif' font-size='9' fill='#C8D0E4' letter-spacing='1.2'>{label} · {vendor.record_no}</text>
</svg>"""
    return svg, 200, {"Content-Type": "image/svg+xml",
                      "Cache-Control": "public, max-age=3600"}


@app.get("/r/<slug>.json")
def record_json(slug):
    vendor = Vendor.query.filter_by(slug=slug).first_or_404()
    claims = [c for c in vendor.claims if c.state == "confirmed"]
    if not claims:
        abort(404)
    return {
        "register": BRAND,
        "record": vendor.record_no,
        "company": vendor.name,
        "company_number": vendor.company_no or None,
        "website": vendor.website or None,
        "verify_url": f"{BASE_URL}/check?q={vendor.record_no}",
        "claims": [{
            "claim_no": c.claim_no,
            "statement": c.text,
            "client": c.client_company if c.show_confirmer else (c.anon_descriptor or "confirmed privately"),
            "grade": c.grade,
            "grade_label": c.grade_label,
            "evidence_checked": c.evidence_items,
            "confirmed_on": c.resolved_at.date().isoformat() if c.resolved_at else None,
            "confirmed_by": (f"{c.confirmer_name}, {c.confirmer_role}" if c.show_confirmer
                             else "verified client contact, identity withheld at the client's request"),
        } for c in claims],
        "standard": f"{BASE_URL}/standard",
    }


@app.get("/llms.txt")
def llms_txt():
    vendors = Vendor.query.all()
    live = [v for v in vendors if any(c.state == "confirmed" for c in v.claims)]
    lines = [f"# {BRAND}",
             "",
             f"> {BRAND} is an open register of B2B claims confirmed by the named client before publication. Facts only, never opinions. Every record shows what evidence was checked.",
             "",
             "## Records"]
    lines += [f"- [{v.name}]({BASE_URL}/r/{v.slug}.json): {v.record_no}" for v in live]
    lines += ["", f"## Standard", f"- [Grades and rules]({BASE_URL}/standard)",
              f"- Verify any record: {BASE_URL}/check"]
    return "\n".join(lines), 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.get("/dispute")
def dispute_form():
    claim_no = (request.args.get("claim") or "").strip().upper()
    return render_template("dispute.html", brand=BRAND, claim_no=claim_no)


@app.post("/dispute")
def dispute_submit():
    claim_no = (request.form.get("claim_no") or "").strip().upper()
    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip()
    detail = (request.form.get("detail") or "").strip()
    if not (claim_no and name and email and detail):
        flash("Claim number, your name, email and the detail are all needed.")
        return render_template("dispute.html", brand=BRAND, claim_no=claim_no), 400
    db.session.add(Dispute(claim_no=claim_no, name=name, email=email,
                           company=(request.form.get("company") or "").strip(),
                           detail=detail))
    db.session.commit()
    return render_template("dispute_thanks.html", brand=BRAND)


@app.get("/r/<slug>/case-study.pdf")
def case_study_pdf(slug):
    vendor = Vendor.query.filter_by(slug=slug).first_or_404()
    claims = [c for c in vendor.claims if c.state == "confirmed"]
    if not claims:
        abort(404)
    from io import BytesIO
    import qrcode
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib.colors import HexColor
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                    HRFlowable, Table, TableStyle, Image as RLImage)
    from reportlab.lib.styles import ParagraphStyle

    NAVY = HexColor("#16254E"); MAROON = HexColor("#6C1D45")
    INK = HexColor("#1C2440"); SOFT = HexColor("#5A6480"); RULE = HexColor("#CBD3E4")

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=20*mm, rightMargin=20*mm,
                            topMargin=18*mm, bottomMargin=16*mm,
                            title=f"{vendor.name} — {BRAND} verified record")
    st_title = ParagraphStyle("t", fontName="Times-Bold", fontSize=22, leading=26, textColor=NAVY, spaceAfter=2)
    st_eyebrow = ParagraphStyle("e", fontName="Helvetica-Bold", fontSize=8.5, textColor=MAROON, spaceAfter=6)
    st_body = ParagraphStyle("b", fontName="Helvetica", fontSize=9.5, leading=13.5, textColor=SOFT)
    st_claim = ParagraphStyle("c", fontName="Times-Bold", fontSize=12, leading=16, textColor=INK, spaceAfter=4)
    st_meta = ParagraphStyle("m", fontName="Helvetica", fontSize=8.5, leading=12, textColor=SOFT)
    st_conf = ParagraphStyle("cf", fontName="Helvetica-Bold", fontSize=9, leading=12.5, textColor=NAVY)

    els = []
    els.append(Paragraph(f"{BRAND.upper()} · VERIFIED RECORD · {vendor.record_no}", st_eyebrow))
    els.append(Paragraph(vendor.name, st_title))
    els.append(Paragraph(
        (vendor.blurb + " · " if vendor.blurb else "") +
        (f"Company No. {vendor.company_no} · " if vendor.company_no else "") +
        "Every claim below was confirmed by the named client before publication.", st_body))
    els.append(Spacer(1, 6))
    els.append(HRFlowable(width="100%", thickness=1.4, color=NAVY, spaceAfter=10))

    for c in claims:
        els.append(Paragraph(f"{c.claim_no} · {c.grade_label.upper()}", st_eyebrow))
        els.append(Paragraph(c.text, st_claim))
        meta = []
        if c.show_confirmer:
            meta.append(f"Client: {c.client_company}")
        elif c.anon_descriptor:
            meta.append(f"Client: {c.anon_descriptor} (named privately)")
        if c.relationship_line: meta.append(f"Relationship: {c.relationship_line}")
        if c.scope_line: meta.append(f"Scope: {c.scope_line}")
        els.append(Paragraph(" · ".join(meta), st_meta))
        if c.evidence_items:
            els.append(Paragraph("Evidence checked: " + "; ".join(c.evidence_items), st_meta))
        else:
            els.append(Paragraph("Evidence: none provided — this claim rests on the client's confirmation alone.", st_meta))
        if c.show_confirmer:
            els.append(Paragraph(f"Countersigned by {c.confirmer_name}, {c.confirmer_role} at {c.client_company}, "
                                 f"{c.resolved_at.strftime('%d %B %Y')}, from a company email address.", st_conf))
        else:
            els.append(Paragraph(f"Countersigned privately by a verified client contact, {c.resolved_at.strftime('%d %B %Y')}. "
                                 f"Identity verified by {BRAND} and withheld at the client's request.", st_conf))
        els.append(Spacer(1, 5))
        els.append(HRFlowable(width="100%", thickness=0.6, color=RULE, spaceAfter=8))

    qr = qrcode.make(f"{BASE_URL}/check?q={vendor.record_no}", box_size=4, border=1)
    qb = BytesIO(); qr.save(qb); qb.seek(0)
    foot = Table([[RLImage(qb, width=52, height=52),
                   Paragraph(f"<b>Check this record.</b><br/>Scan, or enter {vendor.record_no} at {BASE_URL.replace('https://','')}/check. "
                             f"A record that does not exist returns no result. Facts only, never opinions. "
                             f"Evidence is checked, then deleted.", st_body)]],
                 colWidths=[62, None])
    foot.setStyle(TableStyle([("VALIGN", (0,0), (-1,-1), "TOP")]))
    els.append(foot)
    doc.build(els)
    buf.seek(0)
    return buf.getvalue(), 200, {
        "Content-Type": "application/pdf",
        "Content-Disposition": f'inline; filename="{vendor.slug}-{BRAND.lower()}-record.pdf"'}


# ------------------------------------------------------------ countersign
@app.get("/confirm/<token>")
def confirm_view(token):
    claim = Claim.query.filter_by(token=token).first_or_404()
    if claim.state in ("confirmed", "corrected", "declined"):
        return render_template("already_done.html", brand=BRAND, claim=claim)
    return render_template("confirm.html", brand=BRAND, claim=claim)


@app.post("/confirm/<token>")
def confirm_submit(token):
    claim = Claim.query.filter_by(token=token).first_or_404()
    if claim.state in ("confirmed", "corrected", "declined"):
        return render_template("already_done.html", brand=BRAND, claim=claim)

    action = request.form.get("action")
    name = (request.form.get("name") or "").strip()
    role = (request.form.get("role") or "").strip()
    linkedin = (request.form.get("linkedin") or "").strip()
    show = request.form.get("show_confirmer") == "yes"

    if action == "confirm":
        if not name or not role:
            flash("Please add your name and role so the record shows who confirmed it.")
            return render_template("confirm.html", brand=BRAND, claim=claim), 400
        claim.confirmer_name = name
        claim.confirmer_role = role
        claim.confirmer_linkedin = linkedin
        claim.show_confirmer = show
        claim.state = "confirmed"
        claim.resolved_at = now()
        log_event(claim.id, f"Confirmed by {name} ({role})" + ("" if show else " — private"))
        db.session.commit()
        return render_template("thanks.html", brand=BRAND, claim=claim)

    if action == "correct":
        correction = (request.form.get("correction") or "").strip()
        if not correction:
            flash("Tell us what's wrong and we'll fix it before anything publishes.")
            return render_template("confirm.html", brand=BRAND, claim=claim), 400
        claim.correction_text = correction
        claim.confirmer_name = name
        claim.confirmer_role = role
        claim.state = "corrected"
        claim.resolved_at = now()
        log_event(claim.id, "Correction submitted; claim returned to vendor")
        db.session.commit()
        return render_template("thanks_correction.html", brand=BRAND, claim=claim)

    abort(400)


# ---------------------------------------------------------------- login
@app.get("/login")
def login():
    return render_template("login.html", brand=BRAND)


@app.post("/login")
def login_submit():
    email = (request.form.get("email") or "").strip().lower()
    vendor = Vendor.query.filter(db.func.lower(Vendor.contact_email) == email).first() if email else None
    if not vendor:
        # same message either way: never reveal which emails exist
        return render_template("login_sent.html", brand=BRAND, email=email,
                               demo_link=None)
    vendor.login_token = secrets.token_urlsafe(32)
    db.session.commit()
    link = f"{BASE_URL}/auth/{vendor.login_token}"
    demo_link = None
    if POSTMARK_TOKEN and FROM_EMAIL:
        body = {"From": FROM_EMAIL, "To": email,
                "Subject": f"Your {BRAND} sign in link",
                "TextBody": f"Sign in to {BRAND}:\n{link}\n\nThis link works once.",
                "MessageStream": "outbound"}
        req = urllib.request.Request("https://api.postmarkapp.com/email",
            data=json.dumps(body).encode(),
            headers={"Accept": "application/json", "Content-Type": "application/json",
                     "X-Postmark-Server-Token": POSTMARK_TOKEN}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=15)
        except Exception:
            pass
    else:
        demo_link = link      # email not configured yet: show the link (pre-launch mode)
    return render_template("login_sent.html", brand=BRAND, email=email,
                           demo_link=demo_link)


@app.get("/auth/<token>")
def auth(token):
    vendor = Vendor.query.filter_by(login_token=token).first_or_404()
    vendor.login_token = None            # single use
    db.session.commit()
    session["vendor_id"] = vendor.id
    return redirect(url_for("portal"))


@app.get("/logout")
def logout():
    session.pop("vendor_id", None)
    return redirect(url_for("home"))


def current_vendor():
    vid = session.get("vendor_id")
    return db.session.get(Vendor, vid) if vid else None


@app.get("/portal")
def portal():
    vendor = current_vendor()
    if not vendor:
        return redirect(url_for("login"))
    claims = sorted(vendor.claims, key=lambda c: c.id, reverse=True)
    live = [c for c in claims if c.state == "confirmed"]
    return render_template("portal.html", brand=BRAND, vendor=vendor,
                           claims=claims, live=live)


@app.post("/portal/claim")
def portal_add_claim():
    vendor = current_vendor()
    if not vendor:
        return redirect(url_for("login"))
    text = (request.form.get("text") or "").strip()
    client = (request.form.get("client") or "").strip()
    email = (request.form.get("email") or "").strip()
    if not (text and client and email):
        flash("The claim, the client, and who confirms it are all needed.")
        return redirect(url_for("portal"))
    c = Claim(vendor_id=vendor.id, claim_no=next_claim_no(vendor),
              text=text, client_company=client, confirmer_email=email,
              relationship_line=(request.form.get("relationship") or "").strip(),
              scope_line=(request.form.get("scope") or "").strip(),
              status_line=(request.form.get("status") or "").strip(),
              evidence_notes=(request.form.get("evidence") or "").strip(),
              state="submitted", token=secrets.token_urlsafe(32))
    db.session.add(c)
    db.session.flush()
    log_event(c.id, "Submitted by the vendor through the portal")
    db.session.commit()
    flash(f"Claim {c.claim_no} submitted for review. We'll be in touch before anything moves.")
    return redirect(url_for("portal"))


# ------------------------------------------------------------- onboarding
@app.get("/onboard/<token>")
def onboard_view(token):
    vendor = Vendor.query.filter_by(onboard_token=token).first_or_404()
    return render_template("onboard.html", brand=BRAND, vendor=vendor)


@app.post("/onboard/<token>")
def onboard_submit(token):
    vendor = Vendor.query.filter_by(onboard_token=token).first_or_404()
    vendor.website = (request.form.get("website") or vendor.website or "").strip()
    vendor.company_no = (request.form.get("company_no") or vendor.company_no or "").strip()
    vendor.blurb = (request.form.get("blurb") or vendor.blurb or "").strip()

    added = 0
    for i in (1, 2, 3):
        text = (request.form.get(f"claim{i}_text") or "").strip()
        client = (request.form.get(f"claim{i}_client") or "").strip()
        email = (request.form.get(f"claim{i}_email") or "").strip()
        if not text:
            continue
        if not (client and email):
            flash(f"Claim {i} needs the client company and a confirmer email.")
            return render_template("onboard.html", brand=BRAND, vendor=vendor), 400
        c = Claim(vendor_id=vendor.id,
                  claim_no=next_claim_no(vendor),
                  text=text, client_company=client, confirmer_email=email,
                  relationship_line=(request.form.get(f"claim{i}_relationship") or "").strip(),
                  scope_line=(request.form.get(f"claim{i}_scope") or "").strip(),
                  status_line=(request.form.get(f"claim{i}_status") or "").strip(),
                  evidence_notes=(request.form.get(f"claim{i}_evidence") or "").strip(),
                  state="submitted",
                  token=secrets.token_urlsafe(32))
        db.session.add(c)
        db.session.flush()
        log_event(c.id, "Submitted by the vendor through onboarding")
        added += 1

    if added == 0:
        flash("Add at least one claim, that's the whole point!")
        return render_template("onboard.html", brand=BRAND, vendor=vendor), 400

    vendor.onboarded_at = now()
    vendor.onboard_token = None          # single use
    db.session.commit()
    session["vendor_id"] = vendor.id     # they're in: portal from here on
    return render_template("onboard_thanks.html", brand=BRAND,
                           vendor=vendor, added=added)


# ---------------------------------------------------------------- admin
@app.get("/admin")
@admin_required
def admin_home():
    vendors = Vendor.query.order_by(Vendor.created_at.desc()).all()
    pending = Claim.query.filter(Claim.state.in_(["draft", "sent"])).count()
    corrected = Claim.query.filter_by(state="corrected").count()
    confirmed = Claim.query.filter_by(state="confirmed").count()
    invites = InviteRequest.query.filter_by(handled=False).order_by(
        InviteRequest.created_at.desc()).all()
    disputes = Dispute.query.filter_by(handled=False).order_by(
        Dispute.created_at.desc()).all()
    return render_template("admin.html", brand=BRAND, vendors=vendors,
                           pending=pending, corrected=corrected,
                           confirmed=confirmed, invites=invites,
                           disputes=disputes)


@app.post("/admin/vendor")
@admin_required
def admin_add_vendor():
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Vendor name is required.")
        return redirect(url_for("admin_home"))
    v = Vendor(record_no=next_record_no(), name=name,
               slug=slugify(name),
               contact_email=(request.form.get("contact_email") or "").strip(),
               website=(request.form.get("website") or "").strip(),
               company_no=(request.form.get("company_no") or "").strip(),
               blurb=(request.form.get("blurb") or "").strip())
    db.session.add(v)
    db.session.commit()
    flash(f"{v.name} added as {v.record_no}.")
    return redirect(url_for("admin_vendor", vendor_id=v.id))


@app.get("/admin/vendor/<int:vendor_id>")
@admin_required
def admin_vendor(vendor_id):
    vendor = Vendor.query.get_or_404(vendor_id)
    return render_template("admin_vendor.html", brand=BRAND, vendor=vendor,
                           grades=GRADES, base_url=BASE_URL,
                           onboard_email=onboarding_email_text(vendor) if vendor.onboard_token else None,
                           email_texts={c.id: countersign_email_text(c)
                                        for c in vendor.claims})


@app.post("/admin/vendor/<int:vendor_id>/claim")
@admin_required
def admin_add_claim(vendor_id):
    vendor = Vendor.query.get_or_404(vendor_id)
    text = (request.form.get("text") or "").strip()
    client = (request.form.get("client_company") or "").strip()
    email = (request.form.get("confirmer_email") or "").strip()
    if not (text and client and email):
        flash("Claim text, client company and confirmer email are all required.")
        return redirect(url_for("admin_vendor", vendor_id=vendor.id))
    c = Claim(vendor_id=vendor.id,
              claim_no=next_claim_no(vendor),
              text=text, client_company=client, confirmer_email=email,
              relationship_line=(request.form.get("relationship_line") or "").strip(),
              scope_line=(request.form.get("scope_line") or "").strip(),
              status_line=(request.form.get("status_line") or "").strip(),
              grade=request.form.get("grade", "client_confirmed"),
              evidence_checked=(request.form.get("evidence_checked") or "").strip(),
              anon_descriptor=(request.form.get("anon_descriptor") or "").strip(),
              token=secrets.token_urlsafe(32))
    db.session.add(c)
    db.session.flush()
    log_event(c.id, f"Claim drafted as {c.claim_no}")
    db.session.commit()
    flash(f"Claim {c.claim_no} drafted. Send the countersign request when ready.")
    return redirect(url_for("admin_vendor", vendor_id=vendor.id))


@app.post("/admin/claim/<int:claim_id>/mark-sent")
@admin_required
def admin_mark_sent(claim_id):
    claim = Claim.query.get_or_404(claim_id)
    claim.state = "sent"
    claim.sent_at = now()
    log_event(claim.id, "Countersign request sent from vendor's own inbox")
    db.session.commit()
    flash(f"{claim.claim_no} marked as sent.")
    return redirect(url_for("admin_vendor", vendor_id=claim.vendor_id))


@app.post("/admin/claim/<int:claim_id>/send-fallback")
@admin_required
def admin_send_fallback(claim_id):
    claim = Claim.query.get_or_404(claim_id)
    ok, msg = send_via_postmark(claim)
    if ok:
        claim.state = "sent"
        claim.sent_at = now()
        log_event(claim.id, f"Countersign request emailed via {BRAND} (Postmark)")
        db.session.commit()
        flash(f"Sent to {claim.confirmer_email}.")
    else:
        flash(msg)
    return redirect(url_for("admin_vendor", vendor_id=claim.vendor_id))


@app.post("/admin/claim/<int:claim_id>/reopen")
@admin_required
def admin_reopen(claim_id):
    """After a correction: fix the facts, reset to draft, re-send."""
    claim = Claim.query.get_or_404(claim_id)
    for field in ("text", "relationship_line", "scope_line",
                  "status_line", "evidence_checked"):
        if request.form.get(field) is not None:
            setattr(claim, field, request.form.get(field).strip())
    if request.form.get("grade"):
        claim.grade = request.form.get("grade")
    claim.state = "draft"
    claim.token = secrets.token_urlsafe(32)   # old link dies with the old facts
    claim.correction_text = ""
    claim.resolved_at = None
    log_event(claim.id, "Facts revised after correction; new countersign link issued")
    db.session.commit()
    flash(f"{claim.claim_no} revised. Send the new request when ready.")
    return redirect(url_for("admin_vendor", vendor_id=claim.vendor_id))


@app.post("/admin/invite/<int:invite_id>/approve")
@admin_required
def admin_invite_approve(invite_id):
    inv = db.session.get(InviteRequest, invite_id) or abort(404)
    v = Vendor(record_no=next_record_no(), name=inv.company,
               slug=slugify(inv.company), blurb=inv.sells or "",
               contact_email=inv.email,
               onboard_token=secrets.token_urlsafe(32))
    inv.handled = True
    db.session.add(v)
    db.session.commit()
    flash(f"{v.name} approved as {v.record_no}. Copy the onboarding link below and send it to {inv.name} ({inv.email}).")
    return redirect(url_for("admin_vendor", vendor_id=v.id))


@app.post("/admin/vendor/<int:vendor_id>/onboard-link")
@admin_required
def admin_onboard_link(vendor_id):
    vendor = Vendor.query.get_or_404(vendor_id)
    vendor.onboard_token = secrets.token_urlsafe(32)
    db.session.commit()
    flash("Onboarding link generated.")
    return redirect(url_for("admin_vendor", vendor_id=vendor.id))


@app.post("/admin/claim/<int:claim_id>/review")
@admin_required
def admin_review_claim(claim_id):
    """Self serve submission reviewed: facts finalised, graded, moved to draft."""
    claim = Claim.query.get_or_404(claim_id)
    for field in ("text", "relationship_line", "scope_line",
                  "status_line", "evidence_checked"):
        if request.form.get(field) is not None:
            setattr(claim, field, request.form.get(field).strip())
    claim.grade = request.form.get("grade", claim.grade)
    claim.state = "draft"
    log_event(claim.id, "Reviewed and approved for countersign")
    db.session.commit()
    flash(f"{claim.claim_no} approved. Send the countersign request when ready.")
    return redirect(url_for("admin_vendor", vendor_id=claim.vendor_id))


@app.post("/admin/invite/<int:invite_id>/handled")
@admin_required
def admin_invite_handled(invite_id):
    inv = db.session.get(InviteRequest, invite_id) or abort(404)
    inv.handled = True
    db.session.commit()
    return redirect(url_for("admin_home"))


@app.post("/admin/dispute/<int:dispute_id>/handled")
@admin_required
def admin_dispute_handled(dispute_id):
    d = db.session.get(Dispute, dispute_id) or abort(404)
    d.handled = True
    db.session.commit()
    return redirect(url_for("admin_home"))


@app.get("/healthz")
def healthz():
    return {"status": "ok", "admin_key_set": bool(ADMIN_KEY),
            "postmark_configured": bool(POSTMARK_TOKEN and FROM_EMAIL)}, 200


with app.app_context():
    db.create_all()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
