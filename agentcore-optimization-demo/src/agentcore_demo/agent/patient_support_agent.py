"""Healthcare patient-support agent — Strands agent on Bedrock AgentCore Runtime.

Adapted from the AWS sample HR assistant
(reference/.../03-optimize/utils/hr_assistant_agent.py) and re-themed to a
healthcare patient-support assistant (Requirement 10). It keeps the sample's
structural patterns:

  * a per-session agent cache so multi-turn conversation history is preserved
    across turns within a session (Requirements 1.3-1.5);
  * a configuration-bundle runtime hook that overrides the system prompt and
    per-tool descriptions at invocation time, enabling bundle-based A/B testing
    without redeployment (Requirements 4.6, 9.7).

Safety (Requirement 10): all data is synthetic and non-identifiable (no PHI); the
agent is scoped to administrative/informational tasks and is instructed to decline
clinical advice and defer to licensed professionals.

Tool suite (>=5 deterministic tools, Requirement 9) with two designed trajectories:
  refill:     get_patient_profile -> get_medications -> request_prescription_refill
  scheduling: find_provider -> check_coverage -> schedule_appointment

Design note on testability: the deterministic tool *logic*, the mock data, the
session cache, and the bundle-resolution helper are plain Python and importable
without the ``strands`` / ``bedrock_agentcore`` packages. The framework-dependent
agent, app, and entrypoint are built only when those packages are available (e.g.
when deployed to AgentCore Runtime).
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-5"

# ---------------------------------------------------------------------------
# System prompt (Requirement 10: administrative/informational scope, no clinical
# advice, defer to licensed professionals).
# ---------------------------------------------------------------------------
DEFAULT_SYSTEM_PROMPT = """You are a patient-support assistant for Northstar Health, a fictional clinic and health plan.

You help patients with administrative and informational tasks only:
- Looking up a patient's profile and plan on file
- Finding in-network providers by specialty
- Checking insurance coverage for a service
- Scheduling appointments
- Listing medications on file and requesting prescription refills
- Retrieving lab results the patient already has on file
- Explaining clinic and plan policies (billing, privacy, refills, cancellations)

Rules:
- Always use the available tools to look up real data. Never invent patient details,
  coverage amounts, medications, lab values, or policy text.
- Verify a record exists before acting on it: confirm a medication is on file
  (get_medications) before requesting a refill, and verify a provider is in-network
  and the service is covered (find_provider, check_coverage) before scheduling.
- You do NOT provide medical diagnoses, treatment recommendations, medication dosing
  advice, or any other clinical advice. If asked for clinical guidance (for example,
  whether to change a dose or whether a symptom is serious), politely decline and
  advise the patient to contact a licensed clinician; for emergencies, direct them to
  call 911.
