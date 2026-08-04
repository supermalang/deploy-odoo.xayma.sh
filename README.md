Deploy-Odoo
=========

Ansible role that deploys and operates a **shared multi-tenant pool** of
Odoo instances on the Xayma.sh Platform's Kubernetes (k3s) cluster
(`install-platform.xayma.sh` — Traefik v3 + cert-manager + a platform
MinIO). A tenant is never its own workload: it is a Postgres database + a
filestore prefix + a route, served by a shared pool of pods keyed on
`(version, plan)`. See "Architecture" for the full model.

It is designed to run as an AWX Job Template launched by the Xayma app
(`odoo_action` + identity vars + a resolved `plan`/`plan_spec` as
`extra_vars` — see "Input contract"), and equally from the CLI for manual
runs:

```bash
ansible-playbook site.yml -i production \
  -e odoo_action=deploy -e customer=acme -e instancename=laundromat \
  -e custom_domain=laundromat.acme.com -e version=19 -e plan=standard \
  --vault-password-file vault_password -K
```

Requirements
------------
- The Xayma.sh Platform already deployed (`install-platform.xayma.sh`):
  k3s, Traefik v3, cert-manager with a DNS-01-capable ClusterIssuer
  (`letsencrypt-dns01-production` — needed for the wildcard Certificate;
  HTTP-01-only issuers cannot issue a wildcard, full stop), and a platform
  MinIO.
- At least one k3s node labeled to receive tenant workloads (see
  "Configuration" — every pool pod is hard-gated to nodes carrying this
  label; number and size of those nodes is otherwise irrelevant to this
  role).
- Galaxy collections in `requirements.yml` (`kubernetes.core`,
  `community.general`, `community.postgresql`) plus **`psycopg2`/`psycopg`
  installed on the control node/AWX execution environment** —
  `community.postgresql` connects directly to the shared Postgres's
  `ClusterIP`, which is only reachable from inside the cluster's network.
  This role assumes the Ansible control node is co-located with the k3s
  node (consistent with `kubeconfig_path` defaulting to
  `/etc/rancher/k3s/k3s.yaml`); if it isn't, Postgres needs a different
  reachable path before anything here will run.
