"""
Pitch recommender for leadminer.

Given a business record, returns the recommended Voxire service to pitch
based on what's missing or underbuilt in their digital presence.

The output goes into the `recommended_service` column of sales_ready.csv.
Helpers use it as a starting hypothesis on the first outbound message.
It is not a final answer. The discovery call refines it.

Voxire services this maps to:
    - Web Development & Design
    - SEO (lead with regional SEO: Lebanon / KSA)
    - Digital Marketing (Google + Meta + content)
    - Branding & Visual Identity
    - SaaS / Web App Development
    - Mobile App Development
    - Tech Hiring
    - RTYLR (commerce OS, separate sales motion)
"""

from typing import Mapping


def recommend_service(record: Mapping) -> str:
    """
    Returns a single recommended pitch for the lead.
    The label is short and human - it goes into a CSV column helpers
    will scan in seconds.
    """
    has_website = bool(record.get("website"))
    website_live = record.get("website_live") is True
    has_dead_website = has_website and not website_live
    has_email = bool(record.get("email"))
    has_phone = bool(record.get("phone"))
    has_instagram = bool(record.get("instagram"))
    has_facebook = bool(record.get("facebook"))
    has_social = has_instagram or has_facebook
    rating = record.get("rating")
    review_count = record.get("review_count") or 0
    try:
        review_count = int(review_count) if review_count else 0
    except (ValueError, TypeError):
        review_count = 0
    completeness = record.get("completeness_score") or 0
    try:
        completeness = int(completeness) if completeness else 0
    except (ValueError, TypeError):
        completeness = 0
    category = (record.get("category") or "").strip().lower()

    # Tier 1: dead website is the strongest pitch in the database.
    # They already paid for a site once. Now it's broken. Sell the rebuild.
    if has_dead_website:
        return "Website rebuild + maintenance"

    # Tier 2: no website but real social presence.
    # They have an audience but no funnel. Sell the conversion engine.
    if not has_website and has_social and category in _ECOM_FRIENDLY:
        return "E-commerce launch + Instagram-to-store funnel"

    if not has_website and has_social:
        return "Website launch + capture their existing audience"

    # Tier 3: has website but no SEO signals (no real reviews, low ratings,
    # weak completeness). The site exists but it's not pulling weight.
    if has_website and website_live and review_count < 20:
        return "SEO audit + visibility upgrade"

    if has_website and website_live and rating and _is_low_rating(rating):
        return "Reputation + SEO turnaround"

    # Tier 4: high-volume hospitality / F&B with no proper digital infra.
    # These need RTYLR (POS + ERP + CRM + ordering) more than a website.
    if category in _RTYLR_TARGETS and (has_phone or has_social):
        return "RTYLR commerce OS (POS, online ordering, CRM)"

    # Tier 5: clinics, real estate, professional services.
    # Need lead-gen (Google Ads + landing pages + WhatsApp capture).
    if category in _LEAD_GEN_VERTICALS:
        if not has_website:
            return "Lead-gen website + Google Ads launch"
        return "Lead-gen overhaul (landing pages + Google Ads + WhatsApp capture)"

    # Tier 6: nothing at all - no website, no social, just a phone number.
    # Full digital launch package.
    if not has_website and not has_social:
        return "Full digital launch (brand + website + social setup)"

    # Tier 7: has everything basic, looks established.
    # Default to digital marketing to grow what's already there.
    if has_website and website_live and has_social:
        return "Digital marketing retainer (Meta + Google + content)"

    # Fallback
    return "Discovery call - scope the right service"


# F&B, fashion, beauty, retail - good fit for Instagram-first commerce
_ECOM_FRIENDLY = {
    "clothes", "boutique", "shoes", "jewelry", "jewellery", "fashion_accessories",
    "perfumery", "cosmetics", "beauty", "lingerie", "bag",
    "bakery", "patisserie", "confectionery", "chocolate", "deli",
    "pet", "florist", "gift", "art", "music",
    "ice_cream",
}

# Restaurants, cafes, hotels - core RTYLR market
_RTYLR_TARGETS = {
    "restaurant", "cafe", "fast_food", "bar", "pub", "bistro",
    "food_court", "ice_cream", "tea_house", "coffee_shop",
    "hotel", "guest_house", "resort", "boutique_hotel",
    "supermarket", "convenience", "grocery",
}

# Industries where Google/Meta paid lead-gen is the dominant growth lever
_LEAD_GEN_VERTICALS = {
    "clinic", "doctors", "dentist", "veterinary", "physiotherapist",
    "optician", "cosmetic_surgery", "dermatology", "fertility_clinic",
    "medical_clinic", "aesthetic_clinic", "spa", "wellness",
    "real_estate_agent", "real_estate", "estate_agent", "property",
    "lawyer", "law_firm", "attorney", "notary",
    "accountant", "accounting", "tax_advisor", "consulting",
    "architect", "engineering_firm",
    "driving_school", "language_school", "training_centre",
    "fitness_centre", "fitness", "gym",
    "car_dealership", "insurance",
    "travel_agency", "events_venue", "wedding_venue", "photography_studio",
}


def _is_low_rating(rating) -> bool:
    """True if the Google rating signals reputation problems."""
    try:
        return float(rating) < 4.0
    except (ValueError, TypeError):
        return False
