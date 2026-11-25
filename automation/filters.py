"""The auto-apply rules, in one place.

Every rule is one function: it takes a `Candidate` and returns a short human
reason to SKIP, or None to let it through. `evaluate()` runs them in order and
the first skip wins, so every skipped job has exactly one explainable reason
(and one category, which the per-run summary counts).

Nothing here touches the database, Flask, the network or the clock (time is
passed in), so the whole module is plain-Python testable. Sources are turned
into `Candidate`s by automation/candidates.py.

Decisions that shaped these rules (2026-11-25):
- 24-hour recency (was 72h until 2026-11-25); JobRight labels of a day or more ("1 day ago", "3 days ago") and "Reposted ..." labels can't be
  verified, so they are skipped. Exact timestamps (Tsenta, Simplify) are used
  when present.
- The same role in different cities is several roles (apply to all). Only the
  same job posted twice - same URL, or same company + title + a shared
  location - is a duplicate (see DuplicateFilter).
- Tsenta jobs it says it can't apply to (`isSupportedAts` false) are skipped.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit

MAX_AGE = timedelta(hours=24)          # permanent age limit for every source
MAX_YEARS = 5
NO_SPONSORSHIP = "NO_SPONSORSHIP"

# What the per-run summary counts, in display order.
CATEGORIES = (
    "title", "location", "recency", "experience", "blocked-domain",
    "no-apply-list", "sponsorship", "clearance", "unsupported-ats", "duplicate",
)


@dataclass
class Candidate:
    """One job, normalized from any source."""

    source: str
    title: str
    company: Optional[str] = None
    company_website: Optional[str] = None
    url: str = ""
    locations: list = field(default_factory=list)
    posted_at: Optional[datetime] = None       # exact time (aware UTC; naive is read as UTC)
    posted_label: Optional[str] = None         # "2 hours ago" - only used when posted_at is missing
    years_min: Optional[int] = None            # minimum years of experience, if the source states it
    sponsorship: Optional[str] = None          # SPONSORS | CASE_BY_CASE | UNKNOWN | NO_SPONSORSHIP
    clearance_required: Optional[bool] = None
    supported_ats: Optional[bool] = None       # None = unknown (only Tsenta's feed says)
    description: Optional[str] = None
    match_score: Optional[int] = None
    ref: Any = None                            # the source's own handle (Tsenta job id, Job row id)


@dataclass(frozen=True)
class Verdict:
    passed: bool
    category: Optional[str] = None
    reason: Optional[str] = None


# ---------------------------------------------------------------- helpers

def _strip_accents(text):
    return "".join(c for c in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(c))


def _words(text):
    """Lowercase, accents gone, hyphens/underscores/slashes as spaces, one space between words."""
    text = _strip_accents(text).lower()
    text = re.sub(r"[-_/\\]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _host(url_or_domain):
    value = (url_or_domain or "").strip().lower()
    if not value:
        return ""
    if "://" not in value:
        value = "//" + value
    host = (urlsplit(value).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _host_is(host, domain):
    return host == domain or host.endswith("." + domain)


def _as_utc(dt):
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


# ---------------------------------------------------------------- title

# Your target titles, matched as case-insensitive substrings after punctuation
# becomes spaces ("Full-Stack" -> "full stack", "AI/ML Engineer" -> "ai ml engineer").
# These same names are the search terms sent to Tsenta's feed (SEARCH_QUERY).
# Added 2026-11-25 at your request: system/solutions engineer, analytics/BI,
# forward-deployed engineer, AI/ML engineer, ML engineer; "Fullstack" is the
# obvious spelling variant of "Full Stack".
STRONG_TITLES = (
    "Software Engineer", "Software Development Engineer", "Machine Learning Engineer", "Machine Learning Research Scientist",
    "Data Scientist", "Data Engineer", "AI Engineer", "AI Platform Engineer", "AI Product Engineer", "AI Prompt Engineer",
    "AI Research Engineer", "AI Security Engineer", "AI Solutions Engineer", "Full Stack", "Fullstack", "Backend", "Back End",
    "Developer", "MLOps", "ML Ops", "Python Developer", "Data Analyst",
    "ML Engineer", "AI/ML Engineer", "ML/AI Engineer", "Forward Deployed Engineer",
    "Analytics Engineer", "Business Intelligence", "BI Engineer", "BI Analyst", "Data & Analytics",
)
# Generic engineer titles: real in software, but also common in hardware, civil
# and sales, so they need a second look (see _NON_SOFTWARE_DISCIPLINES).
GENERIC_TITLES = (
    "Platform Engineer", "Applications Engineer", "Associate Engineer", "Research Engineer", "Cloud Engineer",
    "Systems Engineer", "System Engineer", "Solutions Engineer", "Solution Engineer",
)
SEARCH_QUERY = "|".join((*STRONG_TITLES, *GENERIC_TITLES))
# Sent to Tsenta as `excludeSearch`; the local title rule (whole words) is the real gate.
EXCLUDE_QUERY = "Staff|Principal|Lead|Manager|Director|Intern"
_STRONG_TITLES = tuple(_words(t) for t in STRONG_TITLES)
_GENERIC_TITLES = tuple(_words(t) for t in GENERIC_TITLES)

_TITLE_MATCH = {t: re.compile(r"(?<![a-z0-9])" + re.escape(t)) for t in (*_STRONG_TITLES, *_GENERIC_TITLES)}

# Skipped even when a target term matches. Whole words, not substrings, so
# "International", "Internal" and "Staffing" don't trip "Intern"/"Staff".
_BLOCKED_TITLE_WORDS = (
    ("Staff", r"staff"), ("Principal", r"principal"), ("Lead", r"leads?"), ("Manager", r"managers?"),
    ("Director", r"directors?"), ("Intern", r"interns?|internships?"),
)
_BLOCKED_TITLE = [(name, re.compile(rf"(?<![a-z0-9])(?:{pat})(?![a-z0-9])")) for name, pat in _BLOCKED_TITLE_WORDS]

# "Pharmacy Technician, Back End" and friends: matches a target term but is
# plainly not software.
_NOT_SOFTWARE = re.compile(
    r"(?<![a-z0-9])(?:technicians?|technologists?|pharmac\w*|nurs\w*|physicians?|therapists?|dental|dentists?|veterinar\w*|"
    r"warehouse|forklift|drivers?|couriers?|mechanics?|electricians?|plumbers?|welders?|machinists?|janitor\w*|custodian\w*|"
    r"cashiers?|baristas?|chefs?|cooks?|stockers?|merchandis\w*|retail|restaurant|kitchen|bartenders?|housekeep\w*|"
    r"real estate developers?|property developers?|land developers?|business developers?|sales developers?|"
    r"developer advocates?|developer relations|developer evangelists?|devrel)(?![a-z0-9])"
)
# Disciplines that, next to a generic "engineer" title, mean it isn't software.
_NON_SOFTWARE_DISCIPLINES = re.compile(
    r"(?<![a-z0-9])(?:mechanical|electrical|electronics?|civil|chemical|structural|manufacturing|industrial|biomedical|"
    r"hardware|semiconductor|optical|photonics|materials?|aerospace|automotive|hvac|field|process|sales|"
    r"rf|petroleum|geotechnical|environmental|construction)(?![a-z0-9])"
)


def rule_title(c, now=None):
    title = _words(c.title)
    if not title:
        return "no title"
    for name, pattern in _BLOCKED_TITLE:
        if pattern.search(title):
            return f"title contains '{name}'"
    bad = _NOT_SOFTWARE.search(title)
    if bad:
        return f"not a software role ('{bad.group(0)}' in the title)"
    strong = [t for t in _STRONG_TITLES if _TITLE_MATCH[t].search(title)]
    generic = [t for t in _GENERIC_TITLES if _TITLE_MATCH[t].search(title)]
    if not strong and not generic:
        return "title isn't one of your target roles"
    if not strong:
        discipline = _NON_SOFTWARE_DISCIPLINES.search(title)
        if discipline:
            return f"not a software role ('{discipline.group(0)}' next to a generic engineer title)"
    return None


# ---------------------------------------------------------------- location

_US_STATE_CODES = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split()
)
_STATE_NAME_LIST = sorted(
    ["alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware", "florida", "georgia",
     "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts",
     "michigan", "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
     "new mexico", "new york", "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island",
     "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont", "virginia", "washington", "west virginia",
     "wisconsin", "wyoming", "district of columbia"],
    key=len, reverse=True,
)
_STATE_CODE_ALT = "|".join(sorted(_US_STATE_CODES))
_STATE_NAME_ALT = "|".join(re.escape(n) for n in _STATE_NAME_LIST)
# ", TX" / ", Texas" straight after a place name means that place is in the US
# ("Paris, TX", "Dublin, CA", "Lebanon, NH") even if the name is also foreign.
_US_STATE_AFTER = re.compile(rf"^\s*,\s*(?:(?:{_STATE_CODE_ALT})(?![A-Za-z])|(?:{_STATE_NAME_ALT})(?![a-z]))", re.I)
_US_STATE_AFTER_CASED = re.compile(rf"^\s*,\s*(?:{_STATE_CODE_ALT})(?![A-Za-z])")

_US_MARKER = re.compile(r"(?<![A-Za-z])(?:USA|U\.S\.A\.?|U\.S\.|US|United States(?: of America)?)(?![A-Za-z])")
_US_STATE_CODE_WORD = re.compile(rf"(?<![A-Za-z])(?:{_STATE_CODE_ALT})(?![A-Za-z])")
_US_STATE_NAME_WORD = re.compile(rf"(?<![a-z])(?:{_STATE_NAME_ALT})(?![a-z])", re.I)
_CANADIAN_PROVINCE_CODE = re.compile(r"(?<![A-Za-z])(?:ON|BC|QC|AB|NS|NB|MB|SK|NL|PE|YT|NT|NU)(?![A-Za-z])")

_NON_US_PLACES = (
    # countries and regions
    "canada", "united kingdom", "uk", "england", "scotland", "wales", "northern ireland", "ireland", "india", "germany", "france",
    "spain", "italy", "netherlands", "holland", "belgium", "luxembourg", "switzerland", "austria", "poland", "czechia",
    "czech republic", "slovakia", "slovenia", "croatia", "serbia", "hungary", "romania", "bulgaria", "greece", "portugal", "sweden",
    "norway", "denmark", "finland", "iceland", "estonia", "latvia", "lithuania", "ukraine", "russia", "turkey", "turkiye",
    "israel", "uae", "united arab emirates", "saudi arabia", "qatar", "kuwait", "bahrain", "egypt", "nigeria", "kenya",
    "south africa", "morocco", "ghana", "brazil", "argentina", "chile", "colombia", "peru", "uruguay", "ecuador", "mexico",
    "costa rica", "panama", "dominican republic", "jamaica", "puerto rico", "japan", "china", "hong kong", "taiwan",
    "south korea", "korea", "singapore", "malaysia", "indonesia", "philippines", "vietnam", "thailand", "pakistan",
    "bangladesh", "sri lanka", "nepal", "australia", "new zealand", "emea", "apac", "latam", "eu", "europe", "asia", "africa",
    "middle east", "north america", "americas", "worldwide", "global", "anywhere", "international",
    # big non-US cities
    "london", "manchester", "edinburgh", "dublin", "toronto", "vancouver", "montreal", "ottawa", "calgary", "waterloo",
    "berlin", "munich", "hamburg", "frankfurt", "paris", "lyon", "amsterdam", "rotterdam", "brussels", "zurich", "geneva",
    "vienna", "prague", "warsaw", "krakow", "wroclaw", "budapest", "bucharest", "sofia", "athens", "lisbon", "porto", "madrid",
    "barcelona", "milan", "rome", "stockholm", "gothenburg", "oslo", "copenhagen", "helsinki", "tallinn", "riga", "vilnius",
    "kyiv", "tel aviv", "jerusalem", "haifa", "dubai", "abu dhabi", "riyadh", "doha", "cairo", "lagos", "nairobi", "cape town",
    "johannesburg", "bangalore", "bengaluru", "hyderabad", "pune", "mumbai", "delhi", "new delhi", "gurgaon", "gurugram",
    "noida", "chennai", "kolkata", "ahmedabad", "karachi", "lahore", "dhaka", "colombo", "kathmandu", "sydney", "melbourne",
    "brisbane", "perth", "auckland", "tokyo", "osaka", "seoul", "beijing", "shanghai", "shenzhen", "taipei", "kuala lumpur",
    "jakarta", "manila", "hanoi", "ho chi minh", "bangkok", "mexico city", "guadalajara", "monterrey", "sao paulo",
    "rio de janeiro", "buenos aires", "santiago", "bogota", "medellin", "lima", "quito", "montevideo", "san jose costa rica",
)
_NON_US_RE = re.compile(r"(?<![A-Za-z])(" + "|".join(re.escape(p) for p in sorted(_NON_US_PLACES, key=len, reverse=True)) + r")(?![A-Za-z])", re.I)

# Bare city names (no state) that are unambiguous enough to count as US.
_US_METROS = (
    "new york", "new york city", "nyc", "brooklyn", "manhattan", "san francisco", "bay area", "san francisco bay area", "silicon valley",
    "los angeles", "san diego", "seattle", "austin", "boston", "chicago", "denver", "atlanta", "dallas", "houston", "miami",
    "washington dc", "washington d c", "philadelphia", "phoenix", "portland", "san jose", "palo alto", "mountain view", "sunnyvale",
    "menlo park", "redwood city", "cambridge", "pittsburgh", "minneapolis", "detroit", "salt lake city", "raleigh", "charlotte",
    "nashville", "las vegas", "orlando", "tampa", "baltimore", "columbus", "indianapolis", "kansas city", "st louis", "san antonio",
    "boulder", "ann arbor", "sf",
)
_US_METRO_RE = re.compile(r"(?<![A-Za-z])(?:" + "|".join(re.escape(m) for m in sorted(_US_METROS, key=len, reverse=True)) + r")(?![A-Za-z])", re.I)
_REMOTE = re.compile(r"remote|work from home|wfh|anywhere", re.I)


_STATE_CODE_ONLY = re.compile(rf"(?:{_STATE_CODE_ALT})")
_STATE_NAME_ONLY = re.compile(rf"(?:{_STATE_NAME_ALT})", re.I)


def _non_us_hit(text):
    """First non-US country/city named in `text`, or None.

    A name that is really a US place doesn't count: US state names are removed
    first ("New Mexico" isn't Mexico), and a place followed by a US state in the
    next comma-segment is a US town ("Paris, TX", "Panama City, FL")."""
    original = re.split(r"[,;|]", text)
    scanned = [_US_STATE_NAME_WORD.sub(" ", seg) for seg in original]
    for i, seg in enumerate(scanned):
        following = original[i + 1].strip() if i + 1 < len(original) else ""
        if _STATE_CODE_ONLY.fullmatch(following) or _STATE_NAME_ONLY.fullmatch(following):
            continue
        m = _NON_US_RE.search(seg)
        if m:
            return m.group(1)
    match = _CANADIAN_PROVINCE_CODE.search(text)
    return match.group(0) if match else None


def _us_evidence(text):
    return bool(
        _US_MARKER.search(text) or _US_STATE_CODE_WORD.search(text) or _US_STATE_NAME_WORD.search(text) or _US_METRO_RE.search(_strip_accents(text))
    )


def rule_location(c, now=None):
    """US only, strictly: a clear US place is required, anything naming a
    non-US country/city is out, and a bare "Remote" doesn't count as US."""
    places = [p for p in (c.locations or []) if p and p.strip()]
    if not places:
        return "no location listed (US-only rule needs one)"
    texts = [_strip_accents(p) for p in places]
    for text in texts:
        hit = _non_us_hit(text)
        if hit:
            return f"non-US location ('{hit}' in '{text.strip()}')"
    confirmed, unknown = False, []
    for text in texts:
        if _us_evidence(text):
            confirmed = True
        elif _REMOTE.search(text):
            return f"remote without an explicit US location ('{text.strip()}')"
        else:
            unknown.append(text.strip())
    if not confirmed:
        return f"can't confirm a US location ('{unknown[0]}')"
    return None


# ---------------------------------------------------------------- recency

_UNIT_SECONDS = {"se": 1, "mi": 60, "ho": 3600, "hr": 3600, "da": 86400, "we": 604800, "wk": 604800, "mo": 2_592_000, "ye": 31_536_000, "yr": 31_536_000}
_LABEL_AGE = re.compile(r"(\d+)\s*(second|sec|minute|min|hour|hr|day|week|wk|month|mo|year|yr)s?\.?\s*ago", re.I)


def _label_age(label):
    """-> (timedelta or None, reason-if-unverifiable). "N days ago" means
    N*24h or more, so 3+ days is past the limit and 1-2 days still fits."""
    text = (label or "").strip().lower()
    if not text:
        return None, "no posting time"
    if "repost" in text:
        return None, f"'{label.strip()}' - reposted, so the original posting time is unknown"
    if text in ("just now", "today", "new"):
        return timedelta(0), None
    if text == "yesterday":
        return timedelta(days=1), None
    m = _LABEL_AGE.search(text)
    if not m:
        return None, f"can't read the posting time '{label.strip()}'"
    per = _UNIT_SECONDS[m.group(2)[:2].lower()]
    return timedelta(seconds=int(m.group(1)) * per), None


def estimated_age(c, now=None):
    """How long ago the job was posted, or None if unknown: exact when the source gives a time,
    otherwise read from a label like "2 hours ago" (approximate - "2 days ago" is at least 48h)."""
    now = _as_utc(now) if now else datetime.now(timezone.utc)
    if c.posted_at is not None:
        return max(timedelta(0), now - _as_utc(c.posted_at))
    return _label_age(c.posted_label)[0]


def rule_recency(c, now=None):
    """Posted within MAX_AGE (24 hours) - and provably so: no verifiable time, no apply."""
    now = _as_utc(now) if now else datetime.now(timezone.utc)
    if c.posted_at is not None:
        age = now - _as_utc(c.posted_at)
        if age < -timedelta(hours=1):
            return "posting time is in the future - can't verify"
        described = f"{age.total_seconds() / 3600:.0f}h ago"
    else:
        age, why = _label_age(c.posted_label)
        if age is None:
            return f"posting time can't be verified: {why}"
        described = f"'{c.posted_label.strip()}'"
    # An exact time may be exactly 24h old; a label like "1 day ago" only says
    # "at least 24h", so there it already counts as over.
    over = age > MAX_AGE if c.posted_at is not None else age >= MAX_AGE
    if over:
        return f"posted {described}, over the {int(MAX_AGE.total_seconds() // 3600)}h limit"
    return None


# ---------------------------------------------------------------- blocked domains

BLOCKED_DOMAINS = ("linkedin.com", "lifeattiktok.com", "apply.careers.microsoft.com", "dice.com")


def rule_blocked_domain(c, now=None):
    host = _host(c.url)
    for domain in BLOCKED_DOMAINS:
        if _host_is(host, domain):
            return f"blocked domain {domain} (Tsenta can't submit there)"
    return None


# ---------------------------------------------------------------- never-apply list

# Company-name patterns, matched on whole words after dots/hyphens are
# normalized ("J.P. Morgan" -> "jp morgan"). Domains cover the company website
# and the posting URL; "jpmc" is also caught anywhere in a URL.
_NEVER_NAMES = (
    r"amazon", r"aws", r"whole foods", r"twitch", r"tesla", r"wells fargo",
    r"jpmorgan", r"jpmorganchase", r"jpmorgan chase", r"jp morgan", r"jpmc", r"chase",
    r"spacex", r"spacexsi", r"xai", r"twitter",
)
_NEVER_NAME_RE = re.compile(r"(?<![a-z0-9])(?:" + "|".join(_NEVER_NAMES) + r")(?![a-z0-9])")
# "X" is the company's whole name, so it only counts as the entire name or
# "X (Twitter)" / "X Corp" - never as a letter inside another name.
_X_CORP_RE = re.compile(r"^x(?:\s+corp(?:oration)?)?(?:\s*\(.*(?:twitter|formerly).*\))?$")
_NEVER_DOMAINS = (
    "amazon.com", "amazon.jobs", "wholefoodsmarket.com", "twitch.tv", "tesla.com", "wellsfargo.com", "wellsfargojobs.com",
    "chase.com", "jpmorganchase.com", "jpmorgan.com", "jpmc.fa.oraclecloud.com", "spacex.com", "x.com", "twitter.com", "x.ai",
)


def _name_key(name):
    text = _strip_accents(name).lower().replace("&", " and ")
    text = re.sub(r"['’]", "", text)
    text = re.sub(r"(?<![a-z0-9])([a-z])\.(?=[a-z](?:\.|(?![a-z0-9])))", r"\1", text)   # J.P. Morgan -> jp. morgan
    text = re.sub(r"[^a-z0-9()]+", " ", text)         # then amazon.com -> amazon com
    return re.sub(r"\s+", " ", text).strip()


def rule_never_apply(c, now=None):
    name = _name_key(c.company or "")
    if name:
        hit = _NEVER_NAME_RE.search(name)
        if hit:
            return f"company is on your never-apply list (name matches '{hit.group(0)}')"
        if _X_CORP_RE.match(name):
            return "company is on your never-apply list (X / Twitter)"
    for label, value in (("website", c.company_website), ("URL", c.url)):
        host = _host(value)
        for domain in _NEVER_DOMAINS:
            if _host_is(host, domain):
                return f"company is on your never-apply list ({label} {domain})"
    if "jpmc" in (c.url or "").lower():
        return "company is on your never-apply list (URL contains 'jpmc')"
    return None


# ---------------------------------------------------------------- experience

_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12, "fifteen": 15}
_NUM = r"(\d{1,2}|" + "|".join(_NUMBER_WORDS) + r")"
# "6+ years", "6 or more years", "at least 6 years", "6-8 years", "minimum of 6 years" ...
_YEARS_PHRASE = re.compile(
    rf"(?:at least|minimum(?: of)?|min\.?|over|more than)?\s*{_NUM}\s*(?:\+|plus|or more|and above)?\s*(?:(?:-|–|—|to)\s*\d{{1,2}}\s*\+?\s*)?(?:years?|yrs?)\b",
    re.I,
)
# A years figure only counts as an experience requirement with one of these near it,
# so "founded 15 years ago" or a "4 year vesting" schedule are ignored.
_EXPERIENCE_CONTEXT = re.compile(r"experience|exp\b|background|hands[- ]on|professional|industry|working|developing|building|engineering|software|years' ", re.I)


