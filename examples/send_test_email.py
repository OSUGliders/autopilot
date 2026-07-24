"""Send one test email over the same SMTP path sfmc-follow uses.

Delivers synchronously (unlike the background-queued sender the real
notifier uses) so a delivery failure raises and prints a traceback
immediately, rather than being logged and retried out of sight. The
sender defaults to the recipient: a mailbox that does not exist (the
default when a service omits --notify-from) is the most common cause
of alerts that never arrive despite --notify-email being set.

Usage:
    uv run python examples/send_test_email.py you@example.edu
    uv run python examples/send_test_email.py you@example.edu \
        --from glider-autopilot@oregonstate.edu --host localhost --port 25
"""

import argparse

from sfmc_api.disconnect_notify import make_smtp_send


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("to", help="recipient address")
    ap.add_argument(
        "--from",
        dest="sender",
        default=None,
        help="From address (default: same as 'to')",
    )
    ap.add_argument("--host", default="localhost", help="SMTP relay (default: %(default)s)")
    ap.add_argument("--port", type=int, default=25, help="default: %(default)s")
    args = ap.parse_args()

    sender = args.sender or args.to
    send = make_smtp_send(
        host=args.host,
        port=args.port,
        sender=sender,
        recipients=[args.to],
        timeout=10.0,
        program="sfmc-follow-test",
    )
    send(
        "[SFMC] TEST notification",
        "Manual check of the SMTP path used by sfmc-follow's "
        "--notify-email alerts (disconnect and FALLBACK notices).\n\n"
        f"From: {sender}\nTo: {args.to}\nRelay: {args.host}:{args.port}\n\n"
        "If this arrives, the service's --notify-email/--notify-from "
        "flags will deliver too.",
    )
    print(f"Sent via {args.host}:{args.port} as {sender} -> {args.to}")


if __name__ == "__main__":
    main()
