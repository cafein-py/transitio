import os
import re
import urllib.parse

import h11
import httpx
import pytest

from transitio.catalog._access import (
    _Borrowed,
    _proxy,
    _redact,
    _Secret,
    _secrets,
    _unsendable,
)
from transitio.exceptions import DownloadError

# Reserved characters and a space; SHORT is a prefix of LONG.
LONG = "a&b=c/d%e f"
SHORT = "a&b"


def test_secret_reads_as_a_mask():
    secret = _Secret("s3cr3t")
    assert repr(secret) == str(secret) == f"{secret}" == "***"
    assert repr({"key": secret, "all": [secret]}) == "{'key': ***, 'all': [***]}"
    assert not hasattr(secret, "__dict__")
    assert secret.reveal() == "s3cr3t"
    for value in ("", None, b"s3cr3t"):
        with pytest.raises(ValueError, match="non-empty string"):
            _Secret(value)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"raw {LONG}.", "raw ***."),
        (
            str(httpx.URL("https://x.org/f", params={"k": LONG})),
            "https://x.org/f?k=***",
        ),
        ("q=" + urllib.parse.quote(LONG, safe=""), "q=***"),
        ("q=a%26b%3dc%2fd%25e%20f", "q=***"),
        ("q=a&b%3Dc/d%25e+f", "q=***"),
        (f"{SHORT}x {LONG}", "***x ***"),
        (f"{SHORT}{LONG}{SHORT}", "***"),
        ("A&B a&c a%b a&b=c/d%e", "A&B a&c a%b ***=c/d%e"),
        ("nothing to hide", "nothing to hide"),
    ],
)
def test_redact_masks_every_form(text, expected):
    secrets = (_Secret(SHORT), _Secret(LONG))
    assert _redact(text, secrets) == expected
    assert any(secret.occurs_in(text) for secret in secrets) == (text != expected)


def test_basic_auth_secrets_are_its_wire_forms():
    fields = {"username": _Secret("user"), "password": _Secret("p@ss:w0rd")}
    request = httpx.Request("GET", "https://x.org/")
    header = next(httpx.BasicAuth("user", "p@ss:w0rd").auth_flow(request))
    header = header.headers["Authorization"]
    secrets = _secrets("basic_auth", fields)
    assert [secret.reveal() for secret in secrets] == [
        "p@ss:w0rd",
        header.removeprefix("Basic "),
        header,
    ]
    assert _redact(f"user: {header}", secrets) == "user: ***"
    keys = {"client_id": _Secret("id"), "client_secret": _Secret("secret")}
    assert _secrets("query_param", keys) == tuple(keys.values())


def _request(method, auth_params, values):
    """The request a method's credentials make, built by httpx."""
    if method == "query_param":
        params = {name: values[field] for name, field in auth_params.items()}
        return httpx.Request("GET", "https://x.org/f", params=params)
    if method == "header":
        headers = {name: values[field] for name, field in auth_params.items()}
    else:
        fields = {name: _Secret(value) for name, value in values.items()}
        _, header = _Secret.basic(fields["username"], fields["password"])
        headers = {"Authorization": header.reveal()}
    return httpx.Request("GET", "https://x.org/f", headers=headers)


@pytest.mark.parametrize(
    ("method", "auth_params", "values", "unsendable"),
    [
        ("query_param", {"key": "key"}, {"key": "a b&c=/%+é😀\x00"}, None),
        (
            "query_param",
            {"id": "client_id", "secret": "client_secret"},
            {"client_id": "id", "client_secret": "s\ud800"},
            "client_secret",
        ),
        ("header", {"X-Api-Key": "token"}, {"token": "!a  b~"}, None),
        ("header", {"X-Api-Key": "token"}, {"token": "abé"}, "token"),
        ("header", {"X-Api-Key": "token"}, {"token": " ab"}, "token"),
        ("header", {"X-Api-Key": "token"}, {"token": "ab "}, "token"),
        ("header", {"X-Api-Key": "token"}, {"token": "a\tb"}, "token"),
        ("header", {"X-Api-Key": "token"}, {"token": "a\x7fb"}, "token"),
        ("basic_auth", {}, {"username": "!user~", "password": "p@ss:w0rd"}, None),
        ("basic_auth", {}, {"username": "us:er", "password": "x"}, "username"),
        ("basic_auth", {}, {"username": "usé", "password": "x"}, "username"),
        ("basic_auth", {}, {"username": "user", "password": "pa ss"}, "password"),
        ("unsupported", {}, {}, ValueError),
    ],
)
def test_values_each_method_can_carry(method, auth_params, values, unsendable):
    fields = {name: _Secret(value) for name, value in values.items()}
    if unsendable is ValueError:
        with pytest.raises(ValueError, match="sends no credentials"):
            _unsendable(method, auth_params, fields)
        return
    assert _unsendable(method, auth_params, fields) == unsendable
    if unsendable is None:
        # What the rules accept, httpx encodes and h11 puts on the wire.
        request = _request(method, auth_params, values)
        h11.Request(
            method="GET", target=request.url.raw_path, headers=request.headers.raw
        )
        if method == "query_param":
            sent = urllib.parse.parse_qs(request.url.query.decode("ascii"))
            assert sent == {
                name: [values[field]] for name, field in auth_params.items()
            }


