"""bootstrap.py's URL building and paste handling (no network)."""

from __future__ import annotations

import pytest

from bootstrap import BootstrapError, build_authorize_url, extract_code


def test_authorize_url_matches_documented_format():
    url = build_authorize_url("abc123", "xyz")
    assert url.startswith("https://cloud.lightspeedapp.com/auth/oauth/authorize?response_type=code&client_id=abc123&scope=")
    assert url.endswith("&state=xyz")
    scope = url.split("scope=")[1].split("&")[0]
    # Scopes joined by a literal '+' (as in Lightspeed's example), colons not encoded.
    assert scope.split("+")[:2] == ["employee:inventory_read", "employee:product_cost"]
    assert "%" not in scope


def test_full_redirect_url_with_matching_state():
    assert extract_code("https://localhost/callback?code=CODE123&state=xyz", "xyz") == "CODE123"


def test_url_without_scheme_and_with_quotes():
    assert extract_code('"localhost/callback?code=CODE123&state=xyz"', "xyz") == "CODE123"


def test_state_mismatch_is_rejected():
    with pytest.raises(BootstrapError, match="state"):
        extract_code("https://localhost/callback?code=CODE123&state=old", "xyz")


def test_bare_code_is_accepted():
    assert extract_code("  CODE123 ", "xyz") == "CODE123"


def test_denied_authorization_is_reported():
    with pytest.raises(BootstrapError, match="did not authorize"):
        extract_code("https://localhost/callback?error=access_denied&error_description=denied&state=xyz", "xyz")


def test_url_without_code_is_rejected():
    with pytest.raises(BootstrapError, match="code="):
        extract_code("https://localhost/callback?state=xyz", "xyz")
