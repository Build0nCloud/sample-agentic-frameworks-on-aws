"""agentcore_demo — end-to-end Amazon Bedrock AgentCore optimization loop demo.

This package orchestrates the AgentCore agent-quality loop against **real**
AgentCore services (Runtime, Gateway, Evaluations, Optimization):

    offline baseline -> deploy/online eval -> recommendation + bundle ->
    offline check -> A/B test -> promotion (human approval) -> new baseline

Modules map to the design's components:
    agent/                healthcare patient-support agent + tool suite
    agentcore_client      single thin service layer over real AgentCore
    evaluation/           offline (batch) + online evaluation + evaluators
    optimization/         recommendations, bundles, A/B testing, promotion gate
    reporting/            evaluation-report email delivery (+ outbox fallback)
    orchestrator, demo    stage runner and CLI entry point
    config, state         centralized config and artifact/state persistence
"""

__version__ = "0.1.0"
