import json
import tempfile
import unittest
from pathlib import Path

import httpx
from test_installer import FixtureServer

from mcinst.catalog import CatalogService
from mcinst.installation.errors import InstallationError
from mcinst.installation.installer import VanillaInstaller
from mcinst.installation.models import InstanceSpec, TargetPlatform
from mcinst.installation.planner import InstallPlanService
from mcinst.installation.repository import state_directory
from mcinst.installation.state import load_state
from mcinst.installation.verify import verify_instance
from mcinst.sources import SourcePolicy
from mcinst.ui.service import InterfaceService, local_instances


class NamedInstanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / ".minecraft"
        self.server = FixtureServer()
        self.service = InterfaceService(httpx.MockTransport(self.server.handle))
        self.spec = InstanceSpec(
            minecraft_version="test-vanilla",
            name="生存世界",
            target=TargetPlatform(os="linux", arch="x86_64", os_version="6.0"),
        )

    async def install(self, spec=None):
        spec = spec or self.spec
        plan = await self.service.preview(self.root, spec, SourcePolicy.OFFICIAL)
        return await self.service.install(
            self.root, plan, SourcePolicy.OFFICIAL, 3, 2, lambda _: None
        )

    async def test_named_json_preserves_metadata_and_matches_folder_and_jar(self):
        await self.install()
        version = self.root / "versions/生存世界"
        content = json.loads((version / "生存世界.json").read_text())
        original = json.loads(self.server.contents["/version.json"])
        self.assertEqual(content.pop("id"), "生存世界")
        original.pop("id")
        self.assertEqual(content, original)
        self.assertEqual((version / "生存世界.jar").read_bytes(), b"client")
        self.assertEqual((version / "natives/native/libtest.so").read_bytes(), b"native")
        self.assertEqual(
            (state_directory(self.root, "生存世界") / "upstream.json").read_bytes(),
            self.server.contents["/version.json"],
        )
        self.assertTrue(verify_instance(self.root, "生存世界").ok)
        self.assertFalse((self.root / "versions/test-vanilla").exists())

    async def test_same_upstream_version_can_have_independent_named_instances(self):
        await self.install()
        other = self.spec.model_copy(update={"name": "创造世界"})
        result = await self.install(other)
        self.assertEqual(result.reused_files, 5)
        self.assertTrue(verify_instance(self.root, "生存世界").ok)
        self.assertTrue(verify_instance(self.root, "创造世界").ok)
        self.assertEqual(
            {item.name for item in local_instances(self.root)}, {"生存世界", "创造世界"}
        )
        self.assertEqual(
            {item.spec.minecraft_version for item in local_instances(self.root)}, {"test-vanilla"}
        )

    async def test_repair_rebuilds_named_json_offline_and_preserves_user_files(self):
        await self.install()
        version = self.root / "versions/生存世界"
        (version / "生存世界.json").write_text('{"id":"broken"}')
        (version / "saves").mkdir()
        (version / "saves/user.dat").write_bytes(b"keep")
        self.assertFalse(verify_instance(self.root, "生存世界").ok)
        self.server.visited.clear()
        self.server.forbid_metadata = True
        plan = await self.service.preview(self.root, self.spec, SourcePolicy.OFFICIAL, repair=True)
        result = await self.service.install(
            self.root, plan, SourcePolicy.OFFICIAL, 3, 2, lambda _: None
        )
        self.assertEqual(result.reused_files, 7)
        self.assertEqual(self.server.visited, [])
        self.assertEqual((version / "saves/user.dat").read_bytes(), b"keep")
        self.assertTrue(verify_instance(self.root, "生存世界").ok)

    async def test_preview_is_read_only_and_install_does_not_replan(self):
        plan = await self.service.preview(self.root, self.spec, SourcePolicy.OFFICIAL)
        self.assertFalse(self.root.exists())
        self.server.visited.clear()
        self.server.forbid_metadata = True
        await self.service.install(self.root, plan, SourcePolicy.OFFICIAL, 3, 2, lambda _: None)
        stored, _ = load_state(state_directory(self.root, "生存世界"))
        self.assertEqual(stored, plan)
        self.assertTrue(verify_instance(self.root, "生存世界").ok)

    async def test_duplicate_case_alias_and_unmanaged_names_are_rejected(self):
        spec = self.spec.model_copy(update={"name": "Survival"})
        await self.install(spec)
        self.server.visited.clear()
        for name in ("Survival", "survival"):
            with self.subTest(name=name), self.assertRaisesRegex(InstallationError, "已存在"):
                await self.service.preview(
                    self.root, self.spec.model_copy(update={"name": name}), SourcePolicy.OFFICIAL
                )
        self.assertEqual(self.server.visited, [])
        self.assertTrue(verify_instance(self.root, "Survival").ok)
        (self.root / "versions/unmanaged").mkdir()
        with self.assertRaisesRegex(InstallationError, "已存在"):
            await self.service.preview(
                self.root, self.spec.model_copy(update={"name": "unmanaged"}), SourcePolicy.OFFICIAL
            )

    async def test_installer_rejects_mismatched_preview_before_download(self):
        async with self.service.client() as client:
            catalog = CatalogService(client, SourcePolicy.OFFICIAL)
            plan = await InstallPlanService(catalog).plan(self.spec)
            self.server.visited.clear()
            other = self.spec.model_copy(update={"name": "另一个实例"})
            with self.assertRaisesRegex(InstallationError, "不一致"):
                await VanillaInstaller(catalog).install(other, self.root, plan=plan)
        self.assertEqual(self.server.visited, [])
        self.assertFalse((self.root / "versions/另一个实例").exists())

    async def test_old_record_without_name_keeps_original_layout_and_reuse(self):
        spec = self.spec.model_copy(update={"name": None})
        await self.install(spec)
        directory = state_directory(self.root, "test-vanilla")
        plan, record = load_state(directory)
        import hashlib

        data = plan.model_dump()
        data["spec"].pop("name")
        raw = json.dumps(data).encode()
        (directory / "install-plan.json").write_bytes(raw)
        record.plan_sha256 = hashlib.sha256(raw).hexdigest()
        (directory / "instance.json").write_text(record.model_dump_json())
        self.assertTrue(verify_instance(self.root).ok)
        self.server.visited.clear()
        plan = await self.service.preview(self.root, spec, SourcePolicy.OFFICIAL, repair=True)
        await self.service.install(self.root, plan, SourcePolicy.OFFICIAL, 3, 2, lambda _: None)
        self.assertEqual(self.server.visited, [])

    async def test_invalid_names_and_default_name_normalization(self):
        for name in (
            "",
            "../x",
            "a/b",
            "a\\b",
            "NUL",
            "CON.json",
            "name.",
            " name",
            "name ",
            "x" * 65,
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                InstanceSpec(**{**self.spec.model_dump(), "name": name})
        spec = InstanceSpec(**{**self.spec.model_dump(), "name": "test-vanilla"})
        self.assertIsNone(spec.name)
        self.assertEqual(spec.upstream_json_path, "versions/test-vanilla/test-vanilla.json")
