"""
Connect to a Claude Code persona runtime via the AgentCore WebSocket Shell.

Persona runtime ARNs are read from .deployed-state.json (written by the deploy
scripts), so there is nothing account-specific hardcoded here.

Usage:
    python connect_persona.py --persona clinician
    python connect_persona.py --persona dev --prompt "List your MCP tools."
    python connect_persona.py --persona auditor --cmd "echo ready"
    python connect_persona.py --persona clinician --session <session-id>
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
STATE_FILE = os.path.join(PROJECT_ROOT, ".deployed-state.json")
PERSONAS = ["clinician", "dev", "auditor", "public"]


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        sys.exit(f"State file not found: {STATE_FILE}. Deploy the demo first (./deploy-all.sh).")
    with open(STATE_FILE) as f:
        return json.load(f)


def persona_runtime_arn(state: dict, persona: str) -> str:
    arn = state.get(f"persona_runtime_{persona}_arn")
    if not arn:
        sys.exit(f"No runtime ARN for persona '{persona}' in state. Run scripts/70_personas.sh.")
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
    state = load_state()
    region = os.environ.get("AWS_REGION") or state.get("region") or "us-west-2"
    persona = args.persona.lower()
    runtime_arn = persona_runtime_arn(state, persona)

    session_id = args.session or str(uuid.uuid4())
    shell_id = str(uuid.uuid4())
    client = AgentCoreRuntimeClient(region=region)

    print(f"Connecting as [{persona.upper()}]...")
    print(f"  Runtime: {runtime_arn}")
    print(f"  Session: {session_id}\n")

    model_flag = f" --model {args.model}" if args.model else ""

    async with client.open_shell(runtime_arn=runtime_arn, session_id=session_id, shell_id=shell_id) as shell:
        if args.prompt:
            safe_prompt = args.prompt.replace("'", "'\\''")
            cmd = f"/app/run.sh{model_flag} '{safe_prompt}'; exit\n"
            print(f"Running prompt as [{persona.upper()}]: {args.prompt}\n")
            await stream_output(shell, cmd)
        elif args.cmd:
            await stream_output(shell, f"{args.cmd}; exit\n")
        else:
            print(f"Connected as [{persona.upper()}]! Launching Claude Code...\n")
            await interactive_pty(shell, f"/app/run.sh{model_flag}\n")

    print(f"\nTo reconnect: python connect_persona.py --persona {persona} --session {session_id}")


def main():
    parser = argparse.ArgumentParser(description="Connect to a Claude Code governance persona")
    parser.add_argument("--persona", required=True, choices=PERSONAS,
                        help="Persona to assume (determines tool access + guardrail behavior)")
    parser.add_argument("--session", help="Reuse an existing session id")
    parser.add_argument("--prompt", help="Run a prompt in headless mode")
    parser.add_argument("--cmd", help="Run a raw shell command")
    parser.add_argument("--model", help="Model id override")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nDisconnecting...")


if __name__ == "__main__":
    main()
