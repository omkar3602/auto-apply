import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
BROWSER_PROFILES_DIR = os.path.join(BASE_DIR, "browser_profiles")

os.makedirs(INSTANCE_DIR, exist_ok=True)
os.makedirs(BROWSER_PROFILES_DIR, exist_ok=True)


JOBRIGHT_FEED_URL = "https://jobright.ai/jobs/recommend"
JOBRIGHT_PROFILE_DIR = os.path.join(BROWSER_PROFILES_DIR, "jobright")

TSENTA_DASHBOARD_URL = "https://dashboard.tsenta.com/dashboard"
TSENTA_PROFILE_DIR = os.path.join(BROWSER_PROFILES_DIR, "tsenta")

SIMPLIFY_LISTINGS_URL = "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json"


class Config:
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{os.path.join(INSTANCE_DIR, 'jobs.db')}"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-secret")
