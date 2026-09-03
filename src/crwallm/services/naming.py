"""Names for the columns, so nobody has to type five of them.

The picker used to open with every box empty and a rule that an empty box
means "do not collect". That makes naming *mandatory* - you cannot press the
button until you have typed at least once - and on a page with five columns it
is five decisions before anything happens. The whole point of the window is
that the first useful result costs one click.

So every box arrives filled and the button works immediately. Clearing a box
is still how you drop a column; it is just no longer the price of entry.

**No model.** The names come from the page's own markup. The structure
detector has already thrown away layout and utility classes
(``col-md-3``, ``mt-2``, ``css-1x9f``), so what survives in a selector is
usually what the author called the thing: ``small.author``, ``span.price``,
``h3.name``. That is a better source than a guess, it costs nothing, and it
works in a build with no Ollama - which is every packaged build.

A name that stays English is fine and deliberate. ``span.sku`` becoming "sku"
tells the person which column it is; inventing "품목번호" for it would be a
guess dressed up as a translation.
"""

from __future__ import annotations

import re

__all__ = ["name_columns", "name_for"]

WORDS: dict[str, str] = {
    # The vocabulary that actually repeats across listing pages. Kept short on
    # purpose: every entry is a claim that this word always means this thing,
    # and a wrong claim is worse than an untranslated class name.
    "title": "제목",
    "name": "이름",
    "heading": "제목",
    "subject": "제목",
    "author": "작성자",
    "writer": "작성자",
    "artist": "작성자",
    "seller": "판매자",
    "brand": "브랜드",
    "price": "가격",
    "cost": "가격",
    "amount": "금액",
    "discount": "할인",
    "date": "날짜",
    "time": "시간",
    "datetime": "날짜",
    "published": "날짜",
    "created": "날짜",
    "updated": "날짜",
    "desc": "설명",
    "description": "설명",
    "summary": "요약",
    "excerpt": "요약",
    "content": "내용",
    "body": "내용",
    "text": "내용",
    "quote": "인용",
    "tag": "태그",
    "tags": "태그",
    "category": "분류",
    "categories": "분류",
    "genre": "장르",
    "rating": "평점",
    "score": "점수",
    "star": "별점",
    "stars": "별점",
    "review": "후기",
    "reviews": "후기",
    "comment": "댓글",
    "comments": "댓글",
    "count": "개수",
    "views": "조회수",
    "view": "조회수",
    "likes": "좋아요",
    "status": "상태",
    "stock": "재고",
    "address": "주소",
    "location": "위치",
    "phone": "전화",
    "tel": "전화",
    "email": "이메일",
    "duration": "길이",
    "size": "크기",
    "color": "색상",
    "image": "이미지",
    "img": "이미지",
    "photo": "사진",
    "thumb": "썸네일",
    "thumbnail": "썸네일",
    "link": "링크",
    "url": "링크",
    "href": "링크",
}

TAGS: dict[str, str] = {
    "h1": "제목",
    "h2": "제목",
    "h3": "제목",
    "h4": "제목",
    "h5": "제목",
    "h6": "제목",
    "img": "이미지",
    "time": "날짜",
    "a": "링크",
    "p": "내용",
    "li": "항목",
    "td": "값",
    "th": "항목",
    "button": "버튼",
    "label": "이름표",
}

KINDS: dict[str, str] = {"href": "링크", "src": "이미지"}
"""What the column *is*, when its markup says nothing useful."""

FALLBACK = "항목"

_SPLIT = re.compile(r"[-_.\s]+")


def _leaf(selector: str) -> str:
    """The last step of a descendant selector.

    ``span > small.author`` describes where the value sits; only the end of it
    describes what the value is.
    """
    return selector.split(">")[-1].strip()


def _words(text: str) -> list[str]:
    """Split a class or attribute name into lowercase words.

    Handles the three spellings the same identifier arrives in:
    ``product-title``, ``product_title`` and ``productTitle``.
    """
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    return [w for w in _SPLIT.split(spaced.lower()) if w]


def name_for(selector: str, kind: str) -> str:
    """One column's name, from its markup.

    Order matters. The class the author wrote is the strongest signal, the tag
    is next, and the kind of value is the last resort - a link inside a heading
    is a title, not a "링크", and reading the kind first would lose that.
    """
    leaf = _leaf(selector)
    tag, _, classes = leaf.partition(".")

    # Classes right to left - `a.btn.add-to-cart` names itself at the end - but
    # *within* a class, left to right. English compounds put the head noun
    # last, so "price_color" ought to be a colour; on the page it is the price,
    # printed in a colour. Measured on books.toscrape.com, where reading the
    # last word labelled the price column 색상.
    for chunk in reversed(classes.split(".")) if classes else []:
        for word in _words(chunk):
            if word in WORDS:
                return WORDS[word]
        readable = " ".join(_words(chunk))
        if readable:
            return readable

    if kind.startswith("attr:"):
        for word in reversed(_words(kind.partition(":")[2])):
            if word in WORDS:
                return WORDS[word]

    if tag in TAGS:
        return TAGS[tag]
    return KINDS.get(kind, FALLBACK)


def name_columns(columns: list[dict[str, object]]) -> list[str]:
    """Names for a whole picker, all different.

    Uniqueness is not cosmetic. A record is a dict, so two columns called
    "링크" is one column - the second overwrites the first and half of what was
    asked for silently never appears.

    What separates them matters too. The same markup usually shows up twice,
    once as text and once as the link under it: ``a.hnuser`` on Hacker News is
    the poster's name and the poster's profile URL. Calling those "hnuser" and
    "hnuser2" is unique and tells the person nothing, so the kind of value
    breaks the tie first and a number is the fallback.
    """
    made = [
        (
            name_for(str(c.get("selector", "")), str(c.get("kind", "text"))),
            str(c.get("kind", "text")),
        )
        for c in columns
    ]
    repeated = {base for base, _ in made if sum(1 for b, _ in made if b == base) > 1}

    used: set[str] = set()
    names: list[str] = []
    for base, kind in made:
        candidate = base
        if base in repeated:
            suffix = KINDS.get(kind)
            # Not when the suffix *is* the name: "링크 링크" helps nobody.
            if suffix and suffix != base:
                candidate = f"{base} {suffix}"
        name, n = candidate, 1
        while name in used:
            n += 1
            name = f"{candidate}{n}"
        used.add(name)
        names.append(name)
    return names
