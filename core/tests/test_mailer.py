"""Link emails (app/mailer.py) against a fake smtplib.SMTP: no network, no real mail."""
import logging
import smtplib

import pytest

from app import mailer
from app.config import settings

E = {"title": "학생회장 선거", "window_start": 1_794_960_000, "window_end": 1_794_992_400}   # 09:00-18:00 KST


class FakeSock:
    def __init__(self):
        self.timeouts = []

    def settimeout(self, t):
        self.timeouts.append(t)


class FakeSMTP:
    """Records what a real connection would send. `refuse` = address → SMTP code the server answers (421 closes the
    socket, as smtplib does), `drop_after` = messages per connection before it is lost, `connect_fails` /
    `login_fails` = the server or the account is unusable, `cost` = seconds each message takes on the fake clock."""
    instances: list["FakeSMTP"] = []
    refuse: dict[str, int] = {}
    drop_after: int | None = None
    connect_fails = login_fails = False
    cost = 0.0
    now = 0.0

    def __init__(self, host, port, timeout=None):
        if FakeSMTP.connect_fails:
            raise OSError("connection refused")
        self.host, self.port, self.timeout = host, port, timeout
        self.tls = self.logged_in = False
        self.sock = FakeSock()
        self.sent = []
        FakeSMTP.instances.append(self)

    def starttls(self):
        self.tls = True

    def login(self, user, password):
        if FakeSMTP.login_fails:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")
        self.logged_in = True

    def send_message(self, msg):
        assert self.tls and self.logged_in and self.sock is not None
        FakeSMTP.now += FakeSMTP.cost
        if FakeSMTP.drop_after is not None and len(self.sent) >= FakeSMTP.drop_after:
            self.sock = None
            raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
        code = FakeSMTP.refuse.get(msg["To"])
        if code:
            if code == 421:
                self.sock = None
            raise smtplib.SMTPRecipientsRefused({msg["To"]: (code, b"refused")})
        self.sent.append(msg)

    def quit(self):
        if self.sock is None:
            raise smtplib.SMTPServerDisconnected("please run connect() first")
        self.sock = None

    def close(self):
        self.sock = None


def reset_fake():
    FakeSMTP.instances, FakeSMTP.refuse, FakeSMTP.drop_after = [], {}, None
    FakeSMTP.connect_fails = FakeSMTP.login_fails = False
    FakeSMTP.cost = FakeSMTP.now = 0.0


@pytest.fixture()
def smtp(monkeypatch):
    reset_fake()
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mailer, "_clock", lambda: FakeSMTP.now)
    monkeypatch.setattr(settings, "smtp_user", "pv.test@gmail.com")
    monkeypatch.setattr(settings, "smtp_password", "app-password")
    monkeypatch.setattr(settings, "mail_from", "")
    return FakeSMTP


def rows(n, without=()):
    return [{"voter_key": f"vtr_{i}", "external_ref": f"s{i}", "name": f"투표자{i}", "link": f"https://pv/vote/e#t=tok{i}",
             "email": None if i in without else f"v{i}@example.org"} for i in range(n)]


def sent(fake):
    return [m for c in fake.instances for m in c.sent]


def summary(sent=0, failed=(), no_address=0, problem=None, configured=True):
    return {"configured": configured, "sent": sent, "failed": len(failed), "no_address": no_address,
            "failed_voter_keys": list(failed), "problem": problem}


def test_not_configured_sends_nothing(smtp, monkeypatch):
    monkeypatch.setattr(settings, "smtp_password", "")
    assert mailer.send_links(E, rows(3, without={1})) == summary(no_address=1, configured=False)
    assert smtp.instances == []


def test_one_connection_per_batch_and_the_message(smtp):
    out = mailer.send_links(E, rows(3, without={1}))
    assert out == summary(sent=2, no_address=1)
    assert len(smtp.instances) == 1
    conn = smtp.instances[0]
    assert (conn.host, conn.port, conn.timeout, conn.tls, conn.sock) == ("smtp.gmail.com", 587, 20, True, None)
    m = conn.sent[0]
    assert m["To"] == "v0@example.org" and m["From"] == "pv.test@gmail.com"
    assert m["Subject"] == "[PureVoting] 학생회장 선거 투표 링크"
    body = m.get_content()
    for part in ("투표자0님", "학생회장 선거", "https://pv/vote/e#t=tok0", "2026-11-18 09:00 ~ 2026-11-18 18:00 (한국 시간)",
                 "다른 사람에게 전달하지 마세요", "메일이 안 보이면 스팸함도 확인해 주세요"):
        assert part in body
    assert [x["To"] for x in sent(smtp)] == ["v0@example.org", "v2@example.org"]


