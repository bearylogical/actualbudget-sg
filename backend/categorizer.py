"""
Auto-categorization engine for bank transactions.
Uses keyword matching + pattern rules with confidence scoring.
"""

import re

# (pattern, category, confidence)
RULES: list[tuple] = [
    # Food & Dining
    (r"mcdonald|mcdonalds|mcd|kfc|burger|subway|popeyes|pizza|domino|jollibee", "Food & Dining", 0.95),
    (r"old chang kee|bengawan|kopitiam|foodpanda|grabfood|deliveroo", "Food & Dining", 0.95),
    (r"restaurant|dining|diner|bistro|cafe|caf[eé]|coffee|starbucks|toast|hawker", "Food & Dining", 0.90),
    (r"mother dough|joji|astons|youngs bar|blu jaz|bytes caf|wok peo", "Food & Dining", 0.90),
    (r"bar |grill|eatery|kitchen|bakery|dessert|bubble tea|boba", "Food & Dining", 0.85),
    (r"ijooz|vending|atlasvending", "Food & Dining", 0.80),
    (r"cold storage|sheng siong|fairprice|giant|ntuc|supermarket|market", "Groceries", 0.92),
    (r"greendot|green dot", "Groceries", 0.85),

    # Transport
    (r"bus/mrt|nets flashpay|ez-link|transitlink|mrt|smrt|sbs transit", "Transport", 0.97),
    (r"grab\*|gojek|comfort|taxi|cabcharge|tada", "Transport", 0.95),
    (r"spc |shell |caltex |petrol |fuel |esso |sinopec", "Transport", 0.92),
    (r"lta e-service|lta |road tax|parking|coupon", "Transport", 0.90),
    (r"uber|lyft|ryde", "Transport", 0.92),

    # Shopping
    (r"uniqlo|zara|h&m|gap|cotton on|marks & spencer|primark", "Shopping", 0.95),
    (r"puma|nike|adidas|reebok|new balance|converse|vans", "Shopping", 0.92),
    (r"shopee|lazada|qoo10|amazon|taobao|aliexpress", "Shopping", 0.90),
    (r"muji|ikea|courts|harvey norman|best denki|challenger", "Shopping", 0.90),
    (r"apple store|xiaomi|samsung|microsoft store|sony|dyson", "Electronics", 0.93),
    (r"british essential|times bookstore|kinokuniya|popular|book", "Shopping", 0.85),
    (r"wine connection|dan murphy|bottle|liquor", "Shopping", 0.88),

    # Bills & Utilities
    (r"gomo|singtel|starhub|m1 |circles|redone|telco|mobile plan", "Bills & Utilities", 0.95),
    (r"sp services|sp group|electricity|water board|utilities|power|gas", "Bills & Utilities", 0.95),
    (r"amazon web services|aws|google cloud|azure|digitalocean|netlify|vercel", "Software & Cloud", 0.95),
    (r"netflix|spotify|youtube|disney\+|apple tv|prime video|hbo|deezer", "Subscriptions", 0.97),
    (r"the economist|wsj|nyt|new york times|subscription|membership", "Subscriptions", 0.90),
    (r"wormhole|sp wormhole", "Software & Cloud", 0.85),

    # Health & Fitness
    (r"tuff club|fitness|gym|yoga|pilates|anytime fitness|pure fitness|goodlife", "Health & Fitness", 0.95),
    (r"guardian|watsons|unity pharmacy|pharmacy|clinic|hospital|dental|doctor|medical", "Health & Medical", 0.95),
    (r"scaled|crossfit|bjj|martial arts|swim|sport", "Health & Fitness", 0.88),

    # Entertainment & Events
    (r"sistic|ticketmaster|ticket|cinema|cathay|shaw|gv |golden village|concert", "Entertainment", 0.93),
    (r"steam |playstation|xbox|nintendo|game|esports", "Entertainment", 0.90),

    # Travel
    (r"changi airport|airport|airlines|airasia|scoot|jetstar|sia |singapore airlines|klook|airbnb|booking\.com|expedia|agoda|hotel", "Travel", 0.93),
    (r"icondo", "Travel", 0.75),

    # Financial & Payments
    (r"paymt thru|payment|bill payment|transfer|topup|top.up|nets flashpay topup", "Payment / Transfer", 0.98),
    (r"insurance|prudential|great eastern|aia |ntuc income|income insurance", "Insurance", 0.95),
    (r"applied materials|work|office|corporate", "Work / Corporate", 0.70),

    # Giving
    (r"giving\.sg|sg gives|donation|charity|nkf|scs|ren ci|touch|spca|wwf", "Donations", 0.97),

    # Education
    (r"coursera|udemy|skillsfuture|smu|nus|ntu|sit |sutd|school|tuition|education", "Education", 0.92),

    # Personal Care
    (r"salon|haircut|hair |spa |massage|nail |beauty|wax|facial", "Personal Care", 0.90),
]


def categorize_transaction(description: str) -> tuple[str, float]:
    """Match description against rule patterns. Returns (category, confidence)."""
    desc_lower = description.lower()
    best_category = "Uncategorized"
    best_confidence = 0.0
    for pattern, category, confidence in RULES:
        if re.search(pattern, desc_lower):
            if confidence > best_confidence:
                best_confidence = confidence
                best_category = category
    return best_category, best_confidence
