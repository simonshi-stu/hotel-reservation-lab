import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("run_hotel", ROOT / "scripts" / "run_hotel.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class IntegratedLauncherTest(unittest.TestCase):
    def test_room_launch_preserves_primary_application(self):
        cwd, command = launcher.launch_spec("room", 8019)
        self.assertEqual(cwd, ROOT)
        self.assertEqual(command[2:], ["-m", "hotel_demo.server", "--port", "8019"])

    def test_type_launch_uses_isolated_working_directory(self):
        cwd, command = launcher.launch_spec("type", 8093)
        self.assertEqual(cwd, ROOT / "labs" / "sqlite_inventory")
        self.assertEqual(command[2:], ["-m", "hotel.demo", "--port", "8093"])
        self.assertTrue((cwd / "hotel" / "demo.py").is_file())

    def test_invalid_model_or_port_cannot_launch(self):
        for model, port in [("unknown", 8019), ("room", 0), ("type", 65536)]:
            with self.assertRaises(ValueError):
                launcher.launch_spec(model, port)


class Python310BusyCompatibilityTest(unittest.TestCase):
    def test_fallback_only_classifies_known_busy_messages(self):
        import sqlite3
        from hotel_sim.sqlite_store import _is_busy_error
        self.assertTrue(_is_busy_error(sqlite3.OperationalError("database is locked")))
        self.assertFalse(_is_busy_error(sqlite3.OperationalError("no such table: reservations")))

    def test_extended_code_retains_primary_busy_code(self):
        from hotel_sim.sqlite_store import _is_busy_error
        class ExtendedError(Exception):
            sqlite_errorcode = 5 | (2 << 8)
        self.assertTrue(_is_busy_error(ExtendedError("different message")))