- Be concise, professional, and empathetic.
"""

# Designed tool trajectories (used by datasets and trajectory evaluators).
TRAJECTORY_REFILL = ["get_patient_profile", "get_medications", "request_prescription_refill"]
TRAJECTORY_SCHEDULING = ["find_provider", "check_coverage", "schedule_appointment"]

# ---------------------------------------------------------------------------
# Deterministic mock data (synthetic, non-PHI). Reproducible across eval runs
# (Requirement 9.2) and kept local to the demo (Requirement 10.5).
# ---------------------------------------------------------------------------
_PATIENTS: dict[str, dict[str, Any]] = {
    "PT-1001": {"name": "Alex Rivera", "dob": "1984-03-12", "plan_id": "PLN-GOLD", "pcp_provider_id": "PRV-204"},
    "PT-1002": {"name": "Jordan Lee", "dob": "1991-07-25", "plan_id": "PLN-SILVER", "pcp_provider_id": "PRV-204"},
    "PT-1003": {"name": "Sam Okafor", "dob": "1976-11-02", "plan_id": "PLN-BRONZE", "pcp_provider_id": "PRV-204"},
}

_PROVIDERS: list[dict[str, Any]] = [
    {"provider_id": "PRV-201", "name": "Dr. Maya Chen", "specialty": "cardiology", "in_network": True},
    {"provider_id": "PRV-202", "name": "Dr. Sam Patel", "specialty": "cardiology", "in_network": False},
    {"provider_id": "PRV-203", "name": "Dr. Lee Grant", "specialty": "dermatology", "in_network": True},
    {"provider_id": "PRV-204", "name": "Dr. Nina Alvarez", "specialty": "primary care", "in_network": True},
]

# Medications on file per patient (PT-1003 intentionally has none).
_MEDICATIONS: dict[str, list[dict[str, Any]]] = {
    "PT-1001": [
        {"medication_id": "MED-01", "name": "Lisinopril 10mg", "refills_remaining": 2},
        {"medication_id": "MED-02", "name": "Atorvastatin 20mg", "refills_remaining": 1},
    ],
    "PT-1002": [
        {"medication_id": "MED-03", "name": "Metformin 500mg", "refills_remaining": 0},
    ],
    "PT-1003": [],
}

# Coverage keyed by (plan_id, service_code).
_SERVICE_CODES = ("OFFICE_VISIT", "SPECIALIST_VISIT", "LAB_PANEL", "MRI")
_COVERAGE: dict[tuple[str, str], dict[str, Any]] = {
    ("PLN-GOLD", "OFFICE_VISIT"): {"covered": True, "copay_usd": 20, "coinsurance_pct": 0},
    ("PLN-GOLD", "SPECIALIST_VISIT"): {"covered": True, "copay_usd": 40, "coinsurance_pct": 0},
    ("PLN-GOLD", "LAB_PANEL"): {"covered": True, "copay_usd": 0, "coinsurance_pct": 10},
    ("PLN-GOLD", "MRI"): {"covered": True, "copay_usd": 0, "coinsurance_pct": 20},
    ("PLN-SILVER", "OFFICE_VISIT"): {"covered": True, "copay_usd": 35, "coinsurance_pct": 0},
    ("PLN-SILVER", "SPECIALIST_VISIT"): {"covered": True, "copay_usd": 60, "coinsurance_pct": 0},
    ("PLN-SILVER", "LAB_PANEL"): {"covered": True, "copay_usd": 15, "coinsurance_pct": 20},
    ("PLN-SILVER", "MRI"): {"covered": False, "copay_usd": 0, "coinsurance_pct": 0},
    ("PLN-BRONZE", "OFFICE_VISIT"): {"covered": True, "copay_usd": 50, "coinsurance_pct": 0},
    ("PLN-BRONZE", "SPECIALIST_VISIT"): {"covered": True, "copay_usd": 90, "coinsurance_pct": 0},
    ("PLN-BRONZE", "LAB_PANEL"): {"covered": False, "copay_usd": 0, "coinsurance_pct": 0},
    ("PLN-BRONZE", "MRI"): {"covered": False, "copay_usd": 0, "coinsurance_pct": 0},
}

_LAB_RESULTS: dict[tuple[str, str], dict[str, Any]] = {
    ("PT-1001", "lipid"): {"panel": "lipid", "collected": "2026-01-15", "results": {"total_cholesterol": 182, "ldl": 101, "hdl": 55}},
    ("PT-1001", "a1c"): {"panel": "a1c", "collected": "2026-01-15", "results": {"hba1c_pct": 5.6}},
    ("PT-1002", "a1c"): {"panel": "a1c", "collected": "2026-02-03", "results": {"hba1c_pct": 7.1}},
}

_HEALTH_POLICIES: dict[str, str] = {
    "billing": (
        "Billing Policy: Statements are issued monthly. Copays are due at the time of service. "
        "Patients may request an itemized bill and set up a payment plan for balances over $200."
    ),
    "privacy": (
        "Privacy Policy: Northstar Health protects patient information and shares it only for "
        "treatment, payment, and healthcare operations, or with the patient's written authorization."
    ),
    "prescription_refill": (
        "Prescription Refill Policy: Refills require an active medication on file. When no refills "
        "remain, the prescribing provider must authorize a renewal, which can take up to 2 business days."
    ),
    "appointment_cancellation": (
        "Appointment Cancellation Policy: Please cancel or reschedule at least 24 hours in advance. "
        "Late cancellations may incur a $25 fee."
    ),
}

_APPT_COUNTER = {"n": 0}
_REFILL_COUNTER = {"n": 0}


# ---------------------------------------------------------------------------
# Pure tool implementations (return structured dicts; never raise for bad input,
# Requirement 9.8). These are the source of truth; the strands @tool wrappers
# below simply call them.
# ---------------------------------------------------------------------------
def _get_patient_profile(patient_id: str) -> dict[str, Any]:
    profile = _PATIENTS.get(patient_id)
    if not profile:
        return {"error": f"Patient '{patient_id}' not found.", "patient_id": patient_id}
    return {"patient_id": patient_id, **profile}


def _find_provider(specialty: str, in_network_only: bool = True) -> dict[str, Any]:
    spec = (specialty or "").strip().lower()
    matches = [
        p for p in _PROVIDERS
        if p["specialty"] == spec and (p["in_network"] or not in_network_only)
    ]
    return {
        "specialty": specialty,
        "in_network_only": in_network_only,
        "providers": matches,
        "count": len(matches),
        "note": None if matches else f"No {'in-network ' if in_network_only else ''}providers found for specialty '{specialty}'.",
    }


def _check_coverage(patient_id: str, service_code: str) -> dict[str, Any]:
    profile = _PATIENTS.get(patient_id)
    if not profile:
        return {"error": f"Patient '{patient_id}' not found.", "patient_id": patient_id}
    code = (service_code or "").strip().upper()
    if code not in _SERVICE_CODES:
        return {
            "error": f"Unknown service_code '{service_code}'.",
            "available_service_codes": list(_SERVICE_CODES),
            "patient_id": patient_id,
        }
    coverage = _COVERAGE.get((profile["plan_id"], code))
    if not coverage:
        return {"patient_id": patient_id, "service_code": code, "covered": False, "note": "No coverage record for this plan/service."}
    return {"patient_id": patient_id, "plan_id": profile["plan_id"], "service_code": code, **coverage}


def _schedule_appointment(patient_id: str, provider_id: str, date: str) -> dict[str, Any]:
    profile = _PATIENTS.get(patient_id)
    if not profile:
        return {"error": f"Patient '{patient_id}' not found.", "patient_id": patient_id}
    provider = next((p for p in _PROVIDERS if p["provider_id"] == provider_id), None)
    if not provider:
        return {"error": f"Provider '{provider_id}' not found.", "provider_id": provider_id}
    if not provider["in_network"]:
        return {
            "error": f"Provider '{provider_id}' ({provider['name']}) is out of network; scheduling was not completed.",
            "provider_id": provider_id,
            "suggestion": "Choose an in-network provider (see find_provider with in_network_only=true).",
        }
    _APPT_COUNTER["n"] += 1
    appt_id = f"APPT-2026-{_APPT_COUNTER['n']:03d}"
    return {
        "appointment_id": appt_id,
        "patient_id": patient_id,
        "provider_id": provider_id,
        "provider_name": provider["name"],
        "date": date,
        "status": "SCHEDULED",
        "message": f"Appointment {appt_id} scheduled with {provider['name']} on {date}.",
    }


def _get_medications(patient_id: str) -> dict[str, Any]:
    if patient_id not in _PATIENTS:
        return {"error": f"Patient '{patient_id}' not found.", "patient_id": patient_id}
    meds = _MEDICATIONS.get(patient_id, [])
    return {"patient_id": patient_id, "medications": meds, "count": len(meds)}


def _request_prescription_refill(patient_id: str, medication_id: str) -> dict[str, Any]:
    if patient_id not in _PATIENTS:
        return {"error": f"Patient '{patient_id}' not found.", "patient_id": patient_id}
    meds = _MEDICATIONS.get(patient_id, [])
    med = next((mm for mm in meds if mm["medication_id"] == medication_id), None)
    if not med:
        return {
            "error": f"Medication '{medication_id}' is not on file for patient '{patient_id}'.",
            "patient_id": patient_id,
            "medication_id": medication_id,
            "suggestion": "Call get_medications first to see the patient's medications on file.",
        }
    if med["refills_remaining"] <= 0:
        return {
            "patient_id": patient_id,
            "medication_id": medication_id,
            "medication_name": med["name"],
            "status": "PENDING_PROVIDER_AUTHORIZATION",
            "message": (
                f"No refills remain for {med['name']}. A renewal request was sent to the prescribing "
                "provider for authorization (up to 2 business days)."
            ),
        }
    _REFILL_COUNTER["n"] += 1
    refill_id = f"RX-2026-{_REFILL_COUNTER['n']:03d}"
    return {
        "refill_id": refill_id,
        "patient_id": patient_id,
        "medication_id": medication_id,
        "medication_name": med["name"],
        "status": "SUBMITTED",
        "message": f"Refill {refill_id} submitted for {med['name']}.",
    }


def _get_lab_results(patient_id: str, panel: str) -> dict[str, Any]:
    if patient_id not in _PATIENTS:
        return {"error": f"Patient '{patient_id}' not found.", "patient_id": patient_id}
    key = (patient_id, (panel or "").strip().lower())
    result = _LAB_RESULTS.get(key)
    if not result:
        available = sorted(p for (pid, p) in _LAB_RESULTS if pid == patient_id)
        return {
            "error": f"No '{panel}' lab results on file for patient '{patient_id}'.",
            "patient_id": patient_id,
            "available_panels": available,
        }
    return {"patient_id": patient_id, **result}


def _lookup_health_policy(topic: str) -> dict[str, Any]:
    key = (topic or "").strip().lower().replace(" ", "_").replace("-", "_")
    text = _HEALTH_POLICIES.get(key)
    if not text:
        return {"error": f"Policy '{topic}' not found.", "available_topics": list(_HEALTH_POLICIES.keys())}
    return {"topic": topic, "policy_text": text}


# Registry of tool name -> pure implementation (used by wrappers and tests).
TOOL_IMPLS: dict[str, Callable[..., dict[str, Any]]] = {
    "get_patient_profile": _get_patient_profile,
    "find_provider": _find_provider,
    "check_coverage": _check_coverage,
    "schedule_appointment": _schedule_appointment,
    "get_medications": _get_medications,
    "request_prescription_refill": _request_prescription_refill,
    "get_lab_results": _get_lab_results,
    "lookup_health_policy": _lookup_health_policy,
}

TOOL_NAMES = tuple(TOOL_IMPLS.keys())

# Default, bundle-overridable tool descriptions (Requirements 9.1, 9.7). A
# tool-description recommendation packaged into a bundle overrides these at runtime.
DEFAULT_TOOL_DESCRIPTIONS: dict[str, str] = {
    "get_patient_profile": "Look up a patient's profile and plan on file by patient_id (e.g. PT-1001). Call this first for patient-specific tasks.",
    "find_provider": "Find providers by specialty (e.g. 'cardiology'). Set in_network_only=true to restrict to in-network providers. Use before scheduling.",
    "check_coverage": "Check whether a service_code (OFFICE_VISIT, SPECIALIST_VISIT, LAB_PANEL, MRI) is covered for a patient's plan, including copay and coinsurance. Verify before scheduling.",
    "schedule_appointment": "Schedule an appointment for a patient with an in-network provider on a date (YYYY-MM-DD). Verify the provider is in-network and the service is covered first.",
    "get_medications": "List the medications currently on file for a patient. Call this before requesting a refill.",
    "request_prescription_refill": "Request a refill for a medication_id that is on file for the patient. Confirm the medication with get_medications first.",
    "get_lab_results": "Retrieve a patient's lab results for a panel (e.g. 'lipid', 'a1c') that is already on file.",
    "lookup_health_policy": "Look up a clinic/plan policy document by topic (billing, privacy, prescription_refill, appointment_cancellation).",
}


# ---------------------------------------------------------------------------
# Pure helpers shared by the runtime entrypoint and by tests.
# ---------------------------------------------------------------------------
def resolve_bundle_config(bundle: dict[str, Any] | None) -> tuple[str, dict[str, str]]:
    """Resolve (system_prompt, tool_descriptions) from a configuration bundle.

    Falls back to the defaults when no bundle (or key) is present. This is the pure
    core of the runtime config-bundle hook (Requirements 4.6, 9.7).
    """
    system_prompt = DEFAULT_SYSTEM_PROMPT
    tool_descs: dict[str, str] = {}
    if bundle:
        system_prompt = bundle.get("system_prompt", DEFAULT_SYSTEM_PROMPT)
        tool_descs = dict(bundle.get("tool_descriptions", {}) or {})
    return system_prompt, tool_descs


class SessionAgentCache:
    """Caches one agent per session_id so multi-turn history is preserved.

    Returns the *same* agent instance for a given session_id (so the underlying
    conversation history accumulates across turns, Requirements 1.3-1.5) and a new
    instance for a new session_id. Sessionless calls (no id) are never cached.
    """

    def __init__(self) -> None:
        self._agents: dict[str, Any] = {}

    def get_or_create(self, session_id: str | None, factory: Callable[[], Any]) -> tuple[Any, bool]:
        """Return (agent, created) for ``session_id``."""
        if session_id and session_id in self._agents:
            return self._agents[session_id], False
        agent = factory()
        if session_id:
            self._agents[session_id] = agent
        return agent, True

    def __contains__(self, session_id: str) -> bool:
        return session_id in self._agents

    def __len__(self) -> int:
        return len(self._agents)


def apply_tool_descriptions(tool_specs: dict[str, dict[str, Any]], overrides: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Apply description overrides onto a mapping of tool_name -> tool_spec.

    Pure/testable form of the runtime override loop. Only known tools with a
    ``description`` field are updated; unknown names are ignored.
    """
    for name, desc in (overrides or {}).items():
        spec = tool_specs.get(name)
        if spec is not None and "description" in spec:
            spec["description"] = desc
    return tool_specs


