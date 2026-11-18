import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import db_restore  # noqa: E402


def request(**overrides):
    values = {
        "engine": "postgresql",
        "host": "localhost",
        "port": "5432",
        "user": "postgres",
        "password": "rahasia",
        "database": "portalweb",
        "dump": Path("/tmp/dump.sql"),
        "create_database": True,
        "clean": False,
    }
    values.update(overrides)
    return db_restore.RestoreRequest(**values)


class ValidateTests(unittest.TestCase):
    def test_valid(self):
        with tempfile.NamedTemporaryFile(suffix=".sql") as handle:
            self.assertIsNone(request(dump=Path(handle.name)).validate())

    def test_invalid_database_name(self):
        with tempfile.NamedTemporaryFile(suffix=".sql") as handle:
            for name in ("portal-web", "1portal", "", "portal;drop"):
                with self.assertRaises(db_restore.RestoreError):
                    request(database=name, dump=Path(handle.name)).validate()

    def test_invalid_port(self):
        with tempfile.NamedTemporaryFile(suffix=".sql") as handle:
            for port in ("abc", "0", "70000"):
                with self.assertRaises(db_restore.RestoreError):
                    request(port=port, dump=Path(handle.name)).validate()

    def test_invalid_host(self):
        with tempfile.NamedTemporaryFile(suffix=".sql") as handle:
            with self.assertRaises(db_restore.RestoreError):
                request(host="-h", dump=Path(handle.name)).validate()

    def test_missing_dump(self):
        with self.assertRaises(db_restore.RestoreError):
            request(dump=Path("/tmp/tidak-ada.sql")).validate()


class QuotingTests(unittest.TestCase):
    def test_ident(self):
        self.assertEqual(db_restore.quote_ident('a"b'), '"a""b"')

    def test_literal(self):
        self.assertEqual(db_restore.quote_literal("a'b"), "'a''b'")

    def test_redact(self):
        self.assertEqual(
            db_restore.redact("password rahasia salah", "rahasia"),
            "password *** salah",
        )


class FormatTests(unittest.TestCase):
    def test_plain_suffix(self):
        with tempfile.NamedTemporaryFile(suffix=".sql") as handle:
            handle.write(b"CREATE TABLE x();")
            handle.flush()
            self.assertEqual(db_restore.detect_format(Path(handle.name)), "plain")

    def test_custom_suffix(self):
        with tempfile.NamedTemporaryFile(suffix=".dump") as handle:
            self.assertEqual(db_restore.detect_format(Path(handle.name)), "custom")

    def test_magic_wins(self):
        with tempfile.NamedTemporaryFile(suffix=".sql") as handle:
            handle.write(b"PGDMP")
            handle.flush()
            self.assertEqual(db_restore.detect_format(Path(handle.name)), "custom")

    def test_unknown(self):
        with tempfile.NamedTemporaryFile(suffix=".txt") as handle:
            with self.assertRaises(db_restore.RestoreError):
                db_restore.detect_format(Path(handle.name))


class CommandTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("db_restore.shutil.which", side_effect=lambda name: f"/usr/bin/{name}")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_plain_command(self):
        with tempfile.NamedTemporaryFile(suffix=".sql") as handle:
            req = request(dump=Path(handle.name))
            command = db_restore.restore_command(req, "plain")
            text = " ".join(command)
            self.assertEqual(command[0], "/usr/bin/psql")
            self.assertIn("-f " + handle.name, text)
            self.assertIn("ON_ERROR_STOP=1", command)
            self.assertIn("-1", command)
            self.assertNotIn("rahasia", text)

    def test_plain_clean_command(self):
        with tempfile.NamedTemporaryFile(suffix=".sql") as handle:
            req = request(dump=Path(handle.name), clean=True)
            command = db_restore.restore_command(req, "plain")
            self.assertIn(db_restore._CLEAN_SQL, command)

    def test_custom_command(self):
        with tempfile.NamedTemporaryFile(suffix=".dump") as handle:
            req = request(dump=Path(handle.name), clean=True)
            command = db_restore.restore_command(req, "custom")
            self.assertEqual(command[0], "/usr/bin/pg_restore")
            self.assertIn("--no-owner", command)
            self.assertIn("--clean", command)
            self.assertIn("--if-exists", command)
            self.assertEqual(command[-1], handle.name)
            self.assertNotIn("rahasia", " ".join(command))


class RestoreFlowTests(unittest.TestCase):
    def test_rejects_missing_database_when_create_disabled(self):
        with tempfile.NamedTemporaryFile(suffix=".sql") as handle:
            req = request(dump=Path(handle.name), create_database=False)
            with (
                mock.patch.object(db_restore, "check_connection"),
                mock.patch.object(db_restore, "database_exists", return_value=False),
                mock.patch.object(db_restore, "detect_format", return_value="plain"),
            ):
                with self.assertRaises(db_restore.RestoreError):
                    db_restore.restore(req)

    def test_creates_database_and_runs(self):
        with tempfile.NamedTemporaryFile(suffix=".sql") as handle:
            req = request(dump=Path(handle.name))
            with (
                mock.patch.object(db_restore, "check_connection"),
                mock.patch.object(db_restore, "database_exists", return_value=False),
                mock.patch.object(db_restore, "create_database") as create,
                mock.patch.object(db_restore, "detect_format", return_value="plain"),
                mock.patch.object(db_restore, "_run_stream") as stream,
            ):
                db_restore.restore(req)
            create.assert_called_once_with(req)
            stream.assert_called_once()

    def test_capture_error_is_redacted(self):
        with tempfile.NamedTemporaryFile(suffix=".sql") as handle:
            req = request(dump=Path(handle.name))
            failed = mock.Mock(
                returncode=1,
                stdout="",
                stderr="password authentication failed for rahasia",
            )
            with mock.patch("db_restore.subprocess.run", return_value=failed):
                with self.assertRaises(db_restore.RestoreError) as caught:
                    db_restore.check_connection(req)
            self.assertNotIn("rahasia", str(caught.exception))
