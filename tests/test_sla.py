"""Tests for the service level framework.

Three things are worth testing here, and only one of them is arithmetic.

The document validation carries the most weight: an SLA is a promise someone
signs, and the failure mode that matters is a promise that was impossible on
the day it was made — a tier above what the dependencies can deliver, a
percentage written as `99.9` where `0.999` was meant. Those have to be startup
errors, because nothing downstream will ever notice them.

The classification tests come second: what counts as a failure decides whether
the number means anything. An authorization denial recorded as an outage makes
every policy tightening look like an incident.

The percentile and budget maths come last. They are the part everyone assumes
is the hard bit, and the part a wrong answer is most likely to be spotted in.
"""

from __future__ import annotations

import io
import json
from contextlib import contextmanager

import anyio
import pytest
import yaml
from mcp import Client
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.shared.exceptions import MCPError

from governance import AUTHORIZATION_DENIED, Sla, SlaError
from governance import sla as sla_module
from governance import sli
from governance.devidp import DevIdentityProvider
from governance.sla import BAD, EXCLUDED, GOOD, ServiceReport, percentile
from governance.verifier import EntraTokenVerifier, StaticJwks

DOCUMENT = """
version: 1
tiers:
  gold:
    availability_objective: 0.999
    latency_objective_ms: 500
    latency_percentile: 95
    deadline_ms: 5000
    window_days: 30
  silver:
    availability_objective: 0.98
    latency_objective_ms: 2000
    latency_percentile: 95
    deadline_ms: 20000
    window_days: 30
dependencies:
  entra:
    availability: 0.9999
  downstream:
    availability: 0.99
platform:
  depends_on: [entra]
services:
  reads:
    tier: gold
    depends_on: []
    enforce_deadline: true
    tools: [alpha, beta]
  writes:
    tier: silver
    depends_on: [downstream]
    enforce_deadline: false
    tools: [gamma]
"""


def document(**changes) -> dict:
    """The valid document above, with `changes` merged into the top level."""
    raw = yaml.safe_load(DOCUMENT)
    raw.update(changes)
    return raw


@pytest.fixture
def sla() -> Sla:
    return Sla.from_mapping(yaml.safe_load(DOCUMENT), source="<test>")


# --------------------------------------------------------------------------
# Document validation — failures that must happen at startup
# --------------------------------------------------------------------------


def test_a_tier_above_what_the_dependencies_allow_is_refused():
    """The check that earns the file.

    Availability composes multiplicatively, so a service reaching a 99%
    system cannot itself be better than 99% however well it is written.
    Promising gold on top of it is not ambitious, it is arithmetic that does
    not work, and it should fail before anyone signs it.
    """
    raw = document()
    raw["services"]["writes"]["tier"] = "gold"
    with pytest.raises(SlaError, match="cap it at"):
        Sla.from_mapping(raw, source="<test>")


def test_the_ceiling_is_the_product_of_the_whole_chain(sla: Sla):
    assert sla.ceiling("reads") == pytest.approx(0.9999)
    assert sla.ceiling("writes") == pytest.approx(0.9999 * 0.99)
    # Identity is in the path of every call whether or not the service names it.
    assert "entra" in sla.dependency_chain("writes")


def test_an_undeclared_dependency_is_refused():
    raw = document()
    raw["services"]["writes"]["depends_on"] = ["downstraem"]
    with pytest.raises(SlaError, match="undeclared dependenc"):
        Sla.from_mapping(raw, source="<test>")


def test_an_undeclared_tier_is_refused():
    raw = document()
    raw["services"]["reads"]["tier"] = "platinum"
    with pytest.raises(SlaError, match="not declared under"):
        Sla.from_mapping(raw, source="<test>")


def test_a_tool_claimed_by_two_services_is_refused():
    """Two services claiming one tool is two promises about the same call."""
    raw = document()
    raw["services"]["writes"]["tools"] = ["gamma", "alpha"]
    with pytest.raises(SlaError, match="claimed by both"):
        Sla.from_mapping(raw, source="<test>")


