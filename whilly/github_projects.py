"""
GitHub Projects v2 integration for Whilly.
Converts Project board items to GitHub Issues for whilly processing.
Supports status-oriented workflows with incremental sync and monitoring.
"""

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Dict, Any, Set

from whilly.config import WhillyConfig


@dataclass
class ProjectItem:
    """Single item from GitHub Project board."""

    id: str
    title: str
    body: str = ""
    status: str = "Todo"
    priority: str = "medium"
    labels: List[str] = field(default_factory=list)
    assignee: Optional[str] = None
    url: Optional[str] = None
    updated_at: Optional[str] = None
    issue_number: Optional[int] = None

    def __post_init__(self):
        if self.labels is None:
            self.labels = []

    @property
    def whilly_label(self) -> str:
        """Map project status to whilly label."""
        status_to_label = {
            "Todo": "whilly:ready",
            "In Progress": "whilly:in-progress",
            "Review": "whilly:review",
            "Done": "whilly:done",
            "Backlog": "whilly:backlog",
        }
        return status_to_label.get(self.status, "whilly:ready")


@dataclass
class SyncConfig:
    """Configuration for status-oriented sync workflow."""

    target_statuses: Set[str] = field(default_factory=lambda: {"Todo"})
    status_mapping: Dict[str, str] = field(
        default_factory=lambda: {
            "Todo": "whilly:ready",
            "In Progress": "whilly:in-progress",
            "Review": "whilly:review",
            "Done": "whilly:done",
            "Backlog": "whilly:backlog",
        }
    )
    reverse_mapping: Dict[str, str] = field(
        default_factory=lambda: {
            "whilly:ready": "Todo",
            "whilly:in-progress": "In Progress",
            "whilly:review": "Review",
            "whilly:done": "Done",
            "whilly:backlog": "Backlog",
        }
    )
    sync_state_file: str = ".whilly_project_sync_state.json"
    watch_interval: int = 60  # seconds


