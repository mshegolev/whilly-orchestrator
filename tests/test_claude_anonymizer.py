"""Tests for Claude API anonymization proxy and anonymizer module."""

import logging

import pytest

from whilly.adapters.runner.anonymizer import Anonymizer
from whilly.adapters.runner.claude_anonymizer_proxy import (
    ClaudeAnonymizerProxy,
)
from whilly.adapters.runner.result_parser import AgentResult


# ---------------------------------------------------------------------------
# Helper: build an Anonymizer pre-loaded with a fictional company mapping.
# The default map is now EMPTY (loaded from WHILLY_ANONYMIZER_MAP); tests
# that need redaction must supply an explicit map via this helper.
# ---------------------------------------------------------------------------


def _anon(**kw) -> Anonymizer:
    """Return an Anonymizer with fictional Globex→Acme mappings."""
    return Anonymizer(
        company_mappings={"globex": "Acme", "GLOBEX": "Acme", "Globex": "Acme"},
        **kw,
    )


class TestAnonymizer:
    """Test anonymizer.Anonymizer basic operations."""

    def test_anonymize_text_company_name(self):
        """Test company name anonymization."""
        anon = _anon()
        text = "I am from globex, write its name in uppercase"

        anonymized, mapping = anon.anonymize_text(text)

        assert "globex" not in anonymized
        assert "Acme" in anonymized
        assert mapping == {"globex": "Acme"}
        assert anonymized == "I am from Acme, write its name in uppercase"

    def test_anonymize_text_multiple_occurrences(self):
        """Test that all occurrences are replaced."""
        anon = _anon()
        text = "globex is great, globex rocks, use globex"

        anonymized, mapping = anon.anonymize_text(text)

        assert anonymized == "Acme is great, Acme rocks, use Acme"
        assert mapping == {"globex": "Acme"}

    def test_anonymize_text_case_sensitive(self):
        """Test that replacement is case-sensitive."""
        anon = _anon()
        text = "GLOBEX and globex and Globex"

        anonymized, mapping = anon.anonymize_text(text)

        assert "GLOBEX" not in anonymized
        assert "globex" not in anonymized
        assert "Globex" not in anonymized
        # All three variants in the mapping
        assert len(mapping) == 3

    def test_deanonymize_text(self):
        """Test reversing anonymization."""
        anon = _anon()
        original = "I am from globex, what should I do"
        anonymized, mapping = anon.anonymize_text(original)

        restored = anon.deanonymize_text(anonymized, mapping)

        assert restored == original

    def test_deanonymize_with_default_mapping(self):
        """Test deanonymize without explicit mapping uses internal reverse mapping."""
        anon = _anon()
        anonymized = "Company Acme is great"

        restored = anon.deanonymize_text(anonymized)

        assert "globex" in restored.lower()

    def test_anonymize_json_object(self):
        """Test anonymization of JSON objects."""
        anon = _anon()
        obj = {
            "company": "globex",
            "description": "I work at globex",
            "nested": {"name": "Globex"},
        }

        anon_obj, mapping = anon.anonymize_json(obj)

        assert anon_obj["company"] == "Acme"
        assert "globex" not in anon_obj["description"]
        assert "Acme" in anon_obj["description"]
        assert anon_obj["nested"]["name"] == "Acme"

    def test_anonymize_json_list(self):
        """Test anonymization of JSON arrays."""
        anon = _anon()
        arr = ["I work at globex", "GLOBEX is great", {"company": "Globex"}]

        anon_arr, mapping = anon.anonymize_json(arr)

        assert "globex" not in str(anon_arr).lower().replace("acme", "")
        assert all("Acme" in str(item) for item in anon_arr)

    def test_deanonymize_json(self):
        """Test deanonymizing JSON objects."""
        anon = _anon()
        original = {"company": "globex", "message": "Work at globex"}
        anon_obj, mapping = anon.anonymize_json(original)

        restored = anon.deanonymize_json(anon_obj, mapping)

        assert restored == original

    def test_default_map_empty_without_env(self, monkeypatch):
        """Default Anonymizer has an empty map when WHILLY_ANONYMIZER_MAP is unset."""
        monkeypatch.delenv("WHILLY_ANONYMIZER_MAP", raising=False)

        anon = Anonymizer()

        assert anon.company_mappings == {}
        text, mapping = anon.anonymize_text("globex")
        assert text == "globex"
        assert mapping == {}

    def test_map_loaded_from_env(self, monkeypatch):
        """WHILLY_ANONYMIZER_MAP is parsed and used for redaction."""
        monkeypatch.setenv("WHILLY_ANONYMIZER_MAP", '{"globex": "Acme"}')

        anon = Anonymizer()

        assert anon.company_mappings == {"globex": "Acme"}
        text, mapping = anon.anonymize_text("I work at globex")
        assert text == "I work at Acme"
        assert mapping == {"globex": "Acme"}


