"""Publish local MPSC audit outputs to the configured GitHub repository.

Authentication is read only from MPSC_GITHUB_TOKEN (or GITHUB_TOKEN).
Never put a token in source code, command-line arguments, or the repository.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

DEFAULT_REPOSITORY = "Pratik03538/mpscstudy"
DEFAULT_BRANCH = "main"
MAX_FILE_BYTES = 90 * 1024 * 1024


def _github_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "MPSCStudy-Audit-Report-Sync",
    }


def _upload_file(
    session: requests.Session,
    repo: str,
    branch: str,
    token: str,
    local_path: Path,
    remote_path: str,
) -> dict[str, Any]:
    size = local_path.stat().st_size
    if size > MAX_FILE_BYTES:
        return {
            "file": local_path.name,
            "path": remote_path,
            "size_bytes": size,
            "status": "skipped_too_large",
            "message": f"File exceeds configured {MAX_FILE_BYTES // (1024 * 1024)} MB upload limit.",
        }

    api_url = f"https://api.github.com/repos/{repo}/contents/{remote_path}"
    headers = _github_headers(token)
    existing = session.get(api_url, headers=headers, params={"ref": branch}, timeout=45)
    if existing.status_code == 404:
        existing_sha = None
    elif existing.ok:
        payload = existing.json()
        existing_sha = payload.get("sha")
        if not existing_sha:
            return {
                "file": local_path.name, "path": remote_path,
                "size_bytes": size, "status": "failed",
                "message": "GitHub did not return an existing file SHA.",
            }
    else:
        return {
            "file": local_path.name, "path": remote_path,
            "size_bytes": size, "status": "failed",
            "message": f"GitHub lookup failed: HTTP {existing.status_code}: {existing.text[:500]}",
        }

    content_b64 = base64.b64encode(local_path.read_bytes()).decode("ascii")
    body: dict[str, Any] = {
        "message": f"Publish MPSC audit output: {local_path.name}",
        "content": content_b64,
        "branch": branch,
    }
    if existing_sha:
        body["sha"] = existing_sha

    uploaded = session.put(api_url, headers=headers, json=body, timeout=120)
    if not uploaded.ok:
        return {
            "file": local_path.name, "path": remote_path,
            "size_bytes": size, "status": "failed",
            "message": f"GitHub upload failed: HTTP {uploaded.status_code}: {uploaded.text[:700]}",
        }
    result = uploaded.json()
    return {
        "file": local_path.name,
        "path": remote_path,
        "size_bytes": size,
        "status": "uploaded",
        "commit_sha": (result.get("commit") or {}).get("sha"),
        "html_url": (result.get("content") or {}).get("html_url"),
    }


def sync_audit_outputs(
    output_dir: Path,
    structured_database: Path | None = None,
    repository: str = DEFAULT_REPOSITORY,
    branch: str = DEFAULT_BRANCH,
) -> dict[str, Any]:
    """Upload latest reports and the normalized SQLite DB to GitHub.

    The source collection DB is never uploaded by this function. Only generated
    audit outputs and the newly built structured DB are selected.
    """
    token = os.getenv("MPSC_GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN")
    manifest_path = output_dir / "github_sync_manifest.json"
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    report_files = sorted(
        path for path in output_dir.rglob("*")
        if path.is_file()
        and path.name != manifest_path.name
        and path.suffix.lower() in {".csv", ".json", ".jsonl"}
    )
    selected = list(report_files)
    if structured_database and structured_database.exists():
        selected.append(structured_database)

    if not token:
        result = {
            "generated_at": generated_at,
            "status": "skipped_missing_token",
            "repository": repository,
            "branch": branch,
            "uploaded_count": 0,
            "failed_count": 0,
            "message": (
                "Set MPSC_GITHUB_TOKEN in the Windows user environment, then rerun "
                "python audit_mpsc_dataset.py to sync reports."
            ),
            "files": [],
        }
        manifest_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("[GITHUB SYNC] Skipped: MPSC_GITHUB_TOKEN is not set.", flush=True)
        return result

    session = requests.Session()
    results: list[dict[str, Any]] = []
    try:
        # Validate credentials and repository access before sending any files.
        repo_check = session.get(
            f"https://api.github.com/repos/{repository}",
            headers=_github_headers(token),
            timeout=30,
        )
        if not repo_check.ok:
            raise RuntimeError(
                f"GitHub repository access failed: HTTP {repo_check.status_code}: "
                f"{repo_check.text[:700]}"
            )
        for local_path in selected:
            if local_path.resolve() == manifest_path.resolve():
                continue
            remote_path = (
                "mpsc_output/audit_reports/latest/"
                + local_path.name
            )
            result = _upload_file(
                session, repository, branch, token, local_path, remote_path
            )
            results.append(result)
            print(
                f"[GITHUB SYNC] {result['status'].upper()} "
                f"{local_path.name} -> {remote_path}",
                flush=True,
            )
    finally:
        session.close()

    uploaded_count = sum(item["status"] == "uploaded" for item in results)
    failed_count = sum(item["status"] != "uploaded" for item in results)
    manifest = {
        "generated_at": generated_at,
        "status": "success" if failed_count == 0 else "partial",
        "repository": repository,
        "branch": branch,
        "remote_directory": "mpsc_output/audit_reports/latest/",
        "uploaded_count": uploaded_count,
        "failed_count": failed_count,
        "files": results,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Upload the manifest last so it records the status of every preceding file.
    try:
        session = requests.Session()
        manifest_result = _upload_file(
            session,
            repository,
            branch,
            token,
            manifest_path,
            "mpsc_output/audit_reports/latest/github_sync_manifest.json",
        )
        if manifest_result["status"] != "uploaded":
            print(
                f"[GITHUB SYNC] Manifest upload failed: {manifest_result.get('message')}",
                flush=True,
            )
    finally:
        session.close()

    print(
        f"[GITHUB SYNC DONE] uploaded={uploaded_count} | "
        f"failed_or_skipped={failed_count} | repo={repository}/{branch}",
        flush=True,
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync already-generated MPSC audit reports and structured DB to GitHub without rerunning the audit."
    )
    project_dir = Path(__file__).resolve().parent
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=project_dir / "mpsc_output" / "data_quality_audit",
        help="Directory containing the existing audit CSV/JSON/JSONL outputs.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=project_dir / "mpsc_output" / "mpsc_questions_structured.db",
        help="Path to the generated normalized SQLite database.",
    )
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY)
    parser.add_argument("--branch", default=DEFAULT_BRANCH)
    args = parser.parse_args()

    if not args.reports_dir.exists():
        parser.error(f"Reports directory does not exist: {args.reports_dir}")
    if not args.db.exists():
        parser.error(f"Structured database does not exist: {args.db}")

    result = sync_audit_outputs(
        output_dir=args.reports_dir,
        structured_database=args.db,
        repository=args.repository,
        branch=args.branch,
    )
    if result.get("status") == "skipped_missing_token":
        return 2
    return 0 if result.get("failed_count", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
