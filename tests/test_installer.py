import asyncio
import hashlib
import io
import json
import stat
import tempfile
import threading
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import httpx

from mcinst.catalog import CatalogService
from mcinst.cli import main
from mcinst.downloads import RetryPolicy
from mcinst.downloads.errors import DownloadError, DownloadFailed
from mcinst.installation.errors import InstallationError
from mcinst.installation.installer import VanillaInstaller
from mcinst.installation.layout import migrate_layout
from mcinst.installation.models import InstanceSpec, TargetPlatform
from mcinst.installation.state import finish_thread, instance_lock, load_state
from mcinst.installation.verify import verify_instance
from mcinst.sources import SourcePolicy


def sha1(data):
    return hashlib.sha1(data).hexdigest()


def zip_bytes(members=None):
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w") as archive:
        for name, content in (
            members or {"native/libtest.so": b"native", "META-INF/a": b"skip"}
        ).items():
            archive.writestr(name, content)
    return result.getvalue()


class FixtureServer:
    """Synthetic, internally consistent metadata and actual native ZIP contents."""

    def __init__(self, native=None):
        self.contents = {}
        self.visited = []
        self.fail_path = None
        self.forbid_metadata = False
        self.wait_path = None
        self.started = asyncio.Event()
        asset = b"asset contents"
        asset_hash = sha1(asset)
        self.contents[f"/{asset_hash[:2]}/{asset_hash}"] = asset
        index = json.dumps(
            {
                "virtual": True,
                "map_to_resources": True,
                "objects": {"minecraft/test.txt": {"hash": asset_hash, "size": len(asset)}},
            }
        ).encode()
        self.contents["/assets.json"] = index
        client = self.file("/client.jar", b"client")
        library = self.file("/example/common.jar", b"common", "libraries.minecraft.net")
        native_info = self.file(
            "/example/native.jar",
            native if native is not None else zip_bytes(),
            "libraries.minecraft.net",
        )
        library["path"] = "example/common.jar"
        native_info["path"] = "example/native.jar"
        version = {
            "id": "test-vanilla",
            "mainClass": "test.Client",
            "type": "release",
            "downloads": {"client": client},
            "javaVersion": {"component": "test", "majorVersion": 17},
            "assetIndex": {
                "id": "test-assets",
                "url": "https://piston-meta.mojang.com/assets.json",
                "sha1": sha1(index),
                "size": len(index),
            },
            "libraries": [
                {"name": "example:common:1", "downloads": {"artifact": library}},
                {
                    "name": "example:native:1",
                    "natives": {"linux": "natives-linux"},
                    "downloads": {"classifiers": {"natives-linux": native_info}},
                    "extract": {"exclude": ["META-INF/"]},
                },
            ],
            "logging": {
                "client": {"file": {**self.file("/client.xml", b"<logging/>"), "id": "client.xml"}}
            },
        }
        raw_version = json.dumps(version).encode()
        self.contents["/version.json"] = raw_version
        self.contents["/mc/game/version_manifest_v2.json"] = json.dumps(
            {
                "latest": {"release": "test-vanilla", "snapshot": "test-vanilla"},
                "versions": [
                    {
                        "id": "test-vanilla",
                        "type": "release",
                        "url": "https://piston-meta.mojang.com/version.json",
                        "sha1": sha1(raw_version),
                        "time": "2026-01-01T00:00:00Z",
                        "releaseTime": "2026-01-01T00:00:00Z",
                    }
                ],
            }
        ).encode()

    def file(self, path, content, host="piston-data.mojang.com"):
        self.contents[path] = content
        return {"url": f"https://{host}{path}", "sha1": sha1(content), "size": len(content)}

    async def handle(self, request):
        path = request.url.path
        self.visited.append(path)
        if self.forbid_metadata and path == "/mc/game/version_manifest_v2.json":
            raise AssertionError("恢复实例时不应重新解析版本清单")
        if path == self.fail_path:
            return httpx.Response(503)
        if path == self.wait_path:
            started = self.started

            class Wait(httpx.AsyncByteStream):
                async def __aiter__(self):
                    started.set()
                    await asyncio.Event().wait()
                    yield b"unreachable"

            return httpx.Response(200, stream=Wait())
        if path not in self.contents:
            raise AssertionError(f"未预期的请求：{path}")
        return httpx.Response(200, content=self.contents[path])


class InstallerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.directory = self.base / "instance"
        self.spec = InstanceSpec(
            minecraft_version="test-vanilla",
            target=TargetPlatform(os="linux", arch="x86_64", os_version="6.0"),
        )
        self.addCleanup(self.temporary.cleanup)

    def installer(self, client, concurrency=3):
        result = VanillaInstaller(
            CatalogService(client, SourcePolicy.OFFICIAL), concurrency=concurrency, per_source=2
        )
        result.downloader.retry = RetryPolicy(attempts_per_source=1)
        return result

    def make_legacy(self):
        (self.directory / ".minecraft").rename(self.directory / "minecraft")
        record = self.directory / "instance.json"
        content = json.loads(record.read_text())
        content.pop("minecraft_directory")
        record.write_text(json.dumps(content))

    async def test_complete_install_and_offline_verify(self):
        server = FixtureServer()
        events = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(server.handle)) as client:
            result = await self.installer(client).install(self.spec, self.directory, events.append)
        root = result.minecraft_directory
        self.assertEqual(root.name, ".minecraft")
        self.assertEqual(load_state(self.directory)[1].minecraft_directory, ".minecraft")
        self.assertEqual((root / "versions/test-vanilla/test-vanilla.jar").read_bytes(), b"client")
        self.assertEqual(
            (root / "versions/test-vanilla/natives/native/libtest.so").read_bytes(), b"native"
        )
        self.assertFalse((root / "versions/test-vanilla/natives/META-INF").exists())
        self.assertEqual((root / "resources/minecraft/test.txt").read_bytes(), b"asset contents")
        self.assertTrue((root / "assets/virtual/test-assets/minecraft/test.txt").is_file())
        self.assertEqual(load_state(self.directory)[1].status, "installed")
        self.assertTrue(verify_instance(self.directory).ok)
        self.assertEqual(result.downloaded_files, 7)
        self.assertEqual(result.derived_files, 3)
        self.assertEqual(events[-1].stage, "postprocessing")
        self.assertEqual(list(self.directory.rglob("*.part")), [])

    async def test_legacy_layout_is_verified_and_reused_without_migration(self):
        server = FixtureServer()
        async with httpx.AsyncClient(transport=httpx.MockTransport(server.handle)) as client:
            installer = self.installer(client)
            await installer.install(self.spec, self.directory)
            self.make_legacy()
            self.assertTrue(verify_instance(self.directory).ok)
            server.visited.clear()
            server.forbid_metadata = True
            result = await installer.install(self.spec, self.directory)
        self.assertEqual(result.minecraft_directory.name, "minecraft")
        self.assertEqual(result.reused_files, 7)
        self.assertEqual(server.visited, [])
        self.assertFalse((self.directory / ".minecraft").exists())

    async def test_layout_migration_preserves_files_and_pinned_plan(self):
        server = FixtureServer()
        async with httpx.AsyncClient(transport=httpx.MockTransport(server.handle)) as client:
            installer = self.installer(client)
            await installer.install(self.spec, self.directory)
            self.make_legacy()
            original_plan = (self.directory / "install-plan.json").read_bytes()
            old_jar = self.directory / "minecraft/versions/test-vanilla/test-vanilla.jar"
            original_inode = old_jar.stat().st_ino
            saves = self.directory / "minecraft/saves"
            saves.mkdir()
            (saves / "user.dat").write_bytes(b"user-data")
            migrated = migrate_layout(self.directory)
            self.assertTrue(migrated.changed)
            self.assertEqual(migrated.minecraft_directory.name, ".minecraft")
            self.assertFalse((self.directory / "minecraft").exists())
            jar = migrated.minecraft_directory / "versions/test-vanilla/test-vanilla.jar"
            self.assertEqual(jar.stat().st_ino, original_inode)
            self.assertEqual(jar.read_bytes(), b"client")
            self.assertEqual(
                (migrated.minecraft_directory / "saves/user.dat").read_bytes(), b"user-data"
            )
            self.assertEqual((self.directory / "install-plan.json").read_bytes(), original_plan)
            self.assertTrue(verify_instance(self.directory).ok)
            self.assertFalse(migrate_layout(self.directory).changed)
            server.visited.clear()
            result = await installer.install(self.spec, self.directory)
        self.assertEqual(result.reused_files, 7)
        self.assertEqual(server.visited, [])

    async def test_layout_migration_refuses_to_merge_two_existing_roots(self):
        server = FixtureServer()
        async with httpx.AsyncClient(transport=httpx.MockTransport(server.handle)) as client:
            await self.installer(client).install(self.spec, self.directory)
        self.make_legacy()
        other = self.directory / ".minecraft"
        other.mkdir()
        (other / "user.txt").write_bytes(b"keep")
        original_record = (self.directory / "instance.json").read_bytes()
        with self.assertRaisesRegex(InstallationError, "同时存在"):
            migrate_layout(self.directory)
        self.assertEqual((self.directory / "instance.json").read_bytes(), original_record)
        self.assertEqual((other / "user.txt").read_bytes(), b"keep")
        self.assertTrue(verify_instance(self.directory).ok)

    async def test_layout_migration_rolls_back_if_record_write_fails(self):
        server = FixtureServer()
        async with httpx.AsyncClient(transport=httpx.MockTransport(server.handle)) as client:
            await self.installer(client).install(self.spec, self.directory)
        self.make_legacy()
        original_record = (self.directory / "instance.json").read_bytes()
        with patch("mcinst.installation.layout.write_record", side_effect=OSError("disk error")):
            with self.assertRaisesRegex(InstallationError, "迁移失败"):
                migrate_layout(self.directory)
        self.assertEqual((self.directory / "instance.json").read_bytes(), original_record)
        self.assertFalse((self.directory / ".minecraft").exists())
        self.assertTrue(verify_instance(self.directory).ok)

    async def test_layout_migration_can_finish_an_interrupted_rename(self):
        server = FixtureServer()
        async with httpx.AsyncClient(transport=httpx.MockTransport(server.handle)) as client:
            await self.installer(client).install(self.spec, self.directory)
        self.make_legacy()
        (self.directory / "minecraft").rename(self.directory / ".minecraft")
        with self.assertRaisesRegex(InstallationError, "migrate-layout"):
            verify_instance(self.directory)
        self.assertTrue(migrate_layout(self.directory).changed)
        self.assertTrue(verify_instance(self.directory).ok)

    async def test_layout_migration_rejects_symlinked_game_root(self):
        server = FixtureServer()
        async with httpx.AsyncClient(transport=httpx.MockTransport(server.handle)) as client:
            await self.installer(client).install(self.spec, self.directory)
        self.make_legacy()
        external = self.base / "external-game"
        (self.directory / "minecraft").rename(external)
        (self.directory / "minecraft").symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(InstallationError, "符号链接"):
            migrate_layout(self.directory)
        self.assertTrue((external / "versions/test-vanilla/test-vanilla.jar").is_file())

    async def test_repeat_reuses_all_files_and_repairs_derived_outputs(self):
        server = FixtureServer()
        async with httpx.AsyncClient(transport=httpx.MockTransport(server.handle)) as client:
            installer = self.installer(client)
            first = await installer.install(self.spec, self.directory)
            root = first.minecraft_directory
            (root / "resources/minecraft/test.txt").unlink()
            (root / "versions/test-vanilla/natives/native/libtest.so").write_bytes(b"broken")
            user_save = root / "saves/user.dat"
            user_save.parent.mkdir()
            user_save.write_bytes(b"user save")
            self.assertFalse(verify_instance(self.directory).ok)
            server.visited.clear()
            server.forbid_metadata = True
            second = await installer.install(self.spec, self.directory)
        self.assertEqual(server.visited, [])
        self.assertEqual(second.reused_files, 7)
        self.assertEqual(second.downloaded_files, 0)
        self.assertEqual(user_save.read_bytes(), b"user save")
        self.assertTrue(verify_instance(self.directory).ok)

    async def test_failed_download_is_recorded_and_resumes_pinned_plan(self):
        server = FixtureServer()
        server.fail_path = "/client.jar"
        async with httpx.AsyncClient(transport=httpx.MockTransport(server.handle)) as client:
            installer = self.installer(client, concurrency=1)
            with self.assertRaises(DownloadFailed):
                await installer.install(self.spec, self.directory)
            self.assertEqual(load_state(self.directory)[1].status, "failed")
            self.assertEqual(load_state(self.directory)[1].downloaded_files, 1)
            plan_bytes = (self.directory / "install-plan.json").read_bytes()
            self.assertFalse(verify_instance(self.directory).ok)
            server.fail_path = None
            server.forbid_metadata = True
            result = await installer.install(self.spec, self.directory)
        self.assertGreaterEqual(result.reused_files, 1)
        self.assertEqual((self.directory / "install-plan.json").read_bytes(), plan_bytes)
        self.assertTrue(verify_instance(self.directory).ok)

    async def test_missing_download_is_repaired_without_replanning(self):
        server = FixtureServer()
        async with httpx.AsyncClient(transport=httpx.MockTransport(server.handle)) as client:
            installer = self.installer(client)
            first = await installer.install(self.spec, self.directory)
            client_path = first.minecraft_directory / "versions/test-vanilla/test-vanilla.jar"
            client_path.unlink()
            check = verify_instance(self.directory)
            self.assertIn("versions/test-vanilla/test-vanilla.jar", check.missing)
            server.visited.clear()
            server.forbid_metadata = True
            repaired = await installer.install(self.spec, self.directory)
        self.assertEqual(server.visited, ["/client.jar"])
        self.assertEqual(repaired.downloaded_files, 1)
        self.assertTrue(verify_instance(self.directory).ok)

    async def test_cancellation_records_state_and_releases_lock(self):
        server = FixtureServer()
        server.wait_path = "/client.jar"
        async with httpx.AsyncClient(transport=httpx.MockTransport(server.handle)) as client:
            installer = self.installer(client, concurrency=1)
            task = asyncio.create_task(installer.install(self.spec, self.directory))
            await asyncio.wait_for(server.started.wait(), timeout=3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(load_state(self.directory)[1].status, "cancelled")
            self.assertEqual(list(self.directory.rglob("*.part")), [])
            with instance_lock(self.directory):
                pass
            server.wait_path = None
            await installer.install(self.spec, self.directory)
        self.assertTrue(verify_instance(self.directory).ok)

    async def test_unsafe_archive_is_rejected_before_native_publication(self):
        for bad_name in ("../escape", "/escape", "folder\\escape", "C:/escape"):
            with self.subTest(name=bad_name):
                directory = self.base / str(len(list(self.base.iterdir())))
                native = zip_bytes({"good.so": b"okay", bad_name: b"bad"})
                server = FixtureServer(native)
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(server.handle)
                ) as client:
                    with self.assertRaises(InstallationError):
                        await self.installer(client).install(self.spec, directory)
                self.assertEqual(load_state(directory)[1].status, "failed")
                self.assertFalse(
                    (directory / ".minecraft/versions/test-vanilla/natives/good.so").exists()
                )
                self.assertFalse((self.base / "escape").exists())

    async def test_zip_symlinks_and_invalid_zip_fail(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            item = zipfile.ZipInfo("lib.so")
            item.create_system = 3
            item.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(item, "../../escape")
        for index, data in enumerate((buffer.getvalue(), b"not a zip")):
            directory = self.base / str(index)
            server = FixtureServer(data)
            async with httpx.AsyncClient(transport=httpx.MockTransport(server.handle)) as client:
                with self.assertRaises(InstallationError):
                    await self.installer(client).install(self.spec, directory)
            self.assertEqual(load_state(directory)[1].status, "failed")

    async def test_nonmanaged_nonempty_directory_is_preserved(self):
        self.directory.mkdir()
        user_file = self.directory / "user.txt"
        user_file.write_bytes(b"keep")
        server = FixtureServer()
        async with httpx.AsyncClient(transport=httpx.MockTransport(server.handle)) as client:
            with self.assertRaisesRegex(InstallationError, "非空"):
                await self.installer(client).install(self.spec, self.directory)
        self.assertEqual(user_file.read_bytes(), b"keep")
        self.assertEqual(server.visited, [])

    async def test_other_version_and_corrupt_plan_are_rejected(self):
        server = FixtureServer()
        async with httpx.AsyncClient(transport=httpx.MockTransport(server.handle)) as client:
            installer = self.installer(client)
            await installer.install(self.spec, self.directory)
            other = self.spec.model_copy(update={"minecraft_version": "other"})
            with self.assertRaisesRegex(InstallationError, "不同"):
                await installer.install(other, self.directory)
            (self.directory / "install-plan.json").write_bytes(b"{}")
            with self.assertRaisesRegex(InstallationError, "校验失败"):
                await installer.install(self.spec, self.directory)

    async def test_instance_lock_rejects_simultaneous_installer(self):
        server = FixtureServer()
        with instance_lock(self.directory):
            async with httpx.AsyncClient(transport=httpx.MockTransport(server.handle)) as client:
                with self.assertRaisesRegex(InstallationError, "已有"):
                    await self.installer(client).install(self.spec, self.directory)
        self.assertEqual(server.visited, [])

    async def test_resume_can_change_source_without_changing_pinned_plan(self):
        server = FixtureServer()
        async with httpx.AsyncClient(transport=httpx.MockTransport(server.handle)) as client:
            installer = self.installer(client)
            await installer.install(self.spec, self.directory)
            original = (self.directory / "install-plan.json").read_bytes()
            (self.directory / ".minecraft/versions/test-vanilla/test-vanilla.jar").unlink()

        visited = []

        def mirror(request):
            visited.append(request.url.host)
            self.assertEqual(request.url.path, "/client.jar")
            return httpx.Response(200, content=b"client")

        async with httpx.AsyncClient(transport=httpx.MockTransport(mirror)) as client:
            installer = VanillaInstaller(CatalogService(client, SourcePolicy.BMCLAPI))
            await installer.install(self.spec, self.directory)
        self.assertEqual(visited, ["bmclapi2.bangbang93.com"])
        self.assertEqual((self.directory / "install-plan.json").read_bytes(), original)

    async def test_symlinked_resource_output_does_not_modify_external_file(self):
        server = FixtureServer()
        async with httpx.AsyncClient(transport=httpx.MockTransport(server.handle)) as client:
            installer = self.installer(client)
            await installer.install(self.spec, self.directory)
            alias = self.directory / ".minecraft/resources/minecraft/test.txt"
            alias.unlink()
            external = self.base / "external.txt"
            external.write_bytes(b"keep")
            alias.symlink_to(external)
            with self.assertRaises(DownloadError) as error:
                await installer.install(self.spec, self.directory)
        self.assertIn("符号链接", str(error.exception))
        self.assertEqual(external.read_bytes(), b"keep")
        self.assertEqual(load_state(self.directory)[1].status, "failed")

    async def test_extraction_size_limit_fails_before_publication(self):
        server = FixtureServer()
        async with httpx.AsyncClient(transport=httpx.MockTransport(server.handle)) as client:
            with patch("mcinst.installation.postprocess.MAX_NATIVE_BYTES", 1):
                with self.assertRaisesRegex(InstallationError, "大小超过限制"):
                    await self.installer(client).install(self.spec, self.directory)
        self.assertFalse((self.directory / ".minecraft/versions/test-vanilla/natives").exists())
        self.assertEqual(load_state(self.directory)[1].status, "failed")

    async def test_cancellation_waits_for_filesystem_thread(self):
        started, release = threading.Event(), threading.Event()

        def operation():
            started.set()
            if not release.wait(3):
                raise RuntimeError("test barrier timed out")
            return True

        task = asyncio.create_task(finish_thread(operation))
        self.assertTrue(await asyncio.to_thread(started.wait, 2))
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task


class InstallCliTests(unittest.TestCase):
    def test_install_cli_json_and_verify(self):
        server = FixtureServer()
        original_client = httpx.AsyncClient

        def client_factory(**options):
            return original_client(transport=httpx.MockTransport(server.handle), **options)

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "cli-instance"
            with patch("mcinst.cli.httpx.AsyncClient", side_effect=client_factory):
                out = io.StringIO()
                with redirect_stdout(out), redirect_stderr(io.StringIO()):
                    code = main(
                        [
                            "install",
                            "test-vanilla",
                            "--directory",
                            str(directory),
                            "--os",
                            "linux",
                            "--arch",
                            "x86_64",
                            "--os-version",
                            "6.0",
                            "--source",
                            "official",
                            "--json",
                        ]
                    )
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(out.getvalue())["downloaded_files"], 7)
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(main(["verify", str(directory), "--json"]), 0)
            self.assertTrue(json.loads(out.getvalue())["ok"])
