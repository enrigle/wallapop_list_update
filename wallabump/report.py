"""Run results, the receipt email, and the Keychain credentials it uses."""

from __future__ import annotations

import logging
import re
import smtplib
import subprocess
from dataclasses import dataclass, field
from email.message import EmailMessage

from .logic import LOG_FILE

log = logging.getLogger("wallabump")


KEYCHAIN_SERVICE = "wallabump-gmail"

# --- Results ------------------------------------------------------------------


@dataclass
class Item:
    href: str
    title: str
    reserved: bool
    sold: bool


@dataclass
class Result:
    site: str
    title: str
    status: str  # ok | dry-run | skipped | unverified | failed
    reason: str = ""


@dataclass
class RunReport:
    results: list[Result] = field(default_factory=list)
    fatal: str = ""

    def count(self, status: str) -> int:
        return sum(1 for r in self.results if r.status == status)

    @property
    def subject(self) -> str:
        if self.fatal:
            return f"Wallabump: FAILED — {self.fatal}"
        ok = self.count("ok") + self.count("dry-run")
        subject = (
            f"Wallabump: {ok} ok, "
            f"{self.count('skipped')} skipped, "
            f"{self.count('unverified')} unverified, "
            f"{self.count('failed')} failed"
        )
        # Keep the re-auth signal readable from the lock screen even when only
        # one of several sites has lost its session.
        if any("RE-AUTH" in r.reason for r in self.results):
            subject += " — RE-AUTH NEEDED"
        return subject

    @property
    def body(self) -> str:
        lines = [self.fatal] if self.fatal else []
        lines += [
            f"{r.site:<9} {r.status:<11} {r.title}"
            + (f"  — {r.reason}" if r.reason else "")
            for r in self.results
        ]
        if not lines:
            lines.append("No listings found.")
        lines += ["", f"Log: {LOG_FILE}"]
        return "\n".join(lines)

    @property
    def exit_code(self) -> int:
        if self.fatal:
            return 2
        return 1 if self.count("failed") or self.count("unverified") else 0


# --- Keychain + email ---------------------------------------------------------


def _security(*args: str) -> str | None:
    proc = subprocess.run(
        ["/usr/bin/security", *args], capture_output=True, text=True, check=False
    )
    return proc.stdout if proc.returncode == 0 else None


def gmail_credentials() -> tuple[str, str] | None:
    """Return (address, app_password) from the Keychain, or None if absent.

    The Keychain item stores the Gmail address as its account, so one entry
    carries both halves:
        security add-generic-password -a you@gmail.com -s wallabump-gmail -w
    """
    password = _security("find-generic-password", "-s", KEYCHAIN_SERVICE, "-w")
    metadata = _security("find-generic-password", "-s", KEYCHAIN_SERVICE)
    if password is None or metadata is None:
        return None
    account = re.search(r'"acct"<blob>="([^"]+)"', metadata)
    if account is None:
        return None
    return account.group(1), password.strip()


def send_email(report: RunReport) -> None:
    """Email the receipt. Never raises — a dead SMTP must not hide the run."""
    credentials = gmail_credentials()
    if credentials is None:
        log.error(
            "No Keychain item '%s'; cannot email. Create it with: "
            "security add-generic-password -a you@gmail.com -s %s -w",
            KEYCHAIN_SERVICE,
            KEYCHAIN_SERVICE,
        )
        return

    address, password = credentials
    message = EmailMessage()
    message["Subject"] = report.subject
    message["From"] = address
    message["To"] = address
    message.set_content(report.body)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.login(address, password)
            smtp.send_message(message)
        log.info("Emailed report to %s", address)
    except (smtplib.SMTPException, OSError) as exc:
        log.error("Email failed: %s", exc)
