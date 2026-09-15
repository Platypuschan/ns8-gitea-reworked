# Gitea for NethServer 8

This repository packages [Gitea](https://about.gitea.com/) as a NethServer 8
application. It is intended primarily for a private Git service on a local or
otherwise trusted network.

The module provides:

- Gitea over HTTPS through the NS8 Traefik instance
- Git over SSH on a second, dynamically allocated NS8 TCP port
- PostgreSQL 15 with persistent application and database volumes
- integration with the NS8 smarthost and backup framework
- private-by-default access settings

## Runtime versions

Runtime images are deliberately pinned in `build-images.sh` and updated only
through reviewed changes:

- Gitea 1.27.3
- PostgreSQL 15.19 (Alpine 3.23)
- Node.js 24.20.0 for the reproducible UI build

The module requires NS8 core 3.2.2 or newer. PostgreSQL stays on major version
15 so an ordinary module update does not silently introduce a database
major-version migration.

## Install

Install the module on an NS8 node:

```bash
add-module ghcr.io/platypuschan/gitea-reworked:latest 1
```

The command returns the instance ID, for example `gitea-reworked1`.

An instance upgraded from the original module can retain an older ID such as
`gitea1`; always use the actual instance ID returned by NS8.

## Configure

Configure the public hostname through the API or the NS8 application UI:

```bash
api-cli run module/gitea-reworked1/configure-module --data - <<'EOF'
{
  "host": "gitea.example.test",
  "http2https": true,
  "lets_encrypt": false
}
EOF
```

Use a resolvable fully qualified hostname. Enable Let's Encrypt only when the
hostname and challenge endpoint are publicly reachable as required by your
certificate setup.

On first access, complete Gitea's installation form and create the
administrator account in the **Administrator Account Settings** section.
Self-registration is disabled after installation, so additional accounts
should be created by an administrator.

The managed defaults are suitable for a private service:

- anonymous users must sign in
- self-registration is disabled
- newly created repositories default to private

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
  "ssh_port": 20042
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
the Gitea release notes, then update the instance:

```bash
api-cli run update-module --data '{
  "module_url": "ghcr.io/platypuschan/gitea-reworked:latest",
  "instances": ["gitea-reworked1"],
  "force": true
}'
```

When updating an instance created by the original one-port module, the
migration is automatic:

- the existing web port is preserved
- one additional NS8 TCP port is allocated for Git SSH
- the SSH port is opened in the node firewall
- the canonical public Gitea URL and smarthost variables are corrected
- existing PostgreSQL data remains on major version 15

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

## Smarthost

Mailer settings are discovered from the centralized NS8 smarthost
configuration. Changes to the cluster smarthost regenerate the managed Gitea
mailer environment and restart the application container.

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
- deterministic Yarn install, UI lint, and production UI build
- the official NS8 install and update scenarios on supported test nodes
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