def test_a_percentage_written_as_a_percentage_is_refused():
    """`99.9` where `0.999` was meant — the misplaced decimal point."""
    raw = document()
    raw["tiers"]["gold"]["availability_objective"] = 99.9
    with pytest.raises(SlaError, match="ratio strictly between 0 and 1"):
        Sla.from_mapping(raw, source="<test>")


def test_a_deadline_inside_the_latency_objective_is_refused():
    """A deadline below the objective cancels calls the objective permits."""
    raw = document()
    raw["tiers"]["gold"]["deadline_ms"] = 400
    with pytest.raises(SlaError, match="must exceed"):
        Sla.from_mapping(raw, source="<test>")


def test_a_service_claiming_no_tools_is_refused():
    raw = document()
    raw["services"]["reads"]["tools"] = []
    with pytest.raises(SlaError, match="claims no tools"):
        Sla.from_mapping(raw, source="<test>")


def test_unsupported_version_is_refused():
    with pytest.raises(SlaError, match="version"):
        Sla.from_mapping({"version": 99})


def test_coverage_names_the_tools_nobody_promised_anything_about(sla: Sla):
    with pytest.raises(SlaError, match="delta"):
        sla.check_coverage(["alpha", "delta"])


# --------------------------------------------------------------------------
# The shipped document
# --------------------------------------------------------------------------


def test_the_shipped_sla_file_is_valid():
    loaded = Sla.load("sla.yaml")
    assert loaded.services
    assert loaded.dependencies["servicenow"].source, "an assumed number must say where it came from"


def test_every_registered_tool_belongs_to_a_service():
    """The counterpart to `test_every_tool_has_a_policy_entry`.

    A tool with no service level is not blocked — a commitment is not an access
    control — so nothing at runtime will complain. This is where it gets
    noticed instead.
    """
    from server import create_server

    server = create_server(auth_mode="off")
    loaded = Sla.load("sla.yaml")

    async def names():
        return [t.name for t in await server.list_tools()]

    loaded.check_coverage(anyio.run(names))


def test_the_itsm_services_cannot_be_promoted_to_gold():
    """The shipped document's own arithmetic, not a synthetic one.

    ServiceNow is assumed at 99%, so anything reaching it is capped below the
    gold objective. This is the concrete form of the rule, and it is here so
    that raising a tier by hand fails a test rather than shipping.
    """
    raw = yaml.safe_load(open("sla.yaml").read())
    raw["services"]["delay-assessment"]["tier"] = "gold"
    with pytest.raises(SlaError, match="cap it at"):
        Sla.from_mapping(raw, source="sla.yaml")


# --------------------------------------------------------------------------
# Attainment and error budget
# --------------------------------------------------------------------------


def sample(service: str, outcome: str, latency_ms: float = 10.0) -> dict:
    return {"service": service, "outcome": outcome, "latency_ms": latency_ms}


def find(reports: list[ServiceReport], name: str) -> ServiceReport:
    return next(r for r in reports if r.service == name)


def test_availability_is_good_over_valid(sla: Sla):
    samples = [sample("reads", GOOD)] * 98 + [sample("reads", BAD)] * 2
    reads = find(sla_module.report(samples, sla), "reads")
    assert reads.valid == 100
    assert reads.availability == pytest.approx(0.98)


def test_excluded_events_leave_the_ratio_alone_and_are_still_counted(sla: Sla):
    """An exclusion must not quietly shrink the denominator without a trace."""
    samples = [sample("reads", GOOD)] * 9 + [sample("reads", BAD)] + [sample("reads", EXCLUDED)] * 40
    reads = find(sla_module.report(samples, sla), "reads")
    assert reads.valid == 10
    assert reads.availability == pytest.approx(0.9)
    assert reads.excluded == 40


