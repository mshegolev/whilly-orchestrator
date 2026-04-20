"""
GitHub Projects v2 integration for Whilly.
Converts Project board items to GitHub Issues for whilly processing.
"""

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Dict, Any
from urllib.parse import urlparse

from whilly.config import WhillyConfig


@dataclass
class ProjectItem:
    """Single item from GitHub Project board."""
    id: str
    title: str
    body: str = ""
    status: str = "Todo"
    priority: str = "medium"
    labels: List[str] = None
    assignee: Optional[str] = None
    url: Optional[str] = None

    def __post_init__(self):
        if self.labels is None:
            self.labels = []


class GitHubProjectsConverter:
    """Converts GitHub Project board items to Issues and Whilly tasks."""

    def __init__(self, config: WhillyConfig = None):
        self.config = config or WhillyConfig.from_env()
        self._check_gh_cli()

    def _check_gh_cli(self):
        """Verify GitHub CLI is available and authenticated."""
        try:
            result = subprocess.run(['gh', 'auth', 'status'],
                                  capture_output=True, text=True, check=True)
            if 'Logged in to github.com' not in result.stderr:
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
            r'github\.com/users/([^/]+)/projects/(\d+)',
            r'github\.com/orgs/([^/]+)/projects/(\d+)',
            r'github\.com/([^/]+)/([^/]+)/projects/(\d+)'
        ]

        for pattern in patterns:
            match = re.search(pattern, project_url)
            if match:
                if len(match.groups()) == 2:  # users/orgs format
                    owner = match.group(1)
                    project_number = match.group(2)
                    return {
                        'owner': owner,
                        'project_number': int(project_number),
                        'type': 'user' if '/users/' in project_url else 'org',
                        'repo': None
                    }
                else:  # repo format
                    owner = match.group(1)
                    repo = match.group(2)
                    project_number = match.group(3)
                    return {
                        'owner': owner,
                        'repo': repo,
                        'project_number': int(project_number),
                        'type': 'repo'
                    }

        raise ValueError(f"Invalid GitHub Project URL format: {project_url}")

    def fetch_project_items(self, project_url: str) -> List[ProjectItem]:
        """Fetch items from GitHub Project board using GraphQL."""

        project_info = self.parse_project_url(project_url)

        # GraphQL query to fetch project items
        query = '''
        query($owner: String!, $number: Int!) {
          user(login: $owner) {
            projectV2(number: $number) {
              items(first: 100) {
                nodes {
                  id
                  fieldValues(first: 20) {
                    nodes {
                      ... on ProjectV2ItemFieldTextValue {
                        text
                        field {
                          ... on ProjectV2FieldCommon {
                            name
                          }
                        }
                      }
                      ... on ProjectV2ItemFieldSingleSelectValue {
                        name
                        field {
                          ... on ProjectV2FieldCommon {
                            name
                          }
                        }
                      }
                    }
                  }
                  content {
                    ... on DraftIssue {
                      title
                      body
                    }
                    ... on Issue {
                      title
                      body
                      url
                      number
                    }
                    ... on PullRequest {
                      title
                      body
                      url
                      number
                    }
                  }
                }
              }
            }
          }
        }
        '''

        try:
            # Execute GraphQL query via gh CLI
            cmd = [
                'gh', 'api', 'graphql',
                '-f', f'query={query}',
                '-F', f'owner={project_info["owner"]}',
                '-F', f'number={project_info["project_number"]}'
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            data = json.loads(result.stdout)

            return self._parse_project_items(data)

        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to fetch project items: {e.stderr}")
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Invalid JSON response from GitHub API: {e}")

    def _parse_project_items(self, data: Dict[str, Any]) -> List[ProjectItem]:
        """Parse GraphQL response into ProjectItem objects."""

        items = []
        project_items = data.get('data', {}).get('user', {}).get('projectV2', {}).get('items', {}).get('nodes', [])

        for item_data in project_items:
            content = item_data.get('content', {})

            # Skip if no content (empty project item)
            if not content:
                continue

            title = content.get('title', 'Untitled')
            body = content.get('body', '')
            url = content.get('url')

            # Extract field values (Status, Priority, etc.)
            status = "Todo"
            priority = "medium"

            field_values = item_data.get('fieldValues', {}).get('nodes', [])
            for field in field_values:
                field_name = field.get('field', {}).get('name', '').lower()

                if field_name == 'status':
                    status = field.get('name', status)
                elif field_name == 'priority':
                    priority = field.get('name', priority).lower()
                elif field_name in ['title']:
                    if 'text' in field:
                        title = field['text'] or title

            # Create ProjectItem
            item = ProjectItem(
                id=item_data['id'],
                title=title,
                body=body,
                status=status,
                priority=priority,
                url=url
            )

            items.append(item)

        return items

    def convert_items_to_issues(self, items: List[ProjectItem],
                               repo_owner: str, repo_name: str,
                               label: str = "whilly:ready") -> List[Dict[str, Any]]:
        """Convert Project items to GitHub Issues."""

        created_issues = []

        for item in items:
            # Skip if already an issue (has URL with /issues/)
            if item.url and '/issues/' in item.url:
                print(f"⏭️  Skipping {item.title} - already an issue")
                continue

            try:
                # Create GitHub Issue
                issue_data = self._create_github_issue(
                    item, repo_owner, repo_name, label
                )
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
            'gh', 'issue', 'create',
            '--repo', f'{owner}/{repo}',
            '--title', item.title,
            '--body', body,
            '--label', label
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        issue_url = result.stdout.strip()

        # Extract issue number from URL
        issue_number = int(issue_url.split('/')[-1])

        return {
            'title': item.title,
            'body': body,
            'url': issue_url,
            'number': issue_number,
            'labels': [label]
        }

    def project_to_whilly_tasks(self, project_url: str,
                               repo_owner: str, repo_name: str,
                               output_file: str = "tasks-from-project.json",
                               label: str = "whilly:ready") -> str:
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
        from whilly.github_converter import GitHubIssuesSource

        print(f"🔄 Generating Whilly tasks from issues with label: {label}")
        source = GitHubIssuesSource()
        source.generate_plan_from_labels([label], output_file)

        print(f"✅ Whilly tasks saved to: {output_file}")
        return output_file


def main():
    """CLI entry point for testing."""
    import sys

    if len(sys.argv) < 4:
        print("Usage: python -m whilly.github_projects <project_url> <repo_owner> <repo_name>")
        print("Example: python -m whilly.github_projects https://github.com/users/mshegolev/projects/4 mshegolev whilly-orchestrator")
        sys.exit(1)

    converter = GitHubProjectsConverter()
    converter.project_to_whilly_tasks(sys.argv[1], sys.argv[2], sys.argv[3])


if __name__ == "__main__":
    main()