"""MCP resource-server authentication. Auth0 owns login, PKCE and token issuance."""
import asyncio
import os
from contextvars import ContextVar
from urllib.parse import urlsplit

import jwt
from starlette.responses import JSONResponse

REQUIRED_SCOPE = 'read:harvest'
WRITE_SCOPE = 'write:harvest'
granted_permissions = ContextVar('harvest_permissions', default=frozenset())


def require_permission(permission):
    if permission not in granted_permissions.get():
        raise PermissionError(f'This operation requires the assigned OAuth permission {permission}')
DEFAULT_ISSUER = 'https://dev-n2yqg305b2t0tq1x.us.auth0.com/'
DEFAULT_AUDIENCE = 'https://harvest-connector-yvdj.onrender.com'


def https_url(value):
    url = urlsplit(value)
    if (url.scheme != 'https' or not url.netloc or url.username or url.password
            or url.query or url.fragment):
        raise ValueError('OAuth configuration requires an absolute HTTPS URL')
    return value


class RequireOAuth:
    def __init__(self, app):
        self.app = app
        self.issuer = https_url(os.getenv('AUTH0_ISSUER', DEFAULT_ISSUER))
        if not self.issuer.endswith('/'):
            raise ValueError('AUTH0_ISSUER must match Auth0 exactly, including its trailing slash')
        self.audience = https_url(os.getenv('AUTH0_AUDIENCE', DEFAULT_AUDIENCE))
        # Use the existing Auth0 API identifier as the RFC 8707 resource identifier.
        self.metadata_url = self.audience.rstrip('/') + '/.well-known/oauth-protected-resource'
        self.jwks = jwt.PyJWKClient(self.issuer + '.well-known/jwks.json',
                                    cache_jwk_set=True, lifespan=300, timeout=10)
        self.metadata = {
            'resource': self.audience,
            'resource_name': 'Harvest Time Tracking',
            'authorization_servers': [self.issuer],
            'scopes_supported': [REQUIRED_SCOPE, WRITE_SCOPE],
            'bearer_methods_supported': ['header'],
        }

    def validate(self, token):
        # Reject algorithm confusion before fetching keys; never trust token jku/x5u.
        header = jwt.get_unverified_header(token)
        if header.get('alg') != 'RS256' or not isinstance(header.get('kid'), str):
            raise jwt.InvalidTokenError('Unsupported signing header')
        key = self.jwks.get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=['RS256'], issuer=self.issuer,
                            audience=self.audience,
                            options={'require': ['iss', 'aud', 'exp', 'iat', 'sub']})
        scopes = claims.get('scope', '')
        permissions = claims.get('permissions', [])
        # Both delegated scope and administrator-assigned RBAC permission are needed.
        # All authorized users access the one Harvest account configured in Render.
        if (not isinstance(scopes, str) or REQUIRED_SCOPE not in scopes.split()
                or not isinstance(permissions, list) or REQUIRED_SCOPE not in permissions):
            return False
        return frozenset(scopes.split()).intersection(p for p in permissions if isinstance(p, str))

    def error(self, status, error=None):
        challenge = f'Bearer resource_metadata="{self.metadata_url}", scope="{REQUIRED_SCOPE}"'
        if error:
            challenge += f', error="{error}"'
        body = {'error': error or 'unauthorized'}
        return JSONResponse(body, status_code=status, headers={
            'WWW-Authenticate': challenge, 'Cache-Control': 'no-store'})

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        path = scope.get('path', '')
        if path in ('/.well-known/oauth-protected-resource',
                    '/.well-known/oauth-protected-resource/mcp'):
            response = (JSONResponse(self.metadata) if scope['method'] in ('GET', 'HEAD')
                        else JSONResponse({'error': 'method_not_allowed'}, 405))
            await response(scope, receive, send)
            return
        if path == '/health' and scope['method'] in ('GET', 'HEAD'):
            await JSONResponse({'status': 'ok'})(scope, receive, send)
            return
        authorization = [v for k, v in scope.get('headers', []) if k.lower() == b'authorization']
        if not authorization:
            await self.error(401)(scope, receive, send)
            return
        parts = authorization[0].decode('latin-1').split()
        if len(authorization) != 1 or len(parts) != 2 or parts[0].lower() != 'bearer':
            await self.error(401, 'invalid_token')(scope, receive, send)
            return
        try:
            permitted = await asyncio.to_thread(self.validate, parts[1])
        except jwt.PyJWKClientConnectionError:
            await JSONResponse({'error': 'temporarily_unavailable'}, 503,
                               headers={'Cache-Control': 'no-store', 'Retry-After': '10'})(scope, receive, send)
            return
        except (jwt.PyJWTError, ValueError, TypeError):
            await self.error(401, 'invalid_token')(scope, receive, send)
            return
        if not permitted:
            await self.error(403, 'insufficient_scope')(scope, receive, send)
            return
        context = granted_permissions.set(permitted)
        try:
            await self.app(scope, receive, send)
        finally:
            granted_permissions.reset(context)
