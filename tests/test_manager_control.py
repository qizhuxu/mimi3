import asyncio
import unittest


class ManagerControlConfigTests(unittest.TestCase):
    def test_manager_autostart_is_runtime_config_field(self):
        from mimo2api.runtime_config import FIELDS

        self.assertIn("manager.autostart", FIELDS)
        field = FIELDS["manager.autostart"]

        self.assertEqual(field.env, "MIMO_MANAGER_AUTOSTART")
        self.assertEqual(field.value_type, "bool")
        self.assertTrue(field.default)


class ManagerControlApiTests(unittest.TestCase):
    def setUp(self):
        import mimo2api.web_service as web_service

        self.web_service = web_service
        self.originals = {}
        for name in (
            "acquire_single_process_lock",
            "get_config_value",
            "init_metrics_db",
            "lifecycle_monitor_worker",
            "metrics_history_worker",
            "reclassify_history",
            "release_single_process_lock",
            "start_manager_tasks",
            "sweep_stale_queues",
            "sync_bridge_ws_env",
            "tunnel_supervisor",
        ):
            self.originals[name] = getattr(web_service, name, None)
        web_service.manager_bg_task = None
        web_service.metrics_persist_task = None
        web_service.sweeper_bg_task = None
        web_service.lifecycle_bg_task = None

    def tearDown(self):
        self.web_service.manager_bg_task = None
        self.web_service.metrics_persist_task = None
        self.web_service.sweeper_bg_task = None
        self.web_service.lifecycle_bg_task = None
        for name, value in self.originals.items():
            if value is None and hasattr(self.web_service, name):
                delattr(self.web_service, name)
            elif value is not None:
                setattr(self.web_service, name, value)

    def test_manager_control_routes_are_registered(self):
        paths = {
            (route.path, ",".join(sorted(getattr(route, "methods", None) or [])))
            for route in self.web_service.app.routes
        }

        self.assertIn(("/api/manager/status", "GET"), paths)
        self.assertIn(("/api/manager/start", "POST"), paths)
        self.assertIn(("/api/manager/stop", "POST"), paths)
        self.assertIn(("/api/bridge-prompts", "GET"), paths)
        self.assertIn(("/api/bridge-prompts", "PUT"), paths)
        self.assertIn(("/api/bridge-prompts/reset", "POST"), paths)
        self.assertIn(("/api/bridge-prompts/import", "POST"), paths)
        self.assertIn(("/api/bridge-prompts/export", "GET"), paths)

    def test_start_manager_is_idempotent_and_stop_cancels_only_local_task(self):
        self.assertTrue(hasattr(self.web_service, "start_manager_background"))
        self.assertTrue(hasattr(self.web_service, "stop_manager_background"))
        self.assertTrue(hasattr(self.web_service, "manager_task_status"))

        async def scenario():
            started = 0

            async def fake_start_manager_tasks():
                nonlocal started
                started += 1
                await asyncio.Event().wait()

            self.web_service.start_manager_tasks = fake_start_manager_tasks
            self.web_service.get_config_value = lambda key, default=None: (
                True if key == "manager.autostart" else default
            )

            first = await self.web_service.start_manager_background()
            await asyncio.sleep(0)
            second = await self.web_service.start_manager_background()

            self.assertTrue(first["started"])
            self.assertFalse(second["started"])
            self.assertEqual(started, 1)
            self.assertTrue(self.web_service.manager_task_status()["running"])

            stopped = await self.web_service.stop_manager_background()

            self.assertTrue(stopped["stopped"])
            self.assertFalse(self.web_service.manager_task_status()["running"])
            self.assertIsNone(self.web_service.manager_bg_task)

        asyncio.run(scenario())

    def test_lifespan_skips_manager_when_autostart_is_disabled(self):
        async def scenario():
            manager_starts = 0

            async def fake_start_manager_tasks():
                nonlocal manager_starts
                manager_starts += 1

            async def idle_worker():
                await asyncio.Event().wait()

            class DummyTunnelSupervisor:
                async def start(self):
                    return None

                async def stop(self):
                    return None

                def snapshot(self):
                    return {"ws_url": "ws://gateway.test/ws"}

            self.web_service.acquire_single_process_lock = lambda: None
            self.web_service.release_single_process_lock = lambda: None
            self.web_service.init_metrics_db = lambda: None
            self.web_service.reclassify_history = lambda: 0
            self.web_service.sync_bridge_ws_env = lambda ws_url=None: ws_url or ""
            self.web_service.tunnel_supervisor = DummyTunnelSupervisor()
            self.web_service.metrics_history_worker = idle_worker
            self.web_service.sweep_stale_queues = idle_worker
            self.web_service.lifecycle_monitor_worker = idle_worker
            self.web_service.start_manager_tasks = fake_start_manager_tasks
            self.web_service.get_config_value = lambda key, default=None: (
                False if key == "manager.autostart" else default
            )

            async with self.web_service.lifespan(self.web_service.app):
                await asyncio.sleep(0)

            self.assertEqual(manager_starts, 0)

        asyncio.run(scenario())
