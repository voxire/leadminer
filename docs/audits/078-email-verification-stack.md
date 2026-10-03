# 078 — Email discovery & verification stack (Lebanon / Saudi domains)

## Verdict

For a leadminer-shaped workload — Lebanese and Saudi SMBs, many with weak or absent
digital presence — **pure discovery tools (Hunter) will underperform because there is
nothing on the open web for them to index.** The right stack is (1) a **free DIY
MX/SMTP pre-screen via dnspython** to kill dead domains and flag catch-alls, (2)
**pattern-guess `first.last@` + `flast@`** from any scraped names (covers ~75% of B2B
addresses before you pay a cent), then (3) a single paid verifier. Under $100/mo the
best value is **NeverBounce Growth ($49/mo for 10,000 verifications ≈ $4.90/1k)**, with
**Hunter Starter (annual, $34/mo)** as an optional discovery add-on only if a trial shows
non-trivial hit rate on `.lb` / `.sa`. "Neverlist" is **not a real vendor** — it appears
to be a typo/duplicate of NeverBounce; treat it as such.

## Findings

### F1 — "Neverlist" is not an email verification product

- **Where:** the assigned vendor list (`Hunter.io, NeverBounce, ZeroBounce, Kickbox, Neverlist`).
- **Breaks:** the evaluation brief includes a service that does not exist. Web search for
  "Neverlist email verification" returns only Beautycounter's "Never List" (banned
  cosmetics ingredients) and unrelated trademark uses — no email tool, no pricing page, no API.
- **Trigger:** anyone attempting to budget for or integrate "Neverlist" will waste time.
- **Fix:** treat "Neverlist" as a duplicate of **NeverBounce** (already in scope). If a
  *different* tool was intended, the likely candidates are `EmailListVerify`, `Bouncer`,
  `MillionVerifier`, or `DeBounce` — none of which change the recommendation below.

### F2 — Hunter.io: discovery-first, weak for leadminer's target segment

- **Where:** relevant to `enricher.py` `check_websites()` (the 40-worker website scan
  that regexes out `email` from live sites — `BRIEF.md:34-35`, `BRIEF.md:60-62`).
- **Breaks:** Hunter's data is *"sourced from the open web"* — it crawls millions of
  pages and exposes emails it found publicly. leadminer deliberately targets businesses
  with *"weak or absent digital presence"* (`BRIEF.md:8`). For those domains there is
  nothing published to crawl, so Hunter's Domain Search / Email Finder will return
  `no result` (which, to its credit, costs no credits). Coverage skews US/EU; Lebanese
  and Saudi SMBs are materially under-indexed relative to the global data.
