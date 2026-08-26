"""Maps a job's application URL to the ATS/job board actually hosting it
(Greenhouse, Workday, etc.) - distinct from `Job.source`, which is which of
*our* tabs (jobright/simplify) the listing was pulled from. Domain-based, so
it's naturally "Unknown" for company-run career sites that don't use a
recognized third-party ATS.
"""

from urllib.parse import urlparse

# (domain, label) - matched against the URL's hostname or any subdomain of it.
JOB_BOARD_DOMAINS = [
    ("greenhouse.io", "Greenhouse"),
    ("myworkdayjobs.com", "Workday"),
    ("oraclecloud.com", "Oracle Cloud"),
    ("icims.com", "iCIMS"),
    ("ashbyhq.com", "Ashby"),
    ("lever.co", "Lever"),
    ("smartrecruiters.com", "SmartRecruiters"),
    ("taleo.net", "Taleo"),
    ("successfactors.com", "SuccessFactors"),
    ("jobvite.com", "Jobvite"),
    ("bamboohr.com", "BambooHR"),
    ("breezy.hr", "Breezy HR"),
    ("recruitee.com", "Recruitee"),
    ("workable.com", "Workable"),
    ("jazzhr.com", "JazzHR"),
    ("ultipro.com", "UKG/UltiPro"),
    ("phenompeople.com", "Phenom"),
    ("linkedin.com", "LinkedIn"),
    ("indeed.com", "Indeed"),
]


def job_board_name(url):
    if not url:
        return "Unknown"
    host = (urlparse(url).netloc or "").lower()
    if not host:
        return "Unknown"
    for domain, label in JOB_BOARD_DOMAINS:
        if host == domain or host.endswith("." + domain):
            return label
    return "Unknown"
