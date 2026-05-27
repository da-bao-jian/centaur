set dotenv-load := true

namespace := env_var_or_default("CENTAUR_NAMESPACE", "centaur")
release := env_var_or_default("CENTAUR_RELEASE", "centaur")
chart := "contrib/chart"
dev_values := "contrib/chart/values.dev.yaml"
slack_values := "contrib/chart/values.slack.yaml"
source := env_var_or_default("CENTAUR_IMAGE_SOURCE", "local")
image_namespace := env_var_or_default("CENTAUR_IMAGE_NAMESPACE", "auto")
image_tag := env_var_or_default("CENTAUR_IMAGE_TAG", "latest")
image_pull_policy := env_var_or_default("CENTAUR_IMAGE_PULL_POLICY", "IfNotPresent")
slack_pf_pid := "/tmp/centaur-slack-pf.pid"
slack_pf_log := "/tmp/centaur-slack-pf.log"
slack_pf_session := "centaur-slack-pf"
slack_tunnel_pid := "/tmp/centaur-slack-tunnel.pid"
slack_tunnel_log := "/tmp/centaur-slack-tunnel.log"
slack_watch_pid := "/tmp/centaur-slack-watch.pid"
slack_watch_log := "/tmp/centaur-slack-watch.log"
slack_watch_session := "centaur-slack-watch"
slack_local_port := "3001"
visibility_ops_pid := "/tmp/centaur-visibility-ops.pid"
visibility_ops_log := "/tmp/centaur-visibility-ops.log"
visibility_ops_session := "centaur-visibility-ops"
visibility_grafana_pid := "/tmp/centaur-visibility-grafana.pid"
visibility_grafana_log := "/tmp/centaur-visibility-grafana.log"
visibility_grafana_session := "centaur-visibility-grafana"
visibility_vm_pid := "/tmp/centaur-visibility-vm.pid"
visibility_vm_log := "/tmp/centaur-visibility-vm.log"
visibility_vm_session := "centaur-visibility-vm"
visibility_vl_pid := "/tmp/centaur-visibility-vl.pid"
visibility_vl_log := "/tmp/centaur-visibility-vl.log"
visibility_vl_session := "centaur-visibility-vl"
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
      for recipe in _build-api _build-iron-proxy _build-slackbot _build-agent _build-codex-token-rotator; do
        just "$recipe" &
        pids+=("$!")
      done
      status=0
      for pid in "${pids[@]}"; do
        wait "$pid" || status=1
      done
      if [[ "$status" -eq 0 ]]; then
        just _load-local-images
      fi
      exit "$status"
    fi

_build-all-sequential:
    just _build-api
    just _build-iron-proxy
    just _build-slackbot
    just _build-agent
    just _build-codex-token-rotator
    just _load-local-images

build-one service:
    #!/usr/bin/env bash
    set -euo pipefail
    image=""
    case "{{service}}" in
      api) just _build-api; image="centaur-api:latest" ;;
      iron-proxy) just _build-iron-proxy; image="centaur-iron-proxy:latest" ;;
      slackbot) just _build-slackbot; image="centaur-slackbot:latest" ;;
      agent|sandbox) just _build-agent; image="centaur-agent:latest" ;;
      codex-token-rotator|token-rotator) just _build-codex-token-rotator; image="centaur-codex-token-rotator:latest" ;;
      *) echo "unknown service: {{service}}" >&2; exit 2 ;;
    esac
    just _kind-load-image "$image"

_build-api:
    docker build -t centaur-api:latest -f services/api/Dockerfile .

_build-iron-proxy:
    docker build -t centaur-iron-proxy:latest -f services/iron-proxy/Dockerfile .

_build-slackbot:
    docker build -t centaur-slackbot:latest -f services/slackbot/Dockerfile .

_build-agent:
    docker build --target sandbox -t centaur-agent:latest -f services/sandbox/Dockerfile .

_build-codex-token-rotator:
    docker build -t centaur-codex-token-rotator:latest -f services/codex-token-rotator/Dockerfile .

_load-local-images:
    just _kind-load-image centaur-api:latest
    just _kind-load-image centaur-iron-proxy:latest
    just _kind-load-image centaur-slackbot:latest
    just _kind-load-image centaur-agent:latest
    just _kind-load-image centaur-codex-token-rotator:latest

