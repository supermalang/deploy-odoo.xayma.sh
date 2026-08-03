Deploy-Odoo
=========

This role deploys and manages a **shared multi-tenant pool** of Odoo
instances as Kubernetes workloads on the Xayma.sh Platform's single-node
**k3s** cluster (the `install-platform.xayma.sh` repo — Traefik v3 +
cert-manager + `kubernetes.core`). It is intended to run as an AWX job
launched by the Xayma app (via the `odoo-tenant` Job Template — see "AWX"
below), which passes identity vars plus a resolved `plan`+`plan_spec` as
`extra_vars`. It can also be run from the CLI for manual/testing use (role
defaults provide sane fallbacks — see "Plan resolution").

```bash
ansible-playbook site.yml -i production \
  -e odoo_action=deploy -e customer=supermalang -e instancename=laundromat \
  -e custom_domain=laundromat.supermalang.com -e version=19 -e plan=standard \
  --vault-password-file vault_password -K
```

This is a **ground-up rewrite** of the previous per-instance "slice" model
(one Deployment + one dedicated Postgres StatefulSet per instance, one
namespace per customer). See "Migrating from the slice model" near the end
of this file for the full contract diff if you're updating a calling app
that still speaks the old flat `plan_*` extra_vars.

Architecture
------------
- **ONE namespace, `xayma-odoo`, for the whole tier.** No more per-customer
  namespaces — every pool, every tenant, and every platform singleton
  (Postgres, PgBouncer, Redis, suspend-backend, etc.) lives in this one
  namespace.
- **A tenant is not a workload.** A tenant is a Postgres database + a
  filestore directory + one IngressRoute (+ a Certificate only when
  `custom_domain` is set). Nothing else. Tenant identity (`customer`/
  `instancename`) is still slugified to RFC-1123 exactly as before;
  `instancename` stays globally unique (guaranteed by the Xayma app) and
  doubles as the Postgres database name.
- **Compute is organized in POOLS keyed by `(version, plan)`.**
  `pool_id = odoo{version}-{plan}` (e.g. `odoo19-standard`). Several
  versions may share a plan name and vice versa. Every tenant of the same
  `(version, plan)` is served by the same pool — deploying a tenant onto an
  already-existing pool never creates new compute, it just adds a database
  + routing entry.
