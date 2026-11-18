import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


def _step_script(job: str, name: str) -> str:
    workflow = yaml.safe_load(Path(".github/workflows/build.yml").read_text())
    steps = workflow["jobs"][job]["steps"]
    return next(step["run"] for step in steps if step.get("name") == name)


def _commit(repo: Path, content: str) -> str:
    (repo / "manifest.yml").write_text(content)
    subprocess.run(["git", "add", "manifest.yml"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            content,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _run_verification(tmp_path: Path, job: str, expected_sha: str):
    event_path = tmp_path / "event.json"
    event_path.write_text('{"inputs":{"callback_token":"test-token"}}')
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl_log = tmp_path / "curl.log"
    (bin_dir / "jq").write_text("#!/bin/sh\necho test-token\n")
    (bin_dir / "curl").write_text('#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$CURL_LOG"\n')
    (bin_dir / "jq").chmod(0o755)
    (bin_dir / "curl").chmod(0o755)
    env = {
        **os.environ,
        "CURL_LOG": str(curl_log),
        "CALLBACK_URL": "https://callback.invalid",
        "EXPECTED_SUBMISSION_SHA": expected_sha,
        "GITHUB_EVENT_PATH": str(event_path),
        "GITHUB_WORKSPACE": str(tmp_path),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
    }
    result = subprocess.run(
        [
            "bash",
            "-euo",
            "pipefail",
            "-c",
            _step_script(job, "Verify submission checkout"),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, curl_log


@pytest.mark.parametrize("job", ["validate-manifest", "build"])
def test_workflow_accepts_matching_checkout(tmp_path, job):
    repo = tmp_path / "work"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    expected_sha = _commit(repo, "first")

    result, curl_log = _run_verification(tmp_path, job, expected_sha)

    assert result.returncode == 0, result.stderr
    if job == "validate-manifest":
        assert expected_sha in curl_log.read_text()
    else:
        assert not curl_log.exists()


@pytest.mark.parametrize("job", ["validate-manifest", "build"])
def test_workflow_rejects_pr_ref_that_moves_before_checkout(tmp_path, job):
    repo = tmp_path / "work"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    expected_sha = _commit(repo, "first")
    actual_sha = _commit(repo, "second")

    result, curl_log = _run_verification(tmp_path, job, expected_sha)

    assert result.returncode != 0
    assert (
        f"Checkout SHA {actual_sha} does not match expected submission SHA {expected_sha}"
        in result.stdout
    )
    assert not curl_log.exists()


@pytest.mark.parametrize("workflow", ["build", "merge", "reprocheck"])
def test_dispatch_workflow_scripts_are_static(workflow):
    jobs = yaml.safe_load(Path(f".github/workflows/{workflow}.yml").read_text())["jobs"]
    for job in jobs.values():
        for step in job["steps"]:
            assert "${{" not in step.get("run", ""), step.get("name", step.get("id"))


@pytest.mark.parametrize("job", ["build", "validate-manifest"])
@pytest.mark.parametrize("separator", ["\r", "\n"])
def test_multiline_detected_id_is_not_exported(tmp_path, job, separator):
    work = tmp_path / "work"
    work.mkdir()
    (work / "app_id").write_bytes(f"org.example.App{separator}SECOND=value\n".encode())
    env_file = tmp_path / "actions env"
    env_file.touch()
    result = subprocess.run(
        [
            "bash",
            "-eu",
            "-c",
            "docker() { :; }\n"
            + _step_script(job, "Detect app ID of the new submission"),
        ],
        env={
            **os.environ,
            "GITHUB_WORKSPACE": str(tmp_path),
            "GITHUB_ENV": str(env_file),
        },
        check=False,
        capture_output=True,
    )
    assert result.returncode != 0
    assert env_file.read_bytes() == b""


@pytest.mark.parametrize(
    "token", ["", "valid-token", "first\nsecond=value", "first\rsecond"]
)
def test_upload_token_env_record(tmp_path, token):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"inputs": {"flat_manager_token": token}}))
    env_file = tmp_path / "actions env"
    env_file.touch()
    result = subprocess.run(
        ["bash", "-eu", "-c", _step_script("build", "Set upload token")],
        env={
            **os.environ,
            "GITHUB_EVENT_PATH": str(event),
            "GITHUB_ENV": str(env_file),
        },
        check=False,
        capture_output=True,
    )
    valid = "\r" not in token and "\n" not in token
    assert (result.returncode == 0) == valid
    assert env_file.read_text() == (f"REPO_TOKEN={token}\n" if valid and token else "")


@pytest.mark.skipif(not shutil.which("just"), reason="checkout recipe requires just")
def test_checkout_preserves_literal_ref(tmp_path):
    source = tmp_path / "source repo"
    subprocess.run(["git", "init", str(source)], check=True)
    sha = _commit(source, "release")
    ref = "refs/heads/branch/release'$VERSION"
    subprocess.run(["git", "update-ref", ref, sha], cwd=source, check=True)
    work = tmp_path / "work"
    work.mkdir()
    shutil.copy("justfile", work / ".flathub.justfile")
    # Run the actual nested checkout command locally, without a container.
    docker_stub = 'docker() { while [[ "$1" != /bin/bash ]]; do shift; done; "$@"; }\n'
    subprocess.run(
        [
            "bash",
            "-eu",
            "-c",
            docker_stub + _step_script("build", "Checkout repository"),
        ],
        cwd=work,
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "GITHUB_WORKSPACE": str(tmp_path),
            "REPO": str(source),
            "REF": ref,
        },
        check=True,
    )
    assert (
        subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=work, text=True
        ).strip()
        == sha
    )
