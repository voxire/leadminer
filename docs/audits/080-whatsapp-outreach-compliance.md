# 080 — WhatsApp Outreach Compliance: Cloud API, Templates, Per-Message Pricing, wa.me Links, and Opt-In Mechanics

## Verdict

Automating cold outbound WhatsApp messages directly to scraped phone numbers is a catastrophic compliance failure: it violates Meta's WhatsApp Business Messaging Policy, risks instant account restriction and phone number revocation, and breaches Saudi Arabia's Personal Data Protection Law (PDPL). The current `leadminer` pipeline makes zero distinction between mobile and landline numbers (`dedup.py:33-63`), performs unvalidated string surgery on `wa.me` links (`enricher.py:129-132, 204-206`), and produces pitches explicitly promising "WhatsApp lead capture funnels" (`pitch_recommender.py:83-96`) without any consent tracking or compliant dispatch architecture. To operate compliantly, `leadminer` must strictly segregate scraped data from automated Cloud API dispatch: cold leads can only be engaged via human-in-the-loop `wa.me` deep links or multi-touch warming funnels (e.g., cold email/calls driving inbound opt-in), while automated messaging requires a verified Meta Business Portfolio, approved marketing templates, strict E.164 mobile validation, an automated webhook suppression engine for opt-outs, and budgeting under Meta's July 2025 per-message pricing model.

---

## Findings

### S1 — Cold Automated Outbound via WhatsApp Cloud API Violates Meta Policy and Triggers Swift Banning
- **Where:** `enricher.py:204-206`, `main.py:82-90`, and `pitch_recommender.py:83-96`.
- **Breaks:** The pipeline extracts phone numbers and website WhatsApp links, tagging leads for WhatsApp-centric pitches (e.g., `"WhatsApp lead capture funnel"`, `"WhatsApp booking & re-engagement system"`). If an engineering team attaches an automated WhatsApp Cloud API worker to message these leads directly from `sales_ready.csv`, the WhatsApp Business Account (WABA) will be banned within hours. Meta's [WhatsApp Business Messaging Policy](https://www.whatsapp.com/legal/business-messaging-policy/) explicitly requires prior affirmative opt-in confirming that the user wishes to receive messages from that specific business. Scraped directory entries, Google Maps listings, and website footers do *not* constitute opt-in.
- **Trigger:** Sending a cold marketing template via Cloud API (`POST /v21.0/{phone-number-id}/messages`) to 200 scraped Lebanese or Saudi business numbers. Recipients immediately use WhatsApp's native "Block and Report Spam" button.
- **Consequences:**
  1. **Quality Rating Degradation:** Number quality rating drops from **Green (High Quality)** to **Yellow (Medium)** to **Red (Low)** within 50–100 delivered messages.
  2. **Messaging Limit Slashing:** Daily conversation limit automatically rolls back from Tier 1K (1,000 unique recipients/24h) to Tier 250 (250 recipients/24h).
  3. **Account Enforcement:** Phone status moves to **Flagged**, then **Restricted**, and finally permanent account termination under Meta's [Policy Enforcement Framework](https://developers.facebook.com/docs/whatsapp/overview/policy-enforcement/).
  4. **Legal Liability:** Violation of the Saudi Personal Data Protection Law (PDPL, Royal Decree M/19, Arts. 26–28) regarding unsolicited electronic direct marketing, carrying administrative fines up to SAR 5,000,000.
- **Fix:** Establish an absolute architectural boundary: scraped phone numbers must **never** be injected into an automated outbound WhatsApp API queue. Automated Cloud API dispatch can only target records with documented, timestamped opt-in in an immutable consent ledger. Cold lead outreach must rely on human-assisted `wa.me` links or external opt-in capture.

