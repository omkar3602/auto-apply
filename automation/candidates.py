"""Turns each source's job into a filters.Candidate.

Duck-typed on purpose (no Flask, no models import): a `Job` row only needs
`.source .title .company .location .job_url .description .posted_label
.raw_json .id`, and Tsenta's feed rows are plain dicts, so this is easy to test.

What each source can and can't tell the rules (2026-11-25):

| field        | Tsenta feed        | Simplify (raw_json)     | JobRight (raw_json)        |
|--------------|--------------------|-------------------------|----------------------------|
| posted time  | exact `datePosted` | exact `date_posted`     | label, dated from when read |
| experience   | `yearsOfExperienceMin` | none -> description | `years_exp` text           |
| sponsorship  | enum               | always "Other" (= unknown) | none -> description     |
| website      | `companyWebsite`   | `company_url`           | none (name + URL only)     |
| supported?   | `isSupportedAts`   | -                       | -                          |
| description  | none (get-jobs-by-ids) | fetched, may be empty | composed, may be empty   |
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from automation.filters import Candidate, label_age, normalize_sponsorship, parse_min_years


def parse_iso(value):
    """ISO 8601 ("2026-11-19T19:49:18.000Z") -> aware UTC datetime, or None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def from_unix(value):
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc) if value else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _raw(job):
    try:
        data = json.loads(job.raw_json or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def from_tsenta(row, description=None):
    """One `get-job-recommendations` row (optionally plus a description from
    `get-jobs-by-ids`)."""
    return Candidate(
        source="tsenta",
        title=row.get("title") or "",
        company=row.get("companyName"),
        company_website=row.get("companyWebsite"),
        url=row.get("url") or "",
        locations=list(row.get("locations") or []),
        posted_at=parse_iso(row.get("datePosted")),
        years_min=row.get("yearsOfExperienceMin"),
        sponsorship=normalize_sponsorship(row.get("sponsorship")),
        clearance_required=row.get("clearanceRequired"),
        supported_ats=row.get("isSupportedAts"),
        description=description or row.get("description"),
        match_score=row.get("matchScore"),
        ref=row.get("id"),
    )


def from_simplify(job):
    raw = _raw(job)
    locations = raw.get("locations") or ([job.location] if job.location else [])
    return Candidate(
        source="simplify",
        title=job.title or "",
        company=job.company,
        company_website=raw.get("company_url"),
        url=job.job_url or "",
        locations=list(locations),
        posted_at=from_unix(raw.get("date_posted")),
        posted_label=job.posted_label,
        sponsorship=normalize_sponsorship(raw.get("sponsorship")),
        description=job.description,
        ref=job.id,
    )


def _jobright_posted_at(job, raw):
    """JobRight only says "2 hours ago", which is true when the card was read and then
    stays frozen in our row. Pin it to a time: when it was read, minus that age. None
    for labels that can't be dated (reposts included), which the recency rule then skips."""
    age = label_age(job.posted_label)[0]
    read_at = parse_iso(raw.get("label_read_at")) or getattr(job, "created_at", None)  # naive = UTC
    if age is None or read_at is None:
        return None
    if read_at.tzinfo is None:
        read_at = read_at.replace(tzinfo=timezone.utc)
    return read_at - age


def from_jobright(job):
    raw = _raw(job)
    return Candidate(
        source="jobright",
        title=job.title or "",
        company=job.company,
        url=job.job_url or "",
        locations=[job.location] if job.location else [],
        posted_at=_jobright_posted_at(job, raw),
        posted_label=job.posted_label,
        years_min=parse_min_years(raw.get("years_exp"), lenient=True),
        description=job.description,
        ref=job.id,
    )


_FROM_JOB = {"simplify": from_simplify, "jobright": from_jobright}


def from_job(job):
    """A stored `Job` row -> Candidate. Sources without a special reader get the
    plain fields only."""
    reader = _FROM_JOB.get(job.source)
    if reader:
        return reader(job)
    return Candidate(
        source=job.source, title=job.title or "", company=job.company, url=job.job_url or "",
        locations=[job.location] if job.location else [], posted_label=job.posted_label,
        description=job.description, ref=job.id,
    )
