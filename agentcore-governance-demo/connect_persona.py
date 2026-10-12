"""
Connect to a Claude Code persona runtime via the AgentCore WebSocket Shell.

Persona runtime ARNs are read from .deployed-state.json (written by the deploy
scripts), so there is nothing account-specific hardcoded here.

Usage:
    python connect_persona.py --persona clinician
    python connect_persona.py --persona dev --prompt "List your MCP tools."
    python connect_persona.py --persona auditor --cmd "echo ready"
    python connect_persona.py --persona clinician --session <session-id>
    python connect_persona.py --persona clinician --agent devin.ai --cmd "echo ready"

The optional --agent flag selects which coding agent runtime to connect to
(default: claude-code). The chosen (persona, agent) runtime must already be
deployed — the flag selects among deployed runtimes, it does not create one.
"""

import argparse
import asyncio
import json
import os
import sys
import termios
import tty
import uuid

from bedrock_agentcore.runtime import AgentCoreRuntimeClient
from bedrock_agentcore.runtime.shell import ShellChannel, ShellSession

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
LEGACY_STATE_FILE = os.path.join(PROJECT_ROOT, ".deployed-state.json")
PERSONAS = ["clinician", "dev", "auditor", "public"]

# Default agent + aliases. Keep this in sync with agent_normalize_slug() in
# lib/common.sh so the CLI and the deploy scripts agree on slugs.
DEFAULT_AGENT = "claude-code"
AGENT_ALIASES = {
    "devin": "devin",
    "devin.ai": "devin",
    "devinai": "devin",
    "claude": "claude-code",
    "claude-code": "claude-code",
    "claudecode": "claude-code",
    "cursor": "cursor",
    "cursor-cli": "cursor",
    "cursor-agent": "cursor",
}


def normalize_agent(raw: str) -> str:
    key = (raw or DEFAULT_AGENT).strip().lower()
    return AGENT_ALIASES.get(key, key)


def resolve_account_id(region: str) -> str | None:
    try:
        import boto3
        return boto3.client("sts", region_name=region).get_caller_identity()["Account"]
    except Exception:
        return None


def state_file_path(region: str) -> str:
    """Per-account state file (.deployed-state.<account>.json), matching the
    deploy scripts. Falls back to the legacy single-file name."""
    account = resolve_account_id(region)
    if account:
        per_account = os.path.join(PROJECT_ROOT, f".deployed-state.{account}.json")
        if os.path.exists(per_account):
            return per_account
    return LEGACY_STATE_FILE


def load_state(region: str) -> dict:
    path = state_file_path(region)
    if not os.path.exists(path):
        sys.exit(f"State file not found: {path}. Deploy the demo first (./deploy-all.sh).")
    with open(path) as f:
        return json.load(f)


def persona_runtime_arn(state: dict, persona: str, agent: str) -> str:
    # Agent-scoped key, e.g. persona_runtime_clinician_devin_arn
    arn = state.get(f"persona_runtime_{persona}_{agent}_arn")
    # Back-compat: older deployments stored claude-code without an agent suffix.
    if not arn and agent == "claude-code":
        arn = state.get(f"persona_runtime_{persona}_arn")
    if not arn:
        deployed = sorted({
            k[len("persona_runtime_"):-len("_arn")]
            for k in state
            if k.startswith("persona_runtime_") and k.endswith("_arn")
        })
        hint = f" Deployed runtimes: {', '.join(deployed)}." if deployed else ""
        sys.exit(
            f"No runtime for persona '{persona}' with agent '{agent}'. "
            f"Deploy it first (set AGENTS to include '{agent}' and run scripts/70_personas.sh).{hint}"
        )
    return arn


async def interactive_pty(shell: ShellSession, initial_cmd: str | None = None):
    old_settings = termios.tcgetattr(sys.stdin)
    try:
        tty.setraw(sys.stdin.fileno())
        cols, rows = os.get_terminal_size()
        await shell.resize(cols, rows)
        if initial_cmd:
            await shell.send(initial_cmd)

        loop = asyncio.get_event_loop()
        stdin_fd = sys.stdin.fileno()

        async def read_stdin():
            while True:
                data = await loop.run_in_executor(None, os.read, stdin_fd, 4096)
                if not data:
                    break
                await shell.send_bytes(data)

        stdin_task = asyncio.create_task(read_stdin())
        try:
            async for frame in shell:
                if frame.channel == ShellChannel.STDOUT:
                    os.write(sys.stdout.fileno(), frame.payload)
                elif frame.channel == ShellChannel.STDERR:
                    os.write(sys.stderr.fileno(), frame.payload)
                elif frame.channel in (ShellChannel.STATUS, ShellChannel.CLOSE):
                    break
        finally:
            stdin_task.cancel()
            try:
                await stdin_task
            except asyncio.CancelledError:
                pass
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
        print()


