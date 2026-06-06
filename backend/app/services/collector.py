from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


USER_AGENT = (
    "Mozilla/5.0 (compatible; IndustrialOpinionAgent/0.2; "
    "+https://localhost/industrial-opinion-agent)"
)

NON_CONTENT_TAGS = {"script", "style", "noscript", "template", "svg", "canvas", "iframe", "object", "embed"}

NOISE_PATTERNS = [
    re.compile(r"(?is)<(script|style|noscript|template|svg|canvas|iframe|object|embed)\b[^>]*>.*?</\1>"),
    re.compile(r"(?is)<!--.*?-->"),
    re.compile(r"(?is)//\s*百度统计\s*var\s+_hmt\b.*?\}\)\(\s*\)\s*;?"),
    re.compile(r"(?is)var\s+_hmt\s*=\s*_hmt\s*\|\|\s*\[\]\s*;.*?\}\)\(\s*\)\s*;?"),
    re.compile(r"(?is)\$\(document\)\.ready\s*\(\s*function\s*\(\)\s*\{.*?\}\s*\)\s*;?"),
    re.compile(r"(?is)\(function\s*\(\s*html\s*\)\s*\{.*?\}\s*\)\s*\(\s*document\.documentElement\s*\)\s*;?"),
    re.compile(r"(?is)\(function\s*\(\s*\)\s*\{.*?MobileDetect.*?\}\s*\)\s*\(\s*\)\s*;?"),
    re.compile(r"(?is)\(function\s*\(\s*w\s*,\s*d\s*,\s*s\s*,\s*l\s*,\s*i\s*\)\s*\{.*?\}\s*\)\s*\([^)]*\)\s*;?"),
    re.compile(r"(?is)window\.addEventListener\s*\([^;{]*\{.*?\}\s*\)\s*;?"),
    re.compile(r"(?is)addAdvertInfo\s*\([^)]*\)\s*;?"),
]

CSS_NOISE_PATTERN = re.compile(
    r"(?is)(?:[#.:a-zA-Z0-9_\-\[\]=,\"^|~\s]+)\{[^{}]*(?:display|padding|margin|font-size|color|"
    r"list-style|justify-content|gap|contain-intrinsic-size|--wp-|background|height|width|"
    r"flex-basis|flex-grow|flex-wrap|align-self|position|z-index|clip-path)[^{}]*\}"
)

