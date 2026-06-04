# Project: `gitlab-component-manager` (gcm)

## Mission

Build a minimal Python CLI tool that acts as a cross-GitLab-instance CI/CD component manager — a polyfill for GitLab's missing cross-instance `include: component:` functionality.

The tool parses annotation comments in `.gitlab-ci.yml`, downloads the referenced component files from a (possibly private) remote GitLab instance via its API, and writes them into `.gitlab-ci/<org>/` in the customer's repository. A manual CI job then commits and pushes those vendored files.

---

## Hard Constraints

- **Python only.** Single-file entry point: `gcm.py`. Supporting modules are allowed but the public surface must be tiny.
- **Minimalism above all.** No frameworks (no Click, no Typer). Use `argparse` + `sys` + `os` + `pathlib` + `subprocess` + `urllib.request` (stdlib only). The only allowed third-party dependency is `requests` (for HTTP).
- **TDD.** Write tests first for every behaviour, then implementation. Use `pytest` only (no other test frameworks). 100% line coverage enforced via `pytest-cov`. Every public function must have at least one test.
- **Git via subprocess.** All git operations (commit, push, status, diff) call the system `git` binary via `subprocess.run`. No `GitPython` or similar.
- **Auth via ENV vars only.** Never accept tokens as CLI arguments. Read them from env vars documented in the README.
- **No lock file complexity in v1.** Simple: annotation is the pin. Each pull overwrites the local file with the exact version requested.

---

## Annotation Language

Annotations are structured YAML comments in `.gitlab-ci.yml`. Each managed component is declared with a comment block **directly above** the `local:` include that references it:

```yaml
# gcm:source=https://gitlab.your-org.com/ci-components/docker
# gcm:version=1.2.0
# gcm:component=build/build.yml
include:
  - local: .gitlab-ci/your-org/build.yml
```

Rules:
- Prefix is always `# gcm:` (lowercase, single space after `#`)
- Required keys: `source` (repo URL, no `.git` suffix), `version` (git tag or branch), `component` (path inside repo to the YAML file)
- Optional key: `sha` (exact commit SHA for pinning; if present, overrides `version` for the actual download but `version` is still required for human readability)
- Annotations must be contiguous — no blank lines between annotation lines or between the last annotation and the `- local:` line
- A single `.gitlab-ci.yml` may contain multiple annotated includes

---

## Project Structure

```
gitlab-component-manager/
├── gcm.py                  # CLI entry point + all core logic
├── tests/
│   └── test_gcm.py         # All tests
├── Dockerfile
├── .github/
│   └── workflows/
│       └── docker.yml      # Build & push to ghcr.io
├── requirements.txt        # requests only
├── requirements-dev.txt    # pytest, pytest-cov
└── README.md
```

---

## CLI Interface

```
gcm [--ci-file PATH] [--components-dir PATH] COMMAND

Commands:
  pull    Download all annotated components to the local components dir
  list    Print all annotated components and their versions (no download)
  diff    Show which components would change (dry run of pull)

Options:
  --ci-file PATH          Path to .gitlab-ci.yml (default: .gitlab-ci.yml)
  --components-dir PATH   Directory to write components into (default: .gitlab-ci)
```

No subcommand = print help and exit 1.

---

## Core Logic Breakdown

### 1. Parsing (`parse_annotations(text: str) -> list[dict]`)

- Input: raw string content of `.gitlab-ci.yml`
- Output: list of dicts, each with keys: `source`, `version`, `component`, `sha` (optional), `local_path` (the `- local:` value on the line immediately after the annotation block)
- Must raise `AnnotationError` (custom exception) with a descriptive message if:
  - A `gcm:` annotation block is missing a required key
  - The line after the annotation block is not a `- local:` line
  - An unknown `gcm:` key is encountered

### 2. Downloading (`download_component(source: str, version: str, component: str, sha: str | None, token: str | None) -> str`)

- Uses GitLab Repository Files API:
  `GET /api/v4/projects/{encoded_path}/repository/files/{encoded_file_path}/raw?ref={ref}`
- `ref` = `sha` if provided, else `version`
- `token` is read from env var `GCM_TOKEN` (caller must pass it in; function must not read env directly — keep it pure/testable)
- Returns the raw file content as a string
- Raises `DownloadError` on non-200 response (include status code and URL in message)
- Must work with both `https://` URLs (calls API) — no SSH support needed

### 3. Pull (`cmd_pull(args, env: dict) -> int`)

- Parses annotations
- For each component: downloads, writes to `local_path` (relative to cwd), creates parent dirs
- Prints `[pull] your-org/build.yml @ 1.2.0` for each
- Returns 0 on success, 1 on any error (print error, continue with remaining components, return 1 at end)

