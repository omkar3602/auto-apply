from scrapers import jobright
from scrapers import simplify
from scrapers import dice
from scrapers import zapplyjobs
from scrapers import speedyapply

SOURCES = {
    "jobright": {
        "label": "JobRight",
        "sync": jobright.sync_jobs,
    },
    "simplify": {
        "label": "Simplify New Grad",
        "sync": simplify.sync_jobs,
    },
    "dice": {
        "label": "Dice",
        "sync": dice.sync_jobs,
    },
    "zapplyjobs": {
        "label": "Zapply New Grad",
        "sync": zapplyjobs.sync_jobs,
    },
    "speedyapply": {
        "label": "SpeedyApply AI/College",
        "sync": speedyapply.sync_jobs,
    },
}
