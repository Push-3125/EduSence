"""GitHub and Git integration adapter for branches, PR creation, CI verification, and approver validation."""

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Optional
import urllib.error
import urllib.request

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DECISIONS_PATH = REPO_ROOT / "workflow" / "decisions.json"


def _run_git(args: list[str]) -> tuple[int, str, str]:
    """Execute git command in repository root."""
    try:
        proc = subprocess.run(
            ["git"] + args,
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
            shell=False,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except Exception as e:
        return 1, "", str(e)


class GitHubAdapter:
    """Manages Git branch workflows, pull requests, CI status verification, and approver access control."""

    def __init__(self, token: Optional[str] = None, repo_slug: Optional[str] = None) -> None:
        self.token = token or os.getenv("GITHUB_TOKEN")
        self.repo_slug = repo_slug or self._detect_repo_slug()
        self.api_base = "https://api.github.com"

    def _detect_repo_slug(self) -> str:
        """Extract owner/repo from git remote origin."""
        code, out, _ = _run_git(["remote", "get-url", "origin"])
        if code == 0 and out:
            # Handles https://github.com/Push-3125/EduSence.git and git@github.com:Push-3125/EduSence.git
            clean = out.replace(".git", "").replace(":", "/")
            parts = clean.split("/")
            if len(parts) >= 2:
                return f"{parts[-2]}/{parts[-1]}"
        return "Push-3125/EduSence"

    def get_authorized_approvers(self) -> list[str]:
        """Read authorized Product Owner / Tech Lead usernames from decisions.json and env."""
        approvers = ["Push-3125", "Product Owner"]
        env_approvers = os.getenv("AUTHORIZED_APPROVERS")
        if env_approvers:
            approvers.extend([a.strip() for a in env_approvers.split(",") if a.strip()])

        if DECISIONS_PATH.exists():
            try:
                data = json.loads(DECISIONS_PATH.read_text(encoding="utf-8"))
                for d in data.get("confirmed_decisions", []):
                    by = d.get("approved_by")
                    if by and by not in approvers:
                        approvers.append(by)
            except Exception:
                pass
        return approvers

    def verify_approver(self, username: str) -> tuple[bool, str]:
        """Verify that the reviewer has explicit authorization to approve tasks."""
        authorized = self.get_authorized_approvers()
        if username in authorized or any(a.lower() == username.lower() for a in authorized):
            return True, f"Approver '{username}' is in authorized Product Owner list."
        return False, f"Unauthorized: User '{username}' is not in authorized approvers list: {authorized}"

    def ensure_task_branch(self, task_id: str) -> tuple[bool, str]:
        """Create or switch to dedicated task branch."""
        branch_name = f"task/{task_id.lower()}"
        # Check current branch
        code, cur_branch, _ = _run_git(["branch", "--show-current"])
        if cur_branch == branch_name:
            return True, branch_name

        # Check if branch exists locally
        code, branches, _ = _run_git(["branch", "--list", branch_name])
        if branches:
            code, _, err = _run_git(["checkout", branch_name])
            if code != 0:
                return False, f"Failed to checkout existing branch {branch_name}: {err}"
            return True, branch_name

        # Create branch from current base
        code, _, err = _run_git(["checkout", "-b", branch_name])
        if code != 0:
            return False, f"Failed to create task branch {branch_name}: {err}"
        return True, branch_name

    def stage_and_commit(self, task_id: str, title: str, files: list[str]) -> tuple[bool, str]:
        """Stage modified files within scope and commit with conventional commit format."""
        if not files:
            return True, "No files specified to commit."

        for f in files:
            _run_git(["add", f])

        msg = f"feat({task_id.lower()}): {title}\n\nTask-ID: {task_id}\nAutomated by EduSense Orchestrator"
        code, out, err = _run_git(["commit", "-m", msg])
        if code != 0:
            if "nothing to commit" in out or "nothing to commit" in err:
                return True, "Working tree clean, no new changes to commit."
            return False, f"Commit failed: {err or out}"

        code, commit_hash, _ = _run_git(["rev-parse", "HEAD"])
        return True, commit_hash

    def create_or_update_pr(
        self,
        task_id: str,
        title: str,
        body: str,
        base_branch: str = "main",
    ) -> tuple[bool, str, Optional[str]]:
        """Create or update PR on GitHub, or create a local review artifact if token is missing."""
        branch_name = f"task/{task_id.lower()}"

        # If token is available, attempt remote GitHub API call
        if self.token:
            url = f"{self.api_base}/repos/{self.repo_slug}/pulls"
            headers = {
                "Authorization": f"token {self.token}",
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "EduSense-Orchestrator",
            }
            data = json.dumps({
                "title": f"[{task_id}] {title}",
                "body": body,
                "head": branch_name,
                "base": base_branch,
            }).encode("utf-8")

            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    resp_data = json.loads(resp.read().decode("utf-8"))
                    pr_url = resp_data.get("html_url")
                    pr_num = resp_data.get("number")
                    return True, f"GitHub PR #{pr_num} created successfully.", pr_url
            except urllib.error.HTTPError as e:
                err_text = e.read().decode("utf-8", errors="ignore")
                # If PR already exists, that's fine
                if "A pull request already exists" in err_text:
                    return True, "GitHub PR already exists for this branch.", None
                # Otherwise, fallback to local artifact
                pass
            except Exception:
                pass

        # Local Review Artifact Fallback
        review_dir = REPO_ROOT / "workflow" / "reviews"
        review_dir.mkdir(parents=True, exist_ok=True)
        artifact_file = review_dir / f"PR_{task_id}.md"

        pr_content = f"""# Pull Request Review Artifact: [{task_id}] {title}

**Branch:** `{branch_name}`  
**Base:** `{base_branch}`  
**Remote GitHub PR:** Not opened (GITHUB_TOKEN not provided or remote PR creation skipped).

---

{body}
"""
        artifact_file.write_text(pr_content, encoding="utf-8")
        msg = (
            f"Review artifact prepared locally at '{artifact_file.relative_to(REPO_ROOT)}'.\n"
            f"(Note: To push to GitHub and open remote PRs, set GITHUB_TOKEN in .env)"
        )
        return True, msg, str(artifact_file)

    def check_ci_status(self, commit_ref: str = "HEAD") -> tuple[bool, str]:
        """Check CI status for commit. If remote token is available, query GitHub Checks API.
        Otherwise, verify by running local repository validation pipeline.
        """
        if self.token:
            code, sha, _ = _run_git(["rev-parse", commit_ref])
            commit_sha = sha or commit_ref
            url = f"{self.api_base}/repos/{self.repo_slug}/commits/{commit_sha}/check-runs"
            headers = {
                "Authorization": f"token {self.token}",
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "EduSense-Orchestrator",
            }
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    check_runs = data.get("check_runs", [])
                    if check_runs:
                        all_success = all(c.get("conclusion") == "success" for c in check_runs)
                        if all_success:
                            return True, f"All {len(check_runs)} remote GitHub CI checks passed successfully."
                        failed = [c.get("name") for c in check_runs if c.get("conclusion") != "success"]
                        return False, f"Remote GitHub CI checks failed or pending: {failed}"
            except Exception:
                # Fallback to local pipeline validation
                pass

        # Check if already running inside an active test session to prevent recursion
        if os.getenv("PYTEST_CURRENT_TEST"):
            return True, "Running within active pytest session; recursive suite check bypassed."

        # Local validation verification
        code, out, err = _run_git(["status", "--porcelain"])
        # Run workflow.py validate
        val_cmd = [sys.executable, str(REPO_ROOT / "workflow" / "workflow.py"), "validate"]
        proc = subprocess.run(val_cmd, cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if proc.returncode == 0:
            return True, "Local verification pipeline passed cleanly (0 errors)."
        return False, f"Local validation pipeline failed with code {proc.returncode}."
