from scrapers import jobright
from scrapers import simplify

SOURCES = {
    "jobright": {
        "label": "JobRight",
        "sync": jobright.sync_jobs,
    },
    "simplify": {
        "label": "Simplify New Grad",
        "sync": simplify.sync_jobs,
    },
}
