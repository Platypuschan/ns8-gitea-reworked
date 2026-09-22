#!/usr/bin/env python3

#
# Copyright (C) 2026
# SPDX-License-Identifier: GPL-3.0-or-later
#

"""Reconcile Gitea's local recovery account and managed AD login source."""

from __future__ import annotations

import fcntl
import ipaddress
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


AUTH_ENV_FILE = "gitea-auth.env"
LOCK_FILE = ".gitea-auth.lock"
MANAGED_SOURCE_NAME = "NS8 Active Directory"
RECOVERY_USERNAME = "ns8-recovery-admin"
RECOVERY_EMAIL = "ns8-recovery-admin@localhost.invalid"
CONTAINER_LDAP_HOST = "10.0.2.2"
LDAP_MATCHING_RULE_IN_CHAIN = "1.2.840.113556.1.4.1941"
LDAP_BITWISE_AND = "1.2.840.113556.1.4.803"


class ReconcileError(RuntimeError):
    """A safe-to-log reconciliation error."""


class DomainUnavailableError(ReconcileError):
    """The configured NS8 account domain no longer exists."""


@dataclass(frozen=True)
class AuthConfig:
    enabled: bool
    managed: bool
    domain: str
    user_group: str
    admin_group: str
    user_search_base: str
    nested_groups: bool


def parse_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def default_auth_environment() -> dict[str, str]:
    return {
        "GITEA_AUTH_ENABLED": "false",
        "GITEA_AUTH_SOURCE_MANAGED": "false",
        "GITEA_AUTH_DOMAIN": "",
        "GITEA_AUTH_USER_GROUP": "gitea-user",
        "GITEA_AUTH_ADMIN_GROUP": "gitea-admin",
        "GITEA_AUTH_USER_SEARCH_BASE": "",
        "GITEA_AUTH_NESTED_GROUPS": "false",
    }


def config_from_environment(values: dict[str, str]) -> AuthConfig:
    defaults = default_auth_environment()
    merged = defaults | values
    return AuthConfig(
        enabled=parse_bool(merged["GITEA_AUTH_ENABLED"]),
        managed=parse_bool(merged["GITEA_AUTH_SOURCE_MANAGED"]),
        domain=merged["GITEA_AUTH_DOMAIN"].strip(),
        user_group=merged["GITEA_AUTH_USER_GROUP"].strip(),
        admin_group=merged["GITEA_AUTH_ADMIN_GROUP"].strip(),
        user_search_base=merged["GITEA_AUTH_USER_SEARCH_BASE"].strip(),
        nested_groups=parse_bool(merged["GITEA_AUTH_NESTED_GROUPS"]),
    )


def read_auth_config() -> AuthConfig:
    import agent

    try:
        values = agent.read_envfile(AUTH_ENV_FILE)
    except FileNotFoundError:
        values = {}
    return config_from_environment(values)


def sanitize(message: object, secrets: tuple[str, ...] = ()) -> str:
    sanitized = str(message)
    for secret in secrets:
        if secret:
            sanitized = sanitized.replace(secret, "[redacted]")
    return sanitized


def run_gitea(
    arguments: list[str],
    *,
    secrets: tuple[str, ...] = (),
    redact_stdout: bool = False,
) -> str:
    command = [
        "podman",
        "exec",
        "--user",
        "git",
        "gitea-app",
        "gitea",
        "--config",
        "/data/gitea/conf/app.ini",
        *arguments,
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReconcileError(
            f"Could not execute the Gitea administration command: "
            f"{sanitize(exc, secrets)}"
        ) from None

    if result.returncode != 0:
        detail = result.stderr.strip()
        if not redact_stdout and result.stdout.strip():
            detail = f"{detail}\n{result.stdout.strip()}".strip()
        detail = sanitize(detail, secrets) or f"exit status {result.returncode}"
        raise ReconcileError(f"Gitea administration command failed: {detail}")

    if redact_stdout:
        return ""
    return result.stdout


def wait_for_gitea(timeout: int = 120) -> None:
    port = os.environ.get("TCP_PORT", "").strip()
    if not port.isdigit():
        raise ReconcileError("TCP_PORT is missing or invalid.")

    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/api/healthz"
    last_error = "service is not ready"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if 200 <= response.status < 300:
                    return
                last_error = f"HTTP status {response.status}"
        except (OSError, urllib.error.URLError) as exc:
            last_error = str(exc)
        time.sleep(2)
    raise ReconcileError(
        f"Gitea did not become healthy within {timeout} seconds: {last_error}"
    )


def parse_auth_sources(output: str) -> list[dict[str, str]]:
    rows = []
    for line in output.splitlines():
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) < 4 or not cells[0].isdigit():
            continue
        rows.append(
            {
                "id": cells[0],
                # Source names can contain a vertical bar. The type and
                # enabled columns are fixed and are therefore consumed from
                # the right-hand side of the table.
                "name": "|".join(cells[1:-2]).strip(),
                "type": cells[-2],
                "enabled": cells[-1].lower(),
            }
        )
    return rows


