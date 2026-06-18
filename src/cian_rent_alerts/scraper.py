from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import cloudscraper
import requests
from bs4 import BeautifulSoup, Tag

from .models import Listing

logger = logging.getLogger(__name__)

LISTING_URL_RE = re.compile(r"https?://(?:[\w-]+\.)?cian\.ru/(rent|sale)/flat/(\d+)/?")
RELATIVE_LISTING_URL_RE = re.compile(r"/(rent|sale)/flat/(\d+)/?")
PRICE_RE = re.compile(r"(\d[\d\s]{2,})\s*(?:₽|руб\.?|р\b)")
ROOMS_RE = re.compile(r"((?:\d+)-комн\.?|студия|студии)", re.IGNORECASE)
_CAPTCHA_MESSAGE = (
    "CIAN returned a captcha/access-check page instead of listings. "
    "The service cannot parse listings from this response."
)
_MAX_DEBUG_HTML_BYTES = 1_000_000


@dataclass(frozen=True, slots=True)
class ScraperConfig:
    search_url: str
    user_agent: str
    timeout_seconds: int
    limit: int
    debug_dir: Path | None = None
    proxy_server: str | None = None
    proxy_username: str | None = None
    proxy_password: str | None = None


class CianScraperError(RuntimeError):
    pass


class CaptchaError(CianScraperError):
    pass


class NetworkFetchError(CianScraperError):
    pass


class EmptyParseError(CianScraperError):
    pass


class CianScraper:
    def fetch(self) -> str:
        raise NotImplementedError

    def parse(self, html: str) -> list[Listing]:
        soup = BeautifulSoup(html, "html.parser")
        listings: dict[str, Listing] = {}

        for listing in self._from_cian_cards(soup):
            listings.setdefault(listing.cian_id, listing)

        for listing in self._from_json_ld(soup):
            listings.setdefault(listing.cian_id, listing)

        for listing in self._from_links(soup):
            existing = listings.get(listing.cian_id)
            if existing is None:
                listings[listing.cian_id] = listing
            else:
                listings[listing.cian_id] = _merge_listing(existing, listing)

        return list(listings.values())

    def _from_cian_cards(self, soup: BeautifulSoup) -> Iterable[Listing]:
        seen: set[str] = set()
        for card in soup.select("article[data-name='CardComponent']"):
            link = card.select_one("div[data-name='LinkArea'] a[href]")
            if link is None:
                continue
            href = str(link.get("href"))
            cian_id = _listing_id_from_url(href)
            if not cian_id or cian_id in seen:
                continue

            seen.add(cian_id)
            card_text = _compact_text(card.get_text(" ", strip=True))
            published = _extract_published_info(card_text)
            title = _first_text(
                card.select("div[data-name='GeneralInfoSectionRowComponent']"),
                fallback=_guess_title(_compact_text(link.get_text(" ", strip=True)), card_text),
            )
            yield Listing(
                cian_id=cian_id,
                url=_normalize_listing_url(href),
                title=title,
                price=_extract_cian_card_price(card) or _guess_price(card_text),
                address=_extract_cian_card_address(card) or _guess_address(card_text),
                rooms=_guess_rooms(title or card_text),
                raw={"source": "cian_card", **published},
            )

    def _from_json_ld(self, soup: BeautifulSoup) -> Iterable[Listing]:
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            text = script.string or script.get_text(strip=True)
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            yield from _walk_json_for_listings(payload)

    def _from_links(self, soup: BeautifulSoup) -> Iterable[Listing]:
        seen: set[str] = set()
        for anchor in soup.find_all("a", href=True):
            href = str(anchor.get("href"))
            cian_id = _listing_id_from_url(href)
            if not cian_id or cian_id in seen:
                continue

            url = _normalize_listing_url(href)
            card = _nearest_card(anchor)
            card_text = _compact_text(card.get_text(" ", strip=True) if card else "")
            anchor_text = _compact_text(anchor.get_text(" ", strip=True))
            text = card_text or anchor_text
            published = _extract_published_info(text)

            seen.add(cian_id)
            yield Listing(
                cian_id=cian_id,
                url=url,
                title=_guess_title(anchor_text, text),
                price=_guess_price(text),
                address=_guess_address(text),
                rooms=_guess_rooms(text),
                raw={"source": "html", **published},
            )


