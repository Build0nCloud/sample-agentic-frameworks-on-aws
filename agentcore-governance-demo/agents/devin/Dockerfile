# Devin CLI agent for AgentCore Runtime.
#
# Same contract as the claude-code image: exposes /app/run.sh and reaches tools
# only through the stdio MCP proxy (/mnt/s3files/mcp/index.js) pointed at
# $GATEWAY_URL, so Cedar + guardrail governance (keyed on the persona IAM role)
# applies identically. Node is required because the MCP proxy is a node script.
#
# Unlike claude-code (which uses Bedrock via the runtime IAM role), Devin CLI
# reaches Cognition's backend for inference and needs a Devin API token. The
# token is fetched from Secrets Manager at startup (never baked into the image).
FROM node:22-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates awscli python3 \
    && rm -rf /var/lib/apt/lists/*

RUN userdel -r node && \
    useradd -m -d /home/agent -u 1000 agent && \
    git config --global user.name "AgentCore Bot" && \
    git config --global user.email "agentcore@example.com"

COPY --chown=agent:agent config.json /home/agent/.config/devin/config.json
COPY --chown=agent:agent AGENTS.md /home/agent/.config/devin/AGENTS.md
COPY --chown=agent:agent healthcheck.py /app/healthcheck.py
COPY run.sh /app/run.sh
RUN chmod +x /app/run.sh && chown -R agent:agent /home/agent/.config

WORKDIR /app
EXPOSE 8080
USER agent

# Install the Devin CLI (arm64 linux binary). No auth needed at build time.
# The installer drops the binary at ~/.local/bin/devin and then tries to launch
# an interactive login; with no TTY that login "cancels" and returns non-zero,
# so we run it with stdin from /dev/null, tolerate that exit, and then assert the
# binary is present. Authentication happens at runtime in run.sh.
ENV PATH="/home/agent/.local/bin:/home/agent/.local/share/devin/bin:${PATH}"
RUN curl -fsSL https://cli.devin.ai/install.sh -o /tmp/devin-install.sh && \
    bash /tmp/devin-install.sh </dev/null || true; \
    rm -f /tmp/devin-install.sh && \
    test -x /home/agent/.local/bin/devin && devin --version

CMD ["python3", "/app/healthcheck.py"]