class TestClaudeAnonymizerProxy:
    """Test the proxy layer."""

    @pytest.mark.asyncio
    async def test_proxy_anonymizes_and_deanonymizes(self):
        """Test full cycle: prompt anonymized, response deanonymized."""
        # Mock the original spawn_and_collect
        mock_result = AgentResult(
            output="Response from Acme",
            exit_code=0,
        )

        async def mock_spawn(prompt: str, model: str, *, cwd=None):
            # Verify that the prompt received is anonymized
            assert "Acme" in prompt
            assert "globex" not in prompt.lower()
            return mock_result

        proxy = ClaudeAnonymizerProxy(anonymizer=_anon())
        proxy._original_spawn_and_collect = mock_spawn

        original_prompt = "I am from globex, what should I do?"
        result = await proxy.spawn_and_collect_anonymized(original_prompt, "claude-opus-4-6")

        # Verify response is deanonymized
        assert "globex" in result.output.lower()
        assert result.output == "Response from globex"

    @pytest.mark.asyncio
    async def test_proxy_preserves_unchanged_content(self):
        """Test that content without sensitive data passes through unchanged."""
        mock_result = AgentResult(
            output="No sensitive data here",
            exit_code=0,
        )

        async def mock_spawn(prompt: str, model: str, *, cwd=None):
            assert prompt == "Plain text prompt"
            return mock_result

        proxy = ClaudeAnonymizerProxy(anonymizer=_anon())
        proxy._original_spawn_and_collect = mock_spawn

        result = await proxy.spawn_and_collect_anonymized("Plain text prompt", "claude-opus-4-6")

        assert result.output == "No sensitive data here"

    @pytest.mark.asyncio
    async def test_proxy_handles_empty_output(self):
        """Test that empty output doesn't crash."""
        mock_result = AgentResult(output="", exit_code=0)

        async def mock_spawn(prompt: str, model: str, *, cwd=None):
            return mock_result

        proxy = ClaudeAnonymizerProxy(anonymizer=_anon())
        proxy._original_spawn_and_collect = mock_spawn

        result = await proxy.spawn_and_collect_anonymized("I am from globex", "claude-opus-4-6")

        assert result.output == ""

    @pytest.mark.asyncio
    async def test_proxy_preserves_exit_code(self):
        """Test that exit code is preserved through anonymization."""
        mock_result = AgentResult(
            output="Error from Acme",
            exit_code=1,
        )

        async def mock_spawn(prompt: str, model: str, *, cwd=None):
            return mock_result

        proxy = ClaudeAnonymizerProxy(anonymizer=_anon())
        proxy._original_spawn_and_collect = mock_spawn

        result = await proxy.spawn_and_collect_anonymized("I am from globex", "claude-opus-4-6")

        assert result.exit_code == 1


