# =============================================================================
# Jira Ticket Creator
# =============================================================================
# Creates a Jira issue from the structured output of Claude's submit_ticket
# tool. The payload matches the exact Jira REST API format for the FIN project.
#
# Field mapping (bot-populated):
#   title              → fields.summary (plain text)
#   description        → fields.description (ADF paragraph)
#   value_to_business  → fields.customfield_12975 (ADF paragraph)
#   acceptance_criteria→ fields.customfield_12977 (ADF ordered list, bold)
#   enablement_plan    → fields.customfield_12978 (ADF paragraph)
#
# Phase 1 addition:
#   enrichments        → appended to description as "Implementation Notes" (ADF)

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

# Jira ADF field byte limit (REST API constraint measures in bytes, not chars)
_JIRA_ADF_BYTE_LIMIT = 32_000  # Conservative buffer below 32,767


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
    """
    Create a Jira issue from the verified ticket data.

    Args:
        ticket_data: Structured output from Claude's submit_ticket tool
        identity: Resolved user identity for reporter attribution
        enrichments: Optional list of review enrichments to append
                     as Implementation Notes in the description
    """
    jira_email = os.environ.get("JIRA_USER_EMAIL", "")
    jira_token = os.environ.get("JIRA_API_TOKEN", "")

    if not jira_email or not jira_token:
        return JiraCreateResult(success=False, error="Jira credentials not configured.")

    # Build attribution line
    if identity.email:
        attribution = f"Requested by: {identity.display_name} ({identity.email})"
    else:
        attribution = f"Requested by: {identity.display_name} (Slack ID: {identity.slack_user_id})"

    full_description = f"{attribution}\n\n{ticket_data.description}"

    # Build description ADF — may include implementation notes
    description_adf = _build_description_adf(full_description, enrichments)

    # Build the exact Jira payload
    fields = {
        "project": {"key": JIRA_CONFIG["project_key"]},
        "issuetype": {"id": JIRA_CONFIG["issue_type_id"]},
        "summary": ticket_data.title,
        "description": description_adf,
        JIRA_CONFIG["custom_fields"]["value_to_the_business"]: _to_adf_paragraphs(
            ticket_data.value_to_business
        ),
        JIRA_CONFIG["custom_fields"]["acceptance_criteria"]: _to_adf_ordered_list(
            ticket_data.acceptance_criteria
        ),
        JIRA_CONFIG["custom_fields"]["enablement_plan"]: _to_adf_paragraphs(
            ticket_data.enablement_plan
        ),
        **JIRA_CONFIG["defaults"],
    }

    if identity.jira_account_id:
        fields["reporter"] = {"accountId": identity.jira_account_id}

    payload = {"fields": fields}

    return _create_issue_with_retry(payload, jira_email, jira_token)


# =============================================================================
# ADF (Atlassian Document Format) Builders
# =============================================================================


def _to_adf_paragraphs(text: str) -> dict:
    """Convert plain text to ADF paragraph format, splitting on newlines."""
    lines = [line for line in text.split("\n") if line.strip()]
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": line}],
            }
            for line in lines
        ],
    }


def _to_adf_ordered_list(text: str) -> dict:
    """Convert acceptance criteria text to ADF ordered list with bold items."""
    items = [line.strip() for line in text.split("\n") if line.strip()]
    # Strip leading numbers like "1. ", "2) ", etc.
    items = [re.sub(r"^\d+[.)]\s*", "", item) for item in items]

    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "orderedList",
                "attrs": {"order": 1},
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": item,
                                        "marks": [{"type": "strong"}],
                                    }
                                ],
                            }
                        ],
                    }
                    for item in items
                ],
            }
        ],
    }


