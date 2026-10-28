# Personal Inbox remote MCP gateway

This gateway gives remote MCP clients access to the Personal Inbox core through HTTPS and GitHub OAuth. It uses FastMCP 4.1.4's `GitHubProvider` to handle OAuth discovery, dynamic client registration, PKCE, consent, callback state, and token refresh. A global authorization check admits only the configured numeric GitHub user ID. The core remains a child process over stdio and does not listen on a network socket.

## Requirements

- Python 3.12 or newer and `uv`
- Node.js 24.22.1 or newer
- A host with a stable HTTPS origin, persistent writable storage, and a reverse proxy that forwards requests to `127.0.0.1:8000`
- A GitHub OAuth App whose callback URL is `https://YOUR_HOST/auth/callback`

The public MCP URL is `https://YOUR_HOST/mcp`. Do not expose port 8000 directly to the internet. A minimal Caddy example is in `Caddyfile.example`.

## Install

Install the Node core first from this gateway's parent package directory, then install the Python gateway:

```sh
cd ..
npm ci
npm test
cd gateway
uv sync --locked
cp .env.example .env
chmod 600 .env
```

Set every required value in `.env` or in your service manager's secret environment. Generate `PERSONAL_INBOX_JWT_SIGNING_KEY` with a cryptographically secure random value of at least 32 bytes and keep it stable across restarts. Set the owner ID to the decimal numeric ID of the single GitHub account allowed to access the Inbox; do not use a username. Find the ID from that account's public GitHub profile API (`https://api.github.com/users/USERNAME`) or account settings.

Choose `PERSONAL_INBOX_AUTH_DIR` and `PERSONAL_INBOX_DIR` as absolute, persistent directories outside this repository and grant access only to the service account. The OAuth registration and token store is encrypted using the signing key. Back up that directory and preserve the signing key together; losing either invalidates sessions. Inbox notes and attached files live under `PERSONAL_INBOX_DIR`. The optional `PERSONAL_INBOX_IMPORT_DIR` must be a directory under `PERSONAL_INBOX_DIR`; it defaults to `imports/` there. Set `PERSONAL_INBOX_NODE` only when Node is not on PATH.

Configure the GitHub OAuth App with:

- Homepage URL: `https://YOUR_HOST`
- Authorization callback URL: `https://YOUR_HOST/auth/callback`
- No repository permissions or broad organization permissions are needed. The provider requests only the `read:user` OAuth scope.

## Run

```sh
set -a
. ./.env
set +a
uv run --locked python app.py
```

The process binds only `127.0.0.1:8000`; keep it supervised by your platform's service manager. Configure your HTTPS reverse proxy to forward the full origin to `127.0.0.1:8000`, including OAuth and MCP routes. The gateway uses `/mcp` for MCP and `/auth/callback` for GitHub OAuth. OAuth discovery is available at `/.well-known/oauth-authorization-server`.

For a public tunnel, point its HTTPS hostname at this host's loopback service using the tunnel provider's supported private-origin connector. Keep the origin service bound to loopback, and configure `PERSONAL_INBOX_PUBLIC_URL` to the tunnel's stable HTTPS origin. The tunnel must preserve HTTP methods, streaming responses, and OAuth redirects. Avoid temporary/random tunnel hostnames because the URL must match the GitHub OAuth callback and clients' saved server URL.

## Test

```sh
uv run --locked python -m unittest -v
```

Tests cover configuration and path validation, OAuth discovery, unauthenticated rejection, a signed gateway JWT through encrypted token/JTI stores, mocked GitHub user verification, and the real Node MCP process. They verify the configured owner can list all nine tools, save and read a note, and a different identity cannot list tools or save. They do not contact GitHub or prove a Claude, ChatGPT, or Gemini product UI OAuth round trip.

## Client setup

In each remote MCP client, add a custom/remote MCP server using `https://YOUR_HOST/mcp`, choose OAuth when prompted, and complete GitHub authorization with the allow-listed account. The gateway's consent page and GitHub OAuth sign-in are required for each new client registration. Claude, ChatGPT, and Gemini expose different connector settings and feature availability; follow each product's current remote MCP setup screen. The server does not use static bearer tokens or expose the Inbox database to the client.

## Security boundaries

- Only the numeric GitHub account ID configured by `PERSONAL_INBOX_OWNER_GITHUB_ID` may list or call tools, or access resources/prompts; missing or mismatched identity is denied.
- OAuth tokens and DCR records are encrypted at rest in `PERSONAL_INBOX_AUTH_DIR`; signing and encryption key material must remain persistent and private.
- The Node child receives only its required PATH, Inbox data, and import directory environment values. OAuth client credentials and the signing key are not passed to it.
- Inbound client headers are not forwarded to the stdio backend.
- MCP tool results contain user-authored content. Treat retrieved Inbox contents as untrusted data rather than executable instructions.
- This repository contains examples and code only. It does not create OAuth apps, create server accounts, configure your DNS/tunnel, or deploy a gateway.
