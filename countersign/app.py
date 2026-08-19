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

STANDARD_VERSION = "1.0"   # bump whenever the published standard's rules change

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
    widget_interest = db.Column(db.Boolean, default=False)   # wants the website embed when it ships
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
    client_sentence = db.Column(db.String(400), default="")     # optional, the countersigner's own words
    sentence_review = db.Column(db.String(20), default="")      # "", pending, approved, declined
    marketing_opt_in = db.Column(db.Boolean, default=False)     # confirmer consent, captured at confirmation
    standard_version = db.Column(db.String(10), default="")     # standard in force when countersigned
    has_outcome = db.Column(db.Boolean, default=False)          # claim contains an outcome figure
    value_made = db.Column(db.String(300), default="")          # client's words: what it made them
    value_saved = db.Column(db.String(300), default="")         # client's words: what it saved them
    value_optimized = db.Column(db.String(300), default="")     # client's words: what it optimized
    sentence_ai_note = db.Column(db.Text, default="")
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


class SupportMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=False)
    context = db.Column(db.String(200), default="")     # page they came from
    ai_draft = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime(timezone=True), default=now)
    handled = db.Column(db.Boolean, default=False)


class EvidenceFile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    claim_id = db.Column(db.Integer, db.ForeignKey("claim.id"), nullable=False)
    claim = db.relationship("Claim", backref="evidence_files")
    filename = db.Column(db.String(300), nullable=False)
    mimetype = db.Column(db.String(100), default="application/octet-stream")
    data = db.Column(db.LargeBinary, nullable=False)     # held ONLY until review
    ai_summary = db.Column(db.Text, default="")
    fingerprint = db.Column(db.String(64), default="")          # sha256 — survives deletion in the audit log
    uploaded_at = db.Column(db.DateTime(timezone=True), default=now)


MAX_EVIDENCE = 10 * 1024 * 1024

def _attach_evidence(claim, files):
    for f in files:
        if not f or not f.filename:
            continue
        data = f.read()
        if not data or len(data) > MAX_EVIDENCE:
            continue
        import hashlib
        db.session.add(EvidenceFile(claim_id=claim.id, filename=f.filename[:300],
                                    mimetype=f.mimetype or "application/octet-stream",
                                    fingerprint=hashlib.sha256(data).hexdigest(),
                                    data=data))


class ProofEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendor.id"), nullable=False)
    kind = db.Column(db.String(30), nullable=False)   # view, linkedin_click, check_click, pdf, json, badge
    referrer = db.Column(db.String(500), default="")
    at = db.Column(db.DateTime(timezone=True), default=now)


class EvidenceFingerprint(db.Model):
    """Retained forever after the document is deleted: proof of what was checked, never the document."""
    id = db.Column(db.Integer, primary_key=True)
    claim_id = db.Column(db.Integer, db.ForeignKey("claim.id"), nullable=False)
    filename = db.Column(db.String(300), nullable=False)
    sha256 = db.Column(db.String(64), nullable=False)
    extracted_facts = db.Column(db.Text, default="")   # the AI's non-sensitive fact summary
    deleted_at = db.Column(db.DateTime(timezone=True), default=now)


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
    places_left = max(0, 100 - (vendors or 0))
    return render_template("home.html", brand=BRAND,
                           confirmed=confirmed, vendors=vendors,
                           places_left=places_left)


@app.get("/registry")
def registry():
    vendors = Vendor.query.order_by(Vendor.created_at.desc()).all()
    visible = [v for v in vendors if any(c.state == "confirmed" for c in v.claims)]
    return render_template("registry.html", brand=BRAND, vendors=visible)


@app.get("/numbers")
def register_numbers():
    all_claims = Claim.query.all()
    live = [c for c in all_claims if c.state == "confirmed"]
    vendors_live = len({c.vendor_id for c in live})
    sent_ever = sum(1 for c in all_claims if c.state in ("sent", "confirmed", "corrected"))
    completion = round(100 * len(live) / sent_ever) if sent_ever else None
    corrections = AuditEvent.query.filter(AuditEvent.event.like("Correction%")).count()
    declined = sum(1 for c in all_claims if c.state == "declined")
    sentences_declined = AuditEvent.query.filter(AuditEvent.event.like("%sentence declined%")).count()
    return render_template("numbers.html", brand=BRAND, vendors=vendors_live,
                           claims=len(live), corrections=corrections,
                           declined=declined, sentences_declined=sentences_declined,
                           completion=completion)


