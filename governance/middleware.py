"""Policy enforcement as server middleware.

Registered on `Server.middleware`, this sees every inbound request before
validation or dispatch, so authorization happens in exactly one place for every
tool rather than as a decorator each tool author has to remember. A tool that
forgets to opt in is denied by the policy's default, not accidentally exposed.

Three methods are intercepted:

* `tools/call`     — the decision that matters; denial raises before the
                     handler runs, so an unauthorized call never touches a
                     downstream system.
* `tools/list`     — filtered to what the caller may actually invoke.
* `resources/read` — same check, matched by URI glob.

Everything else passes through untouched.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.shared.exceptions import MCPError
from mcp_types import ListToolsResult

from . import audit
from .policy import Decision, Policy
from .verifier import describe_principal, roles_from_token

# JSON-RPC reserves -32000..-32099 for the server to define; the SDK documents
# -32000..-32019 as implementation-defined and already uses -32000 (connection
# closed) and -32001 (request timeout), with -32002 retired and never reused.
# -32003 is free, and a distinct code lets a client tell "you may not do this"
# apart from "that did not work", which is the difference between prompting for
# consent and retrying.
AUTHORIZATION_DENIED = -32003


class PolicyEnforcementMiddleware:
    """Applies `Policy` to every inbound request. Implements `ServerMiddleware`."""

    def __init__(self, policy: Policy, *, enforce: bool = True) -> None:
        self.policy = policy
        # `enforce=False` audits without blocking — the shadow mode used to
        # roll a policy out against real traffic and find the calls it would
        # have broken before it starts breaking them.
        self.enforce = enforce

    async def __call__(self, ctx: Any, call_next: Any) -> Any:
        method = getattr(ctx, "method", None)

        if method == "tools/call":
            return await self._guard_tool_call(ctx, call_next)
        if method == "resources/read":
            return await self._guard_resource_read(ctx, call_next)
        if method == "tools/list":
            return await self._filter_tool_list(ctx, call_next)

        return await call_next(ctx)

    # -- interception points ---------------------------------------------

    async def _guard_tool_call(self, ctx: Any, call_next: Any) -> Any:
        params = _params(ctx)
        tool = str(params.get("name", ""))
        arguments = params.get("arguments")
        roles = self._roles()

        decision = self.policy.decide_tool(tool, roles)
        audit.record(
            principal=self._principal(),
            method="tools/call",
            decision=decision,
            arguments=arguments if isinstance(arguments, Mapping) else None,
            protocol_version=getattr(ctx, "protocol_version", None),
        )
        self._raise_if_denied(decision)
        return await call_next(ctx)

    async def _guard_resource_read(self, ctx: Any, call_next: Any) -> Any:
        params = _params(ctx)
        uri = str(params.get("uri", ""))
        roles = self._roles()

        decision = self.policy.decide_resource(uri, roles)
        audit.record(
            principal=self._principal(),
            method="resources/read",
            decision=decision,
            protocol_version=getattr(ctx, "protocol_version", None),
        )
        self._raise_if_denied(decision)
        return await call_next(ctx)

    async def _filter_tool_list(self, ctx: Any, call_next: Any) -> Any:
        result = await call_next(ctx)
        if not self.enforce:
            return result

        visible = self.policy.visible_tools(self._roles())

        # The dispatcher serializes handler results before the middleware chain
        # unwinds, so by here `tools/list` is a plain mapping rather than a
        # `ListToolsResult`. Both shapes are handled: the model form is what an
        # in-process handler would return if that ever changes, and pinning
        # this to one of them would break silently on an SDK bump.
        if isinstance(result, ListToolsResult):
            kept_models = [t for t in result.tools if t.name in visible]
            if len(kept_models) == len(result.tools):
                return result
            return result.model_copy(update={"tools": kept_models})

        if isinstance(result, Mapping) and isinstance(result.get("tools"), list):
            kept = [t for t in result["tools"] if _tool_name(t) in visible]
            if len(kept) == len(result["tools"]):
                return result
            return {**result, "tools": kept}

        return result

    # -- helpers ----------------------------------------------------------

    def _roles(self) -> list[str]:
        return roles_from_token(get_access_token())

    def _principal(self) -> str:
        return describe_principal(get_access_token())

    def _raise_if_denied(self, decision: Decision) -> None:
        if decision.allowed or not self.enforce:
            return
        # The message names what was required, which is actionable for a
        # legitimate caller requesting access, and discloses nothing a caller
        # holding any valid token could not read from the policy anyway.
        raise MCPError(
            AUTHORIZATION_DENIED,
            f"Access denied for {decision.target!r}: {decision.reason}.",
            {
                "target": decision.target,
                "required_roles": list(decision.required_roles),
                "classification": decision.classification,
            },
        )


def _params(ctx: Any) -> Mapping[str, Any]:
    params = getattr(ctx, "params", None)
    return params if isinstance(params, Mapping) else {}


def _tool_name(tool: Any) -> str:
    if isinstance(tool, Mapping):
        return str(tool.get("name", ""))
    return str(getattr(tool, "name", ""))
