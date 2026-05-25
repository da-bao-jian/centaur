set dotenv-load := true

namespace := env_var_or_default("CENTAUR_NAMESPACE", "centaur")
release := env_var_or_default("CENTAUR_RELEASE", "centaur")
chart := "contrib/chart"
dev_values := "contrib/chart/values.dev.yaml"
slack_values := "contrib/chart/values.slack.yaml"
slack_pf_pid := "/tmp/centaur-slack-pf.pid"
slack_pf_log := "/tmp/centaur-slack-pf.log"
slack_pf_session := "centaur-slack-pf"
slack_tunnel_pid := "/tmp/centaur-slack-tunnel.pid"
slack_tunnel_log := "/tmp/centaur-slack-tunnel.log"
slack_watch_pid := "/tmp/centaur-slack-watch.pid"
slack_watch_log := "/tmp/centaur-slack-watch.log"
slack_watch_session := "centaur-slack-watch"
slack_local_port := "3001"
overlay_values := "contrib/chart/values.overlay.yaml"
overlay_dir := "../centaur-overlay"
overlay_image := "centaur-overlay:latest"
kind_cluster := env_var_or_default("KIND_CLUSTER", "centaur")

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
    # Auto-include the Slack overlay when present so `just deploy` doesn't
    # silently disable slackbot (values.dev.yaml sets slackbot.enabled=false).
    # The file's existence is the opt-in.
    if [[ -f {{slack_values}} ]]; then
      extra_args+=(-f {{slack_values}})
    fi
    if [[ -f {{overlay_values}} ]]; then
      extra_args+=(-f {{overlay_values}})
    fi
    helm upgrade --install {{release}} {{chart}} -n {{namespace}} --create-namespace -f {{dev_values}} ${extra_args[@]+"${extra_args[@]}"}

up:
    just bootstrap-secrets
    just build
    just deploy

down:
    kubectl delete namespace {{namespace}} --ignore-not-found --wait

down-clean:
    just slack-down
    just down

restart-clean:
    just down-clean
    just up
    just slack-watch
    just status
    just slack-status

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

# Build the org overlay image (centaur-overlay:latest) and load it into the
# local kind cluster. The image is just a static carrier for the overlay
# tree at /overlay; an initContainer copies it into the api/sandbox pods.
overlay-build:
    docker build -t {{overlay_image}} {{overlay_dir}}
    kind load docker-image {{overlay_image}} --name {{kind_cluster}}

# Build + load overlay, helm-upgrade so values.overlay.yaml gets picked up,
# then rollout-restart the api so the new overlay reaches sandboxes spawned
# after this point. Image tag is unchanged so helm won't restart on its own.
overlay-up:
    #!/usr/bin/env bash
    set -euo pipefail
    just overlay-build
    just deploy
    kubectl -n {{namespace}} rollout restart deploy/{{release}}-centaur-api
    kubectl -n {{namespace}} rollout status  deploy/{{release}}-centaur-api --timeout=180s

