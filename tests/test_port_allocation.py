import contextlib
import io
import json
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
CONFIGURE_SCRIPT = ROOT / "imageroot/actions/configure-module/10configure_environment_vars"
MIGRATION_SCRIPT = ROOT / "imageroot/bin/migrate-environment"
RESTORE_SCRIPT = ROOT / "imageroot/actions/restore-module/40restore_database"
RESTORE_RECONCILE_SCRIPT = (
    ROOT / "imageroot/actions/restore-module/90reconcile_auth"
)
UPDATE_RECONCILE_SCRIPT = ROOT / "imageroot/update-module.d/30reconcile_auth"
BUILD_SCRIPT = ROOT / "build-images.sh"


class FakeAgent(types.ModuleType):
    def __init__(self):
        super().__init__("agent")
        self.files = {}
        self.environment = {}
        self.allocations = []
        self.public_services = []
        self.bound_domains = []

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

    def bind_user_domains(self, domains, check=False):
        self.bound_domains.append((list(domains), check))
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
            self.assertEqual(
                agent.files["gitea-setup.env"],
                {"GITEA_SETUP_MODE": "pending"},
            )
            self.assertEqual(
                stat.S_IMODE((state / "gitea-setup.env").stat().st_mode),
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
            self.assertEqual(gitea["GITEA__security__INSTALL_LOCK"], "true")
            self.assertEqual(
                agent.files["gitea-setup.env"],
                {"GITEA_SETUP_MODE": "managed"},
            )
            self.assertEqual(
                stat.S_IMODE((state / "gitea-setup.env").stat().st_mode),
                0o600,
            )
            self.assertEqual(
                agent.files["gitea-db.env"]["GITEA__database__HOST"],
                "127.0.0.1:5432",
            )
            self.assertEqual(
                agent.files["gitea-auth.env"],
                {
                    "GITEA_AUTH_ENABLED": "false",
                    "GITEA_AUTH_SOURCE_MANAGED": "false",
                    "GITEA_AUTH_DOMAIN": "",
                    "GITEA_AUTH_USER_GROUP": "gitea-user",
                    "GITEA_AUTH_ADMIN_GROUP": "gitea-admin",
                    "GITEA_AUTH_USER_SEARCH_BASE": "",
                    "GITEA_AUTH_NESTED_GROUPS": "false",
                },
            )
            self.assertEqual(agent.bound_domains, [([], False)])
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

    def test_configure_enables_managed_ad_and_locks_installer(self):
        agent = FakeAgent()
        environment = {"SSH_TCP_PORT": "25000"}
        request = {
            "host": "git.own-hub.de",
            "http2https": True,
            "lets_encrypt": False,
            "setup_mode": "managed",
            "ad_enabled": True,
            "ad_domain": "ad.own-hub.de",
            "ad_user_group": "gitea-user",
            "ad_admin_group": "gitea-admin",
            "ad_user_search_base": "CN=Users,DC=ad,DC=own-hub,DC=de",
            "ad_nested_groups": False,
        }

        with isolated_state(agent, environment, json.dumps(request)) as state:
            runpy.run_path(str(CONFIGURE_SCRIPT), run_name="__main__")

            self.assertEqual(
                agent.files["gitea.env"]["GITEA__security__INSTALL_LOCK"],
                "true",
            )
            self.assertEqual(
                agent.files["gitea-setup.env"],
                {"GITEA_SETUP_MODE": "managed"},
            )
            self.assertEqual(
                agent.files["gitea-auth.env"],
                {
                    "GITEA_AUTH_ENABLED": "true",
                    "GITEA_AUTH_SOURCE_MANAGED": "true",
                    "GITEA_AUTH_DOMAIN": "ad.own-hub.de",
                    "GITEA_AUTH_USER_GROUP": "gitea-user",
                    "GITEA_AUTH_ADMIN_GROUP": "gitea-admin",
                    "GITEA_AUTH_USER_SEARCH_BASE": (
                        "CN=Users,DC=ad,DC=own-hub,DC=de"
                    ),
                    "GITEA_AUTH_NESTED_GROUPS": "false",
                },
            )
            self.assertEqual(
                agent.bound_domains,
                [(["ad.own-hub.de"], True)],
            )
            self.assertEqual(
                stat.S_IMODE((state / "gitea-auth.env").stat().st_mode),
                0o600,
            )

    def test_legacy_configure_request_preserves_ad_settings(self):
        agent = FakeAgent()
        agent.files["gitea-auth.env"] = {
            "GITEA_AUTH_ENABLED": "true",
            "GITEA_AUTH_SOURCE_MANAGED": "true",
            "GITEA_AUTH_DOMAIN": "ad.own-hub.de",
            "GITEA_AUTH_USER_GROUP": "gitea-user",
            "GITEA_AUTH_ADMIN_GROUP": "gitea-admin",
            "GITEA_AUTH_USER_SEARCH_BASE": "",
            "GITEA_AUTH_NESTED_GROUPS": "true",
        }
        environment = {"SSH_TCP_PORT": "25000"}
        request = {
            "host": "git.own-hub.de",
            "http2https": True,
            "lets_encrypt": False,
        }

        with isolated_state(agent, environment, json.dumps(request)):
            (Path.cwd() / "gitea-auth.env").touch()
            runpy.run_path(str(CONFIGURE_SCRIPT), run_name="__main__")

        self.assertEqual(
            agent.files["gitea-auth.env"]["GITEA_AUTH_NESTED_GROUPS"],
            "true",
        )
        self.assertEqual(
            agent.files["gitea-setup.env"],
            {"GITEA_SETUP_MODE": "managed"},
        )
        self.assertEqual(agent.bound_domains, [(["ad.own-hub.de"], True)])

    def test_manual_setup_leaves_installer_and_authentication_unmanaged(self):
        agent = FakeAgent()
        agent.files = {
            "gitea-setup.env": {"GITEA_SETUP_MODE": "pending"},
            "gitea.env": {
                "GITEA__service__DISABLE_REGISTRATION": "true",
                "GITEA__service__REQUIRE_SIGNIN_VIEW": "true",
                "GITEA__repository__DEFAULT_PRIVATE": "private",
                "GITEA__security__INSTALL_LOCK": "true",
            },
        }
        request = {
            "host": "git.example.test",
            "http2https": True,
            "lets_encrypt": False,
            "setup_mode": "manual",
            "ad_enabled": False,
        }

        with isolated_state(
            agent,
            {"SSH_TCP_PORT": "25000"},
            json.dumps(request),
        ) as state:
            runpy.run_path(str(CONFIGURE_SCRIPT), run_name="__main__")

            self.assertEqual(
                agent.files["gitea-setup.env"],
                {"GITEA_SETUP_MODE": "manual"},
            )
            for key in (
                "GITEA__service__DISABLE_REGISTRATION",
                "GITEA__service__REQUIRE_SIGNIN_VIEW",
                "GITEA__repository__DEFAULT_PRIVATE",
                "GITEA__security__INSTALL_LOCK",
            ):
                self.assertNotIn(key, agent.files["gitea.env"])
            self.assertEqual(
                agent.files["gitea-auth.env"],
                {
                    "GITEA_AUTH_ENABLED": "false",
                    "GITEA_AUTH_SOURCE_MANAGED": "false",
                    "GITEA_AUTH_DOMAIN": "",
                    "GITEA_AUTH_USER_GROUP": "gitea-user",
                    "GITEA_AUTH_ADMIN_GROUP": "gitea-admin",
                    "GITEA_AUTH_USER_SEARCH_BASE": "",
                    "GITEA_AUTH_NESTED_GROUPS": "false",
                },
            )
            self.assertEqual(agent.bound_domains, [([], True)])
            self.assertEqual(
                stat.S_IMODE((state / "gitea-setup.env").stat().st_mode),
                0o600,
            )

    def test_setup_mode_cannot_change_after_first_configuration(self):
        agent = FakeAgent()
        agent.files["gitea-setup.env"] = {"GITEA_SETUP_MODE": "manual"}
        request = {
            "host": "git.example.test",
            "http2https": True,
            "lets_encrypt": False,
            "setup_mode": "managed",
        }

        with isolated_state(
            agent,
            {"SSH_TCP_PORT": "25000"},
            json.dumps(request),
        ):
            with self.assertRaisesRegex(AssertionError, "fixed"):
                runpy.run_path(str(CONFIGURE_SCRIPT), run_name="__main__")

        self.assertEqual(
            agent.files["gitea-setup.env"],
            {"GITEA_SETUP_MODE": "manual"},
        )

    def test_rejected_manual_ad_request_does_not_lock_setup_mode(self):
        agent = FakeAgent()
        agent.files["gitea-setup.env"] = {"GITEA_SETUP_MODE": "pending"}
        request = {
            "host": "git.example.test",
            "http2https": True,
            "lets_encrypt": False,
            "setup_mode": "manual",
            "ad_enabled": True,
        }

        with isolated_state(
            agent,
            {"SSH_TCP_PORT": "25000"},
            json.dumps(request),
        ):
            with self.assertRaisesRegex(AssertionError, "requires managed"):
                runpy.run_path(str(CONFIGURE_SCRIPT), run_name="__main__")

        self.assertEqual(
            agent.files["gitea-setup.env"],
            {"GITEA_SETUP_MODE": "pending"},
        )
        self.assertNotIn("gitea.env", agent.files)

    def test_manual_setup_survives_environment_migration(self):
        agent = FakeAgent()
        agent.files = {
            "gitea-setup.env": {"GITEA_SETUP_MODE": "manual"},
            "gitea.env": {
                "GITEA__service__DISABLE_REGISTRATION": "true",
                "GITEA__service__REQUIRE_SIGNIN_VIEW": "true",
                "GITEA__repository__DEFAULT_PRIVATE": "private",
                "GITEA__security__INSTALL_LOCK": "true",
            },
            "gitea-auth.env": {
                "GITEA_AUTH_ENABLED": "true",
                "GITEA_AUTH_SOURCE_MANAGED": "true",
                "GITEA_AUTH_DOMAIN": "ad.example.test",
            },
        }
        environment = {
            "MODULE_ID": "gitea-reworked1",
            "SSH_TCP_PORT": "25000",
            "TRAEFIK_HOST": "git.example.test",
        }

        with isolated_state(agent, environment):
            runpy.run_path(str(MIGRATION_SCRIPT), run_name="__main__")

        for key in (
            "GITEA__service__DISABLE_REGISTRATION",
            "GITEA__service__REQUIRE_SIGNIN_VIEW",
            "GITEA__repository__DEFAULT_PRIVATE",
            "GITEA__security__INSTALL_LOCK",
        ):
            self.assertNotIn(key, agent.files["gitea.env"])
        self.assertEqual(
            agent.files["gitea-setup.env"],
            {"GITEA_SETUP_MODE": "manual"},
        )
        self.assertEqual(
            agent.files["gitea-auth.env"]["GITEA_AUTH_ENABLED"],
            "false",
        )
        self.assertEqual(agent.bound_domains, [([], False)])

    def test_node_roles_are_combined_in_one_authorization(self):
        build_script = BUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("node:fwadm,portsadm", build_script)
        self.assertNotIn("node:fwadm node:portsadm", build_script)
        self.assertIn("cluster:accountconsumer", build_script)
        state_include = (ROOT / "imageroot/etc/state-include.conf").read_text(
            encoding="utf-8"
        )
        self.assertIn("state/gitea-setup.env", state_include)


class BackupRestoreTests(unittest.TestCase):
    def test_update_and_restore_wait_for_authentication_reconciliation(self):
        for script in (UPDATE_RECONCILE_SCRIPT, RESTORE_RECONCILE_SCRIPT):
            with self.subTest(script=script):
                source = script.read_text(encoding="utf-8")
                self.assertIn("set -Eeuo pipefail", source)
                self.assertRegex(source, r"(?m)^reconcile-gitea-auth$")

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
