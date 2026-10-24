import asyncio
import importlib.util
import tempfile
import unittest
from pathlib import Path

import httpx
from test_installer import FixtureServer

from mcinst.installation.repository import state_directory
from mcinst.installation.state import load_state
from mcinst.installation.verify import verify_instance
from mcinst.sources import SourcePolicy
from mcinst.ui.service import InterfaceService

TEXTUAL_AVAILABLE = importlib.util.find_spec("textual") is not None
if TEXTUAL_AVAILABLE:
    from textual.widgets import Button, ContentSwitcher, Input, OptionList, Select

    from mcinst.ui.app import McinstApp


@unittest.skipUnless(TEXTUAL_AVAILABLE, "需要 uv sync 安装 Textual")
class TuiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / ".minecraft"
        self.server = FixtureServer()
        self.service = InterfaceService(httpx.MockTransport(self.server.handle))

    async def until(self, predicate, pilot):
        for _ in range(150):
            await pilot.pause()
            if predicate():
                return
            await asyncio.sleep(0.02)
        self.fail("TUI 操作没有完成")

    async def configure(self, app, pilot):
        await self.until(lambda: app.selected is not None, pilot)
        app.query_one("#platform", Select).value = "linux"
        await pilot.pause()
        app.query_one("#os-version", Input).value = "6.0"
        app.query_one("#instance-name", Input).value = "测试实例"
        await pilot.pause()

    async def test_named_install_preview_and_local_verify(self):
        app = McinstApp(self.root, SourcePolicy.OFFICIAL, service=self.service)
        async with app.run_test(size=(110, 45)) as pilot:
            await self.configure(app, pilot)
            self.assertFalse(app.query_one("#task").display)
            app.query_one("#preview", Button).press()
            await self.until(lambda: app.prepared is not None, pilot)
            self.assertFalse(self.root.exists())
            self.assertEqual(app.prepared.spec.instance_id, "测试实例")
            app.query_one("#install", Button).press()
            await self.until(lambda: not app.busy and self.root.exists(), pilot)
            await self.until(lambda: bool(app.instances), pilot)
            self.assertTrue(verify_instance(self.root, "测试实例").ok)
            self.assertTrue(app.query_one("#preview", Button).disabled)
            app.query_one("#nav-local", Button).press()
            await pilot.pause()
            self.assertEqual(app.query_one("#pages", ContentSwitcher).current, "local")
            app.query_one("#verify", Button).press()
            await self.until(lambda: not app.busy, pilot)
            self.assertTrue(verify_instance(self.root, "测试实例").ok)

    async def test_name_validation_and_changed_configuration_invalidates_preview(self):
        app = McinstApp(self.root, SourcePolicy.OFFICIAL, service=self.service)
        async with app.run_test(size=(110, 45)) as pilot:
            await self.configure(app, pilot)
            app.query_one("#instance-name", Input).value = "../escape"
            await pilot.pause()
            self.assertTrue(app.query_one("#preview", Button).disabled)
            app.query_one("#instance-name", Input).value = "测试实例"
            await pilot.pause()
            app.query_one("#preview", Button).press()
            await self.until(lambda: app.prepared is not None, pilot)
            app.query_one("#instance-name", Input).value = "改名"
            await pilot.pause()
            self.assertIsNone(app.prepared)
            self.assertFalse(app.query_one("#plan").display)
            self.assertFalse(self.root.exists())

    async def test_network_failure_can_retry_without_exiting(self):
        self.server.fail_path = "/mc/game/version_manifest_v2.json"
        app = McinstApp(self.root, SourcePolicy.OFFICIAL, service=self.service)
        async with app.run_test(size=(100, 35)) as pilot:
            await self.until(lambda: app.query_one("#error").display, pilot)
            self.assertIsNone(app.selected)
            self.server.fail_path = None
            await pilot.press("ctrl+r")
            await self.until(lambda: app.selected is not None, pilot)
            self.assertGreater(len(app.entries), 0)

    async def test_cancel_install_releases_lock_and_can_repair_offline(self):
        self.server.wait_path = "/client.jar"
        app = McinstApp(self.root, SourcePolicy.OFFICIAL, service=self.service)
        async with app.run_test(size=(110, 45)) as pilot:
            await self.configure(app, pilot)
            app.query_one("#preview", Button).press()
            await self.until(lambda: app.prepared is not None, pilot)
            app.query_one("#install", Button).press()
            await asyncio.wait_for(self.server.started.wait(), 3)
            app.query_one("#cancel", Button).press()
            await self.until(lambda: not app.busy, pilot)
            record = load_state(state_directory(self.root, "测试实例"))[1]
            self.assertEqual(record.status, "cancelled")
            self.assertEqual(list(self.root.rglob("*.part")), [])
            self.server.wait_path = None
            await self.until(lambda: bool(app.instances), pilot)
            self.server.forbid_metadata = True
            app.begin_repair(app.instances[0])
            await self.until(lambda: app.prepared is not None, pilot)
            app.query_one("#install", Button).press()
            await self.until(lambda: not app.busy, pilot)
            self.assertTrue(verify_instance(self.root, "测试实例").ok)

    async def test_keyboard_filter_and_narrow_layout(self):
        app = McinstApp(self.root, SourcePolicy.OFFICIAL, service=self.service)
        async with app.run_test(size=(65, 30)) as pilot:
            await self.until(lambda: app.selected is not None, pilot)
            self.assertTrue(app.has_class("narrow"))
            app.query_one("#version-list", OptionList).focus()
            await pilot.press("slash")
            self.assertEqual(app.focused.id, "search")
            app.query_one("#search", Input).value = "no such version"
            await pilot.pause()
            self.assertEqual(app.visible_entries, [])
            self.assertTrue(app.query_one("#preview", Button).disabled)
            app.query_one("#search", Input).value = "test"
            await self.until(lambda: bool(app.visible_entries), pilot)
