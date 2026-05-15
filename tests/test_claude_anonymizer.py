"""Tests for Claude API anonymization proxy and anonymizer module."""

import logging

import pytest

from whilly.adapters.runner.anonymizer import Anonymizer
from whilly.adapters.runner.claude_anonymizer_proxy import (
    ClaudeAnonymizerProxy,
)
from whilly.adapters.runner.result_parser import AgentResult


class TestAnonymizer:
    """Test anonymizer.Anonymizer basic operations."""

    def test_anonymize_text_company_name(self):
        """Test company name anonymization."""
        anon = Anonymizer()
        text = "я из компании acme, напиши ее название на русском"

        anonymized, mapping = anon.anonymize_text(text)

        assert "acme" not in anonymized.lower()
        assert "Acme" in anonymized
        assert mapping == {"acme": "Acme"}
        assert anonymized == "я из компании Acme, напиши ее название на русском"

    def test_anonymize_text_multiple_occurrences(self):
        """Test that all occurrences are replaced."""
        anon = Anonymizer()
        text = "acme is great, acme rocks, use acme"

        anonymized, mapping = anon.anonymize_text(text)

        assert anonymized == "Acme is great, Acme rocks, use Acme"
        assert mapping == {"acme": "Acme"}

    def test_anonymize_text_case_sensitive(self):
        """Test that replacement is case-sensitive."""
        anon = Anonymizer()
        text = "Acme and acme and Acme"

        anonymized, mapping = anon.anonymize_text(text)

        assert "Acme" not in anonymized
        assert "acme" not in anonymized
        assert "Acme" not in anonymized
        # All three variants in the mapping
        assert len(mapping) == 3

    def test_deanonymize_text(self):
        """Test reversing anonymization."""
        anon = Anonymizer()
        original = "я из компании acme, напиши ее название"
        anonymized, mapping = anon.anonymize_text(original)

        restored = anon.deanonymize_text(anonymized, mapping)

        assert restored == original

    def test_deanonymize_with_default_mapping(self):
        """Test deanonymize without explicit mapping uses internal reverse mapping."""
        anon = Anonymizer()
        anonymized = "Company Acme is great"

        restored = anon.deanonymize_text(anonymized)

        assert "acme" in restored.lower()

    def test_anonymize_json_object(self):
        """Test anonymization of JSON objects."""
        anon = Anonymizer()
        obj = {
            "company": "acme",
            "description": "я работаю в acme",
            "nested": {"name": "Acme"},
        }

        anon_obj, mapping = anon.anonymize_json(obj)

        assert anon_obj["company"] == "Acme"
        assert "acme" not in anon_obj["description"]
        assert "Acme" in anon_obj["description"]
        assert anon_obj["nested"]["name"] == "Acme"

    def test_anonymize_json_list(self):
        """Test anonymization of JSON arrays."""
        anon = Anonymizer()
        arr = ["I work at acme", "Acme is great", {"company": "Acme"}]

        anon_arr, mapping = anon.anonymize_json(arr)

        assert "acme" not in str(anon_arr).lower().replace("acme", "")
        assert all("Acme" in str(item) for item in anon_arr)

    def test_deanonymize_json(self):
        """Test deanonymizing JSON objects."""
        anon = Anonymizer()
        original = {"company": "acme", "message": "Work at acme"}
        anon_obj, mapping = anon.anonymize_json(original)

        restored = anon.deanonymize_json(anon_obj, mapping)

        assert restored == original


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
            assert "acme" not in prompt.lower()
            return mock_result

        proxy = ClaudeAnonymizerProxy()
        proxy._original_spawn_and_collect = mock_spawn

        original_prompt = "я из компании acme, что нужно сделать?"
        result = await proxy.spawn_and_collect_anonymized(original_prompt, "claude-opus-4-6")

        # Verify response is deanonymized
        assert "acme" in result.output.lower()
        assert result.output == "Response from acme"

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

        proxy = ClaudeAnonymizerProxy()
        proxy._original_spawn_and_collect = mock_spawn

        result = await proxy.spawn_and_collect_anonymized("Plain text prompt", "claude-opus-4-6")

        assert result.output == "No sensitive data here"

    @pytest.mark.asyncio
    async def test_proxy_handles_empty_output(self):
        """Test that empty output doesn't crash."""
        mock_result = AgentResult(output="", exit_code=0)

        async def mock_spawn(prompt: str, model: str, *, cwd=None):
            return mock_result

        proxy = ClaudeAnonymizerProxy()
        proxy._original_spawn_and_collect = mock_spawn

        result = await proxy.spawn_and_collect_anonymized("я из acme", "claude-opus-4-6")

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

        proxy = ClaudeAnonymizerProxy()
        proxy._original_spawn_and_collect = mock_spawn

        result = await proxy.spawn_and_collect_anonymized("я из acme", "claude-opus-4-6")

        assert result.exit_code == 1


