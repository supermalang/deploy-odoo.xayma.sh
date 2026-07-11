Deploy-Odoo
=========

This role deploys and manages per-customer Odoo instances as Kubernetes
workloads on the Xayma.sh Platform's single-node **k3s** cluster (the
`install-platform.xayma.sh` repo — Traefik v3 + cert-manager + `kubernetes.core`).
It is intended to run as an AWX job launched by the Xayma app, which passes
identity vars and a fully-resolved `plan` object as `extra_vars`. It can also
be run from the CLI for manual/testing use (a survey provides sane fallbacks).

```bash
ansible-playbook site.yml -i production \
  -e action=deploy -e customer=supermalang -e instancename=laundromat \
  -e domain=laundromat.supermalang.com -e version=19 \
  -e '{"plan": { ... }}' \
  --vault-password-file vault_password -K
```

Architecture
------------
- **One namespace per customer** (e.g. `supermalang`), holding every Odoo
  instance for that customer.
- **Each instance is a self-contained slice** inside that namespace: an Odoo
  Deployment, its OWN dedicated Postgres StatefulSet (not the platform's
  shared Postgres — full per-tenant isolation), a ConfigMap (`odoo.conf`) +
  Secret (DB/admin passwords), filestore + addons PVCs, a Service, an
  IngressRoute + cert-manager Certificate, and a set of NetworkPolicies.
- Kubernetes object names are `{customer}-{instance}-odoo{version}` (e.g.
  `supermalang-laundromat-odoo19`) — `customer`/`instancename` are slugified
  to RFC-1123 (lowercase, `[a-z0-9-]`, ≤63 chars); un-sluggable input fails
  fast with a clear error rather than a mid-deploy Kubernetes API rejection.
- Every resource carries `app.kubernetes.io/part-of: xayma-platform`,
  `app.kubernetes.io/managed-by: ansible`, `app.kubernetes.io/name`
  (`odoo`/`postgres`/`snapshot`/`suspend-backend`), and
  `xayma.sh/{customer,instance,odoo-version}` — teardown and NetworkPolicy
  selectors are entirely label-driven.
- A single **suspend-backend** (busybox httpd) is shared by every instance in
  a customer namespace, serving `suspended.html`/`stopped.html`/`404.html`/
  `50x.html`. Suspending/stopping an instance repoints its IngressRoute at
  this backend (with a `replacePathRegex` Middleware forcing the right page,
  no client-visible redirect); running instances also route their 404/50x
  through it via a Traefik `errors` Middleware, for a styled fallback page.
- **NetworkPolicies are written by this role**, not the platform's
  `deploy-network-policies` (which only iterates its own fixed platform
  namespace list and never sees a customer namespace): default-deny ingress,
  edge → Odoo, `network-zone=observability` (pgAdmin) → Postgres, and
  same-instance-only Odoo/snapshot → Postgres. The namespace itself still
  carries `xayma.sh/network-zone: webservers` so the platform's existing
  `databases ← webservers` rule (a live label selector) lets this
  namespace's snapshot Jobs reach the platform MinIO.

Stateless executor
-------------------
This role ships **no `plans:` dict** and does not look anything up — the
Xayma app is the source of truth for plans and launches the AWX job with a
fully-resolved `plan` object. `tasks/_assert-plan.yml` only asserts the
contract below is met, then templates every value straight through.

There are also no per-instance password fields anywhere in the input
contract: the DB password and Odoo master password are derived
deterministically per instance from a per-role vault seed
(`vault_odoo_db_password`/`vault_odoo_admin_password`) plus the
customer/instance slugs (`vars/main.yml`) — idempotent, nothing generated or
persisted.

Input contract
---------------
Identity vars (also survey-able for manual runs):

| Var            | Description |
|----------------|-------------|
| `customer`     | Customer slug/name (slugified to the namespace name) |
| `instancename` | Instance name (slugified into the k8s object name) |
| `domain`       | Fully-qualified domain the instance is bound to |
| `version`      | Odoo major version, e.g. `"19"` → image `odoo:19.0` |

