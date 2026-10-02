"""Credential values of protected feeds: hidden from text, checked against
what each access method can carry, and masked in messages."""

from __future__ import annotations

import base64
import re

_MASK = "***"

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
