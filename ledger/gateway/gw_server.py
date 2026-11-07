"""Ledger Gateway HTTP service (ledger-gateway.openapi.yaml v1.1), standard library only.

  python3 -I gw_server.py [--env .env] [--port 8100] [--db gateway.db]

Environment (all can also be put in the env file): CHAIN_ID, CONTRACT_ADDRESS, GATEWAY_ACCOUNT_KEY, GATEWAY_RPC_URL
(default: first PINNED_NODE_URL), PINNED_NODE_URL (comma list, reported by GET /v1/chain), CONFIRMATION_DEPTH (2),
CORE_KEY, RULE_KEY, AGENT_KEY, AUDITOR_KEY (an empty key disables that caller), ANCHOR_BATCH_SECONDS (60),
ANCHOR_BATCH_EVENTS (50), NODE_LIST (node_id=org_id=url;...), ORG_IDS (default the three demo organizations).
"""
import argparse
import hmac
import json
import logging
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gw_chain import ChainClient, ChainError  # noqa: E402
from gw_engine import Engine, ValidationError  # noqa: E402
from gw_store import Store  # noqa: E402
from gw_views import NotFound, Views  # noqa: E402

log = logging.getLogger("gateway")
READERS = ("core", "rule", "agent", "auditor")
NOT_IMPLEMENTED = [re.compile(p) for p in (r"^/v1/proposals(/.*)?$", r"^/v1/finding-records(/.*)?$")]


def read_env_file(path):
    vals = {}
    if path and os.path.exists(path):
        for ln in open(path, encoding="utf-8").read().splitlines():
            m = re.match(r"^([A-Z0-9_]+)=(\S*)", ln)
            if m:
                vals[m.group(1)] = m.group(2)
    return vals


def load_config(env_path=None, environ=None):
    env = dict(read_env_file(env_path))
    env.update({k: v for k, v in (environ if environ is not None else os.environ).items() if v != ""})
    for need in ("CHAIN_ID", "CONTRACT_ADDRESS", "GATEWAY_ACCOUNT_KEY"):
        if not env.get(need):
            sys.exit(f"{need} is not set (run ledger/tools/setup_registry.py first, or put it in the env file)")
    pinned = [u.strip() for u in env.get("PINNED_NODE_URL", "").split(",") if u.strip()]
    rpc_url = env.get("GATEWAY_RPC_URL") or (pinned[0] if pinned else "http://127.0.0.1:8541")
    nodes = []
    for part in [p for p in env.get("NODE_LIST", "").split(";") if p]:
        node_id, org_id, url = part.split("=", 2)
        nodes.append({"node_id": node_id, "org_id": org_id, "url": url})
    return {
        "chain_id": env["CHAIN_ID"], "contract": env["CONTRACT_ADDRESS"], "key": env["GATEWAY_ACCOUNT_KEY"],
        "rpc_url": rpc_url, "pinned_urls": pinned, "depth": int(env.get("CONFIRMATION_DEPTH", "2")),
        "batch_seconds": int(env.get("ANCHOR_BATCH_SECONDS", "60")), "batch_events": int(env.get("ANCHOR_BATCH_EVENTS", "50")),
        "keys": {"core": env.get("CORE_KEY", ""), "rule": env.get("RULE_KEY", ""), "agent": env.get("AGENT_KEY", ""),
                 "auditor": env.get("AUDITOR_KEY", "")},
        "org_ids": [o for o in env.get("ORG_IDS", "org_commission,org_observer,org_neutral").split(",") if o],
        "nodes": nodes or [{"node_id": "gateway-node", "org_id": "org_neutral", "url": rpc_url}],
    }


def make_app(cfg, db_path):
    store = Store(db_path)
    chain = ChainClient(cfg["rpc_url"], cfg["chain_id"], cfg["contract"], cfg["key"], cfg["depth"])
    engine = Engine(store, chain, cfg["org_ids"], cfg["batch_seconds"], cfg["batch_events"])
    return engine, Views(engine, cfg)


def problem(handler, status, code, title, detail=None):
    body = {"type": "about:blank", "title": title, "status": status, "code": code}
    if detail:
        body["detail"] = detail
    handler.send_json(status, body, "application/problem+json")


