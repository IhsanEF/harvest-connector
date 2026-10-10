import time
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient
from oauth import RequireOAuth, DEFAULT_AUDIENCE, DEFAULT_ISSUER

@pytest.fixture
def secured():
    async def downstream(scope, receive, send):
        if scope['type'] == 'lifespan':
            while True:
                event = await receive()
                if event['type'] == 'lifespan.startup':
                    await send({'type': 'lifespan.startup.complete'})
                elif event['type'] == 'lifespan.shutdown':
                    await send({'type': 'lifespan.shutdown.complete'})
                    return
        from starlette.responses import JSONResponse
        await JSONResponse({'protected': True})(scope, receive, send)
    app = RequireOAuth(downstream)
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key(), as_dict=True)
    public_jwk.update(kid='test', use='sig', alg='RS256')
    app.jwks.fetch_data = lambda: {'keys': [public_jwk]}
    return app, private

def token(private, **updates):
    claims = {'iss': DEFAULT_ISSUER, 'aud': DEFAULT_AUDIENCE, 'sub': 'auth0|test',
              'iat': int(time.time()) - 1, 'exp': int(time.time()) + 600,
              'scope': 'read:harvest', 'permissions': ['read:harvest']}
    for key, value in updates.items():
        if value is None:
            claims.pop(key, None)
        else:
            claims[key] = value
    return jwt.encode(claims, private, algorithm='RS256', headers={'kid': 'test'})

def test_discovery_and_challenge(secured):
    app, _ = secured
    with TestClient(app) as client:
        for path in ['/.well-known/oauth-protected-resource', '/.well-known/oauth-protected-resource/mcp']:
            metadata = client.get(path)
            assert metadata.status_code == 200
            assert metadata.json()['resource'] == DEFAULT_AUDIENCE
            assert metadata.json()['authorization_servers'] == [DEFAULT_ISSUER]
        assert client.get('/health').status_code == 200
        response = client.post('/mcp')
        assert response.status_code == 401
        assert 'resource_metadata="' + app.metadata_url + '"' in response.headers['www-authenticate']

@pytest.mark.parametrize('claims,status', [
    ({}, 200), ({'iss': 'https://other.example/'}, 401),
    ({'iss': DEFAULT_ISSUER.rstrip('/')}, 401),
    ({'aud': 'other-api'}, 401), ({'aud': ['other', DEFAULT_AUDIENCE]}, 200),
    ({'exp': 1}, 401), ({'exp': None}, 401), ({'iat': None}, 401),
    ({'sub': None}, 401), ({'nbf': int(time.time()) + 3600}, 401),
    ({'scope': 'other:read'}, 403), ({'scope': None}, 403),
    ({'permissions': []}, 403), ({'permissions': None}, 403),
    ({'permissions': 'read:harvest'}, 403),
])
def test_validation(secured, claims, status):
    app, private = secured
    with TestClient(app) as client:
        response = client.post('/mcp', headers={'Authorization': 'Bearer ' + token(private, **claims)})
        assert response.status_code == status
        if status == 403:
            assert 'insufficient_scope' in response.headers['www-authenticate']

@pytest.mark.parametrize('supplied', ['old-static-secret', 'malformed.jwt.token',
    jwt.encode({'sub': 'test'}, 'x' * 32, algorithm='HS256', headers={'kid': 'test'}),
    jwt.encode({'sub': 'test'}, '', algorithm='none', headers={'kid': 'test'})])
def test_reject_non_rs256_and_static_tokens(secured, supplied):
    app, _ = secured
    with TestClient(app) as client:
        assert client.post('/mcp', headers={'Authorization': 'Bearer ' + supplied}).status_code == 401

def test_signature_and_unknown_key(secured):
    app, _ = secured
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with TestClient(app) as client:
        assert client.post('/mcp', headers={'Authorization': 'Bearer ' + token(other)}).status_code == 401
        unknown = jwt.encode({'sub': 'x'}, other, algorithm='RS256', headers={'kid': 'unknown'})
        assert client.post('/mcp', headers={'Authorization': 'Bearer ' + unknown}).status_code == 401

def test_jwks_outage_fails_closed(secured):
    app, private = secured
    def failed(_):
        raise jwt.PyJWKClientConnectionError('unavailable')
    app.jwks.get_signing_key_from_jwt = failed
    with TestClient(app) as client:
        assert client.post('/mcp', headers={'Authorization': 'Bearer ' + token(private)}).status_code == 503

def test_mcp_initialize_list_and_call(monkeypatch, secured):
    import server
    app, private = secured
    monkeypatch.setattr(server.app, 'jwks', app.jwks)
    async def fake_fetch(path, params=None):
        assert path == 'projects'
        return [{'id': 1, 'name': 'Example', 'client': {'name': 'Test'}, 'is_active': True}]
    monkeypatch.setattr(server, 'fetch_all', fake_fetch)
    headers = {'Authorization': 'Bearer ' + token(private),
               'Accept': 'application/json, text/event-stream'}
    with TestClient(server.app, base_url=DEFAULT_AUDIENCE) as client:
        init = client.post('/mcp', headers=headers, json={
            'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
            'params': {'protocolVersion': '2025-11-25', 'capabilities': {},
                       'clientInfo': {'name': 'test', 'version': '1'}}})
        assert init.status_code == 200
        assert init.json()['result']['serverInfo']['name'] == 'harvest-time-tracking'
        headers['MCP-Protocol-Version'] = init.json()['result']['protocolVersion']
        listed = client.post('/mcp', headers=headers, json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
        assert len(listed.json()['result']['tools']) == 4
        for tool in listed.json()['result']['tools']:
            assert tool['_meta']['securitySchemes'][0]['scopes'] == ['read:harvest']
            assert tool['annotations']['readOnlyHint']
        called = client.post('/mcp', headers=headers, json={
            'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
            'params': {'name': 'list_projects', 'arguments': {}}})
        assert called.status_code == 200
        assert not called.json()['result'].get('isError')
        assert 'Example' in str(called.json()['result'])
