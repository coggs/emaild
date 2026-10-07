"""Recognise one-time codes and sign-in links, and read their expiry from the text.

Pure functions (no DB) so they're easy to test. Deliberately conservative: bulk/promotional mail is never treated
as a one-time code (discount codes look similar), and neither is mail from a real person.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

_SUBJECT = re.compile(
    r"(\b(verification|security|secure|one[- ]?time|login|log[- ]?in|sign[- ]?in|confirmation|authentication|access|"
    r"2fa|two[- ]factor|single[- ]use|temporary)\b.{0,40}\b(code|link|pin|passcode|password)\b)"
    r"|(\b(code|link|pin)\b.{0,40}\b(sign[- ]?in|log[- ]?in|verif\w*|confirm\w*|authenticat\w*)\b)"
    r"|\b(otp|passcode|magic link)\b"
    r"|\byour .{0,30}\b(code|pin)\b"
    r"|\b(verify|confirm) (your )?(email|account|identity|sign[- ]?in|login)\b",
    re.I)
_BODY_HINT = re.compile(
    r"(one[- ]?time|verification|security|sign[- ]?in|log[- ]?in|login|authentication|confirm) (code|link|pin)"
    r"|\b(code|passcode|pin) (is|below)\b|\benter (this|the following) code\b"
    r"|\b(click|tap|use) (the|this) (link|button) (below )?to (sign|log) ?in\b|\bmagic link\b",
    re.I)
_CODE = re.compile(r"(?i:code|passcode|pin|otp)[^0-9A-Z]{0,30}\b([0-9]{4,8}|[0-9]{3}[- ][0-9]{3}|[A-Z0-9]{6,8})\b")
_EXPIRY = re.compile(
    r"(?:expire[sd]?|valid|good|active|usable)\b[^.\n]{0,40}?\b(\d{1,3})\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?)\b",
    re.I)
_UNIT = {"s": 1 / 60, "m": 1, "h": 60, "d": 1440}


# Wording only sign-in / verification mail uses (discount and promo codes never say these). Lets a code through even
# when the service sends it with newsletter-style headers (List-Unsubscribe etc.), which many now do.
_SIGN_IN = re.compile(
    r"\b(sign[- ]?in|log[- ]?in|login|verification|one[- ]?time|security|authentication|2fa|two[- ]factor)"
    r" (code|link|pin|passcode|password)\b"
    r"|\b(click|tap|use) (the|this) (link|button) (below )?to (sign|log) ?in\b|\bto (sign|log) ?in to\b"
    r"|\b(magic|secure) link\b|\bverify your (email|identity|account|sign[- ]?in)\b",
    re.I)


def looks_sign_in(subject: str, body: str) -> bool:
    """Stricter than looks_one_time: unmistakably a sign-in/verification email."""
    return looks_one_time(subject, body) and bool(_SIGN_IN.search(f"{subject or ''}\n{(body or '')[:1500]}"))


def looks_one_time(subject: str, body: str) -> bool:
    head = (body or "")[:1500]
    return bool(_SUBJECT.search(subject or "") and (_BODY_HINT.search(head) or _CODE.search(head)
                                                    or _EXPIRY.search(head)))


def expiry_minutes(text: str, default: int) -> int:
    m = _EXPIRY.search((text or "")[:3000])
    if not m:
        return default
    n, unit = int(m.group(1)), m.group(2).lower()[0]
    return max(1, int(round(n * _UNIT.get(unit, 1))))


def extract_code(text: str) -> str | None:
    """The code itself (for the opt-in Telegram display). Links are never extracted."""
    m = _CODE.search((text or "")[:3000])
    return m.group(1) if m else None


def expires_at(received_at: datetime, text: str, default_minutes: int) -> datetime:
    return received_at + timedelta(minutes=expiry_minutes(text, default_minutes))
