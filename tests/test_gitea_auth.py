import importlib.util
import os
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "imageroot/bin/gitea_auth.py"
SPEC = importlib.util.spec_from_file_location("gitea_auth_under_test", MODULE_PATH)
gitea_auth = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gitea_auth
SPEC.loader.exec_module(gitea_auth)


class FilterTests(unittest.TestCase):
    def test_direct_group_filters_enforce_access_and_admin_groups(self):
        hidden = "(!(|(sAMAccountName=Guest)(sAMAccountName=krbtgt)))"
        user_filter = gitea_auth.build_user_filter(
            "CN=gitea-user,CN=Users,DC=ad,DC=example,DC=test",
            nested=False,
            hidden_users_clause=hidden,
        )
        admin_filter = gitea_auth.build_admin_filter(
            "CN=gitea-admin,CN=Users,DC=ad,DC=example,DC=test",
            nested=False,
        )

        self.assertIn("(sAMAccountName=%[1]s)", user_filter)
        self.assertIn(
            "(memberOf=CN=gitea-user,CN=Users,DC=ad,DC=example,DC=test)",
            user_filter,
        )
        self.assertIn("userAccountControl:1.2.840.113556.1.4.803:=2", user_filter)
        self.assertIn(hidden, user_filter)
        self.assertEqual(
            admin_filter,
            "(memberOf=CN=gitea-admin,CN=Users,DC=ad,DC=example,DC=test)",
        )

    def test_nested_filter_uses_ad_matching_rule_and_escapes_literals(self):
        result = gitea_auth.membership_filter(
            "CN=100% (Gitea)*,DC=example,DC=test", nested=True
        )
        self.assertEqual(
            result,
            "(memberOf:1.2.840.113556.1.4.1941:="
            r"CN=100%% \28Gitea\29\2a,DC=example,DC=test)",
        )

    def test_managed_source_uses_local_proxy_and_safe_sync_policy(self):
        arguments = gitea_auth.ldap_source_arguments(
            {
                "port": 20001,
                "user_base": "CN=Users,DC=example,DC=test",
                "user_filter": "(sAMAccountName=%[1]s)",
                "admin_filter": "(memberOf=CN=admins,DC=example,DC=test)",
                "bind_dn": "CN=service,DC=example,DC=test",
                "bind_password": "secret",
            }
        )
        self.assertEqual(arguments[arguments.index("--host") + 1], "10.0.2.2")
        self.assertIn("--synchronize-users", arguments)
        self.assertIn("--allow-deactivate-all=false", arguments)
        self.assertNotIn("--allow-deactivate-all", arguments)


class ParsingTests(unittest.TestCase):
    def test_legacy_configuration_does_not_claim_a_source(self):
        config = gitea_auth.config_from_environment({})
        self.assertFalse(config.enabled)
        self.assertFalse(config.managed)

    def test_auth_source_table(self):
        output = (
            "ID | Name                 | Type               | Enabled\n"
            "1  | NS8 Active Directory | LDAP (via BindDN) | true\n"
            "2  | OAuth | Internal      | OAuth2             | true\n"
        )
        sources = gitea_auth.parse_auth_sources(output)
        self.assertEqual(sources[0]["id"], "1")
        self.assertEqual(sources[0]["name"], "NS8 Active Directory")
        self.assertEqual(sources[0]["type"], "LDAP (via BindDN)")
        self.assertEqual(sources[0]["enabled"], "true")
        self.assertEqual(sources[1]["name"], "OAuth|Internal")

    def test_user_table(self):
        output = (
            "ID Username Email IsActive IsAdmin 2FA\n"
            "1 ns8-recovery-admin ns8@localhost.invalid true true false\n"
        )
        users = gitea_auth.parse_users(output)
        self.assertEqual(users[0]["username"], "ns8-recovery-admin")
        self.assertEqual(users[0]["admin"], "true")

    def test_unmanaged_ldap_source_is_not_silently_overwritten(self):
        sources = [
            {
                "id": "4",
                "name": "Existing LDAP",
                "type": "LDAP (via BindDN)",
                "enabled": "true",
            }
        ]
        with self.assertRaisesRegex(
            gitea_auth.ReconcileError, "unmanaged LDAP authentication source"
        ):
            gitea_auth.managed_source(sources, allow_other_ldap=False)

    def test_disable_does_not_touch_incompatible_name_collision(self):
        sources = [
            {
                "id": "5",
                "name": "NS8 Active Directory",
                "type": "OAuth2",
                "enabled": "true",
            }
        ]
        with mock.patch.object(gitea_auth, "run_gitea") as run:
            gitea_auth.disable_managed_source(sources)
        run.assert_not_called()


