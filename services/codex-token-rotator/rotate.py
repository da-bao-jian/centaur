"""Rotate Codex ChatGPT-plan tokens.

Exchanges the refresh_token at https://auth.openai.com/oauth/token, persists
the rotated refresh_token *and* the fresh access_token back to 1Password, and
patches the access_token, id_token, and account_id into the centaur-infra-env
Kubernetes Secret. The access_token write to 1Password is what iron-proxy
reads (``op://<vault>/<access_token_item>/credential``) to substitute the
``Authorization`` header on outgoing chatgpt.com requests; the K8s Secret
write carries the id_token and account_id through to sandboxes via
KUBERNETES_SANDBOX_EXTRA_ENV.

The cron is the *only* OAuth actor — iron-proxy just rewrites
``placeholder-iron-proxy-overwrites-authorization`` to the access_token. This
avoids the concurrent-sandbox race where iron-proxy's in-process refresh_token
rotation diverges from 1Password.

Required env:
  OP_SERVICE_ACCOUNT_TOKEN  1Password service account token.
  OP_VAULT                  1Password vault name (default: ai-agents).
  CODEX_CLIENT_ID           ChatGPT OAuth client id.
  NAMESPACE                 Kubernetes namespace (default: read from pod).
  SECRET_NAME               Target Secret name (default: centaur-infra-env).
  REFRESH_TOKEN_OP_ITEM     1Password item holding the refresh_token
                            (default: CODEX_REFRESH_TOKEN). The credential
                            field is read and updated in place.
  ACCESS_TOKEN_OP_ITEM      1Password item to update with the rotated
                            access_token, read by iron-proxy
                            (default: CODEX_CHATGPT_ACCESS_TOKEN).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import sys
import urllib.parse
import urllib.request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("codex-token-rotator")


TOKEN_ENDPOINT = "https://auth.openai.com/oauth/token"
ACCOUNT_ID_CLAIM = "https://api.openai.com/auth"
KUBE_SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
KUBE_NS_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
KUBE_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        log.error("missing required env var: %s", name)
        sys.exit(2)
    return value or ""


def _op(*args: str) -> str:
    result = subprocess.run(
        ["op", *args],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ},
    )
    if result.returncode != 0:
        log.error("op %s failed (rc=%s): %s", args, result.returncode, result.stderr.strip())
        sys.exit(3)
    return result.stdout


def _read_op_credential(vault: str, item: str) -> str:
    out = _op("read", f"op://{vault}/{item}/credential")
    return out.strip()


def _write_op_credential(vault: str, item: str, value: str) -> None:
    _op("item", "edit", item, f"credential={value}", "--vault", vault)


def _exchange(refresh_token: str, client_id: str) -> dict:
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "scope": "openid profile email offline_access",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_ENDPOINT,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        log.error("token exchange failed: http %s %s", exc.code, err_body)
        sys.exit(4)
    for key in ("access_token", "refresh_token", "id_token"):
        if not payload.get(key):
            log.error("token exchange response missing %s; payload keys=%s", key, sorted(payload))
            sys.exit(5)
    return payload


def _account_id_from_id_token(id_token: str) -> str:
    try:
        _, payload_b64, _ = id_token.split(".", 2)
    except ValueError:
        log.error("id_token is not a JWT (no dots)")
        sys.exit(6)
    padding = "=" * (-len(payload_b64) % 4)
    payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
    claim = payload.get(ACCOUNT_ID_CLAIM) or {}
    account_id = claim.get("chatgpt_account_id")
    if not account_id:
        log.error("id_token missing chatgpt_account_id claim; claim=%s", claim)
        sys.exit(7)
    return account_id


def _patch_k8s_secret(namespace: str, name: str, data: dict[str, str]) -> None:
    token = open(KUBE_SA_TOKEN_PATH, encoding="utf-8").read().strip()
    encoded = {k: base64.b64encode(v.encode("utf-8")).decode("ascii") for k, v in data.items()}
    patch = json.dumps({"data": encoded}).encode("utf-8")
    url = f"https://kubernetes.default.svc/api/v1/namespaces/{namespace}/secrets/{name}"
    req = urllib.request.Request(
        url,
        data=patch,
        method="PATCH",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/strategic-merge-patch+json",
            "Accept": "application/json",
        },
    )
    import ssl

    ctx = ssl.create_default_context(cafile=KUBE_CA_PATH)
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        log.error("secret patch failed: http %s %s", exc.code, body)
        sys.exit(8)


def main() -> None:
    vault = _env("OP_VAULT", "ai-agents")
    client_id = _env("CODEX_CLIENT_ID", required=True)
    refresh_item = _env("REFRESH_TOKEN_OP_ITEM", "CODEX_REFRESH_TOKEN")
    access_item = _env("ACCESS_TOKEN_OP_ITEM", "CODEX_CHATGPT_ACCESS_TOKEN")
    secret_name = _env("SECRET_NAME", "centaur-infra-env")
    namespace = _env("NAMESPACE") or (
        open(KUBE_NS_PATH, encoding="utf-8").read().strip()
        if os.path.exists(KUBE_NS_PATH)
        else ""
    )
    if not namespace:
        log.error("could not determine namespace (set NAMESPACE or run in-cluster)")
        sys.exit(2)
    _env("OP_SERVICE_ACCOUNT_TOKEN", required=True)

    log.info("reading refresh_token from op://%s/%s/credential", vault, refresh_item)
    refresh_token = _read_op_credential(vault, refresh_item)

    log.info("exchanging refresh_token at %s", TOKEN_ENDPOINT)
    tokens = _exchange(refresh_token, client_id)
    new_refresh = tokens["refresh_token"]
    access_token = tokens["access_token"]
    id_token = tokens["id_token"]
    account_id = _account_id_from_id_token(id_token)

    if new_refresh != refresh_token:
        log.info("refresh_token rotated; writing back to 1Password")
        _write_op_credential(vault, refresh_item, new_refresh)
    else:
        log.info("refresh_token unchanged")

    log.info("writing access_token to op://%s/%s/credential", vault, access_item)
    _write_op_credential(vault, access_item, access_token)

    log.info("patching Secret %s/%s with access_token, id_token, account_id", namespace, secret_name)
    _patch_k8s_secret(
        namespace,
        secret_name,
        {
            "CODEX_USE_CHATGPT_AUTH": "1",
            "CODEX_CHATGPT_ACCESS_TOKEN": access_token,
            "CODEX_CHATGPT_ID_TOKEN": id_token,
            "CODEX_CHATGPT_ACCOUNT_ID": account_id,
        },
    )
    log.info("rotation complete")


if __name__ == "__main__":
    main()