HTML_TAG_PATTERN = re.compile(r"(?is)<[^>]+>")
EXTENDED_NOISE_PATTERNS = [
    re.compile(r"(?is)/\*.*?\*/"),
    re.compile(r"(?is)(?://\s*.{0,40}?\s*)?var\s+_hmt\s*=\s*_hmt\s*\|\|\s*\[\]\s*;.*?\}\)\(\s*\)\s*;?"),
    re.compile(r"(?is)window\.dataLayer\s*=\s*window\.dataLayer\s*\|\|\s*\[\]\s*;?"),
    re.compile(r"(?is)function\s+gtag\s*\(\s*\)\s*\{\s*dataLayer\.push\(arguments\)\s*;?\s*\}"),
    re.compile(r"(?is)gtag\s*\([^;]*\)\s*;?"),
    re.compile(r"(?is)var\s+agree\s*=\s*\$\.cookie\([^;]*;?"),
    re.compile(r"(?is)var\s+storage\s*=\s*['\"](?:denied|granted)['\"]\s*;?"),
    re.compile(r"(?is)if\s*\(\s*agree\s*\)\s*\{\s*storage\s*=\s*['\"]granted['\"]\s*\}"),
    re.compile(r"(?is)devtoolsDetector\.addListener\s*\(.*?\)\s*devtoolsDetector\.launch\(\)\s*;?"),
    re.compile(r"(?is)devtoolsDetector\.launch\(\)\s*;?"),
    re.compile(r"(?is)\$\(\s*function\s*\(\)\s*\{.*?(?:click|mousemove|mouseleave|mouseenter|scroll).*?\}\s*\)\s*;?"),
    re.compile(r"(?is)generate one if it doesn't exist\..*?sourceURL=wp-block-template-skip-link-js-after"),
    re.compile(r"(?is)var\s+ttsPlayerConfig\s*=\s*\{.*?\}\s*;"),
    re.compile(r"(?is)new\s+WX_Custom_Share\(\)\.init\(\)\s*;?"),
    re.compile(
        r"(?is)\(function\s*\(\s*\)\s*\{\s*var\s+\w+\s*=\s*document\.createElement\(['\"]script['\"]\)"
        r".*?parentNode\.insertBefore\(\s*\w+\s*,\s*\w+\s*\)\s*;?\s*\}\s*\)\s*\(\s*\)\s*;?"
    ),
    re.compile(
        r"(?is)(?:new\s+WX_Custom_Share\(\)\.init\(\)\s*;?\s*)?"
        r"\(function\s*\(\s*\)\s*\{\s*var\s+\w+\s*=\s*document\.createElement\s*\(\s*['\"]script['\"]\s*\)"
        r".*?(?:bdstatic|zhanzhang|AreaHits|hm\.gasgoo).*?parentNode\.insertBefore\s*\([^;]*\)\s*;?"
    ),
    re.compile(
        r"(?is)//.{0,80}?(?:stats|statistics|tongji|统计|AreaHits).*?"
        r"parentNode\.insertBefore\(\s*\w+\s*,\s*\w+\s*\)\s*;?"
    ),
    re.compile(r"(?is)//\s*gasgoo.*?parentNode\.insertBefore\(\s*\w+\s*,\s*\w+\s*\)\s*;?"),
    re.compile(r"(?is)document\.createElement\s*\(\s*['\"]script['\"]\s*\).*?parentNode\.insertBefore\s*\([^;]*\)\s*;?"),
    re.compile(r"(?is)var\s+_gas_hm\s*=\s*_gas_hm\s*\|\|\s*\[\]\s*;.*?_gas_hm\.push\([^;]*\)\s*;?"),
    re.compile(r"(?is)_gas_hm\.push\([^;]*\)\s*;?"),
    re.compile(r"(?is)采购项目\s*\$\(\"#TopProcurement\"\).*?——\s*全球视野·中国声音\s*——"),
    re.compile(r"(?is)\$\(\"#top_search\"\)\.click\s*\(.*?\$\(document\)\.click\s*\(.*?\.hide\(\)\s*;?"),
    re.compile(r"(?is)jQuery\s*\(\s*function\s*\(\s*\$\s*\)\s*\{.*?\$\.ajax\s*\([^)]*\)\s*;?"),
]
EXTENDED_CSS_NOISE_PATTERNS = [
    re.compile(r"(?is)@media\s*\([^)]*\)\s*\{\s*\}"),
    re.compile(
        r"(?is)(?:@media\s+)?screen\s+and\s*\(\s*(?:max|min)-width\s*:\s*\d+px\s*\)"
        r"(?:\s+and\s*\(\s*orientation\s*:\s*[a-z]{0,12})?"
    ),
    re.compile(r"(?is)html\s+:where\(img\[class\*.*?(?=\s*(?:\$\(|Article paragraph|[\u4e00-\u9fff]{2,}|$))"),
    re.compile(r"(?is)img:is\([^)]*\)\s*\{[^{}]*\}"),
    re.compile(r"(?is)\.has-fit-text\s*\{[^{}]*\}"),
    re.compile(r"(?is)(?:html\s+:where\(\[style\*\s*)+"),
    re.compile(r"(?is):root\s*\{[^{}]*--wp-[^{}]*\}"),
    re.compile(
        r"(?is)(?:@media[^{]+)?[#.:a-zA-Z0-9_&>\-\[\]=,\"'^|~\s()*.]+"
        r"\{[^{}]*(?:display|padding|margin|font-size|color|list-style|justify-content|gap|cursor|"
        r"text-align|contain-intrinsic-size|--wp-|var\(--wp-|background|height|width|border|box-sizing|"
        r"opacity|pointer-events|visibility|stroke|overflow|rotate|clear|text-decoration|flex-basis|"
        r"flex-grow|flex-wrap|align-self|position|z-index|clip-path)[^{}]*\}"
    ),
]
MORE_NOISE_PATTERNS = [
    re.compile(r"(?is)//\s*Define Adobe Target Property.*?\}\s*\(\s*\)\s*;?"),
    re.compile(r"(?is)//\s*ContentSquare functions\s+function\s+isEmpty\s*\([^)]*\)\s*\{\s*return\s*\([^)]*?val\.length"),
    re.compile(r"(?is)//\s*\$\('\.biaojiwei'\)\.click.*?//\s*img\s+id=['\"]wx_img['\"][^>]*>?"),
    re.compile(r"(?is)\$\.ajax\s*\(\s*\{.*?(?:BydInvestorNotice|/sites/REST/resources).*?\}\s*\)\s*;?"),
    re.compile(r"(?is)WX_Custom_Share\s*=\s*function\s*\(\)\s*\{.*?setShareInfo\s*\(\s*info\s*\)\s*;?\s*\}\s*;?"),
    re.compile(r"(?is)jQuery\s*\(\s*function\s*\(\s*\)\s*\{\s*\$\.ajax\s*\(\s*\{.*?Home\.aspx/GetComments.*?正在热评\s*实时热评"),
]
ORPHAN_CODE_PATTERN = re.compile(r"(?is)(?:^|\s)(?:\}\s*\)\s*;?|\}\s*\)\s*\(\s*\)\s*;?|[{}])+(?=\s|$)")
CHINA_TIMEZONE = timezone(timedelta(hours=8))
PUBLISHED_META_KEYS = (
    "article:published_time",
    "og:article:published_time",
    "publishdate",
    "publish_date",
    "pubdate",
    "date",
    "dc.date",
    "dc.date.issued",
)
PUBLISHED_TEXT_PATTERNS = (
    re.compile(
        r"(?:发布时间|发布日期|发稿时间|更新时间|日期|时间)\s*[:：]?\s*"
        r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
        r"(?:\s*(\d{1,2})\s*[:：]\s*(\d{1,2})(?:\s*[:：]\s*(\d{1,2}))?)?"
    ),
    re.compile(
        r"(?:发布时间|发布日期|发稿时间|更新时间|日期|时间)\s*[:：]?\s*"
        r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})"
        r"(?:\s*(\d{1,2})\s*[:：]\s*(\d{1,2})(?:\s*[:：]\s*(\d{1,2}))?)?"
    ),
)
URL_DATE_PATTERN = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)")


@dataclass(frozen=True)
class FetchedDocument:
    url: str
    text: str
    content_type: str


class CollectionError(RuntimeError):
    """Raised when a configured source cannot be fetched or parsed safely."""


class _ListingHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.meta: dict[str, str] = {}
        self.links: list[dict[str, str]] = []
        self.body_parts: list[str] = []
        self._in_title = False
        self._current_href: str | None = None
        self._current_link_parts: list[str] = []
        self._ignored_tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in NON_CONTENT_TAGS:
            self._ignored_tags.append(tag)
            return
        if self._ignored_tags:
            return
        attr_map = {key.lower(): value or "" for key, value in attrs}
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (attr_map.get("name") or attr_map.get("property") or "").lower()
            content = attr_map.get("content", "")
            if key and content:
                self.meta[key] = compact_text(unescape(content))
        elif tag == "a":
            href = attr_map.get("href", "")
            if href:
                self._current_href = href
                self._current_link_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._ignored_tags:
            if tag == self._ignored_tags[-1]:
                self._ignored_tags.pop()
            elif tag in self._ignored_tags:
                self._ignored_tags.pop(len(self._ignored_tags) - 1 - self._ignored_tags[::-1].index(tag))
            return
        if tag == "title":
            self._in_title = False
        elif tag == "a" and self._current_href:
            text = compact_text(" ".join(self._current_link_parts))
            if text:
                self.links.append({"href": self._current_href, "text": text})
            self._current_href = None
            self._current_link_parts = []

    def handle_data(self, data: str) -> None:
        if self._ignored_tags:
            return
        text = clean_article_text(data)
        if not text:
            return
        if self._in_title:
            self.title_parts.append(text)
        if self._current_href:
            self._current_link_parts.append(text)
        if len(text) > 1:
            self.body_parts.append(text)

    @property
    def title(self) -> str:
        return compact_text(" ".join(self.title_parts))

    @property
    def description(self) -> str:
        return self.meta.get("description") or self.meta.get("og:description") or ""

    @property
    def body_text(self) -> str:
        return compact_text(" ".join(self.body_parts))

    @property
    def published_at(self) -> str | None:
        return _published_at_from_meta(self.meta) or extract_published_at_from_text(self.body_text)