### S1 — Heuristic Phone Normalisation Permits Landlines and Distorts Reachability
- **Where:** `dedup.py:33-63` (`normalize_phone`) and `enricher.py:129-132, 204-206` (`_WHATSAPP_RE`).
- **Breaks:** `dedup.py:45` only enforces `len(digits) < 7`, indiscriminately prepending country calling codes (`+961` or `+966`). It lacks any numbering plan validation or mobile vs. landline classification. WhatsApp accounts can only be registered on **mobile numbers** (and rare `FIXED_LINE_OR_MOBILE` allocations). In Lebanon, fixed landlines (`01`, `04`, `05`, `07`, `08`, `09`) cannot receive WhatsApp messages; in Saudi Arabia, geographic landlines (`011` Riyadh, `012` Western, `013` Eastern) cannot receive WhatsApp messages.
- **Trigger:** A restaurant in Beirut with phone `01 234 567` normalizes to `+9611234567`. A medical clinic in Riyadh with landline `011 456 7890` normalizes to `+966114567890`. Both receive `completeness_score` bumps (`enricher.py:113`) and qualify as `sales_ready` (`main.py:82-90`), but both fail completely if messaged on WhatsApp.
- **Fix:** Replace regex surgery with Google's `phonenumbers` library (`phonenumbers.parse`, `phonenumbers.is_valid_number`, and `phonenumbers.number_type`). Only flag a record as WhatsApp-eligible if `number_type` is `PhoneNumberType.MOBILE` or `PhoneNumberType.FIXED_LINE_OR_MOBILE`.

### S2 — Scraped `wa.me` Links Lack E.164 Validation and Ingest Malformed Placeholders
- **Where:** `enricher.py:129-132` (`_WHATSAPP_RE`) and `enricher.py:204-206`.
- **Breaks:** `_WHATSAPP_RE` captures `(\d{7,15})` from `wa.me/` URLs on scanned websites and creates contacts with `"+" + m.group(1).lstrip("+")`. This captures placeholder links, developer stubs (e.g., `wa.me/123456789`, `wa.me/0000000000`), or numbers lacking international country prefixes.
- **Trigger:** A business website with `href="https://wa.me/03123456"` (Lebanese local mobile format without `+961`) writes `+03123456` into the `whatsapp` field. Downstream CSV consumers and sales reps clicking this number encounter an invalid number error.
- **Fix:** Run all regex-extracted WhatsApp digits through `phonenumbers.parse(raw_digits, default_region=record["country"])`. Discard any value where `phonenumbers.is_valid_number()` evaluates to `False`.

### S2 — Ignorance of Meta's Per-Message Pricing Model (In Force Since July 1, 2025)
- **Where:** Pipeline documentation, sales export schemas (`main.py:149-153`), and cost modeling.
- **Breaks:** System designers frequently assume WhatsApp still uses the legacy 24-hour Conversation-Based Pricing (CBP) model, where a single conversation fee covered unlimited messages for 24 hours. Effective **July 1, 2025**, Meta completely deprecated CBP and moved to **Per-Message Pricing (PMP)**. Every business-initiated template message delivered is charged individually.
- **Financial Trap:** In Saudi Arabia (+966), a delivered Marketing template costs **$0.0576** (or SAR 0.216) per message; in Lebanon (+961 / Rest of Middle East), it costs **$0.0392**. Sending multi-message sequences or retries to unvalidated scraped lists incurs high direct billing from Meta with zero delivery guarantee. Furthermore, effective **October 1, 2026**, service messages (replies sent inside an open 24-hour Customer Service Window) are billable at market utility rates once a number exceeds 1,000 service messages/month.
- **Fix:** Update all financial models and operational planning to reflect Meta's per-delivered-message rate card. Ensure marketing template dispatches are rate-limited, strictly monitored, and budgeted per recipient country code.

### S3 — Missing `wa.me` Deep Link Generation with Localized Context in Exports
- **Where:** `main.py:74-80` (`write_csv`) and `scrapers/base.py:5-28`.
- **Breaks:** Sales reps working from `sales_ready.csv` currently receive raw strings in `phone` and `whatsapp`. Reps must manually copy digits, add country codes, and open their WhatsApp client. If reps make formatting errors (e.g., preserving a leading zero or `+`), the client fails. Moreover, reps have no pre-filled pitch text, leading to inconsistent outreach.
- **Trigger:** Sales rep copies `+966501234567` into an unformatted URL `wa.me/+966501234567`. WhatsApp Web fails because `wa.me` syntax forbids leading `+` or non-numeric characters.
- **Fix:** Add a computed export column `whatsapp_click_url` containing a syntactically perfect, URL-encoded `wa.me` link:
  `https://wa.me/966501234567?text=%D9%85%D8%B1%D8%AD%D8%A8%D8%A7%20...` pre-populated with the business name and recommended pitch in Arabic or English.