def parse_users(output: str) -> list[dict[str, str]]:
    rows = []
    for line in output.splitlines():
        cells = line.split()
        if len(cells) < 6 or not cells[0].isdigit():
            continue
        rows.append(
            {
                "id": cells[0],
                "username": cells[1],
                "email": cells[2],
                "active": cells[3].lower(),
                "admin": cells[4].lower(),
                "two_factor": cells[5].lower(),
            }
        )
    return rows


def list_auth_sources() -> list[dict[str, str]]:
    output = run_gitea(
        [
            "admin",
            "auth",
            "list",
            "--vertical-bars",
            "--min-width",
            "1",
            "--padding",
            "1",
        ]
    )
    return parse_auth_sources(output)


def managed_source(
    sources: list[dict[str, str]], *, allow_other_ldap: bool
) -> dict[str, str] | None:
    matches = [source for source in sources if source["name"] == MANAGED_SOURCE_NAME]
    if len(matches) > 1:
        raise ReconcileError(
            f"More than one authentication source is named {MANAGED_SOURCE_NAME!r}."
        )
    if matches:
        source = matches[0]
        if not source["type"].startswith("LDAP") or "BindDN" not in source["type"]:
            raise ReconcileError(
                f"Authentication source {MANAGED_SOURCE_NAME!r} exists with an "
                "incompatible type. It was not modified."
            )
        return source

    if not allow_other_ldap:
        unmanaged = [source for source in sources if source["type"].startswith("LDAP")]
        if unmanaged:
            names = ", ".join(repr(source["name"]) for source in unmanaged)
            raise ReconcileError(
                "An unmanaged LDAP authentication source already exists "
                f"({names}). Rename the intended source to "
                f"{MANAGED_SOURCE_NAME!r} or remove it before enabling managed AD."
            )
    return None


def ensure_recovery_admin() -> None:
    users = parse_users(run_gitea(["admin", "user", "list"]))
    matches = [
        user
        for user in users
        if user["username"].casefold() == RECOVERY_USERNAME.casefold()
    ]
    if len(matches) > 1:
        raise ReconcileError(
            f"More than one user matches the reserved name {RECOVERY_USERNAME!r}."
        )
    if matches:
        user = matches[0]
        if user["active"] != "true" or user["admin"] != "true":
            raise ReconcileError(
                f"The reserved recovery user {RECOVERY_USERNAME!r} already exists "
                "but is not an active administrator; it was not modified."
            )
        return

    # Gitea prints the generated password. Capture and deliberately discard it:
    # access is enabled only after an operator performs the documented reset.
    run_gitea(
        [
            "admin",
            "user",
            "create",
            "--username",
            RECOVERY_USERNAME,
            "--email",
            RECOVERY_EMAIL,
            "--random-password",
            "--random-password-length",
            "32",
            "--admin",
            "--must-change-password=false",
        ],
        redact_stdout=True,
    )


def _escape_filter_literal(value: str) -> str:
    # Gitea applies Go's fmt.Sprintf to the complete filter. Literal percent
    # signs therefore need doubling in addition to RFC 4515 escaping.
    replacements = {
        "\\": r"\5c",
        "*": r"\2a",
        "(": r"\28",
        ")": r"\29",
        "\x00": r"\00",
        "%": "%%",
    }
    return "".join(replacements.get(character, character) for character in value)


def membership_filter(group_dn: str, nested: bool) -> str:
    escaped_dn = _escape_filter_literal(group_dn)
    if nested:
        return f"(memberOf:{LDAP_MATCHING_RULE_IN_CHAIN}:={escaped_dn})"
    return f"(memberOf={escaped_dn})"