`plan` object (required for `action=deploy`/`restart`/`snapshot`):

| Key | Description |
|-----|-------------|
| `workers` | Odoo `workers` (0 = threaded; >0 wires the 8072 long-polling port) |
| `db_maxconn` | `db_maxconn` in odoo.conf |
| `mem_soft` / `mem_hard` | `limit_memory_soft` / `limit_memory_hard` (bytes) |
| `max_cron_threads` | `max_cron_threads` in odoo.conf |
| `odoo_resources.{requests,limits}` | Odoo container resources |
| `pg_resources.{requests,limits}` | Postgres container resources |
| `filestore_storage` | Filestore PVC size (e.g. `10Gi`) |
| `pg_storage` | Postgres PVC size |
| `snapshots.daily_enabled` | Whether a daily snapshot CronJob is created |
| `snapshots.daily_schedule` | Cron schedule for the daily snapshot |
| `snapshots.daily_retention` | Max daily snapshots kept (oldest pruned) |
| `snapshots.adhoc_allowed` | Quota: max ad-hoc snapshots that may exist at once |
| `snapshots.adhoc_retention` | Max ad-hoc snapshots kept (oldest pruned) |

`action=restore` additionally requires `snapshot_id` (the timestamp prefix
of the snapshot to restore) and accepts optional `snapshot_kind`
(`daily`|`adhoc`, default `daily`).

Actions
-------
Dispatch is driven by a single `action` extra-var (not `--tags` — AWX
surveys set `extra_vars`, so a single Job Template + survey can drive every
action below). The role fails fast with a clear message if `action` is
missing or not one of these values.

| `action` | Scope | Description |
|-----|-------|-------------|
| `deploy` | instance | Create/update an instance (namespace, Postgres, Odoo, ingress, network policies, snapshot CronJob) |
| `start` | instance | Scale up, repoint the IngressRoute at Odoo |
| `stop` | instance | Scale down, repoint the IngressRoute at the stopped page |
| `suspend` | instance | Scale down, repoint the IngressRoute at the suspended page |
| `restart` | instance | Reapply config/Deployment/StatefulSet, then force pod recreation |
| `edit-domain` | instance | Change the domain, preserving the current running/suspended/stopped state |
| `delete` | instance | Delete every resource labelled for this instance |
| `snapshot` | instance | Trigger an ad-hoc snapshot (subject to `adhoc_allowed`/`adhoc_retention`) |
| `restore` | instance | Restore a snapshot (`snapshot_id`, optional `snapshot_kind`) |
| `start-customer` | customer | Start every instance for the customer |
| `stop-customer` | customer | Stop every instance for the customer |
| `suspend-customer` | customer | Suspend every instance for the customer |
| `delete-customer` | customer | Delete the customer's entire namespace |

CLI examples:

```bash
# Instance-scope action, no plan needed
ansible-playbook site.yml -i production \
  -e action=start -e customer=supermalang -e instancename=laundromat \
  --vault-password-file vault_password -K

# Instance-scope action that consumes the plan object
ansible-playbook site.yml -i production \
  -e action=deploy -e customer=supermalang -e instancename=laundromat \
  -e domain=laundromat.supermalang.com -e version=19 \
  -e '{"plan": { ... }}' \
  --vault-password-file vault_password -K

# Customer-scope action (no instancename/domain/version needed)
ansible-playbook site.yml -i production \
  -e action=suspend-customer -e customer=supermalang \
  --vault-password-file vault_password -K
```

### AWX

Run this role from **one** AWX Job Template rather than one per action:

- Job Template → **Variables**: set to *Prompt on launch*. This lets the
  Xayma app pass `action`, the identity vars (`customer`/`instancename`/
  `domain`/`version`), and — for `deploy`/`restart`/`snapshot` — the nested
  `plan` object, all as `extra_vars` via the AWX API on every launch.
