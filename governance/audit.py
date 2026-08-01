"""Authorization audit trail.

Deliberately separate from application logging. An audit record answers "who
was allowed to do what, and when" for someone reading it months later during a
review; an application log answers "what is this process doing" for someone
debugging now. They have different readers, different retention, and different
access controls, so they are different streams from the start — retrofitting
that split later means reprocessing history.

Tranche 1 emits decisions as JSON lines on a dedicated logger. Tranche 4
replaces the sink with an append-only store and adds the OpenTelemetry span
attributes, without changing what is recorded here.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import TextIO

from .policy import Decision

audit_logger = logging.getLogger("governance.audit")


def configure(stream: TextIO | None = None) -> None:
    """Send audit records to their own handler, unformatted and unpropagated.

    Without this the records inherit the application's root handler. The SDK
    installs a `rich` handler, which wraps and truncates long lines to fit the
    terminal — pretty for a human reading a traceback, and fatal for a stream
    whose only job is to be parsed later. An audit record that has been
    line-wrapped is not a record, so the stream owns its own handler and does
    not propagate to the root logger.
    """
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    audit_logger.handlers = [handler]
    audit_logger.propagate = False
    audit_logger.setLevel(logging.INFO)

# Argument values are not recorded. A shipment identifier is low risk, but the
# same code path will carry incident bodies and customer references once the
# connectors land, and a log that quietly became confidential-tier is worse
# than one that never held the data. Tranche 2 attaches per-field
# classification; until then only argument *names* are recorded.
_RECORD_ARGUMENT_VALUES = False


def record(
    *,
    principal: str,
    method: str,
    decision: Decision,
    arguments: dict | None = None,
    protocol_version: str | None = None,
    outcome: str | None = None,
) -> None:
    """Emit one authorization decision as a structured record.

    `outcome` overrides the allow/deny derived from `decision`. It exists for
    the one case that is neither: an approval that has been *requested* and is
    waiting on a human. Recording that as a denial would count every gated call
    twice and make the denial rate meaningless to whoever is watching it.
    """
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "principal": principal,
        "method": method,
        "target": decision.target,
        "outcome": outcome or ("allow" if decision.allowed else "deny"),
        "reason": decision.reason,
        "classification": decision.classification,
        "required_roles": list(decision.required_roles),
        "granted_roles": list(decision.granted_roles),
        "protocol_version": protocol_version,
    }
    if arguments is not None:
        event["argument_names"] = sorted(arguments)
        if _RECORD_ARGUMENT_VALUES:  # pragma: no cover - off by default
            event["arguments"] = arguments

    # Denials are the security-relevant events and should survive a log level
    # set to WARNING in production; allows and pending gates are the volume and
    # can be dropped.
    log = audit_logger.warning if event["outcome"] == "deny" else audit_logger.info
    log(json.dumps(event, separators=(",", ":"), default=str))