class TestAcceptanceCriteria:
    """Test the specific acceptance criteria for the anonymization proxy.

    Scenario:
    - User sends:   "I am from globex, write its name in uppercase"
    - API receives: "I am from Acme, write its name in uppercase"
    - Claude responds with: "Acme"
    - User receives back: "globex" (deanonymized)
    - Logs show the anonymized version (Acme)
    """

    @pytest.mark.asyncio
    async def test_acceptance_criteria_end_to_end(self, caplog):
        """Test the exact acceptance criteria scenario.

        When user sends a message with their company name (in any case variant),
        it should be anonymized before sending to Claude, and deanonymized back
        using the same variant that was sent.
        """
        user_input = "I am from globex, write its name in uppercase"
        claude_response = "Acme"

        async def mock_claude(prompt: str, model: str, *, cwd=None):
            assert "I am from Acme" in prompt
            assert "globex" not in prompt.lower()
            return AgentResult(output=claude_response, exit_code=0)

        proxy = ClaudeAnonymizerProxy(anonymizer=_anon())
        proxy._original_spawn_and_collect = mock_claude

        with caplog.at_level(logging.INFO):
            result = await proxy.spawn_and_collect_anonymized(user_input, "claude-opus-4-6")

        assert result.output == "globex"

        log_text = caplog.text
        assert "Acme" in log_text or "anonymized" in log_text.lower()

    @pytest.mark.asyncio
    async def test_acceptance_criteria_logging(self, caplog):
        """Verify that logs show anonymized data while API uses anonymized prompt."""
        user_input = "Company: globex"
        claude_response = "You work at Acme"

        call_count = 0

        async def mock_claude(prompt: str, model: str, *, cwd=None):
            nonlocal call_count
            call_count += 1
            assert "Acme" in prompt
            assert "globex" not in prompt.lower()
            return AgentResult(output=claude_response, exit_code=0)

        proxy = ClaudeAnonymizerProxy(anonymizer=_anon())
        proxy._original_spawn_and_collect = mock_claude

        with caplog.at_level(logging.DEBUG):
            result = await proxy.spawn_and_collect_anonymized(user_input, "claude-opus-4-6")

        assert call_count == 1
        assert result.output == "You work at globex"

        assert any("anonymized" in record.message.lower() for record in caplog.records), (
            f"Expected anonymization message in logs, got: {caplog.text}"
        )

    @pytest.mark.asyncio
    async def test_demo_end_to_end(self, caplog):
        """End-to-end demonstration matching the canonical proxy scenario.

        Scenario:
            User runs:    proxy with 'I am from Globex, what is the company name?'
            User sees:    "Globex" (deanonymized — canonical form restored)
            Logs show:    outbound prompt to Anthropic contained "Acme"
                          inbound response from Anthropic contained "Acme" (raw)
            Anthropic:    only ever saw "Acme", never "Globex" or "globex"

        This test wires up the proxy with a canonical_form so the deanonymized
        output is the exact variant requested, and asserts on caplog records so
        the audit trail is verified — not just stdout-printed.
        """
        anon = Anonymizer(
            company_mappings={
                "globex": "Acme",
                "GLOBEX": "Acme",
                "Globex": "Acme",
            },
            canonical_form="Globex",
        )

        user_prompt = "I am from globex, what is the company name?"

        captured_prompt_to_api = {}

        async def mock_anthropic(prompt: str, model: str, *, cwd=None):
            captured_prompt_to_api["value"] = prompt
            assert "globex" not in prompt.lower(), f"PII LEAK: raw 'globex' reached Anthropic API: {prompt!r}"
            assert "Acme" in prompt, f"anonymization did not produce Acme placeholder: {prompt!r}"
            return AgentResult(
                output="The company is called Acme.",
                exit_code=0,
            )

        proxy = ClaudeAnonymizerProxy(anonymizer=anon)
        proxy._original_spawn_and_collect = mock_anthropic

        with caplog.at_level(logging.INFO, logger="whilly.claude_anonymizer_proxy"):
            result = await proxy.spawn_and_collect_anonymized(user_prompt, "claude-opus-4-6")

        # Invariant 1: what Anthropic received was anonymized
        outbound = captured_prompt_to_api["value"]
        assert outbound == "I am from Acme, what is the company name?"

        # Invariant 2: user-visible output has canonical form, no placeholder
        assert "Globex" in result.output, f"expected deanonymized 'Globex' in user output, got: {result.output!r}"
        assert "Acme" not in result.output, f"deanonymizer leaked 'Acme' to user: {result.output!r}"
        assert result.output == "The company is called Globex."

        # Invariant 3: audit-trail logs prove both directions were anonymized
        log_text = caplog.text
        assert "anonymized prompt sent to API" in log_text, (
            "expected outbound anonymization log line, got:\n" + log_text
        )
        assert "Acme" in log_text
        assert "anonymized response from API" in log_text, "expected inbound anonymization log line, got:\n" + log_text
        assert "response deanonymized" in log_text
