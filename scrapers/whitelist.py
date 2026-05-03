"""
Category whitelist for leadminer.

Drops geographic noise (mountains, villages, watercourses) and irrelevant
records leaking from OSM and Wikidata. Keeps Voxire's priority industries
plus adjacent business categories.

Usage:
    from .whitelist import is_business_category

    if not is_business_category(category):
        continue
    yield record
"""

# Tier 1: Voxire priority industries (per Sales Playbook Section 01)
PRIORITY_INDUSTRIES = {
    # Restaurants and hospitality
    "restaurant", "cafe", "fast_food", "bar", "pub", "biergarten", "food_court",
    "ice_cream", "bistro", "patisserie", "tea_house", "coffee_shop",
    # Hotels and tourism
    "hotel", "guest_house", "hostel", "motel", "resort", "boutique_hotel",
    "bed_and_breakfast", "tourism", "tour_operator",
    # Healthcare clinics (high-margin)
    "clinic", "clinics", "doctors", "dentist", "dental_clinic", "veterinary",
    "vet", "physiotherapist", "physio", "optician", "optometrist",
    "cosmetic_surgery", "cosmetic_clinic", "dermatology", "dermatologist",
    "fertility_clinic", "medical_clinic", "aesthetic_clinic",
    "spa", "wellness", "medical_centre", "medical_center",
    # Real estate
    "real_estate_agent", "real_estate", "estate_agent", "property",
    # Professional services (high LTV)
    "lawyer", "law_firm", "attorney", "notary", "accountant", "accounting",
    "tax_advisor", "consulting", "marketing_agency", "advertising_agency",
    "design_studio", "architect", "engineering_firm",
    # Fashion and e-commerce
    "clothes", "boutique", "shoes", "jewelry", "jewellery", "bag",
    "watches", "fashion_accessories", "perfumery", "cosmetics",
    "beauty", "lingerie",
    # Tech and SaaS
    "software", "internet_cafe", "computer", "telecommunications",
}

# Tier 2: Adjacent business categories (real businesses, lower priority)
ADJACENT_BUSINESSES = {
    # Retail
    "supermarket", "convenience", "grocery", "bakery", "butcher",
    "deli", "wine", "alcohol", "tobacco", "newsagent", "kiosk",
    "department_store", "mall", "shopping_centre", "marketplace",
    "variety_store", "general", "trade",
    # Personal care and services
    "hairdresser", "barber", "nail_salon", "massage", "tattoo",
    "tanning", "laundry", "dry_cleaning", "tailor", "cobbler",
    # Health and wellness adjacent
    "pharmacy", "chemist", "hospital", "medical_supply",
    "fitness_centre", "fitness", "gym", "yoga", "pilates",
    "sports_centre", "swimming_pool",
    # Education
    "school", "kindergarten", "language_school", "music_school",
    "driving_school", "training_centre", "tutoring", "university",
    "college", "academy",
    # Automotive
    "car", "car_dealership", "car_repair", "car_wash", "car_rental",
    "motorcycle", "tyres", "parts",
    # Home and lifestyle
    "furniture", "interior_decoration", "florist", "garden_centre",
    "hardware", "doityourself", "paint", "lighting", "carpet",
    "curtain", "kitchen", "bathroom_furnishing", "appliance",
    # Specialty retail
    "books", "art", "music", "musical_instrument", "stationery",
    "gift", "toys", "pet", "pet_grooming", "photo", "video", "electronics",
    "mobile_phone", "computer_repair",
    # Food production
    "confectionery", "dairy", "seafood", "spices", "coffee",
    "tea", "chocolate",
    # Financial and admin
    "bank", "atm", "money_lender", "money_transfer", "insurance",
    "currency_exchange", "post_office", "courier", "logistics",
    # Other
    "travel_agency", "events_venue", "wedding_venue", "photography_studio",
    "printing", "copy_shop", "advertising",
}

# Hard-block list: clearly not businesses, drop on sight
GEOGRAPHIC_NOISE = {
    "village/town/city in Lebanon", "village", "town", "city", "hamlet",
    "suburb", "neighbourhood", "quarter", "borough", "municipality",
    "human settlement", "place", "locality",
    "mountain", "peak", "hill", "ridge", "valley", "plateau",
    "watercourse", "river", "stream", "lake", "spring", "waterfall",
    "wadi", "bay", "cape", "island", "beach", "coast",
    "place_of_worship", "church", "mosque", "temple", "shrine",
    "cemetery", "grave_yard", "monastery", "synagogue",
    "monument", "memorial", "archaeological_site", "ruins", "castle",
    "fort", "tower", "obelisk", "statue",
    "park", "garden", "playground", "nature_reserve", "protected_area",
    "forest", "wood", "grassland", "meadow", "wetland",
    "highway", "road", "path", "track", "junction", "roundabout",
    "bus_stop", "parking", "fuel_dispenser",
    "yes",  # OSM tag noise from amenity=yes records
    "metaorganization", "organization",  # too vague
}


def is_business_category(category: str | None) -> bool:
    """
    True if the category is a real business worth keeping.
    False if it's geographic noise or too vague to act on.
    """
    if not category:
        return False
    cat = category.strip().lower()

    # Hard-block geographic and infrastructure noise
    if cat in GEOGRAPHIC_NOISE:
        return False

    # Check priority and adjacent lists
    if cat in PRIORITY_INDUSTRIES or cat in ADJACENT_BUSINESSES:
        return True

    # Soft-allow if the category contains a business-y substring
    business_keywords = (
        "shop", "store", "agency", "firm", "company", "service",
        "studio", "office", "salon", "centre", "center", "boutique",
    )
    if any(kw in cat for kw in business_keywords):
        return True

    return False


def industry_priority(category: str | None) -> str:
    """
    Returns 'high', 'medium', or 'low' priority for a category.
    Used in the sales_ready CSV to let helpers filter.
    """
    if not category:
        return "low"
    cat = category.strip().lower()
    if cat in PRIORITY_INDUSTRIES:
        return "high"
    if cat in ADJACENT_BUSINESSES:
        return "medium"
    return "low"
