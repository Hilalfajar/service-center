import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import backend  # noqa: E402


class QuoteTests(unittest.TestCase):
    def test_safe_unchanged(self):
        self.assertEqual(backend.quote_unit_arg("/usr/bin/node"), "/usr/bin/node")

    def test_spaces_quoted(self):
        self.assertEqual(
            backend.quote_unit_arg("/mnt/data/My App/a.js"),
            '"/mnt/data/My App/a.js"',
        )

    def test_quotes_escaped(self):
        self.assertEqual(backend.quote_unit_arg('a"b'), '"a\\"b"')


class UnitTests(unittest.TestCase):
    def _service(self):
        return backend.Service(
            id="demo",
            name="Demo App",
            file="/mnt/data/PROGRAM/Demo App/app.py",
            command=["/usr/bin/python3", "/mnt/data/PROGRAM/Demo App/app.py"],
            workdir="/mnt/data/PROGRAM/Demo App",
            env={"FOO": "bar baz", "PORT": "5000"},
        )

    def test_build_unit(self):
        unit = backend.build_unit(self._service())
        self.assertIn('ConditionPathExists="/mnt/data/PROGRAM/Demo App/app.py"', unit)
        self.assertIn('ExecStart=/usr/bin/python3 "/mnt/data/PROGRAM/Demo App/app.py"', unit)
        self.assertIn('Environment="FOO=bar baz"', unit)
        self.assertIn('Environment="PORT=5000"', unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("WantedBy=default.target", unit)

    def test_invalid_restart_falls_back(self):
        service = self._service()
        service.restart = "bogus"
        self.assertIn("Restart=on-failure", backend.build_unit(service))

    def test_percent_escaped(self):
        service = self._service()
        service.env = {"TOKEN": "50%"}
        unit = backend.build_unit(service)
        self.assertIn('Environment="TOKEN=50%%"', unit)


class SlugTests(unittest.TestCase):
    def test_slug(self):
        self.assertEqual(backend.slugify("Toko Ghifari Web"), "toko-ghifari-web")
        self.assertEqual(backend.slugify("###"), "layanan")


class ParseShowTests(unittest.TestCase):
    def test_multi_unit(self):
        output = (
            "Id=sc-a.service\nActiveState=active\nSubState=running\n"
            "UnitFileState=enabled\nMainPID=123\n\n"
            "Id=sc-b.service\nActiveState=failed\nSubState=failed\n"
            "UnitFileState=disabled\nMainPID=0\n\n"
        )
        states = backend.parse_show(output)
        self.assertTrue(states["sc-a.service"].running)
        self.assertEqual(states["sc-a.service"].pid, "123")
        self.assertTrue(states["sc-b.service"].failed)


class DetectTests(unittest.TestCase):
    def test_python_with_venv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "venv" / "bin").mkdir(parents=True)
            venv_python = root / "venv" / "bin" / "python"
            venv_python.write_text("#!/bin/sh\n")
            app = root / "app.py"
            app.write_text("print('hi')\n")
            command, workdir = backend.detect_command(app)
            self.assertEqual(command, [str(venv_python), str(app)])
            self.assertEqual(workdir, root)

    def test_package_json_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg = root / "package.json"
            pkg.write_text('{"scripts": {"start": "node app.js"}}')
            command, workdir = backend.detect_command(pkg)
            self.assertTrue(command[0].endswith("npm"))
            self.assertEqual(command[1], "start")
            self.assertEqual(workdir, root)

    def test_package_json_main_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "index.js").write_text("console.log('hi')\n")
            pkg = root / "package.json"
            pkg.write_text('{"main": "index.js"}')
            command, workdir = backend.detect_command(pkg)
            self.assertEqual(command[1], str(root / "index.js"))
            self.assertEqual(workdir, root)

    def test_package_json_without_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "package.json"
            pkg.write_text("{}")
            with self.assertRaises(ValueError):
                backend.detect_command(pkg)

    def test_unknown_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notes.txt"
            path.write_text("x")
            with self.assertRaises(ValueError):
                backend.detect_command(path)


class ManagerTests(unittest.TestCase):
    def test_add_and_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "services.json"
            unit_dir = root / "units"
            manager = backend.Manager(registry_file=registry, unit_dir=unit_dir)
            script = root / "app.py"
            script.write_text("print('ok')\n")

            ok = mock.Mock(returncode=0, stdout="", stderr="")
            with mock.patch.object(backend, "systemctl", return_value=ok):
                service, warnings = manager.add(
                    name="Demo",
                    file=str(script),
                    command=["/usr/bin/python3", str(script)],
                    workdir=str(root),
                    env={},
                    restart="on-failure",
                    autostart=True,
                    start_now=True,
                )
                self.assertEqual(service.id, "demo")
                self.assertEqual(warnings, [])
                unit_file = unit_dir / "sc-demo.service"
                self.assertTrue(unit_file.exists())
                self.assertIn(
                    f"ExecStart=/usr/bin/python3 {script}",
                    unit_file.read_text(encoding="utf-8"),
                )

                reloaded = backend.Manager(registry_file=registry, unit_dir=unit_dir)
                self.assertIsNotNone(reloaded.get("demo"))

                manager.delete("demo")
                self.assertFalse(unit_file.exists())
                self.assertIsNone(manager.get("demo"))
                self.assertFalse(registry.read_text(encoding="utf-8").count("sc-demo"))
