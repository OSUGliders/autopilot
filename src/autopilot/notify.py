"""Pilot email notifications for FALLBACK state changes.

Emails on FALLBACK entry, a reminder while it persists, and recovery.
Send failures are logged, never raised — notification must not break
piloting.
"""

import logging
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage

logger = logging.getLogger("sfmc.notify")


class Notifier:
    """Track NORMAL/FALLBACK transitions for one glider and email pilots.

    Config keys: ``from``, ``to`` (list), ``smtp_host`` (localhost),
    ``smtp_port`` (25), ``reminder_h`` (6).
    """

    def __init__(self, config: dict):
        self.sender = config["from"]
        self.recipients = list(config["to"])
        self.host = config.get("smtp_host", "localhost")
        self.port = int(config.get("smtp_port", 25))
        self.reminder = timedelta(hours=float(config.get("reminder_h", 6.0)))
        self._in_fallback = False
        self._last_sent: datetime | None = None

    def update(
        self,
        now: datetime,
        glider: str,
        ok: bool,
        reason: str = "",
        detail: str = "",
        position: tuple[float, float] | None = None,  # (lat, lon)
    ) -> None:
        """Report the outcome of one surfacing; emails on transitions."""
        if ok:
            if not self._in_fallback:
                return
            subject = f"{glider} autopilot recovered"
            body = f"{glider} is tracking normally again at {now:%Y-%m-%d %H:%M} UTC."
        elif not self._in_fallback:
            subject = f"{glider} autopilot FALLBACK ({reason})"
            body = (
                f"{glider} could not get a safe waypoint at "
                f"{now:%Y-%m-%d %H:%M} UTC and was sent to the safe point.\n"
                f"Reason: {reason}: {detail}\n"
                "It will retry at every surfacing; pilot attention required."
            )
        elif self._last_sent is not None and now - self._last_sent < self.reminder:
            return  # still in fallback, reminder not yet due
        else:
            subject = f"{glider} autopilot still in FALLBACK ({reason})"
            body = (
                f"{glider} is still holding at the safe point as of "
                f"{now:%Y-%m-%d %H:%M} UTC.\nReason: {reason}: {detail}"
            )
        if position is not None:
            body += f"\nGlider position: {position[0]:.4f}, {position[1]:.4f}"

        self._in_fallback = not ok
        if self._send(subject, body):
            self._last_sent = now if not ok else None

    def _send(self, subject: str, body: str) -> bool:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.recipients)
        msg.set_content(body)
        try:
            with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
                smtp.send_message(msg)
            logger.info("Notified %s: %s", msg["To"], subject)
            return True
        except Exception:
            logger.exception("Failed to send notification: %s", subject)
            return False
