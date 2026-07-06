"""Send one test FALLBACK notification to verify email delivery.

Usage:
    uv run python examples/send_test_email.py you@example.edu [smtp_host] [port]
"""

import sys
from datetime import UTC, datetime

from autopilot.notify import Notifier

address = sys.argv[1]
host = sys.argv[2] if len(sys.argv) > 2 else "smtp.oregonstate.edu"
port = int(sys.argv[3]) if len(sys.argv) > 3 else 25

n = Notifier({"from": address, "to": [address], "smtp_host": host, "smtp_port": port})
n.update(
    datetime.now(UTC),
    "osu999",
    False,
    "TEST",
    "manual notifier check",
    (33.13, -117.70),
)
