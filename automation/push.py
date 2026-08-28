"""Web Push: generates/persists a VAPID keypair on first run, and sends the
daily "time to apply" reminder to every subscribed device.

VAPID (RFC 8292) is how this server proves to Apple/Google/Mozilla's push
services that a push for a given subscription comes from the same server
the browser subscribed with - no third-party push-service account (an
FCM/APNs project) needed, just this keypair. The private key lives under
instance/ (gitignored), generated automatically the first time it's needed.
"""

import json
import logging
import os

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from py_vapid.utils import b64urlencode
from pywebpush import WebPushException, webpush

import config as cfg
from db import db
from models import PushSubscription

VAPID_KEY_PATH = os.path.join(cfg.INSTANCE_DIR, "vapid_private.pem")
# Contact the push services can show if they ever need to reach the sender.
# Not the app owner's real address by default - set VAPID_CONTACT_EMAIL if
# a real one is ever needed; personal-use push delivery doesn't require it.
VAPID_CLAIMS = {"sub": os.environ.get("VAPID_CONTACT_EMAIL", "mailto:auto-apply@localhost")}

log = logging.getLogger(__name__)

_vapid = None


def _get_vapid():
    global _vapid
    if _vapid is None:
        # Vapid.from_file generates + saves a fresh keypair the first time
        # this path doesn't exist yet - nothing to set up by hand.
        _vapid = Vapid.from_file(private_key_file=VAPID_KEY_PATH)
    return _vapid


def vapid_public_key_b64url():
    """The raw uncompressed EC point, base64url-encoded - the exact format
    PushManager.subscribe()'s applicationServerKey argument wants."""
    raw = _get_vapid().public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return b64urlencode(raw)


def send_to_all(title, body, url="/"):
    """Best-effort push to every stored subscription. Prunes subscriptions
    the push service reports as gone (404/410 - app uninstalled, subscription
    expired) so the table doesn't accumulate dead endpoints.

    Returns (sent, failed, pruned) counts.
    """
    vapid = _get_vapid()
    payload = json.dumps({"title": title, "body": body, "url": url})
    sent, failed, pruned = 0, 0, 0

    for sub in PushSubscription.query.all():
        subscription_info = {
            "endpoint": sub.endpoint,
            "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
        }
        try:
            webpush(
                subscription_info=subscription_info,
                data=payload,
                vapid_private_key=vapid,
                vapid_claims=dict(VAPID_CLAIMS),
                ttl=3600,
            )
            sent += 1
        except WebPushException as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status in (404, 410):
                db.session.delete(sub)
                pruned += 1
            else:
                failed += 1
                body = exc.response.text if exc.response is not None else None
                log.warning("[push id=%s] failed (status=%s): %s - %s", sub.id, status, exc, body)
        except Exception as exc:
            # A DNS/connection failure for one endpoint (offline push
            # service, flaky network) raises outside WebPushException -
            # never let it stop the rest of the batch from being sent.
            failed += 1
            log.warning("[push id=%s] failed: %r", sub.id, exc)

    if pruned:
        db.session.commit()
    return sent, failed, pruned