# Bring the local Slack integration online: ensure the slackbot Deployment
# is helm-enabled and scaled to 1, then expose the webhook endpoint via a
# port-forward + public tunnel. Idempotent — re-running heals dead processes
# and stale pid files by probing the actual local/private leg on port 3001.
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

    webhook_code() {
      curl -s -o /dev/null -w '%{http_code}' --max-time 5 -X POST "$1" \
        -H 'Content-Type: application/json' -d '{}' || true
    }

    webhook_healthy() {
      case "$1" in
        200|400|401|403) return 0 ;;
        *) return 1 ;;
      esac
    }

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

    pf_code="000"
    if [[ -f {{slack_pf_pid}} ]] && kill -0 "$(cat {{slack_pf_pid}})" 2>/dev/null; then
      pf_code="$(webhook_code "http://127.0.0.1:{{slack_local_port}}/api/webhooks/slack")"
    fi
    if [[ -f {{slack_pf_pid}} ]] && kill -0 "$(cat {{slack_pf_pid}})" 2>/dev/null && webhook_healthy "$pf_code"; then
      echo "==> port-forward already healthy (pid $(cat {{slack_pf_pid}}), HTTP $pf_code)"
    else
      if [[ -f {{slack_pf_pid}} ]]; then
        echo "==> port-forward unhealthy (HTTP $pf_code); restarting"
      fi
      rm -f {{slack_pf_pid}} {{slack_pf_log}}
      echo "==> starting port-forward svc/$svc {{slack_local_port}}:{{slack_local_port}}"
      command -v tmux >/dev/null || { echo "tmux not installed; brew install tmux" >&2; exit 2; }
      tmux kill-session -t {{slack_pf_session}} 2>/dev/null || true
      tmux new-session -d -s {{slack_pf_session}} "
        while true; do
          date '+%Y-%m-%d %H:%M:%S starting kubectl port-forward' >>{{slack_pf_log}}
          tail -f /dev/null | kubectl -n {{namespace}} port-forward svc/$svc {{slack_local_port}}:{{slack_local_port}} >>{{slack_pf_log}} 2>&1
          date '+%Y-%m-%d %H:%M:%S kubectl port-forward exited; restarting' >>{{slack_pf_log}}
          sleep 1
        done
      "
      tmux display-message -p -t {{slack_pf_session}} '#{pane_pid}' > {{slack_pf_pid}}
      sleep 2
    fi

    url=""
    if [[ -n "${NGROK_DOMAIN:-}" ]]; then
      url="https://${NGROK_DOMAIN}"
    elif [[ -f {{slack_tunnel_log}} ]]; then
      for _ in $(seq 1 15); do
        url="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' {{slack_tunnel_log}} | head -n1 || true)"
        [[ -n "$url" ]] && break
        sleep 1
      done
    fi

    tunnel_code="000"
    if [[ -n "$url" ]] && [[ -f {{slack_tunnel_pid}} ]] && kill -0 "$(cat {{slack_tunnel_pid}})" 2>/dev/null; then
      tunnel_code="$(webhook_code "${url}/api/webhooks/slack")"
    fi
    if [[ -n "$url" ]] && [[ -f {{slack_tunnel_pid}} ]] && kill -0 "$(cat {{slack_tunnel_pid}})" 2>/dev/null && webhook_healthy "$tunnel_code"; then
      echo "==> tunnel already healthy (pid $(cat {{slack_tunnel_pid}}), HTTP $tunnel_code)"
    else
      if [[ -f {{slack_tunnel_pid}} ]]; then
        echo "==> tunnel unhealthy (HTTP $tunnel_code); restarting"
        pid="$(cat {{slack_tunnel_pid}})"
        kill "$pid" 2>/dev/null || true
      fi
      rm -f {{slack_tunnel_pid}} {{slack_tunnel_log}}
      if [[ -n "${NGROK_DOMAIN:-}" ]]; then
        command -v ngrok >/dev/null || { echo "ngrok not installed; brew install ngrok or unset NGROK_DOMAIN" >&2; exit 2; }
        echo "==> starting ngrok tunnel on ${NGROK_DOMAIN}"
        ngrok http --domain "${NGROK_DOMAIN}" http://127.0.0.1:{{slack_local_port}} --log stdout --log-format logfmt >{{slack_tunnel_log}} 2>&1 &
        echo $! > {{slack_tunnel_pid}}
        url="https://${NGROK_DOMAIN}"
      else
        command -v cloudflared >/dev/null || { echo "cloudflared not installed; brew install cloudflared or set NGROK_DOMAIN" >&2; exit 2; }
        echo "==> starting cloudflared quick tunnel (URL changes on every restart)"
        cloudflared tunnel --url http://127.0.0.1:{{slack_local_port}} --no-autoupdate >{{slack_tunnel_log}} 2>&1 &
        echo $! > {{slack_tunnel_pid}}
        url=""
        for _ in $(seq 1 15); do
          url="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' {{slack_tunnel_log}} | head -n1 || true)"
          [[ -n "$url" ]] && break
          sleep 1
        done
      fi
      sleep 4
    fi

    if [[ -z "$url" ]]; then
      echo "tunnel did not surface a URL; tail of {{slack_tunnel_log}}:" >&2
      tail -n 30 {{slack_tunnel_log}} >&2 || true
      exit 1
    fi

    echo "==> verifying local port-forward (curl on 127.0.0.1:{{slack_local_port}})"
    pf_code="$(webhook_code "http://127.0.0.1:{{slack_local_port}}/api/webhooks/slack")"
    case "$pf_code" in
      400|401|403) echo "    OK (HTTP $pf_code — slackbot rejected unsigned payload as expected)" ;;
      200) echo "    OK (HTTP 200)" ;;
      *) echo "    WARNING: port-forward returned HTTP $pf_code — pod may not be ready" >&2 ;;
    esac

    echo "==> verifying tunnel reachability (${url}/api/webhooks/slack)"
    tunnel_code="$(webhook_code "${url}/api/webhooks/slack")"
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

    tmux kill-session -t {{slack_watch_session}} 2>/dev/null || true
    rm -f {{slack_watch_pid}} {{slack_watch_log}}

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
    tmux kill-session -t {{slack_pf_session}} 2>/dev/null || true
    rm -f {{slack_pf_log}} {{slack_tunnel_log}}

    deploy="{{release}}-centaur-slackbot"
    if kubectl -n {{namespace}} get deploy "$deploy" >/dev/null 2>&1; then
      current="$(kubectl -n {{namespace}} get deploy "$deploy" -o jsonpath='{.spec.replicas}')"
      if [[ "$current" != "0" ]]; then
        echo "==> scaling $deploy 0"
        kubectl -n {{namespace}} scale deploy/"$deploy" --replicas=0
      fi
    fi