- A container registry tag with the `redis` Python package installed
  (`session_redis`, the mandatory session backend, imports it — the stock
  Docker Hub `odoo` image doesn't ship it). Push one yourself, or let this
  role build it — see "Custom Odoo image".
- **Only if `odoo_image_build.enabled: true`**: `nerdctl-full` (the full
  release bundle, not the bare binary — it needs to include `buildkitd`)
  on the host that runs this playbook.
- The vault password, as a file (CLI) or a credential record (AWX).

Configuration
--------------
1. **Vault secrets** — `defaults/main/02-credentials.yml` (encrypted).
   Copy `defaults/main/02-credentials.yml.example` and fill in every
   `CHANGE_ME`, then `ansible-vault encrypt` it. See "Vault" for what each
   seed derives.
2. **Node label for tenant workloads** — every pool pod's `nodeAffinity`
   requires the label `odoo.tenants_node_label` resolves to (default
   `xayma.sh/node-role=tenants`):
   ```
   kubectl label node <node-name> xayma.sh/node-role=tenants
   ```
   Label as many nodes as you want to host tenant workloads — this role
   doesn't care how many there are or how big they are, it only schedules
   onto whichever ones carry the label. **No labeled node = every pool pod
   stays `Pending`** and the deploy times out (see "Actions" /
   `_resolve-pool.yml`'s readiness wait) — label at least one node before
   the first `deploy`.
3. **Addons repo content** — `odoo.addons_repo_url` (a private git repo,
   auth via the `addons_deploy_key` vault secret) must contain, at
   `odoo.addons_repo_ref`, every module any tenant's `init_modules` needs
   beyond Odoo's own bundled modules — including `session_redis` itself,
   which is mandatory. `_assert-init-modules.yml` fails fast on a missing
   module rather than deep inside a batch-low Job. (`fs_storage`/
   `fs_attachment`/`fs_attachment_s3` come from OCA's own public repo
   automatically — nothing to push for those.)
4. **Odoo image with `redis` installed** — see "Custom Odoo image".
5. **A Job Template pointing at this playbook is mandatory** — the Xayma
   app's only launch target is one AWX Job Template running `site.yml`
   with `Ask variables on launch: ON` and a survey matching "Input
   contract" below. **`Allow simultaneous` must stay OFF** — see
   "Concurrency" for why this is load-bearing, not a default left alone.
   Nothing else about AWX is this role's concern (no Project/Credential/
   Notification setup is automated here).

How it works
-------------
Every action is one AWX Job Template launch (or one `ansible-playbook`
run) with `odoo_action` plus the relevant vars from "Input contract" as
`extra_vars`. Dispatch happens entirely inside `roles/deploy-odoo/tasks/main.yml`
(a `when: odoo_action == '...'` chain) — there are no `--tags`. Every
action but `apply-plan` (pool-scoped) and `check-snapshot-freshness`
(namespace-scoped) operates on exactly one tenant; looping over many
tenants (e.g. suspending every tenant of a non-paying customer, or nightly
per-tenant backups) is the calling app's job, not this role's.

Architecture
------------
- **ONE namespace, `xayma-odoo`, for the whole tier.** Every pool, every
  tenant, and every platform singleton lives here.
- **A tenant = a Postgres database + a filestore prefix + a route.**
  Nothing else — no dedicated workload. `customer`/`instancename` are
  slugified to RFC-1123; `instancename` stays globally unique (guaranteed
  by the calling app) and doubles as the Postgres database name.
- **Compute is organized in pools keyed by `(version, plan)`.**
  `pool_id = odoo{version}-{plan}` (e.g. `odoo19-standard`). Every tenant
  on the same `(version, plan)` shares the same pool — deploying onto an
  existing pool never creates new compute, only a database + route.
- **Each pool = 3 Deployments + 1 ConfigMap + 1 Postgres role:**
  - `{pool}-http` — prefork (`workers >= 2`), `db_host=pgbouncer`, Service
    on `:8069`. The only autoscaled Deployment (HPA on CPU, target 65%,
    bounds from `plan_spec.replicas_min/max`; scale up at most 1 pod/60s,
    scale down only after 300s of headroom).
  - `{pool}-cron` — exactly 1 replica, never scaled, `workers=0`,
    `db_host=`Postgres directly. No Service.
  - `{pool}-gevent` — 1 replica, Service on `:8072`, `db_host=`Postgres
    directly (LISTEN/NOTIFY is incompatible with PgBouncer's transaction
    pooling). Handles `/websocket/*` only.
- **No PVCs anywhere in the pool.** Community/custom addons are cloned
  fresh into an `emptyDir` by a `clone-addons` initContainer on every pod/
  Job (auth: the `addons-deploy-key` Secret); OCA's `fs_storage`/
  `fs_attachment`/`fs_attachment_s3` come from a second, public
  `clone-oca-storage` initContainer, pinned to a commit SHA
  (`odoo.oca_storage_repo_ref`). `ir.attachment` storage itself goes
  straight to the platform MinIO's `odoo-filestore` bucket, one prefix per
  tenant database, via one `fs.storage` record `job-fixup.yaml.j2` upserts
  idempotently. This statelessness is what makes the hard node-affinity
  gate in "Configuration" safe — there's no local data to strand.
- **Both clones are a shallow `git fetch --depth 1` of exactly the pinned
  ref** (works for a branch name or a literal SHA — `git clone --branch`
  can't take a SHA, so this form is used for both instead of two
  different code paths), not a full clone — cheap on every pod start/HPA
  scale-up, but it does mean **pod start now depends on GitHub being
  reachable and the deploy key still being valid**: a GitHub outage, a
  rate-limit, or a revoked key means no new pool pod can start anywhere in
  the cluster. Baking addons into the custom image this role already
  builds (see "Custom Odoo image") removes that dependency entirely, if it
  ever becomes a problem.
- **ONE shared Postgres** (StatefulSet, one PVC, superuser from a vault
  seed) for the entire tier. **PgBouncer** (transaction mode) sits in
  front of it for `{pool}-http` only — see "PgBouncer & per-tenant
  fairness". **Redis** (mandatory session backend, no filesystem
  fallback) via the `session_redis` addon, configured entirely through
  `ODOO_SESSION_REDIS_*` environment variables.
- **One shared suspend/stop backend** for the whole namespace
  (`suspended.html`/`stopped.html`/error pages), attached to every
  tenant's IngressRoute via three Traefik Middlewares created once at
  bootstrap.
- **Per-tenant rate limiting** — each tenant gets its own
  `{tenant}-ratelimit` Middleware, sized from `tenant_spec.ratelimit`.
