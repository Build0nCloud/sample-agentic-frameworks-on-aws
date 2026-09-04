"""Optional Streamlit front end for the AgentCore optimization demo.

Provides a web UI over the ``artifacts/`` tree for visualizing A/B results and the
audit trail, plus a human-approval panel that drives the same :class:`PromotionGate`
approve/reject code paths as the ``demo`` CLI.

Install with the optional extra and launch with the ``demo-ui`` script::

    pip install -e ".[ui]"
    demo-ui
"""
