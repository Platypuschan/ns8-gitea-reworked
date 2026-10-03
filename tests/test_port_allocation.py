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
SMARTHOST_SCRIPT = ROOT / "imageroot/bin/discover-smarthost"
DOMAIN_EVENT_SCRIPT = ROOT / "imageroot/events/user-domain-changed/10reconcile_auth"
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
        self.bound_domains = []
        self.smtp = {"enabled": False}

    def redis_connect(self, use_replica=False):
        return None

    def get_smarthost_settings(self, rdb):
        return self.smtp

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

    def test_configure_enables_managed_ad_and_locks_installer(self):
        agent = FakeAgent()
        agent.files["gitea-setup.env"] = {"GITEA_SETUP_MODE": "pending"}
        environment = {"SSH_TCP_PORT": "25000"}
        request = {
            "host": "git.example.test",
            "http2https": True,
            "lets_encrypt": False,
            "setup_mode": "managed",
            "ad_enabled": True,
            "ad_domain": "ad.example.test",
            "ad_user_group": "gitea-user",
            "ad_admin_group": "gitea-admin",
            "ad_user_search_base": "CN=Users,DC=ad,DC=example,DC=test",
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
                {
                    "GITEA_SETUP_MODE": "managed",
                    "GITEA_MAILER_FROM": "",
                    "GITEA_MAIL_SMARTHOST": "false",
                },
            )
            self.assertEqual(
                agent.files["gitea-auth.env"],
                {
                    "GITEA_AUTH_ENABLED": "true",
                    "GITEA_AUTH_SOURCE_MANAGED": "true",
                    "GITEA_AUTH_DOMAIN": "ad.example.test",
                    "GITEA_AUTH_USER_GROUP": "gitea-user",
                    "GITEA_AUTH_ADMIN_GROUP": "gitea-admin",
                    "GITEA_AUTH_USER_SEARCH_BASE": (
                        "CN=Users,DC=ad,DC=example,DC=test"
                    ),
                    "GITEA_AUTH_NESTED_GROUPS": "false",
                },
            )
            self.assertEqual(
                agent.bound_domains,
                [(["ad.example.test"], True)],
            )
            self.assertEqual(
                stat.S_IMODE((state / "gitea-auth.env").stat().st_mode),
                0o600,
            )

    def test_request_without_ad_fields_preserves_ad_settings(self):
        agent = FakeAgent()
        agent.files["gitea-setup.env"] = {"GITEA_SETUP_MODE": "managed"}
        agent.files["gitea-auth.env"] = {
            "GITEA_AUTH_ENABLED": "true",
            "GITEA_AUTH_SOURCE_MANAGED": "true",
            "GITEA_AUTH_DOMAIN": "ad.example.test",
            "GITEA_AUTH_USER_GROUP": "gitea-user",
            "GITEA_AUTH_ADMIN_GROUP": "gitea-admin",
            "GITEA_AUTH_USER_SEARCH_BASE": "",
            "GITEA_AUTH_NESTED_GROUPS": "true",
        }
        environment = {"SSH_TCP_PORT": "25000"}
        request = {
            "host": "git.example.test",
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
            {
                "GITEA_SETUP_MODE": "managed",
                "GITEA_MAILER_FROM": "",
                "GITEA_MAIL_SMARTHOST": "false",
            },
        )
        self.assertEqual(agent.bound_domains, [(["ad.example.test"], False)])

    def test_restore_with_missing_ad_domain_keeps_configuration_retryable(self):
        class MissingDomainAgent(FakeAgent):
            def bind_user_domains(self, domains, check=False):
                super().bind_user_domains(domains, check=check)
                if domains and check:
                    raise AssertionError("domain missing from target cluster")

        agent = MissingDomainAgent()
        agent.files["gitea-setup.env"] = {
            "GITEA_SETUP_MODE": "managed",
            "GITEA_MAILER_FROM": "git@example.test",
        }
        agent.files["gitea-auth.env"] = {
            "GITEA_AUTH_ENABLED": "true",
            "GITEA_AUTH_DOMAIN": "ad.example.test",
        }
        request = {"host": "git.example.test", "http2https": True, "lets_encrypt": False}
        with isolated_state(agent, {"SSH_TCP_PORT": "25000"}, json.dumps(request)):
            runpy.run_path(str(CONFIGURE_SCRIPT), run_name="__main__")
        self.assertEqual(agent.bound_domains, [(["ad.example.test"], False)])
        self.assertEqual(agent.files["gitea-setup.env"]["GITEA_MAILER_FROM"], "git@example.test")
        self.assertEqual(agent.files["gitea-auth.env"]["GITEA_AUTH_ENABLED"], "true")

        request.update({"ad_enabled": True, "ad_domain": "ad.example.test"})
        with isolated_state(agent, {"SSH_TCP_PORT": "25000"}, json.dumps(request)):
            with self.assertRaisesRegex(AssertionError, "domain missing"):
                runpy.run_path(str(CONFIGURE_SCRIPT), run_name="__main__")
        self.assertEqual(agent.bound_domains[-1], (["ad.example.test"], True))

    def test_mail_sender_rejects_envfile_line_injection_before_writing(self):
        agent = FakeAgent()
        agent.files["gitea-setup.env"] = {"GITEA_SETUP_MODE": "pending"}
        request = {
            "host": "git.example.test",
            "setup_mode": "managed",
            "mailer_from": "git@example.test\nGITEA_SETUP_MODE=manual",
        }
        with isolated_state(agent, {"SSH_TCP_PORT": "25000"}, json.dumps(request)):
            with self.assertRaisesRegex(AssertionError, "valid email address"):
                runpy.run_path(str(CONFIGURE_SCRIPT), run_name="__main__")
        self.assertEqual(agent.files["gitea-setup.env"], {"GITEA_SETUP_MODE": "pending"})
        self.assertNotIn("gitea.env", agent.files)

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
                {
                    "GITEA_SETUP_MODE": "manual",
                    "GITEA_MAILER_FROM": "",
                    "GITEA_MAIL_SMARTHOST": "false",
                },
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

    def test_node_roles_are_combined_in_one_authorization(self):
        build_script = BUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("node:fwadm,portsadm", build_script)
        self.assertNotIn("node:fwadm node:portsadm", build_script)
        self.assertIn("cluster:accountconsumer", build_script)
        state_include = (ROOT / "imageroot/etc/state-include.conf").read_text(
            encoding="utf-8"
        )
        self.assertIn("state/gitea-setup.env", state_include)
        self.assertIn("state/gitea-recovery.env", state_include)


