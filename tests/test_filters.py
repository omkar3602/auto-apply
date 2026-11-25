"""One table per rule. Each case is (input, should_skip) so a failure names the exact input."""
from datetime import datetime, timedelta, timezone

import pytest

from automation import filters as f
from automation.filters import Candidate, DuplicateFilter

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def cand(**kw):
    kw.setdefault("source", "test")
    kw.setdefault("title", "Software Engineer")
    return Candidate(**kw)


# ------------------------------------------------------------------ title

TITLE_PASS = [
    "Software Engineer", "Senior Software Engineer", "Sr. Software Engineer II", "Software Engineer, Backend",
    "Software Engineer (New Grad)", "Software Engineering Associate", "Software Development Engineer II",
    "Machine Learning Engineer", "Machine Learning Research Scientist", "Data Scientist", "Data Engineer",
    "Data Engineer II, Global Logistics, Exports", "AI Engineer", "AI Platform Engineer", "AI Product Engineer",
    "AI Prompt Engineer", "AI Research Engineer", "AI Security Engineer", "AI Solutions Engineer",
    "Full Stack Engineer", "Full-Stack Developer", "Fullstack Engineer", "Backend Engineer", "Back-End Developer",
    "Back End Engineer", "Web Developer", "Python Developer", "Platform Engineer", "Applications Engineer",
    "Associate Engineer, Software", "Research Engineer, NLP", "MLOps Engineer", "ML Ops Engineer", "Cloud Engineer",
    "Data Analyst", "Hardware Software Engineer",
    # added 2026-11-25
    "Systems Engineer", "System Engineer", "Solutions Engineer", "Solutions Engineer, Customer Success", "Senior Solutions Engineer",
    "Forward Deployed Engineer", "AI Forward Deployed Engineer", "Forward-Deployed Engineer", "Analytics Engineer", "Data Analytics Engineer",
    "Business Intelligence Analyst", "BI Engineer", "BI Analyst", "Data & Analytics Consultant", "AI/ML Engineer", "AI / ML Engineer",
    "AI-ML Engineer", "ML/AI Engineer", "ML Engineer", "Senior ML Engineer", "Applied ML Engineer",
    # word boundaries: these merely contain the letters of a blocked word
    "International Software Engineer", "Internal Tools Software Engineer", "Staffing Platform Software Engineer",
    "Software Engineer, Leadership Programs",
]
TITLE_SKIP = [
    ("Staff Software Engineer", "Staff"), ("Software Engineer, Staff", "Staff"), ("Principal Platform Engineer", "Principal"),
    ("Software Engineering Manager", "Manager"), ("Engineering Manager, Backend", "Manager"), ("Director of Software Engineering", "Director"),
    ("Tech Lead, Backend", "Lead"), ("Lead Data Scientist", "Lead"), ("Software Engineer Intern", "Intern"),
    ("Machine Learning Engineer Internship", "Intern"), ("Software Engineer - Summer Intern", "Intern"), ("Product Manager", "Manager"),
    ("Pharmacy Technician, Back End", "not a software role"), ("Warehouse Associate, Back End", "not a software role"),
    ("Real Estate Developer", "not a software role"), ("Business Developer", "not a software role"),
    ("Developer Advocate", "not a software role"), ("Mechanical Applications Engineer", "not a software role"),
    ("Electrical Associate Engineer", "not a software role"), ("Sales Applications Engineer", "not a software role"),
    ("Field Applications Engineer", "not a software role"), ("Civil Research Engineer", "not a software role"),
    ("Hardware Platform Engineer", "not a software role"),
    ("Mechanical Systems Engineer", "not a software role"), ("Aerospace Systems Engineer", "not a software role"),
    ("Sales Solutions Engineer", "not a software role"), ("Pre-Sales Solutions Engineer", "not a software role"),
    ("Electrical Systems Engineer", "not a software role"), ("Business Intelligence Manager", "Manager"),
    ("Data & Analytics Intern", "Intern"), ("HTML Engineer", "target"), ("Sales Engineer", "target"),
    ("Data Entry Clerk", "target"), ("Marketing Analyst", "target"), ("Graphic Designer", "target"), ("DevOps Engineer", "target"), ("", "no title"),
]