_kind-load-image image:
    #!/usr/bin/env bash
    set -euo pipefail
    if ! command -v kind >/dev/null 2>&1; then
      echo "kind is not installed; skipping local cluster load for {{image}}"
      exit 0
    fi
    if ! kind get clusters 2>/dev/null | grep -Fxq "{{kind_cluster}}"; then
      echo "kind cluster {{kind_cluster}} not found; skipping local cluster load for {{image}}"
      exit 0
    fi
    kind load docker-image "{{image}}" --name "{{kind_cluster}}"

bootstrap-secrets *args:
    contrib/scripts/bootstrap-k8s-secrets.sh --namespace {{namespace}} {{args}}

deploy:
    #!/usr/bin/env bash
    set -euo pipefail
    helm dependency update {{chart}} >/dev/null
    extra_args=()
    image_source="{{source}}"
    if [[ "$image_source" == "ghcr" ]]; then
      image_namespace="{{image_namespace}}"
      if [[ "$image_namespace" == "auto" ]]; then
        origin_url="$(git config --get remote.origin.url || true)"
        case "$origin_url" in
          git@github.com:*) repo_slug="${origin_url#git@github.com:}" ;;
          https://github.com/*) repo_slug="${origin_url#https://github.com/}" ;;
          http://github.com/*) repo_slug="${origin_url#http://github.com/}" ;;
          *) repo_slug="paradigmxyz/centaur" ;;
        esac
        repo_slug="${repo_slug%.git}"
        image_namespace="ghcr.io/${repo_slug}"
      fi
      image_tag="{{image_tag}}"
      image_pull_policy="{{image_pull_policy}}"
      extra_args+=(
        --set "api.image.repository=${image_namespace}/centaur-api"
        --set "api.image.tag=${image_tag}"
        --set "api.image.pullPolicy=${image_pull_policy}"
        --set "slackbot.image.repository=${image_namespace}/centaur-slackbot"
        --set "slackbot.image.tag=${image_tag}"
        --set "slackbot.image.pullPolicy=${image_pull_policy}"
        --set "sandbox.image.repository=${image_namespace}/centaur-agent"
        --set "sandbox.image.tag=${image_tag}"
        --set "sandbox.image.pullPolicy=${image_pull_policy}"
        --set "ironProxy.image.repository=${image_namespace}/centaur-iron-proxy"
        --set "ironProxy.image.tag=${image_tag}"
        --set "ironProxy.image.pullPolicy=${image_pull_policy}"
        --set "codexTokenRotator.image.repository=${image_namespace}/centaur-codex-token-rotator"
        --set "codexTokenRotator.image.tag=${image_tag}"
        --set "codexTokenRotator.image.pullPolicy=${image_pull_policy}"
      )
    elif [[ "$image_source" != "local" ]]; then
      echo "unknown image source: ${image_source} (expected local or ghcr)" >&2
      exit 2
    fi
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

