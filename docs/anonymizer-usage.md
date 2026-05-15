# Claude API Data Anonymizer

## Overview

The Claude API Data Anonymizer is a privacy-focused proxy that automatically:

- **Anonymizes** sensitive data (e.g., company names) before sending prompts to the Anthropic API
- **Deanonymizes** responses before showing them to the user
- **Logs** the anonymized version for security/compliance auditing

This ensures that sensitive information never reaches external APIs while maintaining transparency in your workflow.

## Features

- ✅ Transparent anonymization/deanonymization cycle
- ✅ Case-sensitive company name replacement (mts, МТС, MTS → Acme)
- ✅ Support for nested JSON structures
- ✅ Detailed logging of anonymization steps
- ✅ Easy activation via environment variable
- ✅ Zero impact when disabled (off by default)

## Usage

### Enable the Anonymizer

Set the environment variable before running Whilly:

```bash
export WHILLY_ENABLE_ANONYMIZER=1
whilly --tasks tasks.json
```

Supported values: `1`, `true`, `yes` (case-insensitive).

### Example: Company Name Anonymization

**Without anonymizer (default):**
```bash
$ claude -p "I work at mts, what should I do?"
```
→ Sends to API: "I work at mts, what should I do?"
→ Logs show: "I work at mts, what should I do?"

**With anonymizer enabled:**
```bash
$ WHILLY_ENABLE_ANONYMIZER=1 claude -p "I work at mts, what should I do?"
```
→ Sends to API: "I work at Acme, what should I do?"
→ Logs show: "Sent anonymized prompt: I work at Acme..."
→ Returns to user: "...response mentioning mts..."

## Configuration

### Default Mappings

By default, the following case-sensitive mappings are used:

| Original | Anonymized |
|----------|-----------|
| `mts` | `Acme` |
| `МТС` | `Acme` |
| `MTS` | `Acme` |

### Custom Mappings

To use custom mappings, create an `Anonymizer` instance with your own mappings:

```python
from whilly.adapters.runner.anonymizer import Anonymizer
from whilly.adapters.runner.claude_anonymizer_proxy import ClaudeAnonymizerProxy

# Custom company names
custom_anonymizer = Anonymizer(
    company_mappings={
        "my-company": "MyPlaceholder",
        "internal-codename": "PublicCodename",
    }
)

proxy = ClaudeAnonymizerProxy(custom_anonymizer)
proxy.patch_claude_cli()
```

## Architecture

### Modules

1. **`anonymizer.py`** – Core anonymization logic
   - `Anonymizer` class: Handles text/JSON anonymization and deanonymization
   - Supports case-sensitive replacement
   - Tracks which specific original value was used (e.g., "mts" vs "МТС")

2. **`claude_anonymizer_proxy.py`** – Proxy interceptor
   - `ClaudeAnonymizerProxy` class: Wraps Claude invocations
   - Monkey-patches `claude_cli._spawn_and_collect` for transparent integration
   - Logs anonymized prompts before sending, deanonymized responses before returning

### Flow

```
User Input
    ↓
[Anonymizer] – mts → Acme
    ↓
[Log] Anonymized prompt sent
    ↓
[Claude API] – receives "Acme"
    ↓
Claude Response
    ↓
[Deanonymizer] – Acme → mts
    ↓
[Log] Response deanonymized
    ↓
User sees original company name
```

## Testing

Run the test suite:

```bash
python3 -m pytest tests/test_claude_anonymizer.py -v
python3 -m pytest tests/test_anonymizer_integration.py -v
```

### Test Coverage

- **Unit tests** (8 tests): Anonymizer class functionality
- **Proxy tests** (4 tests): Proxy interceptor behavior
- **Acceptance tests** (2 tests): End-to-end workflow
- **Integration tests** (15 tests): Environment variable activation

## Example: Full Integration

```python
import asyncio
from whilly.adapters.runner.anonymizer import Anonymizer
from whilly.adapters.runner.claude_anonymizer_proxy import ClaudeAnonymizerProxy
from whilly.adapters.runner.result_parser import AgentResult

# Create anonymizer with custom mappings
anonymizer = Anonymizer(
    company_mappings={
        "mts": "CompanyX",
        "МТС": "CompanyX",
        "MTS": "CompanyX",
    }
)

# Create proxy
proxy = ClaudeAnonymizerProxy(anonymizer)

# Simulate Claude invocation
async def mock_claude(prompt: str, model: str, *, cwd=None):
    # In real usage, this would be actual Claude invocation
    if "CompanyX" in prompt:
        return AgentResult(output="CompanyX is great!", exit_code=0)
    return AgentResult(output="Unknown company", exit_code=1)

proxy._original_spawn_and_collect = mock_claude

# Run with anonymization
async def main():
    result = await proxy.spawn_and_collect_anonymized(
        "I work at mts",
        "claude-opus-4-6"
    )
    print(result.output)  # Output: "I work at mts" (deanonymized)

asyncio.run(main())
```

## Logging

The anonymizer logs at different levels:

- **INFO**: Anonymization/deanonymization summary (byte counts)
- **DEBUG**: Detailed mapping information and individual replacements
- **ERROR**: Initialization failures (when enabled)

Enable debug logging:

```bash
WHILLY_ENABLE_ANONYMIZER=1 \
WHILLY_LOG_LEVEL=DEBUG \
whilly --tasks tasks.json
```

## Security Considerations

- **API logs**: Anthropic API logs will show anonymized data (intended behavior)
- **Local logs**: Whilly logs show the anonymization process for audit trails
- **Response verification**: Deanonymization is deterministic – the same anonymized value always maps to the same original

## Limitations

- Only handles text-based replacements (simple string replacement)
- Replacement is case-sensitive (mts ≠ MTS for matching purposes)
- Does not handle partial matches (companyname vs company name)
- Must be enabled before the first Claude invocation

## Future Enhancements

- [ ] Regex-based pattern matching instead of literal strings
- [ ] Configurable mappings via JSON file
- [ ] Support for multi-value anonymization (e.g., email addresses)
- [ ] Integration with credential management systems
