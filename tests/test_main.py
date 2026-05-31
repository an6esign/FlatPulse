import pytest
import logging

from cian_rent_alerts.config import ConfigError, Settings
from cian_rent_alerts.main import (
    _ensure_event_loop,
    _parse_log_level,
    configure_logging,
    parse_args,
    run_healthcheck,
    run_parser_smoke,
)


def test_ensure_event_loop_is_idempotent() -> None:
    _ensure_event_loop()
    _ensure_event_loop()


def test_parse_worker_only_mode() -> None:
    args = parse_args(["--worker-only"])

    assert args.worker_only is True
    assert args.bot_only is False


def test_parse_parser_smoke_mode() -> None:
    args = parse_args(["--parser-smoke"])

    assert args.parser_smoke is True
    assert args.once is False


def test_parse_healthcheck_mode() -> None:
    args = parse_args(["--healthcheck"])

    assert args.healthcheck is True
    assert args.once is False


def test_parse_rejects_multiple_modes() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--worker-only", "--healthcheck"])


def test_parse_log_level_accepts_known_levels() -> None:
    assert _parse_log_level("debug") == logging.DEBUG
    assert _parse_log_level("INFO") == logging.INFO


def test_parse_log_level_rejects_unknown_level() -> None:
    with pytest.raises(ConfigError):
        _parse_log_level("chatty")


def test_configure_logging_verbose_overrides_log_level() -> None:
    configure_logging(verbose=True, log_level="ERROR")

    assert logging.getLogger().level == logging.DEBUG
    assert logging.getLogger("httpx").level == logging.WARNING


def test_run_parser_smoke_returns_success(monkeypatch) -> None:
    settings = Settings.from_env(env_file=None)
    seen_limits: list[int] = []

    monkeypatch.setattr("cian_rent_alerts.main.build_search_url", lambda _settings: "https://cian")

    def fake_fetch_listings(smoke_settings):
        seen_limits.append(smoke_settings.listing_limit)
        return [object()]

    monkeypatch.setattr("cian_rent_alerts.main.fetch_listings", fake_fetch_listings)

    assert run_parser_smoke(settings) == 0
    assert seen_limits == [3]


def test_run_parser_smoke_returns_failure(monkeypatch) -> None:
    settings = Settings.from_env(env_file=None)

    monkeypatch.setattr("cian_rent_alerts.main.build_search_url", lambda _settings: "https://cian")
    monkeypatch.setattr(
        "cian_rent_alerts.main.fetch_listings",
        lambda _settings: (_ for _ in ()).throw(RuntimeError("captcha")),
    )

    assert run_parser_smoke(settings) == 2


def test_run_healthcheck_returns_success(monkeypatch) -> None:
    settings = Settings.from_env(env_file=None)

    class FakeStore:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def ping(self) -> None:
            pass

        def schema_version(self) -> str:
            return "20260531_0003"

    monkeypatch.setattr("cian_rent_alerts.main.ListingStore", FakeStore)

    assert run_healthcheck(settings) == 0


def test_run_healthcheck_fails_without_schema_version(monkeypatch) -> None:
    settings = Settings.from_env(env_file=None)

    class FakeStore:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def ping(self) -> None:
            pass

        def schema_version(self) -> None:
            return None

    monkeypatch.setattr("cian_rent_alerts.main.ListingStore", FakeStore)

    assert run_healthcheck(settings) == 2