@pytest.mark.parametrize("title", TITLE_PASS)
def test_title_passes(title):
    assert f.rule_title(cand(title=title)) is None


@pytest.mark.parametrize("title,fragment", TITLE_SKIP)
def test_title_skips(title, fragment):
    reason = f.rule_title(cand(title=title))
    assert reason and fragment.lower() in reason.lower(), reason


# ------------------------------------------------------------------ location

LOCATION_PASS = [
    ["Bellevue, WA"], ["New York, NY"], ["Plano, TX"], ["Remote in USA"], ["Remote - United States"], ["United States"],
    ["Remote, US"], ["US Remote"], ["Naperville, IL, Atlanta, GA"], ["San Francisco, California, United States"],
    ["Austin, TX or Remote"], ["Remote (CA, NY, TX)"], ["New York"], ["NYC"], ["San Francisco Bay Area"], ["Washington, DC"],
    ["Seattle, WA", "Bellevue, WA"], ["Indianapolis, IN"], ["Portland, OR"],
    # foreign-sounding names that are US towns
    ["Paris, TX"], ["Dublin, CA"], ["London, KY"], ["Toronto, OH"],
    # a bare, unknown place alongside a confirmed one
    ["Springfield", "Chicago, IL"],
    # found in real data: state names and towns that contain a foreign name, and Simplify's "SF"
    ["New Mexico"], ["Albuquerque, NM"], ["Panama City, FL"], ["Mexico, MO"], ["SF"], ["Seattle, WA, SF, Austin, TX, San Jose, CA"],
    ["New Mexico, Washington, Kansas, Georgia, Washington, DC, NYC"],
]
LOCATION_SKIP = [
    ([], "no location"), ([""], "no location"), (None, "no location"),
    (["Remote"], "remote without"), (["Remote - Worldwide"], "non-US"), (["Anywhere"], "non-US"),
    (["Toronto, ON"], "non-US"), (["Toronto, ON, Canada"], "non-US"), (["Remote in Canada"], "non-US"), (["Knutsford, UK"], "non-US"),
    (["London, UK, NYC"], "non-US"), (["London"], "non-US"), (["Remote, India"], "non-US"), (["Bengaluru, Karnataka, India"], "non-US"),
    (["Berlin, Germany"], "non-US"), (["São Paulo, Brazil"], "non-US"), (["Mexico City"], "non-US"), (["North America"], "non-US"),
    (["EMEA"], "non-US"), (["US or Canada"], "non-US"), (["Seattle, WA", "Vancouver, BC"], "non-US"),
    (["San Francisco, CA", "Remote"], "remote without"),
    (["Springfield"], "can't confirm"),
    (["Remote in Mexico"], "non-US"), (["Mexico City, Mexico"], "non-US"), (["Panama City, Panama"], "non-US"),
]


@pytest.mark.parametrize("locations", LOCATION_PASS)
def test_location_passes(locations):
    assert f.rule_location(cand(locations=locations)) is None


@pytest.mark.parametrize("locations,fragment", LOCATION_SKIP)
def test_location_skips(locations, fragment):
    reason = f.rule_location(cand(locations=locations))
    assert reason and fragment.lower() in reason.lower(), reason


# ------------------------------------------------------------------ recency

def hours_ago(h):
    return NOW - timedelta(hours=h)