- **Cost per 1k:** verification = **0.5 credit**; discovery = **1 credit/email** (Bulk
  Domain Search: 1 credit per up to 10 emails). Starter is $49/mo monthly or **$34/mo
  annual** for 2,000 credits ⇒ **$0.017/credit ⇒ ≈ $8.50 per 1k verifications** (if the
  whole quota were spent on verification, which it won't be).
- **Accuracy:** no hard percentage guarantee; Hunter emits a per-email `confidence`
  score (e.g. `92`) plus `verification.status` (`valid`/`accept_all`/`unknown`). It
  auto-verifies on paid plans but is weaker as a verifier than the dedicated tools.
- **Rate limits / bulk:** Domain Search, Email Finder and enrichment = **15 req/s,
  500/min**; Email Verifier = **10 req/s, 300/min**; Discover API = **5 req/s, 50/min**.
  Bulk via CSV (Bulk Domain Search / Bulk Email Verifier).

### F3 — NeverBounce: best $/1k bulk verifier under $100

- **Cost per 1k:** pay-as-you-go **$8/1,000** ($0.008/email); **Growth $49/mo for up to
  10,000 ⇒ $4.90/1k**; drops toward ~$0.005/email at 10k+ credits. Free tier ~500/mo.
- **Accuracy:** claims "over 98%" and "99.9% deliverability"; 20+ step verification
  (syntax, MX, SMTP, spam-trap, disposable). Independent benchmarks place it **97–99%**.
  Caveat: ZeroBounce's self-run 2026 benchmark claims NeverBounce returned 50.2%
  "unknown" results on a hard 9,901-address set — vendor-conducted, treat with
  skepticism, but "unknown" handling is worth testing against your own `.lb`/`.sa` list.
- **Bulk / rate limits:** bulk-verifies ~10k emails in ~3 min; API limits documented as
  ~10 concurrent bulk jobs / ~50 job runs per day. Real-time API is rate-limited for
  accounts without a stored payment method.

### F4 — ZeroBounce: highest claimed accuracy, priciest at entry

- **Cost per 1k:** pay-as-you-go minimum is **2,000 credits for $39 ⇒ $19.50/1k**;
  ZeroBounce ONE starts at **$99/mo for 10,000 credits ⇒ $9.90/1k**. 100 free credits/mo.
- **Accuracy:** **99.6% accuracy guarantee with money-back**; independent tests run
  ~93–97–99%; self-run 2026 benchmark scored 98.0% vs 8 competitors; advertises <1.75%
  "unknown" results. Strongest catch-all handling (including M365 / Google Workspace
  catch-all resolution as of Jul 2026).
- **Bulk / rate limits:** batch up to **100 emails/request**, **30 req/min** (40 for ONE)
  ⇒ ~3,000–4,000 emails/min through the API; bulk file upload (min 100 emails).
- **Relevance:** best if you need the highest-confidence answers on catch-all-heavy
  domains, but the $99 floor leaves no room for a second tool under the $100 budget.

### F5 — Kickbox: clear price ladder, 95% guarantee, generous API limits

- **Cost per 1k:** 100 free; 500=$5; 1,000=$10; 2,500=$25; 5,000=$40; 10,000=$80;
  25,000=$200; 100,000=$800. ⇒ **$10/1k at 1k, $8/1k at 10k**, down to $4–5/1k at scale.
- **Accuracy:** **95% deliverability guarantee** — no more than 5% of "Deliverable"
  addresses bounce (with stated conditions). Independent tests place it ~95%.
- **Bulk / rate limits:** **25 parallel requests per IP**, **8,000 requests/clock minute**;
  single `verify` + batch list endpoints; returns `deliverable/undeliverable/risky/unknown`
  plus `sendex` score, `accept_all`, `disposable`, `role` flags.
- **Relevance:** most developer-friendly limits (8,000/min) and clearest pricing, but
  $8/1k at the 10k tier is ~60% more expensive than NeverBounce for the same volume.

### F6 — Open-source MX + SMTP verification via dnspython: $0, but SMTP check is unreliable in 2026

- **Where:** no code exists yet; this would be a new module beside `enricher.py`.
- **Cost per 1k:** **$0** (compute + DNS only). MX lookup is deterministic and accurate.
- **Accuracy / breaks:** two very different steps —
  - **MX check (reliable):** `dns.resolver.resolve(domain, 'MX')` correctly tells you a
    domain *has no mail server* → drop it. Also detect disposable/free/role addresses.
  - **SMTP `RCPT TO` probe (unreliable):** Google Workspace, Microsoft 365 and most
    providers reject address-enumeration probes or return **catch-all (accept-all)**
    responses, producing **false positives** (accept-all accepts anything) and **false
    negatives** (real mailboxes that refuse to confirm). It also gets your sending IP
    greylisted/blocked by Google/Microsoft quickly at scale.
- **Fix / usage:** use dnspython **for MX + catch-all detection only**, then hand the
  uncertain tail to a paid verifier (NeverBounce) that maintains its own mailboxes and
  provider relationships. Do **not** trust raw SMTP RCPT TO as a final valid/invalid
  verdict. Libraries: `dnspython`, `py3-validate-email`, `email-validator`.
- **Rate limits:** none imposed by a vendor; you impose your own throttle. Public
  resolvers (1.1.1.1, 8.8.8.8) tolerate thousands of queries/sec but SMTP probes should
  be throttled to a handful/sec per target MX to avoid blacklisting.

### F7 — Pattern guessing hit rate: two patterns cover ~75% of B2B addresses

- **Where:** leadminer scrapes names inconsistently (the 22-column schema has `name`
  but no separate `first_name`/`last_name` — `BRIEF.md:59-62`), so name-derived guesses
  are only possible when a contact name is available, not for bare business names.
- **Data:** Sendburg's 2026 study of **336,782 B2B emails**:
  - `first.last@` — **47.7%** (the single best blind guess)
  - `flast@` (first initial + last) — **26.8%**
  - **`first.last@` + `flast@` together ≈ 74.5%**
  - `first@` — 8.1% (small teams/startups); `firstlast@`, `first_last@`, `f.last@` ~2% each.
  - Enterprise (10k+ staff): `first.last@` = **74.2%**; small firms (1–10): only **38%**,
    with `first@` ~4× more common.
- **Realistic hit rate for leadminer:** leadminer targets *small* Lebanese/Saudi SMBs, so
  expect the **low end** of the curve — `first.last@` lands ~38–48%, and a `first.last@` +
  `flast@` + `first@` three-pattern guess covers roughly **60–75%** of *name-bearing*
  contacts. Critically, **a "pattern hit" is not a verified address**: you must still
  verify, and transliteration of Arabic names (diacritics, `al-`/`el-` prefixes,
  double-barrelled surnames) reduces the hit rate further on `.lb`/`.sa` domains.
- **Fix:** generate the top-3 patterns per contact, dedupe, then verify — never send
  unverified guesses.

## Recommended stack (≤ $100/month)

Pipeline, in order:

1. **DIY MX + catch-all pre-screen (dnspython) — $0.** For every domain, drop no-MX
   domains, tag catch-all/accept-all and free-mail domains. This removes the addresses
   you'd otherwise pay to learn are dead. (New module, ~100 LOC, no new deps beyond
   `dnspython`.)