PROXY = "http://proxy.test:3128"
API = "https://api.example.com/feed"
VIA = {"HTTPS_PROXY": PROXY}


@pytest.mark.parametrize(
    ("env", "url", "expected"),
    [
        ({}, API, None),
        ({"ALL_PROXY": PROXY}, API, PROXY),
        ({"https_proxy": PROXY, "HTTPS_PROXY": "x:1", "ALL_PROXY": "y:2"}, API, PROXY),
        ({"https_proxy": "", "HTTPS_PROXY": "x:1", "all_proxy": PROXY}, API, PROXY),
        ({"HTTPS_PROXY": "proxy.test:3128"}, API, PROXY),
        ({"NO_PROXY": "*.example.com"}, API, None),
        ({**VIA, "NO_PROXY": "other.org, *"}, API, None),
        ({**VIA, "NO_PROXY": "Example.COM"}, API, None),
        ({**VIA, "NO_PROXY": "example.com"}, "https://notexample.com/feed", PROXY),
        ({**VIA, "NO_PROXY": ".example.com"}, API, None),
        ({**VIA, "NO_PROXY": ".example.com"}, "https://example.com/feed", PROXY),
        ({**VIA, "NO_PROXY": "api.example.com:443"}, API, None),
        ({**VIA, "NO_PROXY": "api.example.com:8443"}, API, PROXY),
        ({**VIA, "no_proxy": "other.org", "NO_PROXY": "example.com"}, API, PROXY),
        ({**VIA, "NO_PROXY": "10.0.0.0/8,::1"}, "https://10.1.2.3:8443/feed", None),
        ({**VIA, "NO_PROXY": "10.0.0.0/8,::1"}, "https://[::1]/feed", None),
        ({**VIA, "NO_PROXY": "10.0.0.0/8,localhost"}, API, PROXY),
        ({**VIA, "NO_PROXY": "https://api.example.com"}, API, DownloadError),
        ({**VIA, "NO_PROXY": "*.example.com"}, API, DownloadError),
        ({**VIA, "NO_PROXY": "*.example.com,example.com"}, API, None),
    ],
)
def test_proxy_of_a_credentialed_request(monkeypatch, env, url, expected):
    for name in list(os.environ):
        if name.lower().endswith("_proxy"):
            monkeypatch.delenv(name)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    if expected is DownloadError:
        message = (
            f"NO_PROXY entry {env['NO_PROXY']} is not supported for protected feeds"
        )
        with pytest.raises(DownloadError, match=f"^{re.escape(message)}$"):
            _proxy(url)
    else:
        assert _proxy(url) == expected


def test_borrowed_transport_stays_open():
    class Stub(httpx.MockTransport):
        closed = False

        def close(self):
            self.closed = True

    stub = Stub(lambda request: httpx.Response(200, text=request.url.host))
    mounts = {"https://a.org": _Borrowed(stub)}
    with httpx.Client(transport=_Borrowed(stub), mounts=mounts) as client:
        hosts = [client.get(f"https://{host}/").text for host in ("a.org", "b.org")]
    assert hosts == ["a.org", "b.org"]
    assert not stub.closed
    with httpx.Client(transport=stub) as client:
        assert client.get("https://a.org/").text == "a.org"
    assert stub.closed
