import os
from collections import defaultdict
from datetime import date
import httpx
from mcp.server.fastmcp import FastMCP
from oauth import RequireOAuth
from mcp.types import ToolAnnotations
from mcp.server.transport_security import TransportSecuritySettings
from urllib.parse import urlsplit

public_origin = os.environ.get('AUTH0_AUDIENCE', 'https://harvest-connector-yvdj.onrender.com')
mcp = FastMCP('harvest-time-tracking', stateless_http=True, json_response=True,
              transport_security=TransportSecuritySettings(
                  enable_dns_rebinding_protection=True,
                  allowed_hosts=[urlsplit(public_origin).netloc, 'localhost:*', '127.0.0.1:*'],
                  allowed_origins=[public_origin, 'http://localhost:*', 'http://127.0.0.1:*']))
BASE = 'https://api.harvestapp.com/v2'

async def fetch_all(path: str, params: dict | None = None):
    token = os.environ['HARVEST_ACCESS_TOKEN']
    account = os.environ['HARVEST_ACCOUNT_ID']
    headers = {
        'Authorization': f'Bearer {token}',
        'Harvest-Account-Id': account,
        'User-Agent': os.environ.get('HARVEST_USER_AGENT', 'HarvestChatGPTConnector (admin@example.com)'),
    }
    items = []
    page = 1
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            query = dict(params or {}, page=page, per_page=100)
            response = await client.get(f'{BASE}/{path}', headers=headers, params=query)
            response.raise_for_status()
            body = response.json()
            key = path.split('/')[0]
            if key not in body or not isinstance(body[key], list):
                raise ValueError(f'Unexpected Harvest response for {path}')
            items.extend(body[key])
            next_page = body.get('next_page')
            if not next_page:
                break
            page = int(next_page)
            if page > 1000:
                raise ValueError('Too many pages; narrow your date range')
    return items

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True), meta={'securitySchemes': [{'type': 'oauth2', 'scopes': ['read:harvest']}]})
async def list_people() -> list[dict]:
    """List Harvest users visible to the authenticated account, with IDs, names and active status."""
    users = await fetch_all('users')
    return [{'id': u['id'], 'name': f"{u.get('first_name','')} {u.get('last_name','')}".strip(), 'email': u.get('email'), 'is_active': u.get('is_active')} for u in users]

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True), meta={'securitySchemes': [{'type': 'oauth2', 'scopes': ['read:harvest']}]})
async def list_projects() -> list[dict]:
    """List Harvest projects including project IDs and names."""
    projects = await fetch_all('projects')
    return [{'id': p['id'], 'name': p.get('name'), 'client': (p.get('client') or {}).get('name'), 'is_active': p.get('is_active')} for p in projects]

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True), meta={'securitySchemes': [{'type': 'oauth2', 'scopes': ['read:harvest']}]})
async def list_time_entries(start_date: str, end_date: str, user_id: int | None = None, project_id: int | None = None) -> list[dict]:
    """Read employee time entries in an inclusive YYYY-MM-DD date range. Optionally filter by Harvest user or project ID."""
    if date.fromisoformat(start_date) > date.fromisoformat(end_date):
        raise ValueError('start_date must not exceed end_date')
    params = {'from': start_date, 'to': end_date}
    if user_id is not None: params['user_id'] = user_id
    if project_id is not None: params['project_id'] = project_id
    entries = await fetch_all('time_entries', params)
    return [{'id': e['id'], 'date': e.get('spent_date'), 'employee': (e.get('user') or {}).get('name'), 'user_id': (e.get('user') or {}).get('id'), 'project': (e.get('project') or {}).get('name'), 'task': (e.get('task') or {}).get('name'), 'hours': e.get('hours'), 'billable': e.get('billable'), 'notes': e.get('notes'), 'approval_status': e.get('approval_status')} for e in entries]

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True), meta={'securitySchemes': [{'type': 'oauth2', 'scopes': ['read:harvest']}]})
async def hours_summary(start_date: str, end_date: str, group_by: str = 'employee') -> list[dict]:
    """Summarize recorded hours by employee, project or day for an inclusive YYYY-MM-DD date range. This is recorded time, not a determination of missing shifts."""
    if group_by not in ('employee', 'project', 'day'):
        raise ValueError('group_by must be employee, project or day')
    entries = await list_time_entries(start_date, end_date)
    totals = defaultdict(float)
    for e in entries:
        key = e['employee'] if group_by == 'employee' else e['project'] if group_by == 'project' else e['date']
        totals[key or '(unspecified)'] += float(e['hours'] or 0)
    return [{group_by: k, 'hours': round(v, 2)} for k, v in sorted(totals.items())]

app = RequireOAuth(mcp.streamable_http_app())