- **Each pool = 3 Deployments + 1 ConfigMap + 1 PG role:**
  - `{pool}-http` — prefork mode (`workers >= 2`), `max_cron_threads=0`,
    `db_host=pgbouncer`, Service on `:8069`. The **only** autoscaled
    Deployment (see "Autoscaling").
  - `{pool}-cron` — exactly 1 replica, **never** scaled, `workers=0`, its
    own `max_cron_threads`, `db_host=`the shared Postgres **directly**. No
    Service — nothing routes traffic here.
  - `{pool}-gevent` — 1 replica, Service on `:8072`, `db_host=`Postgres
    directly (LISTEN/NOTIFY, which the websocket/long-polling worker
    depends on, is incompatible with PgBouncer's transaction pooling). Runs
    via `ODOO_GEVENT_PORT=8072`, the standard technique for splitting the
    long-polling worker into its own pod.
- **ONE shared Postgres** (StatefulSet `postgres:16.6-bookworm`, one PVC,
  superuser Secret from a vault seed) for the entire tier. Every DB
  reference is templated through `pg_target` (`vars/main.yml`), resolved
  from `pg_topology` (`shared` | `per_version` | `per_pool`, default
  `shared`) — **only `shared` is implemented today**; the other two names
  are reserved so a future topology change never touches task logic, only
  that one expression. `_assert-plan.yml` fails fast on anything else.
- **PgBouncer** (transaction mode) in front of Postgres for `{pool}-http`
  only. See "PgBouncer & per-tenant fairness" below — this is the one piece
  of the architecture with real subtlety.
- **Redis** (mandatory Odoo session backend, no filesystem fallback) via the
  community `session_redis` addon (Camptocamp `odoo-cloud-platform`, 19.0
  branch), loaded via `server_wide_modules` on every pool pod and
  configured entirely through `ODOO_SESSION_REDIS_*` environment variables
  (verified against the addon's actual source — see "Known caveats" for the
  one real gap this leaves: the stock Odoo image doesn't ship the `redis`
  Python package the addon needs).
- **One shared suspend-backend** (busybox httpd) for the whole namespace,
  serving `suspended.html`/`stopped.html`/`404.html`/`50x.html`. Three
  Middlewares created once at bootstrap: `mw-suspended`
  (`replacePathRegex` → `/suspended.html`), `mw-stopped` (→
  `/stopped.html`), and `mw-errors` — realized as **two** k8s objects
  (`mw-errors-404`, `mw-errors-50x`) since Traefik's `errors` middleware
  takes one status range + one static page per object; every tenant's
  IngressRoute attaches both.
- **Per-tenant rate limiting** (not pool-level, not a bootstrap singleton):
  each tenant gets its own `{tenant}-ratelimit` Middleware, sized from that
  tenant's resolved `tenant_spec.ratelimit` — see "Plan resolution".
- **TLS**: one **wildcard** Certificate (`*.{platform_domain}`) covers every
  default host, issued via the DNS-01-capable ClusterIssuer
  `letsencrypt-dns01-production` (`wildcard_cluster_issuer` in defaults) —
  a wildcard cert can **only** be issued via ACME DNS-01, a hard protocol
  limitation, which is why this is a separate issuer from the platform's
  original HTTP-01-only `cluster_issuer`. A per-tenant Certificate (via
  that original HTTP-01 issuer) is only created for `custom_domain`, gated
  by a pre-flight DNS check (fails fast if the domain doesn't already
  resolve to this platform).
- **Storage**: one PVC `odoo-filestore` (every tenant under
  `filestore/{instance_slug}/`) and one PVC `odoo-addons` (community/custom
  addons incl. `session_redis`), mounted read-write/read-only respectively
  by every pool pod and Job. `odoo_action=sync-addons` does a `git pull`
  onto the addons PVC.
- **Autoscaling**: an HPA on `{pool}-http` only (`minReplicas`/
  `maxReplicas` from the plan spec, CPU target 65%, conservative behavior —
  scale up at most 1 pod/60s, scale down only after a 300s stabilization
  window), a PodDisruptionBudget (`minAvailable: 1`), preferred (not
  required) pod anti-affinity spreading `{pool}-http` replicas across
  nodes, and `startupProbe`/`readinessProbe`/`livenessProbe` on
  `/web/health`. **Correct but inert on today's single node** — extra pods
  just pend if there's no room. See "Before adding node 2".
- **NetworkPolicies written once at bootstrap**, generic
  component-label-driven selectors (`xayma.sh/pool-role`,
  `xayma.sh/job-role`) so they never need a per-pool or per-tenant copy:
  default-deny; edge → `{pool}-http`/`{pool}-gevent`; `{pool}-http` + Jobs →
  PgBouncer; PgBouncer + `{pool}-cron`/`{pool}-gevent` + Jobs +
  `xayma-tools` (observability, pgAdmin) → Postgres directly; pool pods →
  Redis; edge → suspend-backend; edge → cert-manager's ACME HTTP-01 solver
  pods.
- **PriorityClass `batch-low`** (negative value, `preemptionPolicy: Never`)
  on every Job this role creates. **Namespace ResourceQuota** as a
  guardrail against Job pileups.
- **Observability**: a `postgres-exporter` next to the shared Postgres
  (`prometheus.io/scrape` annotation) plus Traefik's own router metrics —
  see "Observability" below for the three alerts to configure externally.

Every resource still carries `app.kubernetes.io/part-of: xayma-platform`,
`app.kubernetes.io/managed-by: ansible`, `app.kubernetes.io/name`
(`odoo`/`postgres`/`pgbouncer`/`redis`/`postgres-exporter`/`suspend-backend`),
and `xayma.sh/*` labels — but the taxonomy is now split into **pool** labels
(`xayma.sh/pool-id`, `xayma.sh/odoo-version`, `xayma.sh/plan`,
`xayma.sh/pool-role`: `http`|`cron`|`gevent`) and **tenant** labels
(`xayma.sh/customer`, `xayma.sh/instance`, and — on the tenant's IngressRoute
only — `xayma.sh/state`: `running`|`suspended`|`stopped`, which doubles as
the durable record every lifecycle action reads back instead of taking
`version`/`plan`/state as input again; see `tasks/_recover-tenant.yml`).
Job pods additionally carry `xayma.sh/job-role`
(`init`|`fixup`|`restore`|`backup`|`sync-addons`|`fileops`).

Stateless executor, but not stateless about sizing
----------------------------------------------------
This role is still a **stateless executor for tenant identity**: every
secret is derived deterministically from a handful of vault "seeds"
(`vars/main.yml`) rather than persisted per-tenant/per-pool — see "Vault".

It is **no longer** stateless about sizing. The old slice-model doctrine
("no `plans:` dict in the role, the app sends fully-resolved flat `plan_*`
vars") existed because the role's Job Template was driven by a flat AWX
survey. Now that the app calls AWX's REST API directly with real JSON
`extra_vars`, there's no reason to keep sizing flat — see "Plan resolution".

Plan resolution
----------------
Plans are identified by **name** (a slug — it lands in `pool_id` and PG
role names, so it's RFC-1123-asserted). The **Xayma app is the source of
truth**: every real launch sends `plan` (the name) **and** `plan_spec` (the
full nested spec, schema below) as `extra_vars`. This role keeps a small
**fallback catalog** (`defaults/main/00-plans.yml`, same schema, two
example entries — `standard`/`premium`) consulted only when `plan_spec` is
absent (manual CLI/AWX-survey runs that only set `plan`); an unrecognized
`plan` name with no `plan_spec` either falls through to
`plan_spec_hard_fallback` (`defaults/main/01-deploy-odoo-defaults.yml`), a
deliberately conservative safety net, never a real tier.

Precedence, resolved fresh on every call by `tasks/_resolve-plan-spec.yml`:

```
plan_spec (app-supplied extra_var, or plan_spec_json for manual/survey runs)
  > plans[plan]   (defaults/main/00-plans.yml catalog)
  > plan_spec_hard_fallback
```

### `plan_spec` schema

| Key | Scope | Meaning |
|-----|-------|---------|
| `workers` | pool | `{pool}-http`'s Odoo `workers` (must be `>=2` — prefork mode) |
| `replicas_min` / `replicas_max` | pool | HPA bounds for `{pool}-http` |
| `pod.cpu_request` / `pod.cpu_limit` / `pod.mem_request` / `pod.mem_limit` | pool | Applied identically to all 3 Deployments in the pool |
| `limits.mem_soft` / `limits.mem_hard` | pool | odoo.conf `limit_memory_soft`/`hard` (bytes) — **kept below `pod.mem_limit`** so Odoo self-recycles a worker before Kubernetes OOMKills the pod (asserted, not just commented) |
| `limits.time_cpu` / `limits.time_real` / `limits.request` | pool | odoo.conf `limit_time_cpu`/`limit_time_real`/`limit_request` |
| `cron.threads` | pool | `{pool}-cron`'s `max_cron_threads` |
| `db_maxconn` | pool | odoo.conf `db_maxconn` — **worker-side demand**, not the fairness lever (see below) |
| `init_modules` | tenant | Comma-separated (no spaces) `-i` module list for this tenant's one-shot init Job |
| `tenant_db_max_connections` | tenant | PgBouncer's per-tenant-database cap — **the real per-tenant throttle** |
| `ratelimit.avg` / `ratelimit.burst` | tenant | This tenant's own `{tenant}-ratelimit` Traefik Middleware |

**POOL-scoped keys are CREATE-ONLY.** They are applied only the first time
a given `(version, plan)` pool is actually created. The pool's `{pool}-http`
Deployment carries an annotation, `xayma.sh/plan-spec-hash`, recording a
deterministic hash of the pool-scoped spec it was built with. On every
later `deploy`/`change-plan` call touching that same pool, if the freshly-
resolved pool-scoped spec's hash differs from that annotation, the incoming
values are **silently ignored** for sizing purposes (a prominent warning is
logged) — a mismatched `plan_spec` sent by an ordinary tenant operation
**never** resizes/rolls a pool out from under every other tenant on it.

**TENANT-scoped keys are always honored** — every `deploy`/`change-plan`
call re-applies them for that one tenant (its PgBouncer cap, its ratelimit
Middleware, and — for `deploy` only — its init module list).

### `db_maxconn` vs `tenant_db_max_connections` — don't confuse these

`db_maxconn` (pool-scoped, odoo.conf) is how many PG connections **one
Odoo worker process** may open — it's worker-side demand, sized for
throughput. `tenant_db_max_connections` (tenant-scoped, PgBouncer) is how
many connections **one tenant's database** may ever hold through PgBouncer
at once — it's the actual per-tenant fairness lever, protecting every other
tenant on the same pool from one noisy neighbor. **Raising `db_maxconn`
does not fix a starved tenant** — if a tenant is queueing at PgBouncer,
that's PgBouncer enforcing `tenant_db_max_connections` **by design**; the
right lever is `odoo_action=change-plan` onto a plan with a higher
`tenant_db_max_connections`, not touching `db_maxconn`.

### Resizing an existing pool: `odoo_action=apply-plan`

To actually change a pool's frozen sizing (workers/replicas/pod resources/
memory limits/cron threads/`db_maxconn`) for **every** tenant currently on
it, use `odoo_action=apply-plan` (`version`+`plan`+`plan_spec` — no tenant
identity in this action's input at all). It bypasses the create-only guard
and rolls the pool's Deployments/ConfigMap/HPA to the new spec, updating
the hash annotation and logging a prominent warning. **This affects every
tenant currently served by that pool** — there is no partial/per-tenant
apply.

### Individual overrides for manual/testing runs

The only manual-run fallback is `plan_spec_json` (a JSON string, parsed
into `plan_spec` by `tasks/_resolve-plan-spec.yml`) — the flat `plan_*`
extra_vars from the old model are gone entirely (see "Migrating from the
slice model").

Input contract
---------------
Identity vars (also survey-able for manual runs):

| Var | Description |
|-----|-------------|
| `customer` | Customer slug/name (label only now — no longer a namespace name) |
| `instancename` | Instance name — slugified into the tenant's k8s object names and its Postgres database name; globally unique (guaranteed by the Xayma app) |
| `custom_domain` | Optional extra domain, in addition to the always-attached default host `<instance_slug>.<platform_domain>` (blank = default host only; deduped if it equals the default) |
| `version` | Odoo major version, e.g. `"19"` → pool image `{odoo_image_repo}:19.0`. Required for `deploy`/`change-plan` only — every other action recovers it from live state |
| `plan` | Plan **name** — required for `deploy`/`change-plan`/`apply-plan`. See "Plan resolution" |
| `plan_spec` | Optional nested dict, same schema as "Plan resolution" above. Omit to use the catalog/hard-fallback |
| `plan_spec_json` | Optional JSON-string form of `plan_spec`, for AWX surveys/manual runs that can't send a real nested value |

`odoo_action=restore`/`backup` additionally accept `snapshot_id` (restore
only, required — the timestamp prefix of the snapshot) and `snapshot_kind`
(`daily`|`adhoc`, default `daily`).

`odoo_action=sync-addons` requires `addons_repo_url` and accepts
`addons_repo_ref` (default `main`) — this action takes **no** tenant
identity at all.

Actions
-------
Dispatch is driven by a single `odoo_action` extra-var (not `--tags` — AWX
surveys set `extra_vars`, so a single Job Template + survey can drive every
action below). The role fails fast with a clear message if `odoo_action` is
missing or not one of these values.

This role is **strictly tenant-scoped** for every action except
`apply-plan` (pool-scoped) and `sync-addons`/`check-snapshot-freshness`
(namespace-scoped): every other action operates on exactly one tenant, never
more. Customer-level operations (e.g. suspending every tenant for a customer
that hasn't paid) are orchestrated by the Xayma app, which loops the
tenant-scoped Job Template over every active tenant belonging to that
customer.

| `odoo_action` | Scope | Description |
|-----|-------|-------------|
| `deploy` | tenant | Create/update a tenant: bootstrap the platform + pool if needed, create the DB (idempotent — never re-inits), run init/fixup Jobs, render routing |
| `start` | tenant | GRANT the tenant's DB access back, repoint its IngressRoute at its pool |
| `stop` | tenant | REVOKE DB access, terminate backends, purge Redis sessions, repoint at the stopped page |
| `suspend` | tenant | Identical mechanics to `stop`, different page/state |
| `restart` | tenant | **REDEFINED** — see "Known caveats": terminates DB backends + purges Redis sessions; there is no per-tenant process to restart |
| `edit-domain` | tenant | Change the custom domain (default host untouched), preserving current state |
| `change-plan` | tenant | Move a tenant to a different plan, **same Odoo version only** (asserted). No data movement |
| `apply-plan` | **pool** | Reconcile an existing pool's frozen sizing to a new `plan_spec` — see "Plan resolution". Affects every tenant on that pool |
| `change-version` | tenant | **Stub — fails fast.** Out of scope, see "Non-goals" |
| `delete` | tenant | Final adhoc backup, drop the DB, remove filestore dir + PgBouncer entry, delete every labelled object; scales the pool to 0 if it was the last tenant (never deletes pool objects) |
| `restore` | tenant | Restore a snapshot (`snapshot_id`, optional `snapshot_kind`) — fences via REVOKE/terminate, no scaling steps |
| `backup` | tenant | Single-tenant `pg_dump` + filestore tar → MinIO, scoped `xayma.snapshots` credential |
| `sync-addons` | **namespace** | `git pull` onto the shared addons PVC — no tenant identity |
| `check-snapshot-freshness` | **namespace** | Fail unless every live, non-stopped tenant has a `daily` snapshot newer than `snapshot_freshness.max_age_hours` — independent backstop for a tenant whose `backup` silently stops being invoked at all (see "Observability") |

PgBouncer & per-tenant fairness
--------------------------------
`{pool}-http` talks to PgBouncer (transaction pooling); `{pool}-cron`,
`{pool}-gevent`, and every Job talk to Postgres **directly**. PgBouncer
authenticates client connections dynamically via `auth_query` against a
`SECURITY DEFINER` wrapper function over `pg_shadow` (no static userlist
edits when pool roles are created/rotated) — its own outbound connection to
Postgres (to run that query) uses a plaintext credential in a k8s Secret,
same trust model as every other secret in this role.

PgBouncer's `[databases]` section has no native way to vary a per-database
cap by which pool a *dynamically-named* database belongs to under one
wildcard `*` entry, so each tenant's `tenant_db_max_connections` is
materialized as its **own explicit `[databases]` line** in a separate
`%include`d ConfigMap, fully regenerated from every live tenant's
IngressRoute annotation on every `deploy`/`change-plan`/`delete`
(`tasks/_sync-pgbouncer-tenants.yml`) — a full desired-state replace, never
an incremental patch, so concurrent tenant operations can never race or
corrupt each other's entry. PgBouncer is reloaded via `SIGHUP` afterwards
(equivalent to its admin console's `RELOAD`, no dropped connections) —
**note**: the ConfigMap volume mount isn't `subPath`'d, so kubelet's normal
~60s sync delay applies before a brand-new cap is actually live; see "Known
caveats".

PostgreSQL access model
-------------------------
Ansible connects **directly** to the shared Postgres via `community.postgresql`
(not `kubectl exec`) — see "Requirements" for why this is expected to work
in this platform's topology, and `vars/main.yml`/`tasks/_pg-connection.yml`
for how the connection host is resolved (the shared Postgres Service is a
real `ClusterIP`, not headless, specifically so this works without relying
on in-cluster DNS from the control node).

- **One LOGIN role per pool**: `pool_{version}_{plan}`, password derived
  from the same vault-seed pattern as every other secret, keyed on
  `pool_id`.
- **Once, for the whole tier**: `pgadmin_ro`/`backup_ro` (`LOGIN`,
  `pg_read_all_data`) and `pgbouncer_auth`/`pg_exporter` (least-privilege,
  see "Architecture").
- **Per tenant database**: `OWNER` = its pool role; `REVOKE CONNECT FROM
  PUBLIC`; `GRANT CONNECT` to the pool role + `pgadmin_ro` + `backup_ro`.
  The admin/backup grants **survive suspension** — only the pool role's
  grant is toggled by `suspend`/`stop`/`start`.

Autoscaling — before adding node 2
------------------------------------
The HPA/PDB/anti-affinity above are **correct but inert** on today's
single-node cluster — a pod that can't be scheduled just pends. Before
adding a second node, work through this checklist:

- [ ] `odoo-filestore`/`odoo-addons` must become RWX across nodes (an NFS
      export or an RWX-capable provisioner) — `local-path`'s PVCs are tied
      to one node. `attachment_s3` for the filestore is a **future**
      optimization, not implemented here (see "Non-goals").
- [ ] Confirm Redis sessions are actually active for every tenant (see
      "Known caveats" on `session_redis`'s real dependency gap) — a node-2
      pod that can't reach sessions will force every user on it to log in
      again.
- [ ] Confirm the HPA actually schedules `{pool}-http` replicas onto the
      new node (not just that it *wants* to) once the storage above is RWX.
- [ ] `postgres.node_selector` currently pins the shared Postgres
      deliberately (its PVC is `local-path`) — decide (and document) which
      node it stays on before adding a second one.

Observability
--------------
`postgres-exporter` runs next to the shared Postgres
(`prometheus.io/scrape: "true"` on its Service); Traefik's own router
metrics are exposed by the platform already. Configure at least these
three alerts externally (dashboards are out of scope here):

1. **Pool worker occupation / CPU p95** per pool — the earliest signal that
   a pool needs `apply-plan` (more `workers`/replicas) before tenants on it
   feel it.
2. **Postgres disk usage** — one shared disk now serves every tenant.
3. **Per-database size growth** (`pg_database_size` per tenant db) — the
   earliest signal of one tenant needing a `change-plan` or a data cleanup
   conversation, now that noisy-neighbor DB growth is everyone's problem.

Backup failures and snapshot staleness are **not** on this externally-configured
list — they're surfaced through AWX's own notification mechanism instead (see
"AWX" below): a `backup` Job failing triggers `notification_templates_error`
directly, and `check-snapshot-freshness`'s nightly Schedule catches the
otherwise-silent case of a tenant whose `backup` stops being invoked at all
(no Job ever runs, so no Job-failure notification would fire either).

AWX
---
The calling app knows exactly **one** launch target: the Job Template
`odoo-tenant` (`allow_simultaneous: false` — every `odoo_action`, for every
tenant, serializes through this one queue; see "Concurrency" below for why).
Running `ansible-playbook awx-setup.yml` reconciles this Job Template plus
its shared survey plus the nightly `check-snapshot-freshness` Schedule —
nothing else is needed; see "Migrating from the slice model" for the
prerequisites that setup itself relies on (Project/Inventory/credentials).

- `odoo-tenant` runs `site.yml` directly against every `odoo_action`,
  including `sync-addons` (no tenant identity) and
  `check-snapshot-freshness` (namespace-scoped) — dispatch between actions
  happens entirely inside the playbook (`roles/deploy-odoo/tasks/main.yml`'s
  `when: odoo_action == '...'` chain), not in AWX.
- Carries `notification_templates_error: [awx_failure_notification_template_name]`
  (default `deploy-odoo-failures`) directly — this role does **not** create
  that Notification Template object itself (see "Manual steps").

Run `ansible-playbook awx-setup.yml` (with `CONTROLLER_HOST`+
`CONTROLLER_OAUTH_TOKEN` or `CONTROLLER_USERNAME`/`CONTROLLER_PASSWORD` set)
to create/update the Job Template + its survey + the nightly
`check-snapshot-freshness` Schedule (`roles/configure-awx`) — it does
**not** create the AWX Project, Inventory, machine/vault credentials, or
Notification Template it references by name; those are one-time manual
setup (see "Migrating from the slice model").

The survey matches the "Input contract" table above, plus `plan_spec_json`
as the manual-run fallback for `plan_spec`.

### Concurrency

`odoo-tenant` is deliberately a single Job Template with
`allow_simultaneous: false`, not a Workflow Job Template routing to
separate per-action-class Job Templates. An earlier revision of this role
split "fast" (`start`/`stop`/.../`apply-plan`) and "slow"
(`deploy`/`delete`/`restore`/`backup`/`check-snapshot-freshness`) actions
into two independently-queued internal Job Templates behind a WFJT
router, so a long `deploy` for one tenant couldn't block a quick `stop` for
another. That indirection was removed — this deployment doesn't need the
extra throughput, and it's simpler to reason about one queue than a
WFJT + router playbook (`awx.awx.job_launch`/`job_wait` calling the AWX API
from *inside* an AWX job) + two internal templates.

The one queue is not incidental — it's the only thing standing in for real
per-tenant/per-pool locking today. Several task sequences in
`roles/deploy-odoo` read live cluster state, decide something, then write
it back, with no lock of their own:
- `_sync-pgbouncer-tenants.yml` rebuilds the *entire* PgBouncer tenant
  ConfigMap from live IngressRoute state on every call; a brand-new
  tenant's entry (injected via an overlay var, since its own IngressRoute
  isn't live yet) can be silently dropped if another tenant's deploy reads
  cluster state and writes the ConfigMap in the same window.
- `_resolve-pool.yml`'s create-only pool bootstrap checks "does this pool
  exist" then creates it if not — two tenants simultaneously first onto the
  same brand-new `(version, plan)` pool could both pass that check with
  different `plan_spec` overrides, and whichever apply lands second wins
  silently (the "frozen spec, ignoring mismatched keys" warning never
  fires, since neither call saw the pool as already existing).
- `change-plan.yml`/`delete-tenant.yml` count a pool's remaining tenants
  via live IngressRoute labels before deciding whether to scale it to 0;
  two tenants leaving the same pool at once can each see the other as
  "remaining" and neither triggers the scale-down.
- Nothing fences a *double-fire* against the same tenant (e.g. an
  accidental duplicate `stop`+`start`) — whichever call finishes last wins,
  and a multi-step state transition can interleave into a combination no
  single call would ever produce.

None of this is Ansible/Kubernetes handling it for you — it only doesn't
happen today because `allow_simultaneous: false` never lets two of these
sequences run at the same instant. If this Job Template is ever switched to
`allow_simultaneous: true` (or split back into multiple queues) for
throughput, real locking needs to go in for the sequences above first, not
as an afterthought.

### Scheduled operations

AWX `Schedule` objects bind to **one** Job Template with **fixed**
`extra_vars` — they cannot loop over tenants. Nightly backups therefore
stay **app-driven**: the Xayma app's own scheduler calls `odoo-tenant` once
per active tenant on its nightly trigger (consistent with how
customer-scope operations already work in this role — the app loops, the
role never does). Retention and a periodic restore-test are the app's/ops'
responsibility against the MinIO `snapshots` bucket layout below — this
role provides the mechanism (`backup`/`restore`), not the policy.

`check-snapshot-freshness` takes no tenant identity at all, so — unlike
backup — it genuinely fits AWX's own `Schedule`: `awx-setup.yml` reconciles
one, `{{ awx_snapshot_freshness_schedule_name }}` (default
`odoo-snapshot-freshness-check-nightly`), bound directly to `odoo-tenant`
with fixed `extra_data: {odoo_action: check-snapshot-freshness}` and an
`{{ awx_snapshot_freshness_rrule }}` iCal RRULE (default: nightly). It goes
through the same Job Template/Notification Template path as every
app-launched call, so a stale-snapshot condition and an actual `backup` Job
crash alert through the identical Notification Template.

Backups & Restore
-------------------
Snapshots land in the platform MinIO's `snapshots` bucket, pathed
`snapshots/{customer}/{instance}/{daily|adhoc}/{timestamp}/`, under the
**scoped** `xayma.snapshots` MinIO user (Get/Put/Delete objects + list the
`snapshots` bucket only — no admin API, no access to `uploads`/`backups`;
see "Vault"). `backup` (`pg_dump` + filestore tar) and `restore` (the
inverse, fenced by REVOKE/GRANT around the pool role rather than the old
"scale Odoo to 0" — there is no per-tenant process to scale in the pool
model) are both deliberate ops actions in this role, not dynamic policy —
see "Scheduled operations" for how iteration across tenants actually
happens. `check-snapshot-freshness` (`snapshot_freshness.max_age_hours`,
default 30) independently verifies every live, non-stopped tenant actually
has one — see "Observability"/"Scheduled operations".

Versions
--------
Pinned in `defaults/main/01-deploy-odoo-defaults.yml` (`versions:` dict) —
Odoo `"19"` (survey fallback only; real deploys pass `version` explicitly;
image is `{odoo_image_repo}:{version}.0` — see "Requirements" for why
`odoo_image_repo` is NOT just `odoo` in practice), Postgres
`16.6-bookworm` (ONE shared StatefulSet), PgBouncer `1.23.1-p2`
(`edoburu/pgbouncer`), Redis `7.4.1-alpine3.20`, postgres-exporter
`v0.16.0`, `busybox` (suspend-backend + every initContainer config merge),
`rclone` (restore/backup/check-snapshot-freshness Jobs), `git` (sync-addons
Job). Never `latest`.

Vault
-----
`defaults/main/02-credentials.yml` (encrypted) holds three seeds:
`vault_odoo_db_password` (per-**pool** role passwords, keyed on `pool_id`),
`vault_odoo_admin_password` (per-**tenant** admin password, keyed on
customer/instance slugs), and `vault_odoo_platform_password` (derives every
fixed-platform-role credential this role creates once for the whole tier:
the Postgres superuser, `pgadmin_ro`/`backup_ro`, PgBouncer's `auth_query`
role, `pg_exporter`, and Redis's `requirepass` — each
`hash('sha256', seed ~ '|' ~ role_name)`), plus
`vault_odoo_snapshot_secret_key` — the platform MinIO's **scoped**
`xayma.snapshots` user's secret (Get/Put/Delete objects + list the
`snapshots` bucket only, no admin API, no access to `uploads`/`backups`)
for `backup`/`restore`/`check-snapshot-freshness`, **not** the platform
root user (`xayma.admin`). The secret value lives in
`install-platform.xayma.sh`'s vault today (`vault_minio_snapshots_password`)
and must be copied here manually — see "Manual steps". The access key
itself (`xayma.snapshots`) is a fixed identity, not a secret, so it is a
plain default (`odoo.snapshot_access_key`) rather than vaulted. See
`defaults/main/02-credentials.yml.example` to (re)create it.

Known caveats
-------------
- **`restart` semantics changed.** There is no per-tenant Odoo process
  anymore — `odoo_action=restart` now terminates that tenant's live DB
  backends and purges its Redis sessions (forcing a fresh login), and
  touches nothing else. It does **not** restart any pod, and it does
  **not** affect any other tenant sharing the same pool.
- **Registry/import warm-up on first request per pod.** A freshly-scaled
  (or freshly-restarted) `{pool}-http` pod pays Odoo's normal module-
  registry load cost on its first request after starting — with many
  tenants sharing one pool, this is a bigger one-time cost than the old
  per-instance model's, though it only happens on pod start, not per-tenant.
- **PgBouncer log noise on suspend/stop.** Terminating backends while a
  tenant is mid-request produces normal-but-noisy PgBouncer/Postgres
  disconnect log lines — harmless, but don't alert on it.
- **PgBouncer tenant-cap propagation lag.** The tenants ConfigMap isn't
  `subPath`-mounted, so kubelet's normal ~60s sync window applies between
  `_sync-pgbouncer-tenants.yml` writing a new cap and the mounted file
  (and therefore the post-`SIGHUP` reload) actually reflecting it.
- **`session_redis` — verified, but with one real gap left.** Configuration
  (host/port/password/SSL/prefix) was verified against
  `camptocamp/odoo-cloud-platform`'s actual source on its `19.0` branch: it
  is entirely `ODOO_SESSION_REDIS_*` environment variables (NOT odoo.conf
  keys), set per pool in `pool-deployments.yaml.j2`, with
  `ODOO_SESSION_REDIS_SSL=0` (this platform's Redis doesn't terminate TLS,
  but the addon defaults to SSL **on**) and `ODOO_SESSION_REDIS_PREFIX`
  set to the pool id. The Redis key itself carries no per-tenant/per-
  database segment at all (only the stored session *value*'s `db` field
  does), which is why suspend/stop/restart's session purge
  (`tasks/_purge-tenant-redis-sessions.yml`) runs a small Lua script
  server-side (SCAN+GET+filter-by-`db`+DEL) instead of a key-pattern scan.
  The stock Docker Hub `odoo` image does not include the `redis` Python
  package the addon imports — without it, a tenant's first session access
  on any pool pod throws, not at pod startup. See "Custom Odoo image"
  below for how that gets built automatically (opt-in).
- **Single-node SLA statement.** This platform is one k3s node. The HPA/
  PDB/anti-affinity exist and are correct, but they cannot provide any
  actual redundancy until a second node exists — see "Before adding node 2".
- **`apply-plan` blast radius.** It resizes/rolls **every** tenant on the
  targeted pool in one call, by design (see "Plan resolution") — there is
  deliberately no partial/per-tenant apply.
- **Wildcard Certificate needs a DNS-01 ClusterIssuer.** `wildcard_cluster_issuer`
  defaults to `letsencrypt-dns01-production` (added to
  `install-platform.xayma.sh` for exactly this). If it is ever repointed at
  an HTTP-01-only issuer (e.g. `cluster_issuer`), the Certificate k8s
  object applies fine but cert-manager will never actually issue it — this
  is an ACME protocol limitation, not a config choice.
- **`db_maxconn` vs `tenant_db_max_connections`** — see "Plan resolution".
  These are two different levers; confusing them under load is the most
  likely on-call mistake in this architecture.

Non-goals
---------
Stated here, deliberately not implemented: email/SMTP (outbound and
inbound); a dedicated-slice tier; `change-version` migration (stub only —
see the Actions table); `attachment_s3` for the filestore (documented as a
future optimization in "Before adding node 2"); per-tenant branded suspend
pages; multi-node storage work beyond the "Before adding node 2" checklist;
per-tenant AWX Schedules for backup iteration (see "Scheduled operations"
— deliberately app-driven instead). Automatic image build/push
(`odoo_image_build`, see "Custom Odoo image") is implemented but OFF by
default and requires `nerdctl-full` on the execution host — this repo
does not install that itself.

Custom Odoo image
--------------------
`session_redis` (mandatory - see "Architecture") needs the `redis` Python
package, which the stock `odoo` image doesn't ship. `tasks/_ensure-odoo-image.yml`
(called from `_resolve-pool.yml` on every `deploy`/`change-plan`/
`apply-plan`) handles this:

1. Checks whether `{{ odoo_image_repo }}:{{ version }}.0` already exists in
   the registry, via Docker Hub's REST API directly (not a local image
   cache — that can't tell you whether a tag was ever actually *pushed*).
   Works anonymously for public repos; authenticates with
   `vault_odoo_dockerhub_username`/`vault_odoo_dockerhub_token` (a Docker
   Hub **access token**, not your account password) when set, for private
   repos.
2. If it's missing and `odoo_image_build.enabled` is `true` (default
   `false`): builds it from `files/odoo-image/Containerfile` (`FROM
   odoo:{version}.0` + `pip install redis`) and pushes it, via `nerdctl`
   against **k3s's own embedded containerd** (`k3s_containerd_socket`) —
   deliberately not a separate Docker daemon, since k3s already ships a
   container runtime and `nerdctl` is Docker-CLI-compatible against it
   directly. This needs the **`nerdctl-full`** release specifically
   (https://github.com/containerd/nerdctl/releases) — the bare `nerdctl`
   binary doesn't bundle `buildkitd`, which `nerdctl build` requires.
3. If it's missing and `odoo_image_build.enabled` is `false`: fails fast
   with a clear message, rather than letting every pod in a brand-new pool
   crash-loop on a missing image.

To activate it: install `nerdctl-full` on the k3s node (the execution
host), set `odoo_image_build.enabled: true`, point `odoo_image_repo` at
your registry namespace (e.g. `yourdockerhubuser/xayma-odoo`), and add
`vault_odoo_dockerhub_username`/`vault_odoo_dockerhub_token` to the vault.
The build file itself (`files/odoo-image/Containerfile` — named
`Containerfile`, not `Dockerfile`: this repo's `nerdctl build` was verified
against a live host to ignore `-f`/`--file` entirely and unconditionally
look for `Containerfile` in the build context, so the file is named to
match rather than fight it) was written without access to a container
runtime to build-test it — verify the first real build actually starts a
working container before relying on it in production.

If you'd rather not give this role container-build access at all, push
the tag yourself from wherever you already build images (`docker build
--build-arg ODOO_VERSION={version} -t {odoo_image_repo}:{version}.0
-f files/odoo-image/Containerfile files/odoo-image/ && docker push ...` —
or the `nerdctl` equivalent) and
leave `odoo_image_build.enabled` at `false` — the existence check still
runs, it just never needs to build anything once the tag is there.

Requirements
------------
- The Xayma.sh Platform (k3s + Traefik + cert-manager + a platform MinIO)
  already deployed — see `install-platform.xayma.sh`.
- `kubernetes.core`, `community.general`, `community.postgresql`, and
  `awx.awx` (only needed for `awx-setup.yml`) Galaxy collections (see
  `requirements.yml`) — plus **`psycopg2` (or `psycopg`) installed on the
  Ansible control node/AWX execution environment**, required by
  `community.postgresql`.
- **Only if `odoo_image_build.enabled: true`** (see "Custom Odoo image" —
  off by default): the **`nerdctl-full`** release
  (https://github.com/containerd/nerdctl/releases — the full bundle, not
  the bare binary, so `buildkitd` is included) on whatever host runs this
  playbook. No extra Galaxy collection needed for this one — it shells out
  to `nerdctl` against k3s's own embedded containerd.
- **This role assumes the Ansible control node/AWX execution environment is
  co-located with the k3s node** (consistent with `kubeconfig_path`
  defaulting to a local file path, `/etc/rancher/k3s/k3s.yaml`) and can
  reach the cluster's Service CIDR directly by IP — `community.postgresql`
  connects to the shared Postgres's `ClusterIP` (see
  `tasks/_pg-connection.yml`), which is not a routable address from outside
  the cluster's own network. If that assumption doesn't hold in your
  deployment, this needs revisiting (e.g. exposing Postgres via a
  NodePort/host-reachable path) before anything in this role can run.
- `install-platform.xayma.sh`'s `letsencrypt-dns01-production` ClusterIssuer
  (DNS-01, for the wildcard Certificate) — see "Known caveats".
- **A registry tag with the `redis` Python package installed**, referenced
  via `odoo_image_repo` (default `odoo`, i.e. the stock Docker Hub image —
  override this). `session_redis` (mandatory, see "Architecture") imports
  `redis`, which the stock image does not ship. See "Custom Odoo image"
  for how this role can build/push it for you (opt-in), or push it
  yourself and leave that feature off.
- The vault password, as a file (CLI) or a credential record (AWX).

Migrating from the slice model
---------------------------------
This is a ground-up rewrite, not an incremental change. There is no
automated migration path for **existing tenants** created by the old
per-instance role — every tenant in scope for this migration needs a fresh
`odoo_action=deploy` under the new model (which creates a NEW empty
database, not a move of the old one). If tenants already exist in
production under the old slice model, treat this as: back up the old
tenant (its own external backup mechanism), stand up the new platform,
`odoo_action=deploy` a same-named tenant onto it, then restore the old
backup's data into the new tenant via `odoo_action=restore` (the MinIO
snapshot layout is unchanged) rather than expecting any in-place upgrade.

### Architecture changes

| | Old (slice) | New (pool) |
|---|---|---|
| Namespace | One per customer | One (`xayma-odoo`) for the whole tier |
| Compute | One Deployment per instance | 3 Deployments per **pool** (`version`+`plan`), shared by every tenant on that pool |
| Postgres | One dedicated StatefulSet per instance | One shared StatefulSet for the whole tier |
| Connection pooling | None | PgBouncer (transaction mode) in front of `{pool}-http` only |
| Sessions | Odoo's default (filesystem, in-process) | Redis, mandatory, via `session_redis` |
| TLS | One Certificate per instance (HTTP-01) | One wildcard Certificate (DNS-01) + per-tenant only for `custom_domain` |
| Suspend/stop backend | One per customer namespace | One for the whole namespace |
| Rate limiting | None | Per-tenant Traefik Middleware, sized from the plan |
| Autoscaling | None | HPA on `{pool}-http`, inert until a second node exists |
| Filestore/addons storage | One PVC pair per instance | One PVC pair for the whole tier (`filestore/{slug}/` subdirs) |

### Calling app contract diff

1. **`plan_*` flat vars are gone.** Send `plan` (a name, still required)
   plus `plan_spec` (the full nested dict — see "Plan resolution" for the
   schema and the pool-scoped/tenant-scoped split) as a real JSON
   extra_var over the AWX API. There is no flat-field equivalent anymore.
2. **Single launch target, but a new name.** The app now calls the Job
   Template `odoo-tenant` instead of this role's old instance-scoped Job
   Template — same extra_vars contract otherwise. If the app hardcoded the
   old Job Template's numeric ID, it needs `odoo-tenant`'s ID instead (see
   "Manual steps" below).
3. **`version` is no longer sent on every lifecycle call.** Only
   `deploy`/`change-plan`/`apply-plan` take `version`. Every other action
   (`start`/`stop`/`suspend`/`restart`/`edit-domain`/`delete`/`backup`/
   `restore`) drops it — the role recovers the tenant's current pool
   assignment from live state. If the app was always sending `version`
   regardless of action, it's now simply ignored for those actions (not an
   error), but stop sending it if convenient.
4. **New actions**: `change-plan` (move a tenant to a different plan, same
   version), `apply-plan` (pool-scoped — resize an existing pool for every
   tenant on it, admin-only), `backup` (on-demand single-tenant backup, for
   the app's own nightly scheduler — see "Scheduled operations"),
   `check-snapshot-freshness` (namespace-scoped, no tenant identity, AWX
   Schedule-driven — an independent backstop, not something the app needs to
   call itself).
5. **`restart` semantics changed.** It used to restart the tenant's own
   Deployment+StatefulSet pods. It now only terminates that tenant's DB
   backends and purges its Redis sessions — there is no per-tenant process
   to restart anymore. If the app's UI describes this action to end users,
   update the copy.
6. **`odoo_action=deploy` no longer resets replica counts.** The old
   "re-running deploy against a suspended/stopped instance resets it to
   running" caveat is gone — deploy never touches Deployment replica
   counts at all now (pools are shared; a single tenant's deploy call
   can't scale one).
7. **Create-only pool semantics** (see "Plan resolution") mean a
   `deploy`/`change-plan` call's `plan_spec` pool-scoped values are
   IGNORED once a pool already exists with a different frozen spec — if
   the app expects a plan's sizing change to take effect immediately for
   every tenant on that plan, it must call `apply-plan` explicitly, not
   just re-send a different `plan_spec` on the next tenant's `deploy`.

### Manual steps

- [x] **Vault**: `vault_odoo_platform_password` — done (added to the
      encrypted `defaults/main/02-credentials.yml`).
- [ ] **Vault**: `vault_odoo_snapshot_secret_key`
      — copy the real value from `install-platform.xayma.sh`'s vault
      (`vault_minio_snapshots_password`; access key is the plain default
      `xayma.snapshots`, not vaulted)
      into this repo's vault: `ansible-vault edit
      roles/deploy-odoo/defaults/main/02-credentials.yml
      --vault-password-file vault_password`. Confirm out of band that the
      platform side has already created the `xayma.snapshots` MinIO
      user/policy and the `snapshots` bucket — this repo does not and
      should not create either.
- [ ] **AWX Notification Template**: create one (Access → Notifications →
      Add) for backup/snapshot-freshness failure alerts, default name
      `deploy-odoo-failures` (`awx_failure_notification_template_name`) —
      `configure-awx` attaches it to the `odoo-tenant` Job Template
      automatically, it just needs the object to exist. Trigger a
      deliberate failure once (e.g. a temporarily wrong vault value) and
      confirm it actually fires before relying on it.
- [ ] **Addons PVC population**: the shared `odoo-addons` PVC starts
      empty. Run `odoo_action=sync-addons -e addons_repo_url=<git URL>`
      (after at least one `deploy`, which bootstraps the PVC) before
      deploying any tenant whose `init_modules` needs anything beyond
      Odoo's own bundled modules — including `session_redis` itself, which
      is mandatory.
- [ ] **AWX one-time setup** — `roles/configure-awx` only *reconciles* the
      Job Template; it assumes an AWX Project, Inventory, and two
      credentials already exist (by the exact names in
      `roles/configure-awx/defaults/main.yml`, or override those defaults
      to match names you already use):
      1. **Project** (AWX UI: Resources → Projects → Add) — SCM type Git,
         SCM URL = this repo, default name `deploy-odoo.xayma.sh`
         (`awx_project_name`).
      2. **Inventory** (Resources → Inventories → Add) containing the k3s
         host(s) this role targets, default name `xayma-platform`
         (`awx_inventory_name`).
      3. **Machine credential** (Access → Credentials → Add, type
         *Machine*) for SSH/local access to wherever the playbook actually
         runs, default name `xayma-platform-ssh`
         (`awx_machine_credential_name`).
      4. **Vault credential** (Access → Credentials → Add, type *Vault*)
         holding this repo's vault password, default name
         `deploy-odoo-vault` (`awx_vault_credential_name`).
      5. Install collections and run the setup playbook itself **against
         the AWX API** (not the k3s cluster — different creds, different
         target):
         ```
         ansible-galaxy install -r requirements.yml
         export CONTROLLER_HOST=https://<your-awx-host>
         export CONTROLLER_OAUTH_TOKEN=<a token with rights to create/edit templates>
         ansible-playbook awx-setup.yml
         ```
         This creates/updates the `odoo-tenant` Job Template (survey +
         `notification_templates_error` attached) and the nightly
         `check-snapshot-freshness` Schedule bound to it. It's idempotent;
         re-run it any time `roles/configure-awx/defaults` changes (e.g. a
         new survey field).
      6. Point the calling app at `odoo-tenant`'s Job Template ID — find it
         in the AWX UI (Templates → `odoo-tenant`) or via
         `GET /api/v2/job_templates/?name=odoo-tenant`.
- [x] **Wildcard Certificate / DNS-01**: done —
      `install-platform.xayma.sh` provides `letsencrypt-dns01-production`,
      and `wildcard_cluster_issuer` points at it by default.
- [ ] **Custom Odoo image with `redis` installed** (see "Custom Odoo
      image"): either push it yourself and point `odoo_image_repo` at it,
      or install `nerdctl-full` on the k3s node and set
      `odoo_image_build.enabled: true` (+ the two `vault_odoo_dockerhub_*`
      seeds) to have this role build/push it automatically on first use.
- [ ] **`community.postgresql` runtime requirement**: install
      `psycopg2`/`psycopg` on whatever host/execution-environment runs this
      role. Confirm that host can actually reach the shared Postgres's
      ClusterIP directly (see "Requirements" for the co-located-execution
      assumption this rewrite makes; if it doesn't hold, this needs a
      different connectivity plan before anything works).
- [x] **PgBouncer `auth_query` setup**: no manual step — the
      `pgbouncer_auth` role and its `SECURITY DEFINER` lookup function are
      created automatically by `tasks/_ensure-platform.yml` on the first
      `deploy`.

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
