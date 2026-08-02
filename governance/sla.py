"""Declarative service level document.

Loads `sla.yaml` and answers three questions:

* Which service does this tool belong to, and what has been promised about it?
* Is that promise arithmetically possible, given what the service depends on?
* Given a stream of measurements, is the promise being kept, and how much of
  the error budget is left?

The same two properties as the authorization policy, for the same reasons:

* **Fail closed.** A tool that is not mapped to a service has nothing promised
  about it. It is not blocked — a service level is a commitment, not an access
  control, and refusing traffic because nobody wrote an SLO would be a worse
  outcome than serving it — but it is recorded as unmapped and the coverage
  check fails, so it cannot quietly stay that way.
* **Fail fast.** A tier that promises more availability than the service's
  dependencies can deliver is a startup error. This is the check that earns the
  file: dependency arithmetic is the thing that gets skipped, and skipping it
  produces a commitment that is impossible on the day it is signed and only
  discovered a quarter later.

Availability is measured as a **request-based SLI** — good events over valid
events — not as uptime. A stateless replica set behind a load balancer is
almost never wholly down, so uptime flatters the number and does not describe
what a caller experienced. What counts as valid, and what is deliberately
excluded from the denominator, is decided in `sli.py` and argued in
`docs/sla-framework.md`.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# Outcomes an SLI record can carry. `good` and `bad` are the numerator and the
# budget spend; `excluded` is neither, and appears in reports only as a count,
# so that a spike in exclusions is visible rather than silently shrinking the
# denominator.
GOOD = "good"
BAD = "bad"
EXCLUDED = "excluded"

# The service name recorded for a tool no service claims. Deliberately not a
# valid YAML key shape, so it cannot be mistaken for a real service if it
# reaches a dashboard.
UNMAPPED = "<unmapped>"


class SlaError(Exception):
    """Raised when the service level document is malformed or impossible."""


@dataclass(frozen=True)
class Tier:
    name: str
    description: str
    availability_objective: float
    latency_objective_ms: int
    latency_percentile: int
    deadline_ms: int
    window_days: int
    # Declared for completeness; nothing in this repository measures a human
    # response time. Keyed by ServiceNow urgency so a breach maps onto the
    # incident it will be raised as.
    response_targets: Mapping[str, int]


@dataclass(frozen=True)
class Dependency:
    name: str
    description: str
    availability: float
    source: str
    timeout_ms: int | None = None


@dataclass(frozen=True)
class Service:
    name: str
    description: str
    tier: Tier
    owner: str
    business_service: str
    configuration_items: tuple[str, ...]
    depends_on: tuple[str, ...]
    tools: frozenset[str]
    enforce_deadline: bool


@dataclass(frozen=True)
class Sla:
    tiers: Mapping[str, Tier]
    services: Mapping[str, Service]
    dependencies: Mapping[str, Dependency]
    platform_depends_on: tuple[str, ...]
    source: str = "<memory>"

    # -- loading ---------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> Sla:
        path = Path(path)
        try:
            raw = yaml.safe_load(path.read_text())
        except FileNotFoundError as exc:
            raise SlaError(f"service level file not found: {path}") from exc
        except yaml.YAMLError as exc:
            raise SlaError(f"service level file is not valid YAML: {path}: {exc}") from exc
        return cls.from_mapping(raw, source=str(path))

    @classmethod
    def from_mapping(cls, raw: Any, source: str = "<memory>") -> Sla:
        if not isinstance(raw, Mapping):
            raise SlaError(f"{source}: service level document must be a mapping")
        if raw.get("version") != 1:
            raise SlaError(
                f"{source}: unsupported service level version {raw.get('version')!r}, expected 1"
            )

        tiers = _parse_tiers(raw.get("tiers"), source)
        dependencies = _parse_dependencies(raw.get("dependencies"), source)
        platform = _parse_platform(raw.get("platform"), dependencies, source)
        services = _parse_services(raw.get("services"), tiers, dependencies, source)

        sla = cls(
            tiers=tiers,
            services=services,
            dependencies=dependencies,
            platform_depends_on=platform,
            source=source,
        )
        sla._check_objectives_are_achievable()
        return sla

    def _check_objectives_are_achievable(self) -> None:
        """Refuse a promise the dependencies cannot support.

        Availability composes multiplicatively across anything in the request
        path, so a service depending on one 99% system cannot itself be better
        than 99% however well it is written. Catching that here turns an
        impossible commitment into a startup error rather than a quarterly
        surprise, and it is the reason the tier and the dependency list live in
        the same document.
        """
        for service in self.services.values():
            ceiling = self.ceiling(service.name)
            if service.tier.availability_objective > ceiling:
                names = ", ".join(self.dependency_chain(service.name)) or "none"
                raise SlaError(
                    f"{self.source}: service {service.name!r} is tier "
                    f"{service.tier.name!r} ({service.tier.availability_objective:.4%}), but its "
                    f"dependencies ({names}) cap it at {ceiling:.4%}. Lower the tier, remove the "
                    "dependency from the request path, or renegotiate the dependency."
                )

    # -- queries ---------------------------------------------------------

    def dependency_chain(self, service: str) -> tuple[str, ...]:
        """Everything in this service's request path, platform dependencies included."""
        own = self.services[service].depends_on if service in self.services else ()
        seen: dict[str, None] = {}
        for name in (*self.platform_depends_on, *own):
            seen[name] = None
        return tuple(seen)

    def ceiling(self, service: str) -> float:
        """The best availability this service could achieve if it never failed itself."""
        product = 1.0
        for name in self.dependency_chain(service):
            product *= self.dependencies[name].availability
        return product

    def service_for_tool(self, tool: str) -> Service | None:
        for service in self.services.values():
            if tool in service.tools:
                return service
        return None

    def mapped_tools(self) -> frozenset[str]:
        return frozenset().union(*(s.tools for s in self.services.values())) if self.services else frozenset()

    def check_coverage(self, tools: Iterable[str]) -> None:
        """Raise if any of `tools` has no service, and therefore nothing promised.

        The counterpart to the authorization coverage check. Shipping a tool
        with no service level is not a security hole, so this does not run at
        every request — but a tool nobody has committed to is a tool nobody is
        watching, and the moment to notice is when it is added.
        """
        unmapped = sorted(set(tools) - self.mapped_tools())
        if unmapped:
            raise SlaError(
                f"{self.source}: no service claims these tools: {', '.join(unmapped)}. "
                "Add them to a service under `services:`, or add a service for them."
            )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _require_mapping(raw: Any, source: str, what: str) -> Mapping[str, Any]:
    if raw is None:
        raise SlaError(f"{source}: `{what}` is required")
    if not isinstance(raw, Mapping):
        raise SlaError(f"{source}: `{what}` must be a mapping")
    return raw