def test_the_error_budget_is_spent_before_the_objective_is_breached(sla: Sla):
    """Gold allows 0.1% failures: one bad call in 1000 spends the budget exactly."""
    samples = [sample("reads", GOOD)] * 999 + [sample("reads", BAD)]
    reads = find(sla_module.report(samples, sla), "reads")
    assert reads.budget_used == pytest.approx(1.0)
    assert not reads.breached_availability

    samples.append(sample("reads", BAD))
    reads = find(sla_module.report(samples, sla), "reads")
    assert reads.budget_used > 1.0
    assert reads.breached_availability


def test_latency_is_measured_over_successful_calls_only(sla: Sla):
    """A failure that returned in 1 ms is not evidence of a fast service."""
    samples = [sample("reads", GOOD, 900.0)] * 10 + [sample("reads", BAD, 1.0)] * 90
    reads = find(sla_module.report(samples, sla), "reads")
    assert reads.observed_latency_ms == pytest.approx(900.0)
    assert reads.breached_latency


def test_a_service_with_no_samples_is_reported_rather_than_omitted(sla: Sla):
    """A service that went silent reads as healthy in any report that drops it."""
    reports = sla_module.report([sample("reads", GOOD)], sla)
    writes = find(reports, "writes")
    assert writes.valid == 0
    assert writes.availability is None
    assert "no data" in sla_module.format_report(reports)


def test_a_breach_is_named_in_the_formatted_report(sla: Sla):
    # Silver allows 2% failures, so 50 events is the smallest sample that can
    # show a breach at all — see the underpowered test below.
    samples = [sample("writes", GOOD)] * 95 + [sample("writes", BAD)] * 5
    assert "BREACH" in sla_module.format_report(sla_module.report(samples, sla))


def test_an_objective_cannot_be_breached_on_a_sample_too_small_to_evaluate_it(sla: Sla):
    """Five failures out of five is not evidence against a 99.9% objective.

    It is barely evidence of anything. Reporting a breach here would be
    arithmetically defensible and practically wrong: it trains people to
    discount the number, and the number is the whole point.
    """
    reads = find(sla_module.report([sample("reads", BAD)] * 5, sla), "reads")
    assert reads.underpowered
    assert reads.events_needed == 1000
    assert "too few events" in sla_module.format_report([reads])
    assert "BREACH" not in sla_module.format_report([reads])


def test_percentile_is_nearest_rank():
    values = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    assert percentile(values, 95) == 100
    assert percentile(values, 50) == 50
    assert percentile([], 95) is None


def test_records_from_a_file_round_trip_into_a_report(tmp_path, sla: Sla):
    path = tmp_path / "sli.jsonl"
    path.write_text(
        "\n".join(json.dumps(sample("reads", GOOD)) for _ in range(4))
        + "\nnot json at all\n"  # a line from something else sharing the file
        + json.dumps(sample("reads", BAD))
        + "\n"
    )
    loaded = sla_module.load_samples(path)
    assert len(loaded) == 5
    assert find(sla_module.report(loaded, sla), "reads").availability == pytest.approx(0.8)


# --------------------------------------------------------------------------
# Measurement — what counts as a failure
# --------------------------------------------------------------------------


@contextmanager
def captured():
    """Collect the SLI records emitted inside the block."""
    stream = io.StringIO()
    previous_handlers, previous_propagate = sli.sli_logger.handlers, sli.sli_logger.propagate
    sli.configure(stream)
    records: list[dict] = []
    try:
        yield records
    finally:
        sli.sli_logger.handlers, sli.sli_logger.propagate = previous_handlers, previous_propagate
        records.extend(json.loads(line) for line in stream.getvalue().splitlines() if line.strip())


class Ctx:
    """The bits of the request context the middleware reads."""

    def __init__(self, tool: str) -> None:
        self.method = "tools/call"
        self.params = {"name": tool, "arguments": {}}
        self.protocol_version = "2026-07-28"


