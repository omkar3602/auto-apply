from flask import Blueprint, jsonify, request

from automation import push
from db import db
from models import PushSubscription

push_bp = Blueprint("push", __name__, url_prefix="/push")


@push_bp.get("/vapid-public-key")
def vapid_public_key():
    return jsonify({"key": push.vapid_public_key_b64url()})


@push_bp.post("/subscribe")
def subscribe():
    body = request.get_json(silent=True) or {}
    endpoint = body.get("endpoint")
    keys = body.get("keys") or {}
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    if not endpoint or not p256dh or not auth:
        return jsonify({"error": "incomplete subscription"}), 400

    sub = PushSubscription.query.filter_by(endpoint=endpoint).first()
    if sub is None:
        sub = PushSubscription(endpoint=endpoint)
        db.session.add(sub)
    sub.p256dh = p256dh
    sub.auth = auth
    db.session.commit()
    return jsonify({"ok": True})


@push_bp.post("/unsubscribe")
def unsubscribe():
    body = request.get_json(silent=True) or {}
    endpoint = body.get("endpoint")
    if endpoint:
        PushSubscription.query.filter_by(endpoint=endpoint).delete()
        db.session.commit()
    return jsonify({"ok": True})
