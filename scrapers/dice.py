"""Scraper for Dice's official MCP server (https://mcp.dice.com/mcp).

Dice shut down its traditional developer API in 2017; this MCP server -
published by Dice itself, no sign-in required - is the current machine-
access replacement: a standard tool-calling interface
(automation/mcp_client.py), not scraped HTML. One `search_jobs` call per
configured keyword (config.DICE_SEARCH_KEYWORDS), newest first, limited to
postings from the last 3 days via the tool's own `posted_date` filter.

Dice's tool description requires an AI-search disclosure whenever results
are shown to users - DISCLOSURE below is prefixed onto each job's stored
description to satisfy that.
"""

import json
from datetime import datetime, timezone

import config as cfg
from db import db
from automation.mcp_client import call_tool_json
from scrapers import common

DISCLOSURE = (
    "These results were found using AI-powered search. Please verify "
    "details directly with the employer before applying.\n\n"
)
JOBS_PER_PAGE = 50


def sync_jobs(app):
    """Searches Dice for each configured keyword and upserts active
    listings. Returns a summary dict. Must be called inside an app context.
    """
    created, updated, skipped = 0, 0, 0
    seen_guids = set()

    for keyword in cfg.DICE_SEARCH_KEYWORDS:
        result = call_tool_json(
            cfg.DICE_MCP_URL,
            "search_jobs",
            {
                "keyword": keyword,
                "jobs_per_page": JOBS_PER_PAGE,
                "sort": "datePosted",
                "posted_date": "THREE",
            },
        )
        for entry in result.get("data", []):
            guid = entry.get("guid")
            if not guid or guid in seen_guids:
                # The same job can legitimately turn up under more than one
                # keyword search - only process it once per sync.
                continue
            seen_guids.add(guid)

            data = _parse_entry(entry)
            if not data:
                skipped += 1
                continue

            is_new = common.upsert_job("dice", data)
            if is_new:
                created += 1
            else:
                updated += 1

    db.session.commit()
    return {"created": created, "updated": updated, "skipped": skipped}


def _parse_entry(entry):
    guid = entry.get("guid")
    title = entry.get("title")
    url = entry.get("detailsPageUrl")
    if not guid or not title or not url:
        return None

    location = (entry.get("jobLocation") or {}).get("displayName")

    workplace_types = entry.get("workplaceTypes") or []
    work_mode = workplace_types[0] if workplace_types else ("Remote" if entry.get("isRemote") else None)

    summary = (entry.get("summary") or "").strip()
    description = DISCLOSURE + summary if summary else None

    tags = []
    if entry.get("employmentType"):
        tags.append(entry["employmentType"])
    if entry.get("willingToSponsor"):
        tags.append("Sponsorship")

    return {
        "external_id": guid,
        "title": title,
        "company": entry.get("companyName"),
        "location": location,
        "work_mode": work_mode,
        "posted_label": _relative_time(entry.get("postedDate")),
        "tags": ", ".join(tags),
        "job_url": url,
        "description": description,
        "raw_json": json.dumps(entry),
    }


def _relative_time(iso_str):
    if not iso_str:
        return None
    try:
        posted = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except ValueError:
        return None
    seconds_old = (datetime.now(timezone.utc) - posted).total_seconds()
    return common.relative_time_label(seconds_old)
