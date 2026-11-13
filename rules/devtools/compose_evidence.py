"""Run the isolated developer Docker smoke; export safe evidence, never credentials.

From a Windows PowerShell in the repo: py -3.11 rules/devtools/compose_evidence.py
Requires a local Linux-container Docker daemon and Docker Compose v2.24.4+.
"""
import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "rules" / "devtools"
COUNTERS = ("elections", "evaluated", "skipped", "alerts", "read_errors", "alert_errors", "stored", "retry", "rejected")
EXPECTED = {"R1": "PASS", "R2": "PASS", "R3": "PASS", "R4": "INCONCLUSIVE", "R5": "INCONCLUSIVE"}


class RunError(Exception):
    """Only fixed diagnostics, never a raw command, HTTP response, or secret."""


def command(args, env, *, source=None, timeout=90, label="Docker command"):
    try:
        p = subprocess.run(args, cwd=ROOT, env=env, input=source, capture_output=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise RunError(f"{label}: executable not found (is Docker Desktop installed?)") from None
    except subprocess.TimeoutExpired:
        raise RunError(f"{label}: timed out after {timeout} seconds") from None
    if p.returncode:
        # Docker output can contain environment values; don't print or save it.
        detail = ""
        for item in ("Is the docker daemon running", "Cannot connect to the Docker daemon",
                     "error during connect", "no matching manifest", "failed to solve", "unhealthy",
                     "unexpected HTTP status", "timed out: all five stored rule findings",
                     "timed out: Core creation check", "timed out: scheduled opening"):
            if item.lower() in p.stderr.lower():
                detail = " (" + item + ")"
                break
        raise RunError(f"{label}: exit {p.returncode}{detail}")
    return p.stdout.strip()


def poll_rows(output):
    for line in output.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and all(type(row.get(k)) is int and row[k] >= 0 for k in COUNTERS):
            yield {k: row[k] for k in COUNTERS}


def smoke_row(output):
    for line in reversed(output.splitlines()):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if (isinstance(r, dict) and r.get("smoke") == "developer HTTP check" and r.get("core") == "real"
                and r.get("gateway") == "development stand-in" and r.get("official_T01_run") is False
                and type(r.get("checkpoint")) is int and r["checkpoint"] >= 0
                and r.get("matches_smoke_expectation") is True and r.get("results") == EXPECTED
                and type(r.get("ballots_accepted")) is int and r["ballots_accepted"] == 1
                and type(r.get("findings_stored")) is int and r["findings_stored"] == 5):
            return {k: r[k] for k in ("smoke", "core", "gateway", "ballots_accepted", "checkpoint",
                                      "findings_stored", "results", "matches_smoke_expectation", "official_T01_run")}
    raise RunError("Missing or unexpected final worker_smoke.py JSON; no passing evidence")


def execute(report):
    # Do not use the team's .env, project, volumes, ports, credentials, or real node.
    env = os.environ.copy()
    for key in re.findall(r"^([A-Z][A-Z0-9_]+)=", (ROOT / ".env.example").read_text(encoding="utf-8"), re.M):
        env.pop(key, None)
    for key in list(env):
        if key.startswith("COMPOSE_"):
            env.pop(key)
    for key in ("POSTGRES_SUPERUSER_PASSWORD", "DB_CORE_PASSWORD", "STAFF_ADMIN_PASSWORD", "CORE_KEY", "RULE_KEY",
                "GATEWAY_KEY", "AGENT_KEY", "JWT_SECRET"):
        env[key] = secrets.token_hex(24)
    env.update(STAFF_ADMIN_USERNAME="smokeadmin", CHAIN_ID="31337", CONTRACT_ADDRESS="0x" + secrets.token_hex(20),
               RULE_ENGINE_SIGNER_KEY="0x" + format(1 + secrets.randbelow(2**255 - 1), "064x"),
               PINNED_NODE_URL="http://ledger-gateway:8100/rpc", CONFIRMATION_DEPTH="2", RULE_POLL_SECONDS="5")
    # Compose interpolates the real Gateway's required variable before merging overrides.
    # This fresh dummy satisfies interpolation only: the private overlay removes it from
    # the stand-in's environment and removes all real-chain/bootstrap dependencies.
    env["GATEWAY_ACCOUNT_KEY"] = "0x" + format(1 + secrets.randbelow(2**255 - 1), "064x")

    # Don't accidentally use an SSH/cloud daemon selected for unrelated work.
    endpoint = env.get("DOCKER_HOST") if not env.get("DOCKER_CONTEXT") else None
    if not endpoint:
        endpoint = command(["docker", "context", "inspect", "--format", '{{(index .Endpoints "docker").Host}}'],
                           env, label="Docker context lookup")
    if not endpoint.lower().startswith(("npipe://", "unix://")):
        raise RunError("Select a LOCAL Docker Desktop/Linux context (named pipe or Unix socket)")
    if command(["docker", "info", "--format", "{{.OSType}}"], env, label="Docker daemon check") != "linux":
        raise RunError("Switch Docker Desktop to Linux containers")
    version = command(["docker", "compose", "version", "--short"], env, label="Compose version")
    match = re.match(r"v?(\d+)\.(\d+)\.(\d+)", version)
    if not match or tuple(map(int, match.groups())) < (2, 24, 4):
        raise RunError("Docker Compose v2.24.4 or newer is required")
    report["compose_version"] = ".".join(match.groups())
    try:
        commit = command(["git", "rev-parse", "HEAD"], env, label="Git revision")
        if re.fullmatch(r"[a-f0-9]{40,64}", commit):
            report["git_commit"] = commit
            report["tracked_changes_present"] = bool(command(["git", "status", "--porcelain", "--untracked-files=no"],
                                                            env, label="Git tracked changes"))
    except RunError:
        pass  # Running from a source snapshot is also supported.
    report["worker_smoke_sha256"] = hashlib.sha256((TOOLS / "worker_smoke.py").read_bytes()).hexdigest()
    project = "pv-anik-smoke-" + uuid.uuid4().hex[:12]
    report["compose_project"] = project
    with tempfile.TemporaryDirectory(prefix="pv-smoke-") as d:
        temp = Path(d)
        empty, private = temp / "empty.env", temp / "developer-isolation.yml"
        empty.write_text("", encoding="ascii")
        # Keep the actual integration worker, but replace the merged real Gateway and
        # staff bootstrap dependencies. The real registry job mounts the team's .env
        # read-write and must never be reached by this disposable developer smoke.
        private.write_text("""services:
  db:
    ports: !reset []
  core:
    ports: !reset []
  rule-engine:
    ports: !reset []
  staff-setup:
    ports: !reset []
    depends_on: !override
      db: { condition: service_healthy }
  ledger-gateway:
    ports: !reset []
    build: !override { context: ., dockerfile: core/Dockerfile }
    depends_on: !reset {}
    environment: !override
      CHAIN_ID: "${CHAIN_ID}"
      CONTRACT_ADDRESS: "${CONTRACT_ADDRESS}"
      CONFIRMATION_DEPTH: "${CONFIRMATION_DEPTH}"
      PINNED_NODE_URL: "${PINNED_NODE_URL}"
    volumes: !override
      - ./rules/devtools:/app/rules_dev:ro
""", encoding="ascii")
        cmd = ["docker","compose","--project-directory",str(ROOT),"--env-file",str(empty),"-p",project,
               "--profile","mock","-f",str(ROOT/"docker-compose.yml"),"-f",str(ROOT/"docker-compose.integration.yml"),
               "-f",str(TOOLS/"compose.worker-smoke.yml"),"-f",str(private)]
        command(cmd + ["config","--quiet"], env, label="Isolated Compose configuration")
        try:
            print("Building and starting isolated Core, PostgreSQL, mock Gateway and Rule Engine...",flush=True)
            command(cmd + ["up","-d","--build","rule-engine"], env, timeout=1500, label="Compose build/start")
            cid = command(cmd + ["ps","-q","rule-engine"], env, label="Locate worker")
            if not re.fullmatch("[a-f0-9]{12,64}", cid):
                raise RunError("Cannot identify running Rule Engine container")
            def health():
                return command(["docker","inspect","--format","{{.State.Health.Status}}",cid],env,label="Worker health")
            deadline = time.monotonic() + 180
            while health() != "healthy":
                if time.monotonic() >= deadline:
                    raise RunError("Rule Engine was not healthy within 180 seconds")
                time.sleep(2)
            report["worker_health_before_smoke"] = "healthy"
            def polls():
                return list(poll_rows(command(cmd+["logs","--no-color","--no-log-prefix","rule-engine"],
                                              env, label="Read worker poll counters")))
            first = polls()
            if first:
                report["first_poll"] = first[0]
            print("Worker is healthy. Running worker_smoke.py against Core's loopback inside its container...",flush=True)
            # SMOKE credentials go only through stdin to this short-lived interpreter.
            # No additional host python packages or published ports are required.
            script = ("import os\nos.environ.update(" + repr({"SMOKE_USERNAME":env["STAFF_ADMIN_USERNAME"],
                      "SMOKE_PASSWORD":env["STAFF_ADMIN_PASSWORD"]}) + ")\n" + (TOOLS/"worker_smoke.py").read_text(encoding="utf-8"))
            out = command(cmd+["exec","-T","core","python","-","--create-local-election"],env,source=script,
                          timeout=480,label="worker_smoke.py")
            report["worker_smoke"] = smoke_row(out)
            rows = polls()
            if not rows:
                raise RunError("No complete first-poll counters found")
            report["first_poll"] = rows[0]
            # A worker completes its poll just after Core stores the findings; allow the logging race.
            for _ in range(6):
                if any(r["stored"] > 0 for r in rows):
                    break
                time.sleep(1)
                rows = polls()
            stored = next((r for r in rows if r["stored"] > 0), None)
            if not stored:
                raise RunError("No poll with stored findings was captured")
            report["first_storing_poll"] = stored
            if health() != "healthy":
                raise RunError("Rule Engine was no longer healthy after smoke")
            report["worker_health_after_smoke"] = "healthy"
        finally:
            # Only delete the resources of the freshly generated private project.
            try:
                command(cmd+["down","--volumes","--remove-orphans"],env,timeout=120,label="Private smoke cleanup")
                report["cleanup_ok"] = True
            except RunError as exc:
                report["cleanup_ok"] = False
                report["cleanup_message"] = str(exc)
                print(f"Cleanup incomplete: see isolated project {project} in Docker Desktop.",file=sys.stderr)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", type=Path, help="New evidence JSON path (existing files are never overwritten)")
    args = p.parse_args()
    stamp = datetime.now(timezone.utc)
    out = (args.out or (Path.home()/"purevoting-evidence"/("m1-docker-"+stamp.strftime("%Y%m%dT%H%M%SZ")+".json"))).resolve()
    out.parent.mkdir(parents=True,exist_ok=True)
    report = {"started_utc":stamp.isoformat(), "mode":"isolated Docker developer smoke",
              "official_T01_run":False,"real_gateway":False,"real_chain":False,"passed":False}
    try:
        with out.open("x",encoding="utf-8") as f:
            try:
                execute(report)
                report["passed"] = True
            except (RunError, KeyboardInterrupt) as exc:
                report["error"] = str(exc) if isinstance(exc, RunError) else "Interrupted"
            finally:
                report["finished_utc"] = datetime.now(timezone.utc).isoformat()
                json.dump(report,f,indent=2)
                f.write("\n")
    except FileExistsError:
        print("Evidence file already exists; choose a new --out path.",file=sys.stderr)
        return 2
    print(json.dumps(report,indent=2))
    print(f"Evidence file: {out}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