def compact_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def clean_article_text(value: str | None) -> str:
    """Remove webpage code/style noise while preserving readable article text."""
    text = compact_text(unescape(value or ""))
    if not text:
        return ""
    text = _strip_balanced_json_ld(text)
    text = _strip_balanced_css_at_rules(text)
    text = _strip_broken_css_fragments(text)
    text = _strip_known_javascript_noise(text)
    for pattern in NOISE_PATTERNS:
        text = pattern.sub(" ", text)
    for pattern in EXTENDED_NOISE_PATTERNS:
        text = pattern.sub(" ", text)
    for pattern in MORE_NOISE_PATTERNS:
        text = pattern.sub(" ", text)
    previous = None
    while previous != text:
        previous = text
        text = _strip_known_javascript_noise(text)
        text = _strip_balanced_css_at_rules(text)
        text = _strip_broken_css_fragments(text)
        text = CSS_NOISE_PATTERN.sub(" ", text)
        for pattern in EXTENDED_CSS_NOISE_PATTERNS:
            text = pattern.sub(" ", text)
    text = ORPHAN_CODE_PATTERN.sub(" ", text)
    text = HTML_TAG_PATTERN.sub(" ", text)
    return compact_text(text)


def _strip_known_javascript_noise(text: str) -> str:
    text = _strip_government_header_noise(text)
    text = _strip_ndrc_page_scripts(text)
    text = _strip_d1ev_page_noise(text)
    text = _strip_calls_containing(text, "$.ajax", ("BydInvestorNotice",))
    text = _strip_calls_containing(text, "$.ajax", ("GetArticleVideos",))
    text = _strip_calls_containing(text, "$.ajax", ("Home.aspx/GetComments",))
    text = _strip_calls_containing(text, "jQuery", ("Home.aspx/GetComments",))
    text = _strip_malformed_calls_containing(text, "$.ajax", ("BydInvestorNotice", "/sites/REST/resources"))
    text = _strip_malformed_calls_containing(text, "$.ajax", ("GetArticleVideos",))
    text = _strip_malformed_calls_containing(text, "$.ajax", ("Home.aspx/GetComments",))
    text = _strip_malformed_function_containing("function acquirePdfUrl", text, ("$.ajax", "pdfJson", "pdfOpen"))
    text = _strip_function_assignment_containing(text, "WX_Custom_Share", ("admin-ajax.php",))
    text = re.sub(r"(?is)\$\('#biaojiwei2'\)\.click.*?(?=QbitAI|$)", " ", text)
    text = re.sub(r"(?is)//\s*\$\('\.biaojiwei'\)\.click.*?(?=QbitAI|Physical AI article|$)", " ", text)
    text = re.sub(r"(?is)jQuery\s*\(\s*function\s*\(\s*\)\s*\{\s*(?:正在热评\s*实时热评\s*)?", " ", text)
    text = _strip_gasgoo_page_noise(text)
    return text


def _strip_gasgoo_page_noise(text: str) -> str:
    text = re.sub(
        r"(?is)\{\s*var\s+html4\s*=\s*['\"]{2}\s*;\s*for\s*\(\s*var\s+index\s+in\s+data\.d\.purchaseInfo\s*\)"
        r"\s*\{.*?purchase_supply\s+ul.*?html\s*\(\s*html4\s*\)\s*;?\s*\}",
        " ",
        text,
    )
    text = re.sub(r"(?is)function\s+getRandom\s*\([^)]*\)\s*\{[^{}]*?Math\.floor\s*\([^{}]*?\}\s*", " ", text)
    text = re.sub(
        r"(?is)首页\s+资讯\s+行业\s+车企\s+供应链\s+智能网联\s+新能源\s+新技术\s+销量\s+高端访谈\s+内参\s+上市公司\s+创投"
        r".*?邮件订阅",
        " ",
        text,
    )
    text = re.sub(
        r"(?is)盖世汽车资讯官方QQ\s+\d+.*?(?:提交成功|即时体验|即刻体验|采购\s+项目)",
        " ",
        text,
    )
    text = re.sub(r"(?is)用户\s+反馈\s+提示\s+验证码输入错误.*?提交成功", " ", text)
    text = re.sub(r"(?is)var\s+isFirst\b.*?(?=\s*(?:本文地址|好文章|24小时热文|联系我们|$))", " ", text)
    text = re.sub(r"(?is)\{if\(result\.d\s*!=\s*null.*?articleBottomAd.*?\}\)\s*;?\s*\}\)\s*;?\s*\}\)?", " ", text)
    text = re.sub(r"(?is)版权声明：本文为盖世汽车.*?(?=\s*(?:[一-龥]{6,}|$))", " ", text)
    text = re.sub(r"(?is)如欲转载请遵守\s*转载说明.*?(?=\s*(?:本文地址|好文章|24小时热文|联系我们|$))", " ", text)
    text = re.sub(r"(?is)本文地址：https?://auto\.gasgoo\.com/\S+.*?(?=\s*(?:24小时热文|联系我们|$))", " ", text)
    text = re.sub(r"(?is)好文章，需要你的鼓励.*?(?=\s*(?:24小时热文|联系我们|$))", " ", text)
    text = re.sub(r"(?is)24小时热文.*?(?=\s*(?:联系我们|//盖世板块统计代码|$))", " ", text)
    text = re.sub(r"(?is)//盖世板块统计代码异步加载.*$", " ", text)
    text = re.sub(r"(?is)联系我们\s+联系邮箱：info@gasgoo\.com.*$", " ", text)
    text = re.sub(r"(?is)var\s+applyPurchaseLinkEle\s*=.*$", " ", text)
    return text


