"""On-chain tests of PureVotingLedger (stage 2) against a running local PureChain network.
Usage: python3 -I test_ledger.py <solc binary> [rpc ports of node 1..4, default 8541 8542 8543 8544]
Node 1-3 accounts act as org_commission, org_observer and org_neutral; node 4's account is the Gateway account."""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chain import Contract, block_time, deploy, idh, rpc, solc_hashes, wait_block_time  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SOL = os.path.join(HERE, "..", "contracts", "PureVotingLedger.sol")
solc = sys.argv[1]
ports = [int(p) for p in sys.argv[2:6]] or [8541, 8542, 8543, 8544]
if len(ports) < 4:
    ports = [8541, 8542, 8543, 8544]

out = subprocess.run([solc, "--evm-version", "shanghai", "--optimize", "--optimize-runs", "200", "--combined-json", "abi,bin", SOL],
                     capture_output=True, text=True, check=True).stdout
art = next(v for k, v in json.loads(out)["contracts"].items() if k.endswith(":PureVotingLedger"))
abi = art["abi"] if isinstance(art["abi"], list) else json.loads(art["abi"])
fns, errs = solc_hashes(solc, SOL)

acct = [rpc(p, "eth_accounts")[0] for p in ports]
COMM, OBS, NEU, GW = acct
port_of = dict(zip(acct, ports))
c = Contract(port_of, abi, fns, errs)
c.address, deploy_gas = deploy(ports[0], COMM, art["bin"])
print(f"deployed at {c.address}, gas {deploy_gas}, runtime size {(len(rpc(ports[0], 'eth_getCode', [c.address, 'latest'])) - 2) // 2} bytes")

passed = failed = 0
gas = {}


def check(name, got, want):
    global passed, failed
    if got == want:
        passed += 1
    else:
        failed += 1
        print(f"FAIL {name}: got {got!r}, want {want!r}")


def ok(name, res):
    status, rcpt = res
    check(name, status, "ok")
    if rcpt:
        gas.setdefault(name.split(" ")[0], int(rcpt["gasUsed"], 16))
    return rcpt


def rev(name, res, err):
    check(name, res[0], "revert:" + err)


ORG = {n: idh(n) for n in ("org_commission", "org_observer", "org_neutral", "org_stranger")}
ZERO = "0x" + "00" * 32
RULE = "0x" + "00" * 18 + "be" + "ef"

# ---------------------------------------------------------------- registry
rev("register zero org", c.send("registerOrgSigner", ZERO, COMM, sender=COMM), "ZeroValue")
ok("registerOrgSigner commission", c.send("registerOrgSigner", ORG["org_commission"], COMM, sender=COMM))
ok("registerOrgSigner observer", c.send("registerOrgSigner", ORG["org_observer"], OBS, sender=OBS))
ok("registerOrgSigner neutral", c.send("registerOrgSigner", ORG["org_neutral"], NEU, sender=NEU))
rev("same org twice", c.send("registerOrgSigner", ORG["org_commission"], GW, sender=GW), "OrgAlreadyRegistered")
rev("one account, two orgs", c.send("registerOrgSigner", ORG["org_stranger"], GW, sender=COMM), "AccountAlreadyRegistered")
rev("signer key reused", c.send("registerOrgSigner", ORG["org_stranger"], COMM, sender=GW), "SignerAlreadyRegistered")
check("orgSigner", c.call("orgSigner", ORG["org_observer"]), [OBS.lower(), OBS.lower()])
check("orgCount", c.call("orgCount"), 3)
check("orgAt(2)", c.call("orgAt", 2), ORG["org_neutral"])

check("gateway not set", c.call("gatewayAccount")[2], False)
rev("createElection before gateway is set", c.send("createElection", idh("e0"), idh("c"), 1, 2, 60, 60, [ORG[k] for k in ORG if k != "org_stranger"], 2, ORG["org_commission"], 24, 1, sender=GW), "NotGateway")