MAX_PLAUSIBLE_YEARS = 15          # "60 years of experience" is the company talking, not a requirement
_COMPANY_HISTORY = re.compile(r"\s*-?\s*(?:history|of history|in business|ago|old|of service|of innovation|of excellence)\b", re.I)


def parse_min_years(text, lenient=False):
    """Smallest years-of-experience figure in `text`, or None. If a posting lists
    several ("3+ years Python, 8+ years leading teams") the smallest is used,
    which errs toward applying. `lenient` accepts bare "1+ years exp" style
    strings that have no other context."""
    if not text:
        return None
    found = []
    for m in _YEARS_PHRASE.finditer(text):
        raw = m.group(1).lower()
        value = _NUMBER_WORDS.get(raw, None) if not raw.isdigit() else int(raw)
        if value is None:
            continue
        if value > MAX_PLAUSIBLE_YEARS or _COMPANY_HISTORY.match(text[m.end(): m.end() + 30]):
            continue
        window = text[max(0, m.start() - 60): m.end() + 60]
        if lenient or _EXPERIENCE_CONTEXT.search(window):
            found.append(value)
    return min(found) if found else None


def rule_experience(c, now=None):
    """Apply at 0-5 years or when it isn't stated; skip above 5."""
    years = c.years_min if c.years_min is not None else parse_min_years(c.description)
    if years is not None and years > MAX_YEARS:
        return f"asks for {years}+ years of experience (limit {MAX_YEARS})"
    return None