def _strip_d1ev_page_noise(text: str) -> str:
    text = re.sub(r"(?is)\.add-author\s*\{[^{}]*\}", " ", text)
    if "APP下载" in text and "登录 注册" in text:
        byline = re.search(r"(?is)20\d{2}-\d{2}-\d{2}\s+\d{2}:\d{2}\s*//\s*", text[:1200])
        if byline:
            text = text[byline.end() :]
        else:
            text = re.sub(
                r"(?is)电动汽车\s+搜索\s+提问\s+APP下载\s+第一电动\s+充电桩\s+一度用车\s+登录\s+注册\s+投稿"
                r"\s+首页\s+快讯\s+视频\s+专题活动\s+首页\s+资讯\s+市场",
                " ",
                text,
            )
            text = re.sub(r"(?is)//\s*评论\s+收藏\s+点赞.*?(?=\s*(?:要闻|在|[一-龥]{6,}|$))", " ", text)
    text = re.sub(r"(?is)\.add-author\s*\{.*?(?=\s*(?:[\u4e00-\u9fff]{6,}|[A-Z][A-Za-z ]{12,}|$))", " ", text)
    for marker in ("{{each", "{{if", "$(\".share--wraped\")", "$('.share--wraped')", "$(function () {"):
        index = text.find(marker)
        if index >= 0:
            prefix = text[:index]
            if _has_enough_readable_text(prefix):
                text = prefix
    return text


def _strip_ndrc_page_scripts(text: str) -> str:
    text = re.sub(
        r"(?is)var\s+mobilurl\s*=\s*['\"][^'\"]+['\"]\s*;?\s*uaredirect\s*\(\s*mobilurl\s*\)\s*;?",
        " ",
        text,
    )
    return re.sub(
        r"(?is)var\s+tmp\s*=\s*['\"]?\d+['\"]?\s*;\s*"
        r"\$\(\s*['\"]#fgw_['\"]\s*\+\s*tmp\s*\)\.[^;]{0,240};?\s*"
        r"(?:\$\(\s*['\"]#fgw_['\"]\s*\+\s*tmp\s*\)\.[^;]{0,240};?)?",
        " ",
        text,
    )


def _strip_government_header_noise(text: str) -> str:
    text = re.sub(r"(?is)var\s+INFO_FLAG\s*=\s*\{.*?\}\s*", " ", text)
    text = re.sub(r"(?is)首页\s+EN\s+登录\s+个人中心\s+退出\s+邮箱\s+无障碍\s+EN", " ", text)
    text = _strip_function_declaration_containing(text, "function goSearch", ("document.querySelector", "window.open"))
    text = _strip_function_declaration_containing(text, "function listenerKeyUpEventFn", ("goSearch",))
    text = re.sub(r"(?is)https?://www\.gov\.cn/\s*//繁体和简体相互转换.*?(?=\s*(?:[\u4e00-\u9fff]{6,}|Article paragraph|$))", " ", text)
    text = re.sub(r"(?is)//繁体和简体相互转换.*?(?=\s*(?:[\u4e00-\u9fff]{6,}|Article paragraph|$))", " ", text)
    return text


def _strip_balanced_css_at_rules(text: str) -> str:
    text = re.sub(r"(?is)@charset\s+['\"][^'\"]+['\"]\s*;?", " ", text)
    search_from = 0
    while True:
        match = re.search(r"(?is)@(media|supports|keyframes)\b", text[search_from:])
        if not match:
            return text
        start = search_from + match.start()
        brace_start = text.find("{", start)
        if brace_start < 0:
            boundary = _find_readable_text_boundary(text, start)
            end = boundary or len(text)
            text = f"{text[:start]} {text[end:]}"
            search_from = max(0, start - 1)
            continue
        if brace_start - start > 240:
            search_from = start + 1
            continue
        end = _find_balanced_end(text, brace_start, "{", "}")
        if end is None:
            boundary = _find_readable_text_boundary(text, brace_start)
            end = boundary or len(text)
        text = f"{text[:start]} {text[end:]}"
        search_from = max(0, start - 1)


def _strip_broken_css_fragments(text: str) -> str:
    markers = (
        ".wp-block",
        "wp-block-",
        ".section__",
        ".is-style-search",
        ".section-background-image-wrapper",
        ".social-share",
        ":where(.wp-block",
        ":root :where(",
        "body.dark .",
    )
    search_from = 0
    while True:
        positions = [(text.find(marker, search_from), marker) for marker in markers]
        positions = [(position, marker) for position, marker in positions if position >= 0]
        if not positions:
            return text
        marker_index, marker = min(positions, key=lambda item: item[0])
        if marker_index > 0 and not re.search(r"[\s};>(,]$", text[max(0, marker_index - 3) : marker_index]):
            search_from = marker_index + len(marker)
            continue
        segment_start = _find_css_fragment_start(text, marker_index)
        end = _find_readable_text_boundary(text, marker_index + len(marker)) or len(text)
        text = f"{text[:segment_start]} {text[end:]}"
        search_from = max(0, segment_start - 1)


def _find_css_fragment_start(text: str, marker_index: int) -> int:
    segment_start = marker_index
    while segment_start > 0 and text[segment_start - 1] in " };,":
        segment_start -= 1
    selector_prefix = text[max(0, segment_start - 80) : segment_start]
    prefix_match = re.search(r"(?is)(?:^|\s)(?:[a-z][a-z0-9-]*|[.#][a-z0-9_-]+)\s*,\s*$", selector_prefix)
    if prefix_match:
        prefix_base = segment_start - len(selector_prefix)
        return max(0, prefix_base + prefix_match.start())
    return segment_start


