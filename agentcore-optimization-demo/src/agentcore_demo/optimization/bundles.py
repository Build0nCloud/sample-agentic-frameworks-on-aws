"""Configuration bundle management.

A configuration bundle is a versioned, immutable snapshot of agent configuration
(system prompt, model id, tool descriptions). :class:`BundleManager` creates and lists
versions, tracks which version is the current baseline, and records parent lineage on
each update. Selecting a version as a variant makes the runtime serve it via the
config-bundle hook (no code deploy).

Immutability & lineage (Property 8 / Requirement 4.4): creating a new version never
mutates prior versions, and every update records its parent version id(s). The manager
keeps independent (deep-copied) snapshots so stored versions are never mutated by later
operations, mirroring AgentCore's immutable versioning.

Requirements: 4.3, 4.4, 4.5, 4.6, 4.7, 9.7
"""

from __future__ import annotations

import copy
from typing import Sequence

from ..agentcore_client import BundleRef
from ..models import BundleConfig, BundleVersion


class BundleManager:
    """Creates/lists immutable bundle versions and tracks the baseline version."""

    def __init__(self, client, agent_arn: str):
        self.client = client
        self.agent_arn = agent_arn
        self._versions: dict[str, list[BundleVersion]] = {}   # bundle_id -> ordered versions
        self._arns: dict[str, str] = {}                        # bundle_id -> bundle_arn
        self._baseline: dict[str, str] = {}                    # bundle_id -> baseline version_id

    # --- creation / update ------------------------------------------------
    def create(self, bundle_name: str, config: BundleConfig, commit_message: str, description: str = "") -> BundleVersion:
        """Create the first version of a bundle; establishes the baseline."""
        bv = self.client.create_bundle_version(bundle_name, self.agent_arn, config, commit_message, description)
        self._register(bv, set_baseline=True)
        return bv

    def update(self, bundle_id: str, config: BundleConfig, commit_message: str, parent_version_ids: Sequence[str] | None = None) -> BundleVersion:
        """Create a new immutable version; records parent lineage and moves the baseline.

        Prior versions are never mutated (Property 8): the new version is appended and
        references its parent(s). If ``parent_version_ids`` is omitted, the current
        baseline version is used as the parent.
        """
        parents = list(parent_version_ids) if parent_version_ids is not None else self._default_parents(bundle_id)
        bv = self.client.update_bundle_version(bundle_id, self.agent_arn, config, parents, commit_message)
        self._register(bv, set_baseline=True)
        return bv

    def _default_parents(self, bundle_id: str) -> list[str]:
        current = self.current_version(bundle_id)
        return [current] if current else []

    def _register(self, bv: BundleVersion, set_baseline: bool) -> None:
        # Store an independent snapshot so later operations can never mutate it.
        snapshot = copy.deepcopy(bv)
        self._versions.setdefault(snapshot.bundle_id, []).append(snapshot)
        if snapshot.bundle_arn:
            self._arns[snapshot.bundle_id] = snapshot.bundle_arn
        if set_baseline:
            self._baseline[snapshot.bundle_id] = snapshot.version_id

    # --- queries ----------------------------------------------------------
    def versions(self, bundle_id: str) -> list[BundleVersion]:
        """Return immutable snapshots of all known versions for a bundle (in order)."""
        return [copy.deepcopy(v) for v in self._versions.get(bundle_id, [])]

    def current_version(self, bundle_id: str) -> str | None:
        """The version id currently serving as the baseline (Requirement 4.5)."""
        return self._baseline.get(bundle_id)

    def set_baseline(self, bundle_id: str, version_id: str) -> None:
        known = {v.version_id for v in self._versions.get(bundle_id, [])}
        if version_id not in known:
            raise ValueError(f"version {version_id!r} is not a known version of bundle {bundle_id!r}")
        self._baseline[bundle_id] = version_id

    def bundle_arn(self, bundle_id: str) -> str | None:
        return self._arns.get(bundle_id)

    def ref(self, bundle_id: str, version_id: str | None = None) -> BundleRef:
        """A BundleRef for serving a specific version as an A/B variant (Requirement 4.6)."""
        version_id = version_id or self.current_version(bundle_id)
        if version_id is None:
            raise ValueError(f"no version selected for bundle {bundle_id!r}")
        arn = self._arns.get(bundle_id)
        if arn is None:
            raise ValueError(f"unknown bundle_arn for bundle {bundle_id!r}")
        return BundleRef(bundle_arn=arn, version_id=version_id, bundle_id=bundle_id)

    def lineage(self, bundle_id: str) -> list[tuple[str, list[str]]]:
        """Return (version_id, parent_version_ids) for each version in order."""
        return [(v.version_id, list(v.parent_version_ids)) for v in self._versions.get(bundle_id, [])]


def offline_check_bundle(client, agent, bundle_ref: BundleRef, cases, name: str, store=None, ingestion_wait: float = 0.0):
    """Run an offline evaluation against a candidate bundle before online testing (R4.7).

    Sends each case's turns with the candidate bundle injected, then (after CloudWatch
    ingestion) reconstructs tool calls and scores/aggregates via the offline pipeline.
    """
    from ..evaluation.offline import make_client_runner, run_offline_evaluation

    runner = make_client_runner(client, agent, bundle=bundle_ref)
    resolver = (lambda sid: client.fetch_session_tool_calls(sid, agent.spans_log_group)) if ingestion_wait else None
    return run_offline_evaluation(
        name, cases, runner, config_ref=f"bundle:{bundle_ref.version_id}", store=store,
        tool_call_resolver=resolver, ingestion_wait=ingestion_wait,
    )
