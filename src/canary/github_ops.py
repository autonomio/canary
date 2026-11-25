"""GitHub ops: issues for journaling, PRs with auto-merge for patches.

The worker authenticates with a fine-grained PAT scoped to its own repo only
(contents + issues + pull requests). Token arrives via GITHUB_TOKEN env and is
never written to journals, issues, or logs.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import httpx

API = "https://api.github.com"
DEFAULT_REPO = "mikkokotila/Canary"
UPSTREAM_REPO = "autonomio/canary"
UPSTREAM_REVIEWER_ID = 265471408  # bit-mis
BOT_NAME = "canary-bot"
BOT_EMAIL = "canary-bot@users.noreply.github.com"
COMMIT_STAGE_PREFIXES = ("src/canary/", "runs/")


def run_git(
    args: list[str],
    cwd: str | Path | None = None,
    timeout: float = 30,
    check: bool = False,
    stdin=None,
) -> subprocess.CompletedProcess[str]:
    """Run a git command without leaving zombie child processes.

    Uses ``Popen`` as a context-manager plus ``communicate(timeout=...)``
    with an explicit ``wait()`` in a ``finally`` block so the child is
    always reaped, even on timeouts or other errors.
    """
    if stdin is None:
        stdin = subprocess.DEVNULL
    with subprocess.Popen(
        args,
        cwd=cwd,
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as proc:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except Exception:
                stdout, stderr = "", ""
            raise
        except BaseException:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.communicate(timeout=5)
            except Exception:
                pass
            raise
        finally:
            try:
                proc.wait()
            except Exception:
                pass
        rc = proc.returncode
    completed = subprocess.CompletedProcess(args, rc, stdout, stderr)
    if check and rc != 0:
        raise subprocess.CalledProcessError(rc, args, output=stdout, stderr=stderr)
    return completed


def resolve_token(env: dict | None = None) -> str:
    env = env if env is not None else os.environ
    token = env.get("GITHUB_TOKEN", "")
    if not token:
        raise RuntimeError("no GITHUB_TOKEN in environment")
    return token


def resolve_repo(repo_root: str | Path | None = None) -> str:
    if os.environ.get("GITHUB_REPO"):
        return os.environ["GITHUB_REPO"]
    if repo_root is not None:
        r = run_git(["git", "remote", "get-url", "origin"], cwd=repo_root, timeout=30)
        if r.returncode == 0:
            m = re.search(r"github\.com[/:]([^/\s]+/[^/\s]+?)(?:\.git)?\s*$", r.stdout.strip())
            if m:
                return m.group(1)
    return DEFAULT_REPO


@dataclass(frozen=True)
class Issue:
    number: int
    url: str


@dataclass(frozen=True)
class Pull:
    number: int
    url: str
    node_id: str
    head_sha: str


class GitHub:
    def __init__(self, token: str, repo: str, client: httpx.Client | None = None) -> None:
        self.repo = repo
        self._client = client or httpx.Client(
            base_url=API,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "Canary",
            },
            timeout=30.0,
        )

    def _post(self, path: str, payload: dict) -> dict:
        return self._result(self._client.post(path, json=payload))

    @staticmethod
    def _result(resp: httpx.Response):
        if resp.status_code == 401:
            raise RuntimeError("GitHub token rejected (401) — check GITHUB_TOKEN scope/expiry")
        resp.raise_for_status()
        return resp.json()

    def _get(self, path: str) -> dict:
        return self._result(self._client.get(path))

    def create_issue(self, title: str, body: str) -> Issue:
        d = self._post(f"/repos/{self.repo}/issues", {"title": title, "body": body})
        return Issue(number=d["number"], url=d["html_url"])

    def comment(self, number: int, body: str) -> None:
        self._post(f"/repos/{self.repo}/issues/{number}/comments", {"body": body})

    def create_pr(self, title: str, body: str, head: str, base: str = "main") -> Pull:
        d = self._post(f"/repos/{self.repo}/pulls", {"title": title, "body": body, "head": head, "base": base})
        return Pull(number=d["number"], url=d["html_url"], node_id=d["node_id"], head_sha=d["head"]["sha"])

    def get_pr(self, number: int) -> dict:
        return self._get(f"/repos/{self.repo}/pulls/{number}")

    def reviews(self, number: int) -> list[dict]:
        reviews = []
        page = 1
        while True:
            items = self._get(f"/repos/{self.repo}/pulls/{number}/reviews?per_page=100&page={page}")
            if not isinstance(items, list):
                return []
            reviews.extend(items)
            if len(items) < 100:
                return reviews
            page += 1

    def independently_approved(self, number: int, author: str | None, head_sha: str | None = None,
                               *, reviewer_id: int | None = None) -> bool:
        """Require current-head approval by a non-author with repository write access."""
        if not isinstance(author, str) or not author:
            return False
        try:
            if head_sha is None:
                head_sha = self.get_pr(number)["head"]["sha"]
            if not isinstance(head_sha, str) or not head_sha:
                return False
            decisions = {}
            # GitHub returns reviews in chronological order. Comments and drafts
            # do not replace the reviewer's last approval or objection.
            for review in self.reviews(number):
                if not isinstance(review, dict):
                    continue
                user = review.get("user")
                login = user.get("login") if isinstance(user, dict) else None
                if not isinstance(login, str) or not login or login.casefold() == author.casefold():
                    continue
                if reviewer_id is not None and user.get("id") != reviewer_id:
                    continue
                state = review.get("state")
                if state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
                    decisions[login.casefold()] = (login, state, review.get("commit_id"))
            for login, state, commit in decisions.values():
                if state != "APPROVED" or commit != head_sha:
                    continue
                reviewer = quote(login, safe="")
                try:
                    access = self._get(f"/repos/{self.repo}/collaborators/{reviewer}/permission")
                except httpx.HTTPStatusError as error:
                    if error.response.status_code != 404:
                        raise
                    continue
                if isinstance(access, dict) and access.get("permission") in ("write", "maintain", "admin"):
                    return True
        except (httpx.HTTPError, RuntimeError, KeyError, TypeError, ValueError):
            return False
        return False

    def check_runs(self, sha: str) -> list[dict]:
        d = self._get(f"/repos/{self.repo}/commits/{sha}/check-runs")
        return d.get("check_runs", [])

    def enable_automerge(self, node_id: str) -> bool:
        """Best-effort auto-merge flag; False when unsupported. Never raises."""
        try:
            d = self._post("/graphql", {
                "query": "mutation($id:ID!){enablePullRequestAutoMerge(input:{pullRequestId:$id,mergeMethod:SQUASH}){pullRequest{autoMergeRequest{enabledAt}}}}",
                "variables": {"id": node_id},
            })
            req = (d.get("data") or {}).get("enablePullRequestAutoMerge") or {}
            return bool(((req.get("pullRequest") or {}).get("autoMergeRequest") or {}).get("enabledAt"))
        except Exception:
            return False

    def merge(self, number: int, *, head_sha: str | None = None) -> bool:
        payload = {"merge_method": "squash"}
        if head_sha is not None:
            payload["sha"] = head_sha
        try:
            d = self._result(self._client.put(f"/repos/{self.repo}/pulls/{number}/merge", json=payload))
            return bool(d.get("merged"))
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 405 or (head_sha is not None and e.response.status_code == 409):
                return False
            raise

    def delete_branch(self, branch: str) -> None:
        resp = self._client.delete(f"/repos/{self.repo}/git/refs/heads/{branch}")
        if resp.status_code not in (204, 404, 422):
            resp.raise_for_status()

    def wait_and_merge(self, pr: Pull, timeout_s: float = 900, interval_s: float = 20) -> str:
        """Merge a maintenance PR only after checks and independent approval.

        Keep this decision in the publisher. GitHub's auto-merge queue cannot
        enforce the publisher's review policy on downstream repositories.
        """
        reviewer_id = UPSTREAM_REVIEWER_ID if self.repo.casefold() == UPSTREAM_REPO else None
        deadline = time.monotonic() + timeout_s
        unapproved = False
        while time.monotonic() < deadline:
            state = self.get_pr(pr.number)
            if state.get("merged"):
                return "merged"
            if state.get("state") == "closed":
                raise RuntimeError(f"PR #{pr.number} closed without merge")
            head_sha = state["head"]["sha"]
            runs = self.check_runs(head_sha)
            if runs and all(r.get("status") == "completed" for r in runs):
                bad = [r for r in runs if r.get("conclusion") not in ("success", "neutral", "skipped")]
                if bad:
                    names = ", ".join(r.get("name", "?") for r in bad)
                    raise RuntimeError(f"PR #{pr.number} checks failed: {names}")
                author = state.get("user")
                login = author.get("login") if isinstance(author, dict) else None
                if not self.independently_approved(pr.number, login, head_sha, reviewer_id=reviewer_id):
                    unapproved = True
                    time.sleep(interval_s)
                    continue
                unapproved = False
                if self.merge(pr.number, head_sha=head_sha):
                    return "merged"
            time.sleep(interval_s)
        if unapproved:
            reviewer = "bit-mis" if reviewer_id is not None else "a reviewer"
            raise RuntimeError(
                f"PR #{pr.number} has no independent approval for its current commit "
                f"from {reviewer} with repository write access")
        raise RuntimeError(f"PR #{pr.number} did not merge within {timeout_s}s")


def commit_stage_list(repo: Path) -> list[str]:
    """Only code patches and run artifacts may ride the auto-PR. Nothing else."""
    r = run_git(
        ["git", "status", "--porcelain", "--", "src", "runs", "tests"],
        cwd=repo,
        timeout=30,
        check=True,
    )
    files: list[str] = []
    for line in r.stdout.splitlines():
        path = line[3:].strip().strip('"')
        if path.startswith(COMMIT_STAGE_PREFIXES):
            files.append(path)
        elif path:
            raise RuntimeError(f"refusing auto-commit with unexpected change: {path}")
    return files


def commit_and_push(repo: str | Path, branch: str, message: str, token: str) -> list[str]:
    from .revise import require_main_branch, require_revision_trust

    require_revision_trust()
    require_main_branch(repo)
    repo = Path(repo)
    files = commit_stage_list(repo)
    if not files:
        raise RuntimeError("nothing to commit")
    base = ["git", "-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}"]
    run_git(base + ["checkout", "-b", branch], cwd=repo, timeout=60, check=True)
    run_git(base + ["add", "--"] + files, cwd=repo, timeout=60, check=True)
    run_git(base + ["commit", "-m", message], cwd=repo, timeout=60, check=True)
    push = run_git(
        ["git", "-c", f"http.extraheader=AUTHORIZATION: bearer {token}", "push", "-u", "origin", branch],
        cwd=repo,
        timeout=300,
    )
    if push.returncode != 0:
        raise RuntimeError(f"push failed: {push.stderr.strip()[-300:]}")
    return files
