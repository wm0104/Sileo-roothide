#!/usr/bin/env python3
"""Publish the current test build, including authorized same-version replacements."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import quote


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def api(method, path, data=None, file=None, missing=False):
    args = ["gh", "api", "--include", "--method", method, path]
    if file:
        args += ["-H", "Content-Type: application/vnd.debian.binary-package", "--input", str(file)]
    elif data is not None:
        args += ["--input", "-"]
    result = subprocess.run(args, input=json.dumps(data) if data is not None else None,
                            text=True, capture_output=True)
    headers, separator, body = result.stdout.replace("\r\n", "\n").partition("\n\n")
    status = re.match(r"HTTP/\S+ (\d{3})", headers)
    code = int(status[1]) if status else None
    if missing and code == 404:
        return None
    require(result.returncode == 0 and code is not None and 200 <= code < 300,
            f"{method} {path}: HTTP {code or 'unknown'} (gh exit {result.returncode})")
    require(bool(separator), f"{method} {path}: missing response body")
    if code == 204:
        return None
    return json.loads(body)


def commit_notes(repo, sha, base, existing):
    """Use API commit subjects, not shallow checkout history or inferred features."""
    heading = "# 更新内容"
    root = f"repos/{repo}"
    trusted_main = "36f1e2ffbe18e0e82ff48e412ee4322b6e3225c5"
    current = api("GET", root + "/commits/" + sha)
    require(current.get("sha") == sha, "Current commit SHA mismatch")
    subjects = current.get("commit", {}).get("message", "").splitlines()
    # Only the authorized one-parent squash may replace the old notes range.
    reset = (subjects and subjects[0] == "添加配置备份"
             and [p.get("sha") for p in current.get("parents", [])] == [trusted_main])
    if reset:
        base, existing = trusted_main, None
    elif (base == sha and existing and existing.get("body") and not existing["body"].startswith(heading)
          and (not subjects or subjects[0] in existing["body"].splitlines())):
        return existing["body"]
    commits = []
    if base and base != sha:
        page = 1
        total = None
        while total is None or len(commits) < total:
            result = api("GET", root + f"/compare/{base}...{sha}?per_page=100&page={page}")
            require(result.get("status") == "ahead" and result.get("base_commit", {}).get("sha") == base,
                    "Commit comparison is not a valid forward publication range")
            count = result.get("total_commits")
            require(isinstance(count, int) and 0 < count <= 10000 and (total is None or count == total)
                    and (not reset or count == 1),
                    "Invalid commit comparison count")
            total = count
            batch = result.get("commits", [])
            require(bool(batch) and len(commits) + len(batch) <= total, "Incomplete commit comparison page")
            commits.extend(batch)
            page += 1
        require(commits[-1].get("sha") == sha, "Commit comparison does not end at build SHA")
    else:
        commits = [current]
        require(commits[0].get("sha") == sha, "Current commit SHA mismatch")
    lines = []
    seen = set()
    if existing and (existing.get("body") or "").startswith(heading):
        entry = re.compile(r"- .+ \(\[([0-9a-f]{7})\]\(https://github\.com/"
                           + re.escape(repo) + r"/commit/([0-9a-f]{40})\)\)")
        for line in existing["body"].splitlines():
            match = entry.fullmatch(line)
            if match and match[1] == match[2][:7] and match[2] not in seen:
                seen.add(match[2])
                lines.append(line[2:].rsplit(' ([', 1)[0].replace(r'\[', '[').replace(r'\]', ']'))
    elif existing:
        lines = list(dict.fromkeys(line for line in (existing.get("body") or "").splitlines() if line.strip()))
    range_seen = set()
    for commit in commits:
        commit_sha = commit.get("sha", "")
        subject = commit.get("commit", {}).get("message", "").splitlines()
        require(re.fullmatch(r"[0-9a-f]{40}", commit_sha) is not None and commit_sha not in range_seen
                and subject and subject[0].strip(), "Missing or invalid commit subject/SHA")
        range_seen.add(commit_sha)
        if commit_sha in seen:
            continue
        seen.add(commit_sha)
        title = subject[0].strip()
        if title not in lines:
            lines.append(title)
    require(bool(lines), "Empty generated release notes")
    return "\n".join(lines)


def main():
    repo = os.environ["GITHUB_REPOSITORY"]
    sha = os.environ["GITHUB_SHA"]
    require(repo == "wm0104/Sileo-roothide" and os.environ.get("GITHUB_REF") == "refs/heads/test"
            and os.environ.get("GITHUB_EVENT_NAME") == "push", "Publishing is restricted to test pushes")
    require(run("git", "rev-parse", "HEAD") == sha, "Checkout SHA differs from the build SHA")
    packages = Path("packages")
    require(not packages.is_symlink() and packages.is_dir(), "packages must be a real directory")
    debs = list(packages.glob("*.deb"))
    require(len(debs) == 1 and debs[0].is_file() and not debs[0].is_symlink(),
            "Expected exactly one regular, non-symlink DEB")
    deb = debs[0]
    identity, version, arch = [run("dpkg-deb", "--field", str(deb), key)
                               for key in ("Package", "Version", "Architecture")]
    require(identity == "org.coolstar.sileo" and arch == "iphoneos-arm64e", "Unexpected DEB identity/architecture")
    require(bool(version) and deb.name == f"{identity}_{version}_{arch}.deb", "DEB filename/Version mismatch")
    require(re.fullmatch(r"[0-9][0-9A-Za-z.+:~\-]*", version) is not None, "Unsafe DEB version")
    # Read both archives without extracting or executing maintainer scripts.
    subprocess.run(["dpkg-deb", "--ctrl-tarfile", str(deb)], stdout=subprocess.DEVNULL, check=True)
    subprocess.run(["dpkg-deb", "--contents", str(deb)], stdout=subprocess.DEVNULL, check=True)
    digest = "sha256:" + hashlib.sha256(deb.read_bytes()).hexdigest()
    size = deb.stat().st_size
    root = f"repos/{repo}"

    def current_head():
        require(api("GET", root + "/branches/test")["commit"]["sha"] == sha,
                "Remote test HEAD moved; refusing an obsolete build")

    def read_tag(tag=version):
        ref = api("GET", root + "/git/ref/tags/" + quote(tag, safe=""), missing=True)
        obj = ref["object"] if ref else None
        for _ in range(10):
            if obj is None or obj["type"] != "tag":
                break
            obj = api("GET", root + "/git/tags/" + obj["sha"])["object"]
        require(obj is None or obj["type"] == "commit", "Tag does not resolve to a commit")
        return ref, obj

    def check_tag():
        ref, obj = read_tag()
        require(obj is not None and obj["sha"] == sha, "Tag does not point to this exact build SHA")
        return ref is not None

    def set_tag():
        ref, obj = read_tag()
        if not ref:
            current_head()
            api("POST", root + "/git/refs", dict(ref="refs/tags/" + version, sha=sha))
        elif obj["sha"] != sha:
            # Only the exact package-version ref is mutable; never update other tags.
            current_head()
            api("PATCH", root + "/git/refs/tags/" + quote(version, safe=""), dict(sha=sha, force=True))
        require(check_tag(), "Exact build tag missing")

    current_head()
    previous = api("GET", root + "/releases/latest", missing=True)
    if previous is not None:
        latest_version = previous.get("tag_name")
        require(isinstance(latest_version, str)
                and re.fullmatch(r"[0-9][0-9A-Za-z.+:~\-]*", latest_version) is not None,
                "Malformed Latest version tag")
        for candidate in (version, latest_version):
            require(subprocess.run(["dpkg", "--validate-version", candidate],
                                   capture_output=True).returncode == 0,
                    "Invalid Debian version")
        comparison = subprocess.run(["dpkg", "--compare-versions", version, "lt", latest_version],
                                    capture_output=True)
        require(comparison.returncode in (0, 1), "Debian version comparison failed")
        require(comparison.returncode == 1, "Lower DEB version than Latest; refusing publication")
    existing = api("GET", root + "/releases/tags/" + quote(version, safe=""), missing=True)
    if existing:
        require(not existing["draft"], "Existing draft requires manual review")
        require(existing["tag_name"] == version, "Existing release tag mismatch")
    old_ref, old_commit = read_tag()
    base = old_commit["sha"] if old_commit else None
    if base is None:
        if previous and not previous["draft"] and previous["tag_name"] != version:
            _, previous_commit = read_tag(previous["tag_name"])
            base = previous_commit["sha"] if previous_commit else None
    body = commit_notes(repo, sha, base, existing)
    old_assets = api("GET", root + "/releases/" + str(existing["id"]) + "/assets") if existing else []
    # Preserve pre-mutation evidence in the Actions log, without credentials or package contents.
    print("Before publication: " + json.dumps(dict(tag=old_ref, commit=old_commit, notes_base=base,
          release=existing, assets=old_assets), ensure_ascii=False))
    if existing:
        release = existing
    else:
        set_tag()
        current_head()
        release = api("POST", root + "/releases",
                      dict(tag_name=version, name=version, body=body, draft=True, prerelease=False))
    release_path = root + "/releases/" + str(release["id"])

    def valid_asset(asset, name):
        return (asset["name"] == name and asset["size"] == size
                and asset.get("digest") == digest and asset.get("state") == "uploaded")

    # A matching package needs no replacement on same-SHA reruns; still refresh body/Latest.
    matching = [a for a in old_assets if valid_asset(a, deb.name)]
    if matching and old_commit and old_commit["sha"] == sha:
        uploaded = matching[0]
    else:
        # Stage alongside the public package, retaining the old asset until validation succeeds.
        import uuid
        staged_name = deb.name + "." + uuid.uuid4().hex + ".pending"
        upload = f"https://uploads.github.com/repos/{repo}/releases/{release['id']}/assets?name={quote(staged_name, safe='')}"
        current_head()
        uploaded = api("POST", upload, file=deb)
        assets = api("GET", release_path + "/assets")
        require(any(a["id"] == uploaded["id"] and valid_asset(a, staged_name) for a in assets),
                "Staged asset name/size/SHA256 verification failed; not publishing")
    require("sha256:" + hashlib.sha256(deb.read_bytes()).hexdigest() == digest,
            "Local DEB changed during upload; not publishing")
    current_head()
    set_tag()
    # Public same-version updates cannot be atomic across GitHub's ref/asset/release APIs.
    for asset in api("GET", release_path + "/assets"):
        if asset["id"] != uploaded["id"]:
            current_head()
            api("DELETE", root + "/releases/assets/" + str(asset["id"]))
    if uploaded["name"] != deb.name:
        current_head()
        api("PATCH", root + "/releases/assets/" + str(uploaded["id"]), dict(name=deb.name))
    assets = api("GET", release_path + "/assets")
    require(len(assets) == 1 and valid_asset(assets[0], deb.name),
            "Final asset name/count/size/SHA256 verification failed; not publishing")
    require(check_tag(), "Build tag disappeared; not publishing")
    current_head()
    api("PATCH", release_path, dict(name=version, body=body, draft=False, prerelease=False, make_latest="true"))
    published = api("GET", release_path)
    require(not published["draft"] and not published["prerelease"] and published["tag_name"] == version
            and published["name"] == version and published.get("body") == body, "Published release metadata mismatch")
    require(check_tag(), "Published tag missing")
    latest = api("GET", root + "/releases/latest")
    require(latest["id"] == release["id"] and latest["tag_name"] == version, "Release is not Latest")
    print(f"Published {version}: {deb.name} ({digest})")


if __name__ == "__main__":
    main()
