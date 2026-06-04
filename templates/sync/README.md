# sync

Adds a manual `sync-ci-components` job to your pipeline that vendors pinned CI/CD
components from remote GitLab instances into `templates/` and commits the result back
to your repository.

## Usage

GitLab CI components must be hosted on a GitLab instance. This project's source is on
GitHub (`github.com/yariksheptykin/gitlab-component-manager`), so you need to mirror or
fork it to your own GitLab server first. Then include it with:

```yaml
include:
  - component: gitlab.com/Sheptykin/gitlab-component-manager/sync@main
    inputs:
      push_token: $CI_PUSH_TOKEN
      gcm_token: $YOUR_ORG_GCM_TOKEN
```

The component is hosted at `gitlab.com/Sheptykin/gitlab-component-manager` (mirrored from
GitHub). If your pipeline runs on a private GitLab instance, mirror the project there and
replace `gitlab.com/Sheptykin` with your own server and namespace.

Then annotate each `- component:` include you want managed:

```yaml
include:
  # gcm:component=https://gitlab.your-org.com/ci-components/azure/gitlab-ci-azure?version=3.2.1
  - component: $CI_SERVER_FQDN/$CI_PROJECT_PATH/gitlab-ci-azure@$CI_COMMIT_SHA
```

## Inputs

| Input            | Required | Default                                                | Description |
|------------------|----------|--------------------------------------------------------|-------------|
| `push_token`     | Yes      | —                                                      | Token with write access to push the vendor commit back (e.g. `$CI_PUSH_TOKEN`) |
| `gcm_token`      | No       | `""`                                                   | GitLab API token for private component sources |
| `stage`          | No       | `.pre`                                                 | Pipeline stage for the sync job |
| `image`          | No       | `ghcr.io/yariksheptykin/gitlab-component-manager:latest` | gcm Docker image |
| `git_user_name`  | No       | `GCM Bot`                                              | Git author name for the vendor commit |
| `git_user_email` | No       | `gcm-bot@example.com`                                  | Git author email for the vendor commit |

## What the job does

1. Runs `gcm diff` to print what will change (non-zero exit is ignored)
2. Runs `gcm pull --commit` — cleans `templates/`, downloads all annotated components, commits only if something changed
3. Pushes the commit back using `push_token`

If nothing changed, the commit is skipped and the push is a no-op.