# ---------------------------------------------------------------- sponsorship

_NO_SPONSORSHIP_TEXT = re.compile(
    r"\b(?:no|not|without|unable to|cannot|can't|won't|will not|do not|does not|don't|doesn't|not able to|not eligible for|not available for|not offer(?:ing)?|not provid(?:e|ing))\b"
    r"[^.\n]{0,50}?(?:visa|work|immigration|employment)?\s*sponsorship"
    r"|sponsorship\s+(?:is\s+)?(?:not|un)\s*(?:available|offered|provided|supported)"
    r"|(?:unable to|not able to|cannot|can't|won't|will not|do not|does not|don't|doesn't|not)\s+(?:currently\s+)?sponsor\b"
    r"|no\s+visa\s+(?:support|sponsorship)|visa\s+support\s+(?:is\s+)?not"
    r"|(?:not|never)\s+(?:now\s+or\s+in\s+the\s+future\s+)?(?:require|requiring|need|needing)\s+(?:visa\s+|work\s+|immigration\s+)?sponsorship"
    r"|without\s+(?:the\s+need\s+for\s+)?(?:employer\s+|visa\s+|work\s+)?sponsorship",
    re.I,
)
# Wording that *offers* sponsorship must not trigger the pattern above.
_SPONSORSHIP_OFFERED = re.compile(r"(?:will|can|do|does|may|able to|happy to|available|offer(?:s|ed)?)[^.\n]{0,40}sponsor", re.I)
_CITIZENS_ONLY = re.compile(
    r"u\.?s\.?\s+citizens?\s+only|citizens?\s+of\s+the\s+(?:u\.?s\.?|united states)\s+only|only\s+u\.?s\.?\s+citizens?|"
    r"must\s+be\s+(?:a\s+)?u\.?s\.?\s+citizen|u\.?s\.?\s+citizenship\s+(?:is\s+)?(?:required|mandatory)|requires?\s+u\.?s\.?\s+citizenship",
    re.I,
)
_SIMPLIFY_SPONSORSHIP = {"does not offer sponsorship": NO_SPONSORSHIP, "u.s. citizenship is required": NO_SPONSORSHIP}