# operator org: proposed by commission, confirmed by the other two
rev("setOperatorOrg unregistered", c.send("setOperatorOrg", ORG["org_stranger"], sender=COMM), "OrgNotRegistered")
rev("setOperatorOrg by a non-org account", c.send("setOperatorOrg", ORG["org_commission"], sender=GW), "NotRegisteredOrgAccount")
ok("setOperatorOrg", c.send("setOperatorOrg", ORG["org_commission"], sender=COMM))
check("operator not yet effective", c.call("operatorOrg"), [ORG["org_commission"], False])
rev("confirm wrong operator", c.send("confirmOperatorOrg", ORG["org_neutral"], sender=OBS), "WrongPendingValue")
ok("confirmOperatorOrg observer", c.send("confirmOperatorOrg", ORG["org_commission"], sender=OBS))
check("operator needs all three", c.call("operatorOrg")[1], False)
ok("confirmOperatorOrg neutral", c.send("confirmOperatorOrg", ORG["org_commission"], sender=NEU))
check("operator effective", c.call("operatorOrg"), [ORG["org_commission"], True])

# gateway account: registered by the neutral org's node account, confirmed by all
ok("setGatewayAccount", c.send("setGatewayAccount", GW, sender=NEU))
check("gateway pending", c.call("gatewayAccount"), [GW.lower(), ORG["org_neutral"], False])
ok("confirmGatewayAccount commission", c.send("confirmGatewayAccount", GW, sender=COMM))
ok("confirmGatewayAccount observer", c.send("confirmGatewayAccount", GW, sender=OBS))
check("gateway effective", c.call("gatewayAccount"), [GW.lower(), ORG["org_neutral"], True])

# rule engine signer: operated by the observer org
ok("setRuleEngineSigner", c.send("setRuleEngineSigner", RULE, sender=OBS))
ok("confirmRuleEngineSigner commission", c.send("confirmRuleEngineSigner", RULE, sender=COMM))
ok("confirmRuleEngineSigner neutral", c.send("confirmRuleEngineSigner", RULE, sender=NEU))
check("rule engine effective", c.call("ruleEngineSigner"), [RULE.lower(), ORG["org_observer"], True])

# ---------------------------------------------------------------- election creation
ORGS = [ORG["org_commission"], ORG["org_observer"], ORG["org_neutral"]]   # ascending org_id string order
E = idh("election-A")
t0 = block_time(ports[0])
START, END = t0 + 25, t0 + 40


def create(eid=E, cfg=None, start=START, end=END, bucket=5, interval=5, orgs=ORGS, thr=2, op=None, ttl=24, seq=1, sender=GW):
    return c.send("createElection", eid, cfg or idh("config-A"), start, end, bucket, interval, orgs, thr,
                  op or ORG["org_commission"], ttl, seq, sender=sender)


rev("create by a non-gateway account", create(sender=COMM), "NotGateway")
rev("create with configHash 0", create(cfg=ZERO), "InvalidConfigHash")
rev("create with threshold 1", create(thr=1), "InvalidThreshold")
rev("create with threshold = orgs", create(thr=3), "InvalidThreshold")
rev("create with unregistered org", create(orgs=[ORGS[0], ORGS[1], ORG["org_stranger"]]), "OrgNotRegistered")
rev("create with duplicate org", create(orgs=[ORGS[0], ORGS[1], ORGS[1]]), "DuplicateOrg")
rev("create with two orgs", create(orgs=ORGS[:2]), "TooFewOrganizations")
rev("create with wrong operator", create(op=ORG["org_observer"]), "OperatorOrgMismatch")
rev("create with window end <= start", create(end=START), "InvalidWindow")
rev("create with seq 0", create(seq=0), "SeqNotIncreasing")
check("unknown election reads zeros", c.call("electionState", E)[4], ZERO)
rcpt = ok("createElection", create())
check("StateChanged emitted", len(rcpt["logs"]), 1)
st = c.call("electionState", E)
check("state after create", [st[0], st[1], st[2], st[3], st[4], st[5], st[10]],
      [0, START, END, 5, idh("config-A"), ZERO, ORG["org_commission"]])