@app.get("/standard")
def standard():
    return render_template("standard.html", brand=BRAND)


@app.get("/invite")
def invite_redirect():
    return redirect(url_for("signup"))


@app.get("/signup")
def signup():
    places_left = max(0, 100 - Vendor.query.count())
    return render_template("invite.html", brand=BRAND, places_left=places_left)


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
    _track(vendor, "view")
    claims = [c for c in vendor.claims if c.state == "confirmed"]
    claims.sort(key=lambda c: c.resolved_at or c.created_at, reverse=True)
    if not claims:
        abort(404)
    return render_template("proof.html", brand=BRAND, vendor=vendor,
                           claims=claims, grades=GRADES, base_url=BASE_URL)


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


# ------------------------------------------------------------------- ai
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

def ai_call(messages, system=""):
    """Call the Anthropic API. Returns text or None if no key/failed."""
    if not ANTHROPIC_API_KEY:
        return None
    import json as _json, urllib.request
    body = {"model": "claude-sonnet-4-6", "max_tokens": 1500, "messages": messages}
    if system:
        body["system"] = system
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=_json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "x-api-key": ANTHROPIC_API_KEY,
                 "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            out = _json.loads(r.read())
        return "".join(b.get("text", "") for b in out.get("content", []) if b.get("type") == "text")
    except Exception:
        return None


SUPPORT_SYSTEM = f"""You draft replies for {BRAND}, an open register of B2B claims confirmed by the named client before publication. Rules you answer from: facts only, never opinions or ratings; three grades (Client Confirmed, Evidence Verified, Fully Verified); evidence is checked then deleted, never stored; confirmations come from the client's company email; private confirmation is possible with identity held by the register; corrections and disputes are always available; founding membership is free. Write a short, plain, warm reply in UK English. No hyphens or em dashes. If the question needs a human decision (pricing exceptions, legal, complaints), say the team will come back to them and do not invent policy. Sign off as The {BRAND} team."""


def ai_read_evidence(ev):
    """Extract checkable facts from an evidence file. Returns text or None."""
    import base64
    content = []
    if ev.mimetype == "application/pdf":
        content.append({"type": "document", "source": {"type": "base64",
                        "media_type": "application/pdf",
                        "data": base64.b64encode(ev.data).decode()}})
    elif ev.mimetype.startswith("image/"):
        content.append({"type": "image", "source": {"type": "base64",
                        "media_type": ev.mimetype,
                        "data": base64.b64encode(ev.data).decode()}})
    else:
        try:
            content.append({"type": "text", "text": ev.data.decode("utf-8", "ignore")[:20000]})
        except Exception:
            return None
    content.append({"type": "text", "text":
        "This document was supplied as evidence for a claim on a verification register. "
        "List only the checkable facts it establishes: parties named, dates, durations, amounts or counts, "
        "what was agreed or paid, signatures present. Then state in one line what grade of support it gives "
        "(contract evidence, payment evidence, both, or neither). Flag anything that looks inconsistent or edited. "
        "Plain text, short lines, no preamble."})
    return ai_call([{"role": "user", "content": content}])


