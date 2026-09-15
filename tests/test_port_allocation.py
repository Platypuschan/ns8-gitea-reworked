import contextlib
import io
import os
import runpy
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CREATE_SCRIPT = ROOT / "imageroot/actions/create-module/10configure_environment_vars"
MIGRATION_SCRIPT = ROOT / "imageroot/bin/migrate-environment"
RESTORE_SCRIPT = ROOT / "imageroot/actions/restore-module/40restore_database"
BUILD_SCRIPT = ROOT / "build-images.sh"


class FakeAgent(types.ModuleType):
    def __init__(self):
        super().__init__("agent")
        self.files = {}
        self.environment = {}
        self.allocations = []
        self.public_services = []

    @staticmethod
    def assert_exp(expression, message=""):
        if not expression:
            raise AssertionError(message)

    def read_envfile(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        return dict(self.files[path])

    def write_envfile(self, path, values):
        self.files[path] = dict(values)
        Path(path).write_text(
            "".join(f"{key}={value}\n" for key, value in values.items()),
            encoding="utf-8",
        )

    def set_env(self, name, value):
        self.environment[name] = value

    def allocate_ports(self, count, protocol, keep_existing=False):
        self.allocations.append((count, protocol, keep_existing))
        return (24001, 24001)

    def add_public_service(self, name, ports, replace_ports=False):
        self.public_services.append((name, ports, replace_ports))
        return True


@contextlib.contextmanager
def isolated_state(fake_agent, environment, stdin=""):
    previous_directory = os.getcwd()
    with tempfile.TemporaryDirectory() as directory:
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.dict(sys.modules, {"agent": fake_agent}),
            mock.patch.object(sys, "stdin", io.StringIO(stdin)),
        ):
            os.chdir(directory)
            try:
                yield Path(directory)
            finally:
                os.chdir(previous_directory)


class PortAllocationTests(unittest.TestCase):
    def test_new_instance_uses_two_distinct_allocated_ports(self):
        agent = FakeAgent()
        with isolated_state(agent, {"TCP_PORTS": "20000,20001"}, "{}") as state:
            runpy.run_path(str(CREATE_SCRIPT), run_name="__main__")

            self.assertEqual(agent.environment["SSH_TCP_PORT"], "20001")
            database = agent.files["database.env"]
            gitea_database = agent.files["gitea-db.env"]
            self.assertRegex(database["POSTGRES_PASSWORD"], r"^[0-9a-f]{64}$")
            self.assertEqual(
                gitea_database["GITEA__database__PASSWD"],
                database["POSTGRES_PASSWORD"],
            )
            self.assertEqual(
                gitea_database["GITEA__database__HOST"],
                "127.0.0.1:5432",
            )
            self.assertEqual(
                stat.S_IMODE((state / "database.env").stat().st_mode),
                0o600,
            )
            self.assertEqual(
                stat.S_IMODE((state / "gitea-db.env").stat().st_mode),
                0o600,
            )

    def test_legacy_instance_gets_one_additional_ssh_port(self):
        agent = FakeAgent()
        agent.files = {
            "gitea.env": {
                "LOCAL_ROOT_URL": "https://old.example.test",
                "GITEA__mailer__ENABLED": "true",
            },
            "gitea-db.env": {
                "GITEA__database__HOST": "postgresql-app:5432",
            },
        }
        environment = {
            "MODULE_ID": "gitea1",
            "TRAEFIK_HOST": "git.example.test",
        }

        with isolated_state(agent, environment) as state:
            (state / "gitea.env").touch()
            (state / "gitea-db.env").touch()
            runpy.run_path(str(MIGRATION_SCRIPT), run_name="__main__")

            self.assertEqual(agent.allocations, [(1, "tcp", True)])
            self.assertEqual(agent.environment["SSH_TCP_PORT"], "24001")
            self.assertEqual(
                agent.public_services,
                [("gitea1", ["24001/tcp"], True)],
            )

            gitea = agent.files["gitea.env"]
            self.assertNotIn("LOCAL_ROOT_URL", gitea)
            self.assertFalse(
                any(key.startswith("GITEA__mailer__") for key in gitea)
            )
            self.assertEqual(
                gitea["GITEA__server__ROOT_URL"],
                "https://git.example.test/",
            )
            self.assertEqual(gitea["GITEA__server__SSH_PORT"], "24001")
            self.assertEqual(
                gitea["GITEA__service__DISABLE_REGISTRATION"],
                "true",
            )
            self.assertEqual(
                gitea["GITEA__service__REQUIRE_SIGNIN_VIEW"],
                "true",
            )
            self.assertEqual(
                gitea["GITEA__repository__DEFAULT_PRIVATE"],
                "private",
            )
            self.assertEqual(
                agent.files["gitea-db.env"]["GITEA__database__HOST"],
                "127.0.0.1:5432",
            )
            self.assertEqual(
                stat.S_IMODE((state / "smarthost.env").stat().st_mode),
                0o600,
            )

    def test_existing_ssh_port_is_reused(self):
        agent = FakeAgent()
        environment = {
            "MODULE_ID": "gitea-reworked1",
            "SSH_TCP_PORT": "25000",
            "TRAEFIK_HOST": "git.example.test",
        }

        with isolated_state(agent, environment):
            runpy.run_path(str(MIGRATION_SCRIPT), run_name="__main__")

        self.assertEqual(agent.allocations, [])
        self.assertEqual(
            agent.public_services,
            [("gitea-reworked1", ["25000/tcp"], True)],
        )

    def test_node_roles_are_combined_in_one_authorization(self):
        build_script = BUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("node:fwadm,portsadm", build_script)
        self.assertNotIn("node:fwadm node:portsadm", build_script)


class BackupRestoreTests(unittest.TestCase):
    def test_postgres_user_can_read_restore_init_script(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            mock_bin = state / "bin"
            mock_bin.mkdir()
            (state / "gitea.pg_dump").write_bytes(b"valid custom archive")

            podman = mock_bin / "podman"
            podman.write_text(
                """#!/bin/bash
set -Eeuo pipefail
if [[ "$*" == *"pg_restore --list"* ]]; then
    exit 0
fi
test "$(stat -c '%a' restore)" = "755"
test "$(stat -c '%a' restore/gitea_restore.sh)" = "644"
grep -q "pg_restore" restore/gitea_restore.sh
cat >/dev/null
""",
                encoding="utf-8",
            )
            podman.chmod(0o755)

            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{mock_bin}:{environment['PATH']}",
                    "POSTGRES_IMAGE": "postgres:test",
                }
            )
            subprocess.run(
                ["bash", str(RESTORE_SCRIPT)],
                cwd=state,
                env=environment,
                check=True,
            )

            self.assertFalse((state / "gitea.pg_dump").exists())
            self.assertFalse((state / "restore").exists())


if __name__ == "__main__":
    unittest.main()