def normalize_sponsorship(value):
    """Map any source's label onto SPONSORS / CASE_BY_CASE / UNKNOWN / NO_SPONSORSHIP."""
    text = (value or "").strip()
    if not text:
        return None
    if text.upper() in ("SPONSORS", "CASE_BY_CASE", "UNKNOWN", NO_SPONSORSHIP):
        return text.upper()
    lowered = text.lower()
    if lowered in _SIMPLIFY_SPONSORSHIP:
        return _SIMPLIFY_SPONSORSHIP[lowered]
    if lowered == "offers sponsorship":
        return "SPONSORS"
    return "UNKNOWN"          # Simplify's "Other" and anything unrecognized


# Application-form questions ("Will you now or in the future require sponsorship? Yes No",
# "Are you a U.S. person?") are questions, not requirements.
_QUESTION_LEAD = re.compile(r"\b(?:will|would|do|does|are|have|can|could|is|were)\s+you\b[^.\n]{0,90}$", re.I)


def _is_question(text, m):
    before = text[max(0, m.start() - 110): m.start()]
    return bool(_QUESTION_LEAD.search(before) or re.match(r"[^.\n]{0,70}\?", text[m.end(): m.end() + 80]))


def _says_no_sponsorship(text):
    for m in _NO_SPONSORSHIP_TEXT.finditer(text):
        if _is_question(text, m):
            continue
        around = text[max(0, m.start() - 40): m.end() + 10]
        if _SPONSORSHIP_OFFERED.search(around) and not re.search(r"\bnot\b|n't|\bno\b|without|unable|cannot", around, re.I):
            continue
        return m.group(0).strip()
    return None


