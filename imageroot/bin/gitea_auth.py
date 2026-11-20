#!/usr/bin/env python3

#
# Copyright (C) 2026
# SPDX-License-Identifier: GPL-3.0-or-later
#

"""Reconcile Gitea's local recovery account and managed AD login source."""

from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import ipaddress
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path


AUTH_ENV_FILE = "gitea-auth.env"
SETUP_ENV_FILE = "gitea-setup.env"
RECOVERY_ENV_FILE = "gitea-recovery.env"
LOCK_FILE = ".gitea-auth.lock"
MANAGED_SOURCE_NAME = "NS8 Active Directory"
SSO_ENV_FILE = "gitea-sso.env"
SSO_SOURCE_NAME = "ns8-sso"
SSO_SCOPES = "profile email"
SSO_BLOCKED = "blocked"
SSO_GROUPS_CLAIM = "groups"
REVOKE_LOCKS_FILE = "gitea-sso-locks.json"
REVOKE_STATUS_FILE = "gitea-sso-revoke-status.json"
SSO_BLOCK_CLAIM = "ns8-sso-disabled"
RECOVERY_USERNAME = "ns8-recovery-admin"
RECOVERY_EMAIL = "ns8-recovery-admin@localhost.invalid"
RECOVERY_PASSWORD_KEY = "GITEA_RECOVERY_PASSWORD"
RECOVERY_USERNAME_KEY = "GITEA_RECOVERY_USERNAME"
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


def read_setup_mode() -> str:
    import agent

    values = agent.read_envfile(SETUP_ENV_FILE)
    mode = values.get("GITEA_SETUP_MODE", "").strip().lower()
    if mode not in {"pending", "manual", "managed"}:
        raise ReconcileError("The stored Gitea setup mode is invalid.")
    return mode


def sanitize(message: object, secrets: tuple[str, ...] = ()) -> str:
    sanitized = str(message)
    for secret in secrets:
        if secret:
            sanitized = sanitized.replace(secret, "[redacted]")
    return sanitized


