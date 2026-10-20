import hashlib
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import httpx

from mcinst.catalog import CatalogService
from mcinst.cli import main
from mcinst.errors import MetadataError, PlanError
from mcinst.installation.models import InstanceSpec, TargetPlatform, safe_relative_path
from mcinst.installation.planner import InstallPlanService, build_plan, maven_path
from mcinst.installation.rules import rules_allow
from mcinst.models import AssetIndex, AssetIndexInfo, VersionMetadata, VersionResult
from mcinst.sources import SourcePolicy, download_candidates

FIXTURES = Path(__file__).parent / "fixtures"


def detail() -> VersionResult:
    raw = (FIXTURES / "vanilla_version.json").read_bytes()
    return VersionResult(
        metadata=VersionMetadata.model_validate_json(raw),
        sha1=hashlib.sha1(raw).hexdigest(),
        size=len(raw),
        source="bmclapi",
        url="https://bmclapi2.bangbang93.com/example/version.json",
        upstream_url="https://piston-meta.mojang.com/example/version.json",
    )


def asset_index() -> AssetIndex:
    return AssetIndex.model_validate_json((FIXTURES / "asset_index.json").read_bytes())


def spec(os="linux", arch="x86_64", version="6.0", features=None) -> InstanceSpec:
    return InstanceSpec(
        minecraft_version="test-vanilla",
        target=TargetPlatform(os=os, arch=arch, os_version=version),
        features=features or {},
    )


class RulesTests(unittest.TestCase):
    def test_default_and_last_matching_action(self):
        self.assertTrue(rules_allow(None, spec()))
        self.assertFalse(rules_allow([], spec()))
        rules = [
            {"action": "allow"},
            {"action": "disallow", "os": {"name": "linux"}},
            {"action": "allow", "os": {"name": "windows"}},
        ]
        self.assertFalse(rules_allow(rules, spec()))
        self.assertTrue(rules_allow(rules + [{"action": "allow"}], spec()))

    def test_os_arch_version_and_features_are_conjunctive(self):
        rules = [
            {
                "action": "allow",
                "os": {
                    "name": "linux",
                    "arch": "x86_64",
                    "version": r"6\..*",
                },
                "features": {"demo": True, "custom_resolution": False},
            }
        ]
        self.assertTrue(rules_allow(rules, spec(features={"demo": True})))
        self.assertFalse(rules_allow(rules, spec()))
        self.assertFalse(rules_allow(rules, spec(version="5.0", features={"demo": True})))
        self.assertFalse(rules_allow(rules, spec(arch="x86", features={"demo": True})))

    def test_unknown_os_version_is_not_silently_guessed(self):
        rules = [{"action": "allow", "os": {"name": "windows", "version": "10.*"}}]
        with self.assertRaisesRegex(PlanError, "os-version"):
            rules_allow(rules, spec(os="windows", version=None))
        self.assertFalse(rules_allow(rules, spec(os="linux", version=None)))

    def test_unknown_rule_syntax_is_rejected(self):
        for rules in (
            [{"action": "allow", "unknown": True}],
            [{"action": "maybe"}],
            [{"action": "allow", "os": {"arch": "["}}],
            [{"action": "allow", "features": {"demo": "false"}}],
            {"action": "allow"},
        ):
            with self.subTest(rules=rules), self.assertRaises(PlanError):
                rules_allow(rules, spec())

    def test_cross_platform_requires_explicit_architecture(self):
        with patch("mcinst.installation.models.platform.system", return_value="Darwin"):
            with self.assertRaisesRegex(ValueError, "arch"):
                TargetPlatform.detect(os="windows")
            target = TargetPlatform.detect(os="windows", arch="amd64")
        self.assertEqual(target.arch, "x86_64")
        self.assertIsNone(target.os_version)

    def test_mac_version_uses_product_version_instead_of_kernel(self):
        with (
            patch("mcinst.installation.models.platform.system", return_value="Darwin"),
            patch("mcinst.installation.models.platform.machine", return_value="arm64"),
            patch("mcinst.installation.models.platform.mac_ver", return_value=("15.7", (), "")),
            patch("mcinst.installation.models.platform.release", return_value="24.7.0"),
        ):
            target = TargetPlatform.detect()
        self.assertEqual(target.arch, "aarch64")
        self.assertEqual(target.os_version, "15.7")