def rule_sponsorship(c, now=None):
    """Passes SPONSORS, CASE_BY_CASE and UNKNOWN. Skips NO_SPONSORSHIP and any
    posting that says no sponsorship / no visa support / US citizens only."""
    if normalize_sponsorship(c.sponsorship) == NO_SPONSORSHIP:
        return "posting is marked no sponsorship"
    text = c.description or ""
    if text:
        said = _says_no_sponsorship(text)
        if said:
            return f"posting says '{said[:70]}'"
        citizens = next((m for m in _CITIZENS_ONLY.finditer(text) if not _is_question(text, m)), None)
        if citizens:
            return f"posting says '{citizens.group(0)[:70]}'"
    return None


# ---------------------------------------------------------------- clearance / citizenship / ITAR

_CLEARANCE = re.compile(
    r"(?:security|secret|top[- ]secret|ts/sci|ts\s*/\s*sci|dod|government|federal)\s+clearance|clearance\s+(?:is\s+)?(?:required|needed|eligib\w+)|"
    r"\bts/sci\b|top[- ]secret|public\s+trust|polygraph|\bsci\b\s+eligib\w+|active\s+(?:secret|clearance)",
    re.I,
)
_NEGATED = re.compile(r"(?:no|not|without|doesn't|does not|don't|do not|neither)\b[^.\n]{0,40}$", re.I)
_CITIZENSHIP = re.compile(
    r"(?:must|required to|need to|needs to|have to)\s+(?:be|hold)\s+(?:a\s+)?(?:u\.?s\.?|united states)\s+(?:citizen|person)|"
    r"(?:u\.?s\.?|united states)\s+(?:citizenship|person\s+status)\s+(?:is\s+)?(?:required|mandatory)|requires?\s+(?:u\.?s\.?|united states)\s+(?:citizenship|person)|"
    r"(?:only|limited to)\s+(?:u\.?s\.?|united states)\s+(?:citizens|persons)|(?:u\.?s\.?|united states)\s+citizens?\s+(?:only|required)",
    re.I,
)
# Deliberately narrow: "subject to export control restrictions" is boilerplate on many
# postings (Texas Instruments and others) and says nothing about who may apply.
_ITAR = re.compile(r"\bITAR\b|international traffic in arms|export administration regulations|\bu\.?s\.?\s+persons?\b", re.I)
_NONE_AFTER = re.compile(r"\s*[:\-–]?\s*(?:none|no\b|n/a|not\b)", re.I)