class RecoveryAccountTests(unittest.TestCase):
    def test_existing_active_admin_is_preserved(self):
        output = (
            "ID Username Email IsActive IsAdmin 2FA\n"
            "1 ns8-recovery-admin ns8@localhost.invalid true true false\n"
        )
        with mock.patch.object(gitea_auth, "run_gitea", return_value=output) as run:
            gitea_auth.ensure_recovery_admin()
        run.assert_called_once_with(["admin", "user", "list"])

    def test_existing_non_admin_is_not_taken_over(self):
        output = (
            "ID Username Email IsActive IsAdmin 2FA\n"
            "1 ns8-recovery-admin ns8@localhost.invalid true false false\n"
        )
        with mock.patch.object(gitea_auth, "run_gitea", return_value=output):
            with self.assertRaisesRegex(gitea_auth.ReconcileError, "not an active"):
                gitea_auth.ensure_recovery_admin()

    def test_new_account_uses_generated_password_and_commits_credentials(self):
        staged = mock.Mock()
        password = "generated-recovery-password-with-enough-entropy"
        with (
            mock.patch.object(gitea_auth, "recovery_admin_user", return_value=None),
            mock.patch.object(gitea_auth, "read_recovery_credentials", return_value=None),
            mock.patch.object(
                gitea_auth, "generate_recovery_password", return_value=password
            ),
            mock.patch.object(
                gitea_auth, "stage_recovery_credentials", return_value=staged
            ) as stage,
            mock.patch.object(gitea_auth, "commit_recovery_credentials") as commit,
            mock.patch.object(gitea_auth, "run_gitea") as run,
        ):
            gitea_auth.ensure_recovery_admin()

        stage.assert_called_once_with(password)
        commit.assert_called_once_with(staged)
        staged.unlink.assert_called_once_with(missing_ok=True)
        run.assert_called_once_with(
            [
                "admin",
                "user",
                "create",
                "--username",
                "ns8-recovery-admin",
                "--email",
                "ns8-recovery-admin@localhost.invalid",
                "--password",
                password,
                "--admin",
                "--must-change-password=false",
            ],
            secrets=(password,),
            redact_stdout=True,
        )

    def test_existing_stored_password_is_used_when_account_must_be_recreated(self):
        password = "stored-recovery-password-with-enough-entropy"
        with (
            mock.patch.object(gitea_auth, "recovery_admin_user", return_value=None),
            mock.patch.object(
                gitea_auth,
                "read_recovery_credentials",
                return_value={
                    "username": gitea_auth.RECOVERY_USERNAME,
                    "password": password,
                },
            ),
            mock.patch.object(gitea_auth, "stage_recovery_credentials") as stage,
            mock.patch.object(gitea_auth, "run_gitea") as run,
        ):
            gitea_auth.ensure_recovery_admin()

        stage.assert_not_called()
        self.assertEqual(run.call_args.kwargs["secrets"], (password,))
        self.assertIn(password, run.call_args.args[0])

    def test_reset_rotates_password_and_commits_only_after_gitea_accepts_it(self):
        staged = mock.Mock()
        password = "new-recovery-password-with-enough-entropy"
        with (
            mock.patch.object(gitea_auth, "read_setup_mode", return_value="managed"),
            mock.patch.object(gitea_auth, "wait_for_gitea") as wait,
            mock.patch.object(gitea_auth, "ensure_recovery_admin") as ensure,
            mock.patch.object(
                gitea_auth, "generate_recovery_password", return_value=password
            ),
            mock.patch.object(
                gitea_auth, "stage_recovery_credentials", return_value=staged
            ),
            mock.patch.object(gitea_auth, "commit_recovery_credentials") as commit,
            mock.patch.object(gitea_auth, "run_gitea") as run,
        ):
            gitea_auth.reset_recovery_password()

        wait.assert_called_once_with()
        ensure.assert_called_once_with()
        run.assert_called_once_with(
            [
                "admin",
                "user",
                "change-password",
                "--username",
                "ns8-recovery-admin",
                "--password",
                password,
                "--must-change-password=false",
            ],
            secrets=(password,),
            redact_stdout=True,
        )
        commit.assert_called_once_with(staged)
        staged.unlink.assert_called_once_with(missing_ok=True)

    def test_reset_is_rejected_outside_managed_mode(self):
        with (
            mock.patch.object(gitea_auth, "read_setup_mode", return_value="manual"),
            mock.patch.object(gitea_auth, "wait_for_gitea") as wait,
            self.assertRaisesRegex(gitea_auth.ReconcileError, "managed setup mode"),
        ):
            gitea_auth.reset_recovery_password()
        wait.assert_not_called()

    def test_failed_reset_does_not_publish_the_new_password(self):
        staged = mock.Mock()
        with (
            mock.patch.object(gitea_auth, "read_setup_mode", return_value="managed"),
            mock.patch.object(gitea_auth, "wait_for_gitea"),
            mock.patch.object(gitea_auth, "ensure_recovery_admin"),
            mock.patch.object(
                gitea_auth,
                "generate_recovery_password",
                return_value="new-recovery-password-with-enough-entropy",
            ),
            mock.patch.object(
                gitea_auth, "stage_recovery_credentials", return_value=staged
            ),
            mock.patch.object(
                gitea_auth,
                "run_gitea",
                side_effect=gitea_auth.ReconcileError("command failed"),
            ),
            mock.patch.object(gitea_auth, "commit_recovery_credentials") as commit,
            self.assertRaisesRegex(gitea_auth.ReconcileError, "command failed"),
        ):
            gitea_auth.reset_recovery_password()

        commit.assert_not_called()
        staged.unlink.assert_called_once_with(missing_ok=True)

    def test_credentials_file_is_atomic_and_owner_only(self):
        class FakeAgent(types.ModuleType):
            @staticmethod
            def write_envfile(path, values):
                Path(path).write_text(
                    "".join(f"{key}={value}\n" for key, value in values.items()),
                    encoding="utf-8",
                )

            @staticmethod
            def read_envfile(path):
                values = {}
                for line in Path(path).read_text(encoding="utf-8").splitlines():
                    key, value = line.split("=", 1)
                    values[key] = value
                return values

        previous_directory = os.getcwd()
        with tempfile.TemporaryDirectory() as directory:
            os.chdir(directory)
            try:
                with mock.patch.dict(sys.modules, {"agent": FakeAgent("agent")}):
                    password = "secure-recovery-password-with-enough-entropy"
                    staged = gitea_auth.stage_recovery_credentials(password)
                    self.assertEqual(stat.S_IMODE(staged.stat().st_mode), 0o600)
                    gitea_auth.commit_recovery_credentials(staged)
                    self.assertEqual(
                        stat.S_IMODE(Path("gitea-recovery.env").stat().st_mode),
                        0o600,
                    )
                    self.assertEqual(
                        gitea_auth.read_recovery_credentials(),
                        {
                            "username": "ns8-recovery-admin",
                            "password": password,
                        },
                    )
            finally:
                os.chdir(previous_directory)

    def test_generated_password_is_high_entropy_and_url_safe(self):
        first = gitea_auth.generate_recovery_password()
        second = gitea_auth.generate_recovery_password()
        self.assertRegex(first, r"^[A-Za-z0-9_-]{48}$")
        self.assertTrue(any(character.islower() for character in first))
        self.assertTrue(any(character.isupper() for character in first))
        self.assertTrue(any(character.isdigit() for character in first))
        self.assertTrue(any(character in "-_" for character in first))
        self.assertNotEqual(first, second)


class SetupModeTests(unittest.TestCase):
    def test_manual_and_pending_modes_skip_all_managed_reconciliation(self):
        for mode in ("manual", "pending"):
            with (
                self.subTest(mode=mode),
                mock.patch.object(
                    gitea_auth, "read_setup_mode", return_value=mode
                ),
                mock.patch.object(gitea_auth, "read_auth_config") as read_auth,
                mock.patch.object(gitea_auth, "wait_for_gitea") as wait,
                mock.patch.object(gitea_auth, "ensure_recovery_admin") as recovery,
            ):
                gitea_auth.reconcile()
                read_auth.assert_not_called()
                wait.assert_not_called()
                recovery.assert_not_called()


if __name__ == "__main__":
    unittest.main()