### 4. List (`cmd_list(args, env: dict) -> int`)

- Parses annotations, prints a table:
  ```
  source                                      component          version   local_path
  https://gitlab.your-org.com/ci-components   build/build.yml    1.2.0     .gitlab-ci/your-org/build.yml
  ```
- Returns 0

### 5. Diff (`cmd_diff(args, env: dict) -> int`)

- For each annotation: downloads the remote content, compares to local file (if exists)
- Prints `[up-to-date]`, `[changed]`, or `[new]` per component
- Does **not** write anything
- Returns 0 if all up-to-date, 1 if any changed or new

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `GCM_TOKEN` | No | GitLab Personal/Project Access Token for private repos |

Token is optional at the function signature level — unauthenticated requests are attempted if absent (works for public repos).

---

## GitLab CI Integration

Add this job to the customer's `.gitlab-ci.yml` (document it in the README):

```yaml
sync-ci-components:
  stage: .pre
  image: ghcr.io/<github-username>/gitlab-component-manager:latest
  when: manual
  script:
    - gcm diff                        # show what will change
    - gcm pull                        # download components
    - git config user.email "gcm-bot@your-org.com"
    - git config user.name "GCM Bot"
    - git add .gitlab-ci/
    - git diff --cached --stat
    - git commit -m "chore: sync CI components [skip ci]" || echo "Nothing to commit"
    - git push "https://gcm-bot:${CI_PUSH_TOKEN}@${CI_SERVER_HOST}/${CI_PROJECT_PATH}.git" HEAD:${CI_COMMIT_REF_NAME}
  variables:
    GCM_TOKEN: $YOUR_ORG_GCM_TOKEN
```

---

## Dockerfile

- Base: `python:3.12-alpine`
- Install `git` via `apk`
- Copy only `gcm.py` and `requirements.txt`
- `pip install --no-cache-dir -r requirements.txt`
- `ENTRYPOINT ["python", "/app/gcm.py"]`
- Non-root user
- Image must be as small as possible

---

## GitHub Actions Workflow (`.github/workflows/docker.yml`)

- Trigger: push to `main` + push of any tag matching `v*`
- Build multi-platform: `linux/amd64,linux/arm64`
- Push to `ghcr.io/${{ github.repository_owner }}/gitlab-component-manager`
- Tag strategy:
  - `latest` on `main` push
  - Semver tag (`1.2.0`, `1.2`, `1`) on tag push — use `docker/metadata-action`
- Use `GITHUB_TOKEN` for ghcr.io auth (no secrets needed)
- Cache layers with `cache-from: type=gha` / `cache-to: type=gha,mode=max`

---

## Tests (`tests/test_gcm.py`)

Write tests in this order (TDD: write test → watch fail → implement → pass):

1. `test_parse_single_annotation` — happy path, one component
2. `test_parse_multiple_annotations` — two components in one file
3. `test_parse_missing_required_key` — raises `AnnotationError`
4. `test_parse_unknown_key` — raises `AnnotationError`
5. `test_parse_missing_local_line` — raises `AnnotationError`
6. `test_parse_with_sha` — optional `sha` key is captured
7. `test_download_success` — mock `requests.get`, assert URL construction and return value
8. `test_download_with_token` — assert `PRIVATE-TOKEN` header is set
9. `test_download_non_200` — raises `DownloadError`
10. `test_download_ref_uses_sha_when_present` — sha overrides version in `ref`
11. `test_cmd_pull_writes_files` — mock download, assert files written to correct paths
12. `test_cmd_pull_creates_parent_dirs` — nested path is created
13. `test_cmd_pull_continues_on_error` — one failed download, returns 1, others written
14. `test_cmd_list_output` — captures stdout, checks tabular output
15. `test_cmd_diff_up_to_date` — local matches remote → `[up-to-date]`
16. `test_cmd_diff_changed` — local differs → `[changed]`, returns 1
17. `test_cmd_diff_new` — file doesn't exist → `[new]`, returns 1
18. `test_no_subcommand_exits_1` — calling with no args exits with code 1

Use `unittest.mock.patch` for all HTTP and filesystem side effects. No real network calls in tests.

---

## README Requirements

Must include:
1. One-paragraph description
2. Annotation syntax reference (with example)
3. CLI usage (all commands + options)
4. ENV var table
5. GitLab CI job snippet for customer repos
6. Development setup (`pip install -r requirements-dev.txt && pytest --cov=gcm`)
7. How to build and run the Docker image locally

---

## Quality Gates

Before considering the implementation done:

```bash
pytest --cov=gcm --cov-report=term-missing --cov-fail-under=100
```

Must pass with 0 warnings. All 18 tests green. Coverage 100%.
