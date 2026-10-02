import urllib.parse

import h11
import httpx
import pytest

from transitio.catalog._access import _redact, _Secret, _secrets, _unsendable

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