2. **Pattern-guess candidates — $0.** Where `name` resolves to a real first/last pair,
   emit `first.last@`, `flast@`, `first@` per contact (~60–75% candidate coverage at
   zero cost).
3. **NeverBounce Growth — $49/mo (10,000 verifications, $4.90/1k).** Verify the union of
   (a) scraped emails from `enricher.py`, (b) guessed candidates, and (c) anything
   flagged `accept_all`/uncertain by the DIY pass. Fast bulk (10k ≈ 3 min), 97–99%
   accuracy. This is the cheapest credible verifier at the volume leadminer needs.
4. **Optional — Hunter Starter (annual, $34/mo).** Only for domains where *no* email was
   scraped and *no* name exists to guess. Buy a single month first and measure hit rate
   on your actual `.lb`/`.sa` domain list before committing — expect it to be low on
   leadminer's "no digital presence" cohort.

**Total: $49/mo (or $83/mo with Hunter).** Both under $100. The single highest-leverage
free step is the DIY MX/catch-all pass: it prevents paying ~$5–20/1k to verify domains
that have no mail server or accept everything.

## Not a bug, but worth knowing

- **"Unknown" ≠ "valid" and you may still be charged.** NeverBounce/ZeroBounce credit
  each check regardless of outcome; ZeroBounce's "unknown" rate is lower (~1.75%) and it
  advertises refunding unusable results, but verify the actual policy for your account.
- **Catch-all is the dominant accuracy risk on SMB domains.** Lebanese/Saudi SMBs on
  shared hosting (cPanel) are disproportionately catch-all. No tool can fully resolve a
  catch-all without sending; prefer tools with strong catch-all handling (ZeroBounce)
  or send a low-volume test blast and read bounces.
- **All these tools are cold-email-adjacent; keep the scraping/verification separation
  clean.** leadminer currently regexes emails *from* scraped sites; feeding guessed
  addresses into a verifier is a new data-processing purpose, so keep it in a distinct
  module and respect each vendor's list-provenance terms.
