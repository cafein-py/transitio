"""Credential values of protected feeds: hidden from text, checked against
what each access method can carry, and masked in messages; the proxy a
credentialed request takes, a transport adapter that leaves its transport
open, and the transport and redirect walk that send credentials to the access
origin alone."""

from __future__ import annotations

import base64
import contextlib
import http.cookiejar
import ipaddress
import re
import urllib.parse
import urllib.request

import httpx

from transitio.exceptions import DownloadError

_MASK = "***"
_PORTS = {"http": 80, "https": 443}
# The request extension that asks for credentials, and the response header
# that marks a refused Location.
_ARMED = "transitio_access"
_REFUSED = "x-transitio-refused"
_CARRIES = "redirect carries a credential"
_INVALID = "redirect location is not a URL"
_OFF_HTTPS = "redirect to a scheme other than https"
_REDIRECTS = 10
_FOLLOWED = frozenset({301, 302, 303, 307, 308})
# A NO_PROXY name with an optional leading dot and port.
_NO_PROXY_NAME = re.compile(r"(\.?[a-z0-9_-]+(?:\.[a-z0-9_-]+)*)(?::([0-9]+))?")

# The values each access method can carry: a query value is any text UTF-8
# encodes, a header value printable ASCII with no space at either end, a
# Basic-auth password printable ASCII without spaces and a username the same
# without ":".
_QUERY_VALUE = re.compile(r"[^\ud800-\udfff]+")
_HEADER_VALUE = re.compile(r"[\x21-\x7e](?:[\x20-\x7e]*[\x21-\x7e])?")
_BASIC_PASSWORD = re.compile(r"[\x21-\x7e]+")
_BASIC_USERNAME = re.compile(r"[\x21-\x39\x3b-\x7e]+")


class _Secret:
    """A credential value whose ``repr`` and ``str`` are ``***``.

    It is found in text in any mix of raw and percent-encoded characters
    (UTF-8, either hex case), with ``+`` also standing for a space.
    """

    __slots__ = ("_value", "_pattern")

    def __init__(self, value):
        if not isinstance(value, str) or not value:
            raise ValueError("a credential value must be a non-empty string")
        self._value = value
        # A lookahead, so overlapping occurrences are all found.
        self._pattern = re.compile("(?=(" + "".join(map(_char_pattern, value)) + "))")

    def __repr__(self):
        return _MASK

    __str__ = __repr__

    @classmethod
    def basic(cls, username, password):
        """The Basic-auth payload of two secrets and its ``Authorization``
        value, as secrets."""
        # surrogatepass: no exception here can hold the joined text.
        payload = base64.b64encode(
            f"{username._value}:{password._value}".encode("utf-8", "surrogatepass")
        ).decode("ascii")
        return cls(payload), cls("Basic " + payload)

    def reveal(self):
        """The value itself."""
        return self._value

    def fits(self, rule):
        """Whether the compiled ``rule`` matches the whole value."""
        return rule.fullmatch(self._value) is not None

    def occurs_in(self, text):
        """Whether the value occurs in ``text``."""
        return self._pattern.search(text) is not None

    def spans(self, text):
        """The ``(start, end)`` of every occurrence of the value in ``text``,
        overlapping ones included."""
        return [match.span(1) for match in self._pattern.finditer(text)]


def _char_pattern(char):
    """A pattern matching one character: its percent-encoded UTF-8 bytes in
    either hex case, ``+`` for a space, or the character itself."""
    encoded = "".join(
        "%" + "".join(f"[{d}{d.lower()}]" if d.isalpha() else d for d in f"{b:02X}")
        for b in char.encode("utf-8", "surrogatepass")
    )
    plus = r"|\+" if char == " " else ""
    return f"(?:{encoded}{plus}|{re.escape(char)})"


