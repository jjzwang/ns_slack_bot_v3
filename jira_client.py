# =============================================================================
# Jira Ticket Creator
# =============================================================================

import json
import logging
import os
import re
import time
from base64 import b64encode
from dataclasses import dataclass
from typing import Optional

import requests

from claude_client import SubmitTicketResponse
from config import JIRA_CONFIG
from identity import UserIdentity

logger = logging.getLogger(__name__)

_JIRA_ADF_BYTE_LIMIT = 32_000


@dataclass
class JiraCreateResult:
    success: bool
    issue_key: Optional[str] = None
    issue_url: Optional[str] = None
    error: Optional[str] = None


def create_jira_ticket(
    ticket_data: SubmitTicketResponse,
    identity: UserIdentity,
    enrichments: Optional[list[dict]] = None,
) -> JiraCreateResult:
    jira_email = os.environ.get("JIRA_USER_EMAIL", "")
    jira_token = os.environ.get("JIRA_API_TOKEN", "")

    if not jira_email or not jira_token:
        return JiraCreateResult(success=False, error="Jira credentials not configured.")

    if identity.email:
        attribution = f"Requested by: {identity.display_name} ({identity.email})"
    else:
        attribution = f"Requested by: {identity.display_name} (Slack ID: {identity.slack_user_id})"

    full_description = f"{attribution}\n\n{ticket_data.description}"
    description_adf = _build_description_adf(full_description, enrichments)

    fields = {
        "project": {"key": JIRA_CONFIG["project_key"]},
        "issuetype": {"id": JIRA_CONFIG["issue_type_id"]},
        "summary": ticket_data.title,
        "description": description_adf,
        JIRA_CONFIG["custom_fields"]["value_to_the_business"]: _to_adf_paragraphs(ticket_data.value_to_business),
        JIRA_CONFIG["custom_fields"]["acceptance_criteria"]: _to_adf_ordered_list(ticket_data.acceptance_criteria),
        JIRA_CONFIG["custom_fields"]["enablement_plan"]: _to_adf_paragraphs(ticket_data.enablement_plan),
        **JIRA_CONFIG["defaults"],
    }

    if identity.jira_account_id:
        fields["reporter"] = {"accountId": identity.jira_account_id}

    return _create_issue_with_retry({"fields": fields}, jira_email, jira_token)


def _to_adf_paragraphs(text: str) -> dict:
    lines = [line for line in text.split("\n") if line.strip()]
    return {
        "type": "doc", "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": line}]} for line in lines],
    }


def _to_adf_ordered_list(text: str) -> dict:
    items = [re.sub(r"^\d+[.)]\s*", "", line.strip()) for line in text.split("\n") if line.strip()]
    return {
        "type": "doc", "version": 1,
        "content": [{
            "type": "orderedList", "attrs": {"order": 1},
            "content": [{
                "type": "listItem",
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": item, "marks": [{"type": "strong"}]}]}],
            } for item in items],
        }],
    }


def _to_adf_implementation_notes(enrichments: list[dict]) -> list[dict]:
    if not enrichments:
        return []
    labels = {
        "implementation_approach": "Approach",
        "edge_case": "Edge Cases",
        "native_alternative": "Native Alternative Considered",
        "downstream_impact": "Downstream Impact",
        "compliance_risk": "Compliance & Audit",
        "integration_dependency": "Integration Dependencies",
        "governance_concern": "Governance & Performance",
        "scope_clarification": "Scope Notes",
    }
    grouped: dict[str, list[str]] = {}
    for e in enrichments:
        detail = e.get("detail", "")
        if detail:
            grouped.setdefault(e.get("category", "other"), []).append(detail)
    if not grouped:
        return []
    nodes: list[dict] = [{"type": "heading", "attrs": {"level": 3}, "content": [{"type": "text", "text": "Implementation Notes"}]}]
    for category, items in grouped.items():
        label = labels.get(category, category.replace("_", " ").title())
        nodes.append({"type": "paragraph", "content": [{"type": "text", "text": label, "marks": [{"type": "strong"}]}]})
        nodes.append({"type": "bulletList", "content": [
            {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": item}]}]}
            for item in items
        ]})
    return nodes


def _build_description_adf(description_text: str, enrichments: Optional[list[dict]]) -> dict:
    base_adf = _to_adf_paragraphs(description_text)
    if not enrichments:
        return base_adf
    notes_nodes = _to_adf_implementation_notes(enrichments)
    if not notes_nodes:
        return base_adf
    combined_adf = {"type": "doc", "version": 1, "content": base_adf["content"] + notes_nodes}
    if len(json.dumps(combined_adf).encode("utf-8")) > _JIRA_ADF_BYTE_LIMIT:
        logger.warning("ADF description with enrichments exceeds size limit. Dropping implementation notes.")
        return base_adf
    return combined_adf


MAX_RETRIES = 2
BACKOFF_SECONDS = [2, 4]


def _create_issue_with_retry(payload: dict, jira_email: str, jira_token: str) -> JiraCreateResult:
    """
    Create a Jira issue with up to MAX_RETRIES retries on 5xx / network errors.
    Full error bodies are logged server-side; only sanitised messages reach the caller.
    """
    auth_str = b64encode(f"{jira_email}:{jira_token}".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth_str}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = requests.post(JIRA_CONFIG["create_issue_url"], headers=headers, json=payload, timeout=15)
        except requests.RequestException as e:
            logger.warning(f"Jira network error (attempt {attempt + 1}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(BACKOFF_SECONDS[attempt])
                continue
            return JiraCreateResult(success=False, error="Network error contacting Jira. Check server logs.")

        if resp.ok:
            data = resp.json()
            return JiraCreateResult(
                success=True,
                issue_key=data.get("key"),
                issue_url=f"{JIRA_CONFIG['base_url']}/browse/{data.get('key')}",
            )

        if 400 <= resp.status_code < 500:
            logger.error(f"Jira client error {resp.status_code}: {resp.text}")
            return JiraCreateResult(success=False, error=f"Jira API error ({resp.status_code}). Check server logs.")

        logger.warning(f"Jira server error {resp.status_code} (attempt {attempt + 1})")
        if attempt < MAX_RETRIES:
            time.sleep(BACKOFF_SECONDS[attempt])

    return JiraCreateResult(success=False, error=f"Jira server error after {MAX_RETRIES + 1} attempts. Check server logs.")