@pytest.mark.parametrize("posted_at,skip", [
    (hours_ago(2), False), (hours_ago(71), False), (hours_ago(72), False), (hours_ago(73), True), (hours_ago(120), True),
    (NOW + timedelta(minutes=30), False), (NOW + timedelta(hours=3), True),
    (datetime(2026, 11, 20, 10, 0), False),            # naive datetimes are read as UTC
    (datetime(2026, 11, 10, 10, 0), True),
])
def test_recency_exact_time(posted_at, skip):
    assert bool(f.rule_recency(cand(posted_at=posted_at), NOW)) is skip


@pytest.mark.parametrize("label,skip", [
    ("2 hours ago", False), ("45 minutes ago", False), ("Posted 5 hours ago", False), ("just now", False), ("yesterday", False),
    ("1 day ago", False), ("2 days ago", False),
    ("3 days ago", True), ("1 week ago", True), ("2 months ago", True),
    ("Reposted 1 hour ago", True), ("Reposted 3 days ago", True), ("sometime", True), ("", True), (None, True),
])
def test_recency_label_only(label, skip):
    assert bool(f.rule_recency(cand(posted_label=label), NOW)) is skip


def test_recency_exact_time_beats_label():
    old = cand(posted_at=hours_ago(100), posted_label="1 hour ago")
    assert "over the 72h limit" in f.rule_recency(old, NOW)


def test_recency_with_nothing_is_unverifiable():
    assert "can't be verified" in f.rule_recency(cand(), NOW)


# ------------------------------------------------------------------ blocked domains

@pytest.mark.parametrize("url,skip", [
    ("https://www.linkedin.com/jobs/view/123", True), ("https://uk.linkedin.com/jobs/1", True), ("https://lifeattiktok.com/search/123", True),
    ("https://apply.careers.microsoft.com/careers/job/1", True), ("https://www.dice.com/job-detail/abc", True),
    ("https://careers.microsoft.com/us/en/job/1", False),      # only the apply. host is blocked
    ("https://jobs.lever.co/acme/1", False), ("https://notlinkedin.com/x", False), ("https://boards.greenhouse.io/linkedinstuff/jobs/1", False),
    ("", False),
])
def test_blocked_domain(url, skip):
    assert bool(f.rule_blocked_domain(cand(url=url))) is skip


# ------------------------------------------------------------------ never-apply list

NEVER_NAMES = [
    "Amazon", "Amazon.com Services LLC", "Amazon Web Services", "AWS", "Whole Foods Market", "Twitch", "Tesla", "Tesla, Inc.",
    "Wells Fargo", "Wells Fargo Advisors", "JPMorgan Chase & Co.", "JPMorganChase", "J.P. Morgan", "JP Morgan", "Chase",
    "SpaceX", "SpaceXSI", "xAI", "X", "X Corp", "X (Twitter)", "X (formerly Twitter)", "Twitter",
]
ALLOWED_NAMES = [
    "Stripe", "Google", "Meta", "Netflix", "Jane Street", "Accenture", "Chaseable Co", "Purchase Labs", "Laws Inc",
    "Amazonia Energy", "Texas Instruments", "Xactly", "X-Ray Labs", "Max Labs", "Waxman AI", "Truist",
]


@pytest.mark.parametrize("name", NEVER_NAMES)
def test_never_apply_by_name(name):
    assert f.rule_never_apply(cand(company=name)), name


@pytest.mark.parametrize("name", ALLOWED_NAMES)
def test_never_apply_allows_other_companies(name):
    assert f.rule_never_apply(cand(company=name)) is None, name


@pytest.mark.parametrize("field,value", [
    ("company_website", "amazon.com"), ("company_website", "https://www.chase.com"), ("company_website", "jpmorganchase.com"),
    ("company_website", "tesla.com"), ("company_website", "x.com"), ("company_website", "x.ai"), ("company_website", "twitter.com"),
    ("url", "https://www.amazon.jobs/en/jobs/10609538"), ("url", "https://hiring.amazon.com/app#/jobDetails"),
    ("url", "https://careers.jpmorgan.com/us/en/jobs/1"), ("url", "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1002/job/1"),
    ("url", "https://someats.com/jpmc/job/77"), ("url", "https://www.wellsfargojobs.com/en/jobs/1"), ("url", "https://boards.twitch.tv/x"),
])
def test_never_apply_by_website_or_url(field, value):
    assert f.rule_never_apply(cand(company="Acme", **{field: value})), (field, value)


