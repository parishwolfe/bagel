"""Provision a restricted Supabase login and upload secrets via stdin, never Terraform.

Run from the repository root after applying the base GCP infrastructure. Requires
schema-owner credentials in .env and an authenticated gcloud CLI. Rotates the
worker password; deploy the returned secret versions immediately afterwards.
"""

import argparse
import secrets
import subprocess

import psycopg
from bagel_market_data.config import Settings
from dotenv import dotenv_values
from psycopg import sql
from psycopg.conninfo import make_conninfo


def provision(project: str, pooler: str, project_ref: str) -> None:
    settings = Settings()
    settings.require_credentials()
    assert settings.database_url and settings.alpaca_api_key and settings.alpaca_secret_key
    role = "bagel_gcp_worker"
    password = secrets.token_urlsafe(48)
    with psycopg.connect(settings.database_url.get_secret_value(), autocommit=True) as conn:
        verifier = conn.pgconn.encrypt_password(
            password.encode(), role.encode(), algorithm=b"scram-sha-256"
        ).decode()
        if not conn.execute("select 1 from pg_roles where rolname=%s", (role,)).fetchone():
            conn.execute(sql.SQL("create role {} login").format(sql.Identifier(role)))
        flags = conn.execute(
            "select rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls "
            "from pg_roles where rolname=%s",
            (role,),
        ).fetchone()
        if not flags or any(flags):
            raise RuntimeError("Existing worker role has elevated privileges")
        conn.execute(
            sql.SQL("alter role {} with login password {}").format(
                sql.Identifier(role), sql.Literal(verifier)
            )
        )
        conn.execute(sql.SQL("grant bagel_ingester to {}").format(sql.Identifier(role)))
    dsn = make_conninfo(
        host=pooler,
        port=5432,
        dbname="postgres",
        user=f"{role}.{project_ref}",
        password=password,
        sslmode="require",
        connect_timeout=10,
        application_name="bagel-gcp-worker",
    )
    with psycopg.connect(dsn) as conn:
        assert conn.pgconn.ssl_in_use
        assert conn.execute("select count(*) from ingestion.watchlists").fetchone()
        privilege = conn.execute(
            "select has_schema_privilege(current_user,'public','CREATE')"
        ).fetchone()
        assert privilege and not privilege[0]
    values = {
        "bagel-alpaca-api-key": settings.alpaca_api_key.get_secret_value(),
        "bagel-alpaca-secret-key": settings.alpaca_secret_key.get_secret_value(),
        "bagel-database-url": dsn,
    }
    for name, value in values.items():
        result = subprocess.run(
            [
                "gcloud",
                "secrets",
                "versions",
                "add",
                name,
                "--data-file=-",
                "--project",
                project,
                "--billing-project",
                project,
                "--format=value(name)",
                "--quiet",
            ],
            input=value,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(f"Secret upload failed for {name}; output suppressed")
        print(f"Uploaded {name}: version {result.stdout.strip().rsplit('/', 1)[-1]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=dotenv_values(".env").get("GCP_PROJECT_ID"))
    parser.add_argument("--pooler-host", required=True)
    parser.add_argument("--project-ref", required=True)
    args = parser.parse_args()
    if not args.project:
        parser.error("--project or GCP_PROJECT_ID is required")
    try:
        provision(args.project, args.pooler_host, args.project_ref)
    except Exception as exc:
        # Never emit libpq errors, local configuration values, or subprocess output.
        raise SystemExit(
            f"Provisioning failed ({type(exc).__name__}); details suppressed"
        ) from None


if __name__ == "__main__":
    main()