check("lastCoreSeq after create", st[9], 1)
check("governance", c.call("electionGovernance", E), [ORGS, 2, ORG["org_commission"], 24, 5])
rev("create the same election again", create(seq=2), "ElectionExists")

# ---------------------------------------------------------------- state machine and checkpoints
RH = idh("roster-A")
rev("freeze unknown election", c.send("freezeRoster", idh("nope"), RH, 1, sender=GW), "ElectionNotFound")
rev("freeze with empty roster hash", c.send("freezeRoster", E, ZERO, 2, sender=GW), "EmptyRoster")
rev("freeze with old seq", c.send("freezeRoster", E, RH, 1, sender=GW), "SeqNotIncreasing")
rev("open before freeze", c.send("openElection", E, 2, sender=GW), "InvalidStateTransition")
ok("freezeRoster", c.send("freezeRoster", E, RH, 2, sender=GW))
check("status ROSTER_FROZEN", [c.call("electionState", E)[0], c.call("electionState", E)[5]], [1, RH])
rev("freeze twice", c.send("freezeRoster", E, RH, 3, sender=GW), "InvalidStateTransition")
rev("open before window start", c.send("openElection", E, 3, sender=GW), "TimeNotReached")
ok("commitCheckpoint 0 (STATE_CHANGE) while frozen", c.send("commitCheckpoint", E, 0, 1, idh("tables-0"), 3, sender=GW))
rev("CLOSE_FINAL while ROSTER_FROZEN", c.send("commitCheckpoint", E, 1, 2, idh("t"), 4, sender=GW), "InvalidStateTransition")
check("snapshot 0", c.call("electionStateAtCheckpoint", E, 0), [1, END, RH, ZERO, ZERO, 3])

C = idh("election-C")   # second election with the same window: used for the tally checks
ok("create C", create(eid=C, seq=1))
ok("freeze C", c.send("freezeRoster", C, RH, 2, sender=GW))
wait_block_time(ports[0], START)
ok("openElection", c.send("openElection", E, 4, sender=GW))
ok("open C", c.send("openElection", C, 3, sender=GW))
check("status OPEN", c.call("electionState", E)[0], 2)
check("snapshot 0 is unchanged by later state", c.call("electionStateAtCheckpoint", E, 0)[0], 1)

rev("checkpoint 0 again", c.send("commitCheckpoint", E, 0, 0, idh("t"), 5, sender=GW), "CheckpointNotIncreasing")
rev("checkpoint with reason 4", c.send("commitCheckpoint", E, 3, 4, idh("t"), 5, sender=GW), "InvalidReason")
rev("checkpoint for unknown election", c.send("commitCheckpoint", idh("nope"), 1, 0, idh("t"), 1, sender=GW), "ElectionNotFound")
ok("commitCheckpoint 3 (PERIODIC)", c.send("commitCheckpoint", E, 3, 0, idh("tables-3"), 5, sender=GW))
rev("CLOSE_FINAL while OPEN", c.send("commitCheckpoint", E, 4, 2, idh("t"), 6, sender=GW), "InvalidStateTransition")
check("snapshot 3", c.call("electionStateAtCheckpoint", E, 3), [2, END, RH, ZERO, ZERO, 5])
check("lastCheckpoint", c.call("electionState", E)[8], 3)
check("never committed checkpoint reads zeros", c.call("electionStateAtCheckpoint", E, 2)[1], 0)
rev("checkpoint 2 after 3", c.send("commitCheckpoint", E, 2, 0, idh("t"), 6, sender=GW), "CheckpointNotIncreasing")