@pytest.mark.parametrize("field,value", [
    ("company_website", "stripe.com"), ("company_website", "x-ray.com"), ("company_website", "max.com"), ("company_website", "box.com"),
    ("url", "https://boards.greenhouse.io/stripe/jobs/1"), ("url", "https://jobs.lever.co/netflix/abc"),
])
def test_never_apply_domain_lookalikes_pass(field, value):
    assert f.rule_never_apply(cand(company="Acme", **{field: value})) is None


# ------------------------------------------------------------------ experience

@pytest.mark.parametrize("years,skip", [(0, False), (3, False), (5, False), (6, True), (10, True), (None, False)])
def test_experience_from_field(years, skip):
    assert bool(f.rule_experience(cand(years_min=years))) is skip


@pytest.mark.parametrize("text,expected", [
    ("5+ years of experience in software", 5), ("6+ years of software engineering experience", 6),
    ("at least 7 years' experience", 7), ("minimum of 8 years of relevant experience", 8),
    ("3+ years of Python and 8+ years of leadership experience", 3),      # smallest wins: errs toward applying
    ("5-8 years of experience", 5), ("6 to 10 years of experience", 6), ("ten years of experience", 10),
    ("2 or more years of professional experience", 2), ("0-2 years of industry experience", 0),
    ("We were founded 15 years ago", None), ("a 4 year vesting schedule", None), ("", None), (None, None),
    ("Candidates with 3+ years of experience in software", 3),
    # company boilerplate found in real postings - the company talking, not a requirement
    ("With 60 years of experience across industries, Acme builds software.", None),
    ("With over 60 years of experience, AMA specializes in aerospace engineering", None),
    ("Homes.com is a company with 20+ years' experience in leading digital marketplaces", None),
    ("building on our 75+ year history in signal processing software", None),
    ("one of the best insurers in the industry for over 170 years", None),
])
def test_parse_min_years_from_description(text, expected):
    assert f.parse_min_years(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("1+ years exp", 1), ("0+ years exp", 0), ("0–2 years exp", 0), ("1–2 years exp", 1), ("2+ years exp", 2), ("3–5 years exp", 3),
    ("Start in 2027 Spring", None), ("", None), (None, None),
])
def test_parse_min_years_lenient_jobright_strings(text, expected):
    assert f.parse_min_years(text, lenient=True) == expected


def test_experience_field_beats_description():
    assert f.rule_experience(cand(years_min=2, description="10+ years of experience required")) is None


def test_experience_falls_back_to_description():
    assert "asks for 7+" in f.rule_experience(cand(description="Requires 7+ years of experience building software."))
    assert f.rule_experience(cand(description="Requires 3+ years of experience building software.")) is None
    assert f.rule_experience(cand(description="Great team, great benefits.")) is None


# ------------------------------------------------------------------ sponsorship

@pytest.mark.parametrize("value,skip", [
    ("NO_SPONSORSHIP", True), ("SPONSORS", False), ("CASE_BY_CASE", False), ("UNKNOWN", False), (None, False),
    ("Does Not Offer Sponsorship", True), ("U.S. Citizenship is Required", True), ("Offers Sponsorship", False), ("Other", False),
])
def test_sponsorship_label(value, skip):
    assert bool(f.rule_sponsorship(cand(sponsorship=value))) is skip


