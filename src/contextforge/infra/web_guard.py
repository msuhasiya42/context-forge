"""Which URLs the web reader may fetch.

Two rules: a fetch goes only to public internet addresses (no SSRF into the worker's network),
and within a task only to URLs the task has a reason to read (no data carried out through a
query string an injected page asked for).
"""

import ipaddress
import re
import socket
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable
from urllib.parse import urldefrag, urlsplit

from contextforge.core.tools.base import ToolContext


class WebRefusal(Exception):
    """A request the reader will not make or finish. Retrying it fails the same way.

    ``code`` names the rule, so the model can tell a disallowed URL from an oversized page.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


# IPv6 prefixes that carry an IPv4 address in their last 32 bits: well-known NAT64 (RFC 6052)
# and the deprecated IPv4-compatible form. ``is_global`` judges the IPv6 wrapper, not the IPv4
# inside, so 64:ff9b::7f00:1 (127.0.0.1 behind a NAT64 gateway) counts as global.
_EMBEDS_IPV4 = (ipaddress.IPv6Network("64:ff9b::/96"), ipaddress.IPv6Network("::/96"))
# Local-use NAT64 (RFC 8215): where the IPv4 sits depends on the translator's prefix length,
# so it cannot be checked, and it is refused outright.
_LOCAL_NAT64 = ipaddress.IPv6Network("64:ff9b:1::/48")
_WEB_PORTS = frozenset({None, 80, 443})


def resolve_host(host: str) -> list[str]:
    """Every address ``host`` resolves to, in resolver order, without repeats."""
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def is_public_address(text: str) -> bool:
    """True when ``text`` is a global address and so is any IPv4 address embedded in it."""
    address = ipaddress.ip_address(text)
    if not isinstance(address, ipaddress.IPv6Address):
        return address.is_global
    if address in _LOCAL_NAT64:
        return False
    embedded = address.ipv4_mapped
    if embedded is None and any(address in network for network in _EMBEDS_IPV4):
        embedded = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return address.is_global and (embedded is None or embedded.is_global)


def public_url(url: str, resolver: Callable[[str], list[str]]) -> tuple[str, list[str]]:
    """``url``'s host name and its addresses, all public; raises ``WebRefusal`` otherwise.

    Only http(s) on the standard ports, with no credentials. Every resolved address must be
    public: one private answer among public ones is enough to refuse.
    """
    parts = urlsplit(url)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
    ):
        raise WebRefusal("url_refused", "Only public http(s) URLs without credentials are read.")
    try:
        port = parts.port
    except ValueError:  # a port that is not a number in range
        port = -1
    if port not in _WEB_PORTS:
        raise WebRefusal("url_refused", "Only the standard web ports 80 and 443 are read.")
    try:
        addresses = [str(ipaddress.ip_address(parts.hostname))]
    except ValueError:  # a name, not an address literal
        addresses = resolver(parts.hostname)
    if not addresses or not all(is_public_address(address) for address in addresses):
        raise WebRefusal("url_refused", "Only public internet addresses are read.")
    return parts.hostname, addresses


_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
# Punctuation that ends the sentence a URL was written in, not the URL.
_TRAILING = ".,;:!?)]}"


def urls_in(text: str) -> set[str]:
    """The http(s) URLs written in ``text``, in the form the allowlist compares."""
    return {_comparable(match.rstrip(_TRAILING)) for match in _URL_IN_TEXT.findall(text)}


def _comparable(url: str) -> str:
    """A URL trimmed and without its fragment, which no server ever sees."""
    return urldefrag(url.strip()).url


class SeenUrls:
    """Per task, the URLs ``fetch_url`` may read: those ``web_search`` returned in that task and
    those the task's own text (its goal and the user's request) names.

    Any other URL, such as one a fetched page asks for, could carry the user's data out in its
    query string. Held in memory for the newest ``max_tasks`` tasks: a task's searches and
    fetches run in one worker, and a re-run starts a new transcript that searches again.
    """

    def __init__(self, task_text: Callable[[ToolContext], str], *, max_tasks: int = 256) -> None:
        self._task_text = task_text
        self._max_tasks = max_tasks
        self._returned: OrderedDict[str, set[str]] = OrderedDict()
        self._lock = threading.Lock()

    def add(self, task_id: str, urls: Iterable[str]) -> None:
        """Record URLs a search returned in ``task_id``."""
        with self._lock:
            returned = self._returned.setdefault(task_id, set())
            self._returned.move_to_end(task_id)
            returned.update(_comparable(url) for url in urls)
            while len(self._returned) > self._max_tasks:
                self._returned.popitem(last=False)

    def allows(self, ctx: ToolContext, url: str) -> bool:
        wanted = _comparable(url)
        with self._lock:
            if wanted in self._returned.get(ctx.task_id, ()):
                return True
        return wanted in urls_in(self._task_text(ctx))
