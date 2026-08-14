"""Minimal Slack Incoming Webhook delivery — one JSON POST, no SDK.

Deliberately stdlib-only (``urllib``): this is a single POST with no
retries/streaming/auth complexity, not enough to justify a new HTTP
dependency alongside the ones autopilot already has.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable

#: (subject, body) -> deliver it. Same shape as sfmc_api's SendFn.
SendFn = Callable[[str, str], None]


def make_slack_send(webhook_url: str, timeout: float = 10.0) -> SendFn:
    """Return a ``send(subject, body)`` that posts one Slack message.

    Incoming Webhooks take a single ``text`` field — there is no
    separate subject line, so the two are combined with the subject
    bolded, matching Slack's own ``*bold*`` mrkdwn convention.
    """

    def send(subject: str, body: str) -> None:
        payload = json.dumps({"text": f"*{subject}*\n{body}"}).encode()
        request = urllib.request.Request(
            webhook_url, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise RuntimeError(f"Slack webhook returned HTTP {response.status}")

    return send
