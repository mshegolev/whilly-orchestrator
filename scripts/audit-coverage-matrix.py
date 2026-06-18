#!/usr/bin/env python3
"""
Audit the OpenSpec coverage matrix to ensure no spec↔code drift.

This script validates that:
1. Live module count equals matrix body-row count
2. Zero UNMAPPED entries exist
3. Zero double-mapped module paths exist
4. Every capability slug used is one of the taxonomy slugs
5. Every taxonomy capability has ≥1 module row

Exit codes:
- 0: All checks passed
- 1: One or more checks failed
"""

import subprocess
import sys
import os
import re
from collections import Counter


def count_live_modules():
    """Count live whilly/ modules using the same command as documented."""
    try:
        result = subprocess.run(
            ["find", "whilly/", "-name", "*.py", "-not", "-path", "*/__pycache__/*"],
            capture_output=True,
            text=True,
            check=True,
        )
        lines = result.stdout.strip().split("\n")
        # Filter out empty lines (in case of trailing newline)
        files = [line for line in lines if line.strip()]
        return len(files)
    except subprocess.CalledProcessError as e:
        print(f"Error counting live modules: {e}")
        return None


def parse_coverage_matrix():
    """Parse the coverage matrix to extract counts and mappings."""
    matrix_file = "openspec/COVERAGE-MATRIX.md"

    if not os.path.exists(matrix_file):
        print(f"Error: {matrix_file} not found")
        return None

    with open(matrix_file, "r") as f:
        content = f.read()

    # Extract counts from the "Counts" section
    counts_section = re.search(r"## Counts\n\n(.*?)\n##", content, re.DOTALL)
    if not counts_section:
        print("Error: Could not find Counts section in coverage matrix")
        return None

    counts_text = counts_section.group(1)

    # Extract documented counts
    live_count_match = re.search(r"- \*\*Live module count: (\d+)\*\*", counts_text)
    body_rows_match = re.search(r"- \*\*Body rows: (\d+)\*\*", counts_text)
    unmapped_match = re.search(r"- \*\*Unmapped: (\d+)\*\*", counts_text)
    double_mapped_match = re.search(r"- \*\*Double-mapped: (\d+)\*\*", counts_text)

    if not all([live_count_match, body_rows_match, unmapped_match, double_mapped_match]):
        print("Error: Could not extract all counts from coverage matrix")
        return None

    documented_counts = {
        "live_module_count": int(live_count_match.group(1)) if live_count_match else 0,
        "body_rows": int(body_rows_match.group(1)) if body_rows_match else 0,
        "unmapped": int(unmapped_match.group(1)) if unmapped_match else 0,
        "double_mapped": int(double_mapped_match.group(1)) if double_mapped_match else 0,
    }

    # Extract capability mappings from the matrix table
    matrix_section = re.search(r"\| Module \| Capability \| Notes \|(.*?)\n\n", content, re.DOTALL)
    if not matrix_section:
        print("Error: Could not find coverage matrix table")
        return None

    matrix_lines = matrix_section.group(1).strip().split("\n")

    # Skip header lines (already matched by the table header)
    # Parse each row to extract module -> capability mappings
    mappings = []
    capability_slugs = set()

    # Process the actual data rows (skip separator line if present)
    for line in matrix_lines:
        line = line.strip()
        # Skip empty lines, header separators, and table headers
        if not line or line.startswith("|----") or line.startswith("| Module"):
            continue

        # Parse table row: | module | capability | notes |
        parts = [part.strip() for part in line.split("|") if part.strip()]
        if len(parts) >= 2:
            module = parts[0]
            capability = parts[1]
            mappings.append((module, capability))
            capability_slugs.add(capability)

    return {"documented_counts": documented_counts, "mappings": mappings, "capability_slugs": capability_slugs}


def load_taxonomy_slugs():
    """Load the official taxonomy slugs from TAXONOMY.md."""
    taxonomy_file = "openspec/TAXONOMY.md"

    if not os.path.exists(taxonomy_file):
        print(f"Error: {taxonomy_file} not found")
        return None

    with open(taxonomy_file, "r") as f:
        content = f.read()

    # Extract slugs from all taxonomy tables
    # Pattern to match tables with Slug and Purpose columns
    table_pattern = r"\|\s*Slug\s*\|\s*Purpose\s*\|(.*?)(?=\n##|\Z)"
    table_matches = re.findall(table_pattern, content, re.DOTALL)

    if not table_matches:
        print("Error: Could not find taxonomy tables")
        return None

    slugs = set()
    for table_text in table_matches:
        lines = table_text.strip().split("\n")
        for line in lines:
            line = line.strip()
            # Skip empty lines, header separators, and table headers
            if not line or line.startswith("|----") or line.startswith("| Slug") or line.startswith("|------"):
                continue

            # Parse table row: | slug | purpose |
            parts = [part.strip() for part in line.split("|") if part.strip()]
            if len(parts) >= 1:
                slug = parts[0]
                # Clean the slug (remove backticks and extra characters)
                slug = slug.replace("`", "").replace("*", "").replace("_", "").strip()
                if slug and not slug.startswith("---"):
                    slugs.add(slug)

    return slugs


