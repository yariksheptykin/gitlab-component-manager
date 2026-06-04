#!/usr/bin/env python3
"""gcm — GitLab CI/CD Component Manager"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import requests


REQUIRED_KEYS = {"source", "version"}
OPTIONAL_KEYS = {"sha"}
ALL_KEYS = REQUIRED_KEYS | OPTIONAL_KEYS | {"component"}


class AnnotationError(Exception):
    pass


_COMPONENT_REF_VALID_PARAMS = {"version", "component", "sha"}


def _parse_component_ref(ref: str) -> dict:
    parsed = urlsplit(ref)
    params = {k: v[0] for k, v in parse_qs(parsed.query).items()} if parsed.query else {}

    unknown = set(params) - _COMPONENT_REF_VALID_PARAMS
    if unknown:
        raise AnnotationError(f"Unknown parameter(s) in gcm:component: {sorted(unknown)}")

    # Component name: explicit query param, or inferred from the last URL path segment.
    # When inferred, that segment is stripped from the source URL.
    path = parsed.path.rstrip("/")
    if "component" in params:
        component = params["component"]
        source = f"{parsed.scheme}://{parsed.netloc}{path}"
    else:
        last_slash = path.rfind("/")
        component = path[last_slash + 1:]
        source = f"{parsed.scheme}://{parsed.netloc}{path[:last_slash]}"

    version = params.get("version", "main")

    result = {"source": source, "version": version, "component": component}
    if "sha" in params:
        result["sha"] = params["sha"]
    return result


class DownloadError(Exception):
    pass


def parse_annotations(text: str) -> list[dict]:
    lines = text.splitlines()
    results = []
    i = 0
    while i < len(lines):
        if not lines[i].strip().startswith("# gcm:"):
            i += 1
            continue

        block_start = i
        annotation = {}
        while i < len(lines) and lines[i].strip().startswith("# gcm:"):
            raw = lines[i].strip()[len("# gcm:"):]
            if "=" not in raw:
                raise AnnotationError(f"Invalid gcm annotation at line {i + 1}: '{lines[i]}'")
            key, _, value = raw.partition("=")
            if key not in ALL_KEYS:
                raise AnnotationError(
                    f"Unknown gcm key '{key}' at line {i + 1}. Allowed: {sorted(ALL_KEYS)}"
                )
            annotation[key] = value
            i += 1

        if "component" in annotation:
            if len(annotation) > 1:
                extra = sorted(set(annotation) - {"component"})
                raise AnnotationError(
                    f"gcm:component cannot be combined with other annotation keys: {extra}"
                )
            entry = _parse_component_ref(annotation["component"])
        else:
            missing = REQUIRED_KEYS - annotation.keys()
            if missing:
                raise AnnotationError(
                    f"Annotation block starting at line {block_start + 1} is missing required key(s): {sorted(missing)}"
                )
            source = annotation["source"].rstrip("/")
            last_slash = source.rfind("/")
            entry = {
                "source": source[:last_slash],
                "version": annotation["version"],
                "component": source[last_slash + 1:],
            }
            if "sha" in annotation:
                entry["sha"] = annotation["sha"]

        # Scan forward through non-blank lines (e.g. "include:") to find "- component:"
        component_ref = None
        while i < len(lines):
            stripped = lines[i].strip()
            if stripped == "" or lines[i].strip().startswith("# gcm:"):
                break
            if stripped.startswith("- component:"):
                component_ref = stripped[len("- component:"):].strip()
                i += 1
                break
            i += 1

        if component_ref is None:
            raise AnnotationError(
                f"Annotation block starting at line {block_start + 1} must be followed by a '- component:' line"
            )

        entry["component_ref"] = component_ref
        results.append(entry)

    return results


def download_component(
    source: str,
    version: str,
    component: str,
    sha: str | None,
    token: str | None,
) -> str:
    # Strip trailing slash and derive project path from URL
    source = source.rstrip("/")
    # source is e.g. https://gitlab.example.com/group/project
    # We need the host and the project path separately
    # Find the host: scheme + netloc
    without_scheme = source.split("://", 1)[1]
    slash_idx = without_scheme.index("/")
    host = source.split("://")[0] + "://" + without_scheme[:slash_idx]
    project_path = without_scheme[slash_idx + 1:]

    encoded_project = quote(project_path, safe="")
    encoded_file = quote(component, safe="")
    ref = sha if sha else version

    url = f"{host}/api/v4/projects/{encoded_project}/repository/files/{encoded_file}/raw?ref={ref}"

    headers = {}
    if token:
        headers["PRIVATE-TOKEN"] = token

    resp = requests.get(url, headers=headers)
    if resp.status_code != 200:
        raise DownloadError(f"HTTP {resp.status_code} fetching {url}")

    return resp.text


def _fetch_component(source: str, version: str, name: str, sha: str | None, token: str | None) -> str:
    """Download a component, trying single-file then directory layout per GitLab spec."""
    single = f"templates/{name}.yml"
    directory = f"templates/{name}/template.yml"
    for path in (single, directory):
        try:
            return download_component(source, version, path, sha, token)
        except DownloadError as exc:
            if "HTTP 404" not in str(exc):
                raise
    raise DownloadError(
        f"Component '{name}' not found in {source} at ref {sha or version} "
        f"(tried {single} and {directory})"
    )


def _git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True)


def _commit_changes(components_dir: str, env: dict) -> int:
    r = _git("add", components_dir)
    if r.returncode != 0:
        print(f"[commit] git add failed: {r.stderr.strip()}", file=sys.stderr)
        return 1

    r = _git("diff", "--cached", "--quiet")
    if r.returncode == 0:
        print("[commit] nothing to commit")
        return 0

    for var, config_key in (
        ("GCM_GIT_USER_NAME", "user.name"),
        ("GCM_GIT_USER_EMAIL", "user.email"),
    ):
        val = env.get(var)
        if val:
            _git("config", config_key, val)

    message = env.get("GCM_GIT_COMMIT_MESSAGE", "chore: sync CI components [skip ci]")
    r = _git("commit", "-m", message)
    if r.returncode != 0:
        print(f"[commit] git commit failed: {r.stderr.strip()}", file=sys.stderr)
        return 1

    print(f"[commit] {message}")

    if env.get("GCM_GIT_PUSH"):
        remote = env.get("GCM_GIT_REMOTE", "origin")
        branch = env.get("GCM_GIT_BRANCH", "")
        push_args = ["push", remote]
        if branch:
            push_args.append(branch)
        r = _git(*push_args)
        if r.returncode != 0:
            print(f"[commit] git push failed: {r.stderr.strip()}", file=sys.stderr)
            return 1
        print(f"[commit] pushed to {remote}")

    return 0


def _read_ci_file(args) -> str:
    ci_file = getattr(args, "ci_file", ".gitlab-ci.yml")
    return Path(ci_file).read_text()


def _clean_components_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
        print(f"[clean] {path}")


def _no_annotations_hint(ci_file: str) -> None:
    print(
        f"No gcm annotations found in {ci_file}.\n"
        "\nTo track a component, add a gcm:component annotation directly above its - component: line:\n"
        "\n  include:"
        "\n    # gcm:component=https://gitlab.your-org.com/ci-components/<project>/<name>"
        "?version=<tag>"
        "\n    - component: $CI_SERVER_FQDN/$CI_PROJECT_PATH/<name>@$CI_COMMIT_SHA"
    )


def cmd_pull(args, env: dict) -> int:
    token = env.get("GCM_TOKEN")
    try:
        components = parse_annotations(_read_ci_file(args))
    except (AnnotationError, OSError) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1

    if not components:
        _no_annotations_hint(args.ci_file)
        return 0

    cwd = Path(os.getcwd())
    _clean_components_dir(cwd / args.components_dir)
    rc = 0
    for comp in components:
        local_path = cwd / args.components_dir / f"{comp['component']}.yml"
        try:
            content = _fetch_component(
                source=comp["source"],
                version=comp["version"],
                name=comp["component"],
                sha=comp.get("sha"),
                token=token,
            )
        except DownloadError as exc:
            print(f"[error] {exc}", file=sys.stderr)
            rc = 1
            continue

        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(content)
        print(f"[pull] {comp['component']} @ {comp['version']}")

    if getattr(args, "commit", False) and rc == 0:
        rc = _commit_changes(args.components_dir, env)
    return rc


def cmd_list(args, env: dict) -> int:
    try:
        components = parse_annotations(_read_ci_file(args))
    except (AnnotationError, OSError) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1

    if not components:
        _no_annotations_hint(args.ci_file)
        return 0

    col_widths = {
        "source": max(len("source"), *(len(c["source"]) for c in components)),
        "component": max(len("component"), *(len(c["component"]) for c in components)),
        "version": max(len("version"), *(len(c["version"]) for c in components)),
        "component_ref": max(len("component_ref"), *(len(c["component_ref"]) for c in components)),
    }

    header = (
        f"{'source':<{col_widths['source']}}   "
        f"{'component':<{col_widths['component']}}   "
        f"{'version':<{col_widths['version']}}   "
        f"{'component_ref':<{col_widths['component_ref']}}"
    )
    print(header)

    for comp in components:
        row = (
            f"{comp['source']:<{col_widths['source']}}   "
            f"{comp['component']:<{col_widths['component']}}   "
            f"{comp['version']:<{col_widths['version']}}   "
            f"{comp['component_ref']:<{col_widths['component_ref']}}"
        )
        print(row)

    return 0


def cmd_diff(args, env: dict) -> int:
    token = env.get("GCM_TOKEN")
    try:
        components = parse_annotations(_read_ci_file(args))
    except (AnnotationError, OSError) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1

    if not components:
        _no_annotations_hint(args.ci_file)
        return 0

    cwd = Path(os.getcwd())
    rc = 0
    for comp in components:
        local_path = cwd / args.components_dir / f"{comp['component']}.yml"
        try:
            remote_content = _fetch_component(
                source=comp["source"],
                version=comp["version"],
                name=comp["component"],
                sha=comp.get("sha"),
                token=token,
            )
        except DownloadError as exc:
            print(f"[error] {exc}", file=sys.stderr)
            rc = 1
            continue

        label = f"{comp['component']}.yml"
        if not local_path.exists():
            print(f"[new]      {label}")
            rc = 1
        elif local_path.read_text() == remote_content:
            print(f"[up-to-date] {label}")
        else:
            print(f"[changed]  {label}")
            rc = 1

    return rc


def main():
    parser = argparse.ArgumentParser(
        prog="gcm",
        description="GitLab CI/CD Component Manager",
    )
    parser.add_argument(
        "--ci-file",
        default=".gitlab-ci.yml",
        metavar="PATH",
        help="Path to .gitlab-ci.yml (default: .gitlab-ci.yml)",
    )
    parser.add_argument(
        "--components-dir",
        default="templates",
        metavar="PATH",
        help="Directory to write components into (default: templates)",
    )
    subparsers = parser.add_subparsers(dest="command")
    pull_parser = subparsers.add_parser("pull", help="Download all annotated components")
    pull_parser.add_argument(
        "--commit",
        action="store_true",
        help="Stage and commit downloaded components via git",
    )
    subparsers.add_parser("list", help="Print all annotated components")
    subparsers.add_parser("diff", help="Show which components would change")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    env = dict(os.environ)
    commands = {
        "pull": cmd_pull,
        "list": cmd_list,
        "diff": cmd_diff,
    }
    sys.exit(commands[args.command](args, env))


if __name__ == "__main__":
    main()