def _unsendable(method, auth_params, fields):
    """The first credential field whose value ``method`` cannot carry, or
    None when all fit.

    ``auth_params`` maps each query parameter or header name to the field it
    carries (empty for ``basic_auth``, which sends ``username`` and
    ``password``); ``fields`` maps each field to its :class:`_Secret`.
    """
    if method == "basic_auth":
        rules = [("username", _BASIC_USERNAME), ("password", _BASIC_PASSWORD)]
    elif method in ("query_param", "header"):
        rule = _QUERY_VALUE if method == "query_param" else _HEADER_VALUE
        rules = [(field, rule) for field in auth_params.values()]
    else:
        raise ValueError(f"access method {method!r} sends no credentials")
    for field, rule in rules:
        if not fields[field].fits(rule):
            return field
    return None


def _secrets(method, fields):
    """The secrets a credential set puts on the wire: every value but a
    Basic-auth username, and for ``basic_auth`` the payload and the
    ``Authorization`` value."""
    if method != "basic_auth":
        return tuple(fields.values())
    kept = tuple(secret for name, secret in fields.items() if name != "username")
    return kept + _Secret.basic(fields["username"], fields["password"])


def _redact(text, secrets):
    """``text`` with every occurrence of each of ``secrets`` masked as
    ``***``; overlapping and adjacent occurrences share one mask, so no
    fragment of a longer form outlives a shorter one."""
    merged = []
    for start, end in sorted(span for secret in secrets for span in secret.spans(text)):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    parts, kept = [], 0
    for start, end in merged:
        parts += [text[kept:start], _MASK]
        kept = end
    parts.append(text[kept:])
    return "".join(parts)


def _origin(url):
    """The ``(scheme, host, port)`` of ``url``, the host lower-cased and the
    port the effective one."""
    url = httpx.URL(url)
    port = _PORTS.get(url.scheme) if url.port is None else url.port
    return url.scheme, url.host.lower(), port


def _proxy(url):
    """The proxy URL a credentialed request to ``url`` goes through, or None
    for a direct connection.

    The proxy is the environment's one for the scheme of ``url``, else its
    ``all`` one, read as urllib reads them (a lower-case name wins over the
    upper-case one). NO_PROXY exempts the host when it is ``*`` or holds an
    entry matching it: an IP address or range holding it, a name equal to it
    or to a domain above it, a name with a leading dot equal to a domain above
    it, each with an optional port equal to the effective port. With a proxy
    set, an entry of any other shape (a scheme, a wildcard in a name) raises
    :class:`~transitio.exceptions.DownloadError` unless another entry exempts
    the host.
    """
    scheme, host, port = _origin(url)
    proxies = urllib.request.getproxies_environment()
    proxy = proxies.get(scheme) or proxies.get("all")
    if not proxy:
        return None
    unread = None
    for entry in proxies.get("no", "").split(","):
        entry = entry.strip()
        exempt = _exempts(entry.lower(), host, port) if entry else False
        if exempt:
            return None
        if exempt is None and unread is None:
            unread = entry
    if unread is not None:
        raise DownloadError(
            f"NO_PROXY entry {unread} is not supported for protected feeds"
        )
    return proxy if "://" in proxy else "http://" + proxy


def _exempts(entry, host, port):
    """Whether the lower-case NO_PROXY ``entry`` exempts ``host`` at
    ``port``; None for an entry of a shape :func:`_proxy` does not read."""
    if entry == "*":
        return True
    try:
        network = ipaddress.ip_network(entry, strict=False)
    except ValueError:
        pass
    else:
        try:
            return ipaddress.ip_address(host) in network
        except ValueError:
            return False
    match = _NO_PROXY_NAME.fullmatch(entry)
    if match is None:
        return None
    name, entry_port = match.groups()
    if entry_port is not None and int(entry_port) != port:
        return False
    if name.startswith("."):
        return host.endswith(name)
    return host == name or host.endswith("." + name)


class _Borrowed(httpx.BaseTransport):
    """A transport that hands each request to ``inner`` and leaves ``inner``
    open when it is closed, for a client that does not own ``inner``."""

    def __init__(self, inner):
        self._inner = inner

    def handle_request(self, request):
        return self._inner.handle_request(request)

    def close(self):
        pass


