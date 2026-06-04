import runpy
import sys
import os
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock


sys.path.insert(0, str(Path(__file__).parent.parent))
import gcm
from gcm import (
    parse_annotations,
    download_component,
    _fetch_component,
    _fetch_readme,
    cmd_pull,
    cmd_list,
    cmd_diff,
    _commit_changes,
    AnnotationError,
    DownloadError,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

# Long form: component name is the last segment of the source URL.
SINGLE_ANNOTATION = """\
include:
  # gcm:source=https://gitlab.example.com/ci-components/azure/gitlab-ci-azure
  # gcm:version=3.2.1
  - component: $CI_SERVER_FQDN/$CI_PROJECT_PATH/gitlab-ci-azure@$CI_COMMIT_SHA
"""

MULTIPLE_ANNOTATIONS = """\
include:
  # gcm:source=https://gitlab.example.com/ci-components/azure/gitlab-ci-azure
  # gcm:version=3.2.1
  - component: $CI_SERVER_FQDN/$CI_PROJECT_PATH/gitlab-ci-azure@$CI_COMMIT_SHA
  # gcm:source=https://gitlab.example.com/ci-components/docker/gitlab-ci-docker
  # gcm:version=2.0.0
  - component: $CI_SERVER_FQDN/$CI_PROJECT_PATH/gitlab-ci-docker@$CI_COMMIT_SHA
"""

PULL_CI_CONTENT = """\
include:
  # gcm:source=https://gitlab.example.com/ci-components/azure/gitlab-ci-azure
  # gcm:version=3.2.1
  - component: $CI_SERVER_FQDN/$CI_PROJECT_PATH/gitlab-ci-azure@$CI_COMMIT_SHA
"""


# ---------------------------------------------------------------------------
# parse_annotations — long form
# ---------------------------------------------------------------------------

def test_parse_single_annotation():
    result = parse_annotations(SINGLE_ANNOTATION)
    assert len(result) == 1
    assert result[0] == {
        "source": "https://gitlab.example.com/ci-components/azure",
        "version": "3.2.1",
        "component": "gitlab-ci-azure",
        "component_ref": "$CI_SERVER_FQDN/$CI_PROJECT_PATH/gitlab-ci-azure@$CI_COMMIT_SHA",
    }


def test_parse_multiple_annotations():
    result = parse_annotations(MULTIPLE_ANNOTATIONS)
    assert len(result) == 2
    assert result[0]["component"] == "gitlab-ci-azure"
    assert result[0]["version"] == "3.2.1"
    assert result[1]["component"] == "gitlab-ci-docker"
    assert result[1]["version"] == "2.0.0"
    assert result[1]["component_ref"] == "$CI_SERVER_FQDN/$CI_PROJECT_PATH/gitlab-ci-docker@$CI_COMMIT_SHA"


def test_parse_missing_required_key():
    text = """\
include:
  # gcm:source=https://gitlab.example.com/ci-components/azure/gitlab-ci-azure
  - component: $CI_SERVER_FQDN/$CI_PROJECT_PATH/gitlab-ci-azure@$CI_COMMIT_SHA
"""
    with pytest.raises(AnnotationError, match="version"):
        parse_annotations(text)


def test_parse_unknown_key():
    text = """\
include:
  # gcm:source=https://gitlab.example.com/ci-components/azure/gitlab-ci-azure
  # gcm:version=3.2.1
  # gcm:unknown=oops
  - component: $CI_SERVER_FQDN/$CI_PROJECT_PATH/gitlab-ci-azure@$CI_COMMIT_SHA
"""
    with pytest.raises(AnnotationError, match="unknown"):
        parse_annotations(text)


def test_parse_missing_component_line():
    text = """\
include:
  # gcm:source=https://gitlab.example.com/ci-components/azure/gitlab-ci-azure
  # gcm:version=3.2.1
  - template: something.yml
"""
    with pytest.raises(AnnotationError, match="component"):
        parse_annotations(text)


def test_parse_with_sha():
    text = """\
include:
  # gcm:source=https://gitlab.example.com/ci-components/azure/gitlab-ci-azure
  # gcm:version=3.2.1
  # gcm:sha=abc123def456
  - component: $CI_SERVER_FQDN/$CI_PROJECT_PATH/gitlab-ci-azure@$CI_COMMIT_SHA
"""
    result = parse_annotations(text)
    assert len(result) == 1
    assert result[0]["sha"] == "abc123def456"
    assert result[0]["version"] == "3.2.1"


# ---------------------------------------------------------------------------
# download_component
# ---------------------------------------------------------------------------

def test_download_success():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "content of the file"

    with patch("gcm.requests.get", return_value=mock_resp) as mock_get:
        result = download_component(
            source="https://gitlab.example.com/ci-components/azure",
            version="3.2.1",
            component="templates/gitlab-ci-azure.yml",
            sha=None,
            token=None,
        )

    assert result == "content of the file"
    call_url = mock_get.call_args[0][0]
    assert "api/v4/projects" in call_url
    assert "ref=3.2.1" in call_url
    assert "templates%2Fgitlab-ci-azure.yml" in call_url


def test_download_with_token():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "content"

    with patch("gcm.requests.get", return_value=mock_resp) as mock_get:
        download_component(
            source="https://gitlab.example.com/ci-components/azure",
            version="3.2.1",
            component="templates/gitlab-ci-azure.yml",
            sha=None,
            token="mytoken",
        )

    headers = mock_get.call_args[1]["headers"]
    assert headers.get("PRIVATE-TOKEN") == "mytoken"


def test_download_non_200():
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.url = "https://example.com/api/v4/projects/..."

    with patch("gcm.requests.get", return_value=mock_resp):
        with pytest.raises(DownloadError, match="404"):
            download_component(
                source="https://gitlab.example.com/ci-components/azure",
                version="3.2.1",
                component="templates/gitlab-ci-azure.yml",
                sha=None,
                token=None,
            )


def test_download_ref_uses_sha_when_present():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "content"

    with patch("gcm.requests.get", return_value=mock_resp) as mock_get:
        download_component(
            source="https://gitlab.example.com/ci-components/azure",
            version="3.2.1",
            component="templates/gitlab-ci-azure.yml",
            sha="deadbeef",
            token=None,
        )

    call_url = mock_get.call_args[0][0]
    assert "ref=deadbeef" in call_url
    assert "ref=3.2.1" not in call_url


# ---------------------------------------------------------------------------
# cmd_pull
# ---------------------------------------------------------------------------

def _make_pull_args(tmp_path, content=PULL_CI_CONTENT):
    ci_file = tmp_path / ".gitlab-ci.yml"
    ci_file.write_text(content)
    args = MagicMock()
    args.ci_file = str(ci_file)
    args.components_dir = "templates"
    args.commit = False
    return args


def _git_result(returncode=0, stdout="", stderr=""):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


def test_cmd_pull_writes_files(tmp_path):
    args = _make_pull_args(tmp_path)

    with patch("gcm._fetch_component", return_value="component content"):
        with patch("gcm._fetch_readme", return_value=None):
            with patch("gcm.os.getcwd", return_value=str(tmp_path)):
                rc = cmd_pull(args, {"GCM_TOKEN": None})

    assert rc == 0
    written = tmp_path / "templates" / "gitlab-ci-azure.yml"
    assert written.exists()
    assert written.read_text() == "component content"


def test_cmd_pull_cleans_stale_files(tmp_path):
    stale = tmp_path / "templates" / "old-component.yml"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale content")

    args = _make_pull_args(tmp_path)

    with patch("gcm._fetch_component", return_value="new content"):
        with patch("gcm._fetch_readme", return_value=None):
            with patch("gcm.os.getcwd", return_value=str(tmp_path)):
                rc = cmd_pull(args, {})

    assert rc == 0
    assert not stale.exists()
    assert (tmp_path / "templates" / "gitlab-ci-azure.yml").exists()


def test_cmd_pull_creates_parent_dirs(tmp_path):
    args = _make_pull_args(tmp_path)
    args.components_dir = "nested/vendor/templates"

    with patch("gcm._fetch_component", return_value="data"):
        with patch("gcm._fetch_readme", return_value=None):
            with patch("gcm.os.getcwd", return_value=str(tmp_path)):
                rc = cmd_pull(args, {})

    assert rc == 0
    assert (tmp_path / "nested" / "vendor" / "templates" / "gitlab-ci-azure.yml").exists()


def test_cmd_pull_continues_on_error(tmp_path):
    content = """\
include:
  # gcm:source=https://gitlab.example.com/ci-components/azure/gitlab-ci-azure
  # gcm:version=3.2.1
  - component: $CI_SERVER_FQDN/$CI_PROJECT_PATH/gitlab-ci-azure@$CI_COMMIT_SHA
  # gcm:source=https://gitlab.example.com/ci-components/docker/gitlab-ci-docker
  # gcm:version=2.0.0
  - component: $CI_SERVER_FQDN/$CI_PROJECT_PATH/gitlab-ci-docker@$CI_COMMIT_SHA
"""
    args = _make_pull_args(tmp_path, content)

    def side_effect(source, version, name, sha, token):
        if "azure" in source:
            raise DownloadError("HTTP 500")
        return "docker content"

    with patch("gcm._fetch_component", side_effect=side_effect):
        with patch("gcm._fetch_readme", return_value=None):
            with patch("gcm.os.getcwd", return_value=str(tmp_path)):
                rc = cmd_pull(args, {})

    assert rc == 1
    assert (tmp_path / "templates" / "gitlab-ci-docker.yml").exists()
    assert not (tmp_path / "templates" / "gitlab-ci-azure.yml").exists()


# ---------------------------------------------------------------------------
# cmd_list
# ---------------------------------------------------------------------------

def test_cmd_list_output(tmp_path, capsys):
    ci_file = tmp_path / ".gitlab-ci.yml"
    ci_file.write_text(SINGLE_ANNOTATION)
    args = MagicMock()
    args.ci_file = str(ci_file)

    rc = cmd_list(args, {})

    assert rc == 0
    out = capsys.readouterr().out
    assert "https://gitlab.example.com/ci-components/azure" in out
    assert "gitlab-ci-azure" in out
    assert "3.2.1" in out
    assert "$CI_SERVER_FQDN/$CI_PROJECT_PATH/gitlab-ci-azure@$CI_COMMIT_SHA" in out


# ---------------------------------------------------------------------------
# cmd_diff
# ---------------------------------------------------------------------------

def test_cmd_diff_up_to_date(tmp_path, capsys):
    ci_file = tmp_path / ".gitlab-ci.yml"
    ci_file.write_text(PULL_CI_CONTENT)
    local_file = tmp_path / "templates" / "gitlab-ci-azure.yml"
    local_file.parent.mkdir(parents=True)
    local_file.write_text("same content")

    args = MagicMock()
    args.ci_file = str(ci_file)
    args.components_dir = "templates"

    with patch("gcm._fetch_component", return_value="same content"):
        with patch("gcm.os.getcwd", return_value=str(tmp_path)):
            rc = cmd_diff(args, {})

    assert rc == 0
    out = capsys.readouterr().out
    assert "[up-to-date]" in out


def test_cmd_diff_changed(tmp_path, capsys):
    ci_file = tmp_path / ".gitlab-ci.yml"
    ci_file.write_text(PULL_CI_CONTENT)
    local_file = tmp_path / "templates" / "gitlab-ci-azure.yml"
    local_file.parent.mkdir(parents=True)
    local_file.write_text("old content")

    args = MagicMock()
    args.ci_file = str(ci_file)
    args.components_dir = "templates"

    with patch("gcm._fetch_component", return_value="new content"):
        with patch("gcm.os.getcwd", return_value=str(tmp_path)):
            rc = cmd_diff(args, {})

    assert rc == 1
    out = capsys.readouterr().out
    assert "[changed]" in out


def test_cmd_diff_new(tmp_path, capsys):
    ci_file = tmp_path / ".gitlab-ci.yml"
    ci_file.write_text(PULL_CI_CONTENT)

    args = MagicMock()
    args.ci_file = str(ci_file)
    args.components_dir = "templates"

    with patch("gcm._fetch_component", return_value="new content"):
        with patch("gcm.os.getcwd", return_value=str(tmp_path)):
            rc = cmd_diff(args, {})

    assert rc == 1
    out = capsys.readouterr().out
    assert "[new]" in out


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def test_no_subcommand_exits_1():
    with patch("sys.argv", ["gcm"]):
        with pytest.raises(SystemExit) as exc_info:
            gcm.main()
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# Additional coverage tests
# ---------------------------------------------------------------------------

def test_parse_annotation_without_equals():
    text = """\
include:
  # gcm:noequals
  - component: $CI_SERVER_FQDN/$CI_PROJECT_PATH/gitlab-ci-azure@$CI_COMMIT_SHA
"""
    with pytest.raises(AnnotationError, match="Invalid"):
        parse_annotations(text)


def test_parse_blank_line_before_component():
    text = """\
include:
  # gcm:source=https://gitlab.example.com/ci-components/azure/gitlab-ci-azure
  # gcm:version=3.2.1

  - component: $CI_SERVER_FQDN/$CI_PROJECT_PATH/gitlab-ci-azure@$CI_COMMIT_SHA
"""
    with pytest.raises(AnnotationError, match="component"):
        parse_annotations(text)


def test_cmd_pull_file_error(tmp_path):
    args = MagicMock()
    args.ci_file = str(tmp_path / "nonexistent.yml")
    rc = cmd_pull(args, {})
    assert rc == 1


def test_cmd_list_file_error(tmp_path):
    args = MagicMock()
    args.ci_file = str(tmp_path / "nonexistent.yml")
    rc = cmd_list(args, {})
    assert rc == 1


def test_cmd_diff_file_error(tmp_path):
    args = MagicMock()
    args.ci_file = str(tmp_path / "nonexistent.yml")
    rc = cmd_diff(args, {})
    assert rc == 1


def test_cmd_diff_download_error(tmp_path, capsys):
    ci_file = tmp_path / ".gitlab-ci.yml"
    ci_file.write_text(PULL_CI_CONTENT)
    args = MagicMock()
    args.ci_file = str(ci_file)
    args.components_dir = "templates"

    with patch("gcm._fetch_component", side_effect=DownloadError("HTTP 403")):
        with patch("gcm.os.getcwd", return_value=str(tmp_path)):
            rc = cmd_diff(args, {})

    assert rc == 1


def test_main_dispatches_subcommand(tmp_path):
    ci_file = tmp_path / ".gitlab-ci.yml"
    ci_file.write_text(SINGLE_ANNOTATION)
    with patch("sys.argv", ["gcm", "--ci-file", str(ci_file), "list"]):
        with pytest.raises(SystemExit) as exc_info:
            gcm.main()
    assert exc_info.value.code == 0


def test_main_as_module():
    with patch("sys.argv", ["gcm"]):
        with pytest.raises(SystemExit) as exc_info:
            runpy.run_module("gcm", run_name="__main__")
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# _commit_changes
# ---------------------------------------------------------------------------

def test_commit_nothing_to_commit(capsys):
    side_effects = [_git_result(0), _git_result(0)]  # add, diff
    with patch("gcm.subprocess.run", side_effect=side_effects) as mock_run:
        rc = _commit_changes("templates", {})
    assert rc == 0
    assert "nothing to commit" in capsys.readouterr().out
    assert mock_run.call_count == 2


def test_commit_with_changes(capsys):
    side_effects = [_git_result(0), _git_result(1), _git_result(0)]  # add, diff, commit
    with patch("gcm.subprocess.run", side_effect=side_effects) as mock_run:
        rc = _commit_changes("templates", {})
    assert rc == 0
    commit_args = mock_run.call_args_list[2][0][0]
    assert commit_args == ["git", "commit", "-m", "chore: sync CI components [skip ci]"]


def test_commit_custom_message(capsys):
    side_effects = [_git_result(0), _git_result(1), _git_result(0)]
    with patch("gcm.subprocess.run", side_effect=side_effects) as mock_run:
        rc = _commit_changes("templates", {"GCM_GIT_COMMIT_MESSAGE": "ci: update components"})
    assert rc == 0
    commit_args = mock_run.call_args_list[2][0][0]
    assert commit_args == ["git", "commit", "-m", "ci: update components"]


def test_commit_sets_user(capsys):
    # add, diff, config user.name, config user.email, commit
    side_effects = [_git_result(0), _git_result(1), _git_result(0), _git_result(0), _git_result(0)]
    env = {"GCM_GIT_USER_NAME": "GCM Bot", "GCM_GIT_USER_EMAIL": "gcm@example.com"}
    with patch("gcm.subprocess.run", side_effect=side_effects) as mock_run:
        rc = _commit_changes("templates", env)
    assert rc == 0
    assert mock_run.call_args_list[2][0][0] == ["git", "config", "user.name", "GCM Bot"]
    assert mock_run.call_args_list[3][0][0] == ["git", "config", "user.email", "gcm@example.com"]


def test_commit_push_default_remote(capsys):
    # add, diff, commit, push origin
    side_effects = [_git_result(0), _git_result(1), _git_result(0), _git_result(0)]
    with patch("gcm.subprocess.run", side_effect=side_effects) as mock_run:
        rc = _commit_changes("templates", {"GCM_GIT_PUSH": "1"})
    assert rc == 0
    assert mock_run.call_args_list[3][0][0] == ["git", "push", "origin"]
    assert "pushed to origin" in capsys.readouterr().out


def test_commit_push_custom_remote_and_branch(capsys):
    # add, diff, commit, push upstream main
    side_effects = [_git_result(0), _git_result(1), _git_result(0), _git_result(0)]
    env = {"GCM_GIT_PUSH": "1", "GCM_GIT_REMOTE": "upstream", "GCM_GIT_BRANCH": "main"}
    with patch("gcm.subprocess.run", side_effect=side_effects) as mock_run:
        rc = _commit_changes("templates", env)
    assert rc == 0
    assert mock_run.call_args_list[3][0][0] == ["git", "push", "upstream", "main"]


def test_commit_add_fails(capsys):
    with patch("gcm.subprocess.run", return_value=_git_result(1, stderr="permission denied")):
        rc = _commit_changes("templates", {})
    assert rc == 1


def test_commit_commit_fails(capsys):
    side_effects = [_git_result(0), _git_result(1), _git_result(1, stderr="conflict")]
    with patch("gcm.subprocess.run", side_effect=side_effects):
        rc = _commit_changes("templates", {})
    assert rc == 1


def test_commit_push_fails(capsys):
    side_effects = [_git_result(0), _git_result(1), _git_result(0), _git_result(1, stderr="rejected")]
    with patch("gcm.subprocess.run", side_effect=side_effects):
        rc = _commit_changes("templates", {"GCM_GIT_PUSH": "1"})
    assert rc == 1


def test_cmd_pull_with_commit_flag(tmp_path, capsys):
    args = _make_pull_args(tmp_path)
    args.commit = True

    side_effects = [_git_result(0), _git_result(1), _git_result(0)]  # add, diff, commit
    with patch("gcm._fetch_component", return_value="content"):
        with patch("gcm._fetch_readme", return_value=None):
            with patch("gcm.os.getcwd", return_value=str(tmp_path)):
                with patch("gcm.subprocess.run", side_effect=side_effects):
                    rc = cmd_pull(args, {})

    assert rc == 0
    assert (tmp_path / "templates" / "gitlab-ci-azure.yml").exists()


# ---------------------------------------------------------------------------
# _fetch_component — single-file vs directory layout fallback
# ---------------------------------------------------------------------------

def _http(status, text=""):
    m = MagicMock()
    m.status_code = status
    m.text = text
    return m


def test_fetch_component_single_file():
    with patch("gcm.requests.get", return_value=_http(200, "single content")) as mock_get:
        result = _fetch_component("https://gitlab.example.com/org/proj", "1.0", "my-comp", None, None)
    assert result == "single content"
    assert mock_get.call_count == 1
    assert "templates%2Fmy-comp.yml" in mock_get.call_args[0][0]


def test_fetch_component_directory():
    with patch("gcm.requests.get", side_effect=[_http(404), _http(200, "dir content")]) as mock_get:
        result = _fetch_component("https://gitlab.example.com/org/proj", "1.0", "my-comp", None, None)
    assert result == "dir content"
    assert mock_get.call_count == 2
    assert "templates%2Fmy-comp%2Ftemplate.yml" in mock_get.call_args[0][0]


def test_fetch_component_not_found():
    with patch("gcm.requests.get", return_value=_http(404)):
        with pytest.raises(DownloadError, match="my-comp"):
            _fetch_component("https://gitlab.example.com/org/proj", "1.0", "my-comp", None, None)


def test_fetch_component_auth_error():
    with patch("gcm.requests.get", return_value=_http(403)) as mock_get:
        with pytest.raises(DownloadError, match="403"):
            _fetch_component("https://gitlab.example.com/org/proj", "1.0", "my-comp", None, None)
    assert mock_get.call_count == 1  # no retry on non-404


# ---------------------------------------------------------------------------
# _fetch_readme
# ---------------------------------------------------------------------------

def test_fetch_readme_success():
    with patch("gcm.requests.get", return_value=_http(200, "# README")) as mock_get:
        result = _fetch_readme("https://gitlab.example.com/org/proj", "1.0", "my-comp", None, None)
    assert result == "# README"
    assert "templates%2Fmy-comp%2FREADME.md" in mock_get.call_args[0][0]


def test_fetch_readme_not_found():
    with patch("gcm.requests.get", return_value=_http(404)):
        result = _fetch_readme("https://gitlab.example.com/org/proj", "1.0", "my-comp", None, None)
    assert result is None


def test_fetch_readme_other_error():
    with patch("gcm.requests.get", return_value=_http(403)):
        with pytest.raises(DownloadError, match="403"):
            _fetch_readme("https://gitlab.example.com/org/proj", "1.0", "my-comp", None, None)


def test_cmd_pull_downloads_readme(tmp_path):
    args = _make_pull_args(tmp_path)

    with patch("gcm._fetch_component", return_value="component content"):
        with patch("gcm._fetch_readme", return_value="# Component README"):
            with patch("gcm.os.getcwd", return_value=str(tmp_path)):
                rc = cmd_pull(args, {})

    assert rc == 0
    assert (tmp_path / "templates" / "gitlab-ci-azure.yml").exists()
    readme = tmp_path / "templates" / "gitlab-ci-azure" / "README.md"
    assert readme.exists()
    assert readme.read_text() == "# Component README"


def test_cmd_pull_no_readme(tmp_path):
    args = _make_pull_args(tmp_path)

    with patch("gcm._fetch_component", return_value="component content"):
        with patch("gcm._fetch_readme", return_value=None):
            with patch("gcm.os.getcwd", return_value=str(tmp_path)):
                rc = cmd_pull(args, {})

    assert rc == 0
    assert (tmp_path / "templates" / "gitlab-ci-azure.yml").exists()
    assert not (tmp_path / "templates" / "gitlab-ci-azure").exists()


# ---------------------------------------------------------------------------
# gcm:component short form (URL)
# ---------------------------------------------------------------------------

COMPONENT_REF_ANNOTATION = """\
include:
  # gcm:component=https://gitlab.example.com/ci-components/azure?version=3.2.1&component=gitlab-ci-azure
  - component: $CI_SERVER_FQDN/$CI_PROJECT_PATH/gitlab-ci-azure@$CI_COMMIT_SHA
"""


def test_parse_component_ref():
    result = parse_annotations(COMPONENT_REF_ANNOTATION)
    assert len(result) == 1
    assert result[0]["source"] == "https://gitlab.example.com/ci-components/azure"
    assert result[0]["version"] == "3.2.1"
    assert result[0]["component"] == "gitlab-ci-azure"
    assert result[0]["component_ref"] == "$CI_SERVER_FQDN/$CI_PROJECT_PATH/gitlab-ci-azure@$CI_COMMIT_SHA"
    assert "sha" not in result[0]


def test_parse_component_ref_with_sha():
    text = """\
include:
  # gcm:component=https://gitlab.example.com/ci-components/azure?version=3.2.1&component=gitlab-ci-azure&sha=abc123
  - component: $CI_SERVER_FQDN/$CI_PROJECT_PATH/gitlab-ci-azure@$CI_COMMIT_SHA
"""
    result = parse_annotations(text)
    assert result[0]["sha"] == "abc123"


def test_parse_component_ref_no_query_infers_component_and_version():
    text = """\
include:
  # gcm:component=https://gitlab.example.com/ci-components/azure/gitlab-ci-azure
  - component: $CI_SERVER_FQDN/$CI_PROJECT_PATH/gitlab-ci-azure@$CI_COMMIT_SHA
"""
    result = parse_annotations(text)
    assert result[0]["source"] == "https://gitlab.example.com/ci-components/azure"
    assert result[0]["component"] == "gitlab-ci-azure"
    assert result[0]["version"] == "main"


def test_parse_component_ref_infers_component_from_path():
    text = """\
include:
  # gcm:component=https://gitlab.example.com/ci-components/azure/gitlab-ci-azure?version=3.2.1
  - component: $CI_SERVER_FQDN/$CI_PROJECT_PATH/gitlab-ci-azure@$CI_COMMIT_SHA
"""
    result = parse_annotations(text)
    assert result[0]["source"] == "https://gitlab.example.com/ci-components/azure"
    assert result[0]["component"] == "gitlab-ci-azure"
    assert result[0]["version"] == "3.2.1"


def test_parse_component_ref_unknown_param():
    text = """\
include:
  # gcm:component=https://gitlab.example.com/ci-components/azure?version=3.2.1&component=gitlab-ci-azure&flavor=test
  - component: $CI_SERVER_FQDN/$CI_PROJECT_PATH/gitlab-ci-azure@$CI_COMMIT_SHA
"""
    with pytest.raises(AnnotationError, match="flavor"):
        parse_annotations(text)


def test_parse_component_ref_mixed_with_other_keys():
    text = """\
include:
  # gcm:component=https://gitlab.example.com/ci-components/azure?version=3.2.1&component=gitlab-ci-azure
  # gcm:version=3.2.1
  - component: $CI_SERVER_FQDN/$CI_PROJECT_PATH/gitlab-ci-azure@$CI_COMMIT_SHA
"""
    with pytest.raises(AnnotationError, match="version"):
        parse_annotations(text)


# ---------------------------------------------------------------------------
# No annotations — helpful hint
# ---------------------------------------------------------------------------

def test_cmd_pull_no_annotations(tmp_path, capsys):
    ci_file = tmp_path / ".gitlab-ci.yml"
    ci_file.write_text("include:\n  - component: $CI_SERVER_FQDN/$CI_PROJECT_PATH/foo@main\n")
    args = MagicMock()
    args.ci_file = str(ci_file)
    args.commit = False
    rc = cmd_pull(args, {})
    assert rc == 0
    assert "gcm:component" in capsys.readouterr().out


def test_cmd_list_no_annotations(tmp_path, capsys):
    ci_file = tmp_path / ".gitlab-ci.yml"
    ci_file.write_text("include:\n  - component: $CI_SERVER_FQDN/$CI_PROJECT_PATH/foo@main\n")
    args = MagicMock()
    args.ci_file = str(ci_file)
    rc = cmd_list(args, {})
    assert rc == 0
    assert "gcm:component" in capsys.readouterr().out


def test_cmd_diff_no_annotations(tmp_path, capsys):
    ci_file = tmp_path / ".gitlab-ci.yml"
    ci_file.write_text("include:\n  - component: $CI_SERVER_FQDN/$CI_PROJECT_PATH/foo@main\n")
    args = MagicMock()
    args.ci_file = str(ci_file)
    args.components_dir = "templates"
    rc = cmd_diff(args, {})
    assert rc == 0
    assert "gcm:component" in capsys.readouterr().out


def test_cmd_pull_no_commit_on_pull_error(tmp_path):
    args = _make_pull_args(tmp_path)
    args.commit = True

    with patch("gcm._fetch_component", side_effect=DownloadError("HTTP 500")):
        with patch("gcm.os.getcwd", return_value=str(tmp_path)):
            with patch("gcm.subprocess.run") as mock_run:
                rc = cmd_pull(args, {})

    assert rc == 1
    mock_run.assert_not_called()