async def stream_output(shell: ShellSession, initial_cmd: str):
    await shell.send(initial_cmd)
    async for frame in shell:
        if frame.channel == ShellChannel.STDOUT:
            print(frame.text, end="", flush=True)
        elif frame.channel == ShellChannel.STDERR:
            print(frame.text, end="", file=sys.stderr, flush=True)
        elif frame.channel in (ShellChannel.STATUS, ShellChannel.CLOSE):
            break


async def run(args):
    region = os.environ.get("AWS_REGION") or "us-west-2"
    state = load_state(region)
    persona = args.persona.lower()
    agent = normalize_agent(args.agent)
    runtime_arn = persona_runtime_arn(state, persona, agent)

    # Reuse the same session id across connect attempts so the first (cold) attempt
    # triggers the microVM start and a later attempt lands on the warmed VM. Without
    # --session, we still generate one id up front and keep it stable across retries.
    session_id = args.session or str(uuid.uuid4())
    client = AgentCoreRuntimeClient(region=region)

    print(f"Connecting as [{persona.upper()}] using agent [{agent}]...")
    print(f"  Runtime: {runtime_arn}")
    print(f"  Session: {session_id}\n")

    model_flag = f" --model {args.model}" if args.model else ""

    # Cold VPC + S3-Files microVMs can take longer to boot than the WebSocket
    # handshake timeout, so the first connect often times out. Retry the connect
    # (new shell id each time, same session id) until the VM is warm.
    shell_cm = None
    shell = None
    for attempt in range(1, args.connect_attempts + 1):
        try:
            shell_cm = client.open_shell(
                runtime_arn=runtime_arn, session_id=session_id, shell_id=str(uuid.uuid4())
            )
            shell = await shell_cm.__aenter__()
            break
        except TimeoutError:
            shell_cm = None
            if attempt >= args.connect_attempts:
                sys.exit(
                    f"\nCould not connect after {args.connect_attempts} attempts (cold-start "
                    f"handshake kept timing out). Try again, or pre-warm with "
                    f"./warm-personas.sh --persona {persona} --agent {agent}."
                )
            print(f"  cold start — runtime still booting (attempt {attempt}/{args.connect_attempts}), retrying…")
            await asyncio.sleep(args.connect_wait)
        except Exception as e:  # noqa: BLE001 - surface auth/config errors immediately
            msg = str(e)
            if "403" in msg or "expired" in msg.lower():
                sys.exit("\nConnection refused (HTTP 403). Your AWS credentials look expired — "
                         "refresh ~/.aws/credentials and retry.")
            raise

    try:
        if args.prompt:
            safe_prompt = args.prompt.replace("'", "'\\''")
            cmd = f"/app/run.sh{model_flag} '{safe_prompt}'; exit\n"
            print(f"Running prompt as [{persona.upper()}]: {args.prompt}\n")
            await stream_output(shell, cmd)
        elif args.cmd:
            await stream_output(shell, f"{args.cmd}; exit\n")
        else:
            print(f"Connected as [{persona.upper()}]! Launching agent…\n")
            await interactive_pty(shell, f"/app/run.sh{model_flag}\n")
    finally:
        if shell_cm is not None:
            await shell_cm.__aexit__(None, None, None)

    print(f"\nTo reconnect: python connect_persona.py --persona {persona} --agent {agent} --session {session_id}")


def main():
    parser = argparse.ArgumentParser(description="Connect to a Claude Code governance persona")
    parser.add_argument("--persona", required=True, choices=PERSONAS,
                        help="Persona to assume (determines tool access + guardrail behavior)")
    parser.add_argument("--agent", default=DEFAULT_AGENT,
                        help="Coding agent to connect to (default: claude-code). "
                             "Accepts e.g. 'devin.ai'. The (persona, agent) runtime must be deployed.")
    parser.add_argument("--session", help="Reuse an existing session id")
    parser.add_argument("--prompt", help="Run a prompt in headless mode")
    parser.add_argument("--cmd", help="Run a raw shell command")
    parser.add_argument("--model", help="Model id override")
    parser.add_argument("--connect-attempts", type=int, default=8,
                        help="Connect retries for cold-start handshake timeouts (default: 8)")
    parser.add_argument("--connect-wait", type=int, default=15,
                        help="Seconds between connect retries (default: 15)")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nDisconnecting...")


if __name__ == "__main__":
    main()
