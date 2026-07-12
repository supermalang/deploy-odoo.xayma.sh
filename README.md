Deploy-Odoo
=========

This role deploys and manages per-customer Odoo instances as Kubernetes
workloads on the Xayma.sh Platform's single-node **k3s** cluster (the
`install-platform.xayma.sh` repo — Traefik v3 + cert-manager + `kubernetes.core`).
It is intended to run as an AWX job launched by the Xayma app, which passes
identity vars and a fully-resolved set of flat `plan_*` vars as `extra_vars`.
It can also be run from the CLI for manual/testing use (role defaults provide
sane fallbacks for every `plan_*` var).

```bash
ansible-playbook site.yml -i production \
  -e odoo_action=deploy -e customer=supermalang -e instancename=laundromat \
  -e domain=laundromat.supermalang.com -e version=19 \
  -e plan_workers=2 -e plan_db_maxconn=20 \
  -e plan_mem_soft=629145600 -e plan_mem_hard=671088640 \
  -e plan_odoo_mem_limit=1Gi -e plan_pg_mem_limit=512Mi \
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
fully-resolved set of flat `plan_*` extra_vars (see the table below). Every
`plan_*` var has a role default (`defaults/main/01-deploy-odoo-defaults.yml`)
and is templated straight into the rendered manifests — no intermediate
`plan` object, no JSON parsing.

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

`plan_*` vars (consumed by `odoo_action=deploy`/`restart`/`snapshot`; every
one has a role default, so none are strictly required — the Xayma app
overrides them per-instance):

| Var | Default | Description |
|-----|---------|-------------|
| `plan_workers` | `0` | Odoo `workers` (0 = threaded; >0 wires the 8072 long-polling port) |
| `plan_db_maxconn` | `32` | `db_maxconn` in odoo.conf |
| `plan_mem_soft` | `536870912` | `limit_memory_soft` (bytes) |
| `plan_mem_hard` | `1073741824` | `limit_memory_hard` (bytes) |
| `plan_max_cron_threads` | `1` | `max_cron_threads` in odoo.conf |
| `plan_odoo_cpu_request` | `250m` | Odoo container CPU request |
| `plan_odoo_mem_request` | `512Mi` | Odoo container memory request |
| `plan_odoo_cpu_limit` | `1` | Odoo container CPU limit |
| `plan_odoo_mem_limit` | `1Gi` | Odoo container memory limit |
| `plan_pg_cpu_request` | `100m` | Postgres container CPU request |
| `plan_pg_mem_request` | `256Mi` | Postgres container memory request |
| `plan_pg_cpu_limit` | `500m` | Postgres container CPU limit |
| `plan_pg_mem_limit` | `512Mi` | Postgres container memory limit |
| `plan_filestore_storage` | `2Gi` | Filestore PVC size |
| `plan_pg_storage` | `2Gi` | Postgres PVC size |
| `plan_daily_enabled` | `"true"` | Whether a daily snapshot CronJob is created (`"true"`/`"false"`, consumed via `\| bool`) |
| `plan_daily_schedule` | `0 3 * * *` | Cron schedule for the daily snapshot |
| `plan_daily_retention` | `7` | Max daily snapshots kept (oldest pruned) |
| `plan_adhoc_allowed` | `5` | Quota: max ad-hoc snapshots that may exist at once |
| `plan_adhoc_retention` | `3` | Max ad-hoc snapshots kept (oldest pruned) |

`odoo_action=restore` additionally requires `snapshot_id` (the timestamp prefix
of the snapshot to restore) and accepts optional `snapshot_kind`
(`daily`|`adhoc`, default `daily`).

Actions
-------
Dispatch is driven by a single `odoo_action` extra-var (not `--tags` — AWX
surveys set `extra_vars`, so a single Job Template + survey can drive every
action below). The role fails fast with a clear message if `odoo_action` is
missing or not one of these values.

This role is **strictly instance-scoped**: every action operates on exactly
one instance, never more. Customer-level operations (e.g. suspending every
instance for a customer that hasn't paid) are orchestrated by the Xayma app,
which loops this same instance-scoped Job Template over every active
instance belonging to that customer — the role itself carries no
multi-instance action and never will.

| `odoo_action` | Description |
|-----|-------------|
| `deploy` | Create/update an instance (namespace, Postgres, Odoo, ingress, network policies, snapshot CronJob) |
| `start` | Scale up, repoint the IngressRoute at Odoo |
| `stop` | Scale down, repoint the IngressRoute at the stopped page |
| `suspend` | Scale down, repoint the IngressRoute at the suspended page |
| `restart` | Reapply config/Deployment/StatefulSet, then force pod recreation |
| `edit-domain` | Change the domain, preserving the current running/suspended/stopped state |
| `delete` | Delete every resource labelled for this instance; if it was the last instance in the customer namespace, also delete the namespace (and with it the shared suspend-backend/MinIO Secret) |
| `snapshot` | Trigger an ad-hoc snapshot (subject to `adhoc_allowed`/`adhoc_retention`) |
| `restore` | Restore a snapshot (`snapshot_id`, optional `snapshot_kind`) |

CLI examples:

```bash
# Instance-scope action, no plan needed
ansible-playbook site.yml -i production \
  -e odoo_action=start -e customer=supermalang -e instancename=laundromat \
  --vault-password-file vault_password -K

