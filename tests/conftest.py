# =============================================================================
# Test bootstrap
# =============================================================================
# Set fake env vars BEFORE any test module imports app/config. This runs at
# conftest import time (i.e. before pytest collects test files), which is
# earlier than any fixture would. config.validate_config() and
# app.py's module-level reads are satisfied without a real .env.

import os

_TEST_ENV = {
    "SLACK_BOT_TOKEN": "xoxb-test",
    "SLACK_APP_TOKEN": "xapp-test",
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "JIRA_BASE_URL": "https://test.atlassian.net",
    "JIRA_PROJECT_KEY": "TEST",
    "JIRA_USER_EMAIL": "test-bot@example.com",
    "JIRA_API_TOKEN": "test-token",
    "TRIAGE_CHANNEL_ID": "C_TEST_TRIAGE",
    "DATABASE_URL": "postgresql://fake:fake@localhost:5432/fake",
}

for k, v in _TEST_ENV.items():
    os.environ.setdefault(k, v)

# Explicitly ensure migrations don't run on import — there is no real DB.
os.environ.pop("RUN_MIGRATIONS", None)
