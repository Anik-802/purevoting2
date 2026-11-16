"""Link emails after tokens:issue (delivery EMAIL, D-16 note): one plain-text email per newly issued voter that has an
address, over one STARTTLS SMTP connection per batch. Runs after the issuance is committed and outside the election
lock. Addresses, names and links never reach logs, alerts, events or errors; the caller gets counts and voter_keys."""
import logging
import smtplib
import time
from datetime import datetime, timedelta, timezone
from email.errors import MessageError
from email.message import EmailMessage

from app.config import settings

log = logging.getLogger("core.mailer")
KST = timezone(timedelta(hours=9))
TIMEOUT = 20        # seconds per SMTP operation at most
DEADLINE = 45       # seconds for the whole batch: the response must beat nginx's proxy_read_timeout (60 s)
MAX_RECONNECTS = 2  # lost connections reopened per batch; then the rest fail, so the account never sees a login storm
_clock = time.monotonic


def configured() -> bool:
    return bool(settings.smtp_user and settings.smtp_password)


def _kst(t: int) -> str:
    return datetime.fromtimestamp(t, KST).strftime("%Y-%m-%d %H:%M")


def message(e: dict, row: dict) -> EmailMessage:
    m = EmailMessage()
    m["Subject"] = f"[PureVoting] {e['title']} 투표 링크"
    m["From"] = settings.mail_from or settings.smtp_user
    m["To"] = row["email"]
    m.set_content(
        f"{row['name']}님, 안녕하세요.\n\n"
        f"'{e['title']}' 투표 링크를 보내 드려요.\n\n"
        f"투표 기간: {_kst(e['window_start'])} ~ {_kst(e['window_end'])} (한국 시간)\n"
        f"투표 링크: {row['link']}\n\n"
        f"이 링크는 {row['name']}님만 쓰는 1회용 링크예요. 링크를 가진 사람은 누구나 한 번 투표할 수 있으니 "
        "다른 사람에게 전달하지 마세요.\n"
        "메일이 안 보이면 스팸함도 확인해 주세요.\n")
    return m


class _Failed(Exception):
    def __init__(self, problem: str):
        self.problem = problem


def _connect(timeout: float) -> smtplib.SMTP:
    """CONNECT_FAILED (no connection or STARTTLS) or LOGIN_FAILED; the server's text is dropped (it may quote the
    account)."""
    try:
        smtp = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=timeout)
    except Exception:   # noqa: BLE001
        raise _Failed("CONNECT_FAILED") from None
    stage = "CONNECT_FAILED"
    try:
        smtp.starttls()
        stage = "LOGIN_FAILED"
        smtp.login(settings.smtp_user, settings.smtp_password)
    except Exception:   # noqa: BLE001
        _close(smtp)
        raise _Failed(stage) from None
    return smtp


def _close(smtp: smtplib.SMTP | None) -> None:
    if smtp is not None:
        try:
            smtp.quit()
        except Exception:   # noqa: BLE001 - the connection is gone either way
            smtp.close()


def _lost(smtp: smtplib.SMTP, exc: Exception) -> bool:
    """True when the connection cannot carry the next message: smtplib closed the socket, or the server answered 421
    (closing) on the recipient, sender or data step."""
    if getattr(smtp, "sock", None) is None:
        return True
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return any(code == 421 for code, _ in exc.recipients.values())
    if isinstance(exc, smtplib.SMTPResponseException):
        return exc.smtp_code == 421
    return not isinstance(exc, (ValueError, MessageError))


def send_links(e: dict, rows: list[dict]) -> dict:
    """rows: issue_tokens rows (voter_key, name, email, link). Returns the core-api LinkEmailSummary. A refused
    recipient or message keeps the connection; a lost one is reopened for the next voter at most MAX_RECONNECTS
    times; a failed connect or login, the DEADLINE or too many lost connections end the batch, and every voter not
    sent counts as failed (`problem` says why; they get their link from the CSV)."""
    todo = [r for r in rows if r.get("email")]
    out = {"configured": configured(), "sent": 0, "failed": 0, "no_address": len(rows) - len(todo),
           "failed_voter_keys": [], "problem": None}
    if not out["configured"]:
        return out
    end = _clock() + DEADLINE
    smtp, lost = None, 0
    for i, r in enumerate(todo):
        left = end - _clock()
        if left <= 0 or (smtp is None and lost > MAX_RECONNECTS):
            out["problem"] = "TIME_LIMIT" if left <= 0 else "TOO_MANY_DISCONNECTS"
            out["failed_voter_keys"] += [v["voter_key"] for v in todo[i:]]
            break
        try:
            if smtp is None:
                smtp = _connect(min(TIMEOUT, left))
            elif smtp.sock is not None:
                smtp.sock.settimeout(min(TIMEOUT, left))
        except _Failed as f:
            out["problem"] = f.problem
            out["failed_voter_keys"] += [v["voter_key"] for v in todo[i:]]
            break
        try:
            smtp.send_message(message(e, r))
            out["sent"] += 1
        except Exception as exc:   # noqa: BLE001 - never echo it: smtplib errors quote the addresses
            out["failed_voter_keys"].append(r["voter_key"])
            if _lost(smtp, exc):
                _close(smtp)
                smtp, lost = None, lost + 1
    _close(smtp)
    out["failed"] = len(out["failed_voter_keys"])
    if out["failed"]:   # counts and the stage only, never who
        log.warning("link emails: %d of %d not sent (%s)", out["failed"], len(todo), out["problem"] or "refused")
    return out
