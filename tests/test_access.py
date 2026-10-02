import base64
import logging
import os
import re
import urllib.parse

import h11
import httpx
import pytest

from transitio import _http
from transitio.catalog._access import (
    _ARMED,
    _CARRIES,
    _INVALID,
    _OFF_HTTPS,
    _REFUSED,
    _Access,
    _AccessTransport,
    _Borrowed,
    _origin,
    _proxy,
    _redact,
    _Secret,
    _secrets,
    _unsendable,
    _without,
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


BODY = b"feed bytes"
PASSWORD = "p@ss/w%rd=&"
PAYLOAD = base64.b64encode(f"user:{PASSWORD}".encode()).decode()
ENCODED = urllib.parse.quote(LONG, safe="")
# Raw and percent-encoded characters mixed.
MIXED = SHORT + urllib.parse.quote(LONG.removeprefix(SHORT), safe="")
CDN = "https://cdn.example.org"
# Each method's auth_params and credential values.
CREDENTIALS = {
    "query_param": ({"key": "key"}, {"key": LONG}),
    "header": ({"X-Api-Key": "token"}, {"token": LONG}),
    "basic_auth": ({}, {"username": "user", "password": PASSWORD}),
}


def _access(method):
    params, values = CREDENTIALS[method]
    fields = {field: _Secret(value) for field, value in values.items()}
    return _Access(API, method, params, fields)


def _to(location, status=302):
    return status, {"Location": location}


class _Stub(httpx.MockTransport):
    """Answers each URL of ``routes`` with its ``(status, headers)`` or by
    raising its httpx error class with the request's URL, any other with 200
    and BODY, and the access origin with the reason phrase LONG; records the
    requests and whether it was closed."""

    closed = False

    def __init__(self, routes):
        self.seen = []
        routes = {_where(httpx.URL(url)): answer for url, answer in routes.items()}

        def handler(request):
            self.seen.append(request)
            answer = routes.get(_where(request.url), (200, {}))
            if isinstance(answer, type):
                raise answer(str(request.url))
            access = _origin(request.url) == _origin(API)
            phrase = {"reason_phrase": LONG.encode()} if access else {}
            status, headers = answer
            return httpx.Response(
                status, headers=headers, content=BODY, extensions=phrase
            )

        super().__init__(handler)

    def close(self):
        self.closed = True


def _where(url):
    return *_origin(url), url.path


def _download(tmp_path, access, stub):
    headers = {"Cookie": "c=1", "Authorization": "Bearer default"}
    with _http.client(headers=headers) as client:
        return _http.download(
            client, API, tmp_path / "feed.zip", access=access, transport=stub
        )


def _holds(text, access):
    return any(secret.occurs_in(text) for secret in access.secrets)


def _carries(request, access):
    """Whether ``request`` carries the credentials as their method sends
    them; asserts that no secret is anywhere else in its URL or headers."""
    params, values = CREDENTIALS[access.method]
    headers = {name.lower(): value for name, value in request.headers.items()}
    query = request.url.query.decode("ascii")
    if access.method == "query_param":
        sent = urllib.parse.parse_qs(query)
        expected = {name: [values[field]] for name, field in params.items()}
        carried = {name: sent.get(name) for name in params} == expected
        query = _without(query, params)
    elif access.method == "header":
        carried = headers.pop("x-api-key", None) == LONG
    else:
        carried = headers.pop("authorization", None) == f"Basic {PAYLOAD}"
    rest = request.url.copy_with(query=query.encode() or None)
    assert not _holds(f"{rest} {headers}", access)
    return carried


@pytest.mark.parametrize("method", list(CREDENTIALS))
def test_credentials_reach_the_access_origin_alone(
    tmp_path, monkeypatch, caplog, method
):
    access = _access(method)
    wrapped = []
    handle = _AccessTransport.handle_request

    def spy(self, request):
        wrapped.append(str(request.url))
        return handle(self, request)

    monkeypatch.setattr(_AccessTransport, "handle_request", spy)
    caplog.set_level(logging.DEBUG, logger="httpx")
    cookie = "s=1; Domain=example.com; Path=/"
    hops = [
        API,
        "https://api.example.com/feed/v2",
        "https://api.example.com:8443/p",
        "https://u:p@files.example.com/f",
        "https://api.example.com/back",
    ]
    routes = {
        hop: (302, {"Location": target, "Set-Cookie": cookie})
        for hop, target in zip(hops, hops[1:])
    }
    stub = _Stub(routes)
    _download(tmp_path, access, stub)
    assert (tmp_path / "feed.zip").read_bytes() == BODY
    assert [str(r.url.copy_with(query=None)) for r in stub.seen] == hops
    assert [_carries(r, access) for r in stub.seen] == [True, True, False, False, False]
    assert not any("Cookie" in request.headers for request in stub.seen)
    basic = f"Basic {PAYLOAD}" if method == "basic_auth" else None
    authorization = [request.headers.get("Authorization") for request in stub.seen]
    assert authorization == [basic, basic, None, None, None]
    assert wrapped == [hops[0], hops[1], hops[4]]
    assert not stub.closed
    assert caplog.records
    assert not any(_holds(record.getMessage(), access) for record in caplog.records)


def _frames(error):
    """The locals of each transitio frame of ``error``'s traceback."""
    trace = error.__traceback__
    while trace is not None:
        frame = trace.tb_frame
        if frame.f_globals["__name__"].startswith("transitio."):
            yield from frame.f_locals.values()
        trace = trace.tb_next


KEYED = f"{API}?key={ENCODED}"


@pytest.mark.parametrize(
    ("method", "routes", "seen", "error"),
    [
        pytest.param(
            "query_param",
            {API: _to(f"/v2?key={ENCODED}&page=2")},
            [KEYED, f"https://api.example.com/v2?page=2&key={ENCODED}"],
            None,
            id="own-key-same-origin",
        ),
        pytest.param(
            "query_param",
            {API: _to(f"{CDN}/f?key={urllib.parse.quote_plus(LONG)}")},
            [KEYED, f"{CDN}/f"],
            None,
            id="own-key-other-origin",
        ),
        pytest.param(
            "query_param",
            {API: _to("https://api.example.com:0/f")},
            [KEYED, "https://api.example.com:0/f"],
            None,
            id="port-0",
        ),
        pytest.param(
            "basic_auth",
            {API: _to("/user/feed")},
            [API, "https://api.example.com/user/feed"],
            None,
            id="username-in-path",
        ),
        pytest.param(
            "query_param",
            {API: _to(f"/v2?other={urllib.parse.quote_plus(LONG)}")},
            [KEYED],
            _CARRIES,
            id="other-name-plus-form",
        ),
        pytest.param(
            "query_param",
            {API: _to(f"{CDN}/{ENCODED}")},
            [KEYED],
            _CARRIES,
            id="path",
        ),
        pytest.param(
            "header", {API: _to(f"{CDN}/f#{ENCODED}")}, [API], _CARRIES, id="fragment"
        ),
        pytest.param(
            "header",
            {API: _to(f"https://u:{ENCODED}@cdn.example.org/")},
            [API],
            _CARRIES,
            id="userinfo",
        ),
        pytest.param(
            "basic_auth", {API: _to(f"/{PAYLOAD}")}, [API], _CARRIES, id="payload"
        ),
        pytest.param(
            "basic_auth",
            {API: _to(f"{CDN}/f?a={urllib.parse.quote_plus('Basic ' + PAYLOAD)}")},
            [API],
            _CARRIES,
            id="authorization-value",
        ),
        pytest.param(
            "header",
            {API: _to(f"{CDN}/a"), f"{CDN}/a": _to(f"/b?x={MIXED}")},
            [API, f"{CDN}/a"],
            _CARRIES,
            id="from-other-origin",
        ),
        pytest.param(
            "basic_auth",
            {API: _to("http://api.example.com/feed")},
            [API],
            "redirect to http",
            id="http",
        ),
        pytest.param(
            "basic_auth", {API: _to("ftp://x.org/f")}, [API], _OFF_HTTPS, id="ftp"
        ),
        pytest.param(
            "header", {API: _to("https://[::1")}, [API], _INVALID, id="invalid"
        ),
        pytest.param(
            "header",
            {API: _to("/v2", 300)},
            [API],
            "redirect status 300 is not followed",
            id="300",
        ),
        pytest.param(
            "header",
            {API: (302, {})},
            [API],
            "redirect without a location",
            id="no-location",
        ),
        pytest.param(
            "header",
            {API: _to("/v2", 304)},
            [API],
            f"{API}: HTTP 304 Not Modified",
            id="304-ends-walk",
        ),
        pytest.param(
            "header", {API: _to(API)}, [API] * 11, "too many redirects", id="loop"
        ),
        pytest.param(
            "query_param",
            {API: httpx.ConnectError},
            [KEYED],
            f"{API}: ConnectError",
            id="httpx-error",
        ),
    ],
)
def test_redirect_walk(tmp_path, method, routes, seen, error):
    access = _access(method)
    stub = _Stub(routes)
    if error is None:
        _download(tmp_path, access, stub)
        assert (tmp_path / "feed.zip").read_bytes() == BODY
    else:
        with pytest.raises(DownloadError) as caught:
            _download(tmp_path, access, stub)
        assert str(caught.value) == error
        assert caught.value.__cause__ is None and caught.value.__context__ is None
        assert not any(_holds(repr(value), access) for value in _frames(caught.value))
        assert not list(tmp_path.iterdir())
    assert [str(request.url) for request in stub.seen] == seen


@pytest.mark.parametrize(
    ("token", "location"), [("/", "/%2F"), ("/", "/g?token=/"), ("a#b", "/a#b")]
)
def test_delimiters_count_only_as_data(tmp_path, token, location):
    fields = {"token": _Secret(token)}
    access = _Access(API, "header", {"X-Api-Key": "token"}, fields)
    v2 = "https://api.example.com/v2"
    routes = {API: _to("/v2?a=b"), v2: _to(f"{CDN}/f"), f"{CDN}/f": _to(location)}
    stub = _Stub(routes)
    with pytest.raises(DownloadError, match=f"^{_CARRIES}$"):
        _download(tmp_path, access, stub)
    assert [str(request.url) for request in stub.seen] == [API, f"{v2}?a=b", f"{CDN}/f"]


def test_header_holding_a_secret_stays_on_the_access_origin():
    access = _access("header")
    stub = _Stub({API: _to(f"{CDN}/f")})
    with _http.client() as client, access.session(client, stub) as session:
        with pytest.raises(DownloadError, match=f"^{_CARRIES}$"):
            with session.stream("GET", API, headers={"If-Range": f'"{LONG}"'}):
                pass
    assert [str(request.url) for request in stub.seen] == [API]


def test_access_transport_checks_location():
    def handler(request):
        location = {"/a": f"/b?key={ENCODED}", "/c": f"/d?x={ENCODED}"}
        headers = {"Location": location[request.url.path], _REFUSED: LONG}
        return httpx.Response(302, headers=headers, extensions={"reason_phrase": b"x"})

    transport = _AccessTransport(httpx.MockTransport(handler), _access("query_param"))
    with httpx.Client(transport=transport) as client:
        kept, refused = (
            client.get(f"https://api.example.com/{path}", extensions={_ARMED: True})
            for path in "ac"
        )
    assert kept.headers["Location"] == str(kept.next_request.url)
    assert kept.headers["Location"] == "https://api.example.com/b"
    assert _REFUSED not in kept.headers
    assert refused.next_request is None and "Location" not in refused.headers
    assert refused.headers[_REFUSED] == _CARRIES
    assert kept.reason_phrase == refused.reason_phrase == "Found"


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"HTTPS_PROXY": PROXY}, PROXY),
        ({"HTTPS_PROXY": PROXY, "NO_PROXY": "example.com"}, None),
        (
            {"HTTPS_PROXY": PROXY, "NO_PROXY": f"*{LONG}"},
            "NO_PROXY entry **** is not supported for protected feeds",
        ),
    ],
)
def test_access_origin_takes_its_proxy(tmp_path, monkeypatch, env, expected):
    for name in list(os.environ):
        if name.lower().endswith("_proxy"):
            monkeypatch.delenv(name)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    proxies = []

    class Direct(httpx.MockTransport):
        def __init__(self, proxy=None):
            proxies.append(proxy)
            super().__init__(lambda request: httpx.Response(200, content=BODY))

    monkeypatch.setattr(httpx, "HTTPTransport", Direct)
    with _http.client() as client:
        path = tmp_path / "feed.zip"
        if expected and expected.startswith("NO_PROXY"):
            with pytest.raises(DownloadError) as caught:
                _http.download(client, API, path, access=_access("header"))
            assert str(caught.value) == expected
            assert caught.value.__cause__ is None and caught.value.__context__ is None
            assert proxies == [] and not list(tmp_path.iterdir())
        else:
            _http.download(client, API, path, access=_access("header"))
            assert proxies == [expected] and path.read_bytes() == BODY


@pytest.mark.parametrize(
    ("url", "method", "params", "values", "message"),
    [
        (
            "http://api.example.com/feed",
            "header",
            {"X-Api-Key": "token"},
            {"token": "t"},
            "an access URL must be https",
        ),
        (
            API,
            "header",
            {"X-Api-Key": "token", "Set-COOKIE": "token"},
            {"token": "t"},
            "a cookie header is not an access method",
        ),
        (
            API,
            "basic_auth",
            {},
            {"username": "us:er", "password": "p"},
            "credential username has characters its method cannot carry",
        ),
    ],
)
def test_access_refuses_what_it_cannot_send(url, method, params, values, message):
    fields = {field: _Secret(value) for field, value in values.items()}
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        _Access(url, method, params, fields)