SPONSORSHIP_SKIP_TEXT = [
    "No visa sponsorship is available for this role.", "This position does not offer visa sponsorship.",
    "We do not provide sponsorship.", "Sponsorship is not available.", "Candidates must not require sponsorship now or in the future.",
    "We cannot sponsor work visas.", "We are unable to sponsor visas at this time.", "Must be authorized to work in the US without sponsorship.",
    "US citizens only.", "Must be a U.S. citizen.", "No visa support provided.", "We will not sponsor employment visas.",
    "Applicants must possess work authorization which does not require sponsorship by the employer for a visa.",
    "Due to the nature of the work, U.S. citizenship is required.",
]
SPONSORSHIP_PASS_TEXT = [
    "Visa sponsorship is available.", "We sponsor H-1B visas.", "We will sponsor qualified candidates.", "Sponsorship available for the right candidate.",
    "We offer visa sponsorship.", "We are an equal opportunity employer.", "We hire regardless of citizenship status.", "",
    # application-form questions copied into postings: questions, not statements (and "now" is not "no")
    "Will you now or in the future require sponsorship for employment visa status (e.g., H-1B visa status)? Yes No",
    "Will you now or in the future require visa sponsorship to work in the United States? Yes No", "Do you require sponsorship? Yes No",
    "Starting now, we offer sponsorship for strong candidates.",
]


@pytest.mark.parametrize("text", SPONSORSHIP_SKIP_TEXT)
def test_sponsorship_text_skips(text):
    assert f.rule_sponsorship(cand(description=f"About the role. {text} Join us.")), text


@pytest.mark.parametrize("text", SPONSORSHIP_PASS_TEXT)
def test_sponsorship_text_passes(text):
    assert f.rule_sponsorship(cand(description=f"About the role. {text} Join us.")) is None, text


# ------------------------------------------------------------------ clearance / citizenship / ITAR

CLEARANCE_SKIP = [
    "Active Secret clearance required.", "TS/SCI eligibility is a must.", "Ability to obtain a security clearance.", "Must hold a DoD clearance.",
    "Public Trust required.", "Candidates must be a U.S. citizen.", "US citizenship is required.", "U.S. person status is required.",
    "Work is subject to ITAR.", "This is a regulated (ITAR/EAR) environment.", "Polygraph required.", "Minimum Clearance Required to Start: Secret.",
    "Must be a U.S. citizen, lawful permanent resident of the United States, or other protected individual.",
]
CLEARANCE_PASS = [
    "No security clearance required.", "Security clearance is a plus.", "Clearance is not required.", "We hire regardless of citizenship status.",
    "Must be authorized to work in the United States.", "",
    # boilerplate and form questions found in real postings
    "If we determine that export control restrictions would prohibit you from working here, we will let you know.",
    "Subject to export control regulations.", "Clearance Required: None.", "Security Clearance: No.", "Are you a U.S. person? Yes No",
    "Export Control Requirements - Are you a citizen, national, or resident of any of the following countries? Yes No",
]


def test_clearance_flag():
    assert f.rule_clearance_citizenship(cand(clearance_required=True))
    assert f.rule_clearance_citizenship(cand(clearance_required=False)) is None


@pytest.mark.parametrize("text", CLEARANCE_SKIP)
def test_clearance_text_skips(text):
    assert f.rule_clearance_citizenship(cand(description=f"Overview. {text} Thanks.")), text


@pytest.mark.parametrize("text", CLEARANCE_PASS)
def test_clearance_text_passes(text):
    assert f.rule_clearance_citizenship(cand(description=f"Overview. {text} Thanks.")) is None, text


# ------------------------------------------------------------------ unsupported ATS

@pytest.mark.parametrize("supported,skip", [(False, True), (True, False), (None, False)])
def test_unsupported_ats(supported, skip):
    assert bool(f.rule_unsupported_ats(cand(supported_ats=supported))) is skip


# ------------------------------------------------------------------ evaluate()

def good(**kw):
    base = dict(title="Software Engineer", company="Acme", url="https://boards.greenhouse.io/acme/jobs/1", locations=["New York, NY"],
                posted_at=hours_ago(5), years_min=2, sponsorship="UNKNOWN", supported_ats=True)
    base.update(kw)
    return cand(**base)