- **TLS**: one wildcard Certificate (`*.{platform_domain}`, DNS-01) covers
  every default host. A per-tenant Certificate (HTTP-01) is only created
  for `custom_domain`, gated by a pre-flight DNS check.
- **NetworkPolicies** written once at bootstrap, selector-driven
  (`xayma.sh/pool-role`, `xayma.sh/job-role`) so they never need a
  per-pool/per-tenant copy: default-deny; edge → `{pool}-http`/
  `{pool}-gevent`; `{pool}-http` + Jobs → PgBouncer; PgBouncer +
  `{pool}-cron`/`{pool}-gevent` + Jobs + tooling → Postgres directly; pool
  pods → Redis; edge → suspend-backend + cert-manager's ACME solver.
- **PriorityClass `batch-low`** (negative, `preemptionPolicy: Never`) on
  every Job this role creates, plus a namespace ResourceQuota as a
  guardrail against Job pileups.
- **Observability**: `postgres-exporter` next to the shared Postgres — see
  "Observability".

Every resource carries `app.kubernetes.io/part-of: xayma-platform`,
`app.kubernetes.io/managed-by: ansible`, `app.kubernetes.io/name`
(`odoo`/`postgres`/`pgbouncer`/`redis`/`postgres-exporter`/
`suspend-backend`), plus pool labels (`xayma.sh/pool-id`,
`xayma.sh/odoo-version`, `xayma.sh/plan`, `xayma.sh/pool-role`) and tenant
labels (`xayma.sh/customer`, `xayma.sh/instance`, and — on the tenant's
IngressRoute only — `xayma.sh/state`, the durable record every lifecycle
action reads back instead of taking state as input; see
`tasks/_recover-tenant.yml`). Job pods additionally carry
`xayma.sh/job-role` (`init`|`fixup`|`restore`|`backup`|`fileops`).

Input contract
---------------
| Var | Used by | Description |
|-----|---------|-------------|
| `odoo_action` | every call | One of the actions in the table below. Missing/unrecognized fails fast. |
| `customer` | most actions | Customer slug (label only — not a namespace) |
| `instancename` | most actions | Slugified into the tenant's object names and its Postgres database name; globally unique |
| `custom_domain` | `deploy`, `edit-domain` | Optional extra domain, in addition to `<instance_slug>.<platform_domain>` |
| `version` | `deploy`, `change-plan`, `apply-plan` | Odoo major version, e.g. `"19"` → image `{odoo_image_repo}:19.0`. Every other action recovers it from live state |
| `plan` | `deploy`, `change-plan`, `apply-plan` | Plan **name** (RFC-1123 — it lands in `pool_id` and PG role names). See "Plan resolution" |
| `plan_spec` | optional | Full nested spec dict, schema below. Omit to use the catalog/hard-fallback |
| `plan_spec_json` | optional | JSON-string form of `plan_spec`, for surveys/manual runs that can't send a nested value |
| `snapshot_id` | `restore` | Required — the timestamp prefix of the snapshot to restore |
| `snapshot_kind` | `restore`, `backup` | `daily`\|`adhoc`, default `daily` |

### Plan resolution

The calling app is the source of truth: a real launch sends `plan` **and**
`plan_spec`. This role also keeps a small fallback catalog
(`defaults/main/00-plans.yml` — `standard`/`premium`) consulted only when
`plan_spec` is absent; a `plan` name matching neither the app's spec nor
the catalog falls through to `plan_spec_hard_fallback`
(`defaults/main/01-deploy-odoo-defaults.yml`) — deliberately tiny, a
misconfiguration safety net, never a real tier. `plan` defaults to
`"standard"` if omitted entirely (`xayma_odoo.plan`).

Resolved fresh on every call by `tasks/_resolve-plan-spec.yml`:

```
plan_spec (app-supplied, or plan_spec_json for manual runs)
  > plans[plan]   (defaults/main/00-plans.yml catalog)
  > plan_spec_hard_fallback
```

`plan_spec` schema:

