"""Messages to the owner (JAIF-74): a small interface so channels can be added. A message holds the question and the ticket link only,
never a secret. With no channel configured nothing is sent."""
from __future__ import annotations

import json
import smtplib
import urllib.request
from email.message import EmailMessage
from typing import Protocol


class Notifier(Protocol):
    def send(self, text: str) -> None: ...


class Telegram:
    def __init__(self, token: str, chat: str) -> None:
        self.token, self.chat = token, chat

    def send(self, text: str) -> None:
        request = urllib.request.Request(f"https://api.telegram.org/bot{self.token}/sendMessage", method="POST",
                                         data=json.dumps({"chat_id": self.chat, "text": text[:3900]}).encode(),
                                         headers={"Content-Type": "application/json"})
        urllib.request.urlopen(request, timeout=20).close()


class Smtp:
    def __init__(self, host: str, port: int, sender: str, to: str, user: str = "", password: str = "") -> None:
        self.host, self.port, self.sender, self.to, self.user, self.password = host, port, sender, to, user, password

    def send(self, text: str) -> None:
        message = EmailMessage()
        message["Subject"], message["From"], message["To"] = "AI Flow: the owner is needed", self.sender, self.to
        message.set_content(text)
        with smtplib.SMTP(self.host, self.port, timeout=20) as server:
            server.starttls()
            if self.user:
                server.login(self.user, self.password)
            server.send_message(message)


def from_env(env: dict[str, str]) -> Notifier | None:
    """The channel named by AIFLOW_NOTIFY_CHANNEL (telegram | smtp) when all its settings exist, else None."""
    channel = env.get("AIFLOW_NOTIFY_CHANNEL", "").strip().lower()
    if channel == "telegram" and env.get("AIFLOW_NOTIFY_TELEGRAM_TOKEN") and env.get("AIFLOW_NOTIFY_TELEGRAM_CHAT"):
        return Telegram(env["AIFLOW_NOTIFY_TELEGRAM_TOKEN"], env["AIFLOW_NOTIFY_TELEGRAM_CHAT"])
    if channel == "smtp" and all(env.get(name) for name in ("AIFLOW_NOTIFY_SMTP_HOST", "AIFLOW_NOTIFY_SMTP_FROM", "AIFLOW_NOTIFY_SMTP_TO")):
        return Smtp(env["AIFLOW_NOTIFY_SMTP_HOST"], int(env.get("AIFLOW_NOTIFY_SMTP_PORT", "587")), env["AIFLOW_NOTIFY_SMTP_FROM"],
                    env["AIFLOW_NOTIFY_SMTP_TO"], env.get("AIFLOW_NOTIFY_SMTP_USER", ""), env.get("AIFLOW_NOTIFY_SMTP_PASSWORD", ""))
    return None
