# Gitea for NethServer 8

This repository packages [Gitea](https://about.gitea.com/) as a NethServer 8
application. It is intended primarily for a private Git service on a local or
otherwise trusted network.

The module provides:

- Gitea over HTTPS through the NS8 Traefik instance
- Git over SSH on a second, dynamically allocated NS8 TCP port
- PostgreSQL 15 with persistent application and database volumes
- integration with the NS8 backup framework and optional mail through the
  NS8 smarthost
- a one-time choice between Gitea's web installer and NS8-managed setup
- private-by-default access settings in managed setup mode
- optional, managed Active Directory login through an NS8 account domain

## Runtime versions

Runtime images are deliberately pinned in `build-images.sh` and updated only
through reviewed changes:

- Gitea 28.0.0
- PostgreSQL 15.19 (Alpine 3.23)
- Node.js 24.20.0 for the reproducible UI build

The module requires NS8 core 3.2.2 or newer. PostgreSQL stays on major version
15 so an ordinary module update does not silently introduce a database
major-version migration.

## Install

Add the [Platypuschan NS8 module catalog](https://github.com/Platypuschan/ns8-modules#add-the-repository)
as a software repository and install **Gitea Reworked** from the NS8 Software
Center.

To install from the command line, use a released version number from
[`CATALOG_VERSION`](CATALOG_VERSION) or the catalog, for example:

```bash
add-module ghcr.io/platypuschan/gitea-reworked:0.4.2 1
```

The command returns the instance ID, for example `gitea-reworked1`.

Do not install production instances from `:latest` or a branch tag. NS8 takes
the displayed module version from the image tag and offers updates only to
instances with a SemVer version such as `0.3.0`; an instance installed from
`:latest` never receives update notifications.

Always use the actual instance ID returned by NS8.

## Configure

Configure the public hostname through the API or the NS8 application UI:

```bash
api-cli run module/gitea-reworked1/configure-module --data - <<'EOF'
{
  "host": "gitea.example.test",
  "http2https": true,
  "lets_encrypt": false,
  "setup_mode": "managed"
}
EOF
```

Use a resolvable fully qualified hostname. Enable Let's Encrypt only when the
hostname and challenge endpoint are publicly reachable as required by your
certificate setup.

The first successful configuration permanently selects one of two setup modes.
The mode cannot be switched later because reopening an initialized instance's
web installer or changing ownership of authentication settings is unsafe. To
choose another mode, install a new instance and migrate the repositories.

### Managed setup

Select `managed` for an unattended, private-by-default installation. NS8 locks
Gitea's web installer, initializes the database, and creates a local
`ns8-recovery-admin` with an unknown random password. See
[Recovery administrator](#recovery-administrator) before relying on it.

The managed defaults are:

- anonymous users must sign in
- self-registration is disabled
- newly created repositories default to private

Active Directory login remains optional in this mode. If it is disabled,
accounts can still be created and managed with Gitea's administration tools.

### Manual setup with Gitea's web installer

Select `manual` to use Gitea's normal first-run web installer:

```bash
api-cli run module/gitea-reworked1/configure-module --data - <<'EOF'
{
  "host": "gitea.example.test",
  "http2https": true,
  "lets_encrypt": false,
  "setup_mode": "manual",
  "ad_enabled": false
}
EOF
```

Open the configured Gitea URL immediately and complete the installer. Until it
is completed, anyone with network access can create the first administrator
account. NS8 continues to own the PostgreSQL connection, HTTPS route, SSH port,
and backups; do not replace the module-provided database settings.
Gitea owns user creation, authentication sources, mail, and application
security settings in this mode. The module does not create the recovery administrator
and does not manage an Active Directory source.

## Active Directory login

The Settings page can create and maintain one Gitea LDAP authentication source
from an Active Directory account domain already configured in NS8. For an example Active Directory domain, the equivalent API call is:

```bash
api-cli run module/gitea-reworked1/configure-module --data - <<'EOF'
{
  "host": "gitea.example.test",
  "http2https": true,
  "lets_encrypt": false,
  "setup_mode": "managed",
  "ad_enabled": true,
  "ad_domain": "ad.example.test",
  "ad_user_group": "gitea-user",
  "ad_admin_group": "gitea-admin",
  "ad_user_search_base": "CN=Users,DC=ad,DC=example,DC=test",
  "ad_nested_groups": false
}
EOF
```

The rules are intentionally strict:

- every user, including administrators, must be a member of `gitea-user`
- membership in `gitea-admin` additionally grants Gitea administrator rights
- disabled AD accounts are rejected
- direct group membership is used by default; enable nested groups only when
  indirect memberships are required

Group names are resolved to their actual distinguished names below the AD
domain base. They therefore do not need to remain in `CN=Users`. If the user
search base is left blank, the module derives `CN=Users,<domain base DN>`.

The module connects only to the node-local NS8 LDAP proxy. NS8 handles backend
TLS and replica failover, while Gitea receives the current proxy port and bind
credentials automatically. An AD outage does not stop Gitea; the last working
source remains configured and reconciliation is retried on a domain change or
service restart.

The managed Gitea source is named `NS8 Active Directory`. If another LDAP
source already exists under a different name, configuration stops instead of
silently creating a duplicate or overwriting it. Review the existing source,
then either remove it or rename the intended LDAP-via-Bind-DN source to
`NS8 Active Directory` so the module can adopt it.

### Recovery administrator

The local `ns8-recovery-admin` is independent of AD and is reserved for
break-glass access. In managed setup mode, expand **Recovery administrator** on
the module settings page to view and copy its account name and password. The
password is generated with a cryptographically secure random generator and is
stored in the module state file `gitea-recovery.env` with mode `0600`; it is not
added to the container environment or written to application logs.

Use **Generate new password** to replace the current password. The old password
stops working immediately after a successful rotation. Instances upgraded from
a release that did not store recovery credentials show an unavailable notice:
generating a new password is an explicit operation and does not silently
replace a password that an operator may have set manually.

Gitea's administration CLI receives recovery and LDAP bind passwords as
command arguments. They can briefly appear in process listings on the host or
inside the container during account changes and AD reconciliation. After a
password-change timeout, the module checks whether the new password already
works before publishing it. If Gitea is unreachable for that check, retry the
rotation after service recovery if the displayed password does not work.

Do not use this account for routine work. If a pre-existing account already
uses the reserved name but is inactive or is not an administrator, the module
refuses to take it over.

## Git over SSH

NS8 reserves two TCP ports for each instance:

1. the internal web backend port, bound only to loopback and routed by Traefik
2. the Git SSH port, forwarded to port 22 of the Gitea container

Retrieve the assigned SSH port with:

```bash
api-cli run module/gitea-reworked1/get-configuration
```

Example output:

```json
{
  "host": "gitea.example.test",
  "http2https": true,
  "lets_encrypt": false,
  "ssh_port": 20042,
  "setup_mode": "managed",
  "ad_enabled": true,
  "ad_domain": "ad.example.test",
  "ad_user_group": "gitea-user",
  "ad_admin_group": "gitea-admin",
  "ad_user_search_base": "CN=Users,DC=ad,DC=example,DC=test",
  "ad_nested_groups": false
}
```

Gitea advertises clone URLs using that external port. A corresponding manual
clone command is:

```bash
git clone ssh://git@gitea.example.test:20042/OWNER/REPOSITORY.git
```

The module adds the SSH port to the NS8 node firewall automatically. For a
strictly local service, do not forward this port on the Internet-facing router
and restrict access at the surrounding network firewall as appropriate.

## Updates

Create and verify an NS8 application backup before every Gitea upgrade. Review
the Gitea release notes, then update the instance from the NS8 Software Center
or from the command line with the new version number:

```bash
api-cli run update-module --data '{
  "module_url": "ghcr.io/platypuschan/gitea-reworked:0.4.2",
  "instances": ["gitea-reworked1"]
}'
```

An instance that shows the version `latest` was installed or updated from the
moving `:latest` tag. Update it once with the command above to a released
version; afterwards the Software Center offers new catalog versions again.
`force` is only needed for moving development tags such as `:latest`, because
it makes NS8 pull the image again even if the tag is already present locally.

Updates and restores are supported from the base version **0.2.0** onward.
Older releases of this module and the original NS8 Gitea module
(`geniusdynamics/gitea`) cannot be updated to it, and their backups cannot be
restored. Install a new instance instead.

Gitea applies its own schema migrations during startup. Wait for the Status
page to report a healthy service before allowing users to push again.

### Rollback

Do not downgrade only the Gitea container image after it has migrated the
database. The safe rollback path is to restore a backup taken before the
upgrade with the compatible module image. Keep the pre-upgrade backup until
the updated instance has been exercised and verified.

## Backup consistency

The NS8 backup contains a validated PostgreSQL custom-format dump, the Gitea
configuration, and the persistent Gitea data volume. The database dump is
written atomically, and restore aborts on archive or SQL errors.

The database and file volume are separate resources, so they cannot form one
cross-resource transactional snapshot while Gitea is writing. Gitea upstream
therefore requires the application process to be stopped for a fully consistent
backup. Leave PostgreSQL running so the module can create its database dump:

```bash
runagent -m gitea-reworked1 systemctl --user stop gitea-app.service
```

Start and wait for the on-demand NS8 application backup, then start Gitea again
even if the backup failed:

```bash
runagent -m gitea-reworked1 systemctl --user start gitea-app.service
```

Do not stop `gitea.service` for this procedure because that also stops the
PostgreSQL service required by the backup hook. If downtime is not acceptable,
at minimum schedule backups for a period with no repository pushes, attachment
uploads, package writes, or administrative changes.

## Mail and smarthost

In managed setup mode, Gitea sends mail only if **Send Gitea email through the
NS8 smarthost** is enabled in the module settings (`"smarthost_mail": true`).
The option is off for new instances and after updating from 0.1.x.

- **Enabled:** the module takes host, port, encryption, and credentials from
  the NS8 smarthost. Changes to the cluster smarthost restart the Gitea
  container with the new values. The sender defaults to
  `no-reply@<Gitea hostname>` instead of the SMTP login name, which may not be a
  valid email address; set **Gitea mail sender** if the relay only accepts a
  particular sender.
- **Disabled, or no smarthost available:** the Gitea mailer is disabled and its
  host, user, and password are cleared from `app.ini`. This also applies when a
  backup is restored to a cluster without a smarthost, or when the smarthost
  settings cannot be read at startup; mail is never sent with settings left
  over from another system.

In manual setup mode the module does not change Gitea's mail settings.
Configure mail in the web installer or in `app.ini`. The option is not
available in this mode. Manual instances configured with 0.1.x keep the
smarthost values that release wrote to `app.ini` until you change them in
Gitea.

If a backup is restored or an instance is cloned into a cluster without its
configured AD domain, Gitea still starts with the local recovery administrator
and the managed AD source is disabled. The AD settings remain saved; adding the
domain to the target cluster and triggering reconciliation enables the source
again. A new explicit AD configuration still requires a domain that exists.

## Uninstall

```bash
remove-module --no-preserve gitea-reworked1
```

The `--no-preserve` option permanently removes the instance data. Verify your
backups before using it.

## Testing

The repository runs:

- syntax validation for Python, JSON, shell scripts, and current systemd state
  paths
- unit tests for AD filters, safe source adoption, configuration handling,
  and recovery-account handling
- deterministic Yarn install, UI lint, and production UI build
- the NS8 install scenario on supported test nodes
- an upgrade from the newest released catalog version to the tested image,
  without `force`, as the Software Center performs it
- HTTPS health checks and an SSH protocol-banner check on the allocated port

To run the Robot Framework tests against a live NS8 leader, install
`run-ns8-tests` as documented by
[ns8-github-actions](https://github.com/NethServer/ns8-github-actions/blob/v1/README.md#running-tests-locally),
then run:

```bash
run-ns8-tests NS8_LEADER ghcr.io/platypuschan/gitea-reworked:latest
```

Dependency updates are proposed through the shared NS8 Renovate preset and
require manual review; they are not auto-merged.

## Releases

[`CATALOG_VERSION`](CATALOG_VERSION) holds the SemVer version of the next
catalog release. Every change to `imageroot/`, `ui/` or `build-images.sh`
must raise it; the Validate workflow fails otherwise. Documentation and test
changes do not need a new version.

After a push to `main` passes all image tests, the *Publish tested catalog
version* workflow copies exactly the tested image digest to the tag named in
`CATALOG_VERSION`. An existing version tag is never overwritten. The
[catalog](https://github.com/Platypuschan/ns8-modules) picks up new SemVer
tags automatically. `:latest` always follows `main` and is meant only for
development and tests.
