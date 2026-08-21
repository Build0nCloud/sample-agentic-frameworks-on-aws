"""
Mock Jira MCP Server for AgentCore Gateway.

Provides realistic Jira-like tools with fake project data.
Designed for demos — no real Jira connection needed.
"""

import os
from datetime import datetime, timedelta
from typing import Optional

from fastmcp import FastMCP

mcp = FastMCP("JiraMock")

# ── Mock Data ────────────────────────────────────────────────────────────────

PROJECTS = {
    "AC": {
        "key": "AC",
        "name": "AgentCore Platform",
        "lead": "smithzgg",
        "description": "Amazon Bedrock AgentCore development",
    },
    "DEMO": {
        "key": "DEMO",
        "name": "Demo Project",
        "lead": "demo-user",
        "description": "Sample project for gateway demonstrations",
    },
}

ISSUES = {
    "AC-101": {
        "key": "AC-101",
        "project": "AC",
        "summary": "Implement Bedrock Guardrails integration",
        "description": "Add support for content filtering via Bedrock Guardrails on gateway interceptors.",
        "status": "In Progress",
        "priority": "High",
        "assignee": "smithzgg",
        "reporter": "pm-lead",
        "type": "Story",
        "created": "2026-07-20T10:00:00Z",
        "updated": "2026-07-28T14:30:00Z",
        "labels": ["guardrails", "security"],
        "comments": [
            {"author": "pm-lead", "body": "This is a P1 for the demo. Need it by Friday.", "created": "2026-07-20T10:05:00Z"},
            {"author": "smithzgg", "body": "Working on the interceptor config now.", "created": "2026-07-25T09:00:00Z"},
        ],
    },
    "AC-102": {
        "key": "AC-102",
        "project": "AC",
        "summary": "Add DynamoDB MCP server to gateway",
        "description": "Deploy a DynamoDB MCP server and register as gateway target.",
        "status": "To Do",
        "priority": "Medium",
        "assignee": "smithzgg",
        "reporter": "tech-lead",
        "type": "Task",
        "created": "2026-07-22T08:00:00Z",
        "updated": "2026-07-22T08:00:00Z",
        "labels": ["dynamodb", "phase-2"],
        "comments": [],
    },
    "AC-103": {
        "key": "AC-103",
        "project": "AC",
        "summary": "Policy: block write tools for read-only agents",
        "description": "Configure AgentCore Policy to deny put_file, create_pull_request for agents with read-only role.",
        "status": "To Do",
        "priority": "High",
        "assignee": None,
        "reporter": "security-team",
        "type": "Story",
        "created": "2026-07-25T11:00:00Z",
        "updated": "2026-07-25T11:00:00Z",
        "labels": ["policy", "security", "phase-3"],
        "comments": [],
    },
    "DEMO-1": {
        "key": "DEMO-1",
        "project": "DEMO",
        "summary": "Test issue for gateway demo",
        "description": "A simple test issue to verify Jira MCP tools work end-to-end.",
        "status": "Done",
        "priority": "Low",
        "assignee": "demo-user",
        "reporter": "demo-user",
        "type": "Task",
        "created": "2026-07-15T09:00:00Z",
        "updated": "2026-07-16T12:00:00Z",
        "labels": ["demo"],
        "comments": [
            {"author": "demo-user", "body": "Verified working!", "created": "2026-07-16T12:00:00Z"},
        ],
    },
}

_next_issue_num = {"AC": 104, "DEMO": 2}


# ── Tools ────────────────────────────────────────────────────────────────────


@mcp.tool()
def list_projects() -> list[dict]:
    """List all Jira projects. Returns key, name, lead, and description."""
    return list(PROJECTS.values())


@mcp.tool()
def get_issue(issue_key: str) -> dict:
    """Get a Jira issue by key (e.g., AC-101). Returns full issue details."""
    issue = ISSUES.get(issue_key.upper())
    if not issue:
        return {"error": f"Issue {issue_key} not found"}
    return issue


