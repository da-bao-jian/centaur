set dotenv-load := true

namespace := env_var_or_default("CENTAUR_NAMESPACE", "centaur")
release := env_var_or_default("CENTAUR_RELEASE", "centaur")
chart := "contrib/chart"
dev_values := "contrib/chart/values.dev.yaml"
slack_values := "contrib/chart/values.slack.yaml"
slack_pf_pid := "/tmp/centaur-slack-pf.pid"
slack_tunnel_pid := "/tmp/centaur-slack-tunnel.pid"
slack_tunnel_log := "/tmp/centaur-slack-tunnel.log"
slack_local_port := "3001"

default:
    just --list

build:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ "${JUST_BUILD_SEQUENTIAL:-0}" =~ ^(1|true|yes)$ ]]; then
      just _build-all-sequential
    else
      pids=()
      for recipe in _build-api _build-iron-proxy _build-slackbot _build-agent; do
        just "$recipe" &
        pids+=("$!")
      done
      status=0
      for pid in "${pids[@]}"; do
        wait "$pid" || status=1
      done
      exit "$status"
    fi

_build-all-sequential:
    just _build-api
    just _build-iron-proxy
    just _build-slackbot
    just _build-agent

build-one service:
    #!/usr/bin/env bash
    set -euo pipefail
    case "{{service}}" in
      api) just _build-api ;;
      iron-proxy) just _build-iron-proxy ;;
      slackbot) just _build-slackbot ;;
      agent|sandbox) just _build-agent ;;
      *) echo "unknown service: {{service}}" >&2; exit 2 ;;
    esac

_build-api:
    docker build -t centaur-api:latest -f services/api/Dockerfile .

_build-iron-proxy:
    docker build -t centaur-iron-proxy:latest -f services/iron-proxy/Dockerfile .

_build-slackbot:
    docker build -t centaur-slackbot:latest -f services/slackbot/Dockerfile .

_build-agent:
    docker build --target sandbox -t centaur-agent:latest -f services/sandbox/Dockerfile .

bootstrap-secrets *args:
    contrib/scripts/bootstrap-k8s-secrets.sh --namespace {{namespace}} {{args}}

deploy:
    #!/usr/bin/env bash
    set -euo pipefail
    helm dependency update {{chart}} >/dev/null
    extra_args=()
    if [[ -n "${OP_CONNECT_CREDENTIALS_FILE:-}" ]]; then
      extra_args+=(
        --set ironProxy.secretSource=onepassword-connect
        --set onepasswordConnect.connect.create=true
      )
    fi
    helm upgrade --install {{release}} {{chart}} -n {{namespace}} --create-namespace -f {{dev_values}} ${extra_args[@]+"${extra_args[@]}"}

up:
    just bootstrap-secrets
    just build
    just deploy

down:
    kubectl delete namespace {{namespace}} --ignore-not-found --wait

reinstall:
    just down
    just up

status:
    kubectl get all -n {{namespace}}

logs component:
    kubectl logs -n {{namespace}} deploy/{{release}}-centaur-{{component}} --tail=200 -f

slack-thread-logs slack_link since="24h":
    CENTAUR_NAMESPACE={{namespace}} CENTAUR_RELEASE={{release}} bash services/slackbot/scripts/slack-thread-logs.sh "{{slack_link}}" "{{since}}"

slack-thread-report slack_link:
    CENTAUR_NAMESPACE={{namespace}} CENTAUR_RELEASE={{release}} bash services/slackbot/scripts/slack-thread-report.sh "{{slack_link}}"