def drive(sla: Sla, tool: str, call_next, *, enforce_deadlines: bool = True):
    middleware = sli.ServiceLevelMiddleware(sla, enforce_deadlines=enforce_deadlines)
    return anyio.run(lambda: middleware(Ctx(tool), call_next))


async def succeeds(ctx):
    return {"content": [], "isError": False}


async def fails(ctx):
    return {"content": [{"type": "text", "text": "downstream exploded"}], "isError": True}


async def pauses(ctx):
    return {"inputRequests": {"approval": {}}, "requestState": "opaque"}


async def denied(ctx):
    raise MCPError(AUTHORIZATION_DENIED, "Access denied for 'alpha'.")


def test_a_successful_call_is_a_good_event(sla: Sla):
    with captured() as records:
        drive(sla, "alpha", succeeds)
    assert records[0]["outcome"] == GOOD
    assert records[0]["service"] == "reads"
    assert records[0]["tier"] == "gold"
    assert records[0]["latency_ms"] >= 0


def test_a_tool_error_spends_the_error_budget(sla: Sla):
    with captured() as records:
        drive(sla, "alpha", fails)
    assert records[0]["outcome"] == BAD


def test_an_authorization_denial_is_excluded_rather_than_counted_as_a_failure(sla: Sla):
    """The exclusion that matters most.

    Counting a denial as an outage means every tightening of `policy.yaml`
    shows up as a service level incident, which ends with someone loosening the
    policy to protect a dashboard.
    """
    with captured() as records:
        with pytest.raises(MCPError):
            drive(sla, "alpha", denied)
    assert records[0]["outcome"] == EXCLUDED
    assert records[0]["reason"] == "authorization denied"


def test_a_pause_for_human_approval_is_excluded(sla: Sla):
    """Waiting on a person is not the service being slow."""
    with captured() as records:
        drive(sla, "gamma", pauses)
    assert records[0]["outcome"] == EXCLUDED
    assert records[0]["reason"] == "awaiting human approval"


def test_an_unmapped_tool_is_recorded_rather_than_dropped(sla: Sla):
    with captured() as records:
        drive(sla, "not_in_any_service", succeeds)
    assert records[0]["service"] == "<unmapped>"
    assert records[0]["outcome"] == EXCLUDED


def test_a_call_past_the_deadline_is_cancelled_and_counted_against_the_budget(sla: Sla):
    """An objective with nothing enforcing it is a wish.

    A call that hangs does not breach a latency objective — it never arrives in
    the numbers at all, and the SLI reads clean through an outage.
    """
    async def hangs(ctx):
        await anyio.sleep(30)
        return {"isError": False}

    raw = document()
    raw["tiers"]["gold"]["deadline_ms"] = 30  # keeps the test fast; the mechanism is the same
    raw["tiers"]["gold"]["latency_objective_ms"] = 10
    quick = Sla.from_mapping(raw, source="<test>")

    with captured() as records:
        with pytest.raises(MCPError) as exc:
            drive(quick, "alpha", hangs)
    assert exc.value.code == sli.DEADLINE_EXCEEDED
    assert records[0]["outcome"] == BAD
    assert records[0]["reason"] == "deadline exceeded"


def test_deadlines_can_be_measured_without_being_enforced(sla: Sla):
    """The shadow posture: a deadline is the one control here that can turn a
    slow call into a failed one, so it should be observed before it is armed."""
    async def slow(ctx):
        await anyio.sleep(0.05)
        return {"isError": False}

    raw = document()
    raw["tiers"]["gold"]["deadline_ms"] = 20
    raw["tiers"]["gold"]["latency_objective_ms"] = 10
    quick = Sla.from_mapping(raw, source="<test>")

    with captured() as records:
        drive(quick, "alpha", slow, enforce_deadlines=False)
    assert records[0]["outcome"] == GOOD
    assert records[0]["latency_ms"] > 20


