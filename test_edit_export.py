import csv
import io
import pytest
from starlette.testclient import TestClient
from test_oauth import secured, token
from oauth import DEFAULT_AUDIENCE, granted_permissions

@pytest.mark.parametrize('scope,permissions,allowed', [
    ('read:harvest', ['read:harvest', 'write:harvest'], False),
    ('read:harvest write:harvest', ['read:harvest'], False),
    ('read:harvest write:harvest', ['read:harvest', 'write:harvest'], True),
])
def test_write_scope_enforced_through_mcp(monkeypatch, secured, scope, permissions, allowed):
    import server
    import importlib
    server = importlib.reload(server)
    app, private = secured
    monkeypatch.setattr(server.app, 'jwks', app.jwks)
    requests = []
    async def fake_request(method, entry_id, changes=None):
        requests.append((method, entry_id, changes))
        return {'id': entry_id, 'updated_at': 'version1', 'hours': 1, **(changes or {})}
    monkeypatch.setattr(server, 'entry_request', fake_request)
    headers = {'Authorization': 'Bearer ' + token(private, scope=scope, permissions=permissions),
               'Accept': 'application/json, text/event-stream'}
    with TestClient(server.app, base_url=DEFAULT_AUDIENCE) as client:
        init = client.post('/mcp', headers=headers, json={
            'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                'protocolVersion': '2025-11-25', 'capabilities': {},
                'clientInfo': {'name': 'test', 'version': '1'}}})
        headers['MCP-Protocol-Version'] = init.json()['result']['protocolVersion']
        call = {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {
            'name': 'update_time_entry', 'arguments': {
                'entry_id': 1, 'expected_updated_at': 'version1', 'notes': 'Corrected'}}}
        result = client.post('/mcp', headers=headers, json=call).json()['result']
        assert bool(result.get('isError')) is not allowed
        assert requests == ([('GET', 1, None), ('PATCH', 1, {'notes': 'Corrected'})] if allowed else [])
        headers['Authorization'] = 'Bearer ' + token(private)
        requests.clear()
        result = client.post('/mcp', headers=headers, json=call).json()['result']
        assert result['isError']
        assert requests == []

@pytest.fixture
def anyio_backend():
    return 'asyncio'

@pytest.mark.anyio
@pytest.mark.parametrize('before,kwargs', [
    ({'updated_at': 'newer'}, {'notes': 'x'}),
    ({'is_locked': True}, {'notes': 'x'}),
    ({'is_billed': True}, {'notes': 'x'}),
    ({'is_running': True}, {'hours': 2}),
    ({'approval_status': 'approved'}, {'notes': 'x'}),
    ({}, {'hours': -1}), ({}, {'hours': float('nan')}),
    ({}, {'hours': 25}), ({}, {'spent_date': 'bad'}), ({}, {}),
])
async def test_reject_unsafe_edit(monkeypatch, before, kwargs):
    import server
    requests = []
    async def fake_request(method, entry_id, changes=None):
        requests.append(method)
        return {'id': entry_id, 'updated_at': 'version1', **before}
    monkeypatch.setattr(server, 'entry_request', fake_request)
    context = granted_permissions.set(frozenset(['read:harvest', 'write:harvest']))
    try:
        with pytest.raises(ValueError):
            await server.update_time_entry(1, 'version1', **kwargs)
        assert 'PATCH' not in requests
    finally:
        granted_permissions.reset(context)

@pytest.mark.anyio
async def test_csv_export_filters_quoting_and_formula_safety(monkeypatch):
    import server
    async def fake_entries(start, end, user_id, project_id):
        assert (start, end, user_id, project_id) == ('2026-10-01', '2026-10-02', 4, 5)
        return [{'id': 1, 'date': start, 'employee': 'Test', 'project': 'Example',
                 'hours': 2.5, 'notes': '=HYPERLINK("test"),\nnext'}]
    monkeypatch.setattr(server, 'list_time_entries', fake_entries)
    report = await server.export_time_report('2026-10-01', '2026-10-02', user_id=4, project_id=5)
    rows = list(csv.DictReader(io.StringIO(report['csv'])))
    assert report['mime_type'] == 'text/csv' and report['row_count'] == 1
    assert rows[0]['notes'] == "'=HYPERLINK(\"test\"),\nnext"
    summary = await server.export_time_report('2026-10-01', '2026-10-02', 'employee', 4, 5)
    assert list(csv.DictReader(io.StringIO(summary['csv']))) == [{'employee': 'Test', 'hours': '2.5'}]