def test_refused_recipient_is_counted_and_the_batch_goes_on(smtp, caplog):
    smtp.refuse = {"v1@example.org": 550}
    with caplog.at_level(logging.WARNING):
        out = mailer.send_links(E, rows(3))
    assert out == summary(sent=2, failed=["vtr_1"])
    assert len(smtp.instances) == 1                         # a refused recipient keeps the connection
    # no address, name, link or token in the log
    assert "1 of 3" in caplog.text
    for secret in ("example.org", "투표자", "tok", "vtr_"):
        assert secret not in caplog.text


def test_lost_connection_is_reopened_for_the_next_voter(smtp):
    smtp.drop_after = 1
    assert mailer.send_links(E, rows(3)) == summary(sent=2, failed=["vtr_1"])
    assert len(smtp.instances) == 2


def test_421_counts_as_a_lost_connection(smtp, monkeypatch):
    smtp.refuse = {"v1@example.org": 421}                  # smtplib closes the socket and raises RecipientsRefused
    assert mailer.send_links(E, rows(3)) == summary(sent=2, failed=["vtr_1"])
    assert len(smtp.instances) == 2

    # a 421 on the sender step while the socket still looks open: reconnect as well
    reset_fake()
    refused = []

    def refuse_once(self, msg):
        if not refused:
            refused.append(msg)
            raise smtplib.SMTPSenderRefused(421, b"4.7.0 try again later", "pv.test@gmail.com")
        self.sent.append(msg)
    monkeypatch.setattr(FakeSMTP, "send_message", refuse_once)
    assert mailer.send_links(E, rows(2)) == summary(sent=1, failed=["vtr_0"])
    assert len(smtp.instances) == 2


def test_reconnects_are_capped(smtp):
    smtp.drop_after = 0                                     # every connection is lost on its first message
    out = mailer.send_links(E, rows(6))
    assert out == summary(failed=[f"vtr_{i}" for i in range(6)], problem="TOO_MANY_DISCONNECTS")
    assert len(smtp.instances) == 1 + mailer.MAX_RECONNECTS  # never more logins than that


def test_connect_and_login_failures_end_the_batch(smtp):
    smtp.connect_fails = True
    assert mailer.send_links(E, rows(2)) == summary(failed=["vtr_0", "vtr_1"], problem="CONNECT_FAILED")
    reset_fake()
    smtp.login_fails = True
    out = mailer.send_links(E, rows(3, without={0}))
    assert out == summary(failed=["vtr_1", "vtr_2"], no_address=1, problem="LOGIN_FAILED")
    assert len(smtp.instances) == 1 and smtp.instances[0].sock is None     # one login attempt only


def test_deadline_stops_the_batch(smtp):
    smtp.cost = 10.0                                        # 10 s per message on the fake clock, deadline 45 s
    out = mailer.send_links(E, rows(8))
    assert out == summary(sent=5, failed=["vtr_5", "vtr_6", "vtr_7"], problem="TIME_LIMIT")
    assert len(smtp.instances) == 1 and smtp.instances[0].sock is None


def test_per_message_timeout_follows_the_time_left(smtp, monkeypatch):
    smtp.cost = 10.0
    socks = []
    init = FakeSMTP.__init__

    def keep_sock(self, *a, **k):
        init(self, *a, **k)
        socks.append(self.sock)
    monkeypatch.setattr(FakeSMTP, "__init__", keep_sock)
    mailer.send_links(E, rows(5))
    assert socks[0].timeouts == [20, 20, 15, 5]           # 35, 25, 15 and 5 s left before messages 2..5


def test_mail_from_overrides_the_account(smtp, monkeypatch):
    monkeypatch.setattr(settings, "mail_from", "PureVoting <pv.test@gmail.com>")
    mailer.send_links(E, rows(1))
    assert sent(smtp)[0]["From"] == "PureVoting <pv.test@gmail.com>"