visibility:
    #!/usr/bin/env bash
    set -euo pipefail

    start_pf() {
      local name="$1"
      local target="$2"
      local local_port="$3"
      local remote_port="$4"
      local probe_path="$5"
      local pidfile="$6"
      local logfile="$7"
      local session="$8"

      if curl -fsS --max-time 2 "http://127.0.0.1:${local_port}${probe_path}" >/dev/null 2>&1; then
        echo "==> ${name} already reachable on http://localhost:${local_port}"
        return 0
      fi

      command -v tmux >/dev/null || { echo "tmux not installed; brew install tmux" >&2; exit 2; }

      rm -f "$pidfile" "$logfile"
      echo "==> forwarding ${name}: http://localhost:${local_port}"
      tmux kill-session -t "$session" 2>/dev/null || true
      tmux new-session -d -s "$session" "
        while true; do
          date '+%Y-%m-%d %H:%M:%S starting ${name} port-forward' >>$logfile
          kubectl -n {{namespace}} port-forward $target ${local_port}:${remote_port} >>$logfile 2>&1
          date '+%Y-%m-%d %H:%M:%S ${name} port-forward exited; restarting' >>$logfile
          sleep 1
        done
      "
      tmux display-message -p -t "$session" '#{pane_pid}' > "$pidfile"
      for _ in $(seq 1 20); do
        if curl -fsS --max-time 2 "http://127.0.0.1:${local_port}${probe_path}" >/dev/null 2>&1; then
          return 0
        fi
        sleep 1
      done
      echo "port-forward for ${name} did not become ready; log: ${logfile}" >&2
      tail -n 30 "$logfile" >&2 || true
      exit 1
    }

    echo "==> enabling Centaur visibility stack"
    if ! helm status {{release}} -n {{namespace}} >/dev/null 2>&1; then
      just deploy
    fi
    helm dependency update {{chart}} >/dev/null
    visibility_args=(
      --set observability.enabled=true \
      --set api.victoriaMetricsPushEnabled=true \
      --set observability.grafana.anonymous.enabled=true
    )
    if [[ -n "${CENTAUR_VISIBILITY_API_IMAGE_TAG:-}" ]]; then
      visibility_args+=(
        --set "api.image.tag=${CENTAUR_VISIBILITY_API_IMAGE_TAG}"
        --set api.image.pullPolicy=IfNotPresent
      )
    fi
    helm upgrade --install {{release}} {{chart}} -n {{namespace}} --create-namespace --reuse-values "${visibility_args[@]}"

    echo "==> waiting for visibility pods"
    kubectl -n {{namespace}} wait --for=condition=ready pod -l app.kubernetes.io/component=victoriametrics --timeout=300s
    kubectl -n {{namespace}} wait --for=condition=ready pod -l app.kubernetes.io/component=victorialogs --timeout=300s
    kubectl -n {{namespace}} wait --for=condition=ready pod -l app.kubernetes.io/component=grafana --timeout=300s
    kubectl -n {{namespace}} wait --for=condition=ready pod -l app.kubernetes.io/component=otel-collector --timeout=300s
    kubectl -n {{namespace}} rollout status deploy/{{release}}-centaur-api --timeout=180s

    start_pf "Ops Console" "deploy/{{release}}-centaur-api" 8000 8000 /health {{visibility_ops_pid}} {{visibility_ops_log}} {{visibility_ops_session}}
    start_pf "Grafana" "svc/grafana" 3000 3000 /api/health {{visibility_grafana_pid}} {{visibility_grafana_log}} {{visibility_grafana_session}}
    start_pf "VictoriaMetrics" "svc/victoriametrics" 8428 8428 /health {{visibility_vm_pid}} {{visibility_vm_log}} {{visibility_vm_session}}
    start_pf "VictoriaLogs" "svc/victorialogs" 9428 9428 /health {{visibility_vl_pid}} {{visibility_vl_log}} {{visibility_vl_session}}

    encoded_key="$(kubectl -n {{namespace}} get secret centaur-infra-env -o jsonpath='{.data.LOCAL_DEV_API_KEY}' 2>/dev/null || true)"
    api_key=""
    if [[ -n "$encoded_key" ]]; then
      api_key="$(printf '%s' "$encoded_key" | base64 --decode 2>/dev/null || printf '%s' "$encoded_key" | base64 -D 2>/dev/null || true)"
    fi
    cache_bust="$(date +%s)"
    ops_url="http://localhost:8000/ops?visibility=${cache_bust}"
    if [[ -n "$api_key" ]]; then
      ops_url="${ops_url}#api_key=${api_key}"
    fi
    grafana_url="http://localhost:3000/d/centaur-overview/centaur-overview?orgId=1&refresh=30s"
    metrics_url="http://localhost:8428/vmui/"
    logs_url="http://localhost:9428/select/vmui/"

    echo
    echo "Ops Console:     http://localhost:8000/ops"
    echo "Grafana:         ${grafana_url}"
    echo "VictoriaMetrics: ${metrics_url}"
    echo "VictoriaLogs:    ${logs_url}"

    if command -v open >/dev/null 2>&1; then
      if command -v osascript >/dev/null 2>&1; then
        osascript <<'APPLESCRIPT' >/dev/null 2>&1 || true
    tell application "Google Chrome"
      repeat with w in windows
        set tabsToClose to {}
        repeat with t in tabs of w
          set u to URL of t
          if u contains "localhost:8000/ops" or u contains "localhost:3000" or u contains "localhost:8428" or u contains "localhost:9428" then
            set end of tabsToClose to t
          end if
        end repeat
        repeat with t in tabsToClose
          close t
        end repeat
      end repeat
    end tell
    APPLESCRIPT
      fi
      open "$ops_url" "$grafana_url" "$metrics_url" "$logs_url"
    fi