_OPTIONAL_AFTER = re.compile(r"^[^.\n]{0,40}?(?:a plus|preferred|desirable|a bonus|nice to have|helpful|not required|not necessary)", re.I)


def _unnegated(pattern, text):
    for m in pattern.finditer(text):
        before = text[max(0, m.start() - 45): m.start()]
        if (_NEGATED.search(before) or _OPTIONAL_AFTER.search(text[m.end(): m.end() + 60])
                or _NONE_AFTER.match(text[m.end(): m.end() + 20]) or _is_question(text, m)):
            continue
        return m.group(0).strip()
    return None


def rule_clearance_citizenship(c, now=None):
    """Security clearance, US citizenship, or US-person / ITAR requirements."""
    if c.clearance_required:
        return "requires a security clearance (flagged by the source)"
    text = c.description or ""
    if text:
        for label, pattern in (("security clearance", _CLEARANCE), ("US citizenship", _CITIZENSHIP), ("US-person / ITAR", _ITAR)):
            said = _unnegated(pattern, text)
            if said:
                return f"requires {label} (posting says '{said[:60]}')"
    return None


# ---------------------------------------------------------------- Tsenta support

def rule_unsupported_ats(c, now=None):
    if c.supported_ats is False:
        return "Tsenta doesn't support this application system"
    return None


