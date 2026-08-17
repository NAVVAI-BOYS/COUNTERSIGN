# Countersign — plain language changelog

What changed and when, newest at the top. One line per change, no jargon. This is the project's audit trail from day one.

## 17 August 2026

- First version of the grade-object split: claims containing outcome figures are flagged, display 'what the grade covers' on the record (relationship facts graded, outcome figures are the client's confirmation, never audited), carry the flag in their machine readable version, and cannot rise above Client Confirmed until relationship and outcome are graded separately. The standard states the rule.

## 14 August 2026 (overnight and morning)

- Hero rewritten to hit: 'Your word stopped being proof. Your client's signature still is.' The deal now sits in the hero like a price list: first 100 records free for life, places remaining counted live.
- Home page reorganised on the Wolfsberg model: every section now carries the same numbered header (01 The problem, 02 See it yourself, 03 The method, 04 A record, 05 Publications, 06 Membership), and a new publications library lists the register's public documents like an institution's archive: the standard with its version, the live numbers, the specimen, the dispute procedure.
- The pair is now interactive: 'click the one you would believe', with the reveal making the real point land — whichever you picked, you were guessing, and so are your buyers. Works without JavaScript too.
- The pair section restyled as the page's one dark navy band so it dominates the home page, with 'one of these engagements never happened' promoted to the section headline.
- Home page gained the pair: two identically plausible case studies side by side, one fabricated, with the line 'one of these engagements never happened'. The problem made visible in five seconds, pointing at the example record as the answer.
- Signup page rewritten after Clem Chambers' advice: opens by making the risk visible (buyers stopped believing case studies), then states plainly what a record does (less work, less risk), then the deal like a price list (first 100 records free for life, paid tiers announced September) with a live counter of founding places remaining. The register's own pages stay calm and institutional; the selling happens here.
- Fixed a bug where the confirm page browser tab title contained stray code.
- Tidied the top navigation: five clean links plus a Sign up button, no stray dots. The numbers page, ask a question, and challenge a record links moved to the footer where institutional links belong.
- Every countersigned claim is now stamped with the version of the standard it was verified under (Standard v1.0). Records keep this forever.
- The standard page now promises: records are permanent. Nobody can pay to rewrite history.
- Every record makes its own share image, so links posted on LinkedIn show a proper certificate.
- New public page: /numbers. The register's own live statistics, including corrections and declined claims.
- Records show "Countersigned August 2026" prominently, so freshness is always visible.
- New vendors are asked at onboarding if they would want their record shown on their own website one day.
- The register now keeps a digital fingerprint and a summary of facts checked whenever evidence is deleted, so a dispute can be answered later without keeping the document. Wording on the site updated to say exactly this.
- Buyer activity on records is now tracked: views, LinkedIn clicks on confirmers, certificate checks, PDF downloads, machine reads. New admin page: Proof Activity.
- Cleaned out duplicated tracking code left over from working across two builds.
- The live site crashed because the database was older than the code. The app now updates its own database on every start, so this cannot happen again.
- Admin home now opens with live numbers: claims and records against this month's targets, countersigns waiting with clients, and the completion rate.
- New admin page: Activity. Everything that ever happened on the register, newest first.
- New admin page: Countersigners. Every person who has signed, and whether they agreed to hear from us.
- Confirmers now see an optional tick box to hear about the register. Only people who tick it can be emailed marketing. The page's promise was reworded to stay true.
- Thank you page after confirming now carries the founding membership invitation.
- Confirmers get a friendly "Before you start" panel explaining what confirming involves.
- The standard shows worked examples of client sentences that publish and sentences that get declined.
- Client sentences are screened by AI and wait for admin approval before they appear.
- Confirmers can add one optional sentence in their own words, shown separately from the verified facts.
- The standard gained rules for free work and partnerships: recordable, stated plainly, capped below the top grade.
- New: AI evidence reading. Vendors attach documents, admin reads them with AI help, approval deletes them.
- New: support inbox. Contact form on the site, AI drafts a reply, admin approves and sends.
- Home page gained "The problem, in numbers": sourced statistics on why buyers stopped believing vendors.
- Home page states the review promise: nothing on a record is unreviewed, and one gate is always the client.
- Grade tooltips fixed so they can never be cut off on any screen (second fix: right aligned to the chip).

## 13 August 2026, evening — extras and polish

- Private confirmations upgraded: a claim confirmed privately can carry a description like "a global technology company", and the record leads with what was verified.
- New: dispute flow. A flag link on every record, a short form, and an admin queue to handle challenges.
- New: machine readable records. Every record has a JSON version, and /llms.txt lists the register for AI assistants.
- New: verified record PDF. A certificate style document per record with a QR code that resolves to the check page.
- New: embeddable badge. An SVG badge per record showing the best grade and record number, linking back to the register.
- Record pages gained grade tooltips (hover any grade to see exactly what backed it) and a "What a reference call would tell you" section answering the standard reference questions from the record.
- Interaction layer added: gentle scroll animations, a drawing seal, a sticky blurred header. All switched off for people who prefer reduced motion.
- New: the example record. A clearly marked fictional specimen (Meridian Data Services, CS-0000) so visitors can see a full record without any real company being invented.

## 13 August 2026, afternoon — deployed and designed

- The site went live on Render with a real database. One crash on the way (a Python version mismatch) found and fixed.
- Design settled after three rounds: institutional look inspired by financial regulators. Ice blue background, deep navy, maroon reserved for the seal. Flat corners, serif headlines.
- Home page gained engraved certificate patterning (guilloche), step icons, and a sample record card.

## 13 August 2026, morning — the product takes shape

- New: vendor portal. Magic link sign in with no passwords, a home page showing each claim's status in plain words, and a form to submit new claims.
- New: self serve onboarding. Admin approves an application, the vendor gets a single use link, fills in company details and up to three claims in about five minutes. Nothing publishes without admin review and the client's confirmation.
- New: the public site. Home page, the register directory, the published standard, and a founding membership application form.
- First working build of the core loop: admin desk, claims with facts and grades, the countersign email sent from the vendor's own inbox, the magic link confirmation page with a correction path, and the public record page. Every step logged in an audit trail.
- Build decisions locked: Postmark for email, Postgres database, no passwords anywhere (magic links only), and the brand set up as a variable so a rename costs minutes.

## Before the build — how we got here (8 to 13 August 2026)

- 13 Aug: the name Countersign chosen as lead candidate, and the decision made that this becomes its own company, separate from Navvai.
- 11 to 13 Aug: validation calls and written feedback (a VC, the CEO of Zally, Lumenis, GFA Exchange, Sparton, and others). The plan was rewritten around the hardest question raised: do buyers actually change behaviour because of verification?
- 9 Aug: the decisive test reframed. Instead of asking people if they would pay, show real buyers a real record and watch what they do.
- 8 Aug: the idea confirmed. Combine verified case studies and a public register into one product: claims confirmed by the named client, graded by evidence, published where anyone can check them.