# ---------------------------------------------------------------- sample
def _specimen():
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
           grade_label="Fully Verified", standard_version="1.0",
           evidence_items=["Signed master services agreement, dated January 2024",
                           "Invoice history showing 19 consecutive months of payments",
                           "Renewal recorded January 2025"],
           show_confirmer=True, anon_descriptor="",
           confirmer_name="Sarah Whitmore", confirmer_role="Chief Operating Officer",
           confirmer_linkedin="https://www.linkedin.com/", resolved_at=dt(2026, 8, 1),
           client_sentence="Meridian took over a migration two previous suppliers had failed to land, and the pipelines have run without an incident since.",
           sentence_review="approved",
           has_outcome=True,
           value_made="31 qualified pipeline reports a month, delivered without a single missed cycle",
           value_saved="Roughly £40,000 a year against our previous supplier",
           value_optimized="Cut our month-end data close from five days to two"),
        NS(claim_no="CS-0000-02",
           has_outcome=False, value_made="", value_saved="", value_optimized="",
           text="Meridian delivered a reporting automation project that the client operates independently today.",
           client_company="",
           relationship_line="Project completed March 2026",
           scope_line="Reporting automation", status_line="Completed",
           grade="evidence_verified", grade_label="Evidence Verified", standard_version="1.0",
           evidence_items=["Statement of work, dated November 2025",
                           "Project completion sign off, March 2026"],
           show_confirmer=False, anon_descriptor="a national retail group",
           confirmer_name="", confirmer_role="", confirmer_linkedin="",
           resolved_at=dt(2026, 7, 14), client_sentence="", sentence_review=""),
        NS(claim_no="CS-0000-03",
           has_outcome=False, value_made="", value_saved="", value_optimized="",
           text="Meridian provides ad hoc data advisory to Bright & Co Accountants.",
           client_company="Bright & Co Accountants",
           relationship_line="Client since May 2026",
           scope_line="Data advisory", status_line="Ongoing",
           grade="client_confirmed", grade_label="Client Confirmed", standard_version="1.0",
           evidence_items=[],
           show_confirmer=True, anon_descriptor="",
           confirmer_name="James Bright", confirmer_role="Managing Partner",
           confirmer_linkedin="https://www.linkedin.com/", resolved_at=dt(2026, 8, 9),
           client_sentence="", sentence_review=""),
    ]
    return vendor, claims


@app.get("/sample")
def sample():
    vendor, claims = _specimen()
    return render_template("proof.html", brand=BRAND, vendor=vendor,
                           claims=claims, grades=GRADES, specimen=True, base_url=BASE_URL)


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


def _track(vendor, kind):
    try:
        db.session.add(ProofEvent(vendor_id=vendor.id, kind=kind,
                                  referrer=(request.referrer or "")[:500]))
        db.session.commit()
    except Exception:
        db.session.rollback()


@app.post("/e/<slug>/<kind>")
def proof_event(slug, kind):
    if kind not in ("linkedin_click", "check_click", "badge_copy"):
        abort(404)
    vendor = Vendor.query.filter_by(slug=slug).first_or_404()
    _track(vendor, kind)
    return "", 204