def make_handler(cfg, engine, views):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "pv-ledger-gateway"

        def log_message(self, fmt, *args):         # no per-request access log (arrival times of voter events)
            pass

        def send_json(self, status, obj, ctype="application/json"):
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def caller(self):
            key = self.headers.get("X-Service-Key", "")
            for role, secret in cfg["keys"].items():
                if secret and key and hmac.compare_digest(key.encode(), secret.encode()):
                    return role
            return None

        def handle_any(self, method):
            try:
                u = urlparse(self.path)
                path, q = u.path, parse_qs(u.query)
                if path == "/v1/health" and method == "GET":
                    return self.send_json(200, views.health())
                role = self.caller()
                if role is None:
                    return problem(self, 401, "UNAUTHORIZED", "missing or unknown X-Service-Key")
                if any(p.match(path) for p in NOT_IMPLEMENTED):
                    return problem(self, 501, "INTERNAL_ERROR", "not implemented in M1",
                                   "proposals and finding records come with the week 4 and 5 milestones")
                if method == "POST" and path == "/v1/events":
                    if role != "core":
                        return problem(self, 403, "FORBIDDEN", "only the Core key may write events")
                    body = self.read_json()
                    events = body.get("events") if isinstance(body, dict) and set(body) == {"events"} else None
                    if not isinstance(events, list) or not 1 <= len(events) <= 500:
                        raise ValidationError("body must be {events: [1..500 items]}")
                    return self.send_json(200, {"results": engine.submit(events)})
                if method == "POST" and path == "/v1/verify":
                    ids = self.read_json().get("event_ids")
                    if not isinstance(ids, list) or not 1 <= len(ids) <= 1000:
                        raise ValidationError("event_ids must be a list of 1..1000 ids")
                    return self.send_json(200, {"results": views.verify(ids)})
                if method != "GET":
                    return problem(self, 405, "VALIDATION_ERROR", "method not allowed")
                return self.route_get(path, q)
            except ValidationError as e:
                problem(self, 400, "VALIDATION_ERROR", "invalid request", str(e))
            except NotFound as e:
                problem(self, 404, "NOT_FOUND", "not found", str(e))
            except ChainError as e:
                problem(self, 503, "LEDGER_UNAVAILABLE", "node not reachable", str(e))
            except Exception:
                log.exception("request failed")
                problem(self, 500, "INTERNAL_ERROR", "internal error")

        def read_json(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 8_000_000:
                raise ValidationError("body too large")
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                raise ValidationError("body is not JSON")

        @staticmethod
        def one(q, name, required=False):
            v = q.get(name, [None])[0]
            if required and not v:
                raise ValidationError(f"query parameter {name} is required")
            return v

        def page_args(self, q):
            out = {}
            lim = self.one(q, "limit")
            out["limit"] = max(1, min(1000, int(lim))) if lim else 100
            if self.one(q, "cursor"):
                out["cursor"] = self.one(q, "cursor")
            return out

        def route_get(self, path, q):
            m = None
            if path == "/v1/events":
                a = {"election_id": self.one(q, "election_id", True), "type": q.get("type", []), **self.page_args(q)}
                for name in ("checkpoint_max",):
                    if self.one(q, name) is not None:
                        a[name] = int(self.one(q, name))
                for name in ("from", "to", "anchor_status"):
                    if self.one(q, name):
                        a[name] = self.one(q, name)
                return self.send_json(200, views.list_events(a))
            m = re.match(r"^/v1/events/(ev[tr]_[0-9A-HJKMNP-TV-Z]{26})$", path)
            if m:
                return self.send_json(200, views.get_event(m.group(1)))
            m = re.match(r"^/v1/events/(ev[tr]_[0-9A-HJKMNP-TV-Z]{26})/proof$", path)
            if m:
                p = views.proof(m.group(1))
                if p is None:
                    return problem(self, 409, "CONFLICT", "not anchored yet")
                return self.send_json(200, p)
            if path == "/v1/rejections":
                a = {"election_id": self.one(q, "election_id", True), **self.page_args(q)}
                if self.one(q, "checkpoint_max") is not None:
                    a["checkpoint_max"] = int(self.one(q, "checkpoint_max"))
                if self.one(q, "reject_code"):
                    a["reject_code"] = self.one(q, "reject_code")
                return self.send_json(200, views.list_rejections(a))
            m = re.match(r"^/v1/rejections/(rej_[0-9A-HJKMNP-TV-Z]{26})$", path)
            if m:
                return self.send_json(200, views.get_rejection(m.group(1)))
            if path == "/v1/anchors":
                return self.send_json(200, views.list_anchors({"election_id": self.one(q, "election_id", True),
                                                               **self.page_args(q)}))
            m = re.match(r"^/v1/anchors/(anc_[0-9A-HJKMNP-TV-Z]{26})(/leaves)?$", path)
            if m:
                return self.send_json(200, views.anchor_leaves(m.group(1)) if m.group(2) else views.get_anchor(m.group(1)))
            if path == "/v1/chain":
                return self.send_json(200, views.chain_info())
            if path == "/v1/organizations":
                return self.send_json(200, views.organizations())
            m = re.match(r"^/v1/elections/(elc_[0-9A-HJKMNP-TV-Z]{26})/state$", path)
            if m:
                return self.send_json(200, views.election_state(m.group(1)))
            if path == "/v1/nodes":
                return self.send_json(200, views.nodes())
            return problem(self, 404, "NOT_FOUND", "no such path")

        def do_GET(self):
            self.handle_any("GET")

        def do_POST(self):
            self.handle_any("POST")

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default=".env")
    ap.add_argument("--port", type=int, default=int(os.environ.get("GATEWAY_PORT", "8100")))
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--db", default=os.environ.get("GATEWAY_DB", "gateway.db"))
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(a.env)
    engine, views = make_app(cfg, a.db)
    engine.start()
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(cfg, engine, views))
    srv.daemon_threads = True
    log.info("ledger gateway on %s:%d, chain %s contract %s, depth %d, node %s", a.host, a.port, cfg["chain_id"],
             cfg["contract"], cfg["depth"], cfg["rpc_url"])
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        engine.shutdown()


if __name__ == "__main__":
    main()
