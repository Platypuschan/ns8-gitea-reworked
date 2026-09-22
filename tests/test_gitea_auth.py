import importlib.util
import sys
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
            "CN=gitea-user,CN=Users,DC=ad,DC=own-hub,DC=de",
            nested=False,
            hidden_users_clause=hidden,
        )
        admin_filter = gitea_auth.build_admin_filter(
            "CN=gitea-admin,CN=Users,DC=ad,DC=own-hub,DC=de",
            nested=False,
        )

        self.assertIn("(sAMAccountName=%[1]s)", user_filter)
        self.assertIn(
            "(memberOf=CN=gitea-user,CN=Users,DC=ad,DC=own-hub,DC=de)",
            user_filter,
        )
        self.assertIn("userAccountControl:1.2.840.113556.1.4.803:=2", user_filter)
        self.assertIn(hidden, user_filter)
        self.assertEqual(
            admin_filter,
            "(memberOf=CN=gitea-admin,CN=Users,DC=ad,DC=own-hub,DC=de)",
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


if __name__ == "__main__":
    unittest.main()