# ---------------------------------------------------------------- the rule list

# (category, function), checked in this order; the first skip wins. Cheap and
# unambiguous checks go first.
RULES: list = [
    ("blocked-domain", rule_blocked_domain),
    ("no-apply-list", rule_never_apply),
    ("title", rule_title),
    ("location", rule_location),
    ("recency", rule_recency),
    ("unsupported-ats", rule_unsupported_ats),
    ("experience", rule_experience),
    ("sponsorship", rule_sponsorship),
    ("clearance", rule_clearance_citizenship),
]


def evaluate(c, now=None):
    """Run every rule in order; returns the first skip as a Verdict, or a pass."""
    for category, rule in RULES:
        reason = rule(c, now)
        if reason:
            return Verdict(False, category, reason)
    return Verdict(True)


# ---------------------------------------------------------------- duplicates

_TRACKING_PARAMS = re.compile(
    r"^(?:utm_.*|gh_src|gh_jid_src|src|source|sourceid|ref|referrer|refid|trk|trkid|lever-(?:source|origin)|fbclid|gclid|msclkid|mc_[ce]id|"
    r"campaign|cid|rx_.*|jr_id_src|jobsource|jbid_src|codes?|sid|tracking.*)$", re.I,
)


def normalize_url(url):
    """Same posting, same string: lowercase host without www, no fragment or
    trailing slash, tracking parameters dropped. Real job-id parameters
    (gh_jid, jobId, id, req...) are kept."""
    if not url:
        return ""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING_PARAMS.match(k)))
    path = re.sub(r"/+$", "", parts.path) or ""
    return f"{host}{path}" + (f"?{query}" if query else "")