@app.get("/r/<slug>/card.png")
def share_card(slug):
    if slug == "_sample":
        vendor, claims = _specimen()
    else:
        vendor = Vendor.query.filter_by(slug=slug).first_or_404()
        claims = [c for c in vendor.claims if c.state == "confirmed"]
        if not claims:
            abort(404)
        _track(vendor, "card")
    from PIL import Image, ImageDraw, ImageFont
    NAVY = (22, 37, 78); ICE = (245, 247, 252); MAROON = (108, 29, 69)
    SOFT = (176, 186, 214); RULE = (58, 76, 122)
    W, H = 1200, 630
    img = Image.new("RGB", (W, H), NAVY)
    d = ImageDraw.Draw(img)
    FD = "/usr/share/fonts/truetype/dejavu/"
    serif_b = lambda n: ImageFont.truetype(FD + "DejaVuSerif-Bold.ttf", n)
    sans = lambda n: ImageFont.truetype(FD + "DejaVuSans.ttf", n)
    sans_b = lambda n: ImageFont.truetype(FD + "DejaVuSans-Bold.ttf", n)
    mono = lambda n: ImageFont.truetype(FD + "DejaVuSansMono.ttf", n)
    # faint guilloche
    import math
    for k in range(6):
        pts = [(x, H - 90 + int(28 * math.sin(x / 90.0 + k * 1.1)) + k * 7) for x in range(0, W, 6)]
        d.line(pts, fill=(30, 48, 96), width=1)
    # seal
    cx, cy, r = 96, 96, 44
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=ICE, width=4)
    d.line([(cx - 18, cy + 2), (cx - 4, cy + 16), (cx + 22, cy - 14)], fill=ICE, width=6, joint="curve")
    d.text((164, 62), BRAND.upper(), font=sans_b(30), fill=ICE)
    d.text((164, 100), "REGISTER OF COUNTERSIGNED CLAIMS", font=mono(17), fill=SOFT)
    d.line([(64, 176), (W - 64, 176)], fill=RULE, width=2)
    # vendor + record no
    name = vendor.name if len(vendor.name) <= 30 else vendor.name[:29] + "…"
    d.text((64, 216), name, font=serif_b(64), fill=ICE)
    d.text((64, 302), vendor.record_no, font=mono(30), fill=(200, 170, 190))
    # countersigners line
    named = [c.client_company for c in claims if c.show_confirmer]
    priv = len(claims) - len(named)
    parts = named[:2]
    extra = len(named) - len(parts) + priv
    line = "Countersigned by " + ", ".join(parts) if parts else "Countersigned"
    if extra > 0:
        line += f" and {extra} more" if parts else f" by {extra} verified client{'s' if extra > 1 else ''}"
    d.text((64, 372), line, font=sans(30), fill=ICE)
    # best grade chip
    order = ["fully_verified", "evidence_verified", "client_confirmed"]
    best = min(claims, key=lambda c: order.index(c.grade) if c.grade in order else 9).grade
    _g = GRADES.get(best, "Client Confirmed")
    label = (_g if isinstance(_g, str) else _g.get("label", "Client Confirmed")).upper()
    tw = d.textlength(label, font=sans_b(24))
    d.rectangle([64, 440, 64 + tw + 44, 494], fill=ICE)
    d.text((86, 454), label, font=sans_b(24), fill=NAVY)
    # dates + verify line
    latest = max(c.resolved_at for c in claims if c.resolved_at)
    d.text((64, 540), f"Countersigned {latest.strftime('%B %Y')}  ·  Verify: {BASE_URL.replace('https://','')}/check", font=mono(19), fill=SOFT)
    import io as _io
    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    from flask import send_file
    return send_file(buf, mimetype="image/png", max_age=3600)


@app.get("/r/<slug>.json")
def record_json(slug):
    if slug == "_sample":
        vendor, claims = _specimen()
    else:
        vendor = Vendor.query.filter_by(slug=slug).first_or_404()
        _track(vendor, "json")
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
            "standard_version": c.standard_version or None,
            "contains_outcome_figures": bool(c.has_outcome),
            "value_in_clients_words": ({"made": c.value_made or None, "saved": c.value_saved or None,
                                        "optimized": c.value_optimized or None}
                                       if (c.value_made or c.value_saved or c.value_optimized) else None),
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


@app.get("/contact")
def contact_form():
    return render_template("contact.html", brand=BRAND, ctx=request.args.get("ctx", ""))


