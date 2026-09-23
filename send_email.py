
import base64
import os
import smtplib
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Iterable

import msal
import requests


GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]
GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
ENV_FILE_PATH = Path(__file__).resolve().parent / ".env"


def _load_env_file_if_present() -> None:
    if not ENV_FILE_PATH.exists():
        return

    for raw_line in ENV_FILE_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file_if_present()


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def _acquire_access_token() -> str:
    tenant_id = _required_env("TENANT_ID")
    client_id = _required_env("CLIENT_ID")
    client_secret = _required_env("CLIENT_SECRET")
    authority = f"https://login.microsoftonline.com/{tenant_id}"

    app = msal.ConfidentialClientApplication(
        client_id,
        authority=authority,
        client_credential=client_secret,
    )
    result = app.acquire_token_for_client(scopes=GRAPH_SCOPE)

    if "access_token" not in result:
        error_description = result.get("error_description", str(result))
        raise RuntimeError(f"Could not acquire access token: {error_description}")

    return result["access_token"]


def _build_file_attachment(file_path: Path) -> dict:
    content = file_path.read_bytes()
    return {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": file_path.name,
        "contentType": "application/octet-stream",
        "contentBytes": base64.b64encode(content).decode("utf-8"),
    }


def send_email(
    to_emails: list[str],
    subject: str,
    body_html: str,
    attachment_paths: Iterable[Path] | None = None,
) -> None:
    access_token = _acquire_access_token()
    sender_email = _required_env("EMAIL_SENDER")
    sender_name = os.getenv("EMAIL_SENDER_NAME", sender_email)

    recipients = [
        {"emailAddress": {"address": email.strip()}}
        for email in to_emails
        if email.strip()
    ]

    attachments = []
    for path in attachment_paths or []:
        if not path.exists():
            raise FileNotFoundError(f"Attachment not found: {path}")
        attachments.append(_build_file_attachment(path))

    payload = {
        "message": {
            "subject": subject,
            "body": {
                "contentType": "HTML",
                "content": body_html,
            },
            "toRecipients": recipients,
            "from": {
                "emailAddress": {
                    "address": sender_email,
                    "name": sender_name,
                }
            },
        },
        "saveToSentItems": True,
    }

    # Include attachment data only when attachments are explicitly provided.
    if attachments:
        payload["message"]["attachments"] = attachments

    endpoint = f"{GRAPH_BASE_URL}/users/{sender_email}/sendMail"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    response = requests.post(endpoint, headers=headers, json=payload, timeout=30)
    if not response.ok:
        raise RuntimeError(
            f"Email send failed ({response.status_code}): {response.text}"
        )


def smtp_send_email(
    to_emails: list[str],
    subject: str,
    body_html: str,
    attachment_paths: Iterable[Path] | None = None,
) -> None:
    host = _required_env("SENDGRID_SMTP_HOST")
    port = int(os.getenv("SENDGRID_SMTP_PORT", "587"))
    username = _required_env("SENDGRID_SMTP_USERNAME")
    password = _required_env("SENDGRID_SMTP_PASSWORD")
    from_email = _required_env("SENDGRID_FROM_EMAIL")
    from_name = os.getenv("SENDGRID_FROM_NAME", from_email)
    reply_to = os.getenv("SENDGRID_REPLY_TO")

    recipients = [email.strip() for email in to_emails if email.strip()]
    if not recipients:
        raise ValueError("No recipients provided")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((from_name, from_email))
    message["To"] = ", ".join(recipients)
    if reply_to:
        message["Reply-To"] = reply_to
    message.set_content("This alert requires an HTML-capable email client.")
    message.add_alternative(body_html, subtype="html")

    for path in attachment_paths or []:
        if not path.exists():
            raise FileNotFoundError(f"Attachment not found: {path}")
        message.add_attachment(
            path.read_bytes(),
            maintype="application",
            subtype="octet-stream",
            filename=path.name,
        )

    # Port 465 uses implicit TLS; other ports (e.g. 587) upgrade via STARTTLS.
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as server:
            server.login(username, password)
            server.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.starttls()
            server.login(username, password)
            server.send_message(message)
