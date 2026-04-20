# GitHub Projects Status-Oriented Workflow

This document describes the enhanced GitHub Projects integration that supports status-oriented workflows with incremental syncing and monitoring.

## Overview

The status-oriented workflow allows you to:
1. **Sync only specific statuses** - Only process items in "Todo" status
2. **Incremental updates** - Only sync new or changed items 
3. **Status bidirectional sync** - Update Project items when Issues change
4. **Continuous monitoring** - Watch for changes and auto-sync

## Quick Start

### 1. Initial Sync of Todo Items

Sync only Todo items from a GitHub Project to Issues and Whilly tasks:

```bash
whilly --sync-todo "https://github.com/users/mshegolev/projects/4" --repo owner/name
```

This will:
- Fetch only items with "Todo" status from the project
- Create GitHub Issues for new items (with label `whilly:ready`)
- Generate Whilly tasks from the issues
- Track sync state to avoid duplicates

### 2. Continuous Monitoring

Monitor a project for new Todo items and sync them automatically:

```bash
whilly --watch-project "https://github.com/users/mshegolev/projects/4" --repo owner/name
```

This runs continuously and:
- Checks every 60 seconds for new Todo items
- Creates Issues and tasks for newly added items
- Skips items that haven't changed since last sync

### 3. Status Updates

Update a Project item status when an Issue changes:

```bash
whilly --sync-status 123 "In Progress"
```

This updates the corresponding Project item to "In Progress" status.

### 4. Check Sync Status

View current sync state and statistics:

```bash
whilly --project-sync-status
```

Shows:
- Last sync time
- Project URL and repository
- Number of synced items
- Target statuses and status mappings

## Status Mapping

The workflow maps GitHub Project statuses to Whilly labels:

| Project Status | Whilly Label | Description |
|---------------|--------------|-------------|
| Todo | `whilly:ready` | Ready for processing |
| In Progress | `whilly:in-progress` | Currently being worked on |
| Review | `whilly:review` | Under review |
| Done | `whilly:done` | Completed |
| Backlog | `whilly:backlog` | Future work |

## Workflow Examples

### Scenario 1: Manual Sync Workflow

1. User moves items to "Todo" in GitHub Project
2. Run sync command: `whilly --sync-todo PROJECT_URL --repo owner/name`
3. Whilly creates Issues and tasks for new Todo items
4. Work on tasks using regular Whilly workflow
5. Update status as needed: `whilly --sync-status ISSUE_NUMBER "Done"`

### Scenario 2: Continuous Monitoring Workflow

1. Start monitoring: `whilly --watch-project PROJECT_URL --repo owner/name`
2. User moves items to "Todo" in GitHub Project (in web interface)
3. Whilly automatically detects changes and creates Issues/tasks
4. Work proceeds automatically as new tasks appear

### Scenario 3: Hybrid Workflow

1. Use `--sync-todo` for initial batch of Todo items
2. Use `--watch-project` for ongoing monitoring
3. Use `--sync-status` for manual status updates when needed

## Configuration

### Custom Status Filtering

You can customize which statuses to sync by modifying the `SyncConfig`:

```python
from whilly.github_projects import GitHubProjectsConverter, SyncConfig

# Sync both Todo and In Progress items
sync_config = SyncConfig(target_statuses={"Todo", "In Progress"})
converter = GitHubProjectsConverter(sync_config=sync_config)
```

### Custom Watch Interval

Change how often the monitor checks for changes:

```python
# Check every 30 seconds instead of default 60
sync_config = SyncConfig(watch_interval=30)
```

### Custom Status Mapping

Override the default status-to-label mapping:

```python
sync_config = SyncConfig(
    status_mapping={
        "Todo": "my:ready",
        "In Progress": "my:working",
        # ... other mappings
    }
)
```

## State Management

The workflow maintains state in `.whilly_project_sync_state.json` to:
- Track which items have been synced
- Avoid creating duplicate Issues
- Enable incremental updates
- Store last sync timestamps

To reset the state (useful for debugging or re-syncing everything):

```bash
# Reset via CLI (not implemented yet, use file deletion)
rm .whilly_project_sync_state.json
```

## Error Handling

The sync commands handle various error cases:

- **Missing repository**: Auto-detects from git remote or requires `--repo` flag
- **Authentication failures**: Reports GitHub CLI auth issues clearly  
- **API rate limits**: Implements retry logic with exponential backoff
- **Network issues**: Continues monitoring with retry after errors
- **Duplicate items**: Skips items that are already synced and haven't changed

## Integration with Existing Workflows

### Backward Compatibility

The new status-oriented commands work alongside the existing `--from-project` command:

- `--from-project` - Converts ALL project items to Issues/tasks (original behavior)
- `--sync-todo` - Converts only Todo items with incremental sync (new behavior)
- `--watch-project` - Continuous monitoring of Todo items (new behavior)

### Combined Usage

You can combine approaches:

```bash
# Initial full conversion
whilly --from-project PROJECT_URL --repo owner/name

# Switch to incremental Todo-only sync
whilly --sync-todo PROJECT_URL --repo owner/name

# Enable continuous monitoring
whilly --watch-project PROJECT_URL --repo owner/name
```

## Troubleshooting

### Common Issues

**Issue**: "GitHub CLI not authenticated"
**Solution**: Run `gh auth login` and authenticate with GitHub

**Issue**: "Could not determine repository"  
**Solution**: Add `--repo owner/name` flag or run from a git repository

**Issue**: "No Todo items found"
**Solution**: Verify items have "Todo" status in the Project board

**Issue**: "Permission denied" errors
**Solution**: Ensure you have write access to the repository for creating Issues

### Debug Mode

For troubleshooting, you can inspect the sync state:

```bash
cat .whilly_project_sync_state.json | jq .
```

This shows:
- Last sync timestamp
- All synced items with their Issue numbers
- Project URL and repository information

## Advanced Usage

### Programmatic API

You can use the Python API directly for custom workflows:

```python
from whilly.github_projects import GitHubProjectsConverter, SyncConfig

# Custom configuration
sync_config = SyncConfig(
    target_statuses={"Todo", "Urgent"},
    status_mapping={"Todo": "priority:high"},
    watch_interval=30
)

converter = GitHubProjectsConverter(sync_config=sync_config)

# Sync Todo items
stats = converter.sync_todo_items(
    "https://github.com/users/mshegolev/projects/4",
    "owner", "repo"
)
print(f"Synced {stats['created_count']} items")

# Get status
status = converter.get_sync_status()
print(f"Last sync: {status['last_sync']}")

# Reset if needed
converter.reset_sync_state()
```

### Custom Workflows

The modular design allows building custom sync workflows:

```python
# Fetch items with custom filtering
items = converter.fetch_project_items(
    project_url,
    filter_statuses={"Todo", "High Priority"},
    include_updated_at=True
)

# Custom processing
for item in items:
    if item.priority == "high":
        # Create issue with high priority label
        converter._create_github_issue(item, owner, repo, "priority:high")
```