- Add a **survey** for manual/testing runs from the AWX UI, with fields:
  - `action` — multiple choice, one of the values in the table above
  - `customer` — text
  - `instancename` — text
  - `domain` — text
  - `version` — text
  - The `plan` object can't be a flat survey field. For a manual
    `deploy`/`restart`/`snapshot` run, add a `plan` **Textarea** survey field
    and paste in the JSON by hand (or just drive those three actions from the
    Xayma app, which always supplies `plan` itself).

Snapshots
---------
A snapshot is a `pg_dump` of the instance database plus a tar of its
filestore, uploaded to the platform MinIO's `snapshots` bucket, pathed
`snapshots/{customer}/{instance}/{daily|adhoc}/{timestamp}/`. Daily snapshots
run on a per-instance CronJob (only created when `plan.snapshots.daily_enabled`);
ad-hoc snapshots are one-shot Jobs triggered by `action=snapshot`,
which enforce the `adhoc_allowed` quota before dumping anything. Both prune
their own prefix down to the configured retention count after a successful
upload.

Versions
--------
Pinned in `defaults/main/01-deploy-odoo-defaults.yml` (`versions:` dict) —
Odoo `"19"` (`odoo:19.0`, survey fallback only; real deploys pass `version`
explicitly), Postgres `16.6-bookworm` (dedicated per instance), `busybox`
(suspend-backend + config-merge initContainer), `rclone` (snapshot/restore
Jobs). Never `latest`.

Vault
-----
`defaults/main/02-credentials.yml` (encrypted) holds:
`vault_odoo_db_password`, `vault_odoo_admin_password` (per-instance secret
derivation seeds — see `vars/main.yml`), `vault_odoo_minio_access_key`,
`vault_odoo_minio_secret_key` (platform MinIO, snapshots bucket). See
`defaults/main/02-credentials.yml.example` to (re)create it.

Known caveats
-------------
- Re-running `action=deploy` against an already-suspended/stopped instance
  resets it to `running` (the Odoo/Postgres replica count is templated as
  part of the initial-deploy manifests) — use `action=deploy` for
  create/resize while an instance is running; use the dedicated
  `start`/`stop`/`suspend` actions for state changes.
- `action=snapshot`'s quota (`adhoc_allowed`) is a hard ceiling on how many
  ad-hoc snapshots may exist at once, checked before dumping anything;
  `adhoc_retention` is the separate keep-newest-N pruned after a successful
  upload. Set `adhoc_retention <= adhoc_allowed`.
- `action=deploy` does not initialize the base database (no `-i base` job).
  A freshly deployed instance has an empty Postgres with no usable DB until
  initialized out-of-band — this matches the old Docker-based role's
  behavior. `list_db = False` + `dbfilter` in `odoo.conf.j2` mean the
  `/web/database/manager` selector is disabled, so initialization must be
  driven some other way (e.g. a one-off `odoo -i base -d <instance_slug>`
  run, or a restore of an existing snapshot via `action=restore`).

Requirements
------------
- The Xayma.sh Platform (k3s + Traefik + cert-manager + a platform MinIO)
  already deployed — see `install-platform.xayma.sh`.
- `kubernetes.core` and `community.general` Galaxy collections (see
  `requirements.yml`).
- The vault password, as a file (CLI) or a credential record (AWX).

Dependencies
------------
None beyond the platform itself (see Requirements).

License
-------

MIT

Author Information
------------------

- Elhadji Malang Diedhiou
For the past seve years I have been helping businesses to increase efficiency, using automation tools. I am passionate in learning and sharing.
**More about me**:
  * [LinkedIn]
  * [Twitter]
  * [GitHub]

[LinkedIn]: https://linkedin.com/in/supermalang
[GitHub]: https://github.com/supermalang
[Twitter]: https://twitter.com/supermalang_