rev("early close is not implemented yet", c.send("closeElection", E, END, True, ZERO, 0, 0, 6, sender=GW), "EarlyCloseNotImplemented")
rev("close at a time other than the window end", c.send("closeElection", E, END - 5, False, ZERO, 0, 0, 6, sender=GW), "InvalidStateTransition")
rev("close before the window end", c.send("closeElection", E, END, False, ZERO, 0, 0, 6, sender=GW), "TimeNotReached")
wait_block_time(ports[0], END)
ok("closeElection", c.send("closeElection", E, END, False, ZERO, 0, 0, 6, sender=GW))
check("status CLOSED", c.call("electionState", E)[0], 3)
ok("commitCheckpoint 4 (CLOSE_FINAL)", c.send("commitCheckpoint", E, 4, 2, idh("tables-4"), 7, sender=GW))
check("snapshot 4", c.call("electionStateAtCheckpoint", E, 4), [3, END, RH, ZERO, ZERO, 7])
check("lastCoreSeq", c.call("electionState", E)[9], 7)
rev("close twice", c.send("closeElection", E, END, False, ZERO, 0, 0, 8, sender=GW), "InvalidStateTransition")

# ---------------------------------------------------------------- anchors (stage 3a)
A1, A2, A3 = idh("anc-1"), idh("anc-2"), idh("anc-3")
R1, R2, RJ = idh("root-1"), idh("root-2"), idh("rej-1")
check("anchorCount starts at 0", c.call("anchorCount", E), 0)
rev("anchor of an unknown election", c.send("commitAnchor", idh("nope"), A1, R1, RJ, 1, 3, 0, 5, sender=GW), "ElectionNotFound")
rev("anchor sent by a non-gateway account", c.send("commitAnchor", E, A1, R1, RJ, 1, 3, 0, 5, sender=COMM), "NotGateway")
rev("anchor with zero id", c.send("commitAnchor", E, ZERO, R1, RJ, 1, 3, 0, 5, sender=GW), "ZeroValue")
rev("anchor with mode 2", c.send("commitAnchor", E, A1, R1, RJ, 2, 3, 0, 5, sender=GW), "InvalidMode")
rev("anchor with bucketMin > bucketMax", c.send("commitAnchor", E, A1, R1, RJ, 1, 3, END, END - 5, sender=GW), "InvalidBuckets")
rev("batched anchor: bucketMax + bucket > end", c.send("commitAnchor", E, A1, R1, RJ, 1, 3, START, END - 4, sender=GW), "AnchorBeyondWindowEnd")
ok("commitAnchor batched (bucketMax + bucket == end)", c.send("commitAnchor", E, A1, R1, RJ, 1, 3, START, END - 5, sender=GW))
check("anchorCount 1", c.call("anchorCount", E), 1)
check("anchorAt(0)", c.call("anchorAt", E, 0), [A1, R1, RJ, 1, 3, START, END - 5])
ok("commitAnchor immediate (no bucket rule)", c.send("commitAnchor", E, A2, R2, ZERO, 0, 1, END, END, sender=GW))
check("anchorCount 2, commit order kept", [c.call("anchorCount", E), c.call("anchorAt", E, 1)[0]], [2, A2])
ok("same anchor again is a no-op", c.send("commitAnchor", E, A1, R1, RJ, 1, 3, START, END - 5, sender=GW))
check("no-op did not add an anchor", c.call("anchorCount", E), 2)
rev("same anchorId with another root", c.send("commitAnchor", E, A1, R2, RJ, 1, 3, START, END - 5, sender=GW), "AnchorConflict")
check("anchorAt out of range", c.call("anchorAt", E, 2), "revert:IndexOutOfRange")
check("other election has no anchors", c.call("anchorCount", C), 0)

