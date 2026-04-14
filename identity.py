# =============================================================================
# Identity Resolver
# =============================================================================
# Resolves a Slack user ID to their email and Jira account ID.
# If Jira lookup fails, we still proceed with the bot as reporter.

import logging
import os
from base64 import b64encode
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

import requests

from config import JIRA_CONFIG

logger = logging.getLogger(__name__)


@dataclass
class UserIdentity:
    slack_user_id: str
    email: Optional[str] = None
    display_name: str = "Unknown User"
    jira_account_id: Optional[str] = None


def resolve_user_identity(slack_user_id: str, slack_client) -> UserIdentity:
    """
    Resolve a Slack user to their identity across Slack and Jira.

    Flow:
      1. Slack API → get email and display name
      2. Jira API  → search for user by email → get accountId
      3. If any step fails, degrade gracefully (None fields)

    Args:
        slack_user_id: The Slack user ID
        slack_client: Slack WebClient instance

    Returns:
        UserIdentity with whatever we could resolve
    """
    identity = UserIdentity(slack_user_id=slack_user_id)

    # ─── Step 1: Slack User Lookup ───
    try:
        result = slack_client.users_info(user=slack_user_id)
        if result.get("ok"):
            profile = result.get("user", {}).get("profile", {})
            identity.email = profile.get("email")
            identity.display_name = (
                profile.get("display_name")
                or profile.get("real_name")
                or "Unknown User"
            )
    except Exception as e:
        logger.error(f"Slack user lookup failed for {slack_user_id}: {e}")

    # ─── Step 2: Jira User Lookup ───
    if identity.email:
        try:
            identity.jira_account_id = _lookup_jira_user(identity.email)
        except Exception as e:
            logger.error(f"Jira user lookup failed for {identity.email}: {e}")

    return identity


def _lookup_jira_user(email: str) -> Optional[str]:
    """Search Jira for a user by email. Returns accountId if found."""
    jira_email = os.environ.get("JIRA_USER_EMAIL", "")
    jira_token = os.environ.get("JIRA_API_TOKEN", "")

    if not jira_email or not jira_token:
        logger.warning("Jira credentials not configured. Skipping user lookup.")
        return None

    auth_str = b64encode(f"{jira_email}:{jira_token}".encode()).decode()
    search_url = f"{JIRA_CONFIG['base_url']}/rest/api/3/user/search?query={quote(email)}"

    resp = requests.get(
        search_url,
        headers={
            "Authorization": f"Basic {auth_str}",
            "Accept": "application/json",
        },
        timeout=10,
    )

    if not resp.ok:
        logger.error(f"Jira user search returned {resp.status_code}")
        return None

    users = resp.json()
    if isinstance(users, list) and len(users) > 0:
        return users[0].get("accountId")

    logger.warning(f"No Jira user found for email: {email}")
    return None