def _ratio(value: Any, source: str, what: str) -> float:
    try:
        ratio = float(value)
    except (TypeError, ValueError) as exc:
        raise SlaError(f"{source}: `{what}` must be a number between 0 and 1") from exc
    # An objective of exactly 1 is not achievable by anything with a dependency,
    # and one of 0 is not a promise. Both are almost always a misplaced decimal
    # point — 99.9 written where 0.999 was meant.
    if not 0 < ratio < 1:
        raise SlaError(
            f"{source}: `{what}` is {value!r}; expected a ratio strictly between 0 and 1 "
            "(0.999 for 99.9%, not 99.9)"
        )
    return ratio


def _positive_int(value: Any, source: str, what: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise SlaError(f"{source}: `{what}` must be a whole number") from exc
    if number <= 0:
        raise SlaError(f"{source}: `{what}` must be greater than zero, got {number}")
    return number


def _parse_tiers(raw: Any, source: str) -> dict[str, Tier]:
    body = _require_mapping(raw, source, "tiers")
    tiers: dict[str, Tier] = {}
    for name, spec in body.items():
        name = str(name)
        spec = _require_mapping(spec, source, f"tiers.{name}")

        percentile = _positive_int(spec.get("latency_percentile", 95), source,
                                   f"tiers.{name}.latency_percentile")
        if not 50 <= percentile <= 100:
            raise SlaError(
                f"{source}: `tiers.{name}.latency_percentile` is {percentile}; expected 50-100"
            )

        latency = _positive_int(spec.get("latency_objective_ms"), source,
                                f"tiers.{name}.latency_objective_ms")
        deadline = _positive_int(spec.get("deadline_ms"), source, f"tiers.{name}.deadline_ms")
        # A deadline at or below the latency objective cuts off the very calls
        # the objective allows to be slow, so the tier would breach itself.
        if deadline <= latency:
            raise SlaError(
                f"{source}: `tiers.{name}.deadline_ms` ({deadline}) must exceed "
                f"`latency_objective_ms` ({latency}); a deadline inside the objective would "
                "cancel calls the objective permits to be slow."
            )

        targets = spec.get("response_targets") or {}
        if not isinstance(targets, Mapping):
            raise SlaError(f"{source}: `tiers.{name}.response_targets` must be a mapping")

        tiers[name] = Tier(
            name=name,
            description=str(spec.get("description", "")).strip(),
            availability_objective=_ratio(spec.get("availability_objective"), source,
                                          f"tiers.{name}.availability_objective"),
            latency_objective_ms=latency,
            latency_percentile=percentile,
            deadline_ms=deadline,
            window_days=_positive_int(spec.get("window_days", 30), source,
                                      f"tiers.{name}.window_days"),
            response_targets={str(k): int(v) for k, v in targets.items()},
        )
    if not tiers:
        raise SlaError(f"{source}: at least one tier must be declared")
    return tiers


def _parse_dependencies(raw: Any, source: str) -> dict[str, Dependency]:
    if raw is None:
        return {}
    body = _require_mapping(raw, source, "dependencies")
    dependencies: dict[str, Dependency] = {}
    for name, spec in body.items():
        name = str(name)
        spec = _require_mapping(spec, source, f"dependencies.{name}")
        timeout = spec.get("timeout_ms")
        dependencies[name] = Dependency(
            name=name,
            description=str(spec.get("description", "")).strip(),
            availability=_ratio(spec.get("availability"), source,
                                f"dependencies.{name}.availability"),
            # Recorded so that an assumed number can be told apart from a
            # contracted one when someone asks what this promise rests on.
            source=str(spec.get("source", "")).strip(),
            timeout_ms=_positive_int(timeout, source, f"dependencies.{name}.timeout_ms")
            if timeout is not None
            else None,
        )
    return dependencies


def _parse_platform(raw: Any, dependencies: Mapping[str, Dependency], source: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    body = _require_mapping(raw, source, "platform")
    names = body.get("depends_on") or []
    if not isinstance(names, list):
        raise SlaError(f"{source}: `platform.depends_on` must be a list")
    names = [str(n) for n in names]
    _check_declared(names, dependencies, source, "platform.depends_on")
    return tuple(names)


def _parse_services(
    raw: Any,
    tiers: Mapping[str, Tier],
    dependencies: Mapping[str, Dependency],
    source: str,
) -> dict[str, Service]:
    body = _require_mapping(raw, source, "services")
    services: dict[str, Service] = {}
    claimed: dict[str, str] = {}

    for name, spec in body.items():
        name = str(name)
        spec = _require_mapping(spec, source, f"services.{name}")

        tier_name = str(spec.get("tier", ""))
        if tier_name not in tiers:
            raise SlaError(
                f"{source}: `services.{name}.tier` is {tier_name!r}, which is not declared under "
                f"`tiers:`. Declared tiers: {', '.join(sorted(tiers)) or 'none'}."
            )

        depends_on = spec.get("depends_on") or []
        if not isinstance(depends_on, list):
            raise SlaError(f"{source}: `services.{name}.depends_on` must be a list")
        depends_on = [str(d) for d in depends_on]
        _check_declared(depends_on, dependencies, source, f"services.{name}.depends_on")

        tools = spec.get("tools") or []
        if not isinstance(tools, list):
            raise SlaError(f"{source}: `services.{name}.tools` must be a list")
        tools = [str(t) for t in tools]
        if not tools:
            raise SlaError(f"{source}: `services.{name}` claims no tools; a service with no tools "
                           "has nothing to measure")

        for tool in tools:
            # One tool, one owner. Two services claiming the same tool means two
            # different promises about the same call, and the reports would
            # double-count it.
            if tool in claimed:
                raise SlaError(
                    f"{source}: tool {tool!r} is claimed by both {claimed[tool]!r} and {name!r}; "
                    "a tool belongs to exactly one service."
                )
            claimed[tool] = name

        items = spec.get("configuration_items") or []
        if not isinstance(items, list):
            raise SlaError(f"{source}: `services.{name}.configuration_items` must be a list")

        services[name] = Service(
            name=name,
            description=str(spec.get("description", "")).strip(),
            tier=tiers[tier_name],
            owner=str(spec.get("owner", "")).strip(),
            business_service=str(spec.get("business_service", "")).strip(),
            configuration_items=tuple(str(i) for i in items),
            depends_on=tuple(depends_on),
            tools=frozenset(tools),
            enforce_deadline=bool(spec.get("enforce_deadline", False)),
        )

    if not services:
        raise SlaError(f"{source}: at least one service must be declared")
    return services


def _check_declared(
    names: Sequence[str], dependencies: Mapping[str, Dependency], source: str, what: str
) -> None:
    unknown = sorted(set(names) - set(dependencies))
    if unknown:
        raise SlaError(
            f"{source}: `{what}` references undeclared dependenc(ies): {', '.join(unknown)}. "
            "Add them under `dependencies:` with an availability, or fix the spelling."
        )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ServiceReport:
    """Attainment for one service over whatever samples were supplied.

    The objectives are applied at report time rather than baked into the
    records, so a tier change can be evaluated against history — "would we have
    breached last month under the tier we are about to promise" is the question
    worth being able to answer before signing anything.
    """

    service: str
    tier: str
    good: int
    bad: int
    excluded: int
    availability_objective: float
    latency_objective_ms: int
    latency_percentile: int
    # Successful events only. A call that failed in 3 ms is not evidence of a
    # fast service, and letting failures pull the percentile down is how a
    # latency objective survives an outage untouched.
    latencies_ms: tuple[float, ...] = ()

    @property
    def valid(self) -> int:
        return self.good + self.bad

    @property
    def availability(self) -> float | None:
        return self.good / self.valid if self.valid else None

    @property
    def budget_events(self) -> float:
        """How many failures the objective allows over the events observed."""
        return self.valid * (1 - self.availability_objective)

    @property
    def budget_used(self) -> float | None:
        """Fraction of the error budget spent. Above 1.0 is a breach."""
        if not self.valid:
            return None
        if self.budget_events == 0:
            return math.inf if self.bad else 0.0
        return self.bad / self.budget_events

    @property
    def breached_availability(self) -> bool:
        used = self.budget_used
        return used is not None and used > 1.0

    @property
    def events_needed(self) -> int:
        """Events required before the objective could be met at all.

        A 99.9% objective permits one failure in a thousand, so below a
        thousand events a single failure is a 100%-plus budget burn and the
        service reads as catastrophically breached on evidence that supports no
        such conclusion. Reported rather than smoothed over: the honest answer
        to "are we meeting 99.9%" on 10 requests is that the question cannot be
        answered yet, and a framework that hides that teaches people to ignore
        the number when it is finally real.
        """
        return math.ceil(1 / (1 - self.availability_objective))

    @property
    def underpowered(self) -> bool:
        return 0 < self.valid < self.events_needed

    @property
    def observed_latency_ms(self) -> float | None:
        return percentile(self.latencies_ms, self.latency_percentile)

    @property
    def breached_latency(self) -> bool:
        observed = self.observed_latency_ms
        return observed is not None and observed > self.latency_objective_ms

    @property
    def breached(self) -> bool:
        return self.breached_availability or self.breached_latency


def percentile(values: Sequence[float], percentile_rank: int) -> float | None:
    """Nearest-rank percentile. `None` for an empty sample.

    Nearest-rank rather than interpolated: with the handful of events a report
    over a short window contains, interpolation invents a latency that was
    never observed, and the difference is well inside the noise anyway.
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile_rank / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def report(samples: Iterable[Mapping[str, Any]], sla: Sla) -> list[ServiceReport]:
    """Fold SLI records into one report per declared service.

    Services with no samples are still reported, with zero events. A service
    that went silent looks identical to a healthy one in any report that omits
    it, and "no data" is the more urgent of the two.
    """
    good: dict[str, int] = {name: 0 for name in sla.services}
    bad: dict[str, int] = {name: 0 for name in sla.services}
    excluded: dict[str, int] = {name: 0 for name in sla.services}
    latencies: dict[str, list[float]] = {name: [] for name in sla.services}

    for sample in samples:
        service = str(sample.get("service", ""))
        if service not in sla.services:
            continue  # unmapped tools, or a service since removed from the document
        outcome = str(sample.get("outcome", ""))
        if outcome == GOOD:
            good[service] += 1
            latency = sample.get("latency_ms")
            if isinstance(latency, (int, float)):
                latencies[service].append(float(latency))
        elif outcome == BAD:
            bad[service] += 1
        elif outcome == EXCLUDED:
            excluded[service] += 1

    return [
        ServiceReport(
            service=name,
            tier=service.tier.name,
            good=good[name],
            bad=bad[name],
            excluded=excluded[name],
            availability_objective=service.tier.availability_objective,
            latency_objective_ms=service.tier.latency_objective_ms,
            latency_percentile=service.tier.latency_percentile,
            latencies_ms=tuple(latencies[name]),
        )
        for name, service in sla.services.items()
    ]


def load_samples(path: str | Path) -> list[dict[str, Any]]:
    """Read SLI records from a JSON-lines file. `-` reads standard input.

    Unparseable lines are skipped rather than fatal: the stream is written by a
    logger that may share a file with something else, and a report that refuses
    to run because one line was interleaved is a report nobody runs.
    """
    if str(path) == "-":
        import sys

        lines = sys.stdin.readlines()
    else:
        lines = Path(path).read_text().splitlines()

    samples: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            samples.append(parsed)
    return samples


def format_report(reports: Sequence[ServiceReport]) -> str:
    """A fixed-width table, for an operator reading it in a terminal."""
    header = (
        f"{'service':<22}{'tier':<9}{'events':>8}{'avail':>10}{'target':>9}"
        f"{'budget':>9}{'latency':>10}{'target':>9}  status"
    )
    lines = [header, "-" * len(header)]

    for r in sorted(reports, key=lambda r: r.service):
        avail = f"{r.availability:.3%}" if r.availability is not None else "--"
        used = r.budget_used
        if used is None:
            budget = "--"
        elif used == math.inf or used > 9.99:
            # Past ten times over there is no useful precision left, and a
            # five-digit percentage reads as a formatting bug rather than a
            # figure. Almost always a sample too small to say anything anyway.
            budget = ">999%"
        else:
            budget = f"{used:.0%}"
        observed = r.observed_latency_ms
        latency = (
            "--" if observed is None
            else f"{observed:.1f}ms" if observed < 10 else f"{observed:.0f}ms"
        )

        if not r.valid:
            status = "no data"
        elif r.underpowered:
            status = f"too few events (need {r.events_needed})"
        elif r.breached:
            what = [
                name
                for name, flag in (("availability", r.breached_availability),
                                   ("latency", r.breached_latency))
                if flag
            ]
            status = "BREACH: " + " and ".join(what)
        else:
            status = "ok"

        lines.append(
            f"{r.service:<22}{r.tier:<9}{r.valid:>8}{avail:>10}"
            f"{r.availability_objective:>9.3%}{budget:>9}"
            f"{latency:>10}{str(r.latency_objective_ms) + 'ms':>9}  {status}"
        )

    excluded = sum(r.excluded for r in reports)
    if excluded:
        lines.append("")
        lines.append(
            f"{excluded} event(s) excluded from these figures — authorization denials, "
            "approval pauses and unmapped tools. See docs/sla-framework.md."
        )
    if any(r.underpowered for r in reports):
        lines.append(
            "'too few events' means the objective cannot be evaluated on this sample, not "
            "that it was met. An objective of 99.9% permits one failure per 1000 requests, "
            "so fewer than 1000 cannot distinguish a healthy service from a breached one."
        )
    return "\n".join(lines)