visibility-status:
    @kubectl -n {{namespace}} get pods,svc | rg 'victoria|grafana|otel|centaur-api|centaur-slackbot' || true

visibility-down:
    #!/usr/bin/env bash
    set -euo pipefail
    for session in {{visibility_ops_session}} {{visibility_grafana_session}} {{visibility_vm_session}} {{visibility_vl_session}}; do
      tmux kill-session -t "$session" 2>/dev/null || true
    done
    for pidfile in {{visibility_ops_pid}} {{visibility_grafana_pid}} {{visibility_vm_pid}} {{visibility_vl_pid}}; do
      if [[ -f "$pidfile" ]]; then
        pid="$(cat "$pidfile")"
        if kill -0 "$pid" 2>/dev/null; then
          echo "==> killing port-forward pid $pid"
          kill "$pid" 2>/dev/null || true
        fi
        rm -f "$pidfile"
      fi
    done
    rm -f {{visibility_ops_log}} {{visibility_grafana_log}} {{visibility_vm_log}} {{visibility_vl_log}}

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

pris-e2e channel="pris-test":
    #!/usr/bin/env bash
    set -euo pipefail
    API_DEPLOY="deploy/{{release}}-centaur-api"
    SLACKBOT_WEBHOOK_URL="http://{{release}}-centaur-slackbot:{{slack_local_port}}/api/webhooks/slack"
    CHANNEL="{{channel}}"
    CHANNEL="${CHANNEL#\#}"
    if [[ -z "$CHANNEL" ]]; then
      echo "Slack channel is required, for example: just pris-e2e pris-test" >&2
      exit 2
    fi

    decode_secret() {
      local key="$1"
      local encoded
      encoded="$(kubectl -n {{namespace}} get secret centaur-infra-env -o jsonpath="{.data.${key}}" 2>/dev/null || true)"
      if [[ -z "$encoded" ]]; then
        echo "centaur-infra-env is missing ${key}" >&2
        return 1
      fi
      printf '%s' "$encoded" | base64 --decode 2>/dev/null || printf '%s' "$encoded" | base64 -D
    }

    SLACK_BOT_TOKEN="$(decode_secret SLACK_BOT_TOKEN)"
    SLACK_SIGNING_SECRET="$(decode_secret SLACK_SIGNING_SECRET)"
    if [[ -z "$SLACK_BOT_TOKEN" || -z "$SLACK_SIGNING_SECRET" ]]; then
      echo "Slack bot token/signing secret are required for pris-e2e" >&2
      exit 2
    fi

    slack_api_json() {
      local method="$1"
      local payload="$2"
      curl -fsS "https://slack.com/api/${method}" \
        -H "Authorization: Bearer ${SLACK_BOT_TOKEN}" \
        -H "Content-Type: application/json; charset=utf-8" \
        --data "$payload"
    }

    slack_api_get() {
      local method="$1"
      shift
      curl -fsS -G "https://slack.com/api/${method}" \
        -H "Authorization: Bearer ${SLACK_BOT_TOKEN}" \
        "$@"
    }

    require_slack_ok() {
      local method="$1"
      local response="$2"
      if ! printf '%s\n' "$response" | jq -e '.ok == true' >/dev/null; then
        echo "Slack ${method} failed:" >&2
        printf '%s\n' "$response" | jq >&2
        exit 1
      fi
    }

    AUTH_TEST="$(slack_api_json auth.test '{}')"
    require_slack_ok auth.test "$AUTH_TEST"
    TEAM_ID="$(printf '%s\n' "$AUTH_TEST" | jq -r '.team_id')"
    BOT_USER_ID="$(printf '%s\n' "$AUTH_TEST" | jq -r '.user_id')"
    if [[ -z "$TEAM_ID" || "$TEAM_ID" == "null" || -z "$BOT_USER_ID" || "$BOT_USER_ID" == "null" ]]; then
      echo "auth.test did not return team_id and user_id" >&2
      printf '%s\n' "$AUTH_TEST" | jq >&2
      exit 1
    fi

    CHANNEL_ID=""
    if [[ -n "${PRIS_E2E_CHANNEL_ID:-}" ]]; then
      CHANNEL_ID="$PRIS_E2E_CHANNEL_ID"
    elif [[ "$CHANNEL" == "pris-test" ]]; then
      CHANNEL_ID="${PRIS_TEST_CHANNEL_ID:-C0B5G1HMAVD}"
    elif [[ "$CHANNEL" =~ ^[CGD][A-Z0-9]+$ ]]; then
      CHANNEL_ID="$CHANNEL"
    else
      cursor=""
      for _ in $(seq 1 20); do
        LIST_ARGS=(
          --data-urlencode "types=public_channel,private_channel"
          --data-urlencode "exclude_archived=true"
          --data-urlencode "limit=1000"
        )
        if [[ -n "$cursor" ]]; then
          LIST_ARGS+=(--data-urlencode "cursor=${cursor}")
        fi
        LIST_RESPONSE="$(slack_api_get conversations.list "${LIST_ARGS[@]}")"
        require_slack_ok conversations.list "$LIST_RESPONSE"
        CHANNEL_ID="$(printf '%s\n' "$LIST_RESPONSE" | jq -r --arg name "$CHANNEL" '.channels[] | select(.name == $name) | .id' | head -n1)"
        [[ -n "$CHANNEL_ID" ]] && break
        cursor="$(printf '%s\n' "$LIST_RESPONSE" | jq -r '.response_metadata.next_cursor // ""')"
        [[ -z "$cursor" ]] && break
      done
    fi
    if [[ -z "$CHANNEL_ID" ]]; then
      echo "Could not resolve Slack channel: ${CHANNEL}" >&2
      exit 1
    fi

    E2E_USER_ID="${PRIS_E2E_USER_ID:-}"
    if [[ -z "$E2E_USER_ID" ]]; then
      MEMBERS_RESPONSE="$(slack_api_get conversations.members \
        --data-urlencode "channel=${CHANNEL_ID}" \
        --data-urlencode "limit=200")"
      require_slack_ok conversations.members "$MEMBERS_RESPONSE"
      while read -r candidate_user_id; do
        [[ -z "$candidate_user_id" ]] && continue
        USER_RESPONSE="$(slack_api_get users.info --data-urlencode "user=${candidate_user_id}")"
        require_slack_ok users.info "$USER_RESPONSE"
        if printf '%s\n' "$USER_RESPONSE" | jq -e '.user.deleted != true and .user.is_bot != true and .user.id != "USLACKBOT"' >/dev/null; then
          E2E_USER_ID="$candidate_user_id"
          break
        fi
      done < <(printf '%s\n' "$MEMBERS_RESPONSE" | jq -r '.members[]')
    fi
    if [[ -z "$E2E_USER_ID" ]]; then
      echo "Could not resolve a real non-bot Slack user for #${CHANNEL}; set PRIS_E2E_USER_ID=U..." >&2
      exit 1
    fi

    NONCE="PRIS_E2E_$(date +%s)"
    EXPECTED="${NONCE} OK"
    PARENT_TEXT="${NONCE} parent: Pris runtime E2E in #${CHANNEL}; this thread intentionally seeds a stale suspended sandbox before invoking Pris."
    POST_RESPONSE="$(slack_api_json chat.postMessage "$(jq -cn --arg channel "$CHANNEL_ID" --arg text "$PARENT_TEXT" '{channel: $channel, text: $text}')")"
    require_slack_ok chat.postMessage "$POST_RESPONSE"
    PARENT_TS="$(printf '%s\n' "$POST_RESPONSE" | jq -r '.ts')"
    THREAD_KEY="slack:${TEAM_ID}:${CHANNEL_ID}:${PARENT_TS}"
    echo "==> posted E2E parent to #${CHANNEL} (${CHANNEL_ID}) ts=${PARENT_TS}"

    if [[ "${PRIS_E2E_SEED_SUSPENDED:-1}" != "0" ]]; then
      STALE_SANDBOX_ID="centaur-e2e-stale-$(printf '%s' "$NONCE" | tr '[:upper:]_' '[:lower:]-')"
      STALE_SANDBOX_ID="${STALE_SANDBOX_ID//_/-}"
      STALE_SANDBOX_ID="${STALE_SANDBOX_ID:0:63}"
      echo "==> seeding stale suspended sandbox row for ${THREAD_KEY}"
      kubectl exec -i -n {{namespace}} "$API_DEPLOY" -- \
        env THREAD_KEY="$THREAD_KEY" SANDBOX_ID="$STALE_SANDBOX_ID" /app/.venv/bin/python - <<'PY'
    import asyncio
    import os

    import asyncpg


    async def main() -> None:
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            await conn.execute("DELETE FROM sandbox_sessions WHERE thread_key = $1", os.environ["THREAD_KEY"])
            await conn.execute(
                """
                INSERT INTO sandbox_sessions (
                    thread_key, sandbox_id, harness, engine, state, started_at, updated_at
                ) VALUES ($1, $2, 'codex', 'codex', 'suspended', NOW(), NOW())
                """,
                os.environ["THREAD_KEY"],
                os.environ["SANDBOX_ID"],
            )
        finally:
            await conn.close()


    asyncio.run(main())
    PY
    fi

    EVENT_TEXT="<@${BOT_USER_ID}> Reply with exactly ${EXPECTED} and nothing else."
    EVENT_ID="Ev-pris-e2e-${NONCE}"
    EVENT_BODY="$(jq -cn \
      --arg team "$TEAM_ID" \
      --arg event_id "$EVENT_ID" \
      --arg user "$E2E_USER_ID" \
      --arg channel "$CHANNEL_ID" \
      --arg ts "$PARENT_TS" \
      --arg text "$EVENT_TEXT" \
      '{
        type: "event_callback",
        team_id: $team,
        event_id: $event_id,
        event: {
          type: "app_mention",
          user: $user,
          team: $team,
          channel: $channel,
          ts: $ts,
          event_ts: $ts,
          text: $text
        }
      }')"
    REQUEST_TS="$(date +%s)"
    SIGNATURE="$(SLACK_SIGNING_SECRET="$SLACK_SIGNING_SECRET" SLACK_REQUEST_TS="$REQUEST_TS" SLACK_EVENT_BODY="$EVENT_BODY" python3 -c 'import hashlib,hmac,os; base=("v0:%s:%s" % (os.environ["SLACK_REQUEST_TS"], os.environ["SLACK_EVENT_BODY"])).encode(); secret=os.environ["SLACK_SIGNING_SECRET"].encode(); print("v0=" + hmac.new(secret, base, hashlib.sha256).hexdigest())')"
    echo "==> dispatching signed app_mention to local slackbot for ${THREAD_KEY}"
    WEBHOOK_RESPONSE="$(printf '%s' "$EVENT_BODY" | kubectl exec -i -n {{namespace}} "$API_DEPLOY" -- \
      curl -sS -X POST "$SLACKBOT_WEBHOOK_URL" \
        -H "Content-Type: application/json" \
        -H "X-Slack-Request-Timestamp: ${REQUEST_TS}" \
        -H "X-Slack-Signature: ${SIGNATURE}" \
        --data-binary @-)"
    if ! printf '%s\n' "$WEBHOOK_RESPONSE" | jq -e '.ok == true' >/dev/null; then
      echo "Slackbot webhook did not accept event:" >&2
      printf '%s\n' "$WEBHOOK_RESPONSE" | jq >&2
      exit 1
    fi

    echo "==> waiting for Pris reply: ${EXPECTED}"
    for _ in $(seq 1 120); do
      REPLIES="$(slack_api_get conversations.replies \
        --data-urlencode "channel=${CHANNEL_ID}" \
        --data-urlencode "ts=${PARENT_TS}" \
        --data-urlencode "limit=100")"
      require_slack_ok conversations.replies "$REPLIES"
      FAILURE_TEXT="$(printf '%s\n' "$REPLIES" | jq -r '.messages[] | select(.ts != "'"$PARENT_TS"'") | .text // ""' | grep -F "Failed to start the runtime" || true)"
      if [[ -n "$FAILURE_TEXT" ]]; then
        echo "Pris runtime startup failed in #${CHANNEL}:" >&2
        printf '%s\n' "$FAILURE_TEXT" >&2
        exit 1
      fi
      MATCH_TS="$(printf '%s\n' "$REPLIES" | jq -r --arg expected "$EXPECTED" --arg parent "$PARENT_TS" '.messages[] | select(.ts != $parent) | select((.text // "") | contains($expected)) | .ts' | head -n1)"
      if [[ -n "$MATCH_TS" ]]; then
        echo "==> Pris E2E passed in #${CHANNEL}: parent_ts=${PARENT_TS} reply_ts=${MATCH_TS} expected=${EXPECTED}"
        exit 0
      fi
      sleep 2
    done

    echo "Timed out waiting for Pris reply in #${CHANNEL}. parent_ts=${PARENT_TS} expected=${EXPECTED}" >&2
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