MAILER_DISABLED = {
    "GITEA__mailer__ENABLED": "false",
    "GITEA__mailer__FROM": "",
    "GITEA__mailer__PROTOCOL": "",
    "GITEA__mailer__SMTP_ADDR": "",
    "GITEA__mailer__SMTP_PORT": "",
    "GITEA__mailer__USER": "",
    "GITEA__mailer__PASSWD": "",
    "GITEA__mailer__FORCE_TRUST_SERVER_CERT": "false",
}
RELAY = {
    "enabled": True,
    "host": "relay.example.test",
    "port": 587,
    "username": "relayuser",
    "password": "secret",
    "encrypt_smtp": "starttls",
    "tls_verify": True,
}


class SmarthostTests(unittest.TestCase):
    def discover(self, agent):
        with isolated_state(agent, {"TRAEFIK_HOST": "git.example.test"}) as state:
            runpy.run_path(str(SMARTHOST_SCRIPT), run_name="__main__")
            mode = stat.S_IMODE((state / "smarthost.env").stat().st_mode)
        self.assertEqual(mode, 0o600)
        return agent.files["smarthost.env.tmp"]

    def test_opted_in_managed_instance_uses_smarthost_with_valid_sender(self):
        agent = FakeAgent()
        agent.smtp = dict(RELAY)
        agent.files["gitea-setup.env"] = {
            "GITEA_SETUP_MODE": "managed",
            "GITEA_MAIL_SMARTHOST": "true",
        }
        config = self.discover(agent)
        self.assertEqual(config["GITEA__mailer__ENABLED"], "true")
        self.assertEqual(config["GITEA__mailer__SMTP_ADDR"], "relay.example.test")
        self.assertEqual(config["GITEA__mailer__PASSWD"], "secret")
        self.assertEqual(config["GITEA__mailer__FROM"], "no-reply@git.example.test")

    def test_custom_sender_survives_mailer_discovery(self):
        agent = FakeAgent()
        agent.smtp = dict(RELAY, username="", password="")
        agent.files["gitea-setup.env"] = {
            "GITEA_SETUP_MODE": "managed",
            "GITEA_MAIL_SMARTHOST": "true",
            "GITEA_MAILER_FROM": "mail@example.test",
        }
        self.assertEqual(self.discover(agent)["GITEA__mailer__FROM"], "mail@example.test")

    def test_managed_mail_is_disabled_and_cleared_unless_opted_in(self):
        for setup in (
            {"GITEA_SETUP_MODE": "managed"},
            {"GITEA_SETUP_MODE": "managed", "GITEA_MAIL_SMARTHOST": "false"},
        ):
            with self.subTest(setup=setup):
                agent = FakeAgent()
                agent.smtp = dict(RELAY)
                agent.files["gitea-setup.env"] = setup
                self.assertEqual(self.discover(agent), MAILER_DISABLED)

    def test_missing_smarthost_disables_mail_after_restore(self):
        for smtp in ({"enabled": False}, dict(RELAY, host="")):
            with self.subTest(smtp=smtp):
                agent = FakeAgent()
                agent.smtp = smtp
                agent.files["gitea-setup.env"] = {
                    "GITEA_SETUP_MODE": "managed",
                    "GITEA_MAIL_SMARTHOST": "true",
                }
                self.assertEqual(self.discover(agent), MAILER_DISABLED)

    def test_unreadable_smarthost_settings_disable_mail(self):
        agent = FakeAgent()
        agent.files["gitea-setup.env"] = {
            "GITEA_SETUP_MODE": "managed",
            "GITEA_MAIL_SMARTHOST": "true",
        }
        with mock.patch.object(agent, "redis_connect", side_effect=OSError("down")):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(self.discover(agent), MAILER_DISABLED)

    def test_manual_and_pending_modes_leave_the_mailer_to_gitea(self):
        for mode in ("manual", "pending"):
            with self.subTest(mode=mode):
                agent = FakeAgent()
                agent.smtp = dict(RELAY)
                agent.files["gitea-setup.env"] = {
                    "GITEA_SETUP_MODE": mode,
                    "GITEA_MAIL_SMARTHOST": "true",
                }
                self.assertEqual(self.discover(agent), {})

    def configure(self, agent, request):
        request = {"host": "git.example.test", "http2https": True, "lets_encrypt": False} | request
        with isolated_state(agent, {"SSH_TCP_PORT": "25000"}, json.dumps(request)):
            runpy.run_path(str(CONFIGURE_SCRIPT), run_name="__main__")
        return agent.files["gitea-setup.env"]

    def test_configure_stores_opt_in_and_keeps_it_for_restore_requests(self):
        agent = FakeAgent()
        agent.files["gitea-setup.env"] = {"GITEA_SETUP_MODE": "pending"}
        setup = self.configure(agent, {"setup_mode": "managed"})
        self.assertEqual(setup["GITEA_MAIL_SMARTHOST"], "false")
        setup = self.configure(agent, {"setup_mode": "managed", "smarthost_mail": True})
        self.assertEqual(setup["GITEA_MAIL_SMARTHOST"], "true")
        # Restore and clone call configure-module without the field.
        self.assertEqual(self.configure(agent, {})["GITEA_MAIL_SMARTHOST"], "true")
        setup = self.configure(agent, {"smarthost_mail": False})
        self.assertEqual(setup["GITEA_MAIL_SMARTHOST"], "false")

    def test_manual_mode_rejects_smarthost_mail(self):
        agent = FakeAgent()
        agent.files["gitea-setup.env"] = {"GITEA_SETUP_MODE": "pending"}
        with self.assertRaisesRegex(AssertionError, "requires managed setup mode"):
            self.configure(agent, {"setup_mode": "manual", "smarthost_mail": True})
        self.assertEqual(agent.files["gitea-setup.env"], {"GITEA_SETUP_MODE": "pending"})
        setup = self.configure(agent, {"setup_mode": "manual", "smarthost_mail": False})
        self.assertEqual(setup["GITEA_MAIL_SMARTHOST"], "false")

    def test_smarthost_event_restarts_only_opted_in_instances(self):
        source = (ROOT / "imageroot/events/smarthost-changed/10reload_services").read_text(
            encoding="utf-8"
        )
        self.assertIn("GITEA_MAIL_SMARTHOST=true", source)
        self.assertIn("GITEA_SETUP_MODE=managed", source)


class BackupRestoreTests(unittest.TestCase):
    def test_added_domain_retries_binding_after_restore(self):
        agent = FakeAgent()
        agent.files["gitea-auth.env"] = {
            "GITEA_AUTH_ENABLED": "true",
            "GITEA_AUTH_DOMAIN": "ad.example.test",
        }
        with isolated_state(agent, {}, json.dumps({"domains": ["ad.example.test"]})):
            with mock.patch("subprocess.run", return_value=mock.Mock(returncode=1)):
                runpy.run_path(str(DOMAIN_EVENT_SCRIPT), run_name="__main__")
        self.assertEqual(agent.bound_domains, [(["ad.example.test"], False)])

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
