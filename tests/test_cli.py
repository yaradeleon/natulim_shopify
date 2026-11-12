"""Comandos públicos desde una copia limpia y un directorio de trabajo distinto."""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app.storage.database import init_db
from app.storage.repository import save_order
from test_sync import example


ROOT = Path(__file__).resolve().parents[1]
COMMANDS = ("init_db.py", "sync_orders.py", "change_order.py", "batch_orders.py", "check_order.py")


class CliTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "project"
        self.cwd = Path(directory.name) / "elsewhere"
        self.root.mkdir()
        self.cwd.mkdir()
        shutil.copytree(ROOT / "app", self.root / "app",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for name in (*COMMANDS, "main.py", "schema.sql"):
            shutil.copy2(ROOT / name, self.root / name)
        (self.root / ".env").write_text(
            "SHOPIFY_SHOP_DOMAIN=prueba.myshopify.com\n"
            "SHOPIFY_CLIENT_ID=fake-id\nSHOPIFY_CLIENT_SECRET=fake-secret\n"
            "DATABASE_PATH=data/reviewer.sqlite\n", encoding="utf-8",
        )
        self.environment = os.environ.copy()
        for name in list(self.environment):
            if name.startswith("SHOPIFY_") or name == "DATABASE_PATH":
                self.environment.pop(name)
        self.environment["PYTHONIOENCODING"] = "utf-8"
        # Los comandos de estas pruebas no deben llamar a Shopify. Si una
        # regresión lo intenta, un proxy local inexistente evita salir a la red.
        self.environment["HTTP_PROXY"] = "http://127.0.0.1:1"
        self.environment["HTTPS_PROXY"] = "http://127.0.0.1:1"
        self.environment["NO_PROXY"] = ""

    def command(self, name, *arguments, stdlib_only=False):
        return subprocess.run(
            [sys.executable, *(["-S"] if stdlib_only else []), str(self.root / name), *arguments],
            cwd=self.cwd, env=self.environment, capture_output=True, text=True,
            encoding="utf-8", timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def test_all_public_commands_load_help_from_clean_copy(self):
        for name in COMMANDS:
            with self.subTest(command=name):
                result = self.command(name, "--help")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage:", result.stdout)

    def test_initialization_uses_project_path_without_third_party_dependencies(self):
        first = self.command("init_db.py", stdlib_only=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        path = self.root / "data" / "shopify.db"
        self.assertTrue(path.exists())
        with closing(sqlite3.connect(path)) as db:
            db.execute("INSERT INTO sync_state VALUES (?, ?)", ("prueba.myshopify.com", "2026-11-12T10:00:00Z"))
            db.commit()
        self.assertEqual(self.command("init_db.py", stdlib_only=True).returncode, 0)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sync_state").fetchone()[0], 1)
        self.assertFalse((self.cwd / "data").exists())

    def test_local_inspection_loads_project_dotenv_from_another_directory(self):
        path = self.root / "data" / "reviewer.sqlite"
        init_db(path)
        order = example()
        order.update(name="#6501", note="Reviewer test")
        save_order(path, None, order)
        result = self.command("check_order.py", "6501")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("#6501", result.stdout)
        self.assertIn("Reviewer test", result.stdout)
        self.assertFalse((self.cwd / "data").exists())

    def test_incremental_command_initializes_configured_path_without_network(self):
        result = self.command("sync_orders.py", "--incremental")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("No hay fecha de control", result.stdout)
        self.assertTrue((self.root / "data" / "reviewer.sqlite").exists())
        self.assertFalse((self.cwd / "data").exists())

    def test_invalid_individual_id_returns_error_without_creating_database(self):
        result = self.command("change_order.py", "#6501", "PAGADO")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("GID completo", result.stdout)
        self.assertFalse((self.root / "data").exists())

    def test_invalid_batch_manifest_fails_before_database_or_mutation(self):
        source = self.root / "invalid.json"
        source.write_text(json.dumps({"version": 99}), encoding="utf-8")
        output = self.root / "results.json"
        result = self.command("batch_orders.py", "change", "PAGADO", "--input", str(source), "--output", str(output))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("Archivo de lote inválido", result.stdout)
        self.assertFalse(output.exists())
        self.assertFalse((self.root / "data").exists())
