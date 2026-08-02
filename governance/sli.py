"""Service level indicators: measurement, and the deadline that makes the
latency objective mean something.

One middleware, wrapped around every `tools/call`, doing two jobs:

* **Measure.** Time the call, classify the outcome, emit one record. The
  objectives are *not* applied here — records carry facts, and `sla.py` applies
  the document to them at report time, so a tier can be re-evaluated against
  history rather than only against traffic that has not happened yet.
* **Bound.** Cut a call off at the tier's deadline, where the service has said
  that is safe. An objective with nothing enforcing it is a wish: a call that
  hangs forever never breaches a latency objective, it simply never arrives in
  the numbers, and the SLI reads clean through an outage.

Deciding what counts is most of the value here, and two exclusions are the
substance of it:

* **An authorization denial is not a failure.** The system did exactly what it
  was configured to do. Counting `-32003` as an error means every access-control
  tightening looks like an outage, which ends with someone loosening the policy
  to protect a dashboard. Excluded from both numerator and denominator.
* **A pause for human approval is not latency.** The gated call returns an
  `InputRequiredResult` and waits for a person. Whatever that costs, it is not
  the service being slow, and the wait itself happens on the client side
  between two round trips. Excluded.

What is deliberately *not* excluded is a tool that raised because the caller
asked for something that does not exist. That is a caller error, and mature
practice keeps 4xx-equivalents out of the budget — but this layer cannot yet
tell `unknown shipment: SHP-9999` apart from a downstream system falling over,
because both arrive as `isError` with a string. Counting both as failures
over-reports errors, and over-reporting is the safe direction: it makes the
service look worse than it is rather than better. The distinction becomes
available when the connector layer (roadmap tranche 2) types its failures, and
that is where it should be made rather than by pattern-matching messages here.

Records go to their own logger, for the same reason the audit records do:
different reader, different retention, and a stream meant to be parsed must not
share a handler with one meant to be read.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, TextIO

import anyio
from mcp.shared.exceptions import MCPError

from .middleware import AUTHORIZATION_DENIED
from .sla import BAD, EXCLUDED, GOOD, UNMAPPED, Sla

sli_logger = logging.getLogger("governance.sli")

# JSON-RPC implementation-defined range, next after AUTHORIZATION_DENIED
# (-32003). Not the SDK's own -32001 "request timeout": that one means the
# transport gave up, and a client deciding whether to retry needs to tell it
# apart from this server deciding, on purpose, that a call had run long enough.
DEADLINE_EXCEEDED = -32004


def configure(stream: TextIO | None = None) -> None:
    """Send SLI records to their own handler, unformatted and unpropagated."""
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    sli_logger.handlers = [handler]
    sli_logger.propagate = False
    sli_logger.setLevel(logging.INFO)


def record(
    *,
    service: str,
    tier: str,
    tool: str,
    latency_ms: float,
    outcome: str,
    reason: str,
    protocol_version: str | None = None,
) -> None:
    """Emit one SLI sample as a structured record.

    No principal. Attainment is a property of the service, not of whoever
    called it, and copying the principal into a second stream would double the
    surface carrying identity for no question this report can answer. The cost
    is real and stated in the docs: per-consumer attainment is not supported,
    and would need a correlation id added to both streams rather than a name
    added to this one.
    """
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "service": service,
        "tier": tier,
        "tool": tool,
        "latency_ms": round(latency_ms, 3),
        "outcome": outcome,
        "reason": reason,
        "protocol_version": protocol_version,
    }
    # Failures are the events worth keeping if the level is turned up in
    # production; the good ones are the volume. Same split as the audit stream —
    # and the same warning applies, that dropping INFO leaves a report that can
    # count failures but not compute a ratio.
    log = sli_logger.warning if outcome == BAD else sli_logger.info
    log(json.dumps(event, separators=(",", ":"), default=str))


class ServiceLevelMiddleware:
    """Times every `tools/call`, classifies it, and enforces the tier deadline.

    Registered *outside* `PolicyEnforcementMiddleware`, so it observes denials
    and approval pauses and can exclude them itself. The alternative — measuring
    inside the gate — would exclude them by simply never seeing them, which
    looks the same in the numbers and hides how often calls are being refused.
    """

    def __init__(self, sla: Sla, *, enforce_deadlines: bool = True) -> None:
        self.sla = sla
        # Off is the shadow posture: measure everything, cancel nothing. Worth
        # running that way first, because a deadline is the one part of this
        # framework that can turn a slow call into a failed one.
        self.enforce_deadlines = enforce_deadlines

    async def __call__(self, ctx: Any, call_next: Any) -> Any:
        if getattr(ctx, "method", None) != "tools/call":
            return await call_next(ctx)

        params = getattr(ctx, "params", None)
        tool = str(params.get("name", "")) if isinstance(params, Mapping) else ""
        service = self.sla.service_for_tool(tool)
        version = getattr(ctx, "protocol_version", None)

        deadline_ms = (
            service.tier.deadline_ms
            if service is not None and service.enforce_deadline and self.enforce_deadlines
            else None
        )

        started = time.perf_counter()
        try:
            if deadline_ms is None:
                result = await call_next(ctx)
            else:
                with anyio.fail_after(deadline_ms / 1000):
                    result = await call_next(ctx)
        except TimeoutError:
            outcome, reason = BAD, "deadline exceeded"
            self._emit(service, tool, started, outcome, reason, version)
            raise MCPError(
                DEADLINE_EXCEEDED,
                f"{tool!r} exceeded the {deadline_ms} ms deadline for its service level "
                f"and was cancelled.",
                {"target": tool, "deadline_ms": deadline_ms,
                 "service": service.name if service else UNMAPPED},
            ) from None
        except MCPError as exc:
            if exc.code == AUTHORIZATION_DENIED:
                outcome, reason = EXCLUDED, "authorization denied"
            else:
                outcome, reason = BAD, "protocol error"
            self._emit(service, tool, started, outcome, reason, version)
            raise
        except Exception:
            self._emit(service, tool, started, BAD, "unhandled error", version)
            raise

        outcome, reason = self._classify(result, unmapped=service is None)
        self._emit(service, tool, started, outcome, reason, version)
        return result

    @staticmethod
    def _classify(result: Any, *, unmapped: bool) -> tuple[str, str]:
        if unmapped:
            # Nothing has been promised about this tool, so there is nothing to
            # measure it against. Recorded rather than dropped, so the gap shows
            # up in the report instead of only in a test.
            return EXCLUDED, "tool is not mapped to a service"
        if _is_input_required(result):
            return EXCLUDED, "awaiting human approval"
        if _reported_error(result):
            return BAD, "tool reported an error"
        return GOOD, "completed"

    def _emit(
        self,
        service: Any,
        tool: str,
        started: float,
        outcome: str,
        reason: str,
        version: str | None,
    ) -> None:
        record(
            service=service.name if service is not None else UNMAPPED,
            tier=service.tier.name if service is not None else "",
            tool=tool,
            latency_ms=(time.perf_counter() - started) * 1000,
            outcome=outcome,
            reason=reason,
            protocol_version=version,
        )


def _is_input_required(result: Any) -> bool:
    """True for the paused half of the approval flow.

    Both shapes are handled for the same reason the policy middleware handles
    both: a short-circuiting middleware returns its model directly, while a
    result that reached the dispatcher has already been serialized to the wire
    form, and pinning to one of them would break silently on an SDK bump.
    """
    if isinstance(result, Mapping):
        return bool(result.get("inputRequests") or result.get("input_requests"))
    return getattr(result, "input_requests", None) is not None


def _reported_error(result: Any) -> bool:
    if isinstance(result, Mapping):
        return bool(result.get("isError") or result.get("is_error"))
    return bool(getattr(result, "is_error", False))


__all__ = [
    "DEADLINE_EXCEEDED",
    "ServiceLevelMiddleware",
    "configure",
    "record",
    "sli_logger",
]