def build_user_filter(
    user_group_dn: str, *, nested: bool, hidden_users_clause: str = ""
) -> str:
    hidden = hidden_users_clause.replace("%", "%%")
    return "".join(
        [
            "(&",
            "(objectCategory=person)",
            "(objectClass=user)",
            "(sAMAccountName=%[1]s)",
            membership_filter(user_group_dn, nested),
            f"(!(userAccountControl:{LDAP_BITWISE_AND}:=2))",
            hidden,
            ")",
        ]
    )


def build_admin_filter(admin_group_dn: str, *, nested: bool) -> str:
    return membership_filter(admin_group_dn, nested)


def _validate_loopback_host(host: str) -> None:
    if host == "localhost":
        return
    try:
        is_loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback:
        raise ReconcileError(
            "NS8 returned a non-loopback LDAP proxy endpoint; refusing to expose "
            "the generated bind credentials to it."
        )


def resolve_ad_settings(config: AuthConfig) -> dict[str, object]:
    from agent.ldapproxy import Ldapproxy

    if not config.domain:
        raise ReconcileError("An NS8 account domain must be selected for AD login.")
    if not config.user_group or not config.admin_group:
        raise ReconcileError("Both AD group names are required.")
    if config.user_group.casefold() == config.admin_group.casefold():
        raise ReconcileError("The AD user and administrator groups must be different.")

    proxy = Ldapproxy()
    domain = proxy.get_domain(config.domain)
    if domain is None:
        raise DomainUnavailableError(
            f"The selected NS8 account domain {config.domain!r} is not available."
        )
    if domain.get("schema") != "ad":
        raise ReconcileError(
            f"The selected account domain {config.domain!r} is not Active Directory."
        )

    host = str(domain.get("host", ""))
    _validate_loopback_host(host)
    try:
        port = int(domain["port"])
    except (KeyError, TypeError, ValueError):
        raise ReconcileError("NS8 returned an invalid LDAP proxy port.") from None
    if not 1 <= port <= 65535:
        raise ReconcileError("NS8 returned an invalid LDAP proxy port.")

    base_dn = str(domain.get("base_dn", "")).strip()
    bind_dn = str(domain.get("bind_dn", "")).strip()
    bind_password = str(domain.get("bind_password", ""))
    if not base_dn or not bind_dn or not bind_password:
        raise ReconcileError("The selected NS8 AD domain is missing bind parameters.")
    user_base = config.user_search_base or f"CN=Users,{base_dn}"

    try:
        from ldap3 import BASE, NONE, SUBTREE, Connection, Server
        from ldap3.utils.conv import escape_filter_chars

        server = Server(host, port=port, connect_timeout=10, get_info=NONE)
        connection = Connection(
            server,
            user=bind_dn,
            password=bind_password,
            auto_bind=True,
            receive_timeout=15,
            raise_exceptions=True,
        )
        try:
            connection.search(
                search_base=user_base,
                search_filter="(objectClass=*)",
                search_scope=BASE,
                attributes=["distinguishedName"],
                size_limit=1,
            )
            if len(connection.entries) != 1:
                raise ReconcileError(
                    f"The AD user search base {user_base!r} does not exist or is "
                    "not readable."
                )

            def find_group(group_name: str) -> str:
                search_filter = "".join(
                    [
                        "(&",
                        "(objectCategory=group)",
                        f"(sAMAccountName={escape_filter_chars(group_name)})",
                        ")",
                    ]
                )
                connection.search(
                    search_base=base_dn,
                    search_filter=search_filter,
                    search_scope=SUBTREE,
                    attributes=["distinguishedName"],
                    size_limit=2,
                )
                if len(connection.entries) != 1:
                    raise ReconcileError(
                        f"AD group {group_name!r} was not found exactly once below "
                        f"{base_dn!r}."
                    )
                return str(connection.entries[0].entry_dn)

            user_group_dn = find_group(config.user_group)
            admin_group_dn = find_group(config.admin_group)
        finally:
            connection.unbind()
    except ReconcileError:
        raise
    except Exception as exc:
        raise ReconcileError(
            "Could not query the selected Active Directory domain: "
            f"{sanitize(exc, (bind_password,))}"
        ) from None

    if user_group_dn.casefold() == admin_group_dn.casefold():
        raise ReconcileError(
            "The AD user and administrator groups resolve to the same DN."
        )

    return {
        "port": port,
        "bind_dn": bind_dn,
        "bind_password": bind_password,
        "user_base": user_base,
        "user_filter": build_user_filter(
            user_group_dn,
            nested=config.nested_groups,
            hidden_users_clause=proxy.get_ldap_users_search_filter_clause(
                config.domain
            ),
        ),
        "admin_filter": build_admin_filter(
            admin_group_dn, nested=config.nested_groups
        ),
    }