class _Access:
    """How a protected feed's credentials are sent: ``method`` with
    ``auth_params`` (as :func:`_unsendable` reads them) and ``fields``, each
    credential field's :class:`_Secret`, to the origin of the https ``url``
    alone.

    Raises ValueError for a ``url`` that is not https, a ``Cookie`` or
    ``Set-Cookie`` header and a value the method cannot carry, naming the
    field, never the value.
    """

    __slots__ = ("url", "origin", "method", "params", "fields", "secrets")

    def __init__(self, url, method, auth_params, fields):
        self.origin = _origin(url)
        if self.origin[0] != "https":
            raise ValueError("an access URL must be https")
        if method == "header" and {name.lower() for name in auth_params} & {
            "cookie",
            "set-cookie",
        }:
            raise ValueError("a cookie header is not an access method")
        field = _unsendable(method, auth_params, fields)
        if field is not None:
            raise ValueError(
                f"credential {field} has characters its method cannot carry"
            )
        self.url = str(url)
        self.method = method
        self.params = dict(auth_params)
        self.fields = dict(fields)
        self.secrets = _secrets(method, self.fields)

    def session(self, client, transport=None):
        """A :class:`_Session` for these credentials."""
        return _Session(client, self, transport)

    def holds(self, text):
        """Whether a secret occurs in ``text`` (:meth:`_Secret.occurs_in`)."""
        return any(secret.occurs_in(text) for secret in self.secrets)

    def redact(self, text):
        """``text`` with every secret masked (:func:`_redact`)."""
        return _redact(text, self.secrets)


class _AccessTransport(httpx.BaseTransport):
    """A transport that hands ``inner`` a copy of each request, without a
    ``Cookie`` header and, when the request is armed and for the access
    origin, with the credentials of ``access`` added; a response comes back
    with the standard reason phrase for its status and a checked Location
    (:func:`_follow`): absolute and stripped of the credential's query
    parameters, or, refused, replaced by the ``x-transitio-refused`` header."""

    def __init__(self, inner, access):
        self._inner = inner
        self._access = access

    def handle_request(self, request):
        response = self._inner.handle_request(self._credentialed(request))
        phrase = httpx.codes.get_reason_phrase(response.status_code)
        response.extensions["reason_phrase"] = phrase.encode("ascii")
        response.headers.pop(_REFUSED, None)
        if "Location" in response.headers:
            url, refusal = _follow(
                response.headers["Location"], request.url, self._access
            )
            del response.headers["Location"]
            if refusal is None:
                response.headers["Location"] = str(url)
            else:
                response.headers[_REFUSED] = refusal
        return response

    def _credentialed(self, request):
        access, url = self._access, request.url
        headers = httpx.Headers(request.headers)
        headers.pop("Cookie", None)
        extensions = dict(request.extensions)
        armed = extensions.pop(_ARMED, False) is True and _origin(url) == access.origin
        if armed and access.method == "query_param":
            pairs = [
                (name, access.fields[f].reveal()) for name, f in access.params.items()
            ]
            sent = urllib.parse.urlencode(pairs, quote_via=urllib.parse.quote, safe="")
            kept = _without(url.query.decode("ascii"), access.params)
            query = "&".join(filter(None, (kept, sent)))
            url = url.copy_with(query=query.encode("ascii"))
        elif armed and access.method == "header":
            for name, field in access.params.items():
                headers[name] = access.fields[field].reveal()
        elif armed:
            fields = access.fields
            headers["Authorization"] = _Secret.basic(
                fields["username"], fields["password"]
            )[1].reveal()
        return httpx.Request(
            request.method,
            url,
            headers=headers,
            stream=request.stream,
            extensions=extensions,
        )

    def close(self):
        self._inner.close()


def _without(query, names):
    """The raw ``query`` without the pairs whose decoded name is in
    ``names``."""
    pairs = query.split("&")
    kept = [
        p for p in pairs if urllib.parse.unquote_plus(p.partition("=")[0]) not in names
    ]
    return "&".join(kept)


