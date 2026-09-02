"""Opting in to loopback targets.

Crawling your own development server is a normal thing to want, and the SSRF
guard refuses it - correctly, because the guard cannot tell a seed the user
typed from a link a crawled page offered.

This narrows the question rather than answering it loosely. Two properties
make the opt-in safe enough to exist:

**It is loopback only.** ``127.0.0.0/8`` and ``::1``, nothing else. The private
ranges, the cloud metadata address and everything else in ``DENIED_NETWORKS``
stay blocked, so the interesting SSRF targets are unaffected. A dev server on
``192.168.1.50`` is deliberately not covered - reaching *that* is the attack.

**It exists on the command line and not in the API.** A crawl submitted over
HTTP could have been submitted by a page the user happened to be visiting
(docs/11_SECURITY_MODEL.md section 1); a flag typed into a terminal could not.
The asymmetry is the point, so this must never become a config key or a
request field.
"""

from __future__ import annotations

import ipaddress

from crwallm.policy.ssrf import CachingResolver, IPNetwork, SsrfGuard, SystemResolver

__all__ = ["LOOPBACK_NETWORKS", "build_guard"]

LOOPBACK_NETWORKS: tuple[IPNetwork, ...] = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
)


def build_guard(*, allow_local: bool = False) -> SsrfGuard:
    """The guard a command-line crawl should use.

    ``allow_local`` widens it to loopback and nothing else.
    """
    return SsrfGuard(
        CachingResolver(SystemResolver()),
        allow_networks=LOOPBACK_NETWORKS if allow_local else (),
    )
