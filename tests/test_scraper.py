from datetime import date, timedelta

import pytest

from cian_rent_alerts.scraper import (
    CaptchaError,
    EmptyParseError,
    RequestsCianScraper,
    ScraperConfig,
    scrape,
)
from cian_rent_alerts.cian_url import build_cian_search_url, extract_polygon
from cian_rent_alerts.geo import build_radius_polygon
from cian_rent_alerts.bot import _parse_radius_args
from cian_rent_alerts.service import filter_recent_listings
from cian_rent_alerts.models import Listing


def test_build_cian_search_url() -> None:
    url = build_cian_search_url(
        city="Казань",
        region_id=None,
        rooms=("1", "2"),
        min_price=35000,
        max_price=45000,
        rent_type="long",
        sort_by="creation_date_from_newer_to_older",
        polygon="49.1_55.7,49.2_55.7,49.2_55.8",
    )

    assert url.startswith("https://kazan.cian.ru/cat.php?")
    assert "cat.php" in url
    assert "region=4777" in url
    assert "deal_type=rent" in url
    assert "type=4" in url
    assert "room1=1" in url
    assert "room2=1" in url
    assert "minprice=35000" in url
    assert "maxprice=45000" in url
    assert "in_polygon%5B0%5D=49.1_55.7%2C49.2_55.7%2C49.2_55.8" in url


def test_build_cian_search_url_with_any_rent_type() -> None:
    url = build_cian_search_url(
        city="Казань",
        region_id=None,
        rooms=("all",),
        min_price=None,
        max_price=None,
        rent_type="all",
        sort_by="creation_date_from_newer_to_older",
    )

    assert "type=4" not in url
    assert "type=2" not in url
    assert "room1=1" not in url


def test_build_cian_search_url_uses_moscow_host() -> None:
    url = build_cian_search_url(
        city="Москва",
        region_id=None,
        rooms=("all",),
        min_price=None,
        max_price=None,
        rent_type="all",
        sort_by="default",
    )

    assert url.startswith("https://www.cian.ru/cat.php?")


def test_extract_polygon_from_cian_url() -> None:
    polygon = extract_polygon(
        "https://kazan.cian.ru/map/?in_polygon[0]=49.1_55.7%2C49.2_55.7%2C49.2_55.8"
    )

    assert polygon == "49.1_55.7,49.2_55.7,49.2_55.8"


def test_build_radius_polygon() -> None:
    polygon = build_radius_polygon(latitude=55.796127, longitude=49.106405, radius_meters=1000)
    points = polygon.split(",")

    assert len(points) == 24
    assert all("_" in point for point in points)


def test_parse_radius_args() -> None:
    assert _parse_radius_args(["1000", "Казань,", "Кремлевская", "18"]) == (
        1000,
        "Казань, Кремлевская 18",
    )
    assert _parse_radius_args(["Казань,", "Кремлевская", "18", "|", "1500"]) == (
        1500,
        "Казань, Кремлевская 18",
    )


def test_parse_listing_from_html_link() -> None:
    html = """
    <html>
      <body>
        <article>
          <a href="https://www.cian.ru/rent/flat/123456789/">2-комн. квартира</a>
          <span>85 000 ₽/мес.</span>
          <span>Москва, ул. Тверская, 1</span>
        </article>
      </body>
    </html>
    """
    scraper = RequestsCianScraper(
        ScraperConfig(
            search_url="https://www.cian.ru",
            user_agent="test",
            timeout_seconds=1,
            limit=10,
        )
    )

    listings = scraper.parse(html)

    assert len(listings) == 1
    assert listings[0].cian_id == "123456789"
    assert listings[0].price == 85000
    assert listings[0].rooms == "2-комн."


def test_scrape_raises_empty_parse_and_saves_debug_html(tmp_path) -> None:
    class EmptyScraper:
        config = ScraperConfig(
            search_url="https://www.cian.ru/cat.php",
            user_agent="test",
            timeout_seconds=1,
            limit=10,
            debug_dir=tmp_path,
        )

        def fetch(self) -> str:
            return "<html><body>normal page</body></html>"

        def parse(self, _html: str):
            return []

    with pytest.raises(EmptyParseError):
        scrape(EmptyScraper(), limit=10)

    debug_files = list(tmp_path.glob("*_empty_parse.html"))
    assert len(debug_files) == 1
    assert "normal page" in debug_files[0].read_text(encoding="utf-8")


