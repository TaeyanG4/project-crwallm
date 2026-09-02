"""Recognising pages that are the same, or that are not really pages.

URL deduplication catches the same address twice. It does nothing about the
same *content* under different addresses, and on a real site that is most of
the waste: print versions, mirrored paths, session-flavoured URLs that escaped
normalisation, and paginated views that ran off the end
(docs/05_SPIDER_ARCHITECTURE.md).

**simhash, not a hash.** An exact digest catches byte-identical pages, which
is rare - two views of one article differ by a timestamp or a rotating banner.
simhash gives a similarity measure, so "the same page with a different ad" is
recognisable as a duplicate while a genuinely different article is not.

**soft 404s are a status-code lie.** A page that returns 200 and says "not
found" is a page the crawler will happily collect, extract nothing from, and
follow links out of. Detection is by shape rather than by wording, because the
wording is in whatever language the site is written in.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

__all__ = [
    "ContentDeduper",
    "DuplicateVerdict",
    "SoftNotFoundDetector",
    "hamming",
    "simhash",
]

_TOKEN = re.compile(r"[0-9a-zA-Z가-힣一-鿿]+")
_HASH_BITS = 64
_SHINGLE = 3


def _tokens(text: str) -> list[str]:
    """Word-ish units, with CJK handled.

    Korean and Chinese do not delimit words with spaces, so a whitespace split
    would turn a whole sentence into one token and make every page look
    unique. The character-class match keeps CJK runs together while still
    splitting on punctuation.
    """
    return [t.lower() for t in _TOKEN.findall(text)]


def _shingles(tokens: list[str], size: int = _SHINGLE) -> list[str]:
    """Overlapping n-grams.

    Individual words say what a page is about; sequences say how it is
    written. Two different articles on one topic share vocabulary and almost
    no phrasing, which is exactly the distinction needed here.
    """
    if len(tokens) < size:
        return [" ".join(tokens)] if tokens else []
    return [" ".join(tokens[i : i + size]) for i in range(len(tokens) - size + 1)]


def simhash(text: str, *, bits: int = _HASH_BITS) -> int:
    """Locality-sensitive fingerprint of ``text``.

    Similar documents produce fingerprints that differ in few bits, so
    "how alike" becomes a bit count rather than a comparison of every pair of
    documents.
    """
    shingles = _shingles(_tokens(text))
    if not shingles:
        return 0

    vector = [0] * bits
    for shingle in shingles:
        digest = int.from_bytes(hashlib.blake2b(shingle.encode(), digest_size=8).digest(), "big")
        for bit in range(bits):
            vector[bit] += 1 if digest >> bit & 1 else -1

    result = 0
    for bit in range(bits):
        if vector[bit] > 0:
            result |= 1 << bit
    return result


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


@dataclass(frozen=True, slots=True)
class DuplicateVerdict:
    is_duplicate: bool
    of_url: str | None = None
    distance: int | None = None
    via: str = "content"


@dataclass(slots=True)
class ContentDeduper:
    """Remembers what has been seen, by content.

    ``threshold`` is in bits of a 64-bit fingerprint. Three is close to
    identical; eight tolerates a rotating sidebar; beyond about twelve,
    unrelated pages on one template start colliding.
    """

    threshold: int = 6
    min_tokens: int = 30
    _seen: dict[int, str] = field(default_factory=dict)
    _exact: dict[str, str] = field(default_factory=dict)

    def check(self, url: str, text: str | None) -> DuplicateVerdict:
        if not text:
            return DuplicateVerdict(False)

        tokens = _tokens(text)
        if len(tokens) < self.min_tokens:
            # Short pages collide constantly - an empty search result and an
            # error page are genuinely similar - and calling them duplicates
            # would suppress real content.
            return DuplicateVerdict(False)

        exact = hashlib.blake2b(text.encode(), digest_size=16).hexdigest()
        previous = self._exact.get(exact)
        if previous is not None:
            return DuplicateVerdict(True, of_url=previous, distance=0, via="exact")
        self._exact[exact] = url

        fingerprint = simhash(text)
        for known, known_url in self._seen.items():
            distance = hamming(fingerprint, known)
            if distance <= self.threshold:
                return DuplicateVerdict(True, of_url=known_url, distance=distance)

        self._seen[fingerprint] = url
        return DuplicateVerdict(False)

    @property
    def known(self) -> int:
        return len(self._seen)


_NOT_FOUND_PHRASES = (
    "not found",
    "page not found",
    "no longer exists",
    "does not exist",
    "doesn't exist",
    "sorry, we can",
    "찾을 수 없",
    "존재하지 않",
    "페이지를 찾을",
    "삭제된 게시물",
    "ページが見つかり",
    "页面不存在",
    "未找到",
)


@dataclass(slots=True)
class SoftNotFoundDetector:
    """Pages that answer 200 and mean 404.

    Two independent signals, because either alone is wrong often enough to
    matter:

    *Phrasing* is precise when it hits and useless in a language the list does
    not cover.

    *Shape* - almost no text, and text that matches other 200-but-empty pages
    on this site - is language-independent and catches the case where a
    template renders an empty state without saying anything.

    A hit does not discard the page. It burns that URL *pattern*'s budget,
    because one soft 404 means the whole shape is generative
    (docs/05_SPIDER_ARCHITECTURE.md). That consequence is why the shape signal
    is tuned conservatively: being wrong here does not lose one page, it loses
    every page of that shape.
    """

    min_real_tokens: int = 40

    min_shape_tokens: int = 6
    """Below this there is not enough text to fingerprint.

    Found by running the spider rather than by reasoning about it: a page
    whose entire content was "b" got flagged because it resembled another
    one-word page. Two nearly empty documents always look alike, and treating
    that as evidence turns every terse page on a site into a soft 404 - and
    takes its whole URL pattern with it. Below this threshold only the
    phrasing signal applies.
    """

    min_corroboration: int = 3
    """Similar thin pages needed before the shape counts as evidence.

    Two is a coincidence on any site with a couple of stub pages.
    """

    _empty_fingerprints: dict[int, int] = field(default_factory=dict)

    def check(self, text: str | None, *, records_found: int = 0) -> bool:
        if records_found > 0:
            # Whatever it says, a page that produced data is a real page.
            return False
        if not text or not text.strip():
            return True

        tokens = _tokens(text)
        lowered = text.lower()

        if (
            any(phrase in lowered for phrase in _NOT_FOUND_PHRASES)
            and len(tokens) < self.min_real_tokens * 4
        ):
            return True

        if not (self.min_shape_tokens <= len(tokens) < self.min_real_tokens):
            return False

        # Thin, with enough text to compare, and matching several other thin
        # pages here: a template rendering its empty state.
        fingerprint = simhash(text)
        for known, count in self._empty_fingerprints.items():
            if hamming(fingerprint, known) <= 6:
                self._empty_fingerprints[known] = count + 1
                return count + 1 >= self.min_corroboration
        self._empty_fingerprints[fingerprint] = 1
        return False