@app.post("/contact")
def contact_submit():
    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip()
    body = (request.form.get("body") or "").strip()
    if not (name and email and body):
        flash("Name, email and the question are all needed.")
        return render_template("contact.html", brand=BRAND, ctx=""), 400
    msg = SupportMessage(name=name, email=email, body=body,
                         context=(request.form.get("ctx") or "").strip())
    draft = ai_call([{"role": "user", "content":
        f"Question from {name} ({email}), sent from page: {msg.context or 'site'}\n\n{body}"}],
        system=SUPPORT_SYSTEM)
    if draft:
        msg.ai_draft = draft
    db.session.add(msg)
    db.session.commit()
    return render_template("contact_thanks.html", brand=BRAND)


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
    _track(vendor, "pdf")
    claims = [c for c in vendor.claims if c.state == "confirmed"]
    if not claims:
        abort(404)
    if slug != "_sample":
        db.session.commit()
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
        claim.marketing_opt_in = bool(request.form.get("marketing_opt_in"))
        claim.standard_version = STANDARD_VERSION
        claim.value_made = (request.form.get("value_made") or "").strip()[:300]
        claim.value_saved = (request.form.get("value_saved") or "").strip()[:300]
        claim.value_optimized = (request.form.get("value_optimized") or "").strip()[:300]
        if claim.value_made or claim.value_saved or claim.value_optimized:
            claim.has_outcome = True
        claim.client_sentence = (request.form.get("client_sentence") or "").strip()[:400]
        if claim.client_sentence:
            claim.sentence_review = "pending"
            note = ai_call([{"role": "user", "content":
                f"Claim on the record (verified): {claim.text}\n"
                f"Client sentence submitted by the countersigner: {claim.client_sentence}"}],
                system=f"You screen client sentences for {BRAND}, a register of verified B2B facts. "
                       "The sentence is the client's own words shown beside verified facts. Decline reasons: "
                       "it asserts specific facts not in the verified claim (names, numbers, dates not on the record), "
                       "or it makes outcome/ROI claims (revenue, growth, savings figures). "
                       "General praise, opinions and descriptions of the working relationship are FINE — they are "
                       "clearly marked as the client's words, not findings. Reply with exactly one line: "
                       "CLEAR — <five word reason> or FLAG — <what rule it touches>.")
            if note:
                claim.sentence_ai_note = note.strip()[:500]
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
    db.session.flush()
    _attach_evidence(c, request.files.getlist("claim_files"))
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
    vendor.widget_interest = bool(request.form.get("widget_interest"))

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
        _attach_evidence(c, request.files.getlist(f"claim{i}_files"))
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
    support = SupportMessage.query.filter_by(handled=False).order_by(
        SupportMessage.created_at.desc()).all()
    sentences = Claim.query.filter_by(sentence_review="pending").all()

    # ---- live register stats, computed not typed ----
    all_claims = Claim.query.all()
    by_state = {}
    for cl in all_claims:
        by_state[cl.state] = by_state.get(cl.state, 0) + 1
    live = by_state.get("confirmed", 0)
    sent_ever = sum(1 for cl in all_claims if cl.state in ("sent", "confirmed", "corrected"))
    completion = round(100 * live / sent_ever) if sent_ever else None
    vendors_all = Vendor.query.count()
    vendors_live = sum(1 for v in vendors if any(cl.state == "confirmed" for cl in v.claims))
    ladder = {8: (15, 40), 9: (60, 180), 10: (150, 450), 11: (240, 720), 12: (330, 1000)}
    m = now().month
    target = ladder.get(m, ladder[12] if m > 12 or m < 8 else ladder[8])
    stats = {"vendors_all": vendors_all, "vendors_live": vendors_live,
             "claims_live": live, "with_client": by_state.get("sent", 0),
             "in_review": by_state.get("submitted", 0), "drafts": by_state.get("draft", 0),
             "corrections": by_state.get("corrected", 0),
             "completion": completion,
             "target_vendors": target[0], "target_claims": target[1],
             "month_name": now().strftime("%B")}
    return render_template("admin.html", brand=BRAND, vendors=vendors,
                           pending=pending, corrected=corrected,
                           confirmed=confirmed, invites=invites,
                           disputes=disputes, support=support,
                           sentences=sentences, ai_on=bool(ANTHROPIC_API_KEY),
                           stats=stats)


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
        has_outcome=bool(request.form.get("has_outcome")),
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
    for ev in list(claim.evidence_files):
        if ev.ai_summary:
            log_event(claim.id, f"facts extracted before deletion ({ev.filename}): {ev.ai_summary[:400]}")
        db.session.add(EvidenceFingerprint(claim_id=claim.id, filename=ev.filename,
            sha256=ev.fingerprint, extracted_facts=ev.ai_summary or ""))
        log_event(claim.id, f"evidence deleted: {ev.filename} (sha256 {ev.fingerprint}) checked and deleted on approval")
        db.session.delete(ev)
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


@app.post("/admin/claim/<int:claim_id>/toggle-outcome")
@admin_required
def admin_toggle_outcome(claim_id):
    claim = db.session.get(Claim, claim_id) or abort(404)
    claim.has_outcome = not claim.has_outcome
    log_event(claim.id, f"outcome flag set to {claim.has_outcome}")
    db.session.commit()
    return redirect(f"/admin/vendor/{claim.vendor_id}")