# ---------------------------------------------------------------------------
# Framework-dependent agent, app, and entrypoint.
# Built only when strands + bedrock_agentcore are importable (e.g. on the
# AgentCore Runtime). Kept import-safe for local tests without those packages.
# ---------------------------------------------------------------------------
try:  # pragma: no cover - exercised only in a deployed/runtime environment
    from bedrock_agentcore.runtime import BedrockAgentCoreApp, BedrockAgentCoreContext
    from strands import Agent, tool
    from strands.models import BedrockModel

    _HAS_FRAMEWORK = True
except Exception:  # ImportError (packages absent) or any transitive import error
    _HAS_FRAMEWORK = False


if _HAS_FRAMEWORK:  # pragma: no cover - requires strands + bedrock_agentcore + AWS
    app = BedrockAgentCoreApp()

    @tool
    def get_patient_profile(patient_id: str) -> dict:
        """Look up a patient's profile and plan on file.

        Args:
            patient_id: Patient identifier (e.g. PT-1001). Supported: PT-1001, PT-1002, PT-1003.
        """
        return _get_patient_profile(patient_id)

    @tool
    def find_provider(specialty: str, in_network_only: bool = True) -> dict:
        """Find providers by specialty (e.g. cardiology, dermatology, primary care).

        Args:
            specialty: Clinical specialty to search for.
            in_network_only: If true (default), return only in-network providers.
        """
        return _find_provider(specialty, in_network_only)

    @tool
    def check_coverage(patient_id: str, service_code: str) -> dict:
        """Check insurance coverage for a service under the patient's plan.

        Args:
            patient_id: Patient identifier (e.g. PT-1001).
            service_code: One of OFFICE_VISIT, SPECIALIST_VISIT, LAB_PANEL, MRI.
        """
        return _check_coverage(patient_id, service_code)

    @tool
    def schedule_appointment(patient_id: str, provider_id: str, date: str) -> dict:
        """Schedule an appointment with an in-network provider.

        Args:
            patient_id: Patient identifier (e.g. PT-1001).
            provider_id: Provider identifier from find_provider (e.g. PRV-201).
            date: Appointment date in YYYY-MM-DD format.
        """
        return _schedule_appointment(patient_id, provider_id, date)

    @tool
    def get_medications(patient_id: str) -> dict:
        """List the medications currently on file for a patient.

        Args:
            patient_id: Patient identifier (e.g. PT-1001).
        """
        return _get_medications(patient_id)

    @tool
    def request_prescription_refill(patient_id: str, medication_id: str) -> dict:
        """Request a refill for a medication that is on file for the patient.

        Args:
            patient_id: Patient identifier (e.g. PT-1001).
            medication_id: Medication identifier from get_medications (e.g. MED-01).
        """
        return _request_prescription_refill(patient_id, medication_id)

    @tool
    def get_lab_results(patient_id: str, panel: str) -> dict:
        """Retrieve a patient's lab results for a panel already on file.

        Args:
            patient_id: Patient identifier (e.g. PT-1001).
            panel: Lab panel name (e.g. lipid, a1c).
        """
        return _get_lab_results(patient_id, panel)

    @tool
    def lookup_health_policy(topic: str) -> dict:
        """Look up a clinic/plan policy document by topic.

        Args:
            topic: One of billing, privacy, prescription_refill, appointment_cancellation.
        """
        return _lookup_health_policy(topic)

    _MODEL = BedrockModel(model_id=DEFAULT_MODEL_ID)
    _TOOLS = [
        get_patient_profile,
        find_provider,
        check_coverage,
        schedule_appointment,
        get_medications,
        request_prescription_refill,
        get_lab_results,
        lookup_health_policy,
    ]

    # Session cache: session_id -> Agent (preserves conversation history across turns).
    _SESSION_CACHE = SessionAgentCache()

    def _build_agent(system_prompt: str) -> "Agent":
        return Agent(model=_MODEL, tools=_TOOLS, system_prompt=system_prompt)

    def _apply_bundle_to_agent(agent: "Agent", tool_descs: dict[str, str]) -> None:
        if not tool_descs:
            return
        for t in agent.tool_registry.registry.values():
            name = getattr(t, "tool_name", None)
            if name and name in tool_descs and hasattr(t, "tool_spec"):
                t.tool_spec["description"] = tool_descs[name]

    @app.entrypoint
    async def invoke(payload, context):
        """Handle an agent invocation from AgentCore Runtime (multi-turn, bundle-aware)."""
        prompt = payload.get("prompt", "")
        session_id = getattr(context, "session_id", None)
        logger.info("Received prompt (session=%s): %s", session_id, str(prompt)[:80])

        # Read config from the Configuration Bundle (injected via baggage header);
        # fall back to defaults when no bundle is present.
        bundle = BedrockAgentCoreContext.get_config_bundle()
        system_prompt, tool_descs = resolve_bundle_config(bundle)

        agent, _created = _SESSION_CACHE.get_or_create(session_id, lambda: _build_agent(system_prompt))
        agent.system_prompt = system_prompt
        _apply_bundle_to_agent(agent, tool_descs)

        async def stream():
            async for event in agent.stream_async(prompt):
                if "data" in event:
                    yield event["data"]

        return stream()

    def run_scripted(turns: list[str], session_id: str = "local-scripted") -> list[str]:
        """Run a scripted sequence of turns in one session (Requirement 1.6).

        Builds a single agent for the session so history carries across turns, then
        returns the agent's textual response for each turn. Calls real Bedrock.
        """
        agent = _build_agent(DEFAULT_SYSTEM_PROMPT)
        _SESSION_CACHE._agents[session_id] = agent  # register for parity with runtime
        responses: list[str] = []
        for turn in turns:
            responses.append(str(agent(turn)))
        return responses

    def interactive_chat(session_id: str = "local-interactive") -> None:  # pragma: no cover
        """Start a local multi-turn chat loop against the agent (Requirement 1.6)."""
        agent = _build_agent(DEFAULT_SYSTEM_PROMPT)
        print("Patient-support assistant (type 'exit' to quit).")
        while True:
            try:
                user = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if user.lower() in {"exit", "quit"}:
                break
            if not user:
                continue
            print(f"agent> {agent(user)}")

    if __name__ == "__main__":
        # When deployed as the runtime entry module, run the AgentCore app server.
        app.run()

else:  # pragma: no cover - informational path when framework isn't installed
    def _framework_required(*_args, **_kwargs):
        raise RuntimeError(
            "strands and bedrock_agentcore are required to run the agent. "
            "Install runtime dependencies (pip install -e .) and configure AWS credentials."
        )

    run_scripted = _framework_required  # type: ignore[assignment]
    interactive_chat = _framework_required  # type: ignore[assignment]

    if __name__ == "__main__":
        _framework_required()
