from __future__ import annotations

import os
import asyncio
import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import Config, create_server, load_config, owner_only
from fastmcp.server.auth import AccessToken, AuthContext
from fastmcp.tools import Tool
from key_value.aio.stores.filetree import FileTreeStore
from key_value.aio.wrappers.encryption.fernet import FernetEncryptionWrapper
import httpx2
from fastmcp.server.auth.providers.github import GitHubTokenVerifier


class GatewayTests(unittest.TestCase):
    def test_missing_configuration_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "PERSONAL_INBOX_PUBLIC_URL"):
                load_config()

    def test_origin_and_owner_id_are_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {
                "PERSONAL_INBOX_PUBLIC_URL": "http://inbox.example.net",
                "PERSONAL_INBOX_GITHUB_CLIENT_ID": "client",
                "PERSONAL_INBOX_GITHUB_CLIENT_SECRET": "secret",
                "PERSONAL_INBOX_OWNER_GITHUB_ID": "12345",
                "PERSONAL_INBOX_JWT_SIGNING_KEY": "k" * 40,
                "PERSONAL_INBOX_AUTH_DIR": str(root / "auth"),
                "PERSONAL_INBOX_DIR": str(root / "data"),
                "PERSONAL_INBOX_NODE": "/usr/bin/node",
            }
            with patch.dict(os.environ, env, clear=True):
                with self.assertRaisesRegex(ValueError, "HTTPS origin"):
                    load_config()
            env["PERSONAL_INBOX_PUBLIC_URL"] = "https://inbox.example.net"
            env["PERSONAL_INBOX_OWNER_GITHUB_ID"] = "owner"
            with patch.dict(os.environ, env, clear=True):
                with self.assertRaisesRegex(ValueError, "numeric GitHub"):
                    load_config()

    def test_paths_are_checked_before_creation_or_permission_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth_dir, data_dir = root / "auth", root / "data"
            env = {
                "PERSONAL_INBOX_PUBLIC_URL": "https://inbox.example.net",
                "PERSONAL_INBOX_GITHUB_CLIENT_ID": "client",
                "PERSONAL_INBOX_GITHUB_CLIENT_SECRET": "secret",
                "PERSONAL_INBOX_OWNER_GITHUB_ID": "12345",
                "PERSONAL_INBOX_JWT_SIGNING_KEY": "k" * 40,
                "PERSONAL_INBOX_AUTH_DIR": str(auth_dir),
                "PERSONAL_INBOX_DIR": str(data_dir),
                "PERSONAL_INBOX_IMPORT_DIR": str(root / "outside-imports"),
                "PERSONAL_INBOX_NODE": shutil.which("node") or "/opt/homebrew/bin/node",
            }
            with patch.dict(os.environ, env, clear=True):
                with self.assertRaisesRegex(ValueError, "inside PERSONAL_INBOX_DIR"):
                    load_config()
            self.assertFalse(auth_dir.exists())
            self.assertFalse(data_dir.exists())

            data_dir.mkdir()
            data_dir.chmod(0o755)
            blocked = data_dir / "not-a-directory"
            blocked.write_text("test", encoding="utf-8")
            env["PERSONAL_INBOX_IMPORT_DIR"] = str(blocked / "imports")
            with patch.dict(os.environ, env, clear=True):
                with self.assertRaisesRegex(ValueError, "existing parents must be directories"):
                    load_config()
            self.assertFalse(auth_dir.exists())
            self.assertEqual(data_dir.stat().st_mode & 0o777, 0o755)

            env["PERSONAL_INBOX_IMPORT_DIR"] = str(data_dir / "imports")
            env["PERSONAL_INBOX_AUTH_DIR"] = str(Path(__file__).resolve().parents[3] / ".gateway-test-auth-must-not-exist")
            with patch.dict(os.environ, env, clear=True):
                with self.assertRaisesRegex(ValueError, "outside the Git repository"):
                    load_config()
            self.assertFalse(auth_dir.exists())
            self.assertEqual(data_dir.stat().st_mode & 0o777, 0o755)

            env["PERSONAL_INBOX_AUTH_DIR"] = str(auth_dir)
            env["PERSONAL_INBOX_NODE"] = "node"
            with patch.dict(os.environ, env, clear=True):
                with self.assertRaisesRegex(ValueError, "PERSONAL_INBOX_NODE must be an absolute path"):
                    load_config()

    def test_valid_config_initializes_private_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth_dir, data_dir = root / "auth", root / "data"
            env = {
                "PERSONAL_INBOX_PUBLIC_URL": "https://inbox.example.net",
                "PERSONAL_INBOX_GITHUB_CLIENT_ID": "client",
                "PERSONAL_INBOX_GITHUB_CLIENT_SECRET": "secret",
                "PERSONAL_INBOX_OWNER_GITHUB_ID": "12345",
                "PERSONAL_INBOX_JWT_SIGNING_KEY": "k" * 40,
                "PERSONAL_INBOX_AUTH_DIR": str(auth_dir),
                "PERSONAL_INBOX_DIR": str(data_dir),
                "PERSONAL_INBOX_NODE": str(Path(shutil.which("node") or "/opt/homebrew/bin/node").resolve()),
            }
            with patch.dict(os.environ, env, clear=True):
                config = load_config()
            self.assertEqual(config.import_dir, (data_dir / "imports").resolve())
            self.assertTrue(auth_dir.is_dir())
            self.assertTrue(data_dir.is_dir())
            self.assertTrue(config.import_dir.is_dir())
            self.assertEqual(auth_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(data_dir.stat().st_mode & 0o777, 0o700)

    def test_owner_gate_requires_verified_numeric_identity_in_both_fields(self):
        check = owner_only("4242")
        component = Tool(name="x", description="x", parameters={})
        self.assertFalse(check(AuthContext(token=None, component=component)))
        self.assertFalse(check(AuthContext(
            token=AccessToken(token="x", client_id="c", scopes=[], subject="4242", claims={}),
            component=component,
        )))
        self.assertFalse(check(AuthContext(
            token=AccessToken(token="x", client_id="c", scopes=[], subject="7", claims={"sub": "4242"}),
            component=component,
        )))
        self.assertFalse(check(AuthContext(
            token=AccessToken(token="x", client_id="c", scopes=[], subject="4242", claims={"sub": "7"}),
            component=component,
        )))
        self.assertTrue(check(AuthContext(
            token=AccessToken(token="x", client_id="c", scopes=[], subject="4242", claims={"sub": "4242"}),
            component=component,
        )))

    def test_server_mounts_oauth_provider_mcp_route_and_owner_middleware(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config(
                public_url="https://inbox.example.net",
                github_client_id="placeholder-client",
                github_client_secret="placeholder-secret",
                owner_github_id="4242",
                jwt_signing_key="test-signing-material-at-least-32-bytes",
                auth_dir=root / "auth",
                data_dir=root / "data",
                import_dir=root / "data" / "imports",
                node=Path("/usr/bin/node"),
            )
            server = create_server(config)
            self.assertEqual(server.provider_error_strategy, "raise")
            self.assertEqual(server.middleware[0].auth(AuthContext(
                token=AccessToken(token="t", client_id="c", scopes=[], subject="4242", claims={"sub": "4242"}),
                component=Tool(name="x", description="x", parameters={}),
            )), True)
            app = server.http_app(path="/mcp")
            routes = {getattr(route, "path", None) for route in app.routes}
            self.assertIn("/mcp", routes)
            self.assertIn("/auth/callback", routes)
            self.assertIn("/.well-known/oauth-authorization-server", routes)

    def test_asgi_metadata_and_mcp_reject_unauthenticated_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config(
                public_url="https://inbox.example.net",
                github_client_id="placeholder-client",
                github_client_secret="placeholder-secret",
                owner_github_id="4242",
                jwt_signing_key="test-signing-material-at-least-32-bytes",
                auth_dir=root / "auth",
                data_dir=root / "data",
                import_dir=root / "data" / "imports",
                node=Path("/usr/bin/node"),
            )
            app = create_server(config).http_app(path="/mcp")

            async def request(path: str, method: str = "GET"):
                sent = []
                messages = [{"type": "http.request", "body": b"", "more_body": False}]
                async def receive():
                    return messages.pop(0) if messages else {"type": "http.disconnect"}
                async def send(message):
                    sent.append(message)
                await app(
                    {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                     "method": method, "scheme": "https", "path": path, "raw_path": path.encode(),
                     "query_string": b"", "headers": [(b"host", b"inbox.example.net")],
                     "client": ("127.0.0.1", 1234), "server": ("inbox.example.net", 443)},
                    receive, send,
                )
                status = next(m["status"] for m in sent if m["type"] == "http.response.start")
                body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
                return status, body

            status, body = asyncio.run(request("/.well-known/oauth-authorization-server"))
            self.assertEqual(status, 200)
            discovery = json.loads(body)
            self.assertEqual(discovery["issuer"], "https://inbox.example.net/")
            self.assertEqual(discovery["authorization_endpoint"], "https://inbox.example.net/authorize")
            status, _ = asyncio.run(request("/mcp", method="POST"))
            self.assertEqual(status, 401)


class StorageTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _test_node() -> Path:
        explicit = os.environ.get("PERSONAL_INBOX_TEST_NODE")
        candidates = [Path(explicit).expanduser()] if explicit else []
        candidates.extend([
            Path.home() / ".nvm/versions/node/v24.22.1/bin/node",
            Path(shutil.which("node") or "/opt/homebrew/bin/node"),
        ])
        for candidate in candidates:
            candidate = candidate.resolve()
            if candidate.is_file() and os.access(candidate, os.X_OK):
                version = subprocess.run([str(candidate), "--version"], check=True, capture_output=True, text=True).stdout.strip().lstrip("v")
                try:
                    meets_minimum = tuple(map(int, version.split("."))) >= (24, 22, 1)
                except ValueError:
                    meets_minimum = False
                if meets_minimum:
                    return candidate
        raise unittest.SkipTest("Node.js >=24.22.1 is required for the stdio integration test")

    async def test_oauth_store_is_encrypted_and_persistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "oauth"
            key = "stable test signing material of at least 32 bytes"
            first = FernetEncryptionWrapper(
                FileTreeStore(data_directory=path),
                source_material=key,
                salt="personal-inbox-gateway-oauth-storage-v1",
            )
            await first.put("client", {"refresh_token": "secret-refresh-token"})
            blobs = [p.read_bytes() for p in path.rglob("*") if p.is_file()]
            self.assertTrue(blobs)
            self.assertNotIn(b"secret-refresh-token", b"".join(blobs))
            reopened = FernetEncryptionWrapper(
                FileTreeStore(data_directory=path),
                source_material=key,
                salt="personal-inbox-gateway-oauth-storage-v1",
            )
            self.assertEqual(await reopened.get("client"), {"refresh_token": "secret-refresh-token"})

    async def test_github_verifier_propagates_numeric_user_id_to_subject_and_sub(self):
        async def responder(request):
            if request.url.path == "/user":
                return httpx2.Response(200, json={"id": 4242, "login": "inbox-owner"})
            return httpx2.Response(200, headers={"x-oauth-scopes": "read:user"}, json=[])

        client = httpx2.AsyncClient(transport=httpx2.MockTransport(responder))
        verifier = GitHubTokenVerifier(required_scopes=["read:user"], http_client=client)
        try:
            token = await verifier.verify_token("synthetic-github-token")
        finally:
            await client.aclose()
        self.assertIsNotNone(token)
        assert token is not None
        self.assertEqual(token.subject, "4242")
        self.assertEqual(token.claims["sub"], "4242")

    async def test_signed_oauth_tokens_reach_real_node_and_deny_other_github_user(self):
        from fastmcp.server.auth.oauth_proxy.models import JTIMapping, UpstreamTokenSet

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            import_dir = data_dir / "imports"
            import_dir.mkdir(parents=True)
            config = Config(
                public_url="https://inbox.example.test",
                github_client_id="synthetic-client",
                github_client_secret="synthetic-secret",
                owner_github_id="4242",
                jwt_signing_key="synthetic-integration-signing-key-32-bytes-minimum",
                auth_dir=root / "auth",
                data_dir=data_dir,
                import_dir=import_dir,
                node=self._test_node(),
            )
            server = create_server(config)
            app = server.http_app(path="/mcp", stateless_http=True, json_response=True)
            auth = server.auth
            self.assertEqual(auth.required_scopes, ["read:user"])

            async def github_api(request):
                if request.url.path == "/user":
                    uid = 4242 if request.headers.get("authorization") == "Bearer upstream-owner" else 7
                    return httpx2.Response(200, json={"id": uid, "login": "synthetic"})
                return httpx2.Response(200, headers={"x-oauth-scopes": "read:user"}, json=[])

            async def seed(label: str):
                token_id, jti = f"token-{label}", f"jti-{label}"
                await auth._upstream_token_store.put(
                    key=token_id,
                    value=UpstreamTokenSet(
                        upstream_token_id=token_id,
                        access_token=f"upstream-{label}",
                        refresh_token=None,
                        refresh_token_expires_at=None,
                        expires_at=time.time() + 3600,
                        token_type="Bearer",
                        scope="read:user",
                        client_id=f"client-{label}",
                        created_at=time.time(),
                    ),
                )
                await auth._jti_mapping_store.put(
                    key=jti,
                    value=JTIMapping(jti=jti, upstream_token_id=token_id, created_at=time.time()),
                )
                return auth.jwt_issuer.issue_access_token(
                    client_id=f"client-{label}", scopes=["read:user"], jti=jti
                )

            async with httpx2.AsyncClient(transport=httpx2.MockTransport(github_api)) as github:
                auth._token_validator._http_client = github
                owner_token = await seed("owner")
                other_token = await seed("other")
                owner_identity = await auth.load_access_token(owner_token)
                other_identity = await auth.load_access_token(other_token)
                self.assertEqual(owner_identity.subject, "4242")
                self.assertEqual(owner_identity.claims["sub"], "4242")
                self.assertEqual(other_identity.subject, "7")
                self.assertEqual(other_identity.claims["sub"], "7")

                async with app.router.lifespan_context(app):
                    async with httpx2.AsyncClient(
                        transport=httpx2.ASGITransport(app=app), base_url=config.public_url
                    ) as client:
                        async def rpc(token: str, method: str, params: dict, request_id: int):
                            response = await client.post(
                                "/mcp",
                                headers={
                                    "Authorization": f"Bearer {token}",
                                    "Accept": "application/json,text/event-stream",
                                    "Content-Type": "application/json",
                                },
                                json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
                            )
                            self.assertEqual(response.status_code, 200, response.text)
                            return response.json()

                        init = {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "gateway-test", "version": "1"}}
                        await rpc(owner_token, "initialize", init, 1)
                        await rpc(other_token, "initialize", init, 2)
                        listed = await rpc(owner_token, "tools/list", {}, 3)
                        self.assertEqual(len(listed["result"]["tools"]), 9)
                        saved = await rpc(owner_token, "tools/call", {
                            "name": "save_note",
                            "arguments": {"title": "gateway-integration", "body": "synthetic-marker", "task": "gateway-test", "run_key": "owner-save"},
                        }, 4)
                        receipt = json.loads(saved["result"]["content"][0]["text"])
                        self.assertTrue(receipt["saved"])
                        read = await rpc(owner_token, "tools/call", {
                            "name": "read_update", "arguments": {"id": receipt["id"]},
                        }, 5)
                        self.assertEqual(json.loads(read["result"]["content"][0]["text"])["body"], "synthetic-marker")
                        self.assertEqual((await rpc(other_token, "tools/list", {}, 6))["result"]["tools"], [])
                        denied = await rpc(other_token, "tools/call", {
                            "name": "save_note",
                            "arguments": {"title": "denied", "body": "must not save", "task": "gateway-test", "run_key": "other-save"},
                        }, 7)
                        self.assertTrue(denied["result"]["isError"])
                        absent = await rpc(owner_token, "tools/call", {
                            "name": "get_receipt", "arguments": {"task": "gateway-test", "run_key": "other-save"},
                        }, 8)
                        self.assertFalse(json.loads(absent["result"]["content"][0]["text"])["saved"])

    async def test_broken_stdio_backend_returns_jsonrpc_error_not_empty_tool_list(self):
        from fastmcp.server.auth.oauth_proxy.models import JTIMapping, UpstreamTokenSet

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data" / "imports").mkdir(parents=True)
            config = Config(
                public_url="https://inbox.example.test",
                github_client_id="synthetic-client",
                github_client_secret="synthetic-secret",
                owner_github_id="4242",
                jwt_signing_key="synthetic-integration-signing-key-32-bytes-minimum",
                auth_dir=root / "auth",
                data_dir=root / "data",
                import_dir=root / "data" / "imports",
                node=Path(shutil.which("false") or "/usr/bin/false"),
            )
            server = create_server(config)
            app = server.http_app(path="/mcp", stateless_http=True, json_response=True)
            auth = server.auth

            async def github_api(request):
                if request.url.path == "/user":
                    return httpx2.Response(200, json={"id": 4242, "login": "synthetic"})
                return httpx2.Response(200, headers={"x-oauth-scopes": "read:user"}, json=[])

            async with httpx2.AsyncClient(transport=httpx2.MockTransport(github_api)) as github:
                auth._token_validator._http_client = github
                token_id, jti = "token-owner", "jti-owner"
                await auth._upstream_token_store.put(key=token_id, value=UpstreamTokenSet(
                    upstream_token_id=token_id, access_token="upstream-owner", refresh_token=None,
                    refresh_token_expires_at=None, expires_at=time.time() + 3600,
                    token_type="Bearer", scope="read:user", client_id="client-owner", created_at=time.time(),
                ))
                await auth._jti_mapping_store.put(key=jti, value=JTIMapping(
                    jti=jti, upstream_token_id=token_id, created_at=time.time(),
                ))
                token = auth.jwt_issuer.issue_access_token(
                    client_id="client-owner", scopes=["read:user"], jti=jti,
                )
                async with app.router.lifespan_context(app):
                    async with httpx2.AsyncClient(
                        transport=httpx2.ASGITransport(app=app, raise_app_exceptions=False),
                        base_url=config.public_url,
                    ) as client:
                        response = await client.post(
                            "/mcp",
                            headers={"Authorization": f"Bearer {token}", "Accept": "application/json,text/event-stream", "Content-Type": "application/json"},
                            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "backend-failure", "version": "1"}}},
                        )
                        self.assertEqual(response.status_code, 200, response.text)
                        response = await client.post(
                            "/mcp",
                            headers={"Authorization": f"Bearer {token}", "Accept": "application/json,text/event-stream", "Content-Type": "application/json"},
                            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                        )
                        self.assertEqual(response.status_code, 200, response.text)
                        body = response.json()
                        self.assertIn("error", body)
                        self.assertNotIn("result", body)


if __name__ == "__main__":
    unittest.main()