def _strip_function_declaration_containing(text: str, marker: str, required_terms: tuple[str, ...]) -> str:
    search_from = 0
    while True:
        start = text.find(marker, search_from)
        if start < 0:
            return text
        brace_start = text.find("{", start + len(marker))
        if brace_start < 0:
            return text
        end = _find_balanced_end(text, brace_start, "{", "}")
        if end is None:
            boundary = _find_readable_text_boundary(text, brace_start)
            if boundary is None:
                search_from = start + len(marker)
                continue
            segment = text[start:boundary]
            end = boundary
        else:
            segment = text[start:end]
        if all(term in segment for term in required_terms):
            text = f"{text[:start]} {text[end:]}"
            search_from = max(0, start - 1)
        else:
            search_from = end


def _strip_calls_containing(text: str, call_name: str, required_terms: tuple[str, ...]) -> str:
    search_from = 0
    while True:
        start = text.find(call_name, search_from)
        if start < 0:
            return text
        paren_start = text.find("(", start + len(call_name))
        if paren_start < 0:
            return text
        end = _find_balanced_end(text, paren_start, "(", ")")
        if end is None:
            search_from = start + len(call_name)
            continue
        segment = text[start:end]
        if any(term in segment for term in required_terms):
            if end < len(text) and text[end : end + 1] == ";":
                end += 1
            text = f"{text[:start]} {text[end:]}"
            search_from = max(0, start - 1)
        else:
            search_from = end


def _strip_malformed_calls_containing(text: str, call_name: str, required_terms: tuple[str, ...]) -> str:
    search_from = 0
    while True:
        start = text.find(call_name, search_from)
        if start < 0:
            return text
        paren_start = text.find("(", start + len(call_name))
        if paren_start < 0:
            return text
        balanced_end = _find_balanced_end(text, paren_start, "(", ")")
        if balanced_end is not None:
            search_from = balanced_end
            continue

        scan_window = text[start : start + 2000]
        term_matches = [
            (scan_window.find(term), term)
            for term in required_terms
            if scan_window.find(term) >= 0
        ]
        if not term_matches:
            search_from = start + len(call_name)
            continue

        first_offset, first_term = min(term_matches, key=lambda match: match[0])
        term_end = start + first_offset + len(first_term)
        end = _find_readable_text_boundary(text, term_end)
        if end is None:
            search_from = start + len(call_name)
            continue
        text = f"{text[:start]} {text[end:]}"
        search_from = max(0, start - 1)


def _strip_malformed_function_containing(marker: str, text: str, required_terms: tuple[str, ...]) -> str:
    search_from = 0
    while True:
        start = text.find(marker, search_from)
        if start < 0:
            return text
        brace_start = text.find("{", start + len(marker))
        if brace_start < 0:
            return text
        balanced_end = _find_balanced_end(text, brace_start, "{", "}")
        if balanced_end is not None:
            search_from = balanced_end
            continue

        scan_window = text[start : start + 2000]
        if not all(term in scan_window for term in required_terms):
            search_from = start + len(marker)
            continue
        last_term_end = start + max(scan_window.find(term) + len(term) for term in required_terms)
        end = _find_readable_text_boundary(text, last_term_end) or len(text)
        text = f"{text[:start]} {text[end:]}"
        search_from = max(0, start - 1)


def _find_readable_text_boundary(text: str, start: int) -> int | None:
    for match in re.finditer(r"\s+(?=\S)", text[start:]):
        candidate = start + match.end()
        if _looks_like_readable_text(text[candidate : candidate + 160]):
            return candidate
    return None


def _looks_like_readable_text(window: str) -> bool:
    sample = compact_text(window)
    if not sample:
        return False
    code_markers = (
        "$.",
        "ajax",
        "function",
        "var ",
        "url:",
        "type:",
        "success:",
        "data:",
        "console.",
        "charset",
    )
    sample_head = sample[:100].lower()
    marker_positions = [sample_head.find(marker) for marker in code_markers if sample_head.find(marker) >= 0]
    if marker_positions:
        readable_prefix = sample[: min(marker_positions)].strip()
        if _has_enough_readable_text(readable_prefix):
            return True
        return False
    if re.search(r"[{};=]", sample[:80]):
        return False
    return _has_enough_readable_text(sample)


def _has_enough_readable_text(sample: str) -> bool:
    english_words = re.findall(r"[A-Za-z][A-Za-z-]{2,}", sample[:120])
    chinese_chars = re.findall(r"[\u4e00-\u9fff]", sample[:120])
    return len(english_words) >= 5 or len(chinese_chars) >= 12


def _strip_function_assignment_containing(text: str, name: str, required_terms: tuple[str, ...]) -> str:
    search_from = 0
    marker = f"{name} = function"
    while True:
        start = text.find(marker, search_from)
        if start < 0:
            return text
        brace_start = text.find("{", start + len(marker))
        if brace_start < 0:
            return text
        end = _find_balanced_end(text, brace_start, "{", "}")
        if end is None:
            search_from = start + len(marker)
            continue
        segment = text[start:end]
        if any(term in segment for term in required_terms):
            if end < len(text) and text[end : end + 1] == ";":
                end += 1
            text = f"{text[:start]} {text[end:]}"
            search_from = max(0, start - 1)
        else:
            search_from = end


def _strip_css_noise_segments(text: str) -> str:
    markers = (
        "--wp-",
        "var(--wp-",
        ".wp-block",
        "wp-block-",
        ".wp-element",
        ".social-share",
        ".has-drop-cap",
        "forced-colors:",
        "writing-mode:",
    )
    while True:
        marker_positions = [text.find(marker) for marker in markers if text.find(marker) >= 0]
        if not marker_positions:
            return text
        marker_index = min(marker_positions)
        brace_start = text.rfind("{", 0, marker_index + 1)
        next_brace = text.find("{", marker_index)
        if brace_start < 0 and next_brace < 0:
            return text
        if brace_start < 0 or (0 <= next_brace < brace_start):
            brace_start = next_brace
        segment_end = _find_balanced_end(text, brace_start, "{", "}")
        if segment_end is None:
            return text[:marker_index] + " " + text[marker_index + len("--wp-") :]
        segment_start = _find_segment_start(text, brace_start)
        text = f"{text[:segment_start]} {text[segment_end:]}"


