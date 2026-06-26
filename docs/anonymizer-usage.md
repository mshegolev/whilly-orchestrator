# Claude API Data Anonymizer

## Overview

The Claude API Data Anonymizer is a privacy-focused proxy that automatically:

- **Anonymizes** sensitive data (e.g., company names) before sending prompts to the Anthropic API
- **Deanonymizes** responses before showing them to the user
- **Logs** the anonymized version for security/compliance auditing

This ensures that sensitive information never reaches external APIs while maintaining transparency in your workflow.

## Standalone package

The redaction proxy is also published as an independent, `pip`-installable
package so teams outside the Whilly project can reuse it:

- **GitHub:** [`mshegolev/claude-anonymizer`](https://github.com/mshegolev/claude-anonymizer) (MIT-licensed, public)
- **PyPI:** [`claude-anonymizer`](https://pypi.org/project/claude-anonymizer/) — `pip install claude-anonymizer`
- **Container:** [`ghcr.io/mshegolev/claude-anonymizer`](https://github.com/mshegolev/claude-anonymizer/pkgs/container/claude-anonymizer)

The standalone package is the company↔placeholder redaction proxy as a general-purpose
HTTP proxy with its own CLI (`anonymizer-proxy`); the sections below document
the *in-Whilly* integration (`whilly.adapters.runner.anonymizer`), which shares
the same redaction model but is wired directly into the agent runner rather
than fronting the API over the network.

## Features

- ✅ Transparent anonymization/deanonymization cycle
- ✅ Case-sensitive company name replacement via configurable map
- ✅ Support for nested JSON structures
- ✅ Detailed logging of anonymization steps
- ✅ Easy activation via environment variable
- ✅ Zero impact when disabled (off by default — empty map by default)

## Usage

### Enable the Anonymizer

Set the environment variable before running Whilly:

```bash
export WHILLY_ENABLE_ANONYMIZER=1
whilly --tasks tasks.json
```

Supported values: `1`, `true`, `yes` (case-insensitive).

### Configure the Redaction Map

The anonymizer loads its redaction map from `WHILLY_ANONYMIZER_MAP`, a JSON
object that maps original strings to their placeholder equivalents.  The
default is **empty** — no redaction occurs unless the map is configured.

Set this in a **local, gitignored `.env`** file (never commit real company
names to the repository):

```bash
# .env  (gitignored)
WHILLY_ANONYMIZER_MAP='{"Globex": "Acme", "globex": "Acme", "GLOBEX": "Acme"}'
```

Replace `Globex` with your actual company name and `Acme` with any
placeholder you prefer.  Each key is matched case-sensitively against the
prompt text.

### Example: Company Name Anonymization

**Without anonymizer (default):**
```bash
$ claude -p "I work at Globex, what should I do?"
```
→ Sends to API: "I work at Globex, what should I do?"
→ Logs show: "I work at Globex, what should I do?"

**With anonymizer enabled:**
```bash
$ WHILLY_ENABLE_ANONYMIZER=1 \
  WHILLY_ANONYMIZER_MAP='{"Globex": "Acme"}' \
  claude -p "I work at Globex, what should I do?"
```
→ Sends to API: "I work at Acme, what should I do?"
→ Logs show: "Sent anonymized prompt: I work at Acme..."
→ Returns to user: "...response mentioning Globex..."

## Configuration

### Redaction Map (WHILLY_ANONYMIZER_MAP)

The map is a JSON object loaded from `WHILLY_ANONYMIZER_MAP` at process start.
Store it in a gitignored `.env` or export it from a secrets manager.

```bash
# Generic fictional example — substitute your own values
WHILLY_ANONYMIZER_MAP='{"Globex": "Acme", "globex": "Acme", "GLOBEX": "Acme"}'
```

When the variable is absent, empty, or contains invalid JSON, the anonymizer
starts with an **empty map** (no redaction) and logs a warning for the
invalid-JSON case.

### Custom Mappings in Code

To construct the anonymizer programmatically with your own mappings:

```python
from whilly.adapters.runner.anonymizer import Anonymizer
from whilly.adapters.runner.claude_anonymizer_proxy import ClaudeAnonymizerProxy

# Custom company names (fictional example)
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
   - `_load_company_mappings()`: Reads `WHILLY_ANONYMIZER_MAP` from the environment
   - Supports case-sensitive replacement
   - Tracks which specific original value was used

2. **`claude_anonymizer_proxy.py`** – Proxy interceptor
   - `ClaudeAnonymizerProxy` class: Wraps Claude invocations
   - Monkey-patches `claude_cli._spawn_and_collect` for transparent integration
   - Logs anonymized prompts before sending, deanonymized responses before returning

### Flow

```
User Input
    ↓
[Anonymizer] – original → placeholder  (from WHILLY_ANONYMIZER_MAP)
    ↓
[Log] Anonymized prompt sent
    ↓
[Claude API] – receives placeholder only
    ↓
Claude Response
    ↓
[Deanonymizer] – placeholder → original
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

- **Unit tests** (10 tests): Anonymizer class functionality, env-map loading
- **Proxy tests** (4 tests): Proxy interceptor behavior
- **Acceptance tests** (3 tests): End-to-end workflow

## Example: Full Integration

```python
import asyncio
from whilly.adapters.runner.anonymizer import Anonymizer
from whilly.adapters.runner.claude_anonymizer_proxy import ClaudeAnonymizerProxy
from whilly.adapters.runner.result_parser import AgentResult

# Create anonymizer with custom mappings (fictional example)
anonymizer = Anonymizer(
    company_mappings={
        "Globex": "Acme",
        "globex": "Acme",
        "GLOBEX": "Acme",
    }
)

# Create proxy
proxy = ClaudeAnonymizerProxy(anonymizer)

# Simulate Claude invocation
async def mock_claude(prompt: str, model: str, *, cwd=None):
    # In real usage, this would be actual Claude invocation
    if "Acme" in prompt:
        return AgentResult(output="Acme is great!", exit_code=0)
    return AgentResult(output="Unknown company", exit_code=1)

proxy._original_spawn_and_collect = mock_claude

# Run with anonymization
async def main():
    result = await proxy.spawn_and_collect_anonymized(
        "I work at Globex",
        "claude-opus-4-6"
    )
    print(result.output)  # Output: "Globex is great!" (deanonymized)

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
- **Secrets management**: Store `WHILLY_ANONYMIZER_MAP` in a gitignored `.env` or a secrets manager; never commit real company names to the repository

## Limitations

- Only handles text-based replacements (simple string replacement)
- Replacement is case-sensitive (`globex` ≠ `Globex` for matching purposes)
- Does not handle partial matches (companyname vs company name)
- Must be enabled before the first Claude invocation

## Future Enhancements

- [ ] Regex-based pattern matching instead of literal strings
- [ ] Support for multi-value anonymization (e.g., email addresses)
- [ ] Integration with credential management systems