@app.get("/admin/countersigners")
@admin_required
def admin_countersigners():
    rows = (Claim.query.filter(Claim.state == "confirmed")
            .order_by(Claim.resolved_at.desc()).all())
    return render_template("admin_countersigners.html", brand=BRAND, rows=rows)


@app.get("/admin/proof-activity")
@admin_required
def admin_proof_activity():
    vendors = Vendor.query.all()
    summary = []
    for v in vendors:
        evs = ProofEvent.query.filter_by(vendor_id=v.id).all()
        if not evs:
            continue
        counts = {}
        for e in evs:
            counts[e.kind] = counts.get(e.kind, 0) + 1
        summary.append({"vendor": v, "views": counts.get("view", 0),
                        "linkedin": counts.get("linkedin_click", 0),
                        "checks": counts.get("check_click", 0),
                        "pdf": counts.get("pdf", 0), "json": counts.get("json", 0),
                        "last": max(e.at for e in evs)})
    recent = (db.session.query(ProofEvent, Vendor).join(Vendor)
              .order_by(ProofEvent.at.desc()).limit(100).all())
    return render_template("admin_proof_activity.html", brand=BRAND,
                           summary=summary, recent=recent)


@app.get("/admin/activity")
@admin_required
def admin_activity():
    events = (db.session.query(AuditEvent, Claim, Vendor)
              .join(Claim, AuditEvent.claim_id == Claim.id)
              .join(Vendor, Claim.vendor_id == Vendor.id)
              .order_by(AuditEvent.at.desc()).limit(200).all())
    return render_template("admin_activity.html", brand=BRAND, events=events)


@app.post("/admin/sentence/<int:claim_id>/<decision>")
@admin_required
def admin_sentence_decide(claim_id, decision):
    if decision not in ("approve", "decline"):
        abort(404)
    claim = Claim.query.get_or_404(claim_id)
    if decision == "approve":
        claim.sentence_review = "approved"
        log_event(claim.id, "Client sentence approved and published")
    else:
        claim.sentence_review = "declined"
        log_event(claim.id, "Client sentence declined under the standard")
    db.session.commit()
    return redirect(url_for("admin_home"))


@app.post("/admin/support/<int:msg_id>/handled")
@admin_required
def admin_support_handled(msg_id):
    m = db.session.get(SupportMessage, msg_id) or abort(404)
    m.handled = True
    db.session.commit()
    return redirect(url_for("admin_home"))


@app.post("/admin/support/<int:msg_id>/redraft")
@admin_required
def admin_support_redraft(msg_id):
    m = db.session.get(SupportMessage, msg_id) or abort(404)
    draft = ai_call([{"role": "user", "content": f"Question from {m.name}: {m.body}"}], system=SUPPORT_SYSTEM)
    if draft:
        m.ai_draft = draft
        db.session.commit()
        flash("Draft refreshed.")
    else:
        flash("AI is not configured (set ANTHROPIC_API_KEY) or the call failed.")
    return redirect(url_for("admin_home"))


@app.get("/admin/evidence/<int:ev_id>")
@admin_required
def admin_evidence_view(ev_id):
    ev = db.session.get(EvidenceFile, ev_id) or abort(404)
    return ev.data, 200, {"Content-Type": ev.mimetype,
                          "Content-Disposition": f'inline; filename="{ev.filename}"'}


@app.post("/admin/evidence/<int:ev_id>/read")
@admin_required
def admin_evidence_read(ev_id):
    ev = db.session.get(EvidenceFile, ev_id) or abort(404)
    summary = ai_read_evidence(ev)
    if summary:
        ev.ai_summary = summary
        db.session.commit()
        flash("Evidence read. Review the extracted facts, set the grade, then approve — approval deletes the file.")
    else:
        flash("AI is not configured (set ANTHROPIC_API_KEY) or the call failed.")
    return redirect(url_for("admin_vendor", vendor_id=ev.claim.vendor_id))