class GitHubProjectsConverter:
    """Converts GitHub Project board items to Issues and Whilly tasks.

    Supports both batch conversion and incremental status-oriented workflows.
    """

    def __init__(self, config: WhillyConfig = None, sync_config: SyncConfig = None):
        self.config = config or WhillyConfig.from_env()
        self.sync_config = sync_config or SyncConfig()
        self._check_gh_cli()
        self._project_info: Optional[Dict[str, Any]] = None
        self._sync_state: Dict[str, Any] = self._load_sync_state()

    def _load_sync_state(self) -> Dict[str, Any]:
        """Load sync state from file."""
        state_file = Path(self.sync_config.sync_state_file)
        if not state_file.exists():
            return {"last_sync": None, "synced_items": {}, "project_url": None, "repo_owner": None, "repo_name": None}

        try:
            with open(state_file, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"⚠️  Failed to load sync state: {e}")
            return {"last_sync": None, "synced_items": {}, "project_url": None, "repo_owner": None, "repo_name": None}

    def _save_sync_state(self):
        """Save sync state to file."""
        try:
            with open(self.sync_config.sync_state_file, "w") as f:
                json.dump(self._sync_state, f, indent=2, default=str)
        except IOError as e:
            print(f"⚠️  Failed to save sync state: {e}")

    def _check_gh_cli(self):
        """Verify GitHub CLI is available and authenticated."""
        try:
            result = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, check=True)
            if "Logged in to github.com" not in result.stdout:
                raise RuntimeError("GitHub CLI not authenticated. Run: gh auth login")
        except subprocess.CalledProcessError:
            raise RuntimeError("GitHub CLI not authenticated. Run: gh auth login")
        except FileNotFoundError:
            raise RuntimeError("GitHub CLI not found. Install: https://cli.github.com/")

    def parse_project_url(self, project_url: str) -> Dict[str, Any]:
        """Parse GitHub Project URL to extract owner, project number, etc."""

        # Handle different URL formats:
        # https://github.com/users/mshegolev/projects/4/views/1
        # https://github.com/orgs/myorg/projects/5
        # https://github.com/mshegolev/repo/projects/3

        patterns = [
            r"github\.com/users/([^/]+)/projects/(\d+)",
            r"github\.com/orgs/([^/]+)/projects/(\d+)",
            r"github\.com/([^/]+)/([^/]+)/projects/(\d+)",
        ]

        for pattern in patterns:
            match = re.search(pattern, project_url)
            if match:
                if len(match.groups()) == 2:  # users/orgs format
                    owner = match.group(1)
                    project_number = match.group(2)
                    return {
                        "owner": owner,
                        "project_number": int(project_number),
                        "type": "user" if "/users/" in project_url else "org",
                        "repo": None,
                    }
                else:  # repo format
                    owner = match.group(1)
                    repo = match.group(2)
                    project_number = match.group(3)
                    return {"owner": owner, "repo": repo, "project_number": int(project_number), "type": "repo"}

        raise ValueError(f"Invalid GitHub Project URL format: {project_url}")

    def fetch_project_items(
        self, project_url: str, filter_statuses: Optional[Set[str]] = None, include_updated_at: bool = False
    ) -> List[ProjectItem]:
        """Fetch items from GitHub Project board using GraphQL.

        Args:
            project_url: GitHub Project URL
            filter_statuses: Only return items with these statuses. If None, return all items.
            include_updated_at: Include updatedAt field for sync tracking
        """
        project_info = self.parse_project_url(project_url)
        self._project_info = project_info

        # GraphQL query to fetch project items
        updated_at_field = "updatedAt" if include_updated_at else ""

        query = f"""
        query($owner: String!, $number: Int!) {{
          user(login: $owner) {{
            projectV2(number: $number) {{
              items(first: 100) {{
                nodes {{
                  id
                  {updated_at_field}
                  fieldValues(first: 20) {{
                    nodes {{
                      ... on ProjectV2ItemFieldTextValue {{
                        text
                        field {{
                          ... on ProjectV2FieldCommon {{
                            name
                          }}
                        }}
                      }}
                      ... on ProjectV2ItemFieldSingleSelectValue {{
                        name
                        field {{
                          ... on ProjectV2FieldCommon {{
                            name
                          }}
                        }}
                      }}
                    }}
                  }}
                  content {{
                    ... on DraftIssue {{
                      title
                      body
                    }}
                    ... on Issue {{
                      title
                      body
                      url
                      number
                    }}
                    ... on PullRequest {{
                      title
                      body
                      url
                      number
                    }}
                  }}
                }}
              }}
            }}
          }}
        }}
        """

        try:
            # Execute GraphQL query via gh CLI
            cmd = [
                "gh",
                "api",
                "graphql",
                "-f",
                f"query={query}",
                "-F",
                f"owner={project_info['owner']}",
                "-F",
                f"number={project_info['project_number']}",
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            data = json.loads(result.stdout)

            items = self._parse_project_items(data, include_updated_at)

            # Filter by status if requested
            if filter_statuses:
                items = [item for item in items if item.status in filter_statuses]

            return items

        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to fetch project items: {e.stderr}")
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Invalid JSON response from GitHub API: {e}")

    def _parse_project_items(self, data: Dict[str, Any], include_updated_at: bool = False) -> List[ProjectItem]:
        """Parse GraphQL response into ProjectItem objects."""

        items = []
        project_items = data.get("data", {}).get("user", {}).get("projectV2", {}).get("items", {}).get("nodes", [])

        for item_data in project_items:
            content = item_data.get("content", {})

            # Skip if no content (empty project item)
            if not content:
                continue

            title = content.get("title", "Untitled")
            body = content.get("body", "")
            url = content.get("url")
            issue_number = content.get("number")

            # Extract field values (Status, Priority, etc.)
            status = "Todo"
            priority = "medium"
            updated_at = item_data.get("updatedAt") if include_updated_at else None

            field_values = item_data.get("fieldValues", {}).get("nodes", [])
            for field_value in field_values:
                field_name = field_value.get("field", {}).get("name", "").lower()

                if field_name == "status":
                    status = field_value.get("name", status)
                elif field_name == "priority":
                    priority = field_value.get("name", priority).lower()
                elif field_name in ["title"]:
                    if "text" in field_value:
                        title = field_value["text"] or title

            # Create ProjectItem
            item = ProjectItem(
                id=item_data["id"],
                title=title,
                body=body,
                status=status,
                priority=priority,
                url=url,
                updated_at=updated_at,
                issue_number=issue_number,
            )

            items.append(item)

        return items

    def convert_items_to_issues(
        self, items: List[ProjectItem], repo_owner: str, repo_name: str, label: str = "whilly:ready"
    ) -> List[Dict[str, Any]]:
        """Convert Project items to GitHub Issues."""

        created_issues = []

        for item in items:
            # Skip if already an issue (has URL with /issues/)
            if item.url and "/issues/" in item.url:
                print(f"⏭️  Skipping {item.title} - already an issue")
                continue

            try:
                # Create GitHub Issue
                issue_data = self._create_github_issue(item, repo_owner, repo_name, label)
                created_issues.append(issue_data)
                print(f"✅ Created Issue: {item.title}")

            except Exception as e:
                print(f"❌ Failed to create issue for {item.title}: {e}")

        return created_issues

    def _create_github_issue(self, item: ProjectItem, owner: str, repo: str, label: str) -> Dict[str, Any]:
        """Create a single GitHub Issue from ProjectItem."""

        # Prepare issue body
        body = item.body or f"Converted from GitHub Project item.\n\nOriginal Status: {item.status}"

        if item.priority and item.priority != "medium":
            body += f"\nPriority: {item.priority}"

        # Create issue via gh CLI
        cmd = [
            "gh",
            "issue",
            "create",
            "--repo",
            f"{owner}/{repo}",
            "--title",
            item.title,
            "--body",
            body,
            "--label",
            label,
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        issue_url = result.stdout.strip()

        # Extract issue number from URL
        issue_number = int(issue_url.split("/")[-1])

        return {"title": item.title, "body": body, "url": issue_url, "number": issue_number, "labels": [label]}

    def project_to_whilly_tasks(
        self,
        project_url: str,
        repo_owner: str,
        repo_name: str,
        output_file: str = "tasks-from-project.json",
        label: str = "whilly:ready",
    ) -> str:
        """Complete pipeline: Project → Issues → Whilly tasks."""

        print(f"🔄 Fetching items from GitHub Project: {project_url}")
        items = self.fetch_project_items(project_url)
        print(f"📋 Found {len(items)} project items")

        if not items:
            print("❌ No items found in the project")
            return output_file

        print(f"🔄 Converting {len(items)} items to GitHub Issues...")
        created_issues = self.convert_items_to_issues(items, repo_owner, repo_name, label)
        print(f"✅ Created {len(created_issues)} new issues")

        # Now use existing github_converter to create Whilly tasks
        from whilly.sources.github_issues import fetch_github_issues

        print(f"🔄 Generating Whilly tasks from issues with label: {label}")
        repo_spec = f"{repo_owner}/{repo_name}"
        plan_path, stats = fetch_github_issues(repo_spec, label=label, out_path=output_file)

        print(f"✅ Whilly tasks saved to: {output_file}")
        return output_file

    def sync_todo_items(
        self, project_url: str, repo_owner: str, repo_name: str, output_file: str = "tasks-from-project.json"
    ) -> Dict[str, Any]:
        """Sync only Todo items from GitHub Project to Issues and tasks.

        Returns sync statistics and information.
        """
        print(f"🔄 Syncing Todo items from GitHub Project: {project_url}")

        # Fetch only Todo items
        items = self.fetch_project_items(
            project_url, filter_statuses=self.sync_config.target_statuses, include_updated_at=True
        )

        print(f"📋 Found {len(items)} Todo items")

        if not items:
            print("✅ No Todo items to sync")
            return {"synced_count": 0, "created_count": 0, "skipped_count": 0, "total_todo_items": 0}

        # Track what we've already synced
        synced_items = self._sync_state.get("synced_items", {})
        created_count = 0
        skipped_count = 0

        new_issues = []
        for item in items:
            item_key = f"{item.id}:{item.status}"

            # Skip if already synced and not updated
            if item_key in synced_items:
                last_sync = synced_items[item_key].get("last_sync")
                if item.updated_at and last_sync and item.updated_at <= last_sync:
                    print(f"⏭️  Skipping {item.title} - already synced and not updated")
                    skipped_count += 1
                    continue

            # Skip if already an issue
            if item.url and "/issues/" in item.url:
                print(f"⏭️  Skipping {item.title} - already an issue")
                synced_items[item_key] = {
                    "issue_number": item.issue_number,
                    "issue_url": item.url,
                    "last_sync": datetime.now(timezone.utc).isoformat(),
                }
                skipped_count += 1
                continue

            try:
                # Create GitHub Issue with appropriate label
                label = self.sync_config.status_mapping.get(item.status, "whilly:ready")
                issue_data = self._create_github_issue(item, repo_owner, repo_name, label)
                new_issues.append(issue_data)

                # Record in sync state
                synced_items[item_key] = {
                    "issue_number": issue_data["number"],
                    "issue_url": issue_data["url"],
                    "last_sync": datetime.now(timezone.utc).isoformat(),
                }

                print(f"✅ Created Issue: {item.title}")
                created_count += 1

            except Exception as e:
                print(f"❌ Failed to create issue for {item.title}: {e}")

        # Update sync state
        self._sync_state.update(
            {
                "last_sync": datetime.now(timezone.utc).isoformat(),
                "synced_items": synced_items,
                "project_url": project_url,
                "repo_owner": repo_owner,
                "repo_name": repo_name,
            }
        )
        self._save_sync_state()

        # Generate Whilly tasks from all issues with appropriate labels
        if new_issues:
            from whilly.sources.github_issues import fetch_github_issues

            label = self.sync_config.status_mapping.get("Todo", "whilly:ready")
            print(f"🔄 Generating Whilly tasks from issues with label: {label}")
            repo_spec = f"{repo_owner}/{repo_name}"
            plan_path, stats = fetch_github_issues(repo_spec, label=label, out_path=output_file)
            print(f"✅ Whilly tasks updated: {output_file}")

        return {
            "synced_count": created_count,
            "created_count": created_count,
            "skipped_count": skipped_count,
            "total_todo_items": len(items),
        }

    def sync_status_changes(self, issue_number: int, new_status: str) -> bool:
        """Sync status change from Issue back to Project item.

        Args:
            issue_number: GitHub issue number
            new_status: New status to set in the project

        Returns:
            True if sync was successful
        """
        if not self._project_info:
            print("❌ No project info available. Run sync_todo_items first.")
            return False

        # Find the project item ID for this issue
        project_item_id = None
        for item_key, sync_data in self._sync_state.get("synced_items", {}).items():
            if sync_data.get("issue_number") == issue_number:
                project_item_id = item_key.split(":")[0]
                break

        if not project_item_id:
            print(f"❌ Project item not found for issue #{issue_number}")
            return False

        try:
            # Update project item status via GraphQL mutation
            return self._update_project_item_status(project_item_id, new_status)
        except Exception as e:
            print(f"❌ Failed to sync status for issue #{issue_number}: {e}")
            return False

    def _update_project_item_status(self, item_id: str, new_status: str) -> bool:
        """Update project item status using GraphQL mutation."""
        # This would require the field ID for the Status field
        # For now, we'll implement this as a placeholder
        print(f"🔄 Would update project item {item_id} to status: {new_status}")
        print("⚠️  Project item status updates not yet implemented (requires field IDs)")
        return True

    def watch_project(
        self, project_url: str, repo_owner: str, repo_name: str, output_file: str = "tasks-from-project.json"
    ) -> None:
        """Watch project for changes and sync Todo items continuously.

        This method runs indefinitely, checking for changes every sync_config.watch_interval seconds.
        """
        print(f"👀 Watching GitHub Project: {project_url}")
        print(f"🔄 Check interval: {self.sync_config.watch_interval} seconds")
        print("Press Ctrl+C to stop watching")

        try:
            while True:
                try:
                    stats = self.sync_todo_items(project_url, repo_owner, repo_name, output_file)
                    if stats["created_count"] > 0:
                        print(f"🆕 Synced {stats['created_count']} new Todo items")
                    else:
                        print("✅ No new Todo items to sync")

                    time.sleep(self.sync_config.watch_interval)

                except KeyboardInterrupt:
                    print("\n👋 Stopping project watch")
                    break
                except Exception as e:
                    print(f"❌ Error during sync: {e}")
                    print(f"⏳ Retrying in {self.sync_config.watch_interval} seconds...")
                    time.sleep(self.sync_config.watch_interval)

        except KeyboardInterrupt:
            print("\n👋 Project watching stopped")

    def get_sync_status(self) -> Dict[str, Any]:
        """Get current sync status and statistics."""
        state = self._sync_state
        synced_items = state.get("synced_items", {})

        return {
            "last_sync": state.get("last_sync"),
            "project_url": state.get("project_url"),
            "repo": f"{state.get('repo_owner', '')}/{state.get('repo_name', '')}".strip("/"),
            "total_synced_items": len(synced_items),
            "sync_state_file": self.sync_config.sync_state_file,
            "target_statuses": list(self.sync_config.target_statuses),
            "status_mapping": self.sync_config.status_mapping,
        }

    def reset_sync_state(self) -> None:
        """Reset sync state (useful for debugging or re-syncing everything)."""
        self._sync_state = {
            "last_sync": None,
            "synced_items": {},
            "project_url": None,
            "repo_owner": None,
            "repo_name": None,
        }
        self._save_sync_state()
        print("✅ Sync state reset")


def main():
    """CLI entry point for testing."""
    import sys

    if len(sys.argv) < 4:
        print("Usage: python -m whilly.github_projects <project_url> <repo_owner> <repo_name>")
        print(
            "Example: python -m whilly.github_projects https://github.com/users/mshegolev/projects/4 mshegolev whilly-orchestrator"
        )
        sys.exit(1)

    converter = GitHubProjectsConverter()
    converter.project_to_whilly_tasks(sys.argv[1], sys.argv[2], sys.argv[3])


if __name__ == "__main__":
    main()