# Bring the local Slack integration online: ensure the slackbot Deployment
# is helm-enabled and scaled to 1, then expose the webhook endpoint via a
# port-forward + public tunnel. Idempotent — re-running heals dead processes.
#
# Tunnel: prefers ngrok with a stable static domain when NGROK_DOMAIN is set
# (export NGROK_DOMAIN=<your>.ngrok-free.dev), falls back to cloudflared's
# ephemeral quick tunnel otherwise. With a quick tunnel the URL changes on
# every restart, so you must re-paste it into Slack's Event Subscriptions.
slack-up:
    #!/usr/bin/env bash
    set -euo pipefail

    deploy="{{release}}-centaur-slackbot"
    svc="{{release}}-centaur-slackbot"

    if ! kubectl -n {{namespace}} get deploy "$deploy" >/dev/null 2>&1; then
      echo "==> slackbot Deployment missing; helm upgrade with slack overlay"
      helm dependency update {{chart}} >/dev/null
      helm upgrade --install {{release}} {{chart}} -n {{namespace}} --create-namespace \
        -f {{dev_values}} -f {{slack_values}} --reuse-values
    fi

    current="$(kubectl -n {{namespace}} get deploy "$deploy" -o jsonpath='{.spec.replicas}')"
    if [[ "$current" != "1" ]]; then
      echo "==> scaling $deploy 1"
      kubectl -n {{namespace}} scale deploy/"$deploy" --replicas=1
    fi
    kubectl -n {{namespace}} rollout status deploy/"$deploy" --timeout=120s

    if [[ -f {{slack_pf_pid}} ]] && kill -0 "$(cat {{slack_pf_pid}})" 2>/dev/null; then
      echo "==> port-forward already running (pid $(cat {{slack_pf_pid}}))"
    else
      rm -f {{slack_pf_pid}}
      echo "==> starting port-forward svc/$svc {{slack_local_port}}:{{slack_local_port}}"
      kubectl -n {{namespace}} port-forward svc/"$svc" {{slack_local_port}}:{{slack_local_port}} >/dev/null 2>&1 &
      echo $! > {{slack_pf_pid}}
      sleep 2
    fi

    if [[ -f {{slack_tunnel_pid}} ]] && kill -0 "$(cat {{slack_tunnel_pid}})" 2>/dev/null; then
      echo "==> tunnel already running (pid $(cat {{slack_tunnel_pid}}))"
    else
      rm -f {{slack_tunnel_pid}} {{slack_tunnel_log}}
      if [[ -n "${NGROK_DOMAIN:-}" ]]; then
        command -v ngrok >/dev/null || { echo "ngrok not installed; brew install ngrok or unset NGROK_DOMAIN" >&2; exit 2; }
        echo "==> starting ngrok tunnel on ${NGROK_DOMAIN}"
        ngrok http --domain "${NGROK_DOMAIN}" {{slack_local_port}} --log stdout --log-format logfmt >{{slack_tunnel_log}} 2>&1 &
        echo $! > {{slack_tunnel_pid}}
      else
        command -v cloudflared >/dev/null || { echo "cloudflared not installed; brew install cloudflared or set NGROK_DOMAIN" >&2; exit 2; }
        echo "==> starting cloudflared quick tunnel (URL changes on every restart)"
        cloudflared tunnel --url http://localhost:{{slack_local_port}} --no-autoupdate >{{slack_tunnel_log}} 2>&1 &
        echo $! > {{slack_tunnel_pid}}
      fi
      sleep 4
    fi

    url=""
    if [[ -n "${NGROK_DOMAIN:-}" ]]; then
      url="https://${NGROK_DOMAIN}"
    else
      for _ in $(seq 1 15); do
        url="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' {{slack_tunnel_log}} | head -n1 || true)"
        [[ -n "$url" ]] && break
        sleep 1
      done
    fi
    if [[ -z "$url" ]]; then
      echo "tunnel did not surface a URL; tail of {{slack_tunnel_log}}:" >&2
      tail -n 30 {{slack_tunnel_log}} >&2 || true
      exit 1
    fi

    echo "==> verifying local port-forward (curl on 127.0.0.1:{{slack_local_port}})"
    pf_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -X POST "http://127.0.0.1:{{slack_local_port}}/api/webhooks/slack" -H 'Content-Type: application/json' -d '{}' || true)"
    case "$pf_code" in
      400|401|403) echo "    OK (HTTP $pf_code — slackbot rejected unsigned payload as expected)" ;;
      200) echo "    OK (HTTP 200)" ;;
      *) echo "    WARNING: port-forward returned HTTP $pf_code — pod may not be ready" >&2 ;;
    esac

    echo "==> verifying tunnel reachability (${url}/api/webhooks/slack)"
    tunnel_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 -X POST "${url}/api/webhooks/slack" -H 'Content-Type: application/json' -d '{}' || true)"
    case "$tunnel_code" in
      400|401|403|200) echo "    OK (HTTP $tunnel_code)" ;;
      000) echo "    DNS for ${url#https://} not yet resolvable locally — usually fine, Slack's edge resolves separately. Retry curl in ~30s if you need confirmation." ;;
      *) echo "    WARNING: tunnel returned HTTP $tunnel_code" >&2 ;;
    esac

    echo
    echo "Slack webhook URL: ${url}/api/webhooks/slack"
    if [[ -z "${NGROK_DOMAIN:-}" ]]; then
      echo "(quick tunnel — paste this into Slack > Event Subscriptions > Request URL each time it changes)"
    fi

slack-down:
    #!/usr/bin/env bash
    set -euo pipefail

    for pidfile in {{slack_tunnel_pid}} {{slack_pf_pid}}; do
      if [[ -f "$pidfile" ]]; then
        pid="$(cat "$pidfile")"
        if kill -0 "$pid" 2>/dev/null; then
          echo "==> killing $(basename "$pidfile" .pid) pid $pid"
          kill "$pid" 2>/dev/null || true
        fi
        rm -f "$pidfile"
      fi
    done

    deploy="{{release}}-centaur-slackbot"
    if kubectl -n {{namespace}} get deploy "$deploy" >/dev/null 2>&1; then
      current="$(kubectl -n {{namespace}} get deploy "$deploy" -o jsonpath='{.spec.replicas}')"
      if [[ "$current" != "0" ]]; then
        echo "==> scaling $deploy 0"
        kubectl -n {{namespace}} scale deploy/"$deploy" --replicas=0
      fi
    fi