class RequestsCianScraper(CianScraper):
    def __init__(self, config: ScraperConfig) -> None:
        self.config = config
        self.session = cloudscraper.create_scraper(
            browser={
                "browser": "chrome",
                "platform": "darwin",
                "desktop": True,
            }
        )
        self.session.headers.update(
            {
                "User-Agent": config.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.7,en;q=0.5",
                "Cache-Control": "no-cache",
            }
        )
        proxy_url = _requests_proxy_url(config)
        if proxy_url is not None:
            self.session.proxies.update({"http": proxy_url, "https": proxy_url})

    def fetch(self) -> str:
        try:
            response = self.session.get(
                self.config.search_url,
                timeout=self.config.timeout_seconds,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            details = _redact_proxy_secrets(str(exc), self.config)
            raise NetworkFetchError(f"Failed to fetch CIAN search page: {details}") from exc
        if _looks_like_access_check(response.text):
            _save_debug_html(
                response.text,
                self.config.debug_dir,
                reason="captcha",
                search_url=self.config.search_url,
            )
            raise CaptchaError(_CAPTCHA_MESSAGE)
        return response.text


class PlaywrightCianScraper(CianScraper):
    def __init__(self, config: ScraperConfig, headless: bool = True) -> None:
        self.config = config
        self.headless = headless

    def fetch(self) -> str:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is not installed. Install with: pip install '.[playwright]' "
                "and then run: playwright install chromium"
            ) from exc

        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=self.headless,
                    proxy=_playwright_proxy(self.config),
                )
                page = browser.new_page(user_agent=self.config.user_agent, locale="ru-RU")
                page.goto(self.config.search_url, wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(5_000)
                html = page.content()
                browser.close()
        except Exception as exc:
            details = _redact_proxy_secrets(str(exc), self.config)
            raise NetworkFetchError(f"Failed to fetch CIAN with Playwright: {details}") from exc
        if _looks_like_access_check(html):
            _save_debug_html(
                html,
                self.config.debug_dir,
                reason="captcha",
                search_url=self.config.search_url,
            )
            raise CaptchaError(_CAPTCHA_MESSAGE)
        return html


def _requests_proxy_url(config: ScraperConfig) -> str | None:
    if not config.proxy_server:
        return None
    if not config.proxy_username and not config.proxy_password:
        return config.proxy_server

    parsed = urlsplit(config.proxy_server)
    if not parsed.scheme or not parsed.hostname:
        raise ValueError("CIAN_PROXY_SERVER must include a scheme and host")

    username = quote(config.proxy_username or "", safe="")
    password = quote(config.proxy_password or "", safe="")
    credentials = username
    if config.proxy_password is not None:
        credentials = f"{credentials}:{password}"

    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"

    return urlunsplit((parsed.scheme, f"{credentials}@{host}", parsed.path, parsed.query, ""))


def _playwright_proxy(config: ScraperConfig) -> dict[str, str] | None:
    if not config.proxy_server:
        return None

    proxy = {"server": config.proxy_server}
    if config.proxy_username:
        proxy["username"] = config.proxy_username
    if config.proxy_password:
        proxy["password"] = config.proxy_password
    return proxy


def _redact_proxy_secrets(message: str, config: ScraperConfig) -> str:
    redacted = message
    for secret in (config.proxy_username, config.proxy_password):
        if not secret:
            continue
        redacted = redacted.replace(secret, "***")
        redacted = redacted.replace(quote(secret, safe=""), "***")
    return redacted


def scrape(scraper: CianScraper, limit: int) -> list[Listing]:
    html = scraper.fetch()
    listings = scraper.parse(html)
    if not listings:
        if _looks_like_empty_results_page(html):
            logger.info("CIAN returned a valid empty listings page")
            return []
        config = getattr(scraper, "config", None)
        debug_dir = getattr(config, "debug_dir", None)
        search_url = getattr(config, "search_url", "unknown")
        _save_debug_html(html, debug_dir, reason="empty_parse", search_url=search_url)
        logger.warning("No listings found on the page")
        raise EmptyParseError("No listings found on the page")
    return listings[:limit]


def _looks_like_empty_results_page(html: str) -> bool:
    normalized = html.replace(r"\"", '"')
    has_serp_state = "frontend-serp" in normalized or '"pageType":"Listing"' in normalized
    has_empty_count = re.search(r'"offersQty"\s*:\s*0', normalized) is not None
    has_empty_products = re.search(r'"products"\s*:\s*\[\]', normalized) is not None
    return has_serp_state and (has_empty_count or has_empty_products)


def _walk_json_for_listings(payload: Any) -> Iterable[Listing]:
    if isinstance(payload, list):
        for item in payload:
            yield from _walk_json_for_listings(item)
        return

    if not isinstance(payload, dict):
        return

    if payload.get("@type") == "ItemList":
        for element in payload.get("itemListElement", []):
            item = element.get("item", element) if isinstance(element, dict) else element
            yield from _walk_json_for_listings(item)
        return

    url = payload.get("url") or payload.get("@id")
    if isinstance(url, str):
        cian_id = _listing_id_from_url(url)
        if cian_id:
            offers = payload.get("offers") if isinstance(payload.get("offers"), dict) else {}
            address = payload.get("address")
            published = _published_info_from_json(payload)
            if isinstance(address, dict):
                address = address.get("streetAddress") or address.get("addressLocality")
            yield Listing(
                cian_id=cian_id,
                url=_normalize_listing_url(url),
                title=payload.get("name") or payload.get("description"),
                price=_parse_int(offers.get("price")) if offers else None,
                address=address if isinstance(address, str) else None,
                rooms=_guess_rooms(str(payload.get("name", ""))),
                raw={"source": "json_ld", **published},
            )

    for value in payload.values():
        if isinstance(value, (dict, list)):
            yield from _walk_json_for_listings(value)


def _listing_id_from_url(url: str) -> str | None:
    match = LISTING_URL_RE.search(url) or RELATIVE_LISTING_URL_RE.search(url)
    if not match:
        return None
    return match.group(2)


def _normalize_listing_url(url: str) -> str:
    absolute = urljoin("https://www.cian.ru", url)
    match = LISTING_URL_RE.search(absolute)
    if match:
        return f"https://www.cian.ru/{match.group(1)}/flat/{match.group(2)}/"
    return absolute


def _nearest_card(anchor: Tag) -> Tag | None:
    current: Tag | None = anchor
    best: Tag | None = None
    for _ in range(7):
        if current is None:
            break
        text = current.get_text(" ", strip=True)
        if "₽" in text or "руб" in text.lower():
            best = current
        current = current.parent if isinstance(current.parent, Tag) else None
    return best


def _compact_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _guess_title(anchor_text: str, card_text: str) -> str | None:
    for value in (anchor_text, card_text):
        if not value:
            continue
        rooms = _guess_rooms(value)
        if rooms:
            return rooms
        if len(value) <= 120:
            return value
    return None


def _first_text(elements: list[Tag], fallback: str | None = None) -> str | None:
    for element in elements:
        text = _compact_text(element.get_text(" ", strip=True))
        if text:
            return text[:180]
    return fallback


def _extract_cian_card_price(card: Tag) -> int | None:
    price = card.select_one("span[data-mark='MainPrice']")
    if price is None:
        return None
    return _guess_price(price.get_text(" ", strip=True))


def _extract_cian_card_address(card: Tag) -> str | None:
    geo_labels = [
        _compact_text(label.get_text(" ", strip=True))
        for label in card.select("a[data-name='GeoLabel']")
    ]
    if geo_labels:
        return ", ".join(label for label in geo_labels if label)[:240]

    rows = card.select("div[data-name='GeneralInfoSectionRowComponent']")
    if len(rows) > 1:
        return _compact_text(rows[1].get_text(" ", strip=True))[:240]
    return None


def _guess_price(text: str) -> int | None:
    match = PRICE_RE.search(text)
    if not match:
        return None
    return _parse_int(match.group(1))


def _guess_address(text: str) -> str | None:
    parts = [part.strip(" ,;") for part in re.split(r"\s{2,}| · | \| ", text) if part.strip()]
    for part in parts:
        lowered = part.lower()
        if any(
            marker in lowered
            for marker in ("ул.", "улица", "проспект", "пр-т", "шоссе", "мкр", "метро")
        ):
            return part[:240]
    return None


def _guess_rooms(text: str) -> str | None:
    match = ROOMS_RE.search(text)
    if not match:
        return None
    return match.group(1)


def _parse_int(value: Any) -> int | None:
    if value is None:
        return None
    digits = re.sub(r"\D+", "", str(value))
    return int(digits) if digits else None


def _published_info_from_json(payload: dict[str, Any]) -> dict[str, str]:
    value = payload.get("datePublished") or payload.get("datePosted") or payload.get("dateCreated")
    if not isinstance(value, str) or not value.strip():
        return {}
    return {"published_at": value[:10], "published_label": value}


def _extract_published_info(text: str) -> dict[str, str]:
    lowered = text.lower()
    today = date.today()

    if "сегодня" in lowered:
        return _published_info(today, "сегодня")
    if "позавчера" in lowered:
        return _published_info(today - timedelta(days=2), "позавчера")
    if "вчера" in lowered:
        return _published_info(today - timedelta(days=1), "вчера")

    match = re.search(r"(\d+)\s*(?:день|дня|дней)\s+назад", lowered)
    if match:
        days = int(match.group(1))
        return _published_info(today - timedelta(days=days), match.group(0))

    if re.search(r"(\d+)\s*(?:час|часа|часов|минут[а-я]*)\s+назад", lowered):
        return _published_info(today, "сегодня")

    return {}


def _published_info(value: date, label: str) -> dict[str, str]:
    return {"published_at": value.isoformat(), "published_label": label}


def _merge_listing(primary: Listing, secondary: Listing) -> Listing:
    return Listing(
        cian_id=primary.cian_id,
        url=primary.url or secondary.url,
        title=primary.title or secondary.title,
        price=primary.price if primary.price is not None else secondary.price,
        address=primary.address or secondary.address,
        rooms=primary.rooms or secondary.rooms,
        raw={**secondary.raw, **primary.raw},
    )


def _save_debug_html(
    html: str,
    debug_dir: Path | None,
    *,
    reason: str,
    search_url: str,
) -> Path | None:
    if debug_dir is None:
        return None

    debug_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
    filename = f"{timestamp}_{_safe_filename_part(reason)}.html"
    path = debug_dir / filename
    header = f"<!-- reason={reason} url={search_url} -->\n"
    payload = (header + html)[:_MAX_DEBUG_HTML_BYTES]
    path.write_text(payload, encoding="utf-8")
    logger.info("Saved CIAN debug HTML to %s", path)
    return path


def _safe_filename_part(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "_", value.strip())
    return normalized.strip("_") or "debug"


def _looks_like_access_check(html: str) -> bool:
    lowered = html.lower()
    markers = (
        "<title>captcha",
        "captcha",
        "проверка доступа",
        "подтвердите, что вы не робот",
    )
    return any(marker in lowered for marker in markers)
