import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from app.paths import PROJECT_DIR, DEFAULT_DB_PATH


class ConfigurationError(ValueError):
    """Problemas de configuración agrupados, sin valores sensibles."""


@dataclass(frozen=True)
class Settings:
    shop_domain: str
    client_id: str
    client_secret: str
    db_path: Path = DEFAULT_DB_PATH
    api_version: str = "2026-10"
    webhook_secret: str = ""

    @property
    def signing_secret(self):
        return self.webhook_secret or self.client_secret

    @classmethod
    def from_env(cls):
        load_dotenv(PROJECT_DIR / ".env")
        domain = os.environ.get("SHOPIFY_SHOP_DOMAIN", "").strip().lower()
        client_id = os.environ.get("SHOPIFY_CLIENT_ID", "").strip()
        client_secret = os.environ.get("SHOPIFY_CLIENT_SECRET", "").strip()
        secret = os.environ.get("SHOPIFY_WEBHOOK_SECRET", "").strip()
        version = os.environ.get("SHOPIFY_API_VERSION", "2026-10").strip()
        problems = []
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.myshopify\.com", domain):
            problems.append(
                "SHOPIFY_SHOP_DOMAIN: indica el dominio tienda.myshopify.com, sin https:// ni rutas."
            )
        for name, value in (
            ("SHOPIFY_CLIENT_ID", client_id),
            ("SHOPIFY_CLIENT_SECRET", client_secret),
        ):
            if not value or value == "REEMPLAZAR_AQUI":
                problems.append(
                    f"{name}: sustituye el marcador por tu credencial o secreto real."
                )
        if secret == "REEMPLAZAR_AQUI":
            problems.append(
                "SHOPIFY_WEBHOOK_SECRET: indica el secreto de firma o deja vacío para usar el Client Secret."
            )
        if not re.fullmatch(r"\d{4}-(01|04|07|10)", version):
            problems.append(
                "SHOPIFY_API_VERSION: usa AAAA-MM, con mes 01, 04, 07 o 10; por defecto 2026-10."
            )
        raw_path = os.environ.get("DATABASE_PATH", str(DEFAULT_DB_PATH)).strip()
        path = DEFAULT_DB_PATH
        if not raw_path:
            problems.append(
                "DATABASE_PATH: indica un archivo SQLite, por ejemplo data/shopify.db, o elimina la variable."
            )
        else:
            try:
                path = Path(raw_path).expanduser()
                if not path.is_absolute():
                    path = PROJECT_DIR / path
                path = path.resolve()
                if path.is_dir():
                    raise ValueError()
                parent = path.parent
                while not parent.exists():
                    parent = parent.parent
                if not parent.is_dir():
                    raise ValueError()
            except (OSError, ValueError, RuntimeError):
                problems.append(
                    "DATABASE_PATH: usa una ruta válida de archivo SQLite cuyo directorio padre pueda crearse."
                )
        if problems:
            raise ConfigurationError(
                "Configuración inválida:\n- "
                + "\n- ".join(problems)
                + "\nCopia .env.example a .env en la raíz del proyecto y corrige estas variables. "
                "Las variables del entorno tienen prioridad sobre .env."
            )
        return cls(domain, client_id, client_secret, path, version, secret)
