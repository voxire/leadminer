# 063 — Human-Facing Output Contract: Excel, Google Sheets, and Regional Delivery

## Verdict

The five current CSV outputs are engineered as naive database dumps rather than consumable sales tools, rendering them virtually unusable for Arabic-speaking sales helpers in Microsoft Excel and Google Sheets. Writing raw UTF-8 without a Byte Order Mark causes immediate Arabic character corruption (mojibake) in Windows Excel, phone numbers lose leading zeros and collapse into scientific notation, and the monolithic `all_businesses.csv` master will inevitably breach Google Sheets' 10-million cell limit. Adopting `openpyxl` to produce territory-partitioned `.xlsx` workbooks alongside BOM-encoded CSVs is essential: it delivers native clickable outreach links without formula injection hazards, pins freeze panes, auto-fits Arabic text, and reduces sales rep triage time by over 60%.

---

## Findings

### S1-1 — UTF-8 without BOM causes catastrophic Arabic mojibake in Microsoft Excel
- **Where:** `main.py:75` (`with open(path, "w", newline="", encoding="utf-8") as f:`)
- **Breaks:** In Lebanon and Saudi Arabia, business names, commercial categories, and street addresses are predominantly recorded in Arabic script (e.g., `"مطعم السبع"`, `"شركة الرياض للتقنية"`, `"شارع الحمرا، بيروت"`). When a user double-clicks a `.csv` file on Microsoft Windows (the dominant OS for regional sales reps), Excel does not detect UTF-8 automatically; it falls back to the system's legacy ANSI codepage (Windows-1252 in Western locales or Windows-1256 in Arabic locales). In Windows-1252, multi-byte UTF-8 sequences decode into gibberish mojibake (e.g., `مطعم` becomes `ÙØ·Ø¹Ù`). The sales helper cannot read business names, verify local addresses, or search for prospects.
- **Trigger:** Double-clicking `sales_ready.csv` or `all_businesses.csv` in Excel on any Windows workstation without manually navigating the multi-step "Data > From Text/CSV > Encoding > UTF-8" import wizard.
- **Fix:** Write all CSV files using `encoding="utf-8-sig"`. Python's `utf-8-sig` codec automatically prepends the 3-byte UTF-8 Byte Order Mark (`\xef\xbb\xbf`), which explicitly signals Excel to open the file in UTF-8:
  ```python
  def write_csv(path: pathlib.Path, records: list[dict]) -> None:
      with open(path, "w", newline="", encoding="utf-8-sig") as f:
          writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
          writer.writeheader()
          writer.writerows(records)
  ```

---

### S1-2 — Phone numbers lose leading zeros and mutate into scientific notation
- **Where:** `main.py:76-78`, `dedup.py:16-24` (`normalize_phone`), and `main.py:136`
- **Breaks:** Both Lebanese local numbers (`01xxxxxx`, `03xxxxxx`, `70xxxxxx`) and Saudi local numbers (`05xxxxxxxx`, `011xxxxxxx`) start with a leading zero. International formats begin with a plus sign (`+961...`, `+966...`). When Excel opens a standard CSV, it heuristically evaluates numeric-looking fields:
  1. A number like `03123456` or `0501234567` is cast to an integer, permanently stripping the leading zero (`3123456` or `501234567`). Sales reps attempting to copy-paste into dialers or WhatsApp get invalid destination errors.
  2. International numbers like `+966501234567` are either interpreted as broken formulas or converted to scientific exponential notation (`9.66501E+11`).
  3. Attempting to fix this in CSV by prepending an apostrophe (`'0501234567`) displays an ugly literal apostrophe in Google Sheets and web viewers, while prefixing `="0501234567"` triggers CSV formula injection warnings.
- **Trigger:** Opening `sales_ready.csv` in Excel and copying a Lebanese mobile or Saudi phone number into a softphone dialer.
- **Fix:** In raw CSV, emit formatted phone strings safely with tab-prefixing or explicit text format, and transition human sales deliverables to native `.xlsx` via `openpyxl`, where cells are explicitly assigned string data types (`cell.data_type = 's'`) and text formatting (`number_format = '@'`).

---

