"""Unit tests for :func:`whilly.adapters.notifications.factory.make_notifier`.

The factory is the single decision point between :class:`SlackNotifier`
and :class:`NullNotifier`. Tests pin the four boundary conditions so a
config rename or a logic flip is loud at test time.
"""

from __future__ import annotations

import logging

from whilly.adapters.notifications import (
    NullNotifier,
    SlackNotifier,
    make_notifier,
    urllib_post,
)
from whilly.config import WhillyConfig


def _enabled_cfg() -> WhillyConfig:
    cfg = WhillyConfig()
    cfg.SLACK_ACCESS_TOKEN = "xoxe.xoxp-test"
    cfg.SLACK_CHANNEL = "C0B1WT58EBE"
    cfg.SLACK_ENABLED = True
    cfg.SLACK_API_BASE_URL = "https://slack.test/api"
    cfg.SLACK_TIMEOUT_S = 3.0
    cfg.SLACK_MESSAGE_TEMPLATE = "done {plan_id}"
    return cfg


def test_missing_token_yields_null_notifier() -> None:
    cfg = _enabled_cfg()
    cfg.SLACK_ACCESS_TOKEN = ""
    assert isinstance(make_notifier(cfg), NullNotifier)


def test_missing_channel_yields_null_notifier() -> None:
    cfg = _enabled_cfg()
    cfg.SLACK_CHANNEL = ""
    assert isinstance(make_notifier(cfg), NullNotifier)


def test_explicit_disabled_overrides_token_and_channel() -> None:
    cfg = _enabled_cfg()
    cfg.SLACK_ENABLED = False
    assert isinstance(make_notifier(cfg), NullNotifier)


def test_full_config_yields_slack_notifier_with_values_plumbed_in() -> None:
    cfg = _enabled_cfg()
    notifier = make_notifier(cfg)
    assert isinstance(notifier, SlackNotifier)
    assert notifier.token == "xoxe.xoxp-test"
    assert notifier.channel == "C0B1WT58EBE"
    assert notifier.api_base_url == "https://slack.test/api"
    assert notifier.timeout_s == 3.0
    assert notifier.template.template == "done {plan_id}"
    # Default http_post is the urllib-backed module-level function — the
    # adapter never imports a third-party HTTP lib.
    assert notifier.http_post is urllib_post


def test_default_logger_is_named_whilly() -> None:
    notifier = make_notifier(_enabled_cfg())
    assert isinstance(notifier, SlackNotifier)
    assert notifier.logger.name == "whilly"


def test_explicit_logger_is_propagated() -> None:
    custom = logging.getLogger("whilly.test.factory")
    notifier = make_notifier(_enabled_cfg(), logger=custom)
    assert isinstance(notifier, SlackNotifier)
    assert notifier.logger is custom