---

## Technical Evaluation: WhatsApp Business Platform Architecture

The WhatsApp Business Platform allows programmatic communication with WhatsApp users globally. It is strictly separated from the consumer WhatsApp application and the small-business WhatsApp Business App.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          LEADMINER COMPLIANT ARCHITECTURE                   │
│                                                                             │
│  [ Scraped Data ] ──► [ phonenumbers Validation ]                           │
│                             │                                               │
│              ┌──────────────┴──────────────┐                                │
│              ▼                             ▼                                │
│     [ Landline / Invalid ]        [ Valid Mobile E.164 ]                    │
│              │                             │                                │
│              ▼                             ▼                                │
│     Exclude from WA              Has Documented Opt-In?                     │
│                                   │                 │                       │
│                           NO (Cold Lead)       YES (Consented Lead)         │
│                                   │                 │                       │
│                                   ▼                 ▼                       │
│                           [ wa.me Deep Link ]  [ Cloud API Engine ]         │
│                           - Human 1-to-1        - Approved Template         │
│                           - Inbound Lead Gen    - Per-Message Billing       │
│                           - Rep Battle-Card     - Webhook Suppression       │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 1. Cloud API vs. On-Premises API

Meta provides two deployment models for the WhatsApp Business Platform:

1. **Cloud API (Recommended & Standard):**
   - **Hosting:** Hosted globally on Meta's infrastructure.
   - **Protocol:** Standard HTTPS REST calls against the Meta Graph API (`https://graph.facebook.com/v21.0/{phone-number-id}/messages`).
   - **Maintenance:** Zero server maintenance, automated scaling, immediate access to new features (marketing optimizations, interactive buttons, authentication flows).
   - **Cost:** No infrastructure licensing fees; you only pay Meta's direct per-message network fees.
2. **On-Premises API (Legacy / Highly Regulated Enterprise):**
   - **Hosting:** Docker containers running CoreApp and WebApp hosted on your own AWS/GCP/bare-metal cluster.
   - **Maintenance:** High operational overhead (database maintenance, certificate management, regular CoreApp upgrades, high-availability database cluster).
   - **Verdict:** For `leadminer` and B2B growth operations, the **Cloud API** is the only logical choice.

### 2. Core Cloud API Infrastructure Prerequisites

Before any application can dispatch a single message via the Cloud API, the following Meta infrastructure must be configured:

1. **Meta Business Portfolio (formerly Business Manager):**
   - Must undergo **Business Verification** (uploading official commercial registration, e.g., Saudi Ministry of Commerce CR or Lebanese Ministry of Finance registration). Unverified businesses are restricted to 250 business-initiated conversations per rolling 24-hour period.
2. **Meta Developer App:**
   - Type: `Business`.
   - Products added: `WhatsApp`.
   - Permissions: `whatsapp_business_messaging`, `whatsapp_business_management`.
3. **WhatsApp Business Account (WABA):**
   - Container inside the Business Portfolio holding phone numbers, payment methods, message templates, and webhooks.
4. **Dedicated Phone Number:**
   - Cannot be currently registered on the consumer WhatsApp or WhatsApp Business mobile app. If a number is currently on a mobile phone, it must be explicitly deleted from the mobile app before registration on the Cloud API via two-factor SMS/voice verification.
5. **System User & Permanent Access Token:**
   - Permanent 60-day or system user token configured with granular scopes to prevent pipeline breakage due to token expiration.
6. **Webhook Server:**
   - Public HTTPS endpoint (valid TLS certificate) responding to `GET` verification challenges and handling `POST` delivery receipts and inbound messages.

---

## Template Messages and Conversation Categories

Outside of an active 24-hour Customer Service Window (opened only when a user sends an inbound message to your number), businesses **cannot send free-form text**. You can *only* send pre-approved **Template Messages**.

### 1. The Four Categories