# Instance-scope action that consumes the plan_* vars
ansible-playbook site.yml -i production \
  -e odoo_action=deploy -e customer=supermalang -e instancename=laundromat \
  -e domain=laundromat.supermalang.com -e version=19 \
  -e plan_workers=2 -e plan_db_maxconn=20 \
  --vault-password-file vault_password -K
```

### AWX

Run this role from **one instance-scoped** AWX Job Template — there is no
customer-scope Job Template; the Xayma app loops this one over a customer's
active instances when it needs to act on all of them.

- Job Template → **Variables**: leave empty, set to *Prompt on launch*. This
  lets the Xayma app pass `odoo_action`, the identity vars, and — for
  `deploy`/`restart`/`snapshot` — the flat `plan_*` vars, all as
  `extra_vars` via the AWX API on every launch.
- Add a **survey** for manual/testing runs from the AWX UI, with fields
  (defaults match §1/`defaults/main/01-deploy-odoo-defaults.yml`, so every
  field can be left at its default for a quick manual run):
  - `odoo_action` — Multiple Choice, **required**, Answer Variable Name
    `odoo_action`, one of the 9 values in the table above
  - `instancename` — Text, **required**
  - `customer` — Text, **required**
  - `version` — required, default e.g. `"19"`
  - `domain` — optional; when unset the role derives
    `<instancename>.<platform_domain>` (see `defaults/main/01-deploy-odoo-defaults.yml`)
  - Integer fields, Answer Variable Name matches the var name: `plan_workers`,
    `plan_db_maxconn`, `plan_mem_soft`, `plan_mem_hard`, `plan_max_cron_threads`,
    `plan_daily_retention`, `plan_adhoc_allowed`, `plan_adhoc_retention`
  - Text fields: `plan_odoo_cpu_request`, `plan_odoo_mem_request`,
    `plan_odoo_cpu_limit`, `plan_odoo_mem_limit`, `plan_pg_cpu_request`,
    `plan_pg_mem_request`, `plan_pg_cpu_limit`, `plan_pg_mem_limit`,
    `plan_filestore_storage`, `plan_pg_storage`, `plan_daily_schedule`
  - `plan_daily_enabled` — Multiple Choice (`"true"`/`"false"`), consumed via
    `| bool` since AWX surveys have no boolean type
  - `snapshot_id` / `snapshot_kind` — for `restore`

  The Xayma app sends these same flat `plan_*` keys as `extra_vars` on every
  API launch — no nested `plan` object or JSON parsing on either side.

Snapshots
---------
A snapshot is a `pg_dump` of the instance database plus a tar of its
filestore, uploaded to the platform MinIO's `snapshots` bucket, pathed
`snapshots/{customer}/{instance}/{daily|adhoc}/{timestamp}/`. Daily snapshots
run on a per-instance CronJob (only created when `plan_daily_enabled`);
ad-hoc snapshots are one-shot Jobs triggered by `odoo_action=snapshot`,
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
- Re-running `odoo_action=deploy` against an already-suspended/stopped instance
  resets it to `running` (the Odoo/Postgres replica count is templated as
  part of the initial-deploy manifests) — use `odoo_action=deploy` for
  create/resize while an instance is running; use the dedicated
  `start`/`stop`/`suspend` actions for state changes.
- `odoo_action=snapshot`'s quota (`adhoc_allowed`) is a hard ceiling on how many
  ad-hoc snapshots may exist at once, checked before dumping anything;
  `adhoc_retention` is the separate keep-newest-N pruned after a successful
  upload. Set `adhoc_retention <= adhoc_allowed`.
- `odoo_action=deploy` does not initialize the base database (no `-i base` job).
  A freshly deployed instance has an empty Postgres with no usable DB until
  initialized out-of-band — this matches the old Docker-based role's
  behavior. `list_db = False` + `dbfilter` in `odoo.conf.j2` mean the
  `/web/database/manager` selector is disabled, so initialization must be
  driven some other way (e.g. a one-off `odoo -i base -d <instance_slug>`
  run, or a restore of an existing snapshot via `odoo_action=restore`).

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
