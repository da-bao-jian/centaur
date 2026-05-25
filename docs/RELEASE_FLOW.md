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
