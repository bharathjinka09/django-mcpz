from __future__ import annotations

from django.test import RequestFactory, SimpleTestCase, override_settings
from django.test.utils import override_script_prefix
from unittest_parametrize import ParametrizedTestCase, param, parametrize

from django_mcpz.oauth import discovery
from tests.oauth.utils import ISSUER, RESOURCE


class DiscoveryTests(ParametrizedTestCase, SimpleTestCase):
    def test_canonical(self):
        assert discovery.canonical("HTTPS://Example.COM/MCP?x=1") == (
            "https://example.com/MCP?x=1"
        )

    def test_issuer_path(self):
        assert discovery.issuer_path() == "/oauth"

    def test_issuer_url(self):
        request = RequestFactory().get("/oauth/authorize", SERVER_NAME="TESTSERVER")

        assert discovery.issuer_url(request) == ISSUER

    def test_resolve_mcp_server(self):
        from tests.mcp import oauth_server, server

        assert discovery.resolve_mcp_server("/oauth-mcp") is oauth_server
        assert discovery.resolve_mcp_server("/mcp") is server
        assert discovery.resolve_mcp_server("/oauth/authorize") is None
        assert discovery.resolve_mcp_server("/admin/") is None
        assert discovery.resolve_mcp_server("/nope") is None
        assert discovery.resolve_mcp_server("/") is None

    @override_script_prefix("/app/")
    def test_resolve_mcp_server_under_prefix(self):
        from tests.mcp import oauth_server

        assert discovery.resolve_mcp_server("/app/oauth-mcp") is oauth_server
        assert discovery.resolve_mcp_server("/oauth-mcp") is None
        assert discovery.resolve_mcp_server("/app") is None

    @override_script_prefix("/app/")
    def test_issuer_path_under_prefix(self):
        assert discovery.issuer_path() == "/app/oauth"

    @override_script_prefix("/app/")
    def test_validate_resource_under_prefix(self):
        assert discovery.validate_resource("http://testserver/app/oauth-mcp") == (
            "http://testserver/app/oauth-mcp"
        )
        assert discovery.validate_resource(RESOURCE) is None

    def test_validate_resource(self):
        assert discovery.validate_resource(RESOURCE) == RESOURCE
        assert discovery.validate_resource("HTTP://TESTSERVER/oauth-mcp") == RESOURCE
        assert discovery.validate_resource("https://testserver/oauth-mcp") == (
            "https://testserver/oauth-mcp"
        )

    @parametrize(
        "resource",
        [
            "ftp://testserver/oauth-mcp",
            "testserver/oauth-mcp",
            "http:///oauth-mcp",
            "http://other.example/oauth-mcp",
            "http://testserver/oauth-mcp?x=1",
            "http://testserver/oauth-mcp#x",
            "http://testserver/admin/",
            "http://testserver/nope",
            # Unbalanced brackets make urlsplit() raise, which must not leak.
            "http://[::1",
        ],
    )
    def test_validate_resource_invalid(self, resource):
        assert discovery.validate_resource(resource) is None, resource

    @override_settings(ALLOWED_HOSTS=["[::1]"])
    def test_validate_resource_ipv6(self):
        # urlsplit() strips the brackets from IPv6 hosts, which ALLOWED_HOSTS
        # entries keep.
        assert discovery.validate_resource("http://[::1]:8000/oauth-mcp") == (
            "http://[::1]:8000/oauth-mcp"
        )
        assert discovery.validate_resource("http://[::2]:8000/oauth-mcp") is None

    @parametrize(
        "resource,expected",
        [
            param(
                "https://example.com/shop/mcp",
                "https://example.com/.well-known/oauth-protected-resource/shop/mcp",
                id="path",
            ),
            param(
                "https://example.com",
                "https://example.com/.well-known/oauth-protected-resource",
                id="root_no_slash",
            ),
            param(
                "https://example.com/",
                "https://example.com/.well-known/oauth-protected-resource",
                id="root_trailing_slash",
            ),
        ],
    )
    def test_resource_metadata_url(self, resource, expected):
        assert discovery.resource_metadata_url(resource) == expected