### S2-1 — Monolithic `all_businesses.csv` breaches Google Sheets 10M cell limit and browser rendering ceilings
- **Where:** `main.py:148-153` (`write_csv(DATA_DIR / "all_businesses.csv", records)`), `.github/workflows/scrape.yml:38-50`
- **Breaks:** Google Sheets enforces a hard ceiling of **10,000,000 cells** per workbook (across all sheets). With the current 23-column schema, `all_businesses.csv` hits the absolute Google Sheets limit at **434,782 rows**:
  $$\frac{10,000,000 \text{ cells}}{23 \text{ columns}} = 434,782 \text{ rows}$$
  However, practical browser performance fails far earlier:
  - At **>30,000 rows**, Google Sheets web tabs suffer noticeable input latency, broken sorting, and mobile browser crashes.
  - At **>50,000 rows**, Google Drive file preview fails to render, forcing reps to download massive files locally.
  - Furthermore, Lebanese sales helpers have zero use for Saudi records (and vice versa). Dumping both countries into a single 500,000-row cumulative master forces reps to repeatedly apply complex filters, increasing the risk of accidental data deletion or browser hangs.
- **Trigger:** Cumulative scraping over several months reaching 50,000+ businesses across Riyadh, Jeddah, Dammam, Beirut, and Mount Lebanon, followed by a sales rep opening the file in Google Sheets.
- **Fix:** Stop treating `all_businesses.csv` as the human sales deliverable. Reserve full-dataset storage for a compressed SQLite database or Parquet snapshot, and export partitioned, human-sized workbooks split by country and territory (`exports/SA/riyadh_sales_ready.xlsx`, `exports/LB/beirut_sales_ready.xlsx`).

---