class PlanTests(unittest.TestCase):
    def test_complete_plan_filters_platform_and_deduplicates_objects(self):
        plan = build_plan(spec(), detail(), asset_index())
        kinds = [a.kind for a in plan.artifacts]
        self.assertEqual(kinds.count("asset"), 2)  # Three names, two contents.
        self.assertEqual(len(plan.artifacts), 9)
        self.assertEqual(plan.declared_library_count, 3)
        self.assertFalse(any("windows-only" in a.path for a in plan.artifacts))
        self.assertEqual(plan.native_extractions[0].excludes, ["META-INF/"])
        self.assertIn("natives-linux", plan.native_extractions[0].archive)
        self.assertEqual(plan.java_requirement.majorVersion, 17)
        self.assertEqual(plan.unknown_size_count, 0)
        self.assertTrue(all(a.sha1 is not None for a in plan.artifacts))
        self.assertEqual(plan.artifacts[0].upstream_url, detail().upstream_url)
        self.assertEqual(plan.artifacts[0].candidates[0].name, "bmclapi")
        self.assertEqual(plan.known_download_bytes, sum(a.size for a in plan.artifacts))

    def test_windows_32_bit_classifier_substitution(self):
        plan = build_plan(spec("windows", "x86"), detail(), asset_index())
        self.assertIn("natives-windows-32", plan.native_extractions[0].archive)
        self.assertTrue(any("windows-only" in a.path for a in plan.artifacts))

    def test_modern_native_artifacts_are_not_legacy_extraction_steps(self):
        data = detail()
        data.metadata.libraries = [
            {
                "name": "org.example:lwjgl:3.3:natives-linux-arm64",
                "rules": [{"action": "allow", "os": {"name": "linux", "arch": "aarch64"}}],
                "downloads": {
                    "artifact": {
                        "url": "https://libraries.minecraft.net/example/native.jar",
                        "sha1": "1" * 40,
                        "size": 55,
                    }
                },
            }
        ]
        plan = build_plan(spec(arch="arm64"), data, asset_index())
        self.assertEqual([a.kind for a in plan.artifacts].count("native"), 1)
        self.assertEqual(plan.native_extractions, [])

    def test_legacy_natives_do_not_claim_arm64_support(self):
        with self.assertRaisesRegex(PlanError, "ARM64"):
            build_plan(spec(arch="arm64"), detail(), asset_index())

    def test_maven_fallback_exposes_missing_integrity(self):
        data = detail()
        data.metadata.libraries = [{"name": "org.example:old:1.0:extra@zip"}]
        plan = build_plan(spec(), data, asset_index())
        library = next(a for a in plan.artifacts if a.kind == "library")
        self.assertEqual(library.path, "libraries/org/example/old/1.0/old-1.0-extra.zip")
        self.assertIsNone(library.sha1)
        self.assertEqual(plan.unknown_size_count, 1)
        self.assertTrue(any("校验信息" in d for d in plan.diagnostics))

    def test_virtual_and_resource_mappings_share_downloads(self):
        index = asset_index()
        index.virtual = True
        index.map_to_resources = True
        plan = build_plan(spec(), detail(), index)
        self.assertEqual(len(plan.asset_copies), 6)
        self.assertEqual(sum(a.kind == "asset" for a in plan.artifacts), 2)
        destinations = [step.destination for step in plan.asset_copies]
        self.assertIn("assets/virtual/test-assets/minecraft/sounds/a.ogg", destinations)
        self.assertIn("resources/minecraft/sounds/a.ogg", destinations)

    def test_unsupported_formats_fail_instead_of_partial_success(self):
        for field, value in (("inheritsFrom", "base"), ("jar", "other"), ("assetIndex", None)):
            data = detail()
            setattr(data.metadata, field, value)
            with self.subTest(field=field), self.assertRaises(PlanError):
                build_plan(spec(), data, asset_index())

    def test_missing_java_requirement_stays_unknown(self):
        data = detail()
        data.metadata.javaVersion = None
        plan = build_plan(spec(), data, asset_index())
        self.assertIsNone(plan.java_requirement)
        self.assertTrue(any("不推断" in d for d in plan.diagnostics))

    def test_missing_client_checksum_rejects_plan(self):
        data = detail()
        del data.metadata.downloads["client"]["sha1"]
        with self.assertRaisesRegex(PlanError, "客户端"):
            build_plan(spec(), data, asset_index())

    def test_malformed_download_or_logging_is_a_plan_error(self):
        for change in (
            "null_downloads",
            "empty_path",
            "non_string_log_id",
            "orphan_extract",
            "empty_native_classifier",
        ):
            data = detail()
            if change == "null_downloads":
                data.metadata.libraries[0]["downloads"] = None
            elif change == "empty_path":
                data.metadata.libraries[0]["downloads"]["artifact"]["path"] = ""
            elif change == "non_string_log_id":
                data.metadata.logging["client"]["file"]["id"] = 123
            elif change == "orphan_extract":
                data.metadata.libraries[0]["extract"] = {"exclude": []}
            else:
                data.metadata.libraries[2]["natives"]["linux"] = ""
            with self.subTest(change=change), self.assertRaises(PlanError):
                build_plan(spec(), data, asset_index())

    def test_conflicting_library_targets_reject_plan(self):
        data = detail()
        duplicate = data.metadata.libraries[0].copy()
        duplicate["downloads"] = {
            "artifact": {
                **duplicate["downloads"]["artifact"],
                "sha1": "0" * 40,
            }
        }
        data.metadata.libraries.append(duplicate)
        with self.assertRaisesRegex(PlanError, "不同下载声明"):
            build_plan(spec(), data, asset_index())

    def test_asset_alias_path_collisions_are_rejected(self):
        for names in (("sound/A", "sound/a"), ("sound", "sound/a")):
            index = AssetIndex.model_validate(
                {
                    "virtual": True,
                    "objects": {name: {"hash": "a" * 40, "size": 1} for name in names},
                }
            )
            with self.subTest(names=names), self.assertRaises(PlanError):
                build_plan(spec(), detail(), index)

    def test_unsafe_library_paths_are_rejected(self):
        data = detail()
        data.metadata.libraries[0]["downloads"]["artifact"]["path"] = "../../outside.jar"
        with self.assertRaisesRegex(PlanError, "相对路径"):
            build_plan(spec(), data, asset_index())

    def test_same_asset_hash_with_different_sizes_is_rejected(self):
        index = asset_index()
        index.objects["minecraft/sounds/b.ogg"].size = 999
        with self.assertRaisesRegex(PlanError, "不同下载声明"):
            build_plan(spec(), detail(), index)

    def test_path_and_maven_inputs_are_portable(self):
        self.assertEqual(
            maven_path("org.example:thing:1.0:all@zip"), "org/example/thing/1.0/thing-1.0-all.zip"
        )
        for path in ("../x", "/x", "a//b", "a\\b", "C:/x", "a/./b", "NUL.jar", "a. /b"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                safe_relative_path(path)
        for coordinate in ("org:a", "org:a:../../x", "org..bad:a:1", "org:a:1@"):
            with self.subTest(coordinate=coordinate), self.assertRaises((ValueError, PlanError)):
                maven_path(coordinate)


class PlanServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_plan_only_fetches_three_metadata_documents(self):
        version_bytes = (FIXTURES / "vanilla_version.json").read_bytes()
        index_bytes = (FIXTURES / "asset_index.json").read_bytes()
        visited = []

        def handle(request):
            visited.append(request.url.path)
            if request.url.path.endswith("version_manifest_v2.json"):
                return httpx.Response(
                    200,
                    json={
                        "latest": {"release": "test-vanilla", "snapshot": "test-vanilla"},
                        "versions": [
                            {
                                "id": "test-vanilla",
                                "type": "release",
                                "url": "https://piston-meta.mojang.com/example/version.json",
                                "sha1": hashlib.sha1(version_bytes).hexdigest(),
                                "time": "2026-01-01T00:00:00Z",
                                "releaseTime": "2026-01-01T00:00:00Z",
                            }
                        ],
                    },
                )
            if request.url.path.endswith("version.json"):
                return httpx.Response(200, content=version_bytes)
            if request.url.path.endswith("assets.json"):
                return httpx.Response(200, content=index_bytes)
            self.fail(f"规划阶段不应下载游戏文件：{request.url}")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            plan = await InstallPlanService(CatalogService(client)).plan(spec())
        self.assertEqual(len(visited), 3)
        self.assertEqual(plan.artifacts[0].size, len(version_bytes))
        self.assertEqual(plan.artifacts[0].sha1, hashlib.sha1(version_bytes).hexdigest())

    async def test_asset_index_size_and_hash_are_verified_before_expansion(self):
        raw = (FIXTURES / "asset_index.json").read_bytes()
        for body in (raw + b" ", b"x" * len(raw)):
            transport = httpx.MockTransport(lambda _, body=body: httpx.Response(200, content=body))
            async with httpx.AsyncClient(transport=transport) as client:
                info = AssetIndexInfo.model_validate(detail().metadata.assetIndex)
                with self.subTest(body_size=len(body)), self.assertRaises(MetadataError):
                    await CatalogService(client, SourcePolicy.OFFICIAL).asset_index(info)


class PlanCliAndSourceTests(unittest.TestCase):
    def test_plan_json_is_machine_readable(self):
        plan = build_plan(spec(), detail(), asset_index())
        with patch("mcinst.cli.InstallPlanService.plan", return_value=plan):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = main(["plan", "test-vanilla", "--os", "linux", "--arch", "x86_64", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(out.getvalue())["known_download_bytes"], plan.known_download_bytes
        )
        self.assertEqual(err.getvalue(), "")

    def test_plan_features_and_human_preview(self):
        plan = build_plan(spec(), detail(), asset_index())
        with patch("mcinst.cli.InstallPlanService.plan", return_value=plan) as generate:
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = main(
                    [
                        "plan",
                        "test-vanilla",
                        "--os",
                        "linux",
                        "--arch",
                        "x86_64",
                        "--feature",
                        "demo=true",
                        "--files",
                    ]
                )
        self.assertEqual(code, 0)
        self.assertTrue(generate.call_args.args[0].features["demo"])
        self.assertIn("尚未安装", out.getvalue())
        self.assertIn("[extract]", out.getvalue())

    def test_source_mapping_keeps_path_and_query(self):
        for host, prefix in (
            ("libraries.minecraft.net", "/maven"),
            ("resources.download.minecraft.net", "/assets"),
            ("piston-data.mojang.com", ""),
        ):
            url = f"https://{host}/example/file?x=1"
            with self.subTest(host=host):
                candidates = download_candidates(url, SourcePolicy.AUTO)
                self.assertEqual(
                    candidates[0].url, f"https://bmclapi2.bangbang93.com{prefix}/example/file?x=1"
                )
                self.assertEqual(candidates[1].url, url)

    def test_unknown_download_origin_is_not_rewritten(self):
        with self.assertRaises(ValueError):
            download_candidates("https://example.org/libraries.minecraft.net/x", SourcePolicy.AUTO)
