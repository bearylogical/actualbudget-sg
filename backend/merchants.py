"""
Seed merchant rules for Singapore statements.

Each rule: Merchant(pattern, payee, category, confidence, kind, sign)

  pattern     Python regex, matched case-insensitively against the RAW statement
              description. ALWAYS use \\b word boundaries — the old rules matched
              `gap` inside "SINGAPORE" and `nus` inside "BONUS".
              Patterns are also pushed into Actual as `imported_payee matches`
              rules, so keep them JS-regex compatible (no inline flags, no
              possessive quantifiers, no lookbehind).
  payee       clean payee name to use in Actual (None = use the normaliser)
  category    canonical category from taxonomy.py (None for transfers / p2p)
  confidence  0-1, display only
  kind        expense | income | transfer | p2p
                transfer = money moving between your own accounts (card bill
                           payment, Wise/YouTrip top-up, brokerage, CPF/SRS)
                p2p      = PayNow/FAST to a person — can't be auto-categorised,
                           flagged for review instead of guessed
  sign        None | "debit" | "credit" — rule only applies to that direction

ORDER MATTERS: the first matching rule wins. Put specific rules before generic
ones (GrabFood before Grab, Apple.com/bill before Apple Store, merchant rules
before the generic GIRO / transfer catch-alls at the bottom).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Merchant:
    pattern: str
    payee: str | None
    category: str | None
    confidence: float = 0.9
    kind: str = "expense"
    sign: str | None = None
    regex: re.Pattern = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        object.__setattr__(self, "regex", re.compile(self.pattern, re.IGNORECASE))

    def applies(self, description: str, is_credit: bool | None = None) -> bool:
        if self.sign == "debit" and is_credit:
            return False
        if self.sign == "credit" and is_credit is False:
            return False
        return bool(self.regex.search(description))


M = Merchant
MERCHANTS: list[Merchant] = [
    # ── Own-account transfers (must beat everything) ──────────────────────────
    M(r"\bpaymt thru\b|\bpayment thru\b|\bpayment - thank you\b|\bpayment received\b|\bcard payment\b",
      "Card Payment", None, 0.98, "transfer"),
    M(r"\btransferwise\b|\bwise\b(?! ?(men|owl))", "Wise", None, 0.9, "transfer"),
    M(r"\byoutrip\b", "YouTrip", None, 0.9, "transfer"),
    M(r"\brevolut\b", "Revolut", None, 0.9, "transfer"),
    M(r"\bsyfe\b|\bendowus\b|\bstashaway\b|\btiger brokers\b|\bmoomoo\b|\binteractive brokers\b|\bibkr\b|\bsaxo\b|\bpoems\b|\bfsmone\b",
      None, None, 0.9, "transfer"),
    M(r"\bcpf\b", "CPF", None, 0.85, "transfer"),
    M(r"\bsrs\b", "SRS", None, 0.85, "transfer"),
    M(r"^cdp\b|\bcdp-|\bsavings bonds?\b", "CDP", None, 0.85, "transfer"),
    M(r"\b(uob|dbs|posb|ocbc):\s?\d+:\s?i-?bank\b", "Own Account Transfer", None, 0.9, "transfer"),
    M(r"\bcash withdrawal\b|\batm\b", "Cash", None, 0.85, "transfer"),

    # ── Income (credits only) ─────────────────────────────────────────────────
    M(r"\bsalary\b|\bsal\b|\bpayroll\b", "Salary", "Salary", 0.95, "income", "credit"),
    M(r"\binterest (credit|earned|paid)\b|\bbonus interest\b|\bint(erest)? cr\b|\bdividends?\b",
      "Interest", "Interest & Dividends", 0.95, "income", "credit"),
    M(r"\bmindef\b|\bsaf imprest\b|\bgov\b.*\bgov\b|\bgovernment\b|\bcdc vouchers?\b|\bgst voucher\b",
      None, "Other Income", 0.85, "income", "credit"),
    M(r"\bmoneysmart\b|\bsingsaver\b|\bcash ?back\b|\brebate\b|\breward(s)? redemption\b|\bstatement credit\b",
      "Cashback", "Cashback & Rebates", 0.9, "income", "credit"),

    # ── Bank fees & taxes (debits only) ───────────────────────────────────────
    M(r"\bannual fee\b|\blate (payment )?(fee|charge)\b|\bfinance charge\b|\binterest charge\b|\bservice charge\b|\bfx fee\b|\bforeign transaction fee\b|\bcash advance fee\b|\bfall-below fee\b|\bgst on fee\b|\bservice charge\b|\bsvc chg\b",
      "Bank Fees", "Bank Fees & Charges", 0.95, "expense", "debit"),
    M(r"\biras\b|\bincome tax\b|\bproperty tax\b", "IRAS", "Taxes", 0.95),
    M(r"\btown council\b|\bconservancy\b", "Town Council", "Town Council & Conservancy", 0.95),

    # ── Transport ─────────────────────────────────────────────────────────────
    M(r"\bgrab ?pay\b.*\btop[ -]?up\b|\btop[ -]?up\b.*\bgrab ?pay\b", "GrabPay", None, 0.9, "transfer"),
    M(r"\bgrab ?food\b|\bgrab\*? ?food\b", "GrabFood", "Food Delivery", 0.95),
    M(r"\bgrab ?mart\b", "GrabMart", "Groceries", 0.9),
    M(r"\bgrab(?! ?(food|mart|pay))\b|\bgrab\*(?! ?(food|mart))", "Grab", "Ride-hailing & Taxi", 0.8),
    M(r"\bgojek\b", "Gojek", "Ride-hailing & Taxi", 0.95),
    M(r"\bcomfortdelgro\b|\bcdg (taxi|zig)\b|\bcomfort taxi\b|\btada\b|\bryde\b|\bzig\b|\bstrides taxi\b|\btaxi\b|\bcabcharge\b|\buber\b",
      None, "Ride-hailing & Taxi", 0.9),
    M(r"\bbus/mrt\b|\btransitlink\b|\bsimplygo\b|\bez-?link\b|\bsmrt\b|\bsbs transit\b|\bspl auto ?top ?up\b",
      "SimplyGo", "Public Transport", 0.97),
    M(r"\bnets flashpay\b|\bnets auto top ?up\b", "NETS FlashPay", "Public Transport", 0.85),
    M(r"\banywheel\b|\bhelloride\b|\bsg bike\b", None, "Public Transport", 0.9),
    M(r"\bhdb (car ?park|parking)\b|\bparking\.sg\b|\bcar ?park\b|\bparking\b|\berp\b|\blta\b|\broad tax\b",
      None, "Car & Parking", 0.9),
    M(r"\bspc\b|\bshell\b|\bcaltex\b|\besso\b|\bsinopec\b|\bpetrol\b", None, "Car & Parking", 0.9),

    # ── Food ──────────────────────────────────────────────────────────────────
    M(r"\bfoodpanda\b", "foodpanda", "Food Delivery", 0.97),
    M(r"\bdeliveroo\b", "Deliveroo", "Food Delivery", 0.97),
    M(r"\bfairprice group hawker\b|\bhawker ce", None, "Dining & Hawker", 0.9),
    M(r"\b(ntuc )?fairprice\b|\bntuc fp\b|\bfp xtra\b|\bfinest\b", "FairPrice", "Groceries", 0.95),
    M(r"\b7-?eleven\b|\bcheers\b", None, "Groceries", 0.85),
    M(r"\bsheng siong\b", "Sheng Siong", "Groceries", 0.97),
    M(r"\bcold storage\b|\bcs fresh\b", "Cold Storage", "Groceries", 0.95),
    M(r"\bgiant\b", "Giant", "Groceries", 0.9),
    M(r"\bdon don donki\b|\bdonki\b", "Don Don Donki", "Groceries", 0.9),
    M(r"\bprime supermarket\b|\bu stars\b|\bredmart\b|\bsupermarket\b|\bmini ?mart\b|\bgreen ?dot\b",
      None, "Groceries", 0.88),
    M(r"\bstarbucks\b", "Starbucks", "Coffee & Drinks", 0.95),
    M(r"\btoast box\b|\bya kun\b|\bkilliney|\bkopi\b|brewkopi|\billy\b|\bboost juice\b|\bsmoothies?\b|\bvending\b|\bvendcafe\b|\bcoffee bean\b|\bluckin\b|\bflash coffee\b|\bkoi\b|\bliho\b|\bchagee\b|\bmixue\b|\bgong ?cha\b|\bheytea\b|\bpresotea\b|\bbubble tea\b|\bijooz\b|\bcoffee\b|\bcaf[eé]\b",
      None, "Coffee & Drinks", 0.88),
    M(r"\bmcdonald'?s?\b|\bmcd\b|\bkfc\b|\bburger king\b|\bsubway\b|\bpopeyes\b|\bjollibee\b|\bmos burger\b|\blong john\b|\bpizza hut\b|\bdomino'?s\b|\bfive guys\b|\bshake shack\b",
      None, "Dining & Hawker", 0.95),
    M(r"\bkopitiam\b|\bfood ?court\b|\bfoodfare\b|\bkoufu\b|\bfood republic\b|\bhawker\b|\bold chang kee\b|\bbengawan\b|\bbreadtalk\b|\bfour leaves\b|\bpolar puffs\b",
      None, "Dining & Hawker", 0.93),
    M(r"\bmother dough\b|\bjoji\b|\bastons\b|\byoungs bar\b|\bblu jaz\b|bytes ?caf|\bwok pe|\bencik tan\b|\bapplied materials - soc\b|\btwp - |\bfreebird\b|\bstuff'?d\b|\b4 fingers\b|\bkrispy kreme\b|\bsukiya\b|\bbaker (&|and) cook\b|\bhotbake\b|\bchateraise\b",
      None, "Dining & Hawker", 0.9),
    M(r"^ts/|\brestaurant\b|\bbistro\b|\beatery\b|\bpizza\b|\bpancakes?\b|\bnoodles?\b|\broti\b|\byakitori\b|\bhawker\b|\bkitchen\b|\bgrill\b|\bdiner\b|\bdining\b|\bbakery\b|\bramen\b|\bsushi\b|\bdim sum\b|\bbbq\b|\bsteakhouse\b|\bbrasserie\b|\btrattoria\b|\bizakaya\b|\bdessert\b",
      None, "Dining & Hawker", 0.85),

    # ── Subscriptions / cloud (before Apple Store, Amazon) ────────────────────
    M(r"\bapple\.com/bill\b|\bitunes\b|\bicloud\b", "Apple", "Subscriptions", 0.9),
    M(r"\bamazon web services\b|\baws\b", "AWS", "Software & Cloud", 0.97),
    M(r"\bgoogle cloud\b|\bgcp\b|\bdigitalocean\b|\bhetzner\b|\bcloudflare\b|\bnetlify\b|\bovh(cloud)?\b|\bvercel\b|\bgithub\b|\bopenai\b|\banthropic\b|\bclaude\.ai\b|\bnamecheap\b|\bporkbun\b|\blinode\b|\btailscale\b|\b1password\b|\bbackblaze\b|\bjetbrains\b",
      None, "Software & Cloud", 0.95),
    M(r"\bnetflix\b", "Netflix", "Subscriptions", 0.97),
    M(r"\bspotify\b", "Spotify", "Subscriptions", 0.97),
    M(r"youtube|\bgoogle one\b|\bgoogle storage\b", "YouTube", "Subscriptions", 0.95),
    M(r"\bdisney ?(\+|plus)\b|\bprime video\b|\bhbo\b|\bviu\b|\bpatreon\b|\bchatgpt\b|\bthe economist\b|\bwsj\b|\bstraits times\b|\bsph media\b|\bnytimes\b|\bnew york times\b|\bsubstack\b|\bmedium\.com\b",
      None, "Subscriptions", 0.95),

    # ── Housing / bills ───────────────────────────────────────────────────────
    M(r"\bsp (services|group|digital|utilities)\b|\bsp services\b", "SP Group", "Utilities", 0.97),
    M(r"\bcity energy\b|\bgeneco\b|\bsenoko\b|\btuas power\b|\bkeppel electric\b|\bpacific light\b|\bsembcorp power\b",
      None, "Utilities", 0.95),
    M(r"\bsingtel\b|\bstarhub\b|\bm1\b|\bcircles\.?life\b|\bgomo\b|\bsimba\b|\bmyrepublic\b|\bviewqwest\b|\bgiga\b|\bzero1\b|\bredone\b",
      None, "Telco & Internet", 0.95),
    M(r"\bhdb\b", "HDB", "Mortgage & Rent", 0.8),
    M(r"\bikea\b|\bcourts\b|\bdaiso\b|\bmr ?diy\b|\bhoma\b|\bspotlight\b|\bfurniture\b", None, "Home & Furnishing", 0.9),

    # ── Shopping ──────────────────────────────────────────────────────────────
    M(r"\bshopee\b", "Shopee", "Shopping", 0.9),
    M(r"\blazada\b", "Lazada", "Shopping", 0.9),
    M(r"\bamazon\b|\bamzn\b", "Amazon", "Shopping", 0.88),
    M(r"\bqoo10\b|\btaobao\b|\baliexpress\b|\btemu\b|\bshein\b|\bmuji\b|\bkinokuniya\b|\btimes bookstore\b|\bpopular book\b|\bwine connection\b|\bliquor\b",
      None, "Shopping", 0.88),
    M(r"\buniqlo\b|\bzara\b|\bh ?& ?m\b|\bcotton on\b|\bmarks (&|and) spencer\b|\bm ?& ?s\b|\beverlane\b|\blove bonito\b|\bcharles (&|and) keith\b|\bpedro\b|\bnike\b|\badidas\b|\bpuma\b|\bdecathlon\b|\bnew balance\b|\bconverse\b|\basics\b|\bskechers\b|\bfoot locker\b",
      None, "Clothing & Shoes", 0.92),
    M(r"\bapple store\b|\bapple r\d+\b|\bchallenger\b|\bbest denki\b|\bgain city\b|\bharvey norman\b|\bsamsung\b|\bxiaomi\b|\bdyson\b|\bsony\b|\bcanon\b|\blogitech\b",
      None, "Electronics", 0.92),

    # ── Insurance ─────────────────────────────────────────────────────────────
    M(r"\bprudential\b|\bgreat eastern\b|\baia\b|\bincome insurance\b|\bntuc income\b|\bfwd\b|\bsinglife\b|\bmanulife\b|\btokio marine\b|\bhsbc life\b|\baxa\b|\betiqa\b|\bmsig\b|\bchubb\b|\bdirect asia\b|\binsurance\b",
      None, "Insurance", 0.95),

    # ── Health & wellness ─────────────────────────────────────────────────────
    M(r"\bguardian\b|\bwatsons\b|\biherb\b|\bunity pharmacy\b|\bpharmacy\b|\bpolyclinic\b|\bclinic\b|\bhospital\b|\bdental\b|\bmedical\b|\braffles medical\b|\bparkway\b|\bgleneagles\b|\bmount elizabeth\b",
      None, "Medical & Pharmacy", 0.93),
    M(r"\bgym\b|\bfitness\b|\byoga\b|\bpilates\b|\bactivesg\b|\bufit\b|\bcrossfit\b|\bbjj\b|\bmartial arts\b|\bclimb|\bboulder|\btuff club\b|\bscaled\b|\bclasspass\b|\bswim|\bbody ?fit\b|\bezypay",
      None, "Fitness", 0.92),
    M(r"\bsalon\b|\bbarber\b|\bhaircut\b|\bhair\b|\bspa\b|\bmassage\b|\bnails?\b|\bbeauty\b|\bfacial\b|\bwax\b|\bsephora\b",
      None, "Personal Care", 0.88),

    # ── Lifestyle ─────────────────────────────────────────────────────────────
    M(r"\bsistic\b|\bticketmaster\b|\bticketek\b|\bcinema\b|\bgolden village\b|\bgv\b|\bshaw theatres?\b|\bcathay cineplex\b|\bconcert\b|\bsteam\b|\bsteampowered\b|\bplaystation\b|\bxbox\b|\bnintendo\b|\bepic games\b|\buniversal studios\b|\bmandai\b|\bescape room\b|\bbookmyshow\b|\bpeatix\b|\bnational gallery\b|\bmuseum\b",
      None, "Entertainment", 0.92),
    M(r"\bsingapore airlines\b|\bsia\b|\bscoot\b|\bjetstar\b|\bairasia\b|\bcathay pacific\b|\bemirates\b|\bqatar airways\b|\bairlines?\b|\bairways\b|\bagoda\b|\bbooking\.com\b|\bexpedia\b|\btrip\.com\b|\bairbnb\b|\bklook\b|\bkkday\b|\bhotel\b|\bhostel\b|\bresort\b|\bchangi airport\b|\bicondo\b",
      None, "Travel", 0.92),
    M(r"\bcoursera\b|\budemy\b|\bskillsfuture\b|\bnus\b|\bntu\b|\bsmu\b|\bsutd\b|\bsuss\b|\bschool\b|\btuition\b|\bo'?reilly\b|\bdatacamp\b|\bedx\b|\bkaplan\b",
      None, "Education", 0.88),
    M(r"\bgiving\.sg\b|\bdonation\b|\bcharity\b|\bnkf\b|\bsingapore cancer society\b|\bren ci\b|\bspca\b|\bwwf\b|\bzakat\b",
      None, "Gifts & Donations", 0.95),
    M(r"\bapplied materials\b|\bconcur\b|\bexpense claim\b", None, "Claimable Expenses", 0.7),

    # ── Generic catch-alls (LAST) ─────────────────────────────────────────────
    M(r"\bpaynow\b|\bfast (payment|transfer)\b|\bfunds? transfer\b|\bfund trf\b|\bibg\b|\bi-?bank(ing)? (trf|transfer)\b|\btransfer\b|\btrf\b",
      None, None, 0.6, "p2p"),
    M(r"\bgiro\b", None, None, 0.5, "p2p"),
]


def match_merchant(description: str, is_credit: bool | None = None) -> Merchant | None:
    for m in MERCHANTS:
        if m.applies(description, is_credit):
            return m
    return None