| Key | Scope | Meaning |
|-----|-------|---------|
| `workers` | pool | `{pool}-http`'s Odoo `workers` (must be `>=2`) |
| `replicas_min` / `replicas_max` | pool | HPA bounds for `{pool}-http` |
| `pod.cpu_request` / `cpu_limit` / `mem_request` / `mem_limit` | pool | Applied to all 3 Deployments in the pool |
| `limits.mem_soft` / `mem_hard` | pool | odoo.conf `limit_memory_soft`/`hard` (bytes) — kept below `pod.mem_limit` so Odoo self-recycles before Kubernetes OOMKills |
| `limits.time_cpu` / `time_real` / `request` | pool | odoo.conf `limit_time_cpu`/`limit_time_real`/`limit_request` |
| `cron.threads` | pool | `{pool}-cron`'s `max_cron_threads` |
| `db_maxconn` | pool | odoo.conf `db_maxconn` — worker-side connection demand (see below, don't confuse with the tenant cap) |
| `init_modules` | tenant | Comma-separated `-i` module list for this tenant's one-shot init Job |
| `tenant_db_max_connections` | tenant | PgBouncer's per-tenant-database cap — the real per-tenant fairness lever |
| `ratelimit.avg` / `ratelimit.burst` | tenant | This tenant's `{tenant}-ratelimit` Middleware |

**Pool-scoped keys are create-only.** `{pool}-http` carries an annotation
(`xayma.sh/plan-spec-hash`) recording the pool-scoped spec it was built
with. A later `deploy`/`change-plan` whose resolved pool-scoped spec
hashes differently is **silently ignored** for sizing (a warning is
logged) — one tenant's call can never resize a pool out from under every
other tenant on it. Use `odoo_action=apply-plan` (`version`+`plan`+
`plan_spec`, no tenant identity) to actually resize an existing pool —
this rolls **every** tenant currently on it, by design, no partial apply.

**Tenant-scoped keys are always honored** — every `deploy`/`change-plan`
re-applies them for that one tenant.

`db_maxconn` (pool-scoped) is how many PG connections *one worker
process* may open — sized for throughput. `tenant_db_max_connections`
(tenant-scoped) is how many connections *one tenant's database* may ever
hold through PgBouncer at once — the actual fairness lever protecting
every other tenant on the pool. Raising `db_maxconn` does not fix a
starved tenant; `change-plan` onto a higher `tenant_db_max_connections`
does.

Actions
-------
| `odoo_action` | Scope | Description |
|-----|-------|-------------|
| `deploy` | tenant | Create/update: bootstrap the platform + pool if needed, create the DB (idempotent — never re-inits), run init/fixup Jobs, render routing |
| `start` | tenant | Grant the tenant's DB access back, repoint its route at its pool |
| `stop` | tenant | Revoke DB access, terminate backends, purge Redis sessions, repoint at the stopped page |
| `suspend` | tenant | Same mechanics as `stop`, different page/state |
| `restart` | tenant | Terminates DB backends + purges Redis sessions — there is no per-tenant process to restart |
| `edit-domain` | tenant | Change the custom domain, preserving current state |
| `change-plan` | tenant | Move a tenant to a different plan, same Odoo version only. No data movement |
| `apply-plan` | **pool** | Resize an existing pool's frozen sizing — affects every tenant on it |
| `change-version` | tenant | Stub — fails fast, see "Non-goals" |
| `delete` | tenant | Adhoc backup, drop the DB, purge the filestore prefix + PgBouncer entry, delete every labelled object; scales the pool to 0 if it was the last tenant |
| `restore` | tenant | Restore a snapshot (DB only), fenced via REVOKE/terminate |
| `backup` | tenant | On-demand `pg_dump` → MinIO (DB only) |
| `check-snapshot-freshness` | **namespace** | Fails unless every live, non-stopped tenant has a `daily` snapshot newer than `snapshot_freshness.max_age_hours` — the backstop for a tenant whose `backup` silently stops being invoked |

Nightly `backup` iteration across tenants is the calling app's own
scheduler (one launch per active tenant) — AWX `Schedule` objects bind to
one Job Template with fixed `extra_vars`, so they can't loop over
tenants. `check-snapshot-freshness` takes no tenant identity, so it's the
one action that genuinely fits a Schedule directly.

### Concurrency

Every action funnels through the same Job Template with
`Allow simultaneous: OFF` (see "Configuration"). This is standing in for
real per-tenant/per-pool locking that doesn't otherwise exist: several
task sequences read live cluster state, decide something, then write it
back with no lock of their own —
`_sync-pgbouncer-tenants.yml` fully rebuilds the tenant ConfigMap from
live IngressRoute state every call; `_resolve-pool.yml`'s create-only
bootstrap checks "does this pool exist" then creates it; `change-plan`/
`delete` count a pool's remaining tenants before deciding to scale it to
0. Two of these running at the same instant can race silently. Don't
enable simultaneous execution (or split into multiple queues) without
adding real locking to `roles/deploy-odoo` first.

PostgreSQL access model
-------------------------
Ansible connects **directly** to the shared Postgres via
`community.postgresql` (not `kubectl exec`) — see "Requirements" for the
co-located-execution assumption this needs. `vars/main.yml`/
`tasks/_pg-connection.yml` resolve the connection host; the shared
Postgres Service is a real `ClusterIP` (not headless) specifically so this
works without relying on in-cluster DNS from the control node.

- **One LOGIN role per pool**: `pool_{version}_{plan}`, password derived
  from a vault seed keyed on `pool_id`.
- **Once, for the whole tier**: `pgadmin_ro`/`backup_ro`
  (`pg_read_all_data`) and `pgbouncer_auth`/`pg_exporter`
  (least-privilege).
- **Per tenant database**: `OWNER` = its pool role; `REVOKE CONNECT FROM
  PUBLIC`; `GRANT CONNECT` to the pool role + `pgadmin_ro` + `backup_ro`.
  Admin/backup grants survive suspension — only the pool role's grant is
  toggled by `suspend`/`stop`/`start`.

PgBouncer & per-tenant fairness
--------------------------------
`{pool}-http` talks to PgBouncer (transaction pooling); `{pool}-cron`,
`{pool}-gevent`, and every Job talk to Postgres directly. PgBouncer
authenticates client connections dynamically via `auth_query` against a
`SECURITY DEFINER` wrapper over `pg_shadow` — no static userlist edits
when pool roles are created/rotated.

PgBouncer's `[databases]` section has no native way to vary a per-database
cap under one wildcard entry, so each tenant's `tenant_db_max_connections`
is materialized as its own explicit `[databases]` line in a ConfigMap,
fully regenerated from every live tenant's route on every
`deploy`/`change-plan`/`delete` (`_sync-pgbouncer-tenants.yml`) — a full
desired-state replace, never an incremental patch, so concurrent tenant
operations can't corrupt each other's entry. PgBouncer is reloaded via
`SIGHUP` afterwards. The ConfigMap volume isn't `subPath`-mounted, so
kubelet's normal ~60s sync delay applies before a new cap actually takes
effect (see "Known caveats").

Capacity planning
-------------------
Pool pods are hard-gated to labeled nodes (see "Configuration") — if a
labeled node's real allocatable resources are already spoken for, new
pods stay `Pending` rather than falling back elsewhere. Use the numbers
below to decide how many pools/tenants to route at a given node size, and
sanity-check with `kubectl describe node <name>`'s "Allocated resources"
(total *requests*, the scheduler-relevant number — not `kubectl top`,
which is actual usage).

### Fixed resource requirements

Platform singletons, by `mem_request`:

| Singleton | mem_request |
|---|---|
| Postgres | 1Gi |
| PgBouncer | 64Mi |
| Redis | 64Mi |
| **Subtotal** | **~1.13Gi** |

Per-pool footprint (`{pool}-http` × `replicas_min` + `{pool}-cron` ×1 +
`{pool}-gevent` ×1 — see `defaults/main/00-plans.yml`), at steady state
vs. worst-case HPA scale-out:

| Plan | mem_request/pod | At `replicas_min` | At `replicas_max` |
|---|---|---|---|
| `standard` (default plan) | 512Mi | 3 × 512Mi = 1.5Gi | 6 × 512Mi = 3Gi |
| `premium` | 1Gi | 4 × 1Gi = 4Gi | 8 × 1Gi = **8Gi** |

`standard` is the default plan (`xayma_odoo.plan`) and the smallest real
catalog entry. `plan_spec_hard_fallback` is smaller still (384Mi/pod,
`replicas_max: 2`), but it's a misconfiguration safety net, not a normal
sizing target.

### By node size

Applying a consistent ~75%-usable-for-Odoo policy (the remaining ~25% for
kubelet/container runtime/OS/CNI/DaemonSets — deliberately conservative;
tighten it later with real `kubectl top node` data if you want the
margin back), and after the ~1.13Gi singleton subtotal, here's how many
`standard`-vs-`premium` pools (at `replicas_min`) fit per node size:

| Node RAM | Usable ceiling (~75%) | Remaining after singletons | `standard` pools (1.5Gi ea.) | `premium` pools (4Gi ea.) |
|---|---|---|---|---|
| 8Gi | 6Gi | ~4.87Gi | 3 | 1 |
| 15Gi | ~11.25Gi | ~10.12Gi | 6 | 2 |
| 32Gi | 24Gi | ~22.87Gi | 15 | 5 |
| 64Gi | 48Gi | ~46.87Gi | 31 | 11 |

A single `premium` pool fully scaled to `replicas_max` alone needs 8Gi —
an entire 8Gi node's capacity, singletons included; prefer routing one
pool tier at a time there. Recompute using the `replicas_max` figures
above for the worst-case (not steady-state) number on larger sizes.

### Tenants per pool — not a memory question

Multiple tenants share the *same* pool pods (routed by `dbfilter` on the
Host header) — a pool's memory footprint is fixed regardless of tenant
count, so there's no clean "N tenants fit in X memory" formula. What
actually gates tenant count on one pool:

- **Shared Postgres connections** — `postgres.max_connections` (200,
  shared across every pool) vs. each tenant's own
  `tenant_db_max_connections` cap (10 `standard` / 20 `premium`). 200 ÷ 10
  = 20 is a worst-case ceiling if every `standard` tenant simultaneously
  saturated its own cap at once — pessimistic, not realistic.
  `tenant_db_max_connections` caps how many connections *one busy tenant*
  may hold, not a reservation held by every provisioned tenant — PgBouncer
  multiplexes many tenants onto far fewer real backend connections. In
  practice you can provision well beyond 20 tenants on one pool as long
  as they're not all saturating their caps simultaneously.
- **Real aggregate CPU/memory load** on the pool's pods — HPA scales
  `{pool}-http` up to `replicas_max` as traffic grows; beyond that,
  requests queue/slow down, not more pods.
- **Shared Postgres's own CPU/memory** under aggregate query load across
  every tenant database on it.

There's no static number to plan around here — it's a load-testing
question. Watch Postgres's own CPU/memory and `{pool}-http`'s p95 worker
occupation under real traffic (see "Observability") to decide when to
`apply-plan`/split onto a second pool.

Observability
--------------
`postgres-exporter` runs next to the shared Postgres
(`prometheus.io/scrape: "true"`); Traefik's own router metrics are
exposed by the platform already. Configure at least these three alerts
externally (dashboards are out of scope here):

1. **Pool worker occupation / CPU p95** per pool — the earliest signal
   that a pool needs `apply-plan` before tenants on it feel it.
2. **Postgres disk usage** — one shared disk now serves every tenant.
3. **Per-database size growth** (`pg_database_size` per tenant db) — the
   earliest signal of one tenant needing a `change-plan` or a cleanup
   conversation.

Backup failures and snapshot staleness are surfaced through the AWX Job
Template's own Notification Template instead: a `backup` Job failing
triggers it directly, and `check-snapshot-freshness` catches the
otherwise-silent case of a tenant whose `backup` stops being invoked at
all (no Job ever runs, so no failure notification fires either).

Backups & Restore
-------------------
Snapshots land in the platform MinIO's `snapshots` bucket, pathed
`snapshots/{customer}/{instance}/{daily|adhoc}/{timestamp}/`, under the
scoped `xayma.snapshots` MinIO user (Get/Put/Delete + list the
`snapshots` bucket only). `backup`/`restore` are **DB-only** —
`pg_dump` and its inverse, fenced by REVOKE/GRANT around the pool role.
Filestore is deliberately not part of either: it lives in the separate,
always-live `odoo-filestore` bucket — restoring an old `pg_dump` can
leave DB rows referencing attachments since changed/deleted there, with
no point-in-time filestore rollback. `check-snapshot-freshness` (default
30h) independently verifies every live, non-stopped tenant actually has a
recent DB snapshot.

**Filestore currently has no backup of any kind, and this platform's
MinIO cannot provide one on its own.** "No point-in-time rollback" (above)
undersells the real exposure: `install-platform.xayma.sh`'s `deploy-minio`
is `mode: standalone`, `replicas: 1`, with **no bucket versioning and no
replication configured anywhere**. A user-deleted attachment or a bad
prefix delete (e.g. a `delete` action's `job-purge-filestore.yaml.j2`
firing against the wrong prefix) is **unrecoverable** — not "can't roll
back to a point in time", but "gone, permanently, with nothing to restore
from". Enabling bucket versioning on `odoo-filestore` is a platform-side
change (`install-platform.xayma.sh`'s `deploy-minio` role) — out of scope
for this repo, but load-bearing for the claim above to become true again.

### Migrating attachments off the old PVC

Before this repo moved `ir.attachment` storage to S3, it lived on a
per-tenant `odoo-filestore` PVC (`pvcs.yaml.j2`, since deleted). As of
this writing no tenant has ever been deployed against that PVC-based
model on this platform — this migration question is currently moot here,
and this note exists so it doesn't have to be re-investigated later. If
that ever stops being true (e.g. this repo is reused against a cluster
that still has tenants from before the S3 migration), the old
`odoo-filestore`/`odoo-addons` PVCs are orphaned, not removed — their
bytes still exist, but nothing moves them into S3 and nothing points at
them anymore. The one-off fix, per tenant, using the primitive OCA/storage
already ships (`ir.attachment.force_storage()` — see
`fs_storage_upsert.py.j2`'s and `files/force_storage.py`'s header
comments):

1. Deploy the tenant once under the current model (`odoo_action=deploy`)
   so its `fs.storage` record exists and is the default.
2. Mount the tenant's **old** `odoo-filestore` PVC (read-only) into a
   throwaway pod alongside the current `etc-odoo`/`addons` setup, at the
   same path Odoo's local filestore used to live at (`/var/lib/odoo`).
3. `odoo shell -c ... -d <db> --no-http`, then `env['ir.attachment'].force_storage()`
   + `env.cr.commit()` — it walks every attachment still pointing at local
   storage and copies it onto the configured default (S3) storage.
4. Confirm (spot-check attachments load correctly), then delete the
   orphaned `odoo-filestore`/`odoo-addons` PVCs.

Deliberately a manual, one-off runbook — not an automated `odoo_action`.
It only ever needs to run once per tenant, ever, and only for tenants that
predate this repo's S3 migration.

Known caveats
-------------
- **`restart` semantics.** There is no per-tenant process anymore —
  `restart` only terminates that tenant's DB backends and purges its
  Redis sessions (forcing a fresh login). It does not restart any pod and
  does not affect any other tenant sharing the pool.
- **Registry/import warm-up on first request per pod.** A freshly-scaled
  or freshly-restarted `{pool}-http` pod pays Odoo's module-registry load
  cost on its first request — with many tenants sharing one pool, this is
  a bigger one-time cost than a dedicated-instance model's, but it only
  happens on pod start, not per-tenant.
- **PgBouncer log noise on suspend/stop.** Terminating backends mid-request
  produces normal-but-noisy disconnect log lines — harmless, don't alert
  on it.
- **PgBouncer tenant-cap propagation lag.** ~60s kubelet sync delay
  between `_sync-pgbouncer-tenants.yml` writing a new cap and the
  post-`SIGHUP` reload actually reflecting it.
- **`session_redis`'s one real gap.** The stock Docker Hub `odoo` image
  doesn't include the `redis` Python package the addon imports — without
  it, a tenant's first session access on any pool pod throws, not at pod
  startup. See "Custom Odoo image".
- **`role_path`/`playbook_dir` are control-node paths, always.** This role
  runs over SSH against the managed node — a task that shells out to
  something running ON that node (e.g. `_ensure-odoo-image.yml`'s
  `nerdctl build`) cannot hand it a `{{ role_path }}/...` path, which only
  exists on the control node. Fixed by `copy`-ing the build context to the
  managed node first. Any new task needing a control-node-side file on the
  managed node needs the same treatment.
- **Wildcard Certificate needs a DNS-01 ClusterIssuer.** If
  `wildcard_cluster_issuer` is ever repointed at an HTTP-01-only issuer,
  the Certificate object applies fine but cert-manager never actually
  issues it — an ACME protocol limitation, not a config choice.

Non-goals
---------
Deliberately not implemented: email/SMTP (outbound and inbound); a
dedicated-slice tier; `change-version` migration (stub only); point-in-time
filestore recovery; per-tenant branded suspend pages; per-tenant Schedules
for backup iteration (app-driven instead — see "Actions").

Custom Odoo image
--------------------
`session_redis` needs the `redis` Python package the stock `odoo` image
doesn't ship. `tasks/_ensure-odoo-image.yml` (called on every
`deploy`/`change-plan`/`apply-plan`) handles this:

1. Checks whether `{{ odoo_image_repo }}:{{ version }}.0` already exists
   in the registry via its REST API directly (a local image cache can't
   tell you whether a tag was ever actually pushed). Anonymous for public
   repos; authenticates with `vault_odoo_dockerhub_username`/
   `vault_odoo_dockerhub_token` (an access token, not your password) for
   private ones.
2. If missing and `odoo_image_build.enabled: true` (default `false`):
   stages `files/odoo-image/` on the managed node
   (`/tmp/xayma-odoo-image-build/` — see "Known caveats" on why), then
   builds (`FROM odoo:{version}.0` + `pip install redis fsspec[s3]
   python_slugify`) and pushes via `nerdctl` against k3s's own embedded
   containerd — needs the **`nerdctl-full`** release specifically (the
   bare binary doesn't bundle `buildkitd`).
3. If missing and `odoo_image_build.enabled: false`: fails fast with a
   clear message, rather than every pod in a brand-new pool crash-looping
   on a missing image.

To activate: install `nerdctl-full` on the k3s node, set
`odoo_image_build.enabled: true`, point `odoo_image_repo` at your registry
namespace, and add the two `vault_odoo_dockerhub_*` seeds. Otherwise, push
the tag yourself (`docker build`/`nerdctl build` +
`files/odoo-image/Dockerfile`) and leave `odoo_image_build.enabled` at
`false` — the existence check still runs, it just never needs to build
anything once the tag is there.

Versions
--------
Pinned in `defaults/main/01-deploy-odoo-defaults.yml` (`versions:` dict) —
Odoo `"19"` (survey fallback only; real deploys pass `version`
explicitly), Postgres `16.6-bookworm`, PgBouncer `1.23.1-p2`
(`edoburu/pgbouncer`), Redis `7.4.1-alpine3.20`, postgres-exporter
`v0.16.0`, `busybox` (suspend-backend + initContainer config merges),
`rclone` (backup/restore/purge-filestore Jobs), `git` (`clone-addons`/
`clone-oca-storage` initContainers). Never `latest`.
`odoo.oca_storage_repo_ref` is pinned the same way but lives under
`odoo:`, since it's a commit SHA, not a container tag.

Vault
-----
`defaults/main/02-credentials.yml` (encrypted; see
`defaults/main/02-credentials.yml.example` to (re)create it):

| Secret | Derives / is |
|---|---|
| `vault_odoo_admin_password` | Per-tenant admin password seed — `hash('sha256', seed \| customer \| instance)` |
| `vault_odoo_db_password` | Per-pool Postgres role password seed, keyed on `pool_id` |
| `vault_odoo_platform_password` | One seed for every fixed-platform-role credential (Postgres superuser, `pgadmin_ro`/`backup_ro`, PgBouncer's `auth_query` role, `pg_exporter`, Redis's `requirepass`) — each `hash('sha256', seed \| role_name)` |
| `vault_odoo_snapshot_secret_key` | Platform MinIO's scoped `xayma.snapshots` user's secret (Get/Put/Delete + list the `snapshots` bucket only) — copy from `install-platform.xayma.sh`'s vault; this repo does not create that user/bucket |
| `vault_odoo_filestore_secret_key` | Platform MinIO's scoped `xayma.filestore` user's secret (Get/Put/Delete + list the `odoo-filestore` bucket only) — a different credential from the snapshot one by design (live traffic and backup/restore never share a credential); this repo does not create that user/bucket either |
| `vault_odoo_addons_deploy_key` | Read-only GitHub deploy key for `odoo.addons_repo_url` (`ssh-keygen -t ed25519 -f addons_deploy_key -N ""`, public half added to the repo's deploy keys) |
| `vault_odoo_dockerhub_username` / `_token` | Optional — only if `odoo_image_build.enabled: true` |

Rotating any of the three password seeds rotates every credential it
derives on that credential's next reconciling `odoo_action` run — nothing
is persisted per-tenant/per-pool beyond the seed itself.

License
-------
MIT

Author Information
------------------
- Elhadji Malang Diedhiou
For the past seven years I have been helping businesses to increase efficiency, using automation tools. I am passionate in learning and sharing.
**More about me**:
  * [LinkedIn]
  * [Twitter]
  * [GitHub]

[LinkedIn]: https://linkedin.com/in/supermalang
[GitHub]: https://github.com/supermalang
[Twitter]: https://twitter.com/supermalang_
