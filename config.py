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

# Dice's own official MCP server - replaced their traditional developer API
# (shut down in 2017). Public, no sign-in required.
DICE_MCP_URL = "https://mcp.dice.com/mcp"
DICE_SEARCH_KEYWORDS = [
    "Software Engineer",
    "Machine Learning Engineer",
    "Data Scientist",
    "AI Engineer",
    "Data Engineer",
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
