import os
import csv
import io
import math
from collections import defaultdict
from datetime import date
import httpx
from mcp.server.fastmcp import FastMCP
from oauth import RequireOAuth, require_permission, WRITE_SCOPE
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

def harvest_headers():
    return {
        'Authorization': f"Bearer {os.environ['HARVEST_ACCESS_TOKEN']}",
        'Harvest-Account-Id': os.environ['HARVEST_ACCOUNT_ID'],
        'User-Agent': os.environ.get('HARVEST_USER_AGENT', 'HarvestChatGPTConnector (taha@ihsan.ca)'),
    }

async def entry_request(method: str, entry_id: int, changes: dict | None = None):
    if entry_id <= 0:
        raise ValueError('entry_id must be positive')
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.request(method, f'{BASE}/time_entries/{entry_id}',
                                        headers=harvest_headers(), json=changes)
        if response.status_code >= 400:
            # Never include request headers or arbitrary remote response text in tool errors.
            raise ValueError(f'Harvest rejected the entry request (HTTP {response.status_code})')
        return response.json()

def entry_view(e):
    return {'id': e['id'], 'date': e.get('spent_date'),
            'employee': (e.get('user') or {}).get('name'), 'user_id': (e.get('user') or {}).get('id'),
            'project': (e.get('project') or {}).get('name'), 'project_id': (e.get('project') or {}).get('id'),
            'task': (e.get('task') or {}).get('name'), 'task_id': (e.get('task') or {}).get('id'),
            'hours': e.get('hours'), 'billable': e.get('billable'), 'notes': e.get('notes'),
            'approval_status': e.get('approval_status'), 'updated_at': e.get('updated_at'),
            'is_locked': e.get('is_locked'), 'is_running': e.get('is_running')}

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
    return [entry_view(e) for e in entries]

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

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True), meta={'securitySchemes': [{'type': 'oauth2', 'scopes': ['read:harvest']}]})
async def get_time_entry(entry_id: int) -> dict:
    """Read a single time entry, including its updated_at value for an edit."""
    return entry_view(await entry_request('GET', entry_id))

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True), meta={'securitySchemes': [{'type': 'oauth2', 'scopes': ['read:harvest', 'write:harvest']}]})
async def update_time_entry(entry_id: int, expected_updated_at: str, hours: float | None = None,
                            notes: str | None = None, spent_date: str | None = None,
                            project_id: int | None = None, task_id: int | None = None) -> dict:
    """Update an existing entry's specified fields. First read the entry and pass its updated_at.
    Use only for edits the user requested. Omitted fields stay unchanged; notes='' clears notes.
    Requires write:harvest. Locked, billed, approved and running entries are refused.
    """
    require_permission(WRITE_SCOPE)
    changes = {key: value for key, value in {'hours': hours, 'notes': notes,
               'spent_date': spent_date, 'project_id': project_id, 'task_id': task_id}.items()
               if value is not None}
    if not changes:
        raise ValueError('Provide at least one field to update')
    if hours is not None and (not math.isfinite(hours) or hours < 0 or hours > 24):
        raise ValueError('hours must be a finite number from 0 to 24')
    if spent_date is not None:
        date.fromisoformat(spent_date)
    if any(value is not None and value <= 0 for value in (project_id, task_id)):
        raise ValueError('project_id and task_id must be positive')
    before = await entry_request('GET', entry_id)
    if not expected_updated_at or before.get('updated_at') != expected_updated_at:
        raise ValueError('The entry changed; read it again before applying the edit')
    if (before.get('is_locked') or before.get('is_billed') or before.get('is_running')
            or before.get('approval_status') == 'approved'):
        raise ValueError('This entry is locked, billed, approved or running and cannot be edited here')
    after = await entry_request('PATCH', entry_id, changes)
    return {'before': entry_view(before), 'after': entry_view(after)}

def csv_cell(value):
    # Prevent spreadsheet formulas in untrusted names/notes on CSV import.
    if isinstance(value, str) and (value.startswith(('\t', '\r', '\n'))
                                   or value.lstrip().startswith(('=', '+', '-', '@'))):
        return "'" + value
    return value

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True), meta={'securitySchemes': [{'type': 'oauth2', 'scopes': ['read:harvest']}]})
async def export_time_report(start_date: str, end_date: str, report_type: str = 'entries',
                             user_id: int | None = None, project_id: int | None = None) -> dict:
    """Export a CSV of entries or hours grouped by employee, project or day.
    Returns filename, MIME type and CSV text to save as a downloadable report.
    Dates are inclusive YYYY-MM-DD; optional user/project filters apply to every report type.
    """
    if report_type not in ('entries', 'employee', 'project', 'day'):
        raise ValueError('report_type must be entries, employee, project or day')
    entries = await list_time_entries(start_date, end_date, user_id, project_id)
    if report_type == 'entries':
        columns = ['id', 'date', 'employee', 'user_id', 'project', 'project_id', 'task',
                   'task_id', 'hours', 'billable', 'notes', 'approval_status']
        rows = entries
    else:
        totals = defaultdict(float)
        for e in entries:
            key = e['date'] if report_type == 'day' else e[report_type]
            totals[key or '(unspecified)'] += float(e['hours'] or 0)
        columns = [report_type, 'hours']
        rows = [{report_type: key, 'hours': round(value, 2)} for key, value in sorted(totals.items())]
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction='ignore')
    writer.writeheader()
    writer.writerows({key: csv_cell(row.get(key)) for key in columns} for row in rows)
    return {'filename': f'harvest-{report_type}-{start_date}-to-{end_date}.csv',
            'mime_type': 'text/csv', 'row_count': len(rows), 'csv': output.getvalue()}

app = RequireOAuth(mcp.streamable_http_app())
