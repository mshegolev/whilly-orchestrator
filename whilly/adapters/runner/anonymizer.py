"""Data anonymizer for sensitive information before sending to Anthropic API.

Handles serialization/deserialization of sensitive data like company names.
Maintains a mapping of original → anonymized values for reversible transformation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("whilly.anonymizer")


@dataclass(frozen=True)
class AnonymizationMapping:
    """Immutable mapping of original → anonymized values."""

    mapping: dict[str, str] = field(default_factory=dict)

    def anonymize(self, original: str) -> str:
        """Replace original value with anonymized equivalent, or return unchanged if not in mapping."""
        return self.mapping.get(original, original)

    def deanonymize(self, anonymized: str) -> str:
        """Reverse the anonymization mapping."""
        reverse_map = {v: k for k, v in self.mapping.items()}
        return reverse_map.get(anonymized, anonymized)


@dataclass
class Anonymizer:
    """Anonymizes/deanonymizes sensitive data in prompts and responses."""

    # Default company name mappings: real → placeholder
    company_mappings: dict[str, str] = field(
        default_factory=lambda: {
            "acme": "Acme",
            "Acme": "Acme",
            "Acme": "Acme",
        }
    )
    # Optional: when set, deanonymize_response uses this as the canonical
    # restore target for any placeholder (overriding the per-call mapping).
    # Useful when multiple originals collapse to one placeholder ("acme",
    # "Acme", "Acme" → "Acme") and the desired user-facing form is a single
    # canonical variant (e.g., Russian uppercase "Acme").
    canonical_form: str | None = None

    def __post_init__(self) -> None:
        """Initialize reverse mapping for deanonymization."""
        self._reverse_mapping = {v: k for k, v in self.company_mappings.items()}
        logger.debug(
            "anonymizer initialized with %d company mappings",
            len(self.company_mappings),
        )

    def anonymize_text(self, text: str) -> tuple[str, dict[str, str]]:
        """Anonymize all occurrences of mapped values in text.

        Args:
            text: Original text with sensitive data.

        Returns:
            Tuple of (anonymized_text, mapping_used) where mapping_used tracks
            which original→anonymized substitutions were made, using the specific
            original variant that was found in the text (useful for accurate
            deanonymization).
        """
        if not text:
            return text, {}

        anonymized = text
        used_mapping = {}

        # Process mappings in order to handle overlapping cases
        # For each variant of a company name found, record it for reverse mapping
        for original, placeholder in self.company_mappings.items():
            if original in anonymized:
                anonymized = anonymized.replace(original, placeholder)
                # Store original → placeholder mapping
                # If multiple variants map to same placeholder, track the one we found
                used_mapping[original] = placeholder
                logger.debug("anonymized %r → %r", original, placeholder)

        return anonymized, used_mapping

    def deanonymize_text(self, text: str, mapping: dict[str, str] | None = None) -> str:
        """Reverse anonymization in text.

        Args:
            text: Anonymized text.
            mapping: Optional mapping of original→anonymized values. If None,
                uses the default reverse mapping.

        Returns:
            Text with anonymized values replaced with originals.
        """
        if not text:
            return text

        result = text
        if mapping is None:
            mapping = self.company_mappings

        # Reverse the mapping: anonymized → original
        reverse = {v: k for k, v in mapping.items()}
        for anonymized, original in reverse.items():
            if anonymized in result:
                result = result.replace(anonymized, original)
                logger.debug("deanonymized %r → %r", anonymized, original)

        return result

    def anonymize_prompt(self, prompt: str) -> tuple[str, dict[str, str]]:
        """Anonymize a Claude prompt before sending to API.

        Args:
            prompt: Raw prompt from user/system.

        Returns:
            Tuple of (anonymized_prompt, mapping_used).
        """
        return self.anonymize_text(prompt)

    def deanonymize_response(self, response: str, mapping: dict[str, str] | None = None) -> str:
        """Deanonymize Claude's response before showing to user.

        If ``canonical_form`` is configured on this Anonymizer, every
        placeholder is mapped back to that single canonical string,
        regardless of which original variant was used in the prompt. This
        is the GDPR-friendly path: the user always sees one tidy company
        name. Without ``canonical_form`` the per-call mapping is honoured
        so the exact variant the user typed is restored.

        Args:
            response: Response from Claude (may contain anonymized values).
            mapping: Optional mapping used during anonymization.

        Returns:
            Response with original values restored.
        """
        if self.canonical_form is not None and response:
            # Replace every distinct placeholder with the canonical form.
            placeholders = set(self.company_mappings.values())
            result = response
            for placeholder in placeholders:
                if placeholder in result:
                    result = result.replace(placeholder, self.canonical_form)
                    logger.debug(
                        "deanonymized %r → %r (canonical)",
                        placeholder,
                        self.canonical_form,
                    )
            return result
        return self.deanonymize_text(response, mapping)

    def anonymize_json(self, obj: Any) -> tuple[Any, dict[str, str]]:
        """Recursively anonymize all strings in a JSON-serializable object.

        Args:
            obj: Any JSON-serializable Python object.

        Returns:
            Tuple of (anonymized_obj, mapping_used).
        """
        used_mapping = {}

        def _anonymize_value(val: Any) -> Any:
            if isinstance(val, str):
                anonymized, local_map = self.anonymize_text(val)
                used_mapping.update(local_map)
                return anonymized
            elif isinstance(val, dict):
                return {k: _anonymize_value(v) for k, v in val.items()}
            elif isinstance(val, list):
                return [_anonymize_value(item) for item in val]
            else:
                return val

        anonymized_obj = _anonymize_value(obj)
        return anonymized_obj, used_mapping

    def deanonymize_json(self, obj: Any, mapping: dict[str, str] | None = None) -> Any:
        """Recursively deanonymize all strings in a JSON-serializable object.

        Args:
            obj: Any JSON-serializable Python object (potentially anonymized).
            mapping: Optional mapping used during anonymization.

        Returns:
            Object with original values restored.
        """
        if mapping is None:
            mapping = self.company_mappings

        def _deanonymize_value(val: Any) -> Any:
            if isinstance(val, str):
                return self.deanonymize_text(val, mapping)
            elif isinstance(val, dict):
                return {k: _deanonymize_value(v) for k, v in val.items()}
            elif isinstance(val, list):
                return [_deanonymize_value(item) for item in val]
            else:
                return val

        return _deanonymize_value(obj)


def get_default_anonymizer() -> Anonymizer:
    """Return the default global anonymizer instance."""
    return Anonymizer()


__all__ = [
    "AnonymizationMapping",
    "Anonymizer",
    "get_default_anonymizer",
]
