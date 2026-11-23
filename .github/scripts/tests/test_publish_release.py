"""Offline regression tests: execute the real publisher with fake subprocess/GitHub."""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit, unquote

SCRIPT = Path(__file__).resolve().parents[1] / "publish-release.py"
SPEC = importlib.util.spec_from_file_location("publisher", SCRIPT)
publisher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publisher)
VERSION = "2.5.1-15"
SHA = "a" * 40
OLD = "b" * 40
NAME = f"org.coolstar.sileo_{VERSION}_iphoneos-arm64e.deb"
TRUSTED_MAIN = "36f1e2ffbe18e0e82ff48e412ee4322b6e3225c5"
REAL_RUN = subprocess.run


class Fake:
    def __init__(self, existing=False, tag=OLD):
        self.calls = []
        self.tag = tag
        self.release = (dict(id=1, tag_name=VERSION, name=VERSION, body="old", draft=False,
                             prerelease=False) if existing else None)
        self.assets = [dict(id=2, name=NAME, size=3, digest="old", state="uploaded")] if existing else []
        self.head = SHA
        self.bad_upload = False
        self.fail_upload = False
        self.fail_final_patch = False
        self.move_on_upload = False
        self.invalid = False
        self.fields = dict(Package="org.coolstar.sileo", Version=VERSION, Architecture="iphoneos-arm64e")
        self.commits = [dict(sha=SHA, commit=dict(message="修正自动发布\n\n正文不应显示"))]
        self.latest = None
        self.latest_tag = OLD
        self.compare_status = "ahead"
        self.dpkg_compare_error = False
        self.main_compare_status = "ahead"

    def output(self, args, **kwargs):
        if args[:2] == ("git", "rev-parse"):
            return SHA
        if args[:2] == ("dpkg-deb", "--field"):
            return self.fields[args[-1]]
        raise AssertionError(args)

    def run(self, args, **kwargs):
        if args[0] == "dpkg":
            if self.dpkg_compare_error and args[1] == "--compare-versions":
                return subprocess.CompletedProcess(args, 2)
            return REAL_RUN(args, **kwargs)
        if args[0] == "dpkg-deb":
            if self.invalid:
                raise subprocess.CalledProcessError(2, args)
            return subprocess.CompletedProcess(args, 0)
        assert args[:4] == ["gh", "api", "--include", "--method"], args
        method, path = args[4:6]
        data = json.loads(kwargs["input"]) if kwargs.get("input") else None
        self.calls.append((method, path, data))
        code, result = 200, {}
        if "/compare/" in path:
            page = int(parse_qs(urlsplit(path).query)["page"][0])
            base = path.split("/compare/", 1)[1].split("...", 1)[0]
            result = dict(status=self.main_compare_status if base == TRUSTED_MAIN else self.compare_status,
                          base_commit=dict(sha=base),
                          total_commits=len(self.commits), commits=self.commits[(page-1)*100:page*100])
        elif "/commits/" in path:
            result = self.commits[-1] if self.commits else dict(sha=SHA, commit=dict(message=""))
        elif path.endswith("/releases/latest"):
            result = self.latest if self.latest is not None else self.release
            if result is None:
                code = 404
        elif path.endswith("/branches/test"):
            result = dict(commit=dict(sha=self.head))
        elif "/git/refs/tags/" in path:
            assert method == "PATCH" and path.endswith("/" + VERSION)
            assert data == dict(sha=SHA, force=True)
            self.tag = SHA
        elif "/git/ref/tags/" in path:
            assert method == "GET"
            target = self.tag if path.endswith("/" + VERSION) else self.latest_tag
            if target:
                result = dict(ref="refs/tags/" + VERSION, object=dict(type="commit", sha=target))
            else:
                code = 404
        elif path.endswith("/git/refs"):
            assert data == dict(ref="refs/tags/" + VERSION, sha=SHA)
            self.tag = SHA
        elif "/releases/tags/" in path:
            result = self.release
            if result is None:
                code = 404
        elif path.endswith("/releases") and method == "POST":
            self.release = dict(id=1, **data)
            result = self.release
        elif "uploads.github.com" in path:
            if self.fail_upload:
                code = 500
            else:
                payload = Path(args[args.index("--input") + 1]).read_bytes()
                asset = dict(id=99, name=unquote(parse_qs(urlsplit(path).query)["name"][0]),
                             size=len(payload), digest="sha256:" + hashlib.sha256(payload).hexdigest(), state="uploaded")
                if self.bad_upload:
                    asset["digest"] = "wrong"
                self.assets.append(asset)
                result = asset
                if self.move_on_upload:
                    self.head = OLD
        elif "/releases/assets/" in path:
            asset_id = int(path.rsplit("/", 1)[1])
            if method == "DELETE":
                self.assets = [a for a in self.assets if a["id"] != asset_id]
                code = 204
            elif method == "PATCH":
                result = next(a for a in self.assets if a["id"] == asset_id)
                result.update(data)
        elif path.endswith("/assets"):
            result = self.assets
        elif path.endswith("/releases/1"):
            if method == "PATCH":
                if self.fail_final_patch:
                    code = 500
                else:
                    self.release.update(data)
                    self.latest = None
            result = self.release
        elif path.endswith("/releases/latest"):
            result = self.release
        else:
            raise AssertionError((method, path, data))
        return subprocess.CompletedProcess(args, 0 if code < 300 else 1,
                                           f"HTTP/2 {code}\r\n\r\n" + (json.dumps(copy.deepcopy(result)) if code != 204 else ""), "")


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        Path("packages").mkdir()
        Path("packages", NAME).write_bytes(b"validated fake package")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(os.chdir, self.cwd)

    def execute(self, fake):
        with patch.dict(os.environ, dict(GITHUB_REPOSITORY="wm0104/Sileo-roothide", GITHUB_SHA=SHA,
                                        GITHUB_REF="refs/heads/test", GITHUB_EVENT_NAME="push")), \
                patch.object(publisher.subprocess, "check_output", fake.output), \
                patch.object(publisher.subprocess, "run", fake.run), contextlib.redirect_stdout(io.StringIO()):
            publisher.main()

    def assert_published(self, fake):
        self.execute(fake)
        self.assertEqual(fake.tag, SHA)
        self.assertNotIn("#", fake.release["body"])
        self.assertIn("修正自动发布", fake.release["body"])
        self.assertNotIn(SHA[:7], fake.release["body"])
        self.assertEqual(fake.release["name"], VERSION)
        self.assertEqual(fake.release["make_latest"], "true")
        self.assertFalse(fake.release["draft"])
        self.assertEqual([a["name"] for a in fake.assets], [NAME])
        self.assertEqual(fake.assets[0]["digest"], "sha256:" + hashlib.sha256(Path("packages", NAME).read_bytes()).hexdigest())

    def test_new_release_missing_tag(self):
        self.assert_published(Fake(tag=None))

    def test_deleted_release_retained_tag(self):
        fake = Fake()
        fake.commits.insert(0, dict(sha="c" * 40, commit=dict(message="之前的范围提交")))
        self.assert_published(fake)
        self.assertIn("之前的范围提交", fake.release["body"])
        self.assertNotIn("https://", fake.release["body"])
        compare = next(i for i, (m, p, d) in enumerate(fake.calls) if "/compare/" in p)
        switch = next(i for i, (m, p, d) in enumerate(fake.calls) if m == "PATCH" and "/git/refs/tags/" in p)
        self.assertLess(compare, switch)

    def test_existing_release_replaces_package_body_tag_latest(self):
        fake = Fake(existing=True)
        self.assert_published(fake)
        methods = [m for m, p, d in fake.calls]
        upload = next(i for i, (_, p, _) in enumerate(fake.calls) if "uploads.github.com" in p)
        self.assertGreater(methods.index("DELETE"), upload)

    def test_same_sha_rerun(self):
        fake = Fake(existing=True, tag=SHA)
        self.assert_published(fake)
        uploads = sum("uploads.github.com" in p for m, p, d in fake.calls)
        original_body = fake.release["body"]
        fake.commits = []  # Any regeneration would fail; same-SHA must retain the original range.
        self.assert_published(fake)
        self.assertEqual(fake.release["body"], original_body)
        self.assertEqual(sum("uploads.github.com" in p for m, p, d in fake.calls), uploads)
        self.assertFalse(any(m == "PATCH" and "/git/refs/" in p for m, p, d in fake.calls))

    def test_compare_pagination(self):
        fake = Fake(existing=True)
        fake.commits = [dict(sha=f"{i:040x}", commit=dict(message=f"提交 {i}")) for i in range(1, 101)] + fake.commits
        self.assert_published(fake)
        self.assertIn("提交 100", fake.release["body"])
        self.assertEqual(sum("/compare/" in p for m, p, d in fake.calls), 2)
        self.assertNotIn("https://", fake.release["body"])
        self.assertNotIn("正文不应显示", fake.release["body"])

    def test_no_anchor_only_current_commit(self):
        fake = Fake(tag=None)
        self.assert_published(fake)
        self.assertEqual(fake.release["body"], "修正自动发布")
        self.assertFalse(any("/compare/" in p for m, p, d in fake.calls))

    def test_latest_other_version_anchor(self):
        fake = Fake(tag=None)
        fake.latest = dict(tag_name="2.5.1-14", draft=False, prerelease=False)
        self.assert_published(fake)
        self.assertNotIn("https://", fake.release["body"])

    def test_empty_or_invalid_commit_range_fails_before_writes(self):
        for existing, status, commits in ((True, "ahead", []), (True, "diverged", None), (False, "ahead", [])):
            fake = Fake(existing=existing, tag=OLD if existing else None)
            fake.compare_status = status
            if commits is not None:
                fake.commits = commits
            with self.assertRaises(RuntimeError):
                self.execute(fake)
            self.assertFalse(any(m != "GET" for m, p, d in fake.calls))

    def test_stale_before_writes(self):
        fake = Fake(existing=True)
        fake.head = OLD
        with self.assertRaisesRegex(RuntimeError, "obsolete"):
            self.execute(fake)
        self.assertFalse(any(m != "GET" for m, p, d in fake.calls))

    def test_stale_after_upload_keeps_old_asset_tag(self):
        fake = Fake(existing=True)
        fake.move_on_upload = True
        with self.assertRaisesRegex(RuntimeError, "obsolete"):
            self.execute(fake)
        self.assertEqual(fake.tag, OLD)
        self.assertTrue(any(a["id"] == 2 for a in fake.assets))

    def test_bad_or_failed_upload_keeps_old_asset(self):
        for flag in ("bad_upload", "fail_upload"):
            fake = Fake(existing=True)
            setattr(fake, flag, True)
            with self.assertRaises(RuntimeError):
                self.execute(fake)
            self.assertTrue(any(a["id"] == 2 for a in fake.assets))
            self.assertEqual(fake.release["body"], "old")

    def test_invalid_package_no_writes(self):
        fake = Fake(existing=True)
        fake.invalid = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.execute(fake)
        self.assertEqual(fake.calls, [])

    def test_identity_architecture_filename_and_version_guards(self):
        for key, value in (("Package", "other"), ("Architecture", "iphoneos-arm64"),
                           ("Version", "9.9")):
            with self.subTest(key=key):
                fake = Fake(existing=True)
                fake.fields[key] = value
                with self.assertRaises(RuntimeError):
                    self.execute(fake)
                self.assertEqual(fake.calls, [])

    def test_sensitive_switches_have_immediate_head_check(self):
        fake = Fake(existing=True)
        self.assert_published(fake)
        for i, (method, path, data) in enumerate(fake.calls):
            if method == "DELETE" or (method == "PATCH" and
                    ("/git/refs/" in path or path.endswith("/releases/1"))):
                self.assertEqual(fake.calls[i - 1][0], "GET")
                self.assertTrue(fake.calls[i - 1][1].endswith("/branches/test"))

    def test_lower_version_blocks_all_writes(self):
        for existing in (False, True):
            for latest in ("2.5.1-16", "2.5.1-100", "1:1.0"):
                with self.subTest(existing=existing, latest=latest):
                    fake = Fake(existing=existing)
                    fake.latest = dict(tag_name=latest, draft=False)
                    with self.assertRaisesRegex(RuntimeError, "[Ll]ower"):
                        self.execute(fake)
                    self.assertFalse(any(m != "GET" for m, p, d in fake.calls))
                    self.assertTrue(Path("packages", NAME).is_file())

    def test_same_and_higher_version_allowed(self):
        for latest in (VERSION, "2.5.1-9", "2.5.1-15~rc1"):
            with self.subTest(latest=latest):
                fake = Fake(existing=True)
                fake.latest = dict(tag_name=latest, draft=False)
                self.assert_published(fake)

    def test_malformed_latest_blocks_all_writes(self):
        for latest in (None, "", "previous", "v2.5.1-16", "2.5.1_16", "2.5.1-", 42):
            with self.subTest(latest=latest):
                fake = Fake(existing=True)
                fake.latest = dict(tag_name=latest, draft=False)
                with self.assertRaises(RuntimeError):
                    self.execute(fake)
                self.assertFalse(any(m != "GET" for m, p, d in fake.calls))

    def test_same_version_accumulates_actual_sha_and_deduplicates(self):
        fake = Fake(existing=True)
        prior = f"- 旧功能 ([{OLD[:7]}](https://github.com/wm0104/Sileo-roothide/commit/{OLD}))"
        duplicate = f"- 修正自动发布 ([{SHA[:7]}](https://github.com/wm0104/Sileo-roothide/commit/{SHA}))"
        fake.release["body"] = "# 更新内容\n\n旧范围\n\n" + prior + "\n" + prior + "\n" + duplicate
        self.assert_published(fake)
        body = fake.release["body"]
        self.assertEqual(body.count("旧功能"), 1)
        self.assertEqual(body.count("修正自动发布"), 1)
        self.assertIn("旧功能", body)
        self.assertEqual(fake.tag, SHA)
        self.execute(fake)
        self.assertEqual(fake.release["body"], body)

    def test_new_version_does_not_accumulate_previous_body(self):
        fake = Fake(tag=None)
        fake.latest = dict(tag_name="2.5.1-14", draft=False,
                           body="# 更新内容\n\n旧版不应保留")
        self.assert_published(fake)
        self.assertNotIn("旧版不应保留", fake.release["body"])

    def test_authorized_squash_resets_notes_before_tag_write(self):
        fake = Fake(existing=True)
        fake.compare_status = "diverged"
        fake.commits = [dict(sha=SHA, parents=[dict(sha=TRUSTED_MAIN)],
                             commit=dict(message="添加配置备份"))]
        fake.release["body"] = ("# 更新内容\n\n"
            f"- 旧诊断标题 ([{OLD[:7]}](https://github.com/wm0104/Sileo-roothide/commit/{OLD}))")
        self.execute(fake)
        body = fake.release["body"]
        self.assertIn("添加配置备份", body)
        self.assertNotIn("旧诊断标题", body)
        self.assertEqual(body, "添加配置备份")
        compare = next(i for i, (m, p, d) in enumerate(fake.calls)
                       if f"/compare/{TRUSTED_MAIN}..." in p)
        write = next(i for i, (m, p, d) in enumerate(fake.calls) if m != "GET")
        self.assertLess(compare, write)
        self.execute(fake)
        self.assertEqual(fake.release["body"], body)

    def test_squash_final_patch_failure_rerun_resets_old_body(self):
        fake = Fake(existing=True)
        fake.compare_status = "diverged"
        fake.commits = [dict(sha=SHA, parents=[dict(sha=TRUSTED_MAIN)],
                             commit=dict(message="添加配置备份"))]
        old_body = ("# 更新内容\n\n"
            f"- 旧诊断标题 ([{OLD[:7]}](https://github.com/wm0104/Sileo-roothide/commit/{OLD}))")
        fake.release["body"] = old_body
        fake.fail_final_patch = True
        with self.assertRaisesRegex(RuntimeError, "PATCH .*releases/1: HTTP 500"):
            self.execute(fake)
        self.assertEqual(fake.tag, SHA)
        self.assertEqual(fake.release["body"], old_body)
        self.assertEqual([a["name"] for a in fake.assets], [NAME])
        self.assertEqual(fake.assets[0]["digest"],
                         "sha256:" + hashlib.sha256(Path("packages", NAME).read_bytes()).hexdigest())
        uploads = sum("uploads.github.com" in p for m, p, d in fake.calls)
        before_rerun = len(fake.calls)
        fake.fail_final_patch = False
        self.execute(fake)
        body = fake.release["body"]
        self.assertIn("添加配置备份", body)
        self.assertNotIn("旧诊断标题", body)
        self.assertEqual(body.splitlines(), ["添加配置备份"])
        self.assertEqual(body, "添加配置备份")
        self.assertEqual(sum("uploads.github.com" in p for m, p, d in fake.calls), uploads)
        rerun_calls = fake.calls[before_rerun:]
        commit_check = next(i for i, (m, p, d) in enumerate(rerun_calls) if "/commits/" in p)
        comparison = next(i for i, (m, p, d) in enumerate(rerun_calls) if "/compare/" in p)
        write = next(i for i, (m, p, d) in enumerate(rerun_calls) if m != "GET")
        self.assertLess(commit_check, comparison)
        self.assertLess(comparison, write)
        self.assertEqual(fake.release["make_latest"], "true")
        self.execute(fake)
        self.assertEqual(fake.release["body"], body)

    def test_arbitrary_divergence_never_uses_squash_exception(self):
        for parents, title in (([OLD], "添加配置备份"),
                               ([TRUSTED_MAIN, OLD], "添加配置备份"),
                               ([TRUSTED_MAIN], "添加配置备份 "),
                               ([TRUSTED_MAIN], "其他标题")):
            with self.subTest(parents=parents, title=title):
                fake = Fake(existing=True)
                fake.compare_status = "diverged"
                fake.commits = [dict(sha=SHA, parents=[dict(sha=p) for p in parents],
                                     commit=dict(message=title))]
                with self.assertRaises(RuntimeError):
                    self.execute(fake)
                self.assertFalse(any(m != "GET" for m, p, d in fake.calls))

    def test_version_comparison_error_fails_closed(self):
        fake = Fake(existing=True)
        fake.dpkg_compare_error = True
        with self.assertRaisesRegex(RuntimeError, "comparison failed"):
            self.execute(fake)
        self.assertFalse(any(m != "GET" for m, p, d in fake.calls))

    def test_squash_still_requires_valid_main_comparison(self):
        fake = Fake(existing=True)
        fake.main_compare_status = "diverged"
        fake.commits = [dict(sha=SHA, parents=[dict(sha=TRUSTED_MAIN)],
                             commit=dict(message="添加配置备份"))]
        with self.assertRaisesRegex(RuntimeError, "forward publication range"):
            self.execute(fake)
        self.assertFalse(any(m != "GET" for m, p, d in fake.calls))

    def test_existing_draft_is_untouched(self):
        fake = Fake(existing=True)
        fake.release["draft"] = True
        with self.assertRaisesRegex(RuntimeError, "draft"):
            self.execute(fake)
        self.assertFalse(any(m != "GET" for m, p, d in fake.calls))


if __name__ == "__main__":
    unittest.main()
