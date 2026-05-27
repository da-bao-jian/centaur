# Release Flow

Centaur uses a fork-based release flow.

```text
upstream/main  ->  origin/main  ->  origin/staging  ->  origin/prod
 original repo      integration      staging deploy      Pris prod deploy
```

## Remotes

`origin` is the controlled fork: `da-bao-jian/centaur`.

`upstream` is the original project: `paradigmxyz/centaur`.

Do not push release branches to `upstream`. Use `upstream` only as the source
for deliberate sync branches.

## Branches

| Branch | Purpose |
|--------|---------|
| `main` | Integration branch in the fork. Feature work and upstream syncs land here first. |
| `staging` | Release-candidate branch. Deploy this to staging. |
| `prod` | Production branch. Deploy this to the live Pris bot. |

Short-lived branch names:

- `feat/<name>` for features
- `fix/<name>` for normal fixes
- `hotfix/<name>` for urgent fixes cut from `prod`
- `sync/upstream-YYYY-MM-DD` for upstream pulls

Do not create `staging/...` or `prod/...` branches. Git stores branch refs as
paths, so those names block the plain `staging` and `prod` release branches.

## Normal Change Flow

```bash
git fetch origin
git switch main
git pull --ff-only origin main
git switch -c feat/<name>

# edit, test, commit
git push origin feat/<name>
# open PR: feat/<name> -> main
```

After the PR lands in `main`, promote to staging:

```bash
git fetch origin
git switch staging
git pull --ff-only origin staging
git merge --ff-only origin/main
git push origin staging
```

After staging validation, promote to prod:

```bash
git fetch origin
git switch prod
git pull --ff-only origin prod
git merge --ff-only origin/staging
git push origin prod
git tag prod-YYYY-MM-DD-N
git push origin prod-YYYY-MM-DD-N
```

After the deployment controller rolls the target environment, run a real smoke
test against that deployed environment before declaring the release complete:

```bash
CENTAUR_NAMESPACE=<deployed-namespace> CENTAUR_RELEASE=centaur just smoke
```

This is a hard release gate for both `staging` and `prod`. The smoke must run
after the merge/push and after the rollout, because pre-merge tests do not prove
the deployed bot can still spawn a runtime and answer. If the smoke fails or the
operator cannot access the deployed cluster to run it, the release remains
unverified and must not be reported as healthy.

For any Slackbot, runtime lifecycle, sandbox, final-delivery, or Pris-facing
change, also run the real Slack E2E in `#pris-test`:

```bash
CENTAUR_NAMESPACE=<deployed-namespace> CENTAUR_RELEASE=centaur just pris-e2e pris-test
```

`pris-e2e` posts a parent message to `#pris-test`, sends a signed Slack
`app_mention` through the deployed Slackbot path, waits for Pris to reply in the
thread, and fails fast if Slack receives `Failed to start the runtime`. It also
seeds a stale suspended sandbox row for the synthetic test thread by default, so
the release explicitly proves stale-runtime recovery works after deployment.

`prod` should only move forward from `staging`, except for emergency hotfixes.

## Upstream Sync Flow

Sync upstream through a dedicated branch and PR into `main`:

```bash
git fetch upstream origin
git switch main
git pull --ff-only origin main
git switch -c sync/upstream-YYYY-MM-DD
git merge upstream/main

# resolve conflicts, run tests
git push origin sync/upstream-YYYY-MM-DD
# open PR: sync/upstream-YYYY-MM-DD -> main
```

After the sync PR lands in `main`, use the normal staging and prod promotion
flow.

## Hotfix Flow

Use hotfixes only for urgent production fixes:

```bash
git fetch origin
git switch prod
git pull --ff-only origin prod
git switch -c hotfix/<name>

# edit, test, commit
git push origin hotfix/<name>
# open PR: hotfix/<name> -> prod
```

After the hotfix lands in `prod`, merge it back:

```bash
git switch staging
git pull --ff-only origin staging
git merge origin/prod
git push origin staging

git switch main
git pull --ff-only origin main
git merge origin/prod
git push origin main
```

## Deployment Requirements

Deploy automation should accept only exact long-lived branches:

- `staging` deploys staging.
- `prod` deploys the live Pris bot.

Feature, fix, sync, and hotfix branches can run CI and preview builds, but they
must not auto-deploy to the shared bots.

Images and releases should carry enough metadata to answer "what is deployed?"
without guessing:

- branch name
- commit SHA
- release tag for prod
- image digest

Prefer image tags like:

```text
centaur-api:staging-<sha>
centaur-api:prod-<sha>
```

Keep `latest` as a convenience tag only, not as the source of truth.

For small-host or Mac Mini-style deployments, use the same chart and values but
pull service images from GHCR instead of relying on local Docker images loaded
into kind:

```bash
just source=ghcr image_tag=staging-sha-<short-sha> deploy
```

By default `source=ghcr` derives the image namespace from `origin`, for example
`ghcr.io/<owner>/<repo>/centaur-api`. Override it with
`image_namespace=ghcr.io/<owner>/<repo>` when testing a different registry
namespace. This only changes image repositories and tags; it should not change
Centaur workflows, personas, Slack behavior, or the observability stack. The
staging publisher builds Linux arm64 images for Mac Mini/kind nodes.

## Staging E2E Checks

Before promoting visibility or telemetry changes beyond `staging`, verify the
Ops Console against the local stack:

```bash
kubectl -n centaur port-forward deploy/centaur-centaur-api 8000:8000
curl -s -H "Authorization: Bearer $API_KEY" \
  "http://localhost:8000/ops/api/summary?window_hours=24" | jq '.status, .monitors'
```

Then open `http://localhost:8000/ops` in a browser and confirm the monitor
board renders without JavaScript errors.

Before promoting dev-pulse workflow changes beyond `staging`, run the real
Slack delivery E2E against the local stack:

```bash
just dev-pulse-e2e
```

The recipe sends the generated report to `#pris-test`, waits for the workflow
run to finish, and verifies that both metrics collection and Slack delivery
checkpoints exist. `#dev-pulse` is reserved for scheduled or production-like
runs, not staging tests.
