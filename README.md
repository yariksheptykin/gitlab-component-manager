# gitlab-component-manager (gcm)

`gcm` is a minimal Python CLI tool that solves a specific GitLab limitation: the native `include: component:` syntax only works when both projects are on the same GitLab instance. If your components live on a private GitLab instance that your customer's pipeline cannot reach, you're stuck — unless you mirror the entire project.

`gcm` removes that requirement. It lets you selectively vendor individual CI/CD components from any private GitLab instance into your own repository. The vendored files are committed into `templates/` and included via the standard `- component:` syntax pointing at the current project, so pipelines work exactly as if the component were native — no mirrors, no cross-instance authentication at pipeline runtime.

---

## How it works

1. Annotate each `- component:` include in `.gitlab-ci.yml` with a `gcm:component` comment that points at the remote source:

```yaml
include:
  # gcm:component=https://gitlab.your-org.com/ci-components/azure/gitlab-ci-azure?version=3.2.1
  - component: $CI_SERVER_FQDN/$CI_PROJECT_PATH/gitlab-ci-azure@$CI_COMMIT_SHA
    inputs:
      stage: build
```

2. Run `gcm pull` (or trigger the CI job). It fetches `templates/gitlab-ci-azure.yml` from the remote project via the GitLab API and writes it into `templates/` in the current repository.

3. The `- component:` line already references the current project, so no further edits are needed. Pipelines load the component locally at `$CI_COMMIT_SHA` — the exact version you pulled.

---

## Annotation syntax

The simplest form — component name and source project inferred from the URL, version defaults to `main`:

```yaml
include:
  # gcm:component=https://gitlab.your-org.com/ci-components/azure/gitlab-ci-azure
  - component: $CI_SERVER_FQDN/$CI_PROJECT_PATH/gitlab-ci-azure@$CI_COMMIT_SHA
    inputs:
      stage: build
```

Pin a specific version:

```yaml
include:
  # gcm:component=https://gitlab.your-org.com/ci-components/azure/gitlab-ci-azure?version=3.2.1
  - component: $CI_SERVER_FQDN/$CI_PROJECT_PATH/gitlab-ci-azure@$CI_COMMIT_SHA
```

Pin to an exact commit SHA:

```yaml
include:
  # gcm:component=https://gitlab.your-org.com/ci-components/azure/gitlab-ci-azure?version=3.2.1&sha=a1b2c3d4
  - component: $CI_SERVER_FQDN/$CI_PROJECT_PATH/gitlab-ci-azure@$CI_COMMIT_SHA
```

Multiple components in one file:

```yaml
include:
  # gcm:component=https://gitlab.your-org.com/ci-components/azure/gitlab-ci-azure?version=3.2.1
  - component: $CI_SERVER_FQDN/$CI_PROJECT_PATH/gitlab-ci-azure@$CI_COMMIT_SHA
  # gcm:component=https://gitlab.your-org.com/ci-components/docker/gitlab-ci-docker?version=2.0.0
  - component: $CI_SERVER_FQDN/$CI_PROJECT_PATH/gitlab-ci-docker@$CI_COMMIT_SHA
```

**Format:** `<source-url>/<component>[?version=<tag-or-branch>][&sha=<commit-sha>]`

| Part         | Required | Description |
|--------------|----------|-------------|
| `source-url` | Yes      | HTTPS URL of the remote GitLab project (no `.git` suffix) |
| `component`  | No       | Last path segment of the URL if omitted; or override with `?component=<name>` |
| `version`    | No       | Git tag or branch to fetch; defaults to `main` |
| `sha`        | No       | Exact commit SHA; overrides `version` for the actual download |

**Rules:**
- The annotation is indented inside the `include:` block, on the line immediately above its `- component:` entry
- No blank lines between the annotation and the `- component:` line
- Multiple annotated includes in the same `include:` block are supported

---

## CLI usage

```
gcm [--ci-file PATH] [--components-dir PATH] COMMAND

Commands:
  pull [--commit]   Fetch all annotated components and write them to the components dir
  list              Print all annotated components and their versions (no download)
  diff              Show which components would change (dry run of pull)

Global options:
  --ci-file PATH          Path to .gitlab-ci.yml (default: .gitlab-ci.yml)
  --components-dir PATH   Directory to write components into (default: templates)

pull options:
  --commit                Stage and commit downloaded components via git
                          (skipped when nothing changed; configured via GCM_GIT_* env vars)
```

---

## Environment variables

| Variable                  | Required | Default                               | Description                                                 |
|---------------------------|----------|---------------------------------------|-------------------------------------------------------------|
| `GCM_TOKEN`               | No       | —                                     | GitLab Personal/Project Access Token for private projects   |
| `GCM_GIT_USER_NAME`       | No       | git config user.name                  | Overrides `git config user.name` for the commit             |
| `GCM_GIT_USER_EMAIL`      | No       | git config user.email                 | Overrides `git config user.email` for the commit            |
| `GCM_GIT_COMMIT_MESSAGE`  | No       | `chore: sync CI components [skip ci]` | Commit message used by `gcm pull --commit`                  |
| `GCM_GIT_PUSH`            | No       | —                                     | Set to any non-empty value to push after committing         |
| `GCM_GIT_REMOTE`          | No       | `origin`                              | Remote to push to (only when `GCM_GIT_PUSH` is set)        |
| `GCM_GIT_BRANCH`          | No       | current branch                        | Branch to push (only when `GCM_GIT_PUSH` is set)           |

`GCM_TOKEN` is only required for private source projects. Public projects work without it.

---

## GitLab CI integration

### Using the gcm CI component (recommended)

This repository ships a GitLab CI component in `templates/sync/`. Because GitLab CI
components must be hosted on a GitLab instance (GitHub is not supported), you need to
mirror or fork this project to your own GitLab server first. Then include it with:

```yaml
include:
  - component: gitlab.com/Sheptykin/gitlab-component-manager/sync@main
    inputs:
      push_token: $CI_PUSH_TOKEN        # token with write access to this repo
      gcm_token: $YOUR_ORG_GCM_TOKEN   # token for private source projects (omit if public)
```

The component is mirrored from GitHub to `gitlab.com/Sheptykin/gitlab-component-manager`.
If your pipeline runs on a private GitLab instance, mirror the project there and replace
`gitlab.com/Sheptykin` with your own server and namespace.
See [`templates/sync/README.md`](templates/sync/README.md) for the full input reference.

### Manual job definition

If you cannot use the component include (e.g. cross-instance restriction), add the job directly:

```yaml
sync-ci-components:
  stage: .pre
  image: ghcr.io/yariksheptykin/gitlab-component-manager:latest
  when: manual
  script:
    - gcm diff || true
    - gcm pull --commit
    - git push "https://oauth2:${CI_PUSH_TOKEN}@${CI_SERVER_HOST}/${CI_PROJECT_PATH}.git" HEAD:${CI_COMMIT_REF_NAME}
  variables:
    GCM_TOKEN: $YOUR_ORG_GCM_TOKEN
    GCM_GIT_USER_NAME: "GCM Bot"
    GCM_GIT_USER_EMAIL: "gcm-bot@your-org.com"
```

`gcm pull --commit` only creates a commit when something changed; if nothing changed the push is a no-op.

---

## Development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest --cov=gcm --cov-report=term-missing --cov-fail-under=100
```

---

## Docker

Build locally:

```bash
docker build -t gcm .
```

Run against a `.gitlab-ci.yml` in the current directory:

```bash
docker run --rm --user=$(id -u) -v "$PWD:/work" -w /work -e GCM_TOKEN=your_token gcm pull
```