def _find_segment_start(text: str, brace_start: int) -> int:
    boundaries = [text.rfind("}", 0, brace_start), text.rfind(";", 0, brace_start), text.rfind("\n", 0, brace_start)]
    boundary = max(boundaries)
    return boundary + 1 if boundary >= 0 else 0


def _strip_balanced_json_ld(text: str) -> str:
    for marker in ('{"@context"', "{'@context'"):
        while True:
            start = text.find(marker)
            if start < 0:
                break
            end = _find_balanced_end(text, start, "{", "}")
            if end is None:
                break
            text = f"{text[:start]} {text[end:]}"
    return text


def _find_balanced_end(text: str, start: int, opener: str, closer: str) -> int | None:
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def make_excerpt(content: str, limit: int = 360) -> str:
    text = clean_article_text(content)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def build_content_hash(title: str, content: str) -> str:
    normalized = compact_text(f"{title}\n{content}").lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def normalize_raw_item(raw: dict[str, Any], fetched_at: str | None = None) -> dict[str, Any]:
    """Normalize one original article/news item.

    A raw item is the full source article record, not a vector chunk. Chunks can
    be derived later for Chroma without mutating this factual ledger entry.
    """
    title = compact_text(raw.get("title"))
    url = compact_text(raw.get("url"))
    raw_content = raw.get("raw_content") or raw.get("content") or raw.get("summary") or ""
    raw_content = clean_article_text(raw_content)
    if not title:
        raise ValueError("raw item title is required")
    if not url:
        raise ValueError("raw item url is required")
    fetched = fetched_at or raw.get("fetched_at") or datetime.now(timezone.utc).isoformat()
    return {
        "source_id": raw.get("source_id"),
        "url": url,
        "title": title,
        "author": compact_text(raw.get("author")),
        "published_at": raw.get("published_at"),
        "fetched_at": fetched,
        "raw_content": raw_content,
        "content_excerpt": make_excerpt(raw_content),
        "content_hash": build_content_hash(title, raw_content),
        "status": raw.get("status") or "new",
        "source_type": raw.get("source_type"),
        "industry_hint": raw.get("industry_hint"),
        "relevance_industry": raw.get("relevance_industry"),
        "relevance_confidence": raw.get("relevance_confidence"),
        "relevance_reason": raw.get("relevance_reason"),
        "relevance_matched_terms": raw.get("relevance_matched_terms") or [],
        "relevance_provider": raw.get("relevance_provider"),
        "relevance_model": raw.get("relevance_model"),
    }


DEFAULT_SOURCE_ITEM_LIMIT = 200


def fetch_source_items(
    source: dict[str, Any],
    limit: int = DEFAULT_SOURCE_ITEM_LIMIT,
    timeout: int = 8,
    article_page_limit: int | None = None,
) -> list[dict[str, Any]]:
    """Collect real article records from a configured source URL.

    The collector intentionally returns an empty list only when the source can
    be fetched but no real candidate record is discoverable. Network failures,
    HTTP errors, and unparsable source documents are surfaced as CollectionError
    so the caller can record source health instead of inventing data.
    """
    document = fetch_url(str(source["url"]), timeout=timeout)

    def article_fetcher(url: str) -> str:
        return fetch_url(url, timeout=timeout).text

    items = extract_source_items(
        source,
        document.text,
        document.url,
        limit=limit,
        article_fetcher=article_fetcher,
        article_page_limit=article_page_limit,
        published_after=source.get("published_after"),
        published_before=source.get("published_before"),
    )
    return items