- **Rate limits are irrelevant at leadminer's scale.** 40 workers × 1 GET/site is far
  below every vendor's bulk ceiling (ZeroBounce 3k/min, Kickbox 8k/min, NeverBounce
  ~10k/3min). You will hit *credit* ceilings long before *rate* ceilings.

## Recommended order of work

1. Add `dnspython` to `requirements.txt`; write the MX/catch-all/disposable pre-screen.
2. Add a name-splitter for `BusinessRecord["name"]` → `{first, last}` and a top-3 pattern
   generator, wired into a new `verify_email` module (do **not** touch `enricher.py`).
3. Stand up NeverBounce Growth; run one end-to-end pass on a 1k sample and record
   valid/unknown/invalid + catch-all rates for `.lb` and `.sa` separately.
4. Run the 1-month Hunter trial in parallel; compare discovered emails vs guessed-then-
   verified emails on the same sample, then decide whether to keep it.
5. Wire verification results back into `completeness_score` / `lead_score` so a
   *verified* email outranks a scraped-but-unverified one.

## Citations

- Hunter.io pricing & credit rules: https://hunter.io/pricing (credit = 1 find, 0.5 verify; Starter $34/mo annual / 2,000 credits)
- Hunter.io Domain Search API (confidence score, `accept_all`, `pattern`, sources): https://hunter.io/api/domain-search
- Hunter API rate limits: https://help.hunter.io/en/articles/1970956-hunter-api (Domain Search/Email Finder 15 req/s, 500/min; Verifier 10 req/s, 300/min) and https://hunter.io/api-documentation/v2 (Discover 5 req/s, 50/min)
- Hunter "sourced from the open web" / 78M+ domains: https://hunter.io/data-platform and https://hunter.io/bulks/domain-search
- NeverBounce pricing ($8/1k, Growth $49/10k): https://www.neverbounce.com/pricing and https://www.abstractapi.com/guides/email-validation/zerobounce-vs-neverbounce
- NeverBounce accuracy + bulk speed ("10k in 3 min", 99.9%): https://www.neverbounce.com/ and https://mailtoaster.ai/blog/neverbounce-review/
- NeverBounce bulk API limits: https://pipeline.zoominfo.com/sales/neverbounce-api
- ZeroBounce pricing ($39/2,000; ONE $99/10k): https://www.zerobounce.net/pricing and https://mailfloss.com/zerobounce-pricing/
- ZeroBounce accuracy (99.6% guarantee) + benchmark: https://www.zerobounce.net/benchmark and https://www.zerobounce.net/
- ZeroBounce rate limits (100/batch, 30 req/min): https://www.zerobounce.net/docs/api-dashboard/api-rate-limits
- ZeroBounce catch-all improvement (Jul 2026): https://pr.theoutlookonline.com/article/ZeroBounce-Improves-Catch-All-Email-Validation-for-Microsoft-365-Google-Workspace-and-More/6a4e4d011f33de42266d6f98
- Kickbox pricing ladder: https://kickbox.com/pricing/
- Kickbox 95% deliverability guarantee: https://docs.kickbox.com/docs/the-95-deliverability-guarantee
- Kickbox API limits (25 parallel, 8,000/min): https://docs.kickbox.com/docs/using-the-api
- Independent 2026 cross-tool benchmark (ZeroBounce 99%, NeverBounce 97–99%): https://instantly.ai/blog/2026-email-verification-benchmark-accuracy-scores-for-8-top-tools
- Email pattern study (336,782 B2B addresses; first.last 47.7%, flast 26.8%): https://send-burg.com/research/b2b-email-formats and https://www.allegrow.co/knowledge-base/business-email-address-examples
- Company pattern / permutation workflow: https://vonsel.com/blog/cold-email/company-email-formats
- dnspython: https://github.com/rthalley/dnspython
- py3-validate-email (MX + SMTP validation): https://github.com/karolyi/py3-validate-email
- email-validator (syntax + MX): https://pypi.org/project/email-validator/
- Open-source MX/SMTP limits & false-negative risk: https://www.abstractapi.com/guides/email-validation/open-source-email-validation and https://truelist.io/blog/python-email-validation
