# Countersign — v1, week one

The countersign loop, end to end. Concierge model: you enter vendors and claims
through the admin panel, the named client confirms via magic link, and nothing
publishes unless they do.

## What works

- Admin panel (key protected): add vendors, draft claims with facts, grade, evidence list
- Countersign request: generates the email the vendor sends from their own inbox
  (primary flow), or sends via Postmark as the fallback
- Magic link confirm page: client sees the facts, adds name + role + LinkedIn,
  confirms publicly or privately, or submits a correction
- Corrections: claim returns to draft, facts revised, a NEW link is issued and the
  old one dies
- Public: registry (only vendors with a confirmed claim appear), proof page per
  vendor, /check record lookup
- Audit trail on every claim

## Routes

- `/` open register · `/r/<slug>` proof page · `/check` record lookup
- `/confirm/<token>` the client's page · `/admin` your desk
- `/healthz` health + config status

## Environment variables (Render)

| Var | Required | Notes |
|---|---|---|
| ADMIN_KEY | yes | admin panel access |
| SECRET_KEY | yes | any long random string (sessions) |
| DATABASE_URL | yes | from a Render Postgres instance |
| BASE_URL | yes | e.g. https://countersign-demo.onrender.com |
| POSTMARK_TOKEN | no | enables the fallback send |
| FROM_EMAIL | no | verified Postmark sender |
| BRAND_NAME | no | defaults to Countersign |

## Deploy (Render)

1. Create a **PostgreSQL** instance on Render first, copy its Internal Database URL
2. New Web Service, connect the repo
   - Runtime: Python 3 (not Docker)
   - Build: `pip install -r requirements.txt`
   - Start: `gunicorn app:app`
3. Set the env vars above (DATABASE_URL = the Postgres internal URL)
4. Deploy, check `/healthz`

SQLite fallback runs locally with zero setup: `pip install -r requirements.txt && python app.py`

## The site, front to back

Home explains the register. Sign up requests a founding place (curated: you approve).
Sign in is a one time magic link to the work email on file, no passwords. The portal
shows a vendor their claims in plain language (IN REVIEW / WITH YOUR CLIENT / LIVE)
and lets them add claims, which land as submissions for your review. When Postmark
isn't configured yet, sign in runs in pre launch mode and shows the link on screen.

## Onboarding, two routes

- **Concierge**: you enter everything in /admin (the default for calls)
- **Self serve**: approve an invitation request (or hit "generate onboarding link" on any
  vendor), send the link, they fill in company + up to 3 claims in ~5 minutes. Submissions
  land as SUBMITTED for your review: you edit to registry standard, grade against evidence
  you actually checked, approve, then send the countersign. Nothing they type can publish
  without your review AND their client's confirmation. Links are single use.

## The extras (all live)

- `/badge/CS-0001.svg` — embeddable badge, best grade + record number, links back
- `/r/<slug>/case-study.pdf` — the verified record as a certificate-style PDF with a QR to /check
- `/r/<slug>.json` — machine readable record for AI assistants and integrations
- `/llms.txt` — register index for AI crawlers
- `/dispute` — challenge a record; challenges queue on the admin dashboard
- Private confirmations can carry a descriptor ("a global technology company") so the
  record leads with what WAS verified when the client can't be named

## Week one goal

Add Navvai as vendor one, draft the Netformic claim, copy the countersign email
to Tom, get the first real confirmation on the record.