slack-watch interval="15":
    #!/usr/bin/env bash
    set -euo pipefail

    command -v tmux >/dev/null || { echo "tmux not installed; brew install tmux" >&2; exit 2; }
    if [[ -f {{slack_watch_pid}} ]] && kill -0 "$(cat {{slack_watch_pid}})" 2>/dev/null; then
      echo "slack watcher already running (pid $(cat {{slack_watch_pid}}))"
      exit 0
    fi

    tmux kill-session -t {{slack_watch_session}} 2>/dev/null || true
    rm -f {{slack_watch_log}}
    repo="$(pwd)"
    tmux new-session -d -s {{slack_watch_session}} "
      cd \"$repo\"
      while true; do
        date '+%Y-%m-%d %H:%M:%S slack-watch tick' >>{{slack_watch_log}}
        just slack-up >>{{slack_watch_log}} 2>&1 || true
        sleep {{interval}}
      done
    "
    tmux display-message -p -t {{slack_watch_session}} '#{pane_pid}' > {{slack_watch_pid}}
    echo "slack watcher started (pid $(cat {{slack_watch_pid}}), interval {{interval}}s)"

slack-status:
    #!/usr/bin/env bash
    set -euo pipefail

    deploy="{{release}}-centaur-slackbot"
    if kubectl -n {{namespace}} get deploy "$deploy" >/dev/null 2>&1; then
      kubectl -n {{namespace}} get deploy "$deploy" -o wide
    else
      echo "slackbot Deployment not installed"
    fi

    local_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -X POST "http://127.0.0.1:{{slack_local_port}}/api/webhooks/slack" -H 'Content-Type: application/json' -d '{}' || true)"
    echo "local webhook: HTTP ${local_code}"

    for label in port-forward:{{slack_pf_pid}} tunnel:{{slack_tunnel_pid}} watcher:{{slack_watch_pid}}; do
      name="${label%%:*}"; pidfile="${label##*:}"
      if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
        echo "${name}: alive (pid $(cat "$pidfile"))"
      else
        echo "${name}: down"
        if [[ "$name" == "port-forward" && -f {{slack_pf_log}} ]]; then
          tail -n 5 {{slack_pf_log}} || true
        elif [[ "$name" == "watcher" && -f {{slack_watch_log}} ]]; then
          tail -n 5 {{slack_watch_log}} || true
        fi
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
    encoded_key="$(kubectl -n {{namespace}} get secret centaur-infra-env -o jsonpath='{.data.SLACKBOT_API_KEY}' 2>/dev/null || true)"
    API_KEY=""
    if [[ -n "$encoded_key" ]]; then
      API_KEY="$(printf '%s' "$encoded_key" | base64 --decode 2>/dev/null || printf '%s' "$encoded_key" | base64 -D)"
    fi
    if [[ -z "$API_KEY" ]]; then
      echo "SLACKBOT_API_KEY not found in centaur-infra-env" >&2
      exit 2
    fi
    AUTH_ARGS=(-H "Authorization: Bearer ${API_KEY}")
    ASSIGNMENT_GENERATION=""
    cleanup() {
      if [[ -n "${ASSIGNMENT_GENERATION:-}" ]]; then
        kubectl exec -n {{namespace}} "$API_DEPLOY" -- curl -s -X POST \
          "http://localhost:8000/agent/threads/${THREAD_KEY}/release" \
          -H "Content-Type: application/json" "${AUTH_ARGS[@]}" \
          -d "{\"release_id\":\"smoke-${THREAD_KEY}\",\"cancel_inflight\":true}" >/dev/null || true
      fi
    }
    trap cleanup EXIT

    SPAWN=$(kubectl exec -n {{namespace}} "$API_DEPLOY" -- curl -s -X POST http://localhost:8000/agent/spawn \
      -H "Content-Type: application/json" "${AUTH_ARGS[@]}" \
      -d "{\"thread_key\":\"${THREAD_KEY}\"}")
    ASSIGNMENT_GENERATION=$(printf '%s' "$SPAWN" | jq -r '.assignment_generation')

    kubectl exec -n {{namespace}} "$API_DEPLOY" -- curl -s -X POST http://localhost:8000/agent/message \
      -H "Content-Type: application/json" "${AUTH_ARGS[@]}" \
      -d "{\"thread_key\":\"${THREAD_KEY}\",\"assignment_generation\":${ASSIGNMENT_GENERATION},\"role\":\"user\",\"parts\":[{\"type\":\"text\",\"text\":\"Reply with exactly PONG and nothing else.\"}]}" >/dev/null

    EXECUTE=$(kubectl exec -n {{namespace}} "$API_DEPLOY" -- curl -s -X POST http://localhost:8000/agent/execute \
      -H "Content-Type: application/json" "${AUTH_ARGS[@]}" \
      -d "{\"thread_key\":\"${THREAD_KEY}\",\"assignment_generation\":${ASSIGNMENT_GENERATION},\"delivery\":{\"platform\":\"dev\"}}")
    EXECUTION_ID=$(printf '%s' "$EXECUTE" | jq -r '.execution_id')

    for _ in $(seq 1 60); do
      STATE=$(kubectl exec -n {{namespace}} "$API_DEPLOY" -- curl -s "${AUTH_ARGS[@]}" "http://localhost:8000/agent/executions/${EXECUTION_ID}")
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

    kubectl exec -n {{namespace}} "$API_DEPLOY" -- curl -s "${AUTH_ARGS[@]}" "http://localhost:8000/agent/executions/${EXECUTION_ID}" | jq
    echo "smoke timed out waiting for execution ${EXECUTION_ID}" >&2
    exit 1

