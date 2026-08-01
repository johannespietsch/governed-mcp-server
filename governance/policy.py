"""Declarative authorization policy.

Loads `policy.yaml` and answers one question: may a caller holding this set of
Entra app roles invoke this tool or read this resource?

Two properties are deliberate:

* **Fail closed.** A tool with no policy entry is denied. Adding a tool without
  making an authorization decision about it makes the tool unreachable, which
  is a visible bug, rather than public, which is a silent one.
* **Fail fast.** Every role a rule references must be declared in `roles:`. A
  typo in a role name stops the server from starting instead of producing a
  rule that can never match — a rule that never matches is invisible in
  testing and looks like a working deny.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# Ordered least to most sensitive. Recorded per rule so audit, and later
# redaction, are driven from the same declaration as authorization.
CLASSIFICATIONS = ("public", "internal", "confidential", "restricted")


class PolicyError(Exception):
    """Raised when a policy document is malformed or internally inconsistent."""


@dataclass(frozen=True)
class Decision:
    """The outcome of one authorization check, shaped for the audit log."""

    allowed: bool
    reason: str
    target: str
    classification: str | None = None
    required_roles: tuple[str, ...] = ()
    granted_roles: tuple[str, ...] = ()
    # Authorization says the caller *may* do this. Approval says a human has
    # agreed to this particular instance of it. They are different questions,
    # so a tool can require both, and both are declared in the same document.
    requires_approval: bool = False
    approval_prompt: str = ""


@dataclass(frozen=True)
class Rule:
    pattern: str
    allow_roles: frozenset[str]
    classification: str
    description: str = ""
    requires_approval: bool = False
    approval_prompt: str = ""


@dataclass(frozen=True)
class Policy:
    tools: Mapping[str, Rule]
    resources: Mapping[str, Rule]
    declared_roles: frozenset[str]
    source: str = "<memory>"

    # -- loading ---------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> Policy:
        path = Path(path)
        try:
            raw = yaml.safe_load(path.read_text())
        except FileNotFoundError as exc:
            raise PolicyError(f"policy file not found: {path}") from exc
        except yaml.YAMLError as exc:
            raise PolicyError(f"policy file is not valid YAML: {path}: {exc}") from exc
        return cls.from_mapping(raw, source=str(path))

    @classmethod
    def from_mapping(cls, raw: Any, source: str = "<memory>") -> Policy:
        if not isinstance(raw, Mapping):
            raise PolicyError(f"{source}: policy must be a mapping")

        if raw.get("version") != 1:
            raise PolicyError(f"{source}: unsupported policy version {raw.get('version')!r}, expected 1")

        # Only `deny` is accepted. An explicit allow-by-default would make the
        # fail-closed guarantee a configuration option, and it is not one.
        if raw.get("default", "deny") != "deny":
            raise PolicyError(f"{source}: `default` must be `deny`; allow-by-default is not supported")

        declared = cls._parse_roles(raw.get("roles"), source)
        tools = cls._parse_rules(raw.get("tools"), declared, source, section="tools")
        resources = cls._parse_rules(raw.get("resources"), declared, source, section="resources")

        return cls(tools=tools, resources=resources, declared_roles=declared, source=source)

    @staticmethod
    def _parse_roles(raw: Any, source: str) -> frozenset[str]:
        if raw is None:
            return frozenset()
        if not isinstance(raw, Mapping):
            raise PolicyError(f"{source}: `roles` must be a mapping of role name to metadata")
        return frozenset(str(name) for name in raw)

    @staticmethod
    def _parse_rules(
        raw: Any,
        declared: frozenset[str],
        source: str,
        *,
        section: str,
    ) -> dict[str, Rule]:
        if raw is None:
            return {}
        if not isinstance(raw, Mapping):
            raise PolicyError(f"{source}: `{section}` must be a mapping")

        rules: dict[str, Rule] = {}
        for pattern, body in raw.items():
            pattern = str(pattern)
            if not isinstance(body, Mapping):
                raise PolicyError(f"{source}: `{section}.{pattern}` must be a mapping")

            classification = str(body.get("classification", "internal"))
            if classification not in CLASSIFICATIONS:
                raise PolicyError(
                    f"{source}: `{section}.{pattern}` has unknown classification {classification!r}; "
                    f"expected one of {', '.join(CLASSIFICATIONS)}"
                )

            allow_roles = body.get("allow_roles") or []
            if not isinstance(allow_roles, list):
                raise PolicyError(f"{source}: `{section}.{pattern}.allow_roles` must be a list")
            allow_roles = [str(r) for r in allow_roles]

            # The fail-fast check. An undeclared role can never appear in a
            # token we accept, so a rule referencing one is dead weight that
            # reads like a working grant.
            unknown = sorted(set(allow_roles) - declared)
            if unknown:
                raise PolicyError(
                    f"{source}: `{section}.{pattern}` references undeclared "
                    f"role(s): {', '.join(unknown)}. Add them under `roles:` or fix the spelling."
                )

            rules[pattern] = Rule(
                pattern=pattern,
                allow_roles=frozenset(allow_roles),
                classification=classification,
                description=str(body.get("description", "")),
                requires_approval=bool(body.get("requires_approval", False)),
                approval_prompt=str(body.get("approval_prompt", "")),
            )
        return rules

    # -- decisions -------------------------------------------------------

    def decide_tool(self, tool: str, roles: Iterable[str]) -> Decision:
        granted = tuple(sorted(set(roles)))
        rule = self.tools.get(tool)
        if rule is None:
            return Decision(
                allowed=False,
                reason="no policy entry for this tool (default deny)",
                target=tool,
                granted_roles=granted,
            )
        return self._match(rule, tool, granted)

    def decide_resource(self, uri: str, roles: Iterable[str]) -> Decision:
        granted = tuple(sorted(set(roles)))
        rule = self._resource_rule(uri)
        if rule is None:
            return Decision(
                allowed=False,
                reason="no policy entry for this resource (default deny)",
                target=uri,
                granted_roles=granted,
            )
        return self._match(rule, uri, granted)

    def _resource_rule(self, uri: str) -> Rule | None:
        # Longest pattern first, so `shipment://eu/*` wins over `shipment://*`
        # regardless of the order they appear in the file.
        for pattern in sorted(self.resources, key=len, reverse=True):
            if fnmatch.fnmatchcase(uri, pattern):
                return self.resources[pattern]
        return None

    @staticmethod
    def _match(rule: Rule, target: str, granted: tuple[str, ...]) -> Decision:
        required = tuple(sorted(rule.allow_roles))
        allowed = bool(set(granted) & rule.allow_roles)
        return Decision(
            allowed=allowed,
            reason="granted by role" if allowed else "caller holds none of the roles this tool requires",
            target=target,
            classification=rule.classification,
            required_roles=required,
            granted_roles=granted,
            requires_approval=rule.requires_approval,
            approval_prompt=rule.approval_prompt,
        )

    def visible_tools(self, roles: Iterable[str]) -> set[str]:
        """Tools the caller may invoke, used to filter discovery.

        Listing a tool the caller cannot call is a small information leak — it
        discloses the shape of systems they have no access to — and it invites
        an agent to plan around a call that will always be refused.
        """
        granted = set(roles)
        return {name for name, rule in self.tools.items() if granted & rule.allow_roles}
