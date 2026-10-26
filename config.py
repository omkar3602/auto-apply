import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
BROWSER_PROFILES_DIR = os.path.join(BASE_DIR, "browser_profiles")

os.makedirs(INSTANCE_DIR, exist_ok=True)
os.makedirs(BROWSER_PROFILES_DIR, exist_ok=True)


JOBRIGHT_FEED_URL = "https://jobright.ai/jobs/recommend"
JOBRIGHT_PROFILE_DIR = os.path.join(BROWSER_PROFILES_DIR, "jobright")

TSENTA_DASHBOARD_URL = "https://dashboard.tsenta.com/dashboard/recommendations"
TSENTA_PROFILE_DIR = os.path.join(BROWSER_PROFILES_DIR, "tsenta")

SIMPLIFY_LISTINGS_URL = "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json"

# Shared target-role keyword list: a job's title must contain one of these
# phrases (case-insensitive) to count as relevant. Used by
# scrapers/common.title_is_relevant (scrape-time filtering) and
# automation/auto_apply_schedule.py (the scheduler's own selection filter).
# Originally scoped to Dice's own search (removed 2026-10-26 - its real
# apply success rate never recovered from 0/154, even after building and
# fixing a logged-in redirect resolver: only ~1 in 6 postings had an
# external apply link at all, and the one we found didn't work with tsenta
# either), kept as the shared list since zapplyjobs still filters on it.
RELEVANT_TITLE_KEYWORDS = [
    "Software Engineer",
    "Machine Learning Engineer",
    "Data Scientist",
    "AI Engineer",
    "Data Engineer",
    # Added 2026-10-26, evidence-based: pulled every title currently
    # excluded across all active sources (1,731 distinct) and checked
    # real samples for each phrase below before adding it, to avoid
    # guessing. "Research Scientist" and bare "Data Analyst" were
    # considered and left out - real samples showed the former also
    # matches non-tech roles (economics, immunochemistry research), and
    # the latter is a meaningfully different role category, not just a
    # phrasing variant of the roles above.
    "Software Development Engineer",
    "Full Stack",
    "Backend",
    "Back End",
    "Developer",
    "Machine Learning Research Scientist",
    "AI Platform Engineer",
    "AI Product Engineer",
    "AI Prompt Engineer",
    "AI Research Engineer",
    "AI Security Engineer",
    "AI Solutions Engineer",
]

# zapplyjobs and speedyapply publish early-career listings as Markdown
# tables (not JSON like SimplifyJobs) on GitHub, refreshed frequently.
ZAPPLYJOBS_README_URL = "https://raw.githubusercontent.com/zapplyjobs/New-Grad-Jobs-2027/main/README.md"
SPEEDYAPPLY_USA_URL = "https://raw.githubusercontent.com/speedyapply/2027-AI-College-Jobs/main/NEW_GRAD_USA.md"


class Config:
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{os.path.join(INSTANCE_DIR, 'jobs.db')}"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # SQLite allows a single writer at a time, and the sync workers touch the
    # DB either side of slow browser work, so a write transaction can be held
    # for a good while. Wait that out instead of giving up after sqlite3's 5s
    # default - the apply worker losing this race is what stalled the queue.
    # Pairs with the WAL pragma in db.py.
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"timeout": 30}}
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-secret")