def check_double_mappings(mappings):
    """Check for double-mapped modules."""
    module_counter = Counter(module for module, _ in mappings)
    double_mapped = [module for module, count in module_counter.items() if count > 1]
    return double_mapped


def verify_taxonomy_coverage(capability_slugs, taxonomy_slugs):
    """Verify that all used capability slugs are in the taxonomy."""
    invalid_slugs = capability_slugs - taxonomy_slugs - {"UNMAPPED"}
    return invalid_slugs


def verify_capability_usage(mappings, taxonomy_slugs):
    """Verify that every taxonomy capability has ≥1 module row."""
    # Count how many modules each capability has
    capability_counts = Counter(capability for _, capability in mappings)

    # Check each taxonomy slug (except special values)
    unused_capabilities = []
    for slug in taxonomy_slugs:
        if slug not in capability_counts or capability_counts[slug] == 0:
            unused_capabilities.append(slug)

    return unused_capabilities


def main():
    """Main audit function."""
    print("🔍 Auditing OpenSpec coverage matrix...")

    # Step 1: Count live modules
    print("  → Counting live modules...")
    live_count = count_live_modules()
    if live_count is None:
        print("❌ Failed to count live modules")
        return 1

    print(f"    Live modules: {live_count}")

    # Step 2: Parse coverage matrix
    print("  → Parsing coverage matrix...")
    matrix_data = parse_coverage_matrix()
    if matrix_data is None:
        print("❌ Failed to parse coverage matrix")
        return 1

    documented_counts = matrix_data["documented_counts"]
    mappings = matrix_data["mappings"]
    capability_slugs = matrix_data["capability_slugs"]

    print(f"    Documented live count: {documented_counts['live_module_count']}")
    print(f"    Matrix body rows: {documented_counts['body_rows']}")
    print(f"    Unmapped entries: {documented_counts['unmapped']}")
    print(f"    Double-mapped entries: {documented_counts['double_mapped']}")
    print(f"    Unique capability slugs: {len(capability_slugs)}")

    # Step 3: Load taxonomy
    print("  → Loading taxonomy slugs...")
    taxonomy_slugs = load_taxonomy_slugs()
    if taxonomy_slugs is None:
        print("❌ Failed to load taxonomy slugs")
        return 1

    print(f"    Taxonomy slugs: {len(taxonomy_slugs)}")

    # Step 4: Perform all checks
    print("  → Performing validation checks...")

    errors = []

    # Check 1: Live module count == matrix body-row count
    if live_count != documented_counts["body_rows"]:
        errors.append(f"Mismatch: Live modules ({live_count}) != Matrix body rows ({documented_counts['body_rows']})")

    # Check 2: Zero UNMAPPED entries
    if documented_counts["unmapped"] > 0:
        errors.append(f"Found {documented_counts['unmapped']} UNMAPPED entries")

    # Check 3: Zero double-mapped module paths
    double_mapped = check_double_mappings(mappings)
    if double_mapped:
        errors.append(
            f"Found {len(double_mapped)} double-mapped modules: {double_mapped[:5]}{'...' if len(double_mapped) > 5 else ''}"
        )

    # Check 4: Every capability slug used is one of the taxonomy slugs
    invalid_slugs = verify_taxonomy_coverage(capability_slugs, taxonomy_slugs)
    if invalid_slugs:
        errors.append(f"Invalid capability slugs found: {invalid_slugs}")

    # Check 5: Every taxonomy capability has ≥1 module row
    unused_capabilities = verify_capability_usage(mappings, taxonomy_slugs)
    if unused_capabilities:
        errors.append(f"Unused taxonomy capabilities: {unused_capabilities}")

    # Report results
    if errors:
        print("\n❌ Audit FAILED:")
        for error in errors:
            print(f"  • {error}")
        return 1
    else:
        print("\n✅ All audit checks PASSED!")
        print(f"  • Live modules: {live_count}")
        print(f"  • Matrix rows: {documented_counts['body_rows']}")
        print(f"  • Unmapped: {documented_counts['unmapped']}")
        print(f"  • Double-mapped: {documented_counts['double_mapped']}")
        print(f"  • Capability slugs: {len(capability_slugs)}")
        print(f"  • Taxonomy slugs: {len(taxonomy_slugs)}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