def _title_key(title):
    return re.sub(r"[^a-z0-9]+", " ", _strip_accents(title or "").lower()).strip()


def _place_keys(locations):
    keys = set()
    for place in locations or []:
        for part in re.split(r"[;|]", place or ""):
            key = re.sub(r"[^a-z0-9]+", " ", _strip_accents(part).lower()).strip()
            if key:
                keys.add(key)
    return keys


class DuplicateFilter:
    """Never apply to the same job twice. A duplicate is the same normalized URL,
    or the same company + title + a shared location (the same posting reached
    through two sources). The same title at the same company in *different*
    places is a different role and is allowed.

    Seed it with what's already been applied to (`seen_urls`, `seen_roles`), then
    `check()` each candidate and `remember()` the ones you act on.
    """

    def __init__(self, seen_urls=(), seen_roles=()):
        self._urls = {normalize_url(u) for u in seen_urls if u}
        self._roles = {}                 # (company, title) -> set of place keys
        for company, title, locations in seen_roles:
            self._add_role(company, title, locations)

    def _add_role(self, company, title, locations):
        key = (_name_key(company or ""), _title_key(title))
        self._roles.setdefault(key, set()).update(_place_keys(locations) or {""})

    def check(self, c, now=None):
        url = normalize_url(c.url)
        if url and url in self._urls:
            return "already applied or seen: same job URL"
        key = (_name_key(c.company or ""), _title_key(c.title))
        if key[0] and key in self._roles:
            places = _place_keys(c.locations) or {""}
            if places & self._roles[key]:
                return "duplicate cross-post: same company, title and location already handled"
        return None

    def remember(self, c):
        url = normalize_url(c.url)
        if url:
            self._urls.add(url)
        self._add_role(c.company, c.title, c.locations)