dev-pulse-e2e channel="pris-test":
    #!/usr/bin/env bash
    set -euo pipefail
    API_DEPLOY="deploy/{{release}}-centaur-api"
    CHANNEL="{{channel}}"
    CHANNEL="${CHANNEL#\#}"
    if [[ -z "$CHANNEL" ]]; then
      echo "Slack channel is required, for example: just dev-pulse-e2e pris-test" >&2
      exit 2
    fi
    LOOKBACK_HOURS="${DEV_PULSE_E2E_LOOKBACK_HOURS:-24}"
    if ! [[ "$LOOKBACK_HOURS" =~ ^[0-9]+$ ]] || [[ "$LOOKBACK_HOURS" -lt 1 ]]; then
      echo "DEV_PULSE_E2E_LOOKBACK_HOURS must be a positive integer" >&2
      exit 2
    fi

    encoded_key="$(kubectl -n {{namespace}} get secret centaur-infra-env -o jsonpath='{.data.SLACKBOT_API_KEY}' 2>/dev/null || true)"
    API_KEY=""
    if [[ -n "$encoded_key" ]]; then
      API_KEY="$(printf '%s' "$encoded_key" | base64 --decode 2>/dev/null || printf '%s' "$encoded_key" | base64 -D)"
    fi
    AUTH_ARGS=()
    if [[ -n "$API_KEY" ]]; then
      AUTH_ARGS=(-H "Authorization: Bearer ${API_KEY}")
    fi

    TRIGGER_KEY="dev-pulse-e2e-${CHANNEL}-$(date +%s)"
    PAYLOAD="$(jq -n \
      --arg channel "$CHANNEL" \
      --arg trigger_key "$TRIGGER_KEY" \
      --argjson lookback_hours "$LOOKBACK_HOURS" \
      '{
        workflow_name: "dev_pulse_daily",
        trigger_key: $trigger_key,
        eager_start: false,
        input: {
          slack_channel: $channel,
          slack_sender_name: "Pris",
          lookback_hours: $lookback_hours,
          metadata: {
            reason: "dev_pulse_e2e",
            target_channel: $channel
          }
        }
      }')"

    echo "==> enqueueing dev_pulse_daily E2E run to #${CHANNEL}"
    CREATE_RESPONSE="$(printf '%s' "$PAYLOAD" | kubectl exec -i -n {{namespace}} "$API_DEPLOY" -- \
      curl -sS -X POST http://localhost:8000/workflows/runs \
        -H "Content-Type: application/json" "${AUTH_ARGS[@]}" --data-binary @-)"
    printf '%s\n' "$CREATE_RESPONSE" | jq
    RUN_ID="$(printf '%s\n' "$CREATE_RESPONSE" | jq -r '.run_id // empty')"
    if [[ -z "$RUN_ID" ]]; then
      echo "workflow run response did not include run_id" >&2
      exit 1
    fi

    for _ in $(seq 1 120); do
      STATE="$(kubectl exec -n {{namespace}} "$API_DEPLOY" -- \
        curl -sS "${AUTH_ARGS[@]}" "http://localhost:8000/workflows/runs/${RUN_ID}")"
      STATUS="$(printf '%s\n' "$STATE" | jq -r '.status // empty')"
      case "$STATUS" in
        completed)
          echo "==> workflow completed"
          printf '%s\n' "$STATE" | jq '{
            run_id,
            workflow_name,
            status,
            completed_at,
            slack_channel: .output_json.slack_channel,
            counts: .output_json.counts
          }'
          CHECKPOINTS="$(kubectl exec -n {{namespace}} "$API_DEPLOY" -- \
            curl -sS "${AUTH_ARGS[@]}" "http://localhost:8000/workflows/runs/${RUN_ID}/checkpoints")"
          printf '%s\n' "$CHECKPOINTS" | jq '{
            ok,
            run_id,
            checkpoints: [
              .checkpoints[]
              | {
                  checkpoint_name,
                  step_kind,
                  created_at,
                  slack: (
                    if (.checkpoint_name == "tool_slack_send_message" or .checkpoint_name == "post_dev_pulse_to_slack")
                    then {channel: .state.channel, ts: .state.ts, permalink: .state.permalink}
                    else null
                    end
                  )
                }
            ]
          }'
          printf '%s\n' "$CHECKPOINTS" | jq -e '
            (.checkpoints | any(.checkpoint_name == "collect_dev_pulse_metrics")) and
            (.checkpoints | any(.checkpoint_name == "tool_slack_send_message")) and
            (.checkpoints | any(.checkpoint_name == "post_dev_pulse_to_slack"))
          ' >/dev/null
          echo "==> verified metrics collection and Slack delivery checkpoints for #${CHANNEL}"
          exit 0
          ;;
        failed|cancelled)
          printf '%s\n' "$STATE" | jq
          CHECKPOINTS="$(kubectl exec -n {{namespace}} "$API_DEPLOY" -- \
            curl -sS "${AUTH_ARGS[@]}" "http://localhost:8000/workflows/runs/${RUN_ID}/checkpoints" || true)"
          printf '%s\n' "$CHECKPOINTS" | jq . || true
          exit 1
          ;;
      esac
      sleep 2
    done

    kubectl exec -n {{namespace}} "$API_DEPLOY" -- \
      curl -sS "${AUTH_ARGS[@]}" "http://localhost:8000/workflows/runs/${RUN_ID}" | jq
    echo "dev-pulse E2E timed out waiting for workflow run ${RUN_ID}" >&2
    exit 1