@mcp.tool()
def search_issues(
    project: str,
    status: Optional[str] = None,
    assignee: Optional[str] = None,
    label: Optional[str] = None,
) -> list[dict]:
    """Search issues by project, status, assignee, or label. Returns summary list."""
    results = []
    for issue in ISSUES.values():
        if issue["project"] != project.upper():
            continue
        if status and issue["status"].lower() != status.lower():
            continue
        if assignee and issue["assignee"] != assignee:
            continue
        if label and label not in issue["labels"]:
            continue
        results.append({
            "key": issue["key"],
            "summary": issue["summary"],
            "status": issue["status"],
            "priority": issue["priority"],
            "assignee": issue["assignee"],
        })
    return results


@mcp.tool()
def create_issue(
    project: str,
    summary: str,
    description: str = "",
    issue_type: str = "Task",
    priority: str = "Medium",
    assignee: Optional[str] = None,
    labels: Optional[list[str]] = None,
) -> dict:
    """Create a new Jira issue. Returns the new issue key and URL."""
    project = project.upper()
    if project not in PROJECTS:
        return {"error": f"Project {project} not found"}

    num = _next_issue_num.get(project, 1)
    _next_issue_num[project] = num + 1
    key = f"{project}-{num}"

    now = datetime.utcnow().isoformat() + "Z"
    issue = {
        "key": key,
        "project": project,
        "summary": summary,
        "description": description,
        "status": "To Do",
        "priority": priority,
        "assignee": assignee,
        "reporter": "agentcore-bot",
        "type": issue_type,
        "created": now,
        "updated": now,
        "labels": labels or [],
        "comments": [],
    }
    ISSUES[key] = issue
    return {"key": key, "url": f"https://mock-jira.example.com/browse/{key}"}


@mcp.tool()
def update_issue(
    issue_key: str,
    summary: Optional[str] = None,
    description: Optional[str] = None,
    priority: Optional[str] = None,
    assignee: Optional[str] = None,
    labels: Optional[list[str]] = None,
) -> dict:
    """Update fields on an existing Jira issue."""
    issue = ISSUES.get(issue_key.upper())
    if not issue:
        return {"error": f"Issue {issue_key} not found"}

    if summary:
        issue["summary"] = summary
    if description:
        issue["description"] = description
    if priority:
        issue["priority"] = priority
    if assignee is not None:
        issue["assignee"] = assignee
    if labels is not None:
        issue["labels"] = labels
    issue["updated"] = datetime.utcnow().isoformat() + "Z"

    return {"key": issue["key"], "status": "updated"}


@mcp.tool()
def transition_issue(issue_key: str, status: str) -> dict:
    """Transition a Jira issue to a new status (To Do, In Progress, In Review, Done)."""
    valid_statuses = ["To Do", "In Progress", "In Review", "Done"]
    issue = ISSUES.get(issue_key.upper())
    if not issue:
        return {"error": f"Issue {issue_key} not found"}
    if status not in valid_statuses:
        return {"error": f"Invalid status. Valid: {valid_statuses}"}

    issue["status"] = status
    issue["updated"] = datetime.utcnow().isoformat() + "Z"
    return {"key": issue["key"], "status": status}


@mcp.tool()
def add_comment(issue_key: str, body: str) -> dict:
    """Add a comment to a Jira issue."""
    issue = ISSUES.get(issue_key.upper())
    if not issue:
        return {"error": f"Issue {issue_key} not found"}

    comment = {
        "author": "agentcore-bot",
        "body": body,
        "created": datetime.utcnow().isoformat() + "Z",
    }
    issue["comments"].append(comment)
    issue["updated"] = comment["created"]
    return {"key": issue["key"], "comment_id": len(issue["comments"])}


@mcp.tool()
def delete_issue(issue_key: str) -> dict:
    """Delete a Jira issue permanently. This action cannot be undone."""
    issue_key = issue_key.upper()
    if issue_key not in ISSUES:
        return {"error": f"Issue {issue_key} not found"}

    del ISSUES[issue_key]
    return {"deleted": issue_key}


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("MCP_PORT", "8000")), stateless_http=True)
