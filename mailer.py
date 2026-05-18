import os
import time
import requests

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TOKEN_ENDPOINT_TPL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

_token_cache = {"value": None, "expires_at": 0.0}


def _get_graph_token():
    now = time.time()
    if _token_cache["value"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["value"]

    tenant = os.environ["MS_TENANT_ID"]
    resp = requests.post(
        TOKEN_ENDPOINT_TPL.format(tenant=tenant),
        data={
            "client_id": os.environ["MS_CLIENT_ID"],
            "client_secret": os.environ["MS_CLIENT_SECRET"],
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        },
        timeout=10,
    )
    resp.raise_for_status()
    body = resp.json()
    _token_cache["value"] = body["access_token"]
    _token_cache["expires_at"] = now + body.get("expires_in", 3600)
    return _token_cache["value"]


def send_email(to_address, subject, html_body, text_body=None):
    """Envia um email. Backend definido pela env var MAIL_BACKEND.

    - 'console' (default): imprime no stdout — útil em dev.
    - 'graph': envia via Microsoft Graph API usando client_credentials.
      Requer MS_TENANT_ID, MS_CLIENT_ID, MS_CLIENT_SECRET e MS_SENDER.
    """
    backend = os.environ.get("MAIL_BACKEND", "console").lower()

    if backend == "console":
        print("\n===== EMAIL (console) =====", flush=True)
        print(f"To: {to_address}", flush=True)
        print(f"Subject: {subject}", flush=True)
        print((text_body or html_body), flush=True)
        print("===========================\n", flush=True)
        return

    if backend != "graph":
        raise RuntimeError(f"MAIL_BACKEND desconhecido: {backend!r}")

    sender = os.environ["MS_SENDER"]
    token = _get_graph_token()
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": [{"emailAddress": {"address": to_address}}],
        },
        "saveToSentItems": "false",
    }
    resp = requests.post(
        f"{GRAPH_BASE}/users/{sender}/sendMail",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=15,
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"Graph sendMail falhou: {resp.status_code} {resp.text}"
        )
