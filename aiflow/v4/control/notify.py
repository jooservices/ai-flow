"""Alerts to people: email now (Mailpit in the lab), Telegram, Slack and Teams through the same interface.

Channels are configured by environment on the control plane; an alert goes to every configured channel and
failures are logged, never raised, so a broken channel cannot stop the control loops.
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

import httpx

log = logging.getLogger("aiflow4.notify")
ICON = {"critical": "[CRITICAL]", "warning": "[WARNING]", "info": "[INFO]"}


def send(severity: str, title: str, detail: str = "", link: str = "", recipient: str = "") -> list[str]:
    sent: list[str] = []
    subject = f"{ICON.get(severity, '')} AI Flow: {title}"
    body = detail + (f"\n\n{link}" if link else "")
    host = os.environ.get("AIFLOW4_SMTP_HOST", "")
    if host:
        try:
            msg = EmailMessage()
            msg["From"] = os.environ.get("AIFLOW4_SMTP_FROM", "aiflow4@aiflow4.local")
            msg["To"] = recipient or os.environ.get("AIFLOW4_ALERT_EMAIL", "owner@aiflow4.local")
            msg["Subject"] = subject
            msg.set_content(body or title)
            with smtplib.SMTP(host, int(os.environ.get("AIFLOW4_SMTP_PORT", "25")), timeout=10) as s:
                s.send_message(msg)
            sent.append("email")
        except Exception as e:  # noqa: BLE001
            log.warning("email alert failed: %s", e)
    tg_token, tg_chat = os.environ.get("AIFLOW4_TELEGRAM_BOT_TOKEN", ""), os.environ.get("AIFLOW4_TELEGRAM_CHAT_ID", "")
    if tg_token and tg_chat:
        try:
            httpx.post(f"https://api.telegram.org/bot{tg_token}/sendMessage",
                       json={"chat_id": tg_chat, "text": f"{subject}\n{body}"[:4000]}, timeout=10).raise_for_status()
            sent.append("telegram")
        except Exception as e:  # noqa: BLE001
            log.warning("telegram alert failed: %s", e)
    for name in ("SLACK", "TEAMS"):
        url = os.environ.get(f"AIFLOW4_{name}_WEBHOOK", "")
        if url:
            try:
                httpx.post(url, json={"text": f"{subject}\n{body}"[:4000]}, timeout=10).raise_for_status()
                sent.append(name.lower())
            except Exception as e:  # noqa: BLE001
                log.warning("%s alert failed: %s", name.lower(), e)
    return sent