class TestAcceptanceCriteria:
    """Test the specific acceptance criteria from DEMO-9843.

    Criteria:
    - User sends: "я из компании acme, напиши ее название на русском"
    - API receives: "я из компании Acme, напиши ее название на русском"
    - Claude responds with: "Acme"
    - User receives back: "Acme" (deanonymized)
    - Logs show the anonymized version (Acme)
    """

    @pytest.mark.asyncio
    async def test_acceptance_criteria_end_to_end(self, caplog):
        """Test the exact acceptance criteria scenario.

        When user sends a message with their company name (in any case variant),
        it should be anonymized before sending to Claude, and deanonymized back
        using the same variant that was sent.
        """
        # Test with lowercase variant as in the original requirement
        user_input = "я из компании acme, напиши ее название на русском"
        claude_response = "Acme"

        # Mock Claude to verify it receives anonymized prompt and return response
        async def mock_claude(prompt: str, model: str, *, cwd=None):
            # Verify the prompt is anonymized (contains Acme, not acme)
            assert "я из компании Acme" in prompt
            assert "я из компании acme" not in prompt
            return AgentResult(output=claude_response, exit_code=0)

        proxy = ClaudeAnonymizerProxy()
        proxy._original_spawn_and_collect = mock_claude

        # User invokes with original input
        with caplog.at_level(logging.INFO):
            result = await proxy.spawn_and_collect_anonymized(user_input, "claude-opus-4-6")

        # User should see deanonymized response with the same variant they used
        assert result.output == "acme"

        # Logs should show the anonymized version was sent
        log_text = caplog.text
        assert "Acme" in log_text or "anonymized" in log_text.lower()

    @pytest.mark.asyncio
    async def test_acceptance_criteria_logging(self, caplog):
        """Verify that logs show anonymized data while API uses anonymized prompt."""
        user_input = "Company: acme"
        claude_response = "You work at Acme"

        call_count = 0

        async def mock_claude(prompt: str, model: str, *, cwd=None):
            nonlocal call_count
            call_count += 1
            # API should see anonymized version
            assert "Acme" in prompt
            assert "acme" not in prompt.lower()
            return AgentResult(output=claude_response, exit_code=0)

        proxy = ClaudeAnonymizerProxy()
        proxy._original_spawn_and_collect = mock_claude

        with caplog.at_level(logging.DEBUG):
            result = await proxy.spawn_and_collect_anonymized(user_input, "claude-opus-4-6")

        # Verify API was called once with anonymized data
        assert call_count == 1

        # Response should be deanonymized for user
        assert result.output == "You work at acme"

        # Logs should document the anonymization
        assert any("anonymized" in record.message.lower() for record in caplog.records), (
            f"Expected anonymization message in logs, got: {caplog.text}"
        )

    @pytest.mark.asyncio
    async def test_eord_9843_demonstration(self, caplog):
        """DEMO-9843: end-to-end demonstration matching the requirement verbatim.

        Scenario from the acceptance criterion:
            User runs:    claude -p 'я из компании acme, напиши ее название на русском'
            User sees:    "Acme" (deanonymized — Russian company name restored)
            Logs show:    outbound prompt to Anthropic contained "Acme"
                          inbound response from Anthropic contained "Acme" (raw)
            Anthropic:    only ever saw "Acme", never "acme" or "Acme"

        This test wires up the proxy with a Cyrillic-uppercase canonical form
        (Acme) so the deanonymized output is exactly what the requirement asks
        for, and it asserts on caplog records so the audit trail is verified
        — not just stdout-printed.
        """
        from whilly.adapters.runner.anonymizer import Anonymizer

        # Configure the anonymizer so:
        #   outbound:  any of {acme, Acme, Acme} → "Acme" (placeholder)
        #   inbound:   "Acme" → "Acme"          (single canonical Russian form)
        # canonical_form overrides the default per-call deanonymization
        # which would otherwise echo back the exact variant the user typed.
        anon = Anonymizer(
            company_mappings={
                "acme": "Acme",
                "Acme": "Acme",
                "Acme": "Acme",
            },
            canonical_form="Acme",
        )

        # The exact prompt from the acceptance criterion.
        user_prompt = "я из компании acme, напиши ее название на русском"

        # Mock Claude — it only ever sees the anonymized prompt and replies
        # using "Acme" (because that's what it knows about). We assert on
        # the prompt content inside the mock so the test fails loudly if
        # the proxy regressed and let raw "acme" through.
        captured_prompt_to_api = {}

        async def mock_anthropic(prompt: str, model: str, *, cwd=None):
            captured_prompt_to_api["value"] = prompt
            # Hard invariants for what Anthropic must NEVER see.
            assert "acme" not in prompt.lower(), f"PII LEAK: raw 'acme' reached Anthropic API: {prompt!r}"
            assert "Acme" not in prompt, f"PII LEAK: raw 'Acme' reached Anthropic API: {prompt!r}"
            assert "Acme" in prompt, f"anonymization did not produce Acme placeholder: {prompt!r}"
            return AgentResult(
                output="Эта компания называется Acme.",
                exit_code=0,
            )

        proxy = ClaudeAnonymizerProxy(anonymizer=anon)
        proxy._original_spawn_and_collect = mock_anthropic

        with caplog.at_level(logging.INFO, logger="whilly.claude_anonymizer_proxy"):
            result = await proxy.spawn_and_collect_anonymized(user_prompt, "claude-opus-4-6")

        # --- Invariant 1: what Anthropic actually received was anonymized.
        outbound = captured_prompt_to_api["value"]
        assert outbound == "я из компании Acme, напиши ее название на русском"

        # --- Invariant 2: user-visible output has the Russian company name
        #     restored and contains no anonymized placeholder.
        assert "Acme" in result.output, f"expected deanonymized 'Acme' in user output, got: {result.output!r}"
        assert "Acme" not in result.output, f"deanonymizer leaked 'Acme' to user: {result.output!r}"
        assert result.output == "Эта компания называется Acme."

        # --- Invariant 3: audit-trail logs prove both directions were anonymized.
        log_text = caplog.text
        # outbound log line — what was sent to API (must contain "Acme")
        assert "anonymized prompt sent to API" in log_text, (
            "expected outbound anonymization log line, got:\n" + log_text
        )
        assert "Acme" in log_text
        # inbound log line — raw response from API before deanonymization
        # (must contain "Acme", proving Anthropic returned the anonymized form)
        assert "anonymized response from API" in log_text, "expected inbound anonymization log line, got:\n" + log_text
        # And finally, the deanonymization summary log line.
        assert "response deanonymized" in log_text