@contextlib.contextmanager
def authentication_lock():
    """Serialize all operations that modify managed Gitea authentication."""

    lock_path = Path(LOCK_FILE)
    lock_path.touch(mode=0o600, exist_ok=True)
    lock_path.chmod(0o600)
    with lock_path.open("r+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


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


def read_recovery_credentials() -> dict[str, str] | None:
    """Read the module-owned recovery credentials, if they exist."""

    import agent

    credentials_path = Path(RECOVERY_ENV_FILE)
    try:
        credentials_path.chmod(0o600)
        values = agent.read_envfile(RECOVERY_ENV_FILE)
    except FileNotFoundError:
        return None

    username = values.get(RECOVERY_USERNAME_KEY, "").strip()
    password = values.get(RECOVERY_PASSWORD_KEY, "")
    if username != RECOVERY_USERNAME or not 32 <= len(password) <= 128:
        raise ReconcileError("The stored recovery credentials are invalid.")
    return {"username": username, "password": password}


def generate_recovery_password() -> str:
    """Return a secure URL-safe password satisfying all Gitea classes."""

    while True:
        password = secrets.token_urlsafe(36)
        if (
            any(character.islower() for character in password)
            and any(character.isupper() for character in password)
            and any(character.isdigit() for character in password)
            and any(character in "-_" for character in password)
        ):
            return password


def stage_recovery_credentials(password: str) -> Path:
    """Write new credentials to a protected temporary file."""

    import agent

    if not 32 <= len(password) <= 128:
        raise ReconcileError("Refusing to store an invalid recovery password.")
    temporary_path = Path(f"{RECOVERY_ENV_FILE}.tmp")
    temporary_path.unlink(missing_ok=True)
    try:
        # Pre-create the file so the secret is owner-only from its first byte,
        # rather than relying on the process umask until chmod runs.
        temporary_path.touch(mode=0o600, exist_ok=False)
        agent.write_envfile(
            str(temporary_path),
            {
                RECOVERY_USERNAME_KEY: RECOVERY_USERNAME,
                RECOVERY_PASSWORD_KEY: password,
            },
        )
        temporary_path.chmod(0o600)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return temporary_path


def commit_recovery_credentials(temporary_path: Path) -> None:
    """Atomically publish staged credentials with owner-only permissions."""

    os.replace(temporary_path, RECOVERY_ENV_FILE)
    Path(RECOVERY_ENV_FILE).chmod(0o600)


def recovery_admin_user() -> dict[str, str] | None:
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
        return user
    return None


def ensure_recovery_admin() -> None:
    if recovery_admin_user() is not None:
        return

    credentials = read_recovery_credentials()
    password = (
        credentials["password"] if credentials else generate_recovery_password()
    )
    staged_credentials = None
    if credentials is None:
        staged_credentials = stage_recovery_credentials(password)

    try:
        run_gitea(
            [
                "admin",
                "user",
                "create",
                "--username",
                RECOVERY_USERNAME,
                "--email",
                RECOVERY_EMAIL,
                "--password",
                password,
                "--admin",
                "--must-change-password=false",
            ],
            secrets=(password,),
            redact_stdout=True,
        )
        if staged_credentials is not None:
            commit_recovery_credentials(staged_credentials)
    finally:
        if staged_credentials is not None:
            staged_credentials.unlink(missing_ok=True)


def recovery_password_is_active(password: str) -> bool:
    """Resolve an ambiguous CLI failure without exposing the password in logs."""

    port = os.environ.get("TCP_PORT", "").strip()
    if not port.isdigit():
        return False
    credential = base64.b64encode(
        f"{RECOVERY_USERNAME}:{password}".encode("utf-8")
    ).decode("ascii")
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/v1/user")
    request.add_header("Authorization", f"Basic {credential}")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            user = json.load(response)
        return (
            isinstance(user, dict)
            and str(user.get("login", "")).casefold() == RECOVERY_USERNAME.casefold()
        )
    except (OSError, urllib.error.URLError, ValueError, TypeError):
        return False


def reset_recovery_password() -> None:
    """Replace and persist the managed recovery administrator password."""

    if read_setup_mode() != "managed":
        raise ReconcileError(
            "Recovery credentials are available only in NS8-managed setup mode."
        )

    wait_for_gitea()
    ensure_recovery_admin()
    password = generate_recovery_password()
    staged_credentials = stage_recovery_credentials(password)
    try:
        try:
            run_gitea(
                [
                    "admin",
                    "user",
                    "change-password",
                    "--username",
                    RECOVERY_USERNAME,
                    "--password",
                    password,
                    "--must-change-password=false",
                ],
                secrets=(password,),
                redact_stdout=True,
            )
        except ReconcileError as exc:
            # The command can time out after Gitea has committed the change.
            # Verify the new credential before deciding whether to discard it.
            if not recovery_password_is_active(password):
                raise ReconcileError(
                    f"{exc} If the old password no longer works, repeat the "
                    "reset once Gitea is reachable."
                ) from None
        commit_recovery_credentials(staged_credentials)
    finally:
        staged_credentials.unlink(missing_ok=True)


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


def reconcile_ad(config: AuthConfig) -> None:
    sources = list_auth_sources()

    if not config.enabled:
        if not config.managed:
            return
        disable_managed_source(sources)
        return

    try:
        settings = resolve_ad_settings(config)
    except DomainUnavailableError as exc:
        # If the domain was removed from NS8, do not leave a stale managed
        # source enabled. Other transient LDAP errors preserve the last known
        # working configuration so Gitea can recover when AD comes back.
        disable_managed_source(sources)
        # A restored or cloned module can refer to a domain that has not yet
        # been installed in the target cluster. Keep the recovery account and
        # web route usable; a later domain event retries reconciliation.
        print(f"Gitea managed AD login is inactive: {exc}", file=sys.stderr)
        return
    apply_managed_source(sources, settings)


@dataclass(frozen=True)
class SsoConfig:
    enabled: bool
    idp_module: str
    domain: str
    applied: str
    admin_group: str = ""


def read_sso_config() -> SsoConfig:
    import agent

    try:
        values = agent.read_envfile(SSO_ENV_FILE)
    except FileNotFoundError:
        values = {}
    return SsoConfig(
        enabled=parse_bool(values.get("GITEA_SSO_ENABLED")),
        idp_module=values.get("GITEA_SSO_IDP_MODULE", "").strip(),
        domain=values.get("GITEA_SSO_DOMAIN", "").strip(),
        applied=values.get("GITEA_SSO_APPLIED", "").strip(),
        admin_group=values.get("GITEA_SSO_ADMIN_GROUP", "").strip(),
    )


def write_sso_applied(config: SsoConfig, applied: str) -> None:
    import agent

    agent.write_envfile(
        SSO_ENV_FILE,
        {
            "GITEA_SSO_ENABLED": str(config.enabled).lower(),
            "GITEA_SSO_IDP_MODULE": config.idp_module,
            "GITEA_SSO_DOMAIN": config.domain,
            "GITEA_SSO_ADMIN_GROUP": config.admin_group,
            "GITEA_SSO_APPLIED": applied,
        },
    )
    os.chmod(SSO_ENV_FILE, 0o600)


def application_host() -> str:
    import agent

    host = os.environ.get("TRAEFIK_HOST", "").strip()
    if not host:
        host = agent.read_envfile("environment").get("TRAEFIK_HOST", "").strip()
    if not host:
        raise ReconcileError("The application host name is not configured.")
    return host


def register_sso_client(config: SsoConfig, host: str) -> dict[str, str]:
    """Create or update this module's OIDC client; the IdP keeps its secret."""

    import agent.tasks

    callback = f"https://{host}/user/oauth2/{SSO_SOURCE_NAME}/callback"
    try:
        response = agent.tasks.run(
            agent_id="module/" + config.idp_module,
            action="register-client",
            data={
                "domain": config.domain,
                "redirect_uris": [callback],
                "post_logout_redirect_uris": [f"https://{host}/"],
                "web_origins": [f"https://{host}"],
                # Fail closed: nobody signs in until an administrator picks
                # the LDAP group on the IdP access page. The IdP never clears
                # this flag, so a client that is deleted and registered again
                # (SSO switched off and on) cannot lose its restriction.
                "access_required": True,
                # Current LDAP memberships in the administrator group, read
                # live by the IdP at every token. An IdP without this feature
                # ignores the attribute.
                "groups_claim": [config.admin_group] if config.admin_group else [],
            },
            extra={"isNotificationHidden": True},
        )
    except Exception as exc:
        raise ReconcileError(
            f"The IdP register-client task could not run: {type(exc).__name__}."
        ) from None
    if response.get("exit_code") != 0:
        raise ReconcileError(
            f"The IdP {config.idp_module} rejected register-client for domain "
            f"{config.domain}. Check that the module is bound to the domain."
        )
    output = response.get("output")
    registration = json.loads(output) if isinstance(output, str) else output
    for key in ("client_id", "client_secret", "issuer"):
        if not isinstance(registration.get(key), str) or not registration[key]:
            raise ReconcileError(f"The IdP registration lacks {key}.")
    return registration


def restart_gitea() -> None:
    subprocess.run(["systemctl", "--user", "restart", "gitea.service"], check=True)
    wait_for_gitea()


def reconcile_sso() -> None:
    """Keep the OIDC source of the IdP in sync with the module settings."""

    config = read_sso_config()
    sources = list_auth_sources()
    matches = [s for s in sources if s["name"] == SSO_SOURCE_NAME]
    if len(matches) > 1:
        raise ReconcileError(f"More than one authentication source is named {SSO_SOURCE_NAME!r}.")
    source = matches[0] if matches else None
    if source is not None and not source["type"].startswith("OAuth2"):
        raise ReconcileError(
            f"Authentication source {SSO_SOURCE_NAME!r} exists with an incompatible type. "
            "It was not modified."
        )

    if not config.enabled:
        if config.applied and source is not None:
            try:
                run_gitea(["admin", "auth", "delete", "--id", source["id"]])
            except ReconcileError as exc:
                # Gitea refuses to delete a source that accounts still use and
                # has no switch to deactivate it. Block every login instead:
                # no IdP token carries this claim.
                print(f"The SSO source could not be deleted: {exc}", file=sys.stderr)
                if config.applied != SSO_BLOCKED:
                    run_gitea(
                        [
                            "admin", "auth", "update-oauth", "--id", source["id"],
                            "--required-claim-name", SSO_BLOCK_CLAIM,
                            "--required-claim-value", SSO_BLOCK_CLAIM,
                        ]
                    )
                    write_sso_applied(config, SSO_BLOCKED)
                return
        if config.applied:
            write_sso_applied(config, "")
        return

    host = application_host()
    registration = register_sso_client(config, host)
    discovery = registration["issuer"].rstrip("/") + "/.well-known/openid-configuration"
    fingerprint = hashlib.sha256(
        "\0".join(
            [
                registration["client_id"], registration["client_secret"], discovery,
                SSO_SCOPES, config.admin_group,
            ]
        ).encode()
    ).hexdigest()
    if source is not None and config.applied == fingerprint:
        return

    arguments = [
        "--name", SSO_SOURCE_NAME,
        "--provider", "openidConnect",
        "--key", registration["client_id"],
        "--secret", registration["client_secret"],
        "--auto-discover-url", discovery,
        "--scopes", SSO_SCOPES,
    ]
    if config.admin_group:
        arguments += ["--group-claim-name", SSO_GROUPS_CLAIM, "--admin-group", config.admin_group]
    elif source is not None:
        arguments += ["--group-claim-name", "", "--admin-group", ""]
    if source is not None:
        # Clear a block left by an earlier disable operation.
        arguments += ["--required-claim-name", "", "--required-claim-value", ""]
    secret = registration["client_secret"]
    if source is None:
        run_gitea(["admin", "auth", "add-oauth", *arguments], secrets=(secret,))
    else:
        run_gitea(
            ["admin", "auth", "update-oauth", "--id", source["id"], *arguments],
            secrets=(secret,),
        )
        # Gitea keeps the old provider settings in memory until it restarts.
        restart_gitea()
    verified = [s for s in list_auth_sources() if s["name"] == SSO_SOURCE_NAME]
    if len(verified) != 1 or verified[0]["enabled"] != "true":
        raise ReconcileError("The SSO authentication source is not active.")
    write_sso_applied(config, fingerprint)


def reconcile(strict_sso: bool = False) -> None:
    # The web installer and all manually created authentication sources belong
    # exclusively to Gitea in manual mode. Pending instances have not made the
    # one-time setup choice yet.
    if read_setup_mode() != "managed":
        return
    config = read_auth_config()
    wait_for_gitea()
    ensure_recovery_admin()
    try:
        reconcile_ad(config)
    finally:
        try:
            reconcile_sso()
        except ReconcileError as exc:
            # Restore, clone and domain events must not fail because an IdP
            # is temporarily missing; configure-module passes --strict-sso.
            print(f"Gitea SSO reconciliation failed: {exc}", file=sys.stderr)
            if strict_sso:
                raise


# ---------------------------------------------------------------------------
# Access revocation: the IdP stops new SSO logins of users outside its access
# group, but Gitea keeps their sessions, tokens and SSH keys. A timer locks
# the Gitea account (prohibit_login) of SSO users who left the group and
# unlocks the accounts it locked itself when they return. Nothing is deleted.
# ---------------------------------------------------------------------------


def idp_access_group(config: SsoConfig) -> str:
    """The LDAP group the administrator selected for this client at the IdP."""

    import agent.tasks

    try:
        response = agent.tasks.run(
            agent_id="module/" + config.idp_module,
            action="get-client-access",
            data={"domain": config.domain},
            extra={"isNotificationHidden": True},
        )
    except Exception as exc:
        raise ReconcileError(
            f"The IdP access state could not be read: {type(exc).__name__}."
        ) from None
    if response.get("exit_code") != 0:
        raise ReconcileError("The IdP refused to report the client access state.")
    output = response.get("output")
    state = json.loads(output) if isinstance(output, str) else output
    group = state.get("group") if isinstance(state, dict) else None
    if not state.get("enforced") or not isinstance(group, str) or not group:
        raise ReconcileError(
            "The IdP client has no active access group; no account was changed."
        )
    return group


def ldap_group_members(domain_name: str, group_name: str) -> set[str]:
    """Case-folded sAMAccountNames of the enabled direct members of a group."""

    from agent.ldapproxy import Ldapproxy

    proxy = Ldapproxy()
    domain = proxy.get_domain(domain_name)
    if domain is None:
        raise ReconcileError(f"The account domain {domain_name!r} is not available.")
    host = str(domain.get("host", ""))
    _validate_loopback_host(host)
    base_dn = str(domain.get("base_dn", "")).strip()
    bind_dn = str(domain.get("bind_dn", "")).strip()
    bind_password = str(domain.get("bind_password", ""))
    try:
        port = int(domain["port"])
    except (KeyError, TypeError, ValueError):
        raise ReconcileError("NS8 returned an invalid LDAP proxy port.") from None
    if not base_dn or not bind_dn or not bind_password:
        raise ReconcileError("The account domain is missing LDAP bind parameters.")
    try:
        from ldap3 import NONE, SUBTREE, Connection, Server
        from ldap3.utils.conv import escape_filter_chars

        connection = Connection(
            Server(host, port=port, connect_timeout=10, get_info=NONE),
            user=bind_dn,
            password=bind_password,
            auto_bind=True,
            receive_timeout=15,
            raise_exceptions=True,
        )
        try:
            escaped = escape_filter_chars(group_name)
            connection.search(
                search_base=base_dn,
                search_filter=(
                    "(&(objectCategory=group)"
                    f"(|(sAMAccountName={escaped})(cn={escaped})))"
                ),
                search_scope=SUBTREE,
                attributes=["distinguishedName"],
                size_limit=2,
            )
            if len(connection.entries) != 1:
                raise ReconcileError(
                    f"The group {group_name!r} was not found exactly once in the directory."
                )
            group_dn = str(connection.entries[0].entry_dn)
            members: set[str] = set()
            entries = connection.extend.standard.paged_search(
                base_dn,
                "".join(
                    [
                        "(&(objectCategory=person)(objectClass=user)",
                        membership_filter(group_dn, False),
                        f"(!(userAccountControl:{LDAP_BITWISE_AND}:=2))",
                        proxy.get_ldap_users_search_filter_clause(domain_name),
                        ")",
                    ]
                ),
                search_scope=SUBTREE,
                attributes=["sAMAccountName"],
                paged_size=500,
                generator=True,
            )
            for entry in entries:
                name = (entry.get("attributes") or {}).get("sAMAccountName")
                if isinstance(name, list):
                    name = name[0] if name else None
                if name:
                    members.add(str(name).casefold())
            return members
        finally:
            connection.unbind()
    except ReconcileError:
        raise
    except Exception as exc:
        raise ReconcileError(
            "Could not read the group members from the directory: "
            f"{sanitize(exc, (bind_password,))}"
        ) from None


def linked_sso_users() -> list[str]:
    """Gitea users that signed in or were linked through the ns8-sso source."""

    query = (
        'select u.name from external_login_user e join "user" u on u.id = e.user_id '
        "join login_source s on s.id = e.login_source_id "
        f"where s.name = '{SSO_SOURCE_NAME}' order by u.name;"
    )
    try:
        result = subprocess.run(
            [
                "podman", "exec", "-i", "postgresql-app", "sh", "-ceu",
                'exec psql -At -U "$POSTGRES_USER" -d "$POSTGRES_DB"',
            ],
            input=query, capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReconcileError(f"The database query failed: {type(exc).__name__}.") from None
    if result.returncode != 0:
        raise ReconcileError("The database query for SSO users failed.")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


class GiteaApi:
    """Administration API of the local Gitea, as the recovery administrator."""

    def __init__(self, credentials: dict[str, str]):
        port = os.environ.get("TCP_PORT", "").strip()
        if not port.isdigit():
            raise ReconcileError("TCP_PORT is missing or invalid.")
        self.base = f"http://127.0.0.1:{port}/api/v1"
        token = base64.b64encode(
            f"{credentials['username']}:{credentials['password']}".encode()
        ).decode()
        self.headers = {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

    def request(self, method: str, path: str, body: object = None) -> object:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.base + path, data=data, headers=self.headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            raise ReconcileError(f"Gitea API {method} {path.split('?')[0]}: HTTP {exc.code}") from None
        except (OSError, urllib.error.URLError) as exc:
            raise ReconcileError(f"Gitea API {method} {path.split('?')[0]} failed: {type(exc).__name__}") from None
        return json.loads(payload) if payload else None

    def users(self) -> dict[str, dict]:
        found: dict[str, dict] = {}
        page = 1
        while True:
            chunk = self.request("GET", f"/admin/users?limit=50&page={page}")
            if not chunk:
                return found
            for user in chunk:
                found[user["login"]] = user
            page += 1

    def set_prohibit_login(self, user: dict, value: bool) -> None:
        # The edit endpoint requires the account's current login source, so
        # the account is edited with exactly its own values.
        self.request(
            "PATCH",
            "/admin/users/" + urllib.parse.quote(user["login"], safe=""),
            {
                "login_name": user.get("login_name", ""),
                "source_id": user.get("source_id", 0),
                "prohibit_login": value,
            },
        )


def read_revoke_locks() -> set[str]:
    try:
        with open(REVOKE_LOCKS_FILE, encoding="utf-8") as handle:
            return {str(name) for name in json.load(handle)}
    except FileNotFoundError:
        return set()


def write_revoke_locks(names: set[str]) -> None:
    temporary = REVOKE_LOCKS_FILE + ".tmp"
    with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as handle:
        json.dump(sorted(names), handle)
    os.replace(temporary, REVOKE_LOCKS_FILE)


def write_revoke_status(**values: object) -> None:
    temporary = REVOKE_STATUS_FILE + ".tmp"
    values["time"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as handle:
        json.dump(values, handle)
    os.replace(temporary, REVOKE_STATUS_FILE)


def revoke_sso_access() -> dict[str, object]:
    """Lock SSO accounts outside the IdP access group; unlock returning ones."""

    if read_setup_mode() != "managed":
        return {"skipped": "not managed"}
    config = read_sso_config()
    if not (config.enabled and config.idp_module and config.domain):
        return {"skipped": "sso disabled"}
    if subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", "gitea-app.service"], check=False
    ).returncode != 0:
        return {"skipped": "gitea is not running"}
    credentials = read_recovery_credentials()
    if credentials is None:
        raise ReconcileError("The recovery administrator is not available.")

    group = idp_access_group(config)
    members = ldap_group_members(config.domain, group)
    api = GiteaApi(credentials)
    accounts = api.users()
    linked = [name for name in linked_sso_users() if name in accounts]
    locks = read_revoke_locks()
    blocked: list[str] = []
    unblocked: list[str] = []
    failed: list[str] = []

    for name in linked:
        account = accounts[name]
        if name == RECOVERY_USERNAME or name.casefold() in members:
            continue
        if account.get("prohibit_login"):
            # Locked by an administrator or by an earlier run: keep it, and
            # only own the lock if this module placed it.
            continue
        try:
            api.set_prohibit_login(account, True)
        except ReconcileError:
            failed.append(name)
            continue
        account["prohibit_login"] = True
        locks.add(name)
        write_revoke_locks(locks)
        blocked.append(name)

    for name in sorted(locks):
        account = accounts.get(name)
        if account is None:
            locks.discard(name)  # deleted account
        elif not account.get("prohibit_login"):
            locks.discard(name)  # an administrator unlocked it
        elif name.casefold() in members:
            try:
                api.set_prohibit_login(account, False)
            except ReconcileError:
                failed.append(name)
                continue
            locks.discard(name)
            unblocked.append(name)
    write_revoke_locks(locks)

    report = {"group": group, "blocked": blocked, "unblocked": unblocked, "failed": failed}
    write_revoke_status(**report, error="")
    if failed:
        raise ReconcileError(f"{len(failed)} account(s) could not be updated.")
    return report


def revoke_main() -> int:
    with authentication_lock():
        try:
            report = revoke_sso_access()
        except ReconcileError as exc:
            write_revoke_status(error=str(exc))
            print(f"Gitea SSO access revocation failed: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:
            write_revoke_status(error=type(exc).__name__)
            print(
                f"Gitea SSO access revocation failed with an unexpected {type(exc).__name__}.",
                file=sys.stderr,
            )
            return 1
    print("Gitea SSO access revocation: " + json.dumps(report), file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    strict_sso = "--strict-sso" in (sys.argv[1:] if argv is None else argv)
    with authentication_lock():
        try:
            reconcile(strict_sso)
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
