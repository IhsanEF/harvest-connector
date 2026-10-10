# Harvest MCP connector

Read tools: `list_people`, `list_projects`, `list_time_entries`, `get_time_entry`, `hours_summary`, `export_time_report`. Editing tool: `update_time_entry`. Exports return CSV content and a filename for the host to save as a downloadable file.

MCP endpoint: https://harvest-connector-yvdj.onrender.com/mcp

## Run on Render

Use Python 3.12 or newer. Build: `pip install -r requirements.txt`.
Start: `uvicorn server:app --host 0.0.0.0 --port $PORT`.
Health check: `/health`.

Keep `HARVEST_ACCESS_TOKEN` and `HARVEST_ACCOUNT_ID` in Render's environment.
Optional: `HARVEST_USER_AGENT`. Never place secrets in source, plugin files or chat.

OAuth defaults match this service. Optional environment overrides:

* `AUTH0_ISSUER=https://dev-n2yqg305b2t0tq1x.us.auth0.com/`
* `AUTH0_AUDIENCE=https://harvest-connector-yvdj.onrender.com`

The audience is also the RFC 8707 resource identifier; it has no `/mcp` suffix
and no trailing slash. The issuer must include its trailing slash.

## Auth0 setup

1. Tenant **Settings > Advanced > Settings**: enable **Resource Parameter
   Compatibility Profile** so OAuth `resource` selects the Harvest API audience.
   Keep **Include Issuer in Authorization Responses** enabled (currently
   advertised as enabled by the tenant's public discovery document).
2. **Applications > APIs > Harvest API > Permissions**: add `read:harvest` and `write:harvest`.
   Under **Settings**, retain RS256, enable **RBAC** and **Add Permissions in the
   Access Token**. Assign this permission through a role only to approved users.
   Every approved user can read the single Harvest account stored in Render.
3. The tenant currently advertises DCR at `/oidc/register` and PKCE S256, but
   does not advertise CIMD. The plugin uses DCR. Enable **Dynamic Client
   Registration** in tenant advanced settings. Under the Harvest API's
   **Settings > Default Permissions for Third-Party Applications**, select
   **Authorized for User-Delegated Access** and select only `read:harvest` and `write:harvest`.
   Do not enable Client Access. DCR requires this default client grant; it lets
   third-party clients request the permission, while RBAC and user consent still
   control who can actually read Harvest. Under **Authentication > Database
   (or your chosen connection) > Settings > Advanced**, enable **Promote
   Connection to Domain Level**, as third-party clients require domain-level
   connections. Keep user consent enabled. If allowing third-party registration
   is unsuitable, use the existing regular web application as a provided client
   instead, as described below.
4. Verify the exact redirect shown by ChatGPT's connection management page.
   This tenant advertises RFC 9207 issuer identification, so the current stable
   ChatGPT redirect is `https://chatgpt.com/connector_platform_oauth_redirect`.
   DCR sends its redirect URIs during registration. For a provided client,
   allowlist that exact URI, enable Authorization Code with S256 PKCE, use
   `client_secret_post` or `client_secret_basic`, and enter its client secret
   only in ChatGPT's protected connection setup. Configure `client.mode` as
   `provided` and its real public `clientId` in the plugin; never invent an ID.
   If ChatGPT shows a callback-specific URI, copy that exact URI instead.
5. CIMD is an optional future alternative. Enable Auth0's CIMD capability and
   register the exact ChatGPT metadata URI shown in connection management
   (stable mode: `https://chatgpt.com/oauth/client.json`). Grant that specific
   client access. Auth0 currently requires an Enterprise plan for CIMD's
   `private_key_jwt` method, which ChatGPT's stable metadata prefers. Verify
   the imported client's actual supported authentication method before relying
   on CIMD. Switch the plugin to `client.mode: cimd` only after the tenant
   advertises `client_id_metadata_document_supported: true`.

Official references:
* https://developers.openai.com/plugins/build/auth
* https://developers.openai.com/plugins/build/plugins
* https://auth0.com/docs/get-started/applications/third-party-applications/security-controls
* https://support.auth0.com/center/s/article/mcp-audience-error-with-auth0

## Security and verification

Auth0 owns authorization-server discovery, client registration, login, consent,
PKCE and token exchange. This server publishes RFC 9728 protected-resource
metadata at `/.well-known/oauth-protected-resource` and its `/mcp` variant.
Unauthenticated requests receive a 401 discovery challenge. Tokens must pass
RS256 verification using Auth0's cached and refreshable JWKS, exact issuer and
audience checks, expiry and issued-at checks, and subject validation. Access also
requires both `scope` and Auth0 RBAC `permissions` to contain `read:harvest`. Editing additionally requires `write:harvest` in both claims.
Missing permissions return 403. JWKS outages return 503 without granting access.
The old static `MCP_BEARER_TOKEN` is no longer accepted. Discovery and health
return no Harvest data. DNS rebinding protection remains enabled.

Run `pip install -r requirements.txt pytest` then `pytest -q`.
Tests use generated RSA keys and synthetic Harvest data, never real credentials.
They cover invalid signatures, issuer, audience, expiry, missing claims,
algorithm confusion, old static tokens, permissions, discovery, outages and
authenticated MCP initialize/list/call. A real ChatGPT login and Harvest query
still require completing the Auth0 client grant and assigning the user's role.

## Editing and exports

Give Taha both read:harvest and write:harvest; other read-only users need only read:harvest. Editing uses PATCH for a single existing entry and only fields specified by the user. First call get_time_entry (or use list_time_entries) and pass the current updated_at value. The server checks that value immediately before PATCH and refuses locked, billed, approved or running entries. Harvest does not provide an atomic conditional PATCH here, so edits made concurrently after this check can still race. Successful edits return before/after values. No create/delete or billing tools are exposed.

export_time_report returns entries or grouped hours as CSV, applying optional employee/project filters. Untrusted cells are escaped for spreadsheet import. The host can create a downloadable CSV or an Excel workbook from retrieved data. No public file hosting or Harvest secrets are involved.
