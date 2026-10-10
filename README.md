# Harvest MCP connector

Read-only tools: list_people, list_projects, list_time_entries, hours_summary.

Required environment variables: HARVEST_ACCESS_TOKEN, HARVEST_ACCOUNT_ID. Optional HARVEST_USER_AGENT and PORT.

Start locally: `pip install -r requirements.txt && python server.py`.

**Security: Do not deploy this app publicly yet.** The base server does not authenticate inbound MCP callers. Employee time-entry data would be accessible to anyone with its URL. Set up authenticated MCP access before deploying, and never commit Harvest tokens to GitHub.
