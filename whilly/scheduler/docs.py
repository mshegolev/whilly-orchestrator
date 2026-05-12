"""Documentation generation for scheduler rules and workflows."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from whilly.scheduler.models import SchedulerRule, SchedulerPollCycle


@dataclass
class SchedulerDocumentation:
    """Generate documentation for scheduler rules."""

    def generate_rule_markdown(self, rule: SchedulerRule) -> str:
        """Generate markdown documentation for a rule.

        Args:
            rule: SchedulerRule to document

        Returns:
            Markdown-formatted documentation
        """
        lines = [
            f"# {rule.name}",
            "",
            f"**Rule ID:** `{rule.id}`",
            f"**Status:** {'Enabled' if rule.enabled else 'Disabled'}",
            "",
            "## Configuration",
            "",
            f"- **Project Key:** `{rule.jira_project_key}`",
            f"- **JQL Filter:** `{rule.jql_filter}`",
            f"- **Poll Interval:** {rule.poll_interval_seconds} seconds",
            f"- **Max Results per Poll:** {rule.max_results_per_poll}",
            f"- **Deduplication Fields:** {', '.join(rule.deduplication_fields) if rule.deduplication_fields else 'None'}",
            "",
            "## Description",
            "",
            rule.description or "No description provided.",
            "",
        ]

        if rule.plan_config:
            lines.extend(
                [
                    "## Plan Configuration",
                    "",
                    "```json",
                    f"{rule.plan_config}",
                    "```",
                    "",
                ]
            )

        if rule.custom_metadata:
            lines.extend(
                [
                    "## Custom Metadata",
                    "",
                    "```json",
                    f"{rule.custom_metadata}",
                    "```",
                    "",
                ]
            )

        return "\n".join(lines)

    def generate_rules_index(self, rules: list[SchedulerRule]) -> str:
        """Generate index documentation for multiple rules.

        Args:
            rules: List of SchedulerRule objects

        Returns:
            Markdown-formatted index
        """
        lines = [
            "# Scheduler Rules Index",
            "",
            f"Total rules: {len(rules)}",
            f"Enabled: {sum(1 for r in rules if r.enabled)}",
            f"Disabled: {sum(1 for r in rules if not r.enabled)}",
            "",
            "## Rules",
            "",
        ]

        enabled_rules = [r for r in rules if r.enabled]
        disabled_rules = [r for r in rules if not r.enabled]

        if enabled_rules:
            lines.append("### Enabled Rules")
            lines.append("")
            for rule in enabled_rules:
                lines.append(f"- **{rule.name}** (`{rule.id}`)")
                lines.append(f"  - Project: `{rule.jira_project_key}`")
                lines.append(f"  - Poll Interval: {rule.poll_interval_seconds}s")
                lines.append("")

        if disabled_rules:
            lines.append("### Disabled Rules")
            lines.append("")
            for rule in disabled_rules:
                lines.append(f"- **{rule.name}** (`{rule.id}`)")
                lines.append(f"  - Project: `{rule.jira_project_key}`")
                lines.append("")

        return "\n".join(lines)

    def generate_poll_cycle_report(self, cycle: SchedulerPollCycle) -> str:
        """Generate markdown report for a poll cycle.

        Args:
            cycle: SchedulerPollCycle to report

        Returns:
            Markdown-formatted report
        """
        lines = [
            "# Poll Cycle Report",
            "",
            f"**Cycle ID:** {cycle.id}",
            f"**Rule ID:** `{cycle.rule_id}`",
            f"**Status:** {cycle.poll_status.upper()}",
            "",
            "## Results",
            "",
            f"- **Total Issues Found:** {cycle.total_issues_found}",
            f"- **Unique Issues:** {len(cycle.deduplicated_issues) if cycle.deduplicated_issues else 0}",
            f"- **Duplicates Skipped:** {cycle.duplicate_issues_skipped}",
            f"- **New Issues Created:** {cycle.new_issues_created or 0}",
            "",
            "## Timing",
            "",
            f"- **Created At:** {cycle.created_at}",
            f"- **Completed At:** {cycle.completed_at}",
        ]

        if cycle.error_message:
            lines.extend(
                [
                    "",
                    "## Error",
                    "",
                    "```",
                    f"{cycle.error_message}",
                    "```",
                ]
            )

        return "\n".join(lines)

    def write_rule_documentation(self, rule: SchedulerRule, output_dir: Path) -> Path:
        """Write rule documentation to a file.

        Args:
            rule: SchedulerRule to document
            output_dir: Directory to write documentation to

        Returns:
            Path to written file
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        output_file = output_dir / f"{rule.id}.md"

        markdown = self.generate_rule_markdown(rule)
        output_file.write_text(markdown)

        return output_file
