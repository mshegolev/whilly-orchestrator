"""Proxy layer for Claude invocations with data anonymization.

Intercepts prompts before sending to Anthropic API and responses before
returning to the user, ensuring sensitive data is never exposed in logs
or transmitted in plain form.
"""

from __future__ import annotations

import logging
from pathlib import Path

from whilly.adapters.runner import claude_cli
from whilly.adapters.runner.anonymizer import Anonymizer, get_default_anonymizer
from whilly.adapters.runner.result_parser import AgentResult

logger = logging.getLogger("whilly.claude_anonymizer_proxy")


class ClaudeAnonymizerProxy:
    """Wraps Claude CLI invocation with anonymization."""

    def __init__(self, anonymizer: Anonymizer | None = None):
        """Initialize proxy with an anonymizer.

        Args:
            anonymizer: Anonymizer instance. If None, uses default.
        """
        self.anonymizer = anonymizer or get_default_anonymizer()
        self._original_spawn_and_collect = claude_cli._spawn_and_collect

    async def spawn_and_collect_anonymized(
        self,
        prompt: str,
        model: str,
        *,
        cwd: Path | None = None,
    ) -> AgentResult:
        """Spawn Claude with anonymized prompt, deanonymize response.

        Args:
            prompt: Original prompt with potentially sensitive data.
            model: Claude model to use.
            cwd: Working directory for the subprocess.

        Returns:
            AgentResult with deanonymized response.
        """
        # Anonymize the prompt
        anonymized_prompt, mapping = self.anonymizer.anonymize_prompt(prompt)

        logger.info(
            "prompt anonymized: %d byte(s) → %d byte(s)",
            len(prompt),
            len(anonymized_prompt),
        )
        if mapping:
            logger.debug("anonymization mapping: %s", mapping)
            logger.info("anonymized prompt sent to API: %s", anonymized_prompt[:200])

        # Invoke Claude with anonymized prompt
        try:
            result = await self._original_spawn_and_collect(anonymized_prompt, model, cwd=cwd)
        except Exception as exc:
            logger.error("Claude invocation failed: %s", exc)
            raise

        # Deanonymize the response (create new instance since AgentResult is frozen)
        if result.output:
            # Log the RAW (still-anonymized) response from API for audit trail.
            # This is the artefact GDPR compliance asks for: proof that what
            # left the perimeter was anonymized, and what came back from
            # Anthropic was the anonymized form (e.g., "Acme") before any
            # local-side substitution turned it back into the original company name.
            logger.info("anonymized response from API: %s", result.output[:200])
            original_output = self.anonymizer.deanonymize_response(result.output, mapping)
            logger.info(
                "response deanonymized: %d byte(s) → %d byte(s)",
                len(result.output),
                len(original_output),
            )
            # Create a new AgentResult with deanonymized output
            from dataclasses import replace

            result = replace(result, output=original_output)

        return result

    def patch_claude_cli(self) -> None:
        """Monkey-patch claude_cli._spawn_and_collect with anonymizing proxy.

        This allows transparent anonymization for all code paths that call
        the original function.
        """
        claude_cli._spawn_and_collect = self.spawn_and_collect_anonymized


def create_and_patch_proxy(
    anonymizer: Anonymizer | None = None,
) -> ClaudeAnonymizerProxy:
    """Create a proxy and immediately patch claude_cli module.

    Args:
        anonymizer: Optional custom Anonymizer instance.

    Returns:
        The created proxy (mainly for testing/inspection).
    """
    proxy = ClaudeAnonymizerProxy(anonymizer)
    proxy.patch_claude_cli()
    logger.info("Claude anonymizer proxy installed")
    return proxy


__all__ = [
    "ClaudeAnonymizerProxy",
    "create_and_patch_proxy",
]
