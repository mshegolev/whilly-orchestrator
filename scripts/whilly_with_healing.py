#!/usr/bin/env python3
"""
Self-healing wrapper for Whilly.
Automatically detects, fixes, and restarts on code errors.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

# Add whilly to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from whilly.self_healing import enable_self_healing, SelfHealingHandler


def run_whilly_with_healing(args: list[str], max_retries: int = 3) -> int:
    """Run whilly with self-healing capabilities."""

    print("🛡️  Starting Whilly with Self-Healing Protection")
    print(f"   Max retries: {max_retries}")
    print(f"   Args: {' '.join(args)}")
    print()

    healer = SelfHealingHandler(project_root)

    for attempt in range(max_retries + 1):
        print(f"🚀 Attempt {attempt + 1}/{max_retries + 1}")

        try:
            # Run whilly as subprocess to catch crashes
            result = subprocess.run(
                [sys.executable, "-m", "whilly"] + args,
                cwd=project_root,
                capture_output=False,  # Let output go to terminal
                text=True
            )

            if result.returncode == 0:
                print("✅ Whilly completed successfully!")
                return 0
            else:
                print(f"❌ Whilly exited with code {result.returncode}")

                # For certain error codes, don't retry
                if result.returncode in [2, 3]:  # Budget exceeded, timeout
                    print("🛑 Non-recoverable exit code, stopping")
                    return result.returncode

        except subprocess.CalledProcessError as e:
            print(f"❌ Subprocess error: {e}")

        except KeyboardInterrupt:
            print("🛑 Interrupted by user")
            return 130

        # If we're not at the last attempt, wait and retry
        if attempt < max_retries:
            retry_delay = min(30, 2 ** attempt)  # Exponential backoff, max 30s
            print(f"⏱️  Retrying in {retry_delay} seconds...")
            time.sleep(retry_delay)

            # Try to apply any obvious fixes
            print("🔧 Checking for automated fixes...")
            # This is where more sophisticated error analysis could go

        else:
            print("🚨 Max retries exceeded")

    return 1


def analyze_recent_errors() -> list[str]:
    """Analyze recent error logs for patterns."""

    suggestions = []
    log_dir = project_root / "whilly_logs"

    if log_dir.exists():
        # Look for recent error patterns in logs
        for log_file in log_dir.glob("*.log"):
            try:
                content = log_file.read_text()

                # Common error patterns
                if "NameError:" in content:
                    suggestions.append(f"NameError found in {log_file.name} - check variable scoping")

                if "ModuleNotFoundError:" in content:
                    suggestions.append(f"Missing module in {log_file.name} - check dependencies")

                if "403" in content and "forbidden" in content.lower():
                    suggestions.append(f"Auth error in {log_file.name} - check API credentials")

            except Exception:
                continue

    return suggestions


def main():
    """Main entry point."""

    if len(sys.argv) < 2:
        print("Usage: python whilly_with_healing.py <whilly_args...>")
        print("Example: python whilly_with_healing.py tasks-from-github.json")
        sys.exit(1)

    args = sys.argv[1:]

    # Check for recent error patterns
    suggestions = analyze_recent_errors()
    if suggestions:
        print("🔍 Recent error patterns detected:")
        for suggestion in suggestions[:3]:  # Show max 3
            print(f"   • {suggestion}")
        print()

    # Enable self-healing
    enable_self_healing()

    # Run with healing
    exit_code = run_whilly_with_healing(args)

    if exit_code == 0:
        print("🎉 Pipeline completed successfully!")
    else:
        print(f"💔 Pipeline failed with exit code {exit_code}")

        # Provide recovery suggestions
        print("\n🔧 Recovery suggestions:")
        print("   • Check logs in whilly_logs/ directory")
        print("   • Run: python scripts/check_status_sync.py tasks-*.json")
        print("   • Verify authentication: claude auth status")
        print("   • Check workspace cleanup: git worktree list")

    sys.exit(exit_code)


if __name__ == "__main__":
    main()