Meta enforces four distinct message categories ([Meta Template Categorization Guide](https://developers.facebook.com/docs/whatsapp/business-management-api/message-templates)):

| Category | Definition & Scope | Outreach Suitability |
|---|---|---|
| **MARKETING** | Promotions, service announcements, cold pitches, discount offers, product recommendations, newsletters, or any message containing commercial persuasion. | **Mandatory** for any cold or warm B2B sales pitch. |
| **UTILITY** | Transactional confirmations, billing updates, appointment reminders, shipping notifications, or order status. Must be tied directly to a specific ongoing transaction. | **Strictly Prohibited** for pitches. Attempting to disguise a pitch as utility causes template rejection or immediate WABA suspension. |
| **AUTHENTICATION** | One-Time Passcodes (OTPs) for account login, password reset, or multi-factor verification. | Transactional security only. |
| **SERVICE** | Any free-form response sent by a customer service agent or automated bot *inside* an open 24-hour customer service window initiated by the user. | Inbound engagement only. |

### 2. Marketing Template Approval & The Mandatory Opt-Out Button

Meta enforces strict guidelines on Marketing templates:
- **Language and Syntax:** Templates are defined in specific languages (e.g., `ar` for Arabic, `en_US` for English) with dynamic positional parameters `{{1}}`, `{{2}}`.
- **Review Engine:** Templates are evaluated by automated machine-learning classifiers and human reviewers before approval.
- **Mandatory Opt-Out Mechanism:** Under Meta's current policy, marketing templates must provide a clear, friction-free way to unsubscribe. This is enforced via interactive **Quick Reply buttons** (e.g., `"إلغاء الاشتراك" / "Stop promotions"`).

#### Compliant Marketing Template Example (Arabic B2B Follow-up)
```json
{
  "name": "b2b_agency_outreach_ar",
  "category": "MARKETING",
  "language": "ar",
  "components": [
    {
      "type": "HEADER",
      "format": "TEXT",
      "text": "تطوير الحضور الرقمي لـ {{1}}"
    },
    {
      "type": "BODY",
      "text": "مرحباً {{2}}، لاحظنا في فريقنا أن موقعكم الإلكتروني بحاجة لتحديث تقني لزيادة المبيعات والطلبات المباشرة عبر الواتساب. يسعدنا تقديم استشارة مجانية لتحسين مبيعاتكم الرقمية."
    },
    {
      "type": "FOOTER",
      "text": "اضغط على الزر أدناه لإلغاء الاشتراك في أي وقت."
    },
    {
      "type": "BUTTONS",
      "buttons": [
        {
          "type": "QUICK_REPLY",
          "text": "طلب تفاصيل أكثر"
        },
        {
          "type": "QUICK_REPLY",
          "text": "إلغاء الاشتراك"
        }
      ]
    }
  ]
}
```

---

## Per-Message Pricing Model (In Force Now)

On **July 1, 2025**, Meta permanently retired Conversation-Based Pricing (CBP) and transitioned the entire WhatsApp Business Platform to **Per-Message Pricing (PMP)** ([Meta Pricing Documentation](https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing/)).

### 1. Key Rules of the Per-Message Pricing Model
1. **Billed on Delivery:** Charges are incurred *only* when a message is successfully delivered to the recipient's device (webhook status: `delivered`). Unreachable numbers or invalid mobile routes are not billed.
2. **Category & Market Driven:** Rates depend on two variables:
   - The **template category** (`MARKETING`, `UTILITY`, `AUTHENTICATION`, `SERVICE`).
   - The **recipient's country calling code** (not the sender's country).
3. **No 24-Hour Blanket Coverage:** Under legacy CBP, paying for one template opened a 24-hour window where subsequent templates were free. Under PMP, **every single template message delivered incurs an individual charge**. If you send 3 marketing templates in 24 hours, you are billed 3 marketing charges.
4. **Customer Service Window (CSW) Dynamics:**
   - When a user sends an inbound message, a **24-hour Customer Service Window** opens.
   - Utility templates sent inside an open CSW were made free as of July 1, 2025.
   - Non-template free-form responses inside the CSW are classified as **Service Messages**.
5. **2026 Service Message Updates (Effective October 1, 2026):**
   - Each business phone number receives a **free tier of 1,000 service messages per calendar month**.
   - Beyond 1,000 service messages, Meta charges for each delivered service message at the destination market's published utility rate.
6. **Free Entry Point (FEP) Window:**
   - If a user initiates a conversation via a Click-to-WhatsApp Facebook/Instagram ad or Facebook Page CTA, an extended **72-hour Free Entry Point window** opens.
   - All messages delivered inside this 72-hour window (including marketing templates) are **100% free of Meta delivery charges**.

### 2. Rate Card Comparison: Saudi Arabia vs. Lebanon

Current published Meta rates (USD list rate per delivered message, as of late 2025 / 2026):

| Market | Calling Code | Marketing Rate | Utility Rate | Authentication Rate | Service Rate (above 1,000/mo) |
|---|---|---|---|---|---|
| **Saudi Arabia** | `+966` | **$0.0576** (~SAR 0.216) | **$0.0107** (~SAR 0.040) | **$0.0107** | **$0.0107** |
| **Lebanon** *(Rest of Middle East)* | `+961` | **$0.0392** | **$0.0091** | **$0.0091** | **$0.0091** |

### 3. Cost Implications for `leadminer`
- Blasting 5,000 scraped Saudi numbers with an outbound marketing template:
  $$5,000 \times \$0.0576 = \$288.00 \text{ per blast}$$
- If 40% of numbers are landlines or inactive, money is wasted on failed attempts, but more critically, the remaining delivered messages will trigger spam blocks resulting in total account termination.

---

## Number Normalisation, Validation, and Filtering

### 1. Strict E.164 Standards
WhatsApp addresses users strictly via E.164 format:
`+[country code][subscriber number including area code]` (maximum 15 digits, no spaces, hyphens, brackets, or leading trunk zeros).

However, **different platform APIs expect different string formats**:
- **WhatsApp Cloud API (`POST /messages`):** Expects digits **without** the leading `+` (e.g., `"to": "966501234567"`).
- **`wa.me` Deep Links:** Expects digits **without** the leading `+` (e.g., `https://wa.me/966501234567`).
- **Internal Database / CSV Records:** Must store standard E.164 **with** leading `+` (e.g., `+966501234567`) for global telephony compatibility.

### 2. The Mobile vs. Landline Gate
The current codebase (`dedup.py:33-63`) fails to differentiate phone types. To fix this, `leadminer` must use `phonenumbers`:

```python
import phonenumbers
from phonenumbers import PhoneNumberType

def parse_and_validate_whatsapp(raw_phone: str, country: str) -> dict:
    """
    Validates a phone number and confirms WhatsApp mobile viability.
    Returns structured data or None if invalid.
    """
    if not raw_phone:
        return None
    try:
        parsed = phonenumbers.parse(raw_phone, country)
    except phonenumbers.NumberParseException:
        return None

    if not phonenumbers.is_valid_number(parsed):
        return None

    num_type = phonenumbers.number_type(parsed)
    # WhatsApp requires mobile or fixed-line-or-mobile allocations
    is_wa_eligible = num_type in (
        PhoneNumberType.MOBILE,
        PhoneNumberType.FIXED_LINE_OR_MOBILE
    )

    e164_standard = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    # Digits only for Cloud API and wa.me URLs
    digits_only = e164_standard.lstrip("+")

    return {
        "e164": e164_standard,
        "digits": digits_only,
        "is_wa_eligible": is_wa_eligible,
        "type": "MOBILE" if is_wa_eligible else "LANDLINE"
    }
```

### 3. WhatsApp Account Verification
The Cloud API **does not offer a free public endpoint to check whether a phone number has an active WhatsApp account**.
- Attempting to send a template to an active mobile number that has no WhatsApp account results in a Graph API error:
  `{"error": {"code": 131026, "message": "Receiver is incapable of receiving this message"}}`.
- Repeated delivery failures to inactive numbers degrade your account's quality rating.

---

## wa.me Deep Links: The Compliant Bridge for Human Outreach

For cold leads scraped from the web, automated Cloud API messaging is illegal and policy-violating. The compliant operational mechanism is **Click-to-Chat (`wa.me`) Deep Links** utilized by human sales development representatives (SDRs).

### 1. Technical Syntax Requirements
The official format ([WhatsApp Click to Chat Guide](https://faq.whatsapp.com/5913398998672934)) is:
`https://wa.me/<number>?text=<urlencoded_text>`

- **`<number>` Rules:** Must be numeric digits only in international format. **Do not include `+`, dashes, brackets, or leading zeros.**
  - Correct: `https://wa.me/966501234567`
  - Broken: `https://wa.me/+966501234567` (throws error in browser/app)
  - Broken: `https://wa.me/00966501234567`
  - Broken: `https://wa.me/966-50-123-4567`
- **`<text>` Rules:** Must be RFC 3986 URL-encoded UTF-8 text (e.g., spaces encoded as `%20`, newlines as `%0A`, Arabic characters properly percent-encoded).

### 2. Operational Superiority for `leadminer`
When a sales rep clicks an ergonomic `wa.me` link from `sales_ready.csv`:
1. It opens the rep's desktop or mobile WhatsApp application directly to a chat window with the lead.
2. The greeting and personalized pitch are pre-filled in the text composer.
3. The rep can tailor the opening sentence before pressing send.
4. **Policy Compliance:** The message is sent from an individual rep's WhatsApp Business app, or better, the link can be embedded on outbound emails/landing pages where the *lead* clicks it to contact the agency first.
5. If the lead clicks a `wa.me` link and messages the agency, the lead opens an inbound Customer Service Window, allowing compliant automated responses.

#### Generator Code for `leadminer` Exports
```python
import urllib.parse

def generate_wa_click_url(e164_phone: str, business_name: str, recommended_service: str) -> str:
    """
    Generates a compliant wa.me deep link with pre-filled Arabic pitch text.
    """
    if not e164_phone or not e164_phone.startswith("+"):
        return ""
    
    digits = e164_phone.lstrip("+")
    pitch_text = (
        f"السلام عليكم {business_name}، "
        f"معكم فريق تطوير الأعمال الرقمية. "
        f"اطلعنا على نشاطكم ونقترح تحسين {recommended_service} لزيادة وصولكم لعملاء جدد. "
        f"هل يناسبكم مناقشة سريعة؟"
    )
    
    encoded_text = urllib.parse.quote(pitch_text)
    return f"https://wa.me/{digits}?text={encoded_text}"
```

---

## Compliance Requirements: Opt-In, Legal Mandates, and Anti-Spam Violations

### 1. Meta Business Messaging Policy Mandates
Under Meta's [Business Messaging Policy](https://www.whatsapp.com/legal/business-messaging-policy/):
- **Explicit Agreement:** A business may only initiate outreach to a person who has affirmatively provided their number and consented to receive messages from **that specific business**.
- **Named Business:** General consent granted to a third party or aggregator does not transfer. If a business signs up for an OSM or Google Places directory, they consented to directory publication, *not* to WhatsApp promotional outreach from third-party agencies.
- **Immediate Opt-Out Honoring:** Any inbound message containing `"STOP"`, `"UNSUBSCRIBE"`, `"CANCEL"`, or Arabic equivalents (`"توقف"`, `"إلغاء"`) must be processed immediately. The business must permanently remove the user from all subsequent outreach.

### 2. Regional Legal Frameworks (KSA & Lebanon)

#### Saudi Arabia: Personal Data Protection Law (PDPL) & CITC Anti-Spam Regulations
- **Law:** Saudi Personal Data Protection Law (promulgated under Royal Decree No. M/19 and amended by Royal Decree No. M/148) overseen by the Saudi Data & AI Authority (SDAIA).
- **Direct Marketing Provisions (Article 26):** Personal data (including mobile phone numbers) cannot be processed for direct marketing without explicit, prior consent of the data subject.
- **Enforcement:** Sending unsolicited commercial communications to Saudi residents without proof of opt-in carries penalties of up to **SAR 5,000,000** and confiscation of commercial licenses.
- **Communications and Information Technology Commission (CITC) Regulations:** Commercial electronic messages require clear identity of sender, unbundled consent, and an active, free opt-out mechanism.

#### Lebanon: Law No. 81/2018 on Electronic Transactions and Personal Data
- **Provisions:** Articles 86–98 govern personal data processing and commercial advertising. Unsolicited commercial communications must provide clear identification of the sender and a simple, free-of-charge opt-out route.

### 3. What Constitutes a Violation?
| Action | Status | Consequence |
|---|---|---|
| Ingesting `sales_ready.csv` into a script calling Cloud API `POST /messages` | **STRICT VIOLATION** | Immediate user reports, Quality Rating drops to Red, WABA banned, PDPL liability. |
| Masking a cold sales pitch under a `UTILITY` template to reduce fees | **STRICT VIOLATION** | Template rejected or revoked; WABA penalized for template circumvention. |
| Ignoring an inbound `"STOP"` message and sending another message | **STRICT VIOLATION** | Permanent account ban with no appeal; regulatory fine. |
| Using unofficial WhatsApp automation tools (e.g., headless browser Puppeteer/Playwright puppet on WhatsApp Web) | **STRICT VIOLATION** | Instant phone number ban via WhatsApp anti-bot heuristics; violation of Meta Terms of Service. |

---

## Production Blueprint: The 4-Tier Compliant Engagement Engine

To safely leverage WhatsApp in `leadminer` without incurring bans or legal liability, the system must implement a tiered architecture:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            THE COMPLIANT ENGAGEMENT STACK                   │
│                                                                             │
│  TIER 1: Enrichment & Gating                                                │
│  - Validate E.164 via libphonenumber                                        │
│  - Filter out landlines (Lebanon 01/04..., KSA 011/012...)                  │
│  - Emit compliant wa.me deep links in sales_ready.csv                       │
│                                                                             │
│  TIER 2: Multi-Touch Inbound Opt-In Funnel                                  │
│  - Cold Outreach via Email / Phone / Ads (Compliant B2B cold outreach)      │
│  - Prospect clicks landing page or wa.me deep link                          │
│  - Prospect initiates chat -> Inbound CSW opens (72h or 24h free)           │
│                                                                             │
│  TIER 3: Consent Ledger & Ledger State Machine                              │
│  - Capture (phone, timestamp, opt_in_channel, consent_wording)              │
│  - Mark record as CLOUD_API_READY                                           │
│                                                                             │
│  TIER 4: Cloud API Outbound & Webhook Suppression                           │
│  - Send approved MARKETING template with Opt-Out button                     │
│  - Webhook listener captures delivery receipts                              │
│  - Webhook listener captures inbound "STOP" -> writes to suppression table  │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 1. Consent Ledger Database Schema
Before any phone number is placed into an automated Cloud API dispatch queue, it must exist in an audit-ready consent ledger:

```sql
CREATE TABLE whatsapp_consent_ledger (
    phone_e164 VARCHAR(20) PRIMARY KEY,
    business_id VARCHAR(64) NOT NULL,
    country_code VARCHAR(2) NOT NULL,
    opt_in_status VARCHAR(20) NOT NULL CHECK (opt_in_status IN ('PENDING', 'ACTIVE', 'REVOKED')),
    opt_in_channel VARCHAR(50) NOT NULL, -- 'INBOUND_WA', 'WEB_FORM', 'CHECKOUT'
    opt_in_wording TEXT NOT NULL,
    opt_in_timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
    opt_out_timestamp TIMESTAMP WITH TIME ZONE,
    opt_out_reason VARCHAR(100),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_consent_lookup ON whatsapp_consent_ledger(phone_e164, opt_in_status);
```

### 2. Webhook Ingestion & Automated Suppression Engine
A production Cloud API implementation requires a webhook server running 24/7 to capture delivery states and user opt-outs:

```python
from flask import Flask, request, jsonify

app = Flask(__name__)

STOP_KEYWORDS = {"stop", "unsubscribe", "cancel", "توقف", "الغاء", "إلغاء"}

@app.route("/webhooks/whatsapp", methods=["POST"])
def whatsapp_webhook():
    payload = request.get_json()
    
    # Process entry items
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            
            # Handle inbound user messages (Opt-out detection)
            messages = value.get("messages", [])
            for msg in messages:
                from_number = msg.get("from")
                msg_type = msg.get("type")
                
                text_body = ""
                if msg_type == "text":
                    text_body = msg.get("text", {}).get("body", "").strip().lower()
                elif msg_type == "button":
                    text_body = msg.get("button", {}).get("text", "").strip().lower()

                if text_body in STOP_KEYWORDS:
                    suppress_contact(f"+{from_number}", reason=f"Keyword '{text_body}' received")
            
            # Handle status callbacks (Delivery & Quality tracking)
            statuses = value.get("statuses", [])
            for status in statuses:
                recipient_id = status.get("recipient_id")
                current_status = status.get("status") # sent, delivered, read, failed
                if current_status == "failed":
                    errors = status.get("errors", [])
                    log_delivery_failure(recipient_id, errors)

    return jsonify({"status": "EVENT_RECEIVED"}), 200

def suppress_contact(phone_e164: str, reason: str):
    """Marks contact as REVOKED in database to prevent further messaging."""
    print(f"SUPPRESSING {phone_e164}: {reason}")
    # UPDATE whatsapp_consent_ledger SET opt_in_status='REVOKED', opt_out_timestamp=NOW() WHERE phone_e164=...
```

---

## Recommended Order of Work

1. **Immediate Codebase Fix — Phone & WhatsApp Normalisation (`dedup.py` & `enricher.py`):**
   - Install `phonenumbers==9.0.30`.
   - Update `dedup.normalize_phone` to parse, validate against country metadata, and verify mobile vs. landline type.
   - Refactor `enricher.py:129-132` to run scraped `wa.me` strings through `phonenumbers.parse` and discard invalid or non-mobile numbers.
2. **Export Enhancement — Add Actionable `whatsapp_click_url` (`main.py`):**
   - Add a computed column `whatsapp_click_url` in `sales_ready.csv`.
   - Format URLs strictly as `https://wa.me/<digits_without_plus>?text=<url_encoded_arabic_pitch>`.
   - Enable sales reps to conduct compliant, 1-to-1 outreach directly from their devices.
3. **Architecture Segregation — Enforce API Dispatch Isolation:**
   - Explicitly document and codify that `leadminer` output feeds an SDR workbench or inbound marketing funnel, **not** an automated Cloud API queue.
   - Implement an automated consent validation check: if an automated dispatcher ever reads a lead without a corresponding record in `whatsapp_consent_ledger`, execution hard-fails.
4. **Cloud API Infrastructure Setup (When Running Inbound/Warm Campaigns):**
   - Complete Meta Business Portfolio verification for the operating legal entity.
   - Provision a clean phone number and register it on WhatsApp Cloud API.
   - Submit and obtain approval for localized Marketing templates with mandatory Quick Reply opt-out buttons.
5. **Implement Webhook & Suppression Engine:**
   - Deploy an HTTPS webhook endpoint subscribed to `messages` and `message_status`.
   - Implement real-time keyword parsing for `"STOP"` / `"إلغاء"` with immediate database suppression to maintain Green quality rating.

---

## Reference Links & Official Documentation

- **Meta WhatsApp Business Messaging Policy:**  
  [https://www.whatsapp.com/legal/business-messaging-policy/](https://www.whatsapp.com/legal/business-messaging-policy/)
- **Meta WhatsApp Business Platform Pricing & Per-Message Transition:**  
  [https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing/](https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing/)
- **Meta Pricing Non-Template & Service Message Updates (2026):**  
  [https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing/non-template-messages](https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing/non-template-messages)
- **Meta Message Template Guidelines & Categorization:**  
  [https://developers.facebook.com/docs/whatsapp/business-management-api/message-templates](https://developers.facebook.com/docs/whatsapp/business-management-api/message-templates)
- **Meta WhatsApp Business Platform Policy Enforcement:**  
  [https://developers.facebook.com/docs/whatsapp/overview/policy-enforcement/](https://developers.facebook.com/docs/whatsapp/overview/policy-enforcement/)
- **WhatsApp Official Click to Chat (`wa.me`) Documentation:**  
  [https://faq.whatsapp.com/5913398998672934](https://faq.whatsapp.com/5913398998672934)
- **Saudi Data & AI Authority (SDAIA) — Personal Data Protection Law (PDPL):**  
  [https://sdaia.gov.sa/en/SDAIA/about/Pages/PersonalDataProtectionLaw.aspx](https://sdaia.gov.sa/en/SDAIA/about/Pages/PersonalDataProtectionLaw.aspx)
- **Communications, Space & Technology Commission (CST / CITC) Spam Regulations:**  
  [https://www.cst.gov.sa/en/rulesandregulations/regulatorydocuments/otherregulatorydocuments/Documents/SPAM-Regulations-EN.pdf](https://www.cst.gov.sa/en/rulesandregulations/regulatorydocuments/otherregulatorydocuments/Documents/SPAM-Regulations-EN.pdf)