slack-status:
    #!/usr/bin/env bash
    set -euo pipefail

    deploy="{{release}}-centaur-slackbot"
    if kubectl -n {{namespace}} get deploy "$deploy" >/dev/null 2>&1; then
      kubectl -n {{namespace}} get deploy "$deploy" -o wide
    else
      echo "slackbot Deployment not installed"
    fi

    for label in port-forward:{{slack_pf_pid}} tunnel:{{slack_tunnel_pid}}; do
      name="${label%%:*}"; pidfile="${label##*:}"
      if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
        echo "${name}: alive (pid $(cat "$pidfile"))"
      else
        echo "${name}: down"
      fi
    done

    url=""
    if [[ -n "${NGROK_DOMAIN:-}" ]]; then
      url="https://${NGROK_DOMAIN}"
    elif [[ -f {{slack_tunnel_log}} ]]; then
      url="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' {{slack_tunnel_log}} | head -n1 || true)"
    fi
    if [[ -n "$url" ]]; then
      echo "url: ${url}"
      code="$(curl -s -o /dev/null -w '%{http_code}' -X POST "${url}/api/webhooks/slack" -H 'Content-Type: application/json' -d '{}' || true)"
      echo "health: HTTP ${code}"
    fi

cleanup-orphan-proxy-services mode="dry-run":
    #!/usr/bin/env bash
    set -euo pipefail
    case "{{mode}}" in
      dry-run|delete) ;;
      *) echo "mode must be dry-run or delete" >&2; exit 2 ;;
    esac

    live_sandboxes="$(mktemp)"
    trap 'rm -f "$live_sandboxes"' EXIT
    kubectl -n {{namespace}} get pod -l centaur.ai/managed=true \
      -o jsonpath='{range .items[*]}{.metadata.labels.centaur\.ai/sandbox-id}{"\n"}{end}' \
      | sort -u > "$live_sandboxes"

    found=0
    while IFS=$'\t' read -r service sandbox_id; do
      [[ -n "$service" && -n "$sandbox_id" ]] || continue
      [[ "$sandbox_id" != "api" ]] || continue
      if grep -qx "$sandbox_id" "$live_sandboxes"; then
        continue
      fi
      found=1
      if [[ "{{mode}}" == "delete" ]]; then
        kubectl -n {{namespace}} delete svc "$service"
      else
        printf 'orphan proxy service: %s sandbox_id=%s\n' "$service" "$sandbox_id"
      fi
    done < <(
      kubectl -n {{namespace}} get svc -l centaur.ai/iron-proxy=true \
        -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.metadata.labels.centaur\.ai/sandbox-id}{"\n"}{end}'
    )

    if [[ "$found" -eq 0 ]]; then
      echo "No orphan proxy services found."
    fi

shell component:
    kubectl exec -it -n {{namespace}} deploy/{{release}}-centaur-{{component}} -- sh

smoke:
    #!/usr/bin/env bash
    set -euo pipefail
    THREAD_KEY="smoke-$(date +%s)"
    API_DEPLOY="deploy/{{release}}-centaur-api"

    SPAWN=$(kubectl exec -n {{namespace}} "$API_DEPLOY" -- curl -s -X POST http://localhost:8000/agent/spawn \
      -H "Content-Type: application/json" \
      -d "{\"thread_key\":\"${THREAD_KEY}\"}")
    ASSIGNMENT_GENERATION=$(printf '%s' "$SPAWN" | jq -r '.assignment_generation')

    kubectl exec -n {{namespace}} "$API_DEPLOY" -- curl -s -X POST http://localhost:8000/agent/message \
      -H "Content-Type: application/json" \
      -d "{\"thread_key\":\"${THREAD_KEY}\",\"assignment_generation\":${ASSIGNMENT_GENERATION},\"role\":\"user\",\"parts\":[{\"type\":\"text\",\"text\":\"Reply with exactly PONG and nothing else.\"}]}" >/dev/null

    EXECUTE=$(kubectl exec -n {{namespace}} "$API_DEPLOY" -- curl -s -X POST http://localhost:8000/agent/execute \
      -H "Content-Type: application/json" \
      -d "{\"thread_key\":\"${THREAD_KEY}\",\"assignment_generation\":${ASSIGNMENT_GENERATION},\"delivery\":{\"platform\":\"dev\"}}")
    EXECUTION_ID=$(printf '%s' "$EXECUTE" | jq -r '.execution_id')

    for _ in $(seq 1 60); do
      STATE=$(kubectl exec -n {{namespace}} "$API_DEPLOY" -- curl -s "http://localhost:8000/agent/executions/${EXECUTION_ID}")
      STATUS=$(printf '%s' "$STATE" | jq -r '.status // empty')
      case "$STATUS" in
        completed)
          printf '%s\n' "$STATE" | jq
          printf '%s\n' "$STATE" | jq -e '.result_text | contains("PONG")' >/dev/null
          exit 0
          ;;
        failed|failed_permanent|cancelled)
          printf '%s\n' "$STATE" | jq
          exit 1
          ;;
      esac
      sleep 2
    done

    kubectl exec -n {{namespace}} "$API_DEPLOY" -- curl -s "http://localhost:8000/agent/executions/${EXECUTION_ID}" | jq
    echo "smoke timed out waiting for execution ${EXECUTION_ID}" >&2
    exit 1
