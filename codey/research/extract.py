"""Turn raw HTML into clean readable text."""

from __future__ import annotations

import contextlib
import re
from html.parser import HTMLParser

_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "head"}
_BLOCK_TAGS = {
    "p", "div", "section", "article", "header", "footer", "li", "ul", "ol",
    "table", "tr", "td", "th", "br", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre",
}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_DELETED_TAGS = {"s", "del", "strike"}
_MULTI_BLANK_RE = re.compile(r"\n{3,}")
_SPACES_RE = re.compile(r"[ \t\f\v]+")
_HIDDEN_STYLE_RE = re.compile(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\s*(?:!important\s*)?(?:;|$)", re.I)
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False
        self._in_head = False
        self._title_seen = False
        self._hidden_tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        hidden = ("hidden" in attributes or (attributes.get("aria-hidden") or "").lower() == "true"
                  or _HIDDEN_STYLE_RE.search(attributes.get("style", "") or ""))
        if self._hidden_tags or hidden:
            if tag not in _VOID_TAGS:
                self._hidden_tags.append(tag)
            return
        if tag == "head":
            self._in_head = True
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            if not self._title_seen and self._skip_depth == int(self._in_head):
                self._in_title = True
                self._title_seen = True
        elif tag in _HEADING_TAGS:
            self.parts.append("\n\n## ")
        elif tag in _DELETED_TAGS and not self._skip_depth:
            self.parts.append(" [deleted text: ")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self._hidden_tags:
            if tag in self._hidden_tags:
                # Tolerate omitted/mismatched HTML end tags without consuming
                # the next visible sibling as hidden content.
                index = len(self._hidden_tags) - 1 - self._hidden_tags[::-1].index(tag)
                del self._hidden_tags[index:]
            return
        if tag == "head":
            self._in_head = False
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in _DELETED_TAGS and not self._skip_depth:
            self.parts.append("] ")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._hidden_tags:
            return
        if self._in_title:
            self.title += data
            return
        if self._skip_depth > 0:
            return
        text = data.strip("\n")
        if text.strip():
            self.parts.append(text)


def extract_text(html: str) -> str:
    parser = _Extractor()
    with contextlib.suppress(Exception):
        parser.feed(html or "")
    text = "".join(parser.parts)
    lines = [_SPACES_RE.sub(" ", line).strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line is not None)
    return _MULTI_BLANK_RE.sub("\n\n", text).strip()


def extract_title(html: str) -> str:
    parser = _Extractor()
    with contextlib.suppress(Exception):
        parser.feed(html or "")
    return _SPACES_RE.sub(" ", parser.title).strip()


class _ApplicationShellParser(_Extractor):
    """Recognize an unfilled application region, not an arbitrary short page."""

    def __init__(self) -> None:
        super().__init__()
        self.bootstrap_script = False
        self.regions: list[tuple[int, int, bool]] = []
        self.tags: list[str] = []
        self.roots: list[str] = []
        self.mains: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "script" and (attributes.get("src") or attributes.get("type") == "module"):
            self.bootstrap_script = True
        root = tag == "app-root" or attributes.get("id") in {"root", "app", "__next"}
        main = bool(self.regions) and (tag == "main" or attributes.get("role") == "main")
        super().handle_starttag(tag, attrs)
        if root or main:
            self.regions.append((len(self.tags), len(self.parts), main))
        if tag not in _VOID_TAGS:
            self.tags.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self.tags:
            depth = len(self.tags) - 1 - self.tags[::-1].index(tag)
            while self.regions and self.regions[-1][0] >= depth:
                _, start, main = self.regions.pop()
                (self.mains if main else self.roots).append("".join(self.parts[start:]))
            del self.tags[depth:]
        super().handle_endtag(tag)


def is_application_shell(html: str) -> bool:
    parser = _ApplicationShellParser()
    parser.feed(html)
    regions = parser.mains or parser.roots
    return bool(parser.bootstrap_script and regions) and all(
        not text.strip() or re.fullmatch(r"(?:loading|please wait|加载中)[.\s…]*", text.strip(), re.I)
        for text in regions
    )


_MAX_PAGE_CHARS = 200_000

_FETCH_PAGE_ERROR_MARKERS = (
    "access denied",
    "are you a robot",
    "captcha",
    "checking your browser",
    "cookies must be enabled",
    "enable javascript",
    "enable cookies",
    "not automatically redirected",
    "temporarily unavailable",
    "unusual traffic",
    "verify you are human",
    "just a moment",
    "页面安全校验中",
    "正在验证您的浏览器",
    "人机验证",
    "请完成安全验证",
)

_FETCH_CHALLENGE_TEXT_MAX_CHARS = 1500

_FETCH_BOILERPLATE_LINES = frozenset({
    "hhs vulnerability disclosure", "skip to main content", "skip to content",
    "privacy policy", "accessibility", "terms of use", "contact us",
})

def _usable_fetch_page_text(text: str) -> bool:
    normalized = _clean_space(text).casefold()
    if not normalized:
        return False
    if _looks_like_short_fetch_challenge(normalized):
        return False
    if all(_clean_space(line).casefold() in _FETCH_BOILERPLATE_LINES
           for line in text.splitlines() if line.strip()):
        return False
    return len(normalized) >= 24

def _fetch_page_failure_kind(text: str) -> str:
    normalized = _clean_space(text).casefold()
    if _looks_like_short_fetch_challenge(normalized):
        return "page_unavailable"
    return "page_blank"

def _looks_like_short_fetch_challenge(normalized_text: str) -> bool:
    if len(normalized_text) > _FETCH_CHALLENGE_TEXT_MAX_CHARS:
        return False
    return any(marker in normalized_text for marker in _FETCH_PAGE_ERROR_MARKERS)

def _clean_space(text: str) -> str:
    return " ".join((text or "").split())