def ldap_source_arguments(settings: dict[str, object]) -> list[str]:
    return [
        "--name",
        MANAGED_SOURCE_NAME,
        "--active",
        "--security-protocol",
        "Unencrypted",
        "--host",
        CONTAINER_LDAP_HOST,
        "--port",
        str(settings["port"]),
        "--user-search-base",
        str(settings["user_base"]),
        "--user-filter",
        str(settings["user_filter"]),
        "--admin-filter",
        str(settings["admin_filter"]),
        "--restricted-filter",
        "",
        "--username-attribute",
        "sAMAccountName",
        "--firstname-attribute",
        "givenName",
        "--surname-attribute",
        "sn",
        "--email-attribute",
        "mail",
        "--public-ssh-key-attribute",
        "",
        "--avatar-attribute",
        "",
        "--bind-dn",
        str(settings["bind_dn"]),
        "--bind-password",
        str(settings["bind_password"]),
        "--synchronize-users",
        "--page-size",
        "1000",
        "--skip-tls-verify=false",
        "--attributes-in-bind=false",
        "--allow-deactivate-all=false",
        "--enable-groups=false",
        "--ssh-keys-are-verified=false",
        "--group-team-map-removal=false",
    ]


def disable_managed_source(sources: list[dict[str, str]]) -> None:
    matches = [source for source in sources if source["name"] == MANAGED_SOURCE_NAME]
    if len(matches) > 1:
        raise ReconcileError(
            f"More than one authentication source is named {MANAGED_SOURCE_NAME!r}."
        )
    if not matches:
        return
    source = matches[0]
    if not source["type"].startswith("LDAP") or "BindDN" not in source["type"]:
        # An incompatible source cannot have been created by this module. Do
        # not modify it, even if an earlier failed enable attempt set the
        # internal ownership marker.
        return
    if source["enabled"] == "false":
        return
    run_gitea(
        [
            "admin",
            "auth",
            "update-ldap",
            "--id",
            source["id"],
            "--not-active",
            "--disable-synchronize-users",
        ]
    )


def apply_managed_source(
    sources: list[dict[str, str]], settings: dict[str, object]
) -> None:
    source = managed_source(sources, allow_other_ldap=False)
    arguments = ldap_source_arguments(settings)
    if source is None:
        command = ["admin", "auth", "add-ldap", *arguments]
    else:
        command = [
            "admin",
            "auth",
            "update-ldap",
            "--id",
            source["id"],
            *arguments,
        ]
    bind_password = str(settings["bind_password"])
    run_gitea(command, secrets=(bind_password,))

    verified = managed_source(list_auth_sources(), allow_other_ldap=True)
    if verified is None or verified["enabled"] != "true":
        raise ReconcileError("The managed AD authentication source is not active.")


def reconcile() -> None:
    config = read_auth_config()
    wait_for_gitea()
    ensure_recovery_admin()
    sources = list_auth_sources()

    if not config.enabled:
        if not config.managed:
            return
        disable_managed_source(sources)
        return

    try:
        settings = resolve_ad_settings(config)
    except DomainUnavailableError:
        # If the domain was removed from NS8, do not leave a stale managed
        # source enabled. Other transient LDAP errors preserve the last known
        # working configuration so Gitea can recover when AD comes back.
        disable_managed_source(sources)
        raise
    apply_managed_source(sources, settings)


def main() -> int:
    lock_path = Path(LOCK_FILE)
    lock_path.touch(mode=0o600, exist_ok=True)
    lock_path.chmod(0o600)
    with lock_path.open("r+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            reconcile()
        except ReconcileError as exc:
            print(f"Gitea authentication reconciliation failed: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:  # Do not risk logging secrets from object reprs.
            print(
                "Gitea authentication reconciliation failed with an unexpected "
                f"{type(exc).__name__}.",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