def test_scrape_returns_empty_list_for_valid_empty_results_page(tmp_path) -> None:
    class EmptyResultsScraper:
        config = ScraperConfig(
            search_url="https://www.cian.ru/cat.php",
            user_agent="test",
            timeout_seconds=1,
            limit=10,
            debug_dir=tmp_path,
        )

        def fetch(self) -> str:
            return """
            <html>
              <body>
                <script>
                  window.__state = {
                    "application": "frontend-serp",
                    "pageType": "Listing",
                    "offersQty": 0,
                    "products": []
                  };
                </script>
              </body>
            </html>
            """

        def parse(self, _html: str):
            return []

    assert scrape(EmptyResultsScraper(), limit=10) == []
    assert list(tmp_path.glob("*.html")) == []


def test_access_check_raises_captcha_and_saves_debug_html(tmp_path) -> None:
    class Response:
        text = "<html><title>captcha</title></html>"

        def raise_for_status(self) -> None:
            return None

    scraper = RequestsCianScraper(
        ScraperConfig(
            search_url="https://www.cian.ru",
            user_agent="test",
            timeout_seconds=1,
            limit=10,
            debug_dir=tmp_path,
        )
    )
    scraper.session.get = lambda *_args, **_kwargs: Response()

    with pytest.raises(CaptchaError):
        scraper.fetch()

    debug_files = list(tmp_path.glob("*_captcha.html"))
    assert len(debug_files) == 1
    assert "captcha" in debug_files[0].read_text(encoding="utf-8")


def test_parse_listing_from_cian_card() -> None:
    html = """
    <html>
      <body>
        <article data-name="CardComponent">
          <div data-name="LinkArea">
            <a href="https://kazan.cian.ru/rent/flat/987654321/">1-комн. квартира</a>
            <div data-name="GeneralInfoSectionRowComponent">1-комн. квартира, 38 м², 4/9 этаж</div>
            <div data-name="GeneralInfoSectionRowComponent">
              <a data-name="GeoLabel">Казань</a>
              <a data-name="GeoLabel">ул. Пушкина</a>
              <a data-name="GeoLabel">10</a>
            </div>
            <span data-mark="MainPrice">42 000 ₽/мес.</span>
          </div>
        </article>
      </body>
    </html>
    """
    scraper = RequestsCianScraper(
        ScraperConfig(
            search_url="https://www.cian.ru",
            user_agent="test",
            timeout_seconds=1,
            limit=10,
        )
    )

    listings = scraper.parse(html)

    assert len(listings) == 1
    assert listings[0].cian_id == "987654321"
    assert listings[0].url == "https://www.cian.ru/rent/flat/987654321/"
    assert listings[0].price == 42000
    assert listings[0].address == "Казань, ул. Пушкина, 10"


def test_parse_listing_publish_label_from_cian_card() -> None:
    html = """
    <html>
      <body>
        <article data-name="CardComponent">
          <div data-name="LinkArea">
            <a href="https://kazan.cian.ru/rent/flat/987654321/">1-комн. квартира</a>
            <div data-name="GeneralInfoSectionRowComponent">1-комн. квартира, 38 м²</div>
            <span data-mark="MainPrice">42 000 ₽/мес.</span>
            <span>вчера</span>
          </div>
        </article>
      </body>
    </html>
    """
    scraper = RequestsCianScraper(
        ScraperConfig(
            search_url="https://www.cian.ru",
            user_agent="test",
            timeout_seconds=1,
            limit=10,
        )
    )

    listings = scraper.parse(html)

    assert listings[0].raw["published_label"] == "вчера"


def test_filter_recent_listings_by_known_publish_date() -> None:
    today = date.today()
    recent = Listing(
        cian_id="1",
        url="https://www.cian.ru/rent/flat/1/",
        raw={"published_at": today.isoformat()},
    )
    old = Listing(
        cian_id="2",
        url="https://www.cian.ru/rent/flat/2/",
        raw={"published_at": (today - timedelta(days=10)).isoformat()},
    )
    unknown = Listing(cian_id="3", url="https://www.cian.ru/rent/flat/3/")

    assert filter_recent_listings([recent, old, unknown], max_age_days=2) == [recent, unknown]
