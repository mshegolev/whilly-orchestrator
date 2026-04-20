#!/usr/bin/env python3
"""
Example demonstrating the GitHub Projects status-oriented workflow.

This example shows how to use the new sync functionality programmatically.
"""

from whilly.github_projects import GitHubProjectsConverter, SyncConfig
import tempfile
from pathlib import Path


def example_sync_workflow():
    """Example of using the GitHub Projects sync workflow."""

    print("GitHub Projects Status-Oriented Workflow Example")
    print("=" * 50)

    # Create a temporary directory for the sync state
    with tempfile.TemporaryDirectory() as temp_dir:
        sync_state_file = Path(temp_dir) / "sync_state.json"

        # Configure sync for Todo items only
        sync_config = SyncConfig(
            target_statuses={"Todo"},
            sync_state_file=str(sync_state_file),
            watch_interval=30,  # Check every 30 seconds
        )

        # Create converter (this would normally check GitHub CLI auth)
        try:
            converter = GitHubProjectsConverter(sync_config=sync_config)
        except RuntimeError as e:
            print(f"⚠️  {e}")
            print("This example requires GitHub CLI authentication.")
            print("Run: gh auth login")
            return

        print("✅ GitHub Projects converter initialized")
        print(f"📁 Sync state file: {sync_state_file}")

        # Example project URL (replace with your own)
        project_url = "https://github.com/users/mshegolev/projects/4"
        repo_owner = "mshegolev"
        repo_name = "whilly-orchestrator"

        print(f"🔗 Project URL: {project_url}")
        print(f"📦 Repository: {repo_owner}/{repo_name}")

        # Show initial sync status
        print("\n📊 Initial Sync Status:")
        status = converter.get_sync_status()
        for key, value in status.items():
            print(f"  {key}: {value}")

        print("\n🔍 Available status mappings:")
        for project_status, whilly_label in sync_config.status_mapping.items():
            print(f"  {project_status} → {whilly_label}")

        # Note: Actual sync would require valid project and repo access
        print("\n📝 Example sync workflow:")
        print("1. Move items to 'Todo' status in your GitHub Project")
        print("2. Run sync: converter.sync_todo_items(project_url, repo_owner, repo_name)")
        print("3. Whilly creates Issues for new Todo items")
        print("4. Work on tasks using regular Whilly workflow")
        print("5. Update status: converter.sync_status_changes(issue_num, 'Done')")

        # Example of checking sync state after operations
        print("\n🔄 Sync configuration:")
        print(f"  Target statuses: {sync_config.target_statuses}")
        print(f"  Watch interval: {sync_config.watch_interval}s")
        print(f"  State file: {sync_config.sync_state_file}")


def example_cli_workflow():
    """Example CLI commands for the workflow."""

    print("\n🖥️  CLI Workflow Examples")
    print("=" * 30)

    project_url = "https://github.com/users/mshegolev/projects/4"
    repo_spec = "mshegolev/whilly-orchestrator"

    commands = [
        ("Initial sync of Todo items", f"whilly --sync-todo '{project_url}' --repo {repo_spec}"),
        ("Continuous monitoring", f"whilly --watch-project '{project_url}' --repo {repo_spec}"),
        ("Update item status", "whilly --sync-status 123 'In Progress'"),
        ("Check sync status", "whilly --project-sync-status"),
        ("Full conversion (original)", f"whilly --from-project '{project_url}' --repo {repo_spec}"),
    ]

    for description, command in commands:
        print(f"\n{description}:")
        print(f"  {command}")


if __name__ == "__main__":
    # Run the examples
    example_sync_workflow()
    example_cli_workflow()

    print("\n✅ Example completed!")
    print("\nTo use in practice:")
    print("1. Ensure GitHub CLI is authenticated: gh auth login")
    print("2. Replace project URL and repository with your own")
    print("3. Run sync commands as shown above")