@app.post("/admin/evidence/<int:ev_id>/delete")
@admin_required
def admin_evidence_delete(ev_id):
    ev = db.session.get(EvidenceFile, ev_id) or abort(404)
    vid = ev.claim.vendor_id
    if ev.ai_summary:
        log_event(ev.claim_id, f"facts extracted before deletion ({ev.filename}): {ev.ai_summary[:400]}")
    db.session.add(EvidenceFingerprint(claim_id=ev.claim_id, filename=ev.filename,
        sha256=ev.fingerprint, extracted_facts=ev.ai_summary or ""))
    log_event(ev.claim_id, f"evidence deleted: {ev.filename} (sha256 {ev.fingerprint}) checked and deleted")
    db.session.delete(ev)
    db.session.commit()
    return redirect(url_for("admin_vendor", vendor_id=vid))


@app.post("/admin/dispute/<int:dispute_id>/handled")
@admin_required
def admin_dispute_handled(dispute_id):
    d = db.session.get(Dispute, dispute_id) or abort(404)
    d.handled = True
    db.session.commit()
    return redirect(url_for("admin_home"))


@app.errorhandler(404)
def not_found(e):
    return render_template("404.html", brand=BRAND), 404


@app.get("/robots.txt")
def robots():
    return ("User-agent: *\nDisallow: /admin\nDisallow: /confirm/\nDisallow: /onboard/\nDisallow: /auth/\n"
            f"Sitemap: {BASE_URL}/llms.txt\n"), 200, {"Content-Type": "text/plain"}


@app.get("/healthz")
def healthz():
    return {"status": "ok", "admin_key_set": bool(ADMIN_KEY),
            "postmark_configured": bool(POSTMARK_TOKEN and FROM_EMAIL)}, 200


with app.app_context():
    db.create_all()
    # self migration: add columns introduced after the first deploy.
    # create_all makes missing tables but never alters existing ones.
    _migrations = [
        "ALTER TABLE claim ADD COLUMN IF NOT EXISTS anon_descriptor VARCHAR(200) DEFAULT ''",
        "ALTER TABLE claim ADD COLUMN IF NOT EXISTS client_sentence VARCHAR(400) DEFAULT ''",
        "ALTER TABLE claim ADD COLUMN IF NOT EXISTS sentence_review VARCHAR(20) DEFAULT ''",
        "ALTER TABLE claim ADD COLUMN IF NOT EXISTS sentence_ai_note TEXT DEFAULT ''",
        "ALTER TABLE claim ADD COLUMN IF NOT EXISTS marketing_opt_in BOOLEAN DEFAULT FALSE",
        "ALTER TABLE evidence_file ADD COLUMN IF NOT EXISTS fingerprint VARCHAR(64) DEFAULT ''",
        "ALTER TABLE vendor ADD COLUMN IF NOT EXISTS contact_email VARCHAR(200) DEFAULT ''",
        "ALTER TABLE vendor ADD COLUMN IF NOT EXISTS onboard_token VARCHAR(64)",
        "ALTER TABLE vendor ADD COLUMN IF NOT EXISTS onboarded_at TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE vendor ADD COLUMN IF NOT EXISTS widget_interest BOOLEAN DEFAULT FALSE",
        "ALTER TABLE claim ADD COLUMN IF NOT EXISTS standard_version VARCHAR(10) DEFAULT ''",
        "ALTER TABLE claim ADD COLUMN IF NOT EXISTS has_outcome BOOLEAN DEFAULT FALSE",
        "ALTER TABLE claim ADD COLUMN IF NOT EXISTS value_made VARCHAR(300) DEFAULT ''",
        "ALTER TABLE claim ADD COLUMN IF NOT EXISTS value_saved VARCHAR(300) DEFAULT ''",
        "ALTER TABLE claim ADD COLUMN IF NOT EXISTS value_optimized VARCHAR(300) DEFAULT ''",
    ]
    from sqlalchemy import text as _sqltext
    for _m in _migrations:
        try:
            db.session.execute(_sqltext(_m))
            db.session.commit()
        except Exception:
            db.session.rollback()   # SQLite (no IF NOT EXISTS) or already applied

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
