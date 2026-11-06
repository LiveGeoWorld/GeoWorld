"""Shared constants for pipeline_v2: type mappings, colors, labels."""

# ============================================================
# Visual colors — simplified: one color per entity kind
# ============================================================

PLACE_COLOR      = "#3388ff"   # blue   all places/buildings
ACTOR_COLOR      = "#e74c3c"   # red    all virtual characters
DISTRICT_COLOR   = "#666666"   # grey   district centers
CORRIDOR_COLOR   = "#999999"   # grey   transit corridors

# ============================================================
# primary_type → 8 visual categories  (107 → 8)
# Used by filter dropdowns, NOT for coloring.
# ============================================================

TYPE_TO_CATEGORY: dict[str, str] = {
    # ── restaurant ──
    "restaurant":                      "restaurant",
    "japanese_restaurant":             "restaurant",
    "ramen_restaurant":                "restaurant",
    "sushi_restaurant":                "restaurant",
    "tonkatsu_restaurant":             "restaurant",
    "japanese_izakaya_restaurant":     "restaurant",
    "fast_food_restaurant":            "restaurant",
    "hamburger_restaurant":            "restaurant",
    "chicken_restaurant":              "restaurant",
    "steak_house":                     "restaurant",
    "italian_restaurant":              "restaurant",
    "chinese_restaurant":              "restaurant",
    "indian_restaurant":               "restaurant",
    "thai_restaurant":                 "restaurant",
    "spanish_restaurant":              "restaurant",
    "brazilian_restaurant":            "restaurant",
    "australian_restaurant":           "restaurant",
    "american_restaurant":             "restaurant",
    "western_restaurant":              "restaurant",
    "halal_restaurant":                "restaurant",
    "vegan_restaurant":                "restaurant",
    "hot_pot_restaurant":              "restaurant",
    "dumpling_restaurant":             "restaurant",
    "yakiniku_restaurant":             "restaurant",
    "yakitori_restaurant":             "restaurant",
    "japanese_curry_restaurant":       "restaurant",
    "bistro":                          "restaurant",
    "gastropub":                       "restaurant",
    "food_court":                      "restaurant",
    "meal_takeaway":                   "restaurant",
    "cafeteria":                       "restaurant",
    # ── cafe / bar ──
    "cafe":                            "cafe",
    "coffee_shop":                     "cafe",
    "bar":                             "cafe",
    "pub":                             "cafe",
    "brewpub":                         "cafe",
    "bakery":                          "cafe",
    "cat_cafe":                        "cafe",
    "internet_cafe":                   "cafe",
    "dessert_restaurant":              "cafe",
    "dessert_shop":                    "cafe",
    "confectionery":                   "cafe",
    "deli":                            "cafe",
    # ── hotel ──
    "hotel":                           "hotel",
    "lodging":                         "hotel",
    "hostel":                          "hotel",
    "resort_hotel":                    "hotel",
    # ── retail ──
    "shopping_mall":                   "retail",
    "department_store":                "retail",
    "store":                           "retail",
    "book_store":                      "retail",
    "electronics_store":               "retail",
    "clothing_store":                  "retail",
    "womens_clothing_store":           "retail",
    "sportswear_store":                "retail",
    "gift_shop":                       "retail",
    "toy_store":                       "retail",
    "discount_store":                  "retail",
    "general_store":                   "retail",
    "grocery_store":                   "retail",
    "supermarket":                     "retail",
    "market":                          "retail",
    "manufacturer":                    "retail",
    "service":                         "retail",
    # ── attraction ──
    "tourist_attraction":              "attraction",
    "museum":                          "attraction",
    "art_museum":                      "attraction",
    "history_museum":                  "attraction",
    "movie_theater":                   "attraction",
    "observation_deck":                "attraction",
    "amusement_center":                "attraction",
    "amusement_park":                  "attraction",
    "art_gallery":                     "attraction",
    "shinto_shrine":                   "attraction",
    "buddhist_temple":                 "attraction",
    "castle":                          "attraction",
    "sculpture":                       "attraction",
    "event_venue":                     "attraction",
    "banquet_hall":                    "attraction",
    "karaoke":                         "attraction",
    "public_bath":                     "attraction",
    "stadium":                         "attraction",
    "sports_complex":                  "attraction",
    # ── outdoor ──
    "park":                            "outdoor",
    "garden":                          "outdoor",
    "national_park":                   "outdoor",
    "barbecue_area":                   "outdoor",
    # ── transit ──
    "train_station":                   "transit",
    "subway_station":                  "transit",
    "transit_station":                 "transit",
    "bus_station":                     "transit",
    "train_ticket_office":             "transit",
    "taxi_stand":                      "transit",
    "taxi_service":                    "transit",
    "transportation_service":          "transit",
    "travel_agency":                   "transit",
    # ── healthcare ──
    "hospital":                        "healthcare",
    "general_hospital":                "healthcare",
    "doctor":                          "healthcare",
    "medical_clinic":                  "healthcare",
    "medical_center":                  "healthcare",
    "health":                          "healthcare",
    "university":                      "education",
    "educational_institution":         "education",
    # ── other (fallback) ──
    "educational_institution":         "attraction",
    "business_center":                 "retail",
    "association_or_organization":     "retail",
    "pizza_restaurant":                "restaurant",
}

CATEGORY_LABELS_ZH: dict[str, str] = {
    "restaurant":  "餐厅",
    "cafe":        "咖啡饮品",
    "hotel":       "酒店住宿",
    "retail":      "零售购物",
    "attraction":  "景点场馆",
    "outdoor":     "户外公园",
    "transit":     "交通站点",
    "healthcare":  "医疗",
    "education":   "教育",
}

CATEGORY_LABELS_EN: dict[str, str] = {
    "restaurant":  "Restaurant",
    "cafe":        "Cafe & Bar",
    "hotel":       "Hotel",
    "retail":      "Retail",
    "attraction":  "Attraction",
    "outdoor":     "Outdoor",
    "transit":     "Transit",
    "healthcare":  "Healthcare",
    "education":   "Education",
}

# ============================================================
# Place type aliases (Google Places → normalized)
# ============================================================

PLACE_TYPE_ALIASES: dict[str, str] = {
    "clinic":       "medical_clinic",
    "clinics":      "medical_clinic",
    "medical":      "medical_clinic",
    "retail":       "store",
    "retail_pickup":"store",
    "pickup":       "store",
    "shopping":     "shopping_mall",
    "mall":         "shopping_mall",
    "coffee":       "coffee_shop",
    "station":      "train_station",
    "rail_station": "train_station",
    "subway":       "subway_station",
    "tourism":      "tourist_attraction",
    "attraction":   "tourist_attraction",
    "theater":      "movie_theater",
    "nightlife":    "bar",
}