def fetch_url(url: str, timeout: int = 12) -> FetchedDocument:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read()
            content_type = response.headers.get("content-type", "")
            final_url = response.geturl()
    except HTTPError as exc:
        raise CollectionError(f"HTTP {exc.code} while fetching {url}") from exc
    except URLError as exc:
        raise CollectionError(f"Network error while fetching {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise CollectionError(f"Timeout while fetching {url}") from exc

    return FetchedDocument(url=final_url, text=_decode_bytes(payload, content_type), content_type=content_type)


def extract_source_items(
    source: dict[str, Any],
    document_text: str,
    base_url: str,
    limit: int = 12,
    article_fetcher: Any | None = None,
    article_page_limit: int | None = None,
    published_after: datetime | str | None = None,
    published_before: datetime | str | None = None,
) -> list[dict[str, Any]]:
    text = document_text.strip()
    if not text:
        return []
    window = _published_window(published_after, published_before)
    if _looks_like_xml(text):
        return _extract_feed_items(source, text, base_url, limit, article_fetcher, article_page_limit, window)
    return _extract_html_items(source, text, base_url, limit, article_fetcher, article_page_limit, window)


def _extract_feed_items(
    source: dict[str, Any],
    document_text: str,
    base_url: str,
    limit: int,
    article_fetcher: Any | None,
    article_page_limit: int | None,
    window: tuple[datetime | None, datetime | None],
) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(document_text)
    except ET.ParseError as exc:
        raise CollectionError(f"Unable to parse XML feed: {exc}") from exc
    records: list[dict[str, Any]] = []
    for node in root.iter():
        if _local_name(node.tag) not in {"item", "entry"}:
            continue
        title = compact_text(_child_text(node, {"title"}))
        link = _feed_link(node)
        summary = compact_text(_child_text(node, {"description", "summary", "content", "encoded"}))
        published_at = _normalize_published_at(_child_text(node, {"pubDate", "published", "updated"}))
        if not title or not link:
            continue
        url = urljoin(base_url, link)
        content = summary
        if article_fetcher:
            article = _article_from_fetcher(article_fetcher, url)
            if article["content"]:
                title = article["title"] or title
                content = article["content"]
                published_at = published_at or article["published_at"]
        if not content:
            continue
        if not _published_in_window(published_at or extract_published_at_from_url(url), window):
            continue
        records.append(_candidate(source, url, title, content, published_at))
        if len(records) >= limit:
            break
    return records


def _extract_html_items(
    source: dict[str, Any],
    document_text: str,
    base_url: str,
    limit: int,
    article_fetcher: Any | None,
    article_page_limit: int | None,
    window: tuple[datetime | None, datetime | None],
) -> list[dict[str, Any]]:
    parser = _parse_html(document_text)
    records: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for link in parser.links:
        title = compact_text(link.get("text"))
        url = _clean_url(urljoin(base_url, link.get("href", "")))
        if not _usable_candidate_url(base_url, url, title):
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        if not article_fetcher:
            continue
        content = ""
        published_at = None
        article = _article_from_fetcher(article_fetcher, url)
        if article["content"]:
            title = article["title"] or title
            content = article["content"]
            published_at = article["published_at"]
        if not _published_in_window(published_at or extract_published_at_from_url(url), window):
            continue
        if not _has_substantive_article_body(content, title):
            continue
        if len(content) < 20:
            continue
        records.append(_candidate(source, url, title, content, published_at))
        if len(records) >= limit:
            break
    return records


def _candidate(source: dict[str, Any], url: str, title: str, raw_content: str, published_at: str | None) -> dict[str, Any]:
    return {
        "source_id": source.get("id"),
        "url": url,
        "title": title,
        "raw_content": clean_article_text(raw_content),
        "published_at": published_at or extract_published_at_from_url(url),
    }


def _published_window(
    published_after: datetime | str | None,
    published_before: datetime | str | None,
) -> tuple[datetime | None, datetime | None]:
    return _coerce_datetime(published_after), _coerce_datetime(published_before)


def _coerce_datetime(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _published_in_window(value: str | None, window: tuple[datetime | None, datetime | None]) -> bool:
    start, end = window
    if not start and not end:
        return True
    published = _coerce_datetime(value)
    if published is None:
        return False
    comparable = published.astimezone(timezone.utc)
    if start and comparable < start.astimezone(timezone.utc):
        return False
    if end and comparable > end.astimezone(timezone.utc):
        return False
    return True


def _has_substantive_article_body(content: str, title: str = "") -> bool:
    text = clean_article_text(content)
    if not text or len(text) < 20:
        return False
    shell_markers = (
        "首页",
        "APP下载",
        "登录",
        "注册",
        "投稿",
        "验证码",
        "联系我们",
        "联系邮箱",
        "版权所有",
        "站内导航",
        "版权声明",
        "扫码下载APP",
        "用户 反馈",
        "提交成功",
    )
    marker_count = sum(1 for marker in shell_markers if marker in text)
    title_text = compact_text(title)
    body_without_title = text.replace(title_text, " ", 1) if title_text else text
    meaningful_body = re.sub(r"(?is)\bhttps?://\S+", " ", body_without_title)
    shell_phrase_hits = 0
    for shell_phrase in (
        "企业库",
        "销量",
        "查询",
        "采购",
        "项目",
        "AutoNews",
        "工具栏",
        "寻求",
        "报道",
        "公众号",
    ):
        if shell_phrase in meaningful_body:
            shell_phrase_hits += 1
            meaningful_body = meaningful_body.replace(shell_phrase, " ")
    chinese_chars = re.findall(r"[\u4e00-\u9fff]", body_without_title)
    meaningful_chinese_chars = re.findall(r"[\u4e00-\u9fff]", meaningful_body)
    meaningful_english_words = re.findall(r"[A-Za-z][A-Za-z-]{2,}", meaningful_body)
    sentence_count = len(re.findall(r"[。！？!?]", body_without_title))
    if marker_count >= 5 and (len(chinese_chars) < 120 or sentence_count < 2):
        return False
    if "html4" in text or "purchaseInfo" in text or "ProductName" in text or "add-author" in text:
        return False
    if _looks_like_semantic_tag_shell(meaningful_body):
        return False
    if marker_count >= 3 and len(chinese_chars) < 80 and len(re.findall(r"[A-Za-z][A-Za-z-]{2,}", body_without_title)) < 20:
        return False
    if shell_phrase_hits >= 2 and len(meaningful_chinese_chars) < 20 and len(meaningful_english_words) < 8:
        return False
    return True


def _looks_like_semantic_tag_shell(text: str) -> bool:
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9-]*", text.lower())
    if len(tokens) < 3:
        return False
    semantic_tag_tokens = {
        "a",
        "article",
        "aside",
        "blockquote",
        "body",
        "button",
        "canvas",
        "cite",
        "div",
        "em",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "img",
        "input",
        "label",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "path",
        "section",
        "span",
        "strong",
        "svg",
        "ul",
    }
    return all(token in semantic_tag_tokens for token in tokens)


def _decode_bytes(payload: bytes, content_type: str) -> str:
    charset_match = re.search(r"charset=([\w.-]+)", content_type or "", re.I)
    encodings = [charset_match.group(1)] if charset_match else []
    encodings.extend(["utf-8", "gb18030", "windows-1252", "latin-1"])
    candidates: list[tuple[int, int, str]] = []
    for index, encoding in enumerate(dict.fromkeys(encodings)):
        try:
            decoded = payload.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
        candidates.append((_mojibake_score(decoded), index, decoded))
    if candidates:
        return min(candidates)[2]
    return payload.decode("utf-8", errors="replace")


def _mojibake_score(text: str) -> int:
    # Some sites misreport UTF-8 pages as latin-1/iso-8859-1. Single-byte decoders
    # never fail, so pick the candidate with the fewest common mojibake signals.
    markers = (
        "ï¼",
        "ï½",
        "ï»¿",
        "â€",
        "â€œ",
        "â€",
        "â",
        "Ã",
        "Â",
        "ä¸",
        "äº",
        "å",
        "åŽ",
        "å…",
        "æ–",
        "è½",
        "èƒ",
        "é¾",
        "çš",
        "ç”",
        "ç‰",
        "ç­",
        "ç®",
        "ç«",
        "ç‚",
        "ç›",
        "ç§",
        "ç»",
        "锛",
        "鈥",
        "銆",
        "妯",
        "鍙",
        "浜",
        "绛",
        "ðŸ",
    )
    score = text.count("\ufffd") * 100
    score += sum(1 for char in text if 0x80 <= ord(char) <= 0x9F) * 20
    score += sum(text.count(marker) for marker in markers) * 10
    return score


def _looks_like_xml(text: str) -> bool:
    prefix = text[:200].lower()
    return prefix.startswith("<?xml") or "<rss" in prefix or "<feed" in prefix or "<rdf" in prefix


def _parse_html(text: str) -> _ListingHTMLParser:
    parser = _ListingHTMLParser()
    parser.feed(text)
    return parser


def _article_from_fetcher(article_fetcher: Any, url: str) -> dict[str, str | None]:
    try:
        raw_html = article_fetcher(url)
    except Exception:
        return {"title": "", "content": "", "published_at": None}
    parser = _parse_html(raw_html)
    return {
        "title": parser.title,
        "content": parser.body_text or clean_article_text(raw_html),
        "published_at": parser.published_at,
    }


def _usable_candidate_url(base_url: str, url: str, title: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    if _clean_url(base_url) == url:
        return False
    if not title or len(title) < 4:
        return False
    lowered_url = url.lower()
    lowered_title = title.lower()
    listing_url_tokens = ["investorannals", "investorrelations", "periodic-reports"]
    listing_title_tokens = ["periodic reports", "investor relations", "annual reports", "interim reports"]
    if any(token in lowered_url for token in listing_url_tokens):
        return False
    if any(token in lowered_title for token in listing_title_tokens):
        return False
    if any(token in lowered_url for token in ["javascript:", "mailto:", "/login", "/signin", "beian.gov.cn"]):
        return False
    if lowered_url.endswith((".jpg", ".jpeg", ".png", ".gif", ".svg", ".css", ".js", ".ico", ".zip", ".rar")):
        return False
    path = parsed.path.lower().rstrip("/")
    path_parts = [part for part in path.split("/") if part]
    filename = path_parts[-1] if path_parts else ""
    nav_path_tokens = {
        "about",
        "contact",
        "lxwm",
        "xwzx",
        "news",
        "index",
        "home",
        "disclosure",
        "listed",
        "notice",
        "press",
        "blog",
        "reits",
    }
    nav_title_tokens = [
        "联系我们",
        "联系",
        "新闻中心",
        "新闻发布",
        "信息披露",
        "首页",
        "公网安备",
        "copyright",
        "sitemap",
        "关于我们",
    ]
    if filename in {"index.html", "index.htm", "index.shtml", ""}:
        parent = path_parts[-2] if len(path_parts) >= 2 else ""
        if parent in nav_path_tokens or any(token in title for token in nav_title_tokens):
            return False
    if path_parts and path_parts[-1] in nav_path_tokens:
        return False
    if any(token in title for token in nav_title_tokens):
        return False
    article_patterns = [
        r"/20\d{2}",
        r"20\d{6,}",
        r"t20\d{6}_",
        r"content_\d+",
        r"art_\w+",
        r"/c\.html$",
        r"/news/[^/]*\d+",
        r"/[^/]*\d+[^/]*\.(html|htm|shtml)$",
    ]
    if any(re.search(pattern, lowered_url) for pattern in article_patterns):
        return True
    article_title_tokens = [
        "发布",
        "召开",
        "印发",
        "公告",
        "通知",
        "公示",
        "签署",
        "会见",
        "调研",
        "投资",
        "融资",
        "发布",
        "launch",
        "announces",
        "article",
        "release",
        "reports",
    ]
    return len(title) >= 14 and any(token in title or token in lowered_title for token in article_title_tokens)


def _clean_url(url: str) -> str:
    parsed = urlparse(url)
    return parsed._replace(fragment="").geturl()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(node: ET.Element, names: set[str]) -> str:
    for child in list(node):
        if _local_name(child.tag) in names:
            return "".join(child.itertext())
    return ""


def _feed_link(node: ET.Element) -> str:
    for child in list(node):
        if _local_name(child.tag) != "link":
            continue
        href = child.attrib.get("href")
        if href:
            return href
        text = compact_text(child.text)
        if text:
            return text
    return ""


def _published_at_from_meta(meta: dict[str, str]) -> str | None:
    for key in PUBLISHED_META_KEYS:
        parsed = _normalize_published_at(meta.get(key))
        if parsed:
            return parsed
    return None


def extract_published_at_from_text(value: str | None) -> str | None:
    text = compact_text(value)
    if not text:
        return None
    for pattern in PUBLISHED_TEXT_PATTERNS:
        match = pattern.search(text[:1200])
        if not match:
            continue
        year, month, day, hour, minute, second = match.groups()
        try:
            parsed = datetime(
                int(year),
                int(month),
                int(day),
                int(hour or 0),
                int(minute or 0),
                int(second or 0),
                tzinfo=CHINA_TIMEZONE,
            )
        except ValueError:
            continue
        return parsed.isoformat()
    return None


def extract_published_at_from_url(url: str | None) -> str | None:
    text = compact_text(url)
    if not text:
        return None
    for match in URL_DATE_PATTERN.finditer(text):
        year, month, day = match.groups()
        try:
            parsed = datetime(int(year), int(month), int(day), tzinfo=CHINA_TIMEZONE)
        except ValueError:
            continue
        return parsed.isoformat()
    return None


def _normalize_published_at(value: str | None) -> str | None:
    text = compact_text(value)
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.isoformat()
    except (TypeError, ValueError):
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.isoformat()
    except ValueError:
        return None
