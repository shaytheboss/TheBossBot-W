"""Third-party request loggers must stay quiet.

httpx logs EVERY HTTP call at INFO with the full URL (Polymarket token_ids are
78 chars). At ~1,600 calls per 5-minute cycle that is ~460,000 lines / ~70 MB
per day, which both costs resources and buries the bot's own log messages.

These tests assert the intent declaratively so the silencing cannot be dropped
by accident.
"""
import logging
import re
import pathlib

MAIN = pathlib.Path(__file__).resolve().parents[2] / "app" / "main.py"

NOISY = ("httpx", "httpcore", "urllib3")


def test_main_silences_noisy_request_loggers():
    src = MAIN.read_text()
    block = re.search(r"for _noisy in \(([^)]*)\)", src)
    assert block, "main.py must silence third-party request loggers"
    named = block.group(1)
    for name in NOISY:
        assert f'"{name}"' in named, f"{name} must be quietened"


def test_silencing_uses_warning_not_disable():
    """Warnings and errors must still surface — we quieten, never disable."""
    src = MAIN.read_text()
    assert "setLevel(logging.WARNING)" in src
    assert "logging.disable(" not in src


def test_applying_the_config_suppresses_info_but_keeps_warnings(caplog):
    for name in NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)

    log = logging.getLogger("httpx")
    with caplog.at_level(logging.DEBUG):
        log.info("HTTP Request: GET https://clob.polymarket.com/midpoint?token_id=" + "9" * 78)
        log.warning("real problem")

    messages = [r.message for r in caplog.records if r.name == "httpx"]
    assert not any("midpoint" in m for m in messages), "per-request INFO must be dropped"
    assert any("real problem" in m for m in messages), "warnings must still appear"


def test_app_logger_is_unaffected():
    """Only third-party loggers are quietened; the bot's own INFO stays.

    Reproduce main.py's setup: root at INFO, then silence the noisy ones.
    """
    root = logging.getLogger()
    prev = root.level
    try:
        root.setLevel(logging.INFO)          # what basicConfig(level=INFO) does
        for name in NOISY:
            logging.getLogger(name).setLevel(logging.WARNING)

        app_log = logging.getLogger("app.collectors.polymarket_collector")
        assert app_log.getEffectiveLevel() <= logging.INFO, (
            "the bot's own INFO logging must survive the silencing"
        )
        assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
    finally:
        root.setLevel(prev)
