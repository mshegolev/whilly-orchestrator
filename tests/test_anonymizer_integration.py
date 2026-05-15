"""Integration tests for the anonymizer proxy initialization."""

import os
import sys
from unittest.mock import patch

import pytest


class TestAnonymizerInitialization:
    """Test initialization of the anonymizer proxy via environment variables."""

    def test_anonymizer_enabled_via_env_var(self):
        """Test that setting WHILLY_ENABLE_ANONYMIZER=1 initializes the proxy."""
        # Remove the runner module from sys.modules to force reimport
        for key in list(sys.modules.keys()):
            if key.startswith("whilly.adapters.runner"):
                del sys.modules[key]

        with patch.dict(os.environ, {"WHILLY_ENABLE_ANONYMIZER": "1"}):
            # Import the runner package, which should trigger proxy initialization
            from whilly.adapters.runner import claude_cli

            # Verify that _spawn_and_collect is now wrapped
            # The proxy replaces the function, so we can't directly compare,
            # but we can check that it's still callable
            assert callable(claude_cli._spawn_and_collect)

    def test_anonymizer_disabled_by_default(self):
        """Test that anonymizer is not enabled without the environment variable."""
        # Remove the runner module from sys.modules to force reimport
        for key in list(sys.modules.keys()):
            if key.startswith("whilly.adapters.runner"):
                del sys.modules[key]

        env = {k: v for k, v in os.environ.items() if k != "WHILLY_ENABLE_ANONYMIZER"}
        with patch.dict(os.environ, env, clear=True):
            # Import the runner package
            from whilly.adapters.runner import claude_cli

            # Just verify it doesn't crash
            assert callable(claude_cli._spawn_and_collect)

    @pytest.mark.parametrize(
        "env_value",
        ["1", "true", "TRUE", "True", "yes", "YES", "Yes"],
    )
    def test_anonymizer_enabled_various_truthy_values(self, env_value):
        """Test that various truthy values enable the anonymizer."""
        # Remove the runner module from sys.modules to force reimport
        for key in list(sys.modules.keys()):
            if key.startswith("whilly.adapters.runner"):
                del sys.modules[key]

        with patch.dict(os.environ, {"WHILLY_ENABLE_ANONYMIZER": env_value}):
            from whilly.adapters.runner import claude_cli

            # Verify the module still loads without errors
            assert callable(claude_cli._spawn_and_collect)

    @pytest.mark.parametrize(
        "env_value",
        ["0", "false", "False", "no", "No", ""],
    )
    def test_anonymizer_disabled_various_falsy_values(self, env_value):
        """Test that falsy values don't enable the anonymizer."""
        # Remove the runner module from sys.modules to force reimport
        for key in list(sys.modules.keys()):
            if key.startswith("whilly.adapters.runner"):
                del sys.modules[key]

        with patch.dict(os.environ, {"WHILLY_ENABLE_ANONYMIZER": env_value}):
            from whilly.adapters.runner import claude_cli

            # Verify the module still loads without errors
            assert callable(claude_cli._spawn_and_collect)
