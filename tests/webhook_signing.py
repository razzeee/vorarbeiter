import hmac
import json
import uuid
from typing import TypedDict

from app.config import settings


def webhook_signature(body: bytes) -> str:
    digest = hmac.digest(settings.github_webhook_secret.encode(), body, "sha256")
    return f"sha256={digest.hex()}"


class SignedWebhook(TypedDict):
    content: bytes
    headers: dict[str, str]


def signed_webhook(
    payload: dict | bytes, delivery_id: str | None = None
) -> SignedWebhook:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return {
        "content": body,
        "headers": {
            "X-GitHub-Delivery": delivery_id
            if delivery_id is not None
            else str(uuid.uuid4()),
            "Content-Type": "application/json",
            "X-Hub-Signature-256": webhook_signature(body),
        },
    }