def test_a_good_candidate_passes_every_rule():
    verdict = f.evaluate(good(), NOW)
    assert verdict.passed and verdict.category is None and verdict.reason is None


@pytest.mark.parametrize("change,category", [
    ({"url": "https://www.linkedin.com/jobs/view/1"}, "blocked-domain"), ({"company": "Amazon"}, "no-apply-list"),
    ({"title": "Staff Software Engineer"}, "title"), ({"locations": ["London, UK"]}, "location"),
    ({"posted_at": hours_ago(100)}, "recency"), ({"supported_ats": False}, "unsupported-ats"),
    ({"years_min": 8}, "experience"), ({"sponsorship": "NO_SPONSORSHIP"}, "sponsorship"),
    ({"clearance_required": True}, "clearance"),
])
def test_each_rule_reports_its_own_category(change, category):
    verdict = f.evaluate(good(**change), NOW)
    assert not verdict.passed and verdict.category == category and verdict.reason


def test_first_failing_rule_wins_in_rule_order():
    both = good(company="Amazon", title="Staff Software Engineer")           # never-apply is checked before title
    assert f.evaluate(both, NOW).category == "no-apply-list"


def test_every_rule_category_is_one_the_summary_counts():
    assert {category for category, _ in f.RULES} <= set(f.CATEGORIES)


# ------------------------------------------------------------------ URLs and duplicates

@pytest.mark.parametrize("a,b,same", [
    ("https://boards.greenhouse.io/acme/jobs/123?gh_src=abc", "https://www.boards.greenhouse.io/acme/jobs/123/", True),
    ("https://acme.com/careers/1?utm_source=x&utm_medium=y#apply", "https://acme.com/careers/1", True),
    ("https://acme.com/job?b=2&a=1", "https://acme.com/job?a=1&b=2", True),
    ("HTTPS://ACME.com/Job/1", "https://acme.com/Job/1", True),
    ("https://acme.com/careers?gh_jid=1", "https://acme.com/careers?gh_jid=2", False),
    ("https://acme.com/jobs/1", "https://acme.com/jobs/2", False),
])
def test_normalize_url(a, b, same):
    assert (f.normalize_url(a) == f.normalize_url(b)) is same


def test_duplicate_same_url_after_tracking_params_removed():
    dup = DuplicateFilter()
    first = good(url="https://boards.greenhouse.io/acme/jobs/1?gh_src=abc")
    assert dup.check(first) is None
    dup.remember(first)
    assert "same job URL" in dup.check(good(url="https://boards.greenhouse.io/acme/jobs/1"))


def test_same_role_in_another_city_is_a_different_role():
    dup = DuplicateFilter()
    dup.remember(good(url="https://a.com/1", locations=["New York, NY"]))
    assert dup.check(good(url="https://a.com/2", locations=["Seattle, WA"])) is None


def test_same_company_title_and_location_from_another_source_is_a_duplicate():
    dup = DuplicateFilter()
    dup.remember(good(url="https://a.com/1", locations=["New York, NY"], company="ACME Inc"))
    reason = dup.check(good(url="https://other.com/9", locations=["New York, NY"], company="Acme, Inc."))
    assert reason and "cross-post" in reason


def test_missing_location_is_not_treated_as_a_duplicate():
    dup = DuplicateFilter()
    dup.remember(good(url="https://a.com/1", locations=["New York, NY"]))
    assert dup.check(good(url="https://a.com/2", locations=[])) is None


def test_seeded_history_blocks_repeats():
    dup = DuplicateFilter(seen_urls=["https://a.com/1?utm_source=x"], seen_roles=[("Acme", "Software Engineer", ["New York, NY"])])
    assert dup.check(good(url="https://a.com/1"))
    assert dup.check(good(url="https://b.com/2"))            # same role via history
    assert dup.check(good(url="https://b.com/3", locations=["Boston, MA"])) is None