def test_a_service_that_opted_out_is_not_deadline_enforced(sla: Sla):
    """`incident-raising` is off deliberately: cancelling a write mid-flight can
    leave the downstream record created and the caller told it failed."""
    async def slow(ctx):
        await anyio.sleep(0.05)
        return {"isError": False}

    raw = document()
    raw["tiers"]["silver"]["deadline_ms"] = 20
    raw["tiers"]["silver"]["latency_objective_ms"] = 10
    quick = Sla.from_mapping(raw, source="<test>")

    with captured() as records:
        drive(quick, "gamma", slow)  # `writes` has enforce_deadline: false
    assert records[0]["outcome"] == GOOD


def test_records_carry_no_principal(sla: Sla):
    """Attainment is a property of the service, not of who called it.

    Stated as a test because the tempting change — adding the principal, which
    is right there in the auth context — would put identity into a second
    stream with its own retention, for a question this report cannot answer
    anyway. See the note in governance/sli.py.
    """
    with captured() as records:
        drive(sla, "alpha", succeeds)
    assert "principal" not in records[0]


# --------------------------------------------------------------------------
# Through the real server — the ordering the unit tests cannot prove
# --------------------------------------------------------------------------


def build(roles: list[str]):
    from server import create_server

    idp = DevIdentityProvider()
    server = create_server(auth_mode="dev", dev_idp=idp)
    verifier = EntraTokenVerifier(
        issuer=idp.issuer, audience=idp.audience, key_source=StaticJwks(idp.jwks())
    )
    access_token = anyio.run(verifier.verify_token, idp.issue(roles=roles))
    assert access_token is not None
    return server, access_token


@contextmanager
def as_principal(access_token):
    reset = auth_context_var.set(AuthenticatedUser(access_token) if access_token else None)
    try:
        yield
    finally:
        auth_context_var.reset(reset)


def test_measurement_wraps_enforcement_so_denials_are_observed():
    """Registration order, asserted rather than assumed.

    If the service level middleware were registered inside the policy one it
    would never see a denial, which produces the same clean availability number
    while hiding how often callers are refused. The observable difference is
    whether a record exists at all.
    """
    server, token = build(["some.unrelated.role"])

    async def scenario():
        async with Client(server) as client:
            with pytest.raises(MCPError):
                await client.call_tool("get_shipment_status", {"shipment_id": "SHP-1002"})

    with captured() as records:
        with as_principal(token):
            anyio.run(scenario)

    assert records, "a denied call produced no service level record"
    assert records[0]["service"] == "transport-visibility"
    assert records[0]["outcome"] == EXCLUDED


def test_an_authorized_call_through_the_server_is_a_good_event():
    server, token = build(["tlo.reader"])

    async def scenario():
        async with Client(server) as client:
            await client.call_tool("get_shipment_status", {"shipment_id": "SHP-1002"})

    with captured() as records:
        with as_principal(token):
            anyio.run(scenario)

    assert [r["outcome"] for r in records] == [GOOD]
    assert records[0]["tool"] == "get_shipment_status"


def test_a_tool_raising_on_bad_input_is_recorded_as_a_failure():
    """Deliberate, and deliberately imperfect.

    A caller asking for a shipment that does not exist is a caller error, and
    mature practice keeps those out of the budget. This layer cannot tell it
    apart from a downstream system falling over — both arrive as `isError` with
    a string — so both count, which over-reports failures rather than under-
    reporting them. The distinction becomes available when the connector layer
    types its failures (roadmap tranche 2).
    """
    server, token = build(["tlo.reader"])

    async def scenario():
        async with Client(server) as client:
            await client.call_tool("get_shipment_status", {"shipment_id": "SHP-DOES-NOT-EXIST"})

    with captured() as records:
        with as_principal(token):
            anyio.run(scenario)

    assert records[0]["outcome"] == BAD
