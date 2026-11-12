import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import ConfigurationError, Settings


class ConfigTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        project = patch("app.config.PROJECT_DIR", self.root)
        project.start()
        self.addCleanup(project.stop)

    def write_env(self, extra=""):
        (self.root / ".env").write_text(
            "SHOPIFY_SHOP_DOMAIN=natulim-sevica.myshopify.com\n"
            "SHOPIFY_CLIENT_ID=token-ficticio\n"
            "SHOPIFY_CLIENT_SECRET=secreto-ficticio\n" + extra,
            encoding="utf-8",
        )

    def test_loads_dotenv_and_defaults(self):
        self.write_env()
        settings = Settings.from_env()
        self.assertEqual(settings.shop_domain, "natulim-sevica.myshopify.com")
        self.assertEqual(settings.client_id, "token-ficticio")
        self.assertEqual(settings.client_secret, "secreto-ficticio")
        self.assertEqual(settings.api_version, "2026-10")

    def test_environment_has_priority(self):
        self.write_env()
        os.environ["SHOPIFY_CLIENT_ID"] = "token-del-entorno"
        self.assertEqual(Settings.from_env().client_id, "token-del-entorno")

    def test_webhook_secret_defaults_to_client_secret(self):
        self.write_env()
        self.assertEqual(Settings.from_env().signing_secret, "secreto-ficticio")
        os.environ["SHOPIFY_WEBHOOK_SECRET"] = "firma-ficticia"
        self.assertEqual(Settings.from_env().signing_secret, "firma-ficticia")

    def test_all_errors_in_one_message_without_secret_values(self):
        self.write_env("SHOPIFY_API_VERSION=incorrecta\nDATABASE_PATH=\n")
        os.environ.update({
            "SHOPIFY_SHOP_DOMAIN": "https://incorrecto.example",
            "SHOPIFY_CLIENT_ID": "REEMPLAZAR_AQUI",
            "SHOPIFY_CLIENT_SECRET": "",
        })
        with self.assertRaises(ConfigurationError) as caught:
            Settings.from_env()
        message = str(caught.exception)
        for name in ("SHOPIFY_SHOP_DOMAIN", "SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET",
                     "SHOPIFY_API_VERSION", "DATABASE_PATH"):
            self.assertIn(name, message)
        for value in ("token-ficticio", "secreto-ficticio", "https://incorrecto.example", "REEMPLAZAR_AQUI"):
            self.assertNotIn(value, message)

    def test_missing_required_variables(self):
        with self.assertRaises(ConfigurationError) as caught:
            Settings.from_env()
        for name in ("SHOPIFY_SHOP_DOMAIN", "SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET"):
            self.assertIn(name, str(caught.exception))

    def test_database_path_is_project_relative(self):
        self.write_env("DATABASE_PATH=data/reviewer.db\n")
        self.assertEqual(Settings.from_env().db_path, self.root / "data" / "reviewer.db")

    def test_directory_and_invalid_parent_are_rejected(self):
        self.write_env()
        file = self.root / "not-a-directory"
        file.write_text("ficticio", encoding="utf-8")
        for path in (str(self.root), str(file / "database.db")):
            with self.subTest(path=path):
                os.environ["DATABASE_PATH"] = path
                with self.assertRaises(ConfigurationError) as caught:
                    Settings.from_env()
                self.assertIn("DATABASE_PATH", str(caught.exception))
