"""Arranque real de Uvicorn desde una copia temporal, sin credenciales reales."""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from urllib.request import ProxyHandler, build_opener


ROOT = Path(__file__).resolve().parents[1]


class StartupTests(unittest.TestCase):
    def test_clean_copy_loads_dotenv_and_serves_health(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("config.py", "main.py", "init_db.py", "repository.py", "shopify.py",
                         "sync_service.py", "schema.sql", ".env.example", "requirements.txt"):
                shutil.copy2(ROOT / name, root / name)
            content = (root / ".env.example").read_text(encoding="utf-8")
            content = content.replace("REEMPLAZAR_AQUI", "credencial-ficticia-local")
            content = content.replace("DATABASE_PATH=data/shopify.db", "DATABASE_PATH=data/from-env.db")
            (root / ".env").write_text(content, encoding="utf-8")
            environment = os.environ.copy()
            for name in ("SHOPIFY_SHOP_DOMAIN", "SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET", "SHOPIFY_WEBHOOK_SECRET",
                         "SHOPIFY_API_VERSION", "DATABASE_PATH"):
                environment.pop(name, None)
            subprocess.run([sys.executable, "init_db.py"], cwd=root, env=environment,
                           check=True, capture_output=True)
            self.assertTrue((root / "data" / "shopify.db").is_file())
            log_path = root / "startup.log"
            with log_path.open("w", encoding="utf-8") as log:
                process = subprocess.Popen(
                    [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
                     "--port", "0", "--workers", "1"],
                    cwd=root, env=environment, stdout=log, stderr=log,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                try:
                    deadline = time.monotonic() + 15
                    address = None
                    while time.monotonic() < deadline:
                        output = log_path.read_text(encoding="utf-8")
                        match = re.search(r"http://127\.0\.0\.1:(\d+)", output)
                        if match:
                            address = f"http://127.0.0.1:{match.group(1)}/health"
                            break
                        if process.poll() is not None:
                            self.fail(f"Uvicorn no arrancó: {output}")
                        time.sleep(0.05)
                    self.assertIsNotNone(address, "Uvicorn no abrió el puerto a tiempo")
                    with build_opener(ProxyHandler({})).open(address, timeout=3) as response:
                        self.assertEqual(json.load(response), {"status": "ok"})
                    # Solo .env apunta a esta base: confirma la carga por el comando real.
                    with closing(sqlite3.connect(root / "data" / "from-env.db")) as db:
                        self.assertEqual(db.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)
                    self.assertNotIn("credencial-ficticia-local", log_path.read_text(encoding="utf-8"))
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
