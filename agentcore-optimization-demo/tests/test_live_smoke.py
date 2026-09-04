"""Opt-in live smoke test against REAL Amazon Bedrock AgentCore (task 17.3).

This is the only test that touches real AWS. It is marked ``live`` (excluded by the
default ``-m 'not live'`` addopts) AND additionally guarded by the ``RUN_LIVE``
environment variable, so it never runs by default / in CI (avoiding cost).

Run it explicitly with real AWS credentials configured:

    RUN_LIVE=1 pytest -m live tests/test_live_smoke.py

It deploys the patient-support agent, drives one multi-turn session, asserts a
non-empty response, then tears the runtime down. For the full loop, use the CLI:
``demo --yes run-all`` and then ``demo approve <id>`` / ``demo reject <id>``.
"""

import os

import pytest

pytestmark = pytest.mark.live


def _skip_unless_live():
    if not os.environ.get("RUN_LIVE"):
        pytest.skip("Set RUN_LIVE=1 and configure AWS credentials to run the live smoke test.")


def test_live_deploy_invoke_and_teardown():
    _skip_unless_live()
    from agentcore_demo.agentcore_client import AgentCoreClient
    from agentcore_demo.config import load_config

    config = load_config()
    client = AgentCoreClient(config)
    agent = None
    try:
        agent = client.deploy_agent(f"{config.runtime_name}Smoke", "v1")
        result = client.send_session(
            agent,
            ["I'm patient PT-1001. What medications am I on?", "Please refill the Lisinopril."],
            session_id="live-smoke-1",
        )
        assert result.final_response  # agent responded
    finally:
        if agent is not None:
            try:
                client.delete_runtime(agent.runtime_id)
            except Exception as exc:  # noqa: BLE001 - teardown is best-effort
                print(f"teardown warning: {exc}")