def _follow(location, base, access):
    """``(url, refusal)`` of a redirect from ``base`` to ``location``: the
    absolute URL without the query parameters a ``query_param`` credential is
    sent in, and None; or None and why it is refused, a location that is no
    URL or one whose userinfo, path, query or fragment holds a secret of
    ``access`` in any encoding (:meth:`_Secret.occurs_in`)."""
    try:
        url = base.join(location)
        if access.method == "query_param":
            query = _without(url.query.decode("ascii"), access.params)
            url = url.copy_with(query=query.encode("ascii") or None)
        parts = (url.userinfo.decode("ascii"), url.raw_path.decode("ascii"))
        holds = any(access.holds(part) for part in (*parts, url.fragment))
    except Exception:  # noqa: B902 — no error may carry the location away
        return None, _INVALID
    if not url.host:
        return None, _INVALID
    return (None, _CARRIES) if holds else (url, None)


def _refusing_jar():
    """A cookie jar that keeps no cookie."""
    return http.cookiejar.CookieJar(
        http.cookiejar.DefaultCookiePolicy(allowed_domains=())
    )


class _Session:
    """The clients of one protected download, which keep no cookies and walk
    redirects themselves (:meth:`stream`). A request to the access origin goes
    through :class:`_AccessTransport` over ``transport``, else over a
    transport of its own through the :func:`_proxy` proxy; any other origin's
    request goes through ``transport``, else httpx's own routing. A
    ``transport`` is left open. ``client`` gives the headers and timeout."""

    def __init__(self, client, access, transport=None):
        if transport is None:
            inner, other = httpx.HTTPTransport(proxy=_proxy(access.url)), None
        else:
            inner = other = _Borrowed(transport)
        user_agent = {"User-Agent": client.headers["User-Agent"]}
        options = {"headers": user_agent, "timeout": client.timeout}
        self._access = access
        self._own = httpx.Client(
            transport=_AccessTransport(inner, access),
            cookies=_refusing_jar(),
            **options,
        )
        self._other = httpx.Client(transport=other, cookies=_refusing_jar(), **options)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def close(self):
        self._own.close()
        self._other.close()

    @contextlib.contextmanager
    def stream(self, method, url, headers=None):
        """The streamed response to ``method`` on ``url`` after its redirects.

        Each hop is armed while every earlier hop stayed on the access origin.
        A 304 or a status other than 3xx ends the walk; a 301, 302, 303, 307
        or 308 is followed to its checked Location (:func:`_follow`) when that
        is https. Raises :class:`~transitio.exceptions.DownloadError` for any
        other 3xx, a redirect without a Location, a refused or non-https one,
        a header holding a secret for another origin and more than ten
        redirects; the message holds no URL.
        """
        response = self._walk(method, httpx.URL(url), headers)
        try:
            yield response
        finally:
            response.close()

    def _walk(self, method, url, headers):
        armed = True
        for _ in range(_REDIRECTS + 1):
            own = _origin(url) == self._access.origin
            armed = armed and own
            client = self._own if own else self._other
            extensions = {_ARMED: True} if armed else None
            request = client.build_request(
                method, url, headers=headers, extensions=extensions
            )
            request.headers.pop("Cookie", None)
            if not own:
                request.headers.pop("Authorization", None)
                request.headers.pop("Proxy-Authorization", None)
                # No header left, a resume's If-Range included, may hold a secret.
                if any(map(self._access.holds, request.headers.values())):
                    raise DownloadError(_CARRIES)
            # A no-op auth, so httpx makes no Authorization from the userinfo.
            response = client.send(request, stream=True, auth=httpx.Auth())
            status = response.status_code
            if status == 304 or not response.is_redirect:
                return response
            response.close()
            refusal = response.headers.get(_REFUSED) if own else None
            if refusal is None and status not in _FOLLOWED:
                refusal = f"redirect status {status} is not followed"
            if refusal is None and "Location" not in response.headers:
                refusal = "redirect without a location"
            if refusal is None:
                url, refusal = _follow(response.headers["Location"], url, self._access)
            if refusal is None and url.scheme != "https":
                refusal = "redirect to http" if url.scheme == "http" else _OFF_HTTPS
            if refusal is not None:
                raise DownloadError(refusal)
        raise DownloadError("too many redirects")