def _to_adf_implementation_notes(enrichments: list[dict]) -> list[dict]:
    """
    Convert structured enrichments into ADF content nodes with
    categorized sections (headings + bullet lists).

    Returns a list of ADF nodes that can be appended to the description
    field's existing content array. Returns empty list if no enrichments.
    """
    if not enrichments:
        return []

    # Category display names
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

    # Group enrichments by category
    grouped: dict[str, list[str]] = {}
    for e in enrichments:
        category = e.get("category", "other")
        detail = e.get("detail", "")
        if detail:
            grouped.setdefault(category, []).append(detail)

    if not grouped:
        return []

    # Build ADF content nodes
    content_nodes: list[dict] = [
        # Section heading
        {
            "type": "heading",
            "attrs": {"level": 3},
            "content": [{"type": "text", "text": "Implementation Notes"}],
        },
    ]

    for category, items in grouped.items():
        label = labels.get(category, category.replace("_", " ").title())
        # Category sub-heading (bold paragraph)
        content_nodes.append({
            "type": "paragraph",
            "content": [
                {"type": "text", "text": label, "marks": [{"type": "strong"}]}
            ],
        })
        # Bullet list of items
        content_nodes.append({
            "type": "bulletList",
            "content": [
                {
                    "type": "listItem",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": item}],
                        }
                    ],
                }
                for item in items
            ],
        })

    return content_nodes


def _build_description_adf(description_text: str, enrichments: Optional[list[dict]]) -> dict:
    """
    Build the complete description ADF document, optionally appending
    implementation notes from enrichments.

    Includes a size guard: if the combined ADF would exceed Jira's
    character limit, enrichments are dropped with a warning.
    """
    base_adf = _to_adf_paragraphs(description_text)

    if not enrichments:
        return base_adf

    notes_nodes = _to_adf_implementation_notes(enrichments)
    if not notes_nodes:
        return base_adf

    # Size guard — check if combined ADF fits within Jira's byte limit.
    # Jira measures payload size in bytes; emoji, accented characters, and
    # smart quotes are multibyte in UTF-8 so len(str) undercounts.
    combined_content = base_adf["content"] + notes_nodes
    combined_adf = {
        "type": "doc",
        "version": 1,
        "content": combined_content,
    }

    serialized_size = len(json.dumps(combined_adf).encode("utf-8"))
    if serialized_size > _JIRA_ADF_BYTE_LIMIT:
        logger.warning(
            f"ADF description with enrichments exceeds size limit "
            f"({serialized_size} > {_JIRA_ADF_BYTE_LIMIT}). "
            f"Dropping implementation notes to fit."
        )
        return base_adf

    return combined_adf


# =============================================================================
# API Call with Retry
# =============================================================================

MAX_RETRIES = 2
BACKOFF_SECONDS = [2, 4]


def _create_issue_with_retry(
    payload: dict,
    jira_email: str,
    jira_token: str,
    attempt: int = 0,
) -> JiraCreateResult:
    """Create Jira issue with retry on 5xx errors."""
    try:
        auth_str = b64encode(f"{jira_email}:{jira_token}".encode()).decode()
        resp = requests.post(
            JIRA_CONFIG["create_issue_url"],
            headers={
                "Authorization": f"Basic {auth_str}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json=payload,
            timeout=15,
        )

        if resp.ok:
            data = resp.json()
            return JiraCreateResult(
                success=True,
                issue_key=data.get("key"),
                issue_url=f"{JIRA_CONFIG['base_url']}/browse/{data.get('key')}",
            )

        # Client error — don't retry
        if 400 <= resp.status_code < 500:
            error_body = resp.text
            logger.error(f"Jira client error {resp.status_code}: {error_body}")
            return JiraCreateResult(
                success=False,
                error=f"Jira returned {resp.status_code}: {error_body}",
            )

        # Server error — retry
        if attempt < MAX_RETRIES:
            wait = BACKOFF_SECONDS[attempt]
            logger.warning(f"Jira server error {resp.status_code}. Retrying in {wait}s...")
            time.sleep(wait)
            return _create_issue_with_retry(payload, jira_email, jira_token, attempt + 1)

        return JiraCreateResult(
            success=False,
            error=f"Jira server error {resp.status_code} after {MAX_RETRIES} retries.",
        )

    except requests.RequestException as e:
        if attempt < MAX_RETRIES:
            wait = BACKOFF_SECONDS[attempt]
            logger.warning(f"Jira network error. Retrying in {wait}s...")
            time.sleep(wait)
            return _create_issue_with_retry(payload, jira_email, jira_token, attempt + 1)

        return JiraCreateResult(
            success=False,
            error=f"Network error creating Jira issue: {e}",
        )