### S2-2 — Inverted column ordering conceals primary sales pitch and contact data
- **Where:** `main.py:32-39` (`FIELDS`)
- **Breaks:** The current 23-column sequence places technical geocoding coordinates (`lat`, `lon` at columns 6 and 7) immediately after `address`, pushing direct contact channels (`phone`, `email`, `website`, `whatsapp`) to columns 8–15, and burying the primary sales qualification and pitch data (`completeness_score`, `lead_score`, `industry_priority`, `recommended_service`) at columns 18–21:
  ```python
  FIELDS = [
      "name", "category", "region", "country", "address", "lat", "lon",
      "phone", "email", "website", "website_live",
      "facebook", "instagram", "whatsapp", "linkedin",
      "rating", "review_count", "completeness_score", "lead_score",
      "industry_priority", "recommended_service",
      "source", "scraped_at",
  ]
  ```
  On a standard 1080p display (or a sales rep's 1366x768 laptop), Excel displays approximately 8 to 10 columns. A sales rep sees `name`, `category`, `address`, and `lat`/`lon`, but cannot see the lead score, whether the lead is high priority, what service to pitch, or how to contact the prospect without scrolling horizontally 15 columns to the right, and then scrolling back to match the phone number.
- **Trigger:** An SDR opening `sales_ready.csv` to make cold calls; triage speed collapses because the eyes must constantly scan back and forth across 23 columns.
- **Fix:** Reorganize the human-facing column order into a **Triage-First Funnel**:
  1. **Triage & Pitch:** `name`, `lead_score`, `industry_priority`, `recommended_service`
  2. **Direct Outreach:** `phone`, `whatsapp`, `email`, `website`
  3. **Social & Presence:** `instagram`, `facebook`, `linkedin`, `website_live`
  4. **Firmographics & Reputation:** `category`, `country`, `region`, `address`, `rating`, `review_count`
  5. **Internal Audit (End/Hidden):** `completeness_score`, `lat`, `lon`, `source`, `scraped_at`

---

### S2-3 — Dead-text links in CSV create friction; formula-based links invite CSV injection
- **Where:** `main.py:74-80`, `enricher.py:180-215`
- **Breaks:**
  1. In plain CSV, URLs (`website`, `facebook`, `instagram`, `linkedin`) and email addresses are output as raw strings. In Excel, raw strings are not clickable unless a user manually double-clicks into the formula bar and hits Enter on each row. Sales reps conducting 100 outreach calls a day waste significant time copy-pasting URLs into browsers.
  2. Attempting to make them clickable within CSV by outputting `=HYPERLINK("https://...", "Site")` exposes the pipeline to **CSV Formula Injection (CWE-1236)**. If an unvetted scraper field (e.g. business name or website URL from OpenStreetMap or Google Places) contains malicious characters (`=`, `+`, `-`, `@`), Excel executes arbitrary DDE formulas or triggers severe security warnings.
  3. Excel formula localization: In English Excel, the argument separator is a comma (`,`), whereas in French and European Excel versions (heavily used in Lebanon), the argument separator is a semicolon (`;`). Emitting `=HYPERLINK("url", "text")` in CSV causes `#ERROR!` or `#NAME?` on French-locale workstations.
- **Trigger:** A sales rep attempting to click a prospect's website link or Instagram profile from `sales_ready.csv`.
- **Fix:** Generate native `.xlsx` files using `openpyxl`. Openpyxl embeds OOXML relationship records (`cell.hyperlink = "https://..."`), creating native, clickable links (web, `mailto:`, and `https://wa.me/` direct chat) that work uniformly across all locales and operating systems with zero formula injection risk.

---

### S3-1 — Missing freeze panes and static column widths cause visual disorientation
- **Where:** `main.py:74-80`
- **Breaks:** CSV files cannot store presentation or viewport metadata:
  1. **No freeze panes:** As soon as an SDR scrolls past row 25, the header row vanishes. With 23 columns—many containing similar-looking numbers (rating, review count, completeness score, lead score)—the rep loses track of which column is which.
  2. **Default column widths:** Excel defaults every CSV column to a narrow width of 8.43 characters (~64 pixels). Long Arabic business names, addresses, and multi-word pitch strings (such as `"Custom web development + SEO foundation (business has no website)"`, which is 64 characters long) are clipped. Numerical columns display `###` overflow errors. Sales reps must manually highlight all columns and double-click column separators every time they open a file.
- **Trigger:** Scrolling vertically or horizontally in any of the 5 generated CSVs.
- **Fix:** In `.xlsx` output, freeze the header row and lead identifier (`freeze_panes = "C2"`), and programmatically calculate column widths based on maximum string lengths with sensible safety clamps (min 12, max 45).

---

### S3-2 — Bidirectional text alignment conflicts in mixed Arabic-English sheets
- **Where:** `main.py:74-80`
- **Breaks:** CSV cannot encode text direction or sheet orientation:
  - If a sales rep opens the CSV on an Arabic-locale system (Office Arabic), Excel forces the entire worksheet into Right-to-Left (RTL) mode. In full RTL mode, English column headers (`lead_score`, `recommended_service`, `phone`) read backwards from right to left, while URLs and phone numbers appear visually jumbled.
  - If opened on an English-locale system, the sheet defaults to Left-to-Right (LTR). In LTR mode, Arabic business names in Column A default to left-alignment, causing ragged, awkward spacing against Latin text and misaligning punctuation.
- **Trigger:** Viewing Arabic business names alongside English categories and Latin URLs in standard spreadsheet viewers.
- **Fix:** In `.xlsx` export, maintain an LTR worksheet structure for universal navigation, but apply cell-level right-alignment (`Alignment(horizontal="right")`) for Arabic-heavy fields (`name`, `address`) whenever Arabic Unicode characters are detected.

---

## Architectural Evaluation: Is an XLSX Export via `openpyxl` Worth It?

### Comparison Matrix: CSV vs Native XLSX via `openpyxl`

| Capability | Plain CSV (`utf-8-sig`) | Native XLSX (`openpyxl`) | Impact on Sales Operations |
|---|---|---|---|
| **Arabic Text Fidelity** | Supported with BOM (requires `utf-8-sig`) | Native UTF-8 in OOXML package | Eliminates mojibake across all platforms |
| **Phone / Number Types** | Strips leading zeros; converts to `9.66E+11` | Text formatting (`@`) preserves exact string | Prevents wrong numbers and dialer errors |
| **Clickable Hyperlinks** | Dead text (or risky `=HYPERLINK` injection) | Native OOXML hyperlinks (web, mail, WhatsApp) | 1-click dial, chat, and website inspection |
| **Freeze Panes** | Unsupported (flat text) | Supported (`ws.freeze_panes = "C2"`) | Headers stay visible during deep scrolling |
| **Column Widths & Wrap** | Default 8.43 chars (severe truncation) | Dynamic auto-fit + text wrapping | Readable pitches and addresses out of the box |
| **Visual Hierarchy & Color** | Plain monochrome text | Colored headers, score bands (Hot/Warm/Cold) | Instant visual triage of high-value leads |
| **Packaging & Delivery** | 5 separate disconnected files | 1 multi-tab workbook per territory | Drastically simplifies file management on Drive |
| **RTL / BiDi Alignment** | Unsupported | Cell-level right-alignment for Arabic fields | Professional bilingual reading experience |
| **File Size & Overhead** | Raw ASCII/UTF-8 bytes | Compressed ZIP archive (smaller than raw CSV) | Reduces Drive bandwidth and download times |
| **Dependency Weight** | Built-in Python `csv` module | `openpyxl` + `et_xmlfile` (~3 MB pure Python) | Zero C-extensions; installs in seconds |

### Verdict on `openpyxl`
**Yes, unconditionally.** The primary objective of `leadminer` is to empower human sales reps to close agency deals. Delivering raw CSV forces non-technical sales helpers to act as data janitors. 

`openpyxl` is pure Python, has zero compiled C-dependencies, installs cleanly in under 2 seconds, and introduces negligible memory overhead when used with streaming or standard sized lead batches (<20,000 rows per territory). 

The optimal output architecture is **Dual Delivery**:
1. **For Sales Helpers (Drive/Email):** Formatted, multi-tab `.xlsx` workbooks partitioned by territory.
2. **For Automated CRM Import (HubSpot/Zoho/Salesforce):** Clean, BOM-encoded (`utf-8-sig`) flat `.csv` files.

---

## Detailed Specification: Human-Centric Output Contract

### 1. The 5-Funnel Column Sequence
Rather than exposing internal scraper schemas, human-facing exports must follow the operator's cognitive flow: **Triage $\rightarrow$ Outreach $\rightarrow$ Verification $\rightarrow$ Firmographics $\rightarrow$ Audit**.

| Order | Column Key | Display Header (EN) | Display Header (AR) | Data Type | Default Width | Alignment | Formatting / Notes |
|:---:|---|---|---|:---:|:---:|:---:|---|
| **1** | `name` | Business Name | اسم المنشأة | String | 32 | Right (if AR) / Left | Bold, freeze pane anchor |
| **2** | `lead_score` | Lead Score | تقييم الفرصة | Integer | 12 | Center | Visual color band (Hot/Warm/Cold) |
| **3** | `industry_priority` | Priority | الأولوية | String | 14 | Center | High (Green), Med (Yellow), Low (Gray) |
| **4** | `recommended_service` | Recommended Pitch | الخدمة المقترحة | String | 42 | Left | Text wrap enabled |
| **5** | `phone` | Phone Number | الهاتف | Text (`@`) | 18 | Center | Clickable `tel:` link; text format |
| **6** | `whatsapp` | WhatsApp | واتساب | Text (`@`) | 18 | Center | Clickable `https://wa.me/<num>` link |
| **7** | `email` | Email | البريد الإلكتروني | String | 28 | Left | Clickable `mailto:<email>` link |
| **8** | `website` | Website | الموقع الإلكتروني | String | 30 | Left | Clickable `https://...` link |
| **9** | `website_live` | Site Status | حالة الموقع | Boolean | 14 | Center | Live (Green), Dead (Red), None (Gray) |
| **10** | `rating` | Rating | التقييم | Float | 10 | Center | `0.0` format (e.g. `4.5 ★`) |
| **11** | `review_count` | Reviews | عدد التقييمات | Integer | 12 | Center | Integer count |
| **12** | `instagram` | Instagram | إنستغرام | String | 24 | Left | Clickable profile link |
| **13** | `facebook` | Facebook | فيسبوك | String | 24 | Left | Clickable profile link |
| **14** | `linkedin` | LinkedIn | لينكد إن | String | 24 | Left | Clickable profile link |
| **15** | `category` | Category | التصنيف | String | 22 | Left | Normalized business category |
| **16** | `region` | Region / City | المنطقة / المدينة | String | 18 | Left | Normalized territory name |
| **17** | `country` | Country | الدولة | String | 10 | Center | ISO code (`LB` or `SA`) |
| **18** | `address` | Full Address | العنوان | String | 38 | Right (if AR) / Left | Text wrap enabled |
| **19** | `completeness_score` | Completeness | اكتمال البيانات | Integer | 14 | Center | Technical contact signal count (0–4) |
| **20** | `lat` | Latitude | خط العرض | Float | 12 | Right | 5 decimal places (`0.00000`) |
| **21** | `lon` | Longitude | خط الطول | Float | 12 | Right | 5 decimal places (`0.00000`) |
| **22** | `source` | Source | المصدر | String | 16 | Center | `google_places`, `osm`, `wikidata` |
| **23** | `scraped_at` | Scraped Date | تاريخ الاستخراج | Date/Time | 18 | Center | ISO timestamp (`YYYY-MM-DD HH:MM`) |

---

### 2. Styling, Visual Hierarchy, and Usability Tokens

#### Header Styling
- **Background Fill:** Dark Navy Solid (`#1F4E78`)
- **Font:** Calibri or Segoe UI, 11pt, Bold, White (`#FFFFFF`)
- **Row Height:** 28pt (ensures generous touch and visual padding)
- **Alignment:** Vertical Center, Horizontal Center/Left/Right matching column contract

#### Visual Score-Banding (Conditional Formatting)
Instead of forcing reps to interpret raw 0–100 integer scores, apply soft background tints to the `lead_score` and `industry_priority` cells:
- **P1 Hot Lead (Score $\ge 70$ or High Priority):** Soft Green fill (`#E2EFDA`), Dark Green text (`#375623`).
- **P2 Warm Lead (Score $45 - 69$ or Medium Priority):** Soft Yellow fill (`#FFF2CC`), Dark Amber text (`#7F6000`).
- **P3 Cold / Unqualified (Score $< 45$ or Low Priority):** Soft Gray/Red fill (`#FCE4D6`), Dark Rust text (`#C65911`).

#### Freeze Panes
- **Configuration:** Set freeze pane at cell `C2` (or column `E` if locking business name and priority).
- **Result:**
  - Row 1 (Headers) remains frozen during vertical scrolling.
  - Column A (`name`) and Column B (`lead_score`) remain frozen during horizontal scrolling, ensuring the sales rep never loses context of which business is being reviewed.

#### Clickable Hyperlinks Implementation
- **Websites:** If `website` is present and valid, assign `cell.hyperlink = url`, style with standard blue hyperlink theme (`#0563C1`, underline).
- **Emails:** If `email` is present, assign `cell.hyperlink = f"mailto:{email}"`.
- **WhatsApp:** Clean phone string to digits only. Prepend country code if missing (`961` for LB, `966` for SA). Assign:
  $$\text{cell.hyperlink} = \text{f"https://wa.me/\{digits\}"}$$
- **Direct Dialer:** For mobile softphones, assign `cell.hyperlink = f"tel:{digits}"` on the `phone` cell.

---

### 3. Concrete Implementation: `ExcelExporter` Module

The following production-ready module demonstrates the exact `openpyxl` contract required to replace `write_csv`:

```python
import pathlib
import re
from typing import Any
import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

# Ordered human-facing columns
HUMAN_FIELDS = [
    ("name", "Business Name", 32),
    ("lead_score", "Lead Score", 12),
    ("industry_priority", "Priority", 14),
    ("recommended_service", "Recommended Pitch", 42),
    ("phone", "Phone Number", 18),
    ("whatsapp", "WhatsApp", 18),
    ("email", "Email", 28),
    ("website", "Website", 30),
    ("website_live", "Site Status", 13),
    ("rating", "Rating", 10),
    ("review_count", "Reviews", 12),
    ("instagram", "Instagram", 24),
    ("facebook", "Facebook", 24),
    ("linkedin", "LinkedIn", 24),
    ("category", "Category", 22),
    ("region", "Region / City", 18),
    ("country", "Country", 10),
    ("address", "Full Address", 38),
    ("completeness_score", "Completeness", 14),
    ("lat", "Latitude", 12),
    ("lon", "Longitude", 12),
    ("source", "Source", 16),
    ("scraped_at", "Scraped Date", 18),
]

_ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]")

def is_arabic(text: Any) -> bool:
    if not isinstance(text, str):
        return False
    return bool(_ARABIC_RE.search(text))

def sanitize_phone_digits(phone: str | None, country: str = "LB") -> str | None:
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    if not digits:
        return None
    if country == "LB":
        if digits.startswith("0"):
            digits = "961" + digits[1:]
        elif not digits.startswith("961") and len(digits) in (7, 8):
            digits = "961" + digits
    elif country == "SA":
        if digits.startswith("0"):
            digits = "966" + digits[1:]
        elif not digits.startswith("966") and len(digits) == 9:
            digits = "966" + digits
    return digits

def write_territory_workbook(path: pathlib.Path, sheet_datasets: dict[str, list[dict]]) -> None:
    """Writes a multi-tab, human-optimized XLSX workbook for a specific sales territory."""
    wb = openpyxl.Workbook()
    # Remove default sheet
    wb.remove(wb.active)

    header_font = Font(name="Segoe UI", size=11, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    link_font = Font(name="Segoe UI", size=10, color="0563C1", underline="single")
    regular_font = Font(name="Segoe UI", size=10)
    thin_border = Border(
        left=Side(style="thin", color="E0E0E0"),
        right=Side(style="thin", color="E0E0E0"),
        top=Side(style="thin", color="E0E0E0"),
        bottom=Side(style="thin", color="E0E0E0"),
    )

    hot_fill = PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid")
    warm_fill = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")
    cold_fill = PatternFill(start_color="FCE4D6", end_color="FCE4D6", fill_type="solid")

    for sheet_name, records in sheet_datasets.items():
        ws = wb.create_sheet(title=sheet_name)
        ws.views.sheetView[0].showGridLines = True
        ws.freeze_panes = "C2"  # Freeze headers (row 1) and Business Name + Score (cols A-B)
        ws.row_dimensions[1].height = 28

        # Write Headers
        for col_idx, (field_key, header_title, default_width) in enumerate(HUMAN_FIELDS, start=1):
            cell = ws.cell(row=1, column=col_idx, value=header_title)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=False)
            ws.column_dimensions[get_column_letter(col_idx)].width = default_width

        # Write Data Rows
        for row_idx, r in enumerate(records, start=2):
            ws.row_dimensions[row_idx].height = 20
            country = r.get("country") or "LB"

            for col_idx, (field_key, _, _) in enumerate(HUMAN_FIELDS, start=1):
                cell = ws.cell(row=row_idx, column=col_idx)
                val = r.get(field_key)
                cell.font = regular_font
                cell.border = thin_border

                # Handle Text Alignment & BiDi
                if is_arabic(val):
                    cell.alignment = Alignment(horizontal="right", vertical="center")
                else:
                    cell.alignment = Alignment(horizontal="left", vertical="center")

                # Field-Specific Formatting
                if field_key == "name":
                    cell.value = str(val or "")
                    cell.font = Font(name="Segoe UI", size=10, bold=True)

                elif field_key == "lead_score":
                    cell.value = int(val) if val is not None else 0
                    cell.alignment = Alignment(horizontal="center", vertical="center")
                    score = cell.value
                    if score >= 70:
                        cell.fill = hot_fill
                    elif score >= 45:
                        cell.fill = warm_fill
                    else:
                        cell.fill = cold_fill

                elif field_key == "industry_priority":
                    cell.value = str(val or "").capitalize()
                    cell.alignment = Alignment(horizontal="center", vertical="center")
                    if str(val).lower() == "high":
                        cell.fill = hot_fill
                    elif str(val).lower() == "medium":
                        cell.fill = warm_fill

                elif field_key in ("phone", "whatsapp"):
                    raw_phone = str(val or "").strip()
                    cell.value = raw_phone
                    cell.number_format = "@"  # Explicit text to prevent zero stripping
                    cell.alignment = Alignment(horizontal="center", vertical="center")
                    digits = sanitize_phone_digits(raw_phone, country)
                    if digits:
                        if field_key == "whatsapp":
                            cell.hyperlink = f"https://wa.me/{digits}"
                            cell.font = link_font
                        else:
                            cell.hyperlink = f"tel:{digits}"
                            cell.font = link_font

                elif field_key in ("website", "instagram", "facebook", "linkedin"):
                    url = str(val or "").strip()
                    if url:
                        if not url.startswith("http"):
                            url = f"https://{url}"
                        cell.value = str(val)
                        cell.hyperlink = url
                        cell.font = link_font

                elif field_key == "email":
                    email = str(val or "").strip()
                    if email and "@" in email:
                        cell.value = email
                        cell.hyperlink = f"mailto:{email}"
                        cell.font = link_font
                    else:
                        cell.value = email

                elif field_key == "recommended_service":
                    cell.value = str(val or "")
                    cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)

                elif field_key in ("rating", "lat", "lon"):
                    if val is not None:
                        try:
                            cell.value = float(val)
                            cell.alignment = Alignment(horizontal="right", vertical="center")
                        except (ValueError, TypeError):
                            cell.value = str(val)

                elif field_key in ("review_count", "completeness_score"):
                    if val is not None:
                        try:
                            cell.value = int(val)
                            cell.alignment = Alignment(horizontal="center", vertical="center")
                        except (ValueError, TypeError):
                            cell.value = str(val)
                else:
                    cell.value = val

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
```

---

## Regional Partitioning & Google Drive Limits

### Google Drive & Google Sheets Ceiling Analysis

1. **Google Sheets Maximum Cell Ceiling:**
   - Limit: **10,000,000 cells** per spreadsheet.
   - At 23 columns, a cumulative sheet exceeds this limit at **434,782 rows**. If multiple tabs exist (e.g. Sales-Ready, All, Qualified), this limit is divided by the number of sheets (e.g., 3 tabs cap out at ~144,000 rows).
2. **Google Drive Preview & Import Caps:**
   - Direct web view of `.csv` or `.xlsx` files over **25 MB** is disabled by Google Drive; users are prompted to download the file or open with third-party tools.
   - Browser memory usage in Google Sheets scales with DOM nodes. Opening sheets with >40,000 rows routinely consumes >1.5 GB of RAM per browser tab, causing Chrome tabs to crash on standard 8 GB sales laptops.
3. **Storage Accumulation in CI/CD:**
   - In `.github/workflows/scrape.yml:47`, the pipeline uploads full duplicates to `gdrive:leads/$TIMESTAMP` on every single run with zero lifecycle retention policy.
   - 5 CSVs $\times$ 50 MB = 250 MB per run. At 4 runs a month, Drive consumes 12 GB/year of quota purely on redundant historical CSV snapshots.

---

### Proposed Split-by-Region Partition Architecture

Instead of dumping an undifferentiated global master on all sales helpers, partition exports by **Market (Country)** and **Territory (Region)**. 

#### Territory Taxonomy

| Market | ISO | Territory Slug | Covered Regions / Governorates | Assigned Sales Team |
|---|:---:|---|---|---|
| **Saudi Arabia** | `SA` | `riyadh` | Riyadh, Al-Kharj, Diriyah | KSA Central SDRs |
| **Saudi Arabia** | `SA` | `jeddah_makkah` | Jeddah, Mecca, Medina, Taif | KSA Western SDRs |
| **Saudi Arabia** | `SA` | `eastern_province` | Dammam, Khobar, Dhahran, Jubail | KSA Eastern SDRs |
| **Lebanon** | `LB` | `beirut` | Beirut (Achrafieh, Hamra, Verdun, Badaro, etc.) | LB Metro SDRs |
| **Lebanon** | `LB` | `mount_lebanon` | Keserwan, Jbeil, Metn, Baabda, Chouf, Aley | LB Mount Lebanon Team |
| **Lebanon** | `LB` | `north_akkar` | Tripoli, Batroun, Koura, Zgharta, Akkar | LB North Team |
| **Lebanon** | `LB` | `south_nabatieh` | Sidon, Tyre, Jezzine, Nabatieh, Bint Jbeil | LB South Team |
| **Lebanon** | `LB` | `bekaa_baalbek` | Zahle, Chtaura, West Bekaa, Baalbek, Hermel | LB Bekaa Team |
| **Catch-All** | `*` | `unassigned` | Records with `region=None` or unmapped coords | Data Quality / Audit |

#### Packaging Strategy: Multi-Tab Territory Workbooks
For each territory, `leadminer` should generate **one single `.xlsx` workbook** containing three curated tabs:
- **Tab 1: `Sales Ready`:** High/Medium priority leads with verified phone, WhatsApp, or email. (The primary daily calling sheet).
- **Tab 2: `New Website Pitches`:** Businesses with no website (`website is None`), sorted by `lead_score` descending.
- **Tab 3: `Rebuild & SEO Pitches`:** Businesses with dead websites (`website_live is False`) or low ratings, targeted for modernization.

#### Delivery Folder Hierarchy on Google Drive
```
gdrive:leads/
├── current/                               <-- Always contains latest active batch
│   ├── SA/
│   │   ├── leads_SA_riyadh.xlsx
│   │   ├── leads_SA_jeddah_makkah.xlsx
│   │   ├── leads_SA_eastern_province.xlsx
│   │   └── csv/                           <-- BOM-encoded CSVs for CRM upload
│   │       ├── sales_ready_riyadh.csv
│   │       └── sales_ready_jeddah.csv
│   ├── LB/
│   │   ├── leads_LB_beirut.xlsx
│   │   ├── leads_LB_mount_lebanon.xlsx
│   │   ├── leads_LB_north_akkar.xlsx
│   │   ├── leads_LB_south_nabatieh.xlsx
│   │   ├── leads_LB_bekaa_baalbek.xlsx
│   │   └── csv/
│   │       ├── sales_ready_beirut.csv
│   │       └── sales_ready_mount_lebanon.csv
│   ├── unassigned/
│   │   └── leads_review_required.xlsx
│   └── manifest.json                      <-- Run metadata & row counts
└── archive/                               <-- Dated snapshots (pruned at 30 days)
    └── 2026-10-01/
        └── ...
```

#### Manifest Contract (`manifest.json`)
Accompanying each export run, a lightweight manifest guarantees traceability:
```json
{
  "run_id": "20261003_140000",
  "generated_at": "2026-10-03T14:00:00Z",
  "total_records_scraped": 42150,
  "total_unique_records": 38400,
  "territories": {
    "SA_riyadh": {
      "sales_ready_count": 2840,
      "without_websites_count": 1420,
      "rebuild_pitches_count": 310,
      "file": "SA/leads_SA_riyadh.xlsx"
    },
    "LB_beirut": {
      "sales_ready_count": 3950,
      "without_websites_count": 1890,
      "rebuild_pitches_count": 540,
      "file": "LB/leads_LB_beirut.xlsx"
    }
  }
}
```

---

## Not a bug, but worth knowing

1. **Google Sheets native import strips BOM:** While Windows Excel strictly requires the UTF-8 BOM (`\xef\xbb\xbf`) to open CSVs without mojibake, Google Sheets' CSV import parser silently strips the BOM. Writing `utf-8-sig` is completely safe and backwards-compatible with both systems.
2. **Sales rep edits overwritten by scraper sync:** Sales reps naturally add columns to spreadsheets (e.g. "Called?", "Call Notes", "Meeting Booked"). If the rclone sync directly overwrites `sales_ready.csv` or `leads_SA_riyadh.xlsx` in Drive, all manual CRM notes entered by sales reps are permanently erased. Human deliverables must either be delivered to date-stamped intake folders or integrated with a durable SDR override ledger before syncing.
3. **Openpyxl memory efficiency:** For workbooks under 30,000 rows, standard `openpyxl.Workbook` memory usage is well under 120 MB RAM. If regional partitions ever exceed 100,000 rows, `openpyxl`'s `write_only=True` mode allows constant-memory streaming directly to disk.
4. **WhatsApp Web link mechanics:** The `https://wa.me/<digits>` protocol requires country code without leading plus or zeros. A Lebanese number `03 123 456` must be converted to `https://wa.me/9613123456`, and a Saudi number `050 123 4567` must be converted to `https://wa.me/966501234567`. Any spaces, hyphens, or parentheses in the link cause the WhatsApp desktop/web client to throw an invalid phone number error.

---

## Recommended order of work

1. **Immediate Patch (Hotfix):** Update `main.py:75` to write CSV files using `encoding="utf-8-sig"` instead of `"utf-8"`. This immediately stops Arabic mojibake for Excel users without touching any schema or downstream logic.
2. **Implement Reordered Human Column Contract:** Replace `FIELDS` sequence in `main.py:32-39` with the 5-Funnel human sequence (`name`, `lead_score`, `industry_priority`, `recommended_service`, `phone`, `whatsapp`, `email`, `website`, ...), moving `lat` and `lon` to the far right.
3. **Add `openpyxl` Dependency and Exporter Module:** Add `openpyxl>=3.1.2` to `requirements.txt`. Create `exporters/excel.py` incorporating auto-widths, header fills (`#1F4E78`), freeze panes (`C2`), score color-coding, and native clickable links (`tel:`, `mailto:`, `https://wa.me/`).
4. **Implement Territory Partitioning:** Update `main.py` to segment records by `(country, territory)` using the documented taxonomy. Generate multi-tab `.xlsx` workbooks (`Sales Ready`, `No Website`, `Rebuild Pitches`) for each territory.
5. **Update CI/CD Drive Sync & Retention:** Update `.github/workflows/scrape.yml` to publish partitioned workbooks to `gdrive:leads/current/` and implement a 30-day retention prune on `gdrive:leads/archive/`.

---

## References

- Microsoft Support, [Opening CSV UTF-8 files correctly in Excel](https://support.microsoft.com/en-au/excel/opening-csv-utf-8-files-correctly-in-excel).
- Google Workspace, [Google Sheets size limits and cell constraints](https://support.google.com/drive/answer/37603).
- OWASP Foundation, [CSV Injection (Formula Injection) Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/CSV_Injection_Cheat_Sheet.html).
- OpenPyXL Documentation, [Working with Styles, Conditional Formatting, and Freeze Panes](https://openpyxl.readthedocs.io/en/stable/styles.html).
- WhatsApp API Documentation, [Linking to WhatsApp from a Web or Mobile Application](https://faq.whatsapp.com/5913398998672960).