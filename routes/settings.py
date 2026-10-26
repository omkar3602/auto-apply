from flask import Blueprint, jsonify, request

from automation import user_settings
from scrapers import SOURCES

settings_bp = Blueprint("settings", __name__, url_prefix="/settings")


@settings_bp.get("")
def get_settings():
    return jsonify(user_settings.load())


@settings_bp.post("")
def update_settings():
    body = request.get_json(force=True, silent=True) or {}
    update = {}

    if "keyboard_mode" in body:
        update["keyboard_mode"] = bool(body["keyboard_mode"])

    if "swipe_sources" in body:
        raw = body["swipe_sources"]
        # None = no filter, as opposed to a list (even an empty one) -
        # silently dropping anything that isn't a known source key rather
        # than erroring, so a stale/renamed source in an old request body
        # never breaks the whole update.
        update["swipe_sources"] = (
            None if raw is None else [s for s in raw if s in SOURCES]
        )

    return jsonify(user_settings.save(update))