# ---------------------------------------------------------------- CLOSE_FINAL and tally (T15 d)
RES = idh("result-1")
ok("close C", c.send("closeElection", C, END, False, ZERO, 0, 0, 4, sender=GW))
rev("tally of an unknown election", c.send("publishTally", idh("nope"), RES, 1, 2, sender=GW), "ElectionNotFound")
rev("tally before any CLOSE_FINAL checkpoint", c.send("publishTally", C, RES, 1, 5, sender=GW), "InvalidFinalCheckpoint")
ok("C: commitCheckpoint 1 (PERIODIC) after close", c.send("commitCheckpoint", C, 1, 0, idh("c-tables-1"), 5, sender=GW))
rev("tally pointing at a PERIODIC checkpoint", c.send("publishTally", C, RES, 1, 6, sender=GW), "InvalidFinalCheckpoint")
ok("C: commitCheckpoint 2 (CLOSE_FINAL)", c.send("commitCheckpoint", C, 2, 2, idh("c-tables-2"), 6, sender=GW))
rev("second CLOSE_FINAL checkpoint", c.send("commitCheckpoint", C, 3, 2, idh("c-tables-3"), 7, sender=GW), "CloseFinalAlreadyCommitted")
rev("tally one checkpoint earlier than CLOSE_FINAL (T15 d)", c.send("publishTally", C, RES, 1, 7, sender=GW), "InvalidFinalCheckpoint")
rev("tally one checkpoint later than CLOSE_FINAL", c.send("publishTally", C, RES, 3, 7, sender=GW), "InvalidFinalCheckpoint")
rev("tally with zero result hash", c.send("publishTally", C, ZERO, 2, 7, sender=GW), "ZeroValue")
rev("tally by a non-gateway account", c.send("publishTally", C, RES, 2, 7, sender=COMM), "NotGateway")
rev("tally with an old seq", c.send("publishTally", C, RES, 2, 6, sender=GW), "SeqNotIncreasing")
ok("publishTally", c.send("publishTally", C, RES, 2, 7, sender=GW))
st = c.call("electionState", C)
check("status TALLIED and result stored", [st[0], st[7], st[9]], [4, RES, 7])
rev("tally twice", c.send("publishTally", C, RES, 2, 8, sender=GW), "InvalidStateTransition")
ok("C: commitCheckpoint 4 (TALLY)", c.send("commitCheckpoint", C, 4, 3, idh("c-tables-4"), 8, sender=GW))
check("TALLY snapshot holds status and result", c.call("electionStateAtCheckpoint", C, 4), [4, END, RH, ZERO, RES, 8])
check("CLOSE_FINAL snapshot has no result yet", c.call("electionStateAtCheckpoint", C, 2)[4], ZERO)
ok("anchor after tally is still accepted", c.send("commitAnchor", C, A3, R1, RJ, 1, 2, START, END - 5, sender=GW))
rev("tally with a wrong checkpoint number on E (has CLOSE_FINAL 4)", c.send("publishTally", E, RES, 99, 8, sender=GW), "InvalidFinalCheckpoint")

# an election whose window ended while the roster was frozen can no longer be opened (only VOID remains)
B = idh("election-B")
tb = block_time(ports[0])
ok("create B (window already over)", create(eid=B, start=tb - 20, end=tb - 10, seq=1))
ok("freeze B", c.send("freezeRoster", B, RH, 2, sender=GW))
rev("open B after its window", c.send("openElection", B, 3, sender=GW), "InvalidStateTransition")
rev("tally of a ROSTER_FROZEN election", c.send("publishTally", B, idh("result-1"), 1, 3, sender=GW), "InvalidStateTransition")

# a registry change makes the gateway ineffective again until everyone re-confirms
ok("setGatewayAccount (re-register)", c.send("setGatewayAccount", GW, sender=NEU))
rev("gateway blocked until re-confirmed", c.send("freezeRoster", B, RH, 9, sender=GW), "NotGateway")

print()
print("gas used per function (first call):", json.dumps(gas, indent=0).replace("\n", " "))
print(f"RESULT: {passed} checks passed, {failed} failed")
sys.exit(1 if failed else 0)
