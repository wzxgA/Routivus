from __future__ import annotations

import pytest

from routivus.web.errors import WebSecurityError
from routivus.web.url_policy import URLPolicy


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "javascript:alert(1)", "http://localhost/a",
    "http://127.0.0.1/a", "http://10.0.0.1/a", "http://[::1]/a",
    "https://user:pass@example.com/a", "https://example.com:22/a",
])
def test_unsafe_urls_rejected(url):
    with pytest.raises((WebSecurityError, ValueError)):
        URLPolicy(resolve_dns=False).validate(url)


def test_dns_result_is_checked():
    def resolver(host, port, **kwargs):
        return [(0, 0, 0, "", ("192.168.1.2", port))]
    with pytest.raises(WebSecurityError):
        URLPolicy(resolver=resolver).validate("https://public.example/a")


def test_redirect_is_resolved_relative_and_validated():
    policy = URLPolicy(resolve_dns=False)
    result = policy.validate_redirect("https://example.com/a", "/next")
    assert result.url == "https://example.com/next"
