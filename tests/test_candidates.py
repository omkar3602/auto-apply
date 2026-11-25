"""Source -> Candidate adapters, and the rules run on real-shaped data."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from automation import candidates as cands
from automation import filters as f

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)

# Rows copied from a real get-job-recommendations response (2026-11-25), trimmed to the fields we read.
REAL_FEED = {
    "amazon_de": {
        "id": "a19257ed-c8ad-4887-8848-c03cadb585c5", "title": "Data Engineer II, Global Logistics, Exports", "companyName": "Amazon",
        "companyWebsite": "amazon.com", "url": "https://www.amazon.jobs/en/jobs/10609538", "locations": ["Bellevue, WA"], "matchScore": 84,
        "datePosted": "2026-11-19T19:49:18.000Z", "isSupportedAts": True, "yearsOfExperienceMin": 3, "sponsorship": "UNKNOWN", "clearanceRequired": False,
    },
    "chase_swe": {
        "id": "ce8e194d-e762-4266-81d3-cc28eab81819", "title": "Software Engineer III - Databricks/Snowflake/AWS", "companyName": "Chase", "companyWebsite": "chase.com",
        "url": "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1002/job/210784266", "locations": ["Plano, TX"], "matchScore": 84,
        "datePosted": "2026-11-18T20:41:14.000Z", "isSupportedAts": True, "yearsOfExperienceMin": 3, "sponsorship": "UNKNOWN", "clearanceRequired": False,
    },
    "amazon_sde": {
        "id": "b60b115e-3019-4ff5-bd17-dda2b4dc359a", "title": "Software Development Engineer, AWS Marketplace & Partner Services", "companyName": "Amazon",
        "companyWebsite": "amazon.com", "url": "https://www.amazon.jobs/en/jobs/10599075", "locations": ["Seattle, WA"], "matchScore": 84,
        "datePosted": "2026-11-18T22:27:52.000Z", "isSupportedAts": True, "yearsOfExperienceMin": 3, "sponsorship": "UNKNOWN", "clearanceRequired": False,
        "siblings": [{"id": "e5dc618f", "url": "https://www.amazon.jobs/en/jobs/10599076", "locations": ["Seattle, WA"]}],
    },
    "truist_de": {
        "id": "6cff4799-e2ae-4783-8f4c-bf88cfa760e5", "title": "Data Engineer", "companyName": "Truist", "companyWebsite": "truist.com",
        "url": "https://truist.wd1.myworkdayjobs.com/careers/job/charlotte-nc/data-engineer_r0122029", "locations": ["Charlotte, NC"], "matchScore": 84,
        "datePosted": "2026-11-19T14:11:20.000Z", "isSupportedAts": True, "yearsOfExperienceMin": 7, "sponsorship": "UNKNOWN", "clearanceRequired": False,
    },
}


def test_from_tsenta_maps_every_field_the_rules_use():
    c = cands.from_tsenta(REAL_FEED["truist_de"], description="Great role.")
    assert (c.source, c.title, c.company, c.company_website) == ("tsenta", "Data Engineer", "Truist", "truist.com")
    assert c.locations == ["Charlotte, NC"] and c.years_min == 7 and c.sponsorship == "UNKNOWN"
    assert c.supported_ats is True and c.clearance_required is False and c.match_score == 84 and c.description == "Great role."
    assert c.posted_at == datetime(2026, 11, 19, 14, 11, 20, tzinfo=timezone.utc)
    assert c.ref == "6cff4799-e2ae-4783-8f4c-bf88cfa760e5"


@pytest.mark.parametrize("key,category", [
    ("amazon_de", "no-apply-list"), ("chase_swe", "no-apply-list"), ("amazon_sde", "no-apply-list"), ("truist_de", "experience"),
])
def test_real_feed_rows_are_skipped_for_the_right_reason(key, category):
    verdict = f.evaluate(cands.from_tsenta(REAL_FEED[key]), NOW)
    assert not verdict.passed and verdict.category == category, verdict


def test_a_good_feed_row_passes():
    row = {**REAL_FEED["truist_de"], "companyName": "Eight Sleep", "companyWebsite": "eightsleep.com", "yearsOfExperienceMin": None,
           "url": "https://jobs.ashbyhq.com/eightsleep/abc", "locations": ["New York City"], "datePosted": "2026-11-20T02:00:00.000Z",
           "title": "New Grad - Engineering"}
    row["title"] = "Software Engineer, New Grad"
    assert f.evaluate(cands.from_tsenta(row), NOW).passed


def test_feed_row_that_tsenta_cannot_apply_to_is_skipped():
    row = {**REAL_FEED["truist_de"], "companyName": "Acme", "companyWebsite": "acme.com", "yearsOfExperienceMin": 2, "isSupportedAts": False}
    assert f.evaluate(cands.from_tsenta(row), NOW).category == "unsupported-ats"


def test_feed_description_can_still_reject_it():
    row = {**REAL_FEED["truist_de"], "companyName": "Acme", "companyWebsite": "acme.com", "yearsOfExperienceMin": None}
    assert f.evaluate(cands.from_tsenta(row, description="Candidates must be a U.S. citizen."), NOW).category == "sponsorship"
    assert f.evaluate(cands.from_tsenta(row, description="Requires an active Secret clearance."), NOW).category == "clearance"


# ------------------------------------------------------------------ Simplify / JobRight Job rows

def job(**kw):
    base = dict(id=1, source="simplify", title="Software Engineer New Grad", company="Acme", location="New York, NY", job_url="https://boards.greenhouse.io/acme/jobs/1",
                description=None, posted_label=None, raw_json="{}")
    base.update(kw)
    return SimpleNamespace(**base)


def simplify_job(**raw):
    entry = {"id": "x", "date_posted": int(datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc).timestamp()), "company_url": "https://acme.com",
             "sponsorship": "Other", "locations": ["New York, NY", "Boston, MA"], **raw}
    return job(raw_json=json.dumps(entry))


def test_from_simplify_reads_the_exact_time_website_and_locations():
    c = cands.from_job(simplify_job())
    assert c.source == "simplify" and c.company_website == "https://acme.com" and c.locations == ["New York, NY", "Boston, MA"]
    assert c.posted_at == datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc)
    assert c.sponsorship == "UNKNOWN"                       # Simplify's "Other" carries no signal
    assert f.evaluate(c, NOW).passed


def test_simplify_sponsorship_labels_map_and_skip():
    c = cands.from_job(simplify_job(sponsorship="Does Not Offer Sponsorship"))
    assert c.sponsorship == "NO_SPONSORSHIP" and f.evaluate(c, NOW).category == "sponsorship"
    assert cands.from_job(simplify_job(sponsorship="Offers Sponsorship")).sponsorship == "SPONSORS"


def test_simplify_old_posting_is_skipped_on_its_exact_time():
    old = simplify_job(date_posted=int(datetime(2026, 11, 10, tzinfo=timezone.utc).timestamp()))
    assert f.evaluate(cands.from_job(old), NOW).category == "recency"


def test_simplify_falls_back_to_the_location_column_when_the_list_is_missing():
    j = job(raw_json=json.dumps({"date_posted": 1_700_000_000}), location="Austin, TX")
    assert cands.from_job(j).locations == ["Austin, TX"]


@pytest.mark.parametrize("years_exp,expected", [("0–2 years exp", 0), ("1+ years exp", 1), ("3–5 years exp", 3), ("Start in 2027 Spring", None), (None, None)])
def test_from_jobright_reads_the_experience_text(years_exp, expected):
    j = job(source="jobright", posted_label="2 hours ago", raw_json=json.dumps({"years_exp": years_exp}))
    assert cands.from_job(j).years_min == expected


def test_jobright_uses_the_label_and_skips_reposts_and_old_days():
    ok = cands.from_job(job(source="jobright", posted_label="2 hours ago", raw_json="{}"))
    assert ok.posted_at is None and f.evaluate(ok, NOW).passed
    assert f.evaluate(cands.from_job(job(source="jobright", posted_label="Reposted 1 hour ago")), NOW).category == "recency"
    assert f.evaluate(cands.from_job(job(source="jobright", posted_label="3 days ago")), NOW).category == "recency"
    assert f.evaluate(cands.from_job(job(source="jobright", posted_label="2 days ago")), NOW).passed


def test_jobright_over_five_years_is_skipped():
    j = job(source="jobright", posted_label="1 hour ago", raw_json=json.dumps({"years_exp": "6+ years exp"}))
    assert f.evaluate(cands.from_job(j), NOW).category == "experience"


def test_jobright_has_no_website_so_the_name_and_url_still_catch_the_never_apply_list():
    j = job(source="jobright", company="Amazon Robotics", posted_label="1 hour ago")
    assert f.evaluate(cands.from_job(j), NOW).category == "no-apply-list"
    j = job(source="jobright", company="Acme", posted_label="1 hour ago", job_url="https://jpmc.fa.oraclecloud.com/x/1")
    assert f.evaluate(cands.from_job(j), NOW).category == "no-apply-list"


def test_other_sources_get_the_plain_fields():
    c = cands.from_job(job(source="zapplyjobs", posted_label="3m"))
    assert c.source == "zapplyjobs" and c.title and c.locations == ["New York, NY"]


# ------------------------------------------------------------------ small parsers

@pytest.mark.parametrize("value,expected", [
    ("2026-11-19T19:49:18.000Z", datetime(2026, 11, 19, 19, 49, 18, tzinfo=timezone.utc)),
    ("2026-11-19T19:49:18+00:00", datetime(2026, 11, 19, 19, 49, 18, tzinfo=timezone.utc)),
    ("2026-11-19T15:49:18-04:00", datetime(2026, 11, 19, 19, 49, 18, tzinfo=timezone.utc)),
    ("2026-11-19T19:49:18", datetime(2026, 11, 19, 19, 49, 18, tzinfo=timezone.utc)),
    ("garbage", None), ("", None), (None, None),
])
def test_parse_iso(value, expected):
    assert cands.parse_iso(value) == expected


def test_from_unix_and_bad_json_are_tolerated():
    assert cands.from_unix(0) is None and cands.from_unix("nope") is None
    assert cands.from_unix(1_700_000_000).tzinfo is not None
    assert cands.from_job(job(raw_json="not json")).title == "Software Engineer New Grad"
    assert cands.from_job(job(raw_json="[1, 2]")).title == "Software Engineer New Grad"
