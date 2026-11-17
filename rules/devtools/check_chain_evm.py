#!/usr/bin/env python3
"""Optional local-EVM validation of the actual Solidity getters used by R4/R5.

Run from the repository root after installing rules/requirements-lock.txt and
rules/devtools/requirements-evm.txt into a disposable virtual environment:

    python rules/devtools/check_chain_evm.py --solc /path/to/solc --output evidence.json

The fixture is a deterministic creation/freeze/checkpoint, with independently
specified expected state. Contract execution is real PyEVM; the HTTP transport
adapts that local EVM to JSON-RPC. This is not PureChain, a real Gateway run,
finalization, or official T01. No network or external account is used.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from copy import deepcopy
from datetime import datetime, timezone


def validate(solc: str, root: Path) -> dict:
    if not __debug__:
        raise RuntimeError("Run validation without -O: its assertions must execute.")
    sys.path[:0] = [str(root / "rules"), str(root / "rules/tests")]
    import httpx
    from eth_abi import encode
    from eth_tester import EthereumTester
    from eth_utils import keccak
    from purevoting_rules.live_chain import PinnedChain, ChainPins, _READ_ABI
    from purevoting_rules.chain_audit import audit_chain
    from purevoting_rules.hashing import H, Hc
    from test_chain_audit import clean_snapshot

    source = root / "ledger/contracts/PureVotingLedger.sol"
    flags = ["--evm-version", "shanghai", "--optimize", "--optimize-runs", "200"]
    command = [solc, *flags, "--combined-json", "abi,bin", str(source)]
    # Compiler output is isolated and removed, including on a failed check.
    # The requested executable is used; no compiler is downloaded implicitly.
    with TemporaryDirectory(prefix="purevoting-evm-check-") as temporary:
        artifact = Path(temporary) / "ledger.json"
        with artifact.open("w", encoding="utf-8") as stream:
            subprocess.run(command, cwd=root, stdout=stream, stderr=subprocess.PIPE,
                           text=True, encoding="utf-8", check=True)
        compiled = json.loads(artifact.read_text(encoding="utf-8"))
    compiler_version = subprocess.run([solc, "--version"], capture_output=True, text=True,
                                      encoding="utf-8", check=True).stdout.strip().splitlines()[-1]
    base_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                                 text=True, encoding="utf-8", check=True).stdout.strip()
    tracked_changes = bool(subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=root,
                                          capture_output=True, text=True, encoding="utf-8", check=True).stdout.strip())
    art = next(v for k,v in compiled['contracts'].items() if k.endswith(':PureVotingLedger'))
    abi = {entry['name']:entry for entry in art['abi'] if entry['type']=='function'}
    checked = []
    missing_getters = []
    for name,(inputs,outputs) in _READ_ABI.items():
        if name not in abi:
            assert name == 'proposalState'
            missing_getters.append(name)
            continue
        assert inputs == tuple(i['type'] for i in abi[name]['inputs']), name
        assert outputs == tuple(i['type'] for i in abi[name]['outputs']), name
        checked.append(name)
    et = EthereumTester()
    accounts = et.get_accounts()
    txh = et.send_transaction({'from':accounts[0], 'gas':8_000_000, 'data':'0x'+art['bin']})
    receipt = et.get_transaction_receipt(txh)
    assert receipt['status']==1
    contract = receipt['contract_address']
    def send(name,*args,sender=None):
        entry=abi[name]
        types=[i['type'] for i in entry['inputs']]
        sig=name+'('+','.join(types)+')'
        data='0x'+(keccak(text=sig)[:4]+encode(types,args)).hex()
        tx=et.send_transaction({'from':sender or accounts[3], 'to':contract,'gas':5_000_000,'data':data})
        receipt=et.get_transaction_receipt(tx)
        assert receipt['status']==1, name
        return receipt
    s=clean_snapshot()
    for i in range(3):
        send('registerOrgSigner',bytes.fromhex(H('org_'+str(i+1))),accounts[i],sender=accounts[i])
    send('setOperatorOrg',bytes.fromhex(H('org_1')),sender=accounts[0])
    for i in (1,2):
        send('confirmOperatorOrg',bytes.fromhex(H('org_1')),sender=accounts[i])
    send('setGatewayAccount',accounts[3],sender=accounts[2])
    for i in (0,1):
        send('confirmGatewayAccount',accounts[3],sender=accounts[i])
    # Fixture signing addresses are replaced by the real EVM registry accounts.
    p=s['events'][0]['payload']
    for i,org in enumerate(p['organizations']):
        org['signer']=accounts[i].lower()
    s['views']['election_config'][0]['organizations']=deepcopy(p['organizations'])
    s['events'][0]['payload_hash']=Hc(p)
    # Rebuild only the first leaf after changing creation's signer addresses.
    from purevoting_rules.hashing import leaf_hash
    first=s['events'][0]
    digest=leaf_hash(first)
    aid=first['anchor']['anchor_id']
    first['anchor']['merkle_root']=digest
    s['anchor_evidence']['anchors'][0]['merkle_root']=digest
    s['anchor_evidence']['leaves'][aid]['leaves'][0]['leaf_hash']=digest
    s['anchor_evidence']['proofs'][first['event_id']].update(leaf_hash=digest,merkle_root=digest)
    el=bytes.fromhex(H(s['election_id']))
    send('createElection',el,bytes.fromhex(p['config_hash']),1794873600,1794874200,30,60,
         [bytes.fromhex(H(o['org_id'])) for o in p['organizations']],2,bytes.fromhex(H('org_1')),24,1)
    send('freezeRoster',el,bytes.fromhex(H('roster')),2)
    send('commitCheckpoint',el,0,1,bytes.fromhex(H('tables')),3)
    for event,record in zip(s['events'],s['anchor_evidence']['anchors']):
        aid=record['anchor_id']
        receipt=send('commitAnchor',el,bytes.fromhex(H(aid)),bytes.fromhex(record['merkle_root']),bytes.fromhex(H('')),0,1,1794873600,1794873600)
        fields={'tx_hash':receipt['transaction_hash'],'block_number':receipt['block_number']}
        event['anchor'].update(fields)
        record.update(fields)
        s['anchor_evidence']['proofs'][event['event_id']].update(fields)
    et.mine_blocks(2)
    trace=[]
    def handler(req):
        assert not {'authorization','cookie','x-service-key'} & set(req.headers)
        body=json.loads(req.content); method=body['method']; params=body['params'];trace.append(body)
        try:
            if method=='eth_chainId':
                value=hex(et.backend.chain.chain_id)
            elif method=='eth_blockNumber':
                value=hex(et.get_block_by_number('latest')['number'])
            elif method=='eth_call':
                value=et.call(dict(params[0],**{'from':accounts[0]}),int(params[1],16))
            elif method=='eth_getLogs':
                f=params[0]
                logs=et.get_logs(from_block=int(f['fromBlock'],16),to_block=int(f['toBlock'],16),address=f['address'],topics=f['topics'])
                value=[{'address':l['address'],'topics':l['topics'],'data':l['data'],'blockNumber':hex(l['block_number']),
                        'transactionHash':l['transaction_hash'],'removed':False} for l in logs]
            else:
                raise AssertionError(method)
            return httpx.Response(200,json={'jsonrpc':'2.0','id':body['id'],'result':value})
        except Exception as exc:
            raise AssertionError((method,params,type(exc).__name__,str(exc))) from exc
    pins=ChainPins(str(et.backend.chain.chain_id),contract,('http://pinned-evm/',),2)
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        s['chain']=PinnedChain(pins,http).collect(s,s['anchor_evidence'])
    assert s['chain']['available'],s['chain']
    assert s['chain']['anchors'],s['chain']
    out={};refs=[]
    failed,reason=audit_chain(s,s['events'],s['rejections'],out,refs)
    assert (failed,reason)==(False,None),(out,reason,refs)
    # Verify a dishonest Gateway can no longer substitute a self-consistent root.
    s['anchor_evidence']['proofs'][s['events'][0]['event_id']]['merkle_root']=H('forged Gateway root')
    bad={};badrefs=[]
    failed,reason=audit_chain(s,s['events'],s['rejections'],bad,badrefs)
    assert failed and bad['anchors_mismatch']>0,bad
    summary={'base_commit':base_commit, 'tracked_changes_present':tracked_changes, 'contract_source_sha256':H(source.read_bytes()), 'contract_spec':'v1.2.2 (572c51f)', 'validation':'compiled upstream Solidity in local PyEVM; no PureChain/Gateway deployment claimed',
             'compiler':compiler_version, 'compiler_flags':flags,'abi_getters_matched':checked,'upstream_getter_missing':missing_getters,
             'contract_address':contract,'rpc_reads':len(trace),'confirmed_block':s['chain']['confirmed_block'],
             'clean_chain_audit':'PASS','forged_gateway_root':'FAIL','normal_observed':out}
    summary['finished_utc'] = datetime.now(timezone.utc).isoformat()
    summary['validation_source_sha256'] = {
        str(path.relative_to(root)).replace('\\', '/'): H(path.read_bytes()) for path in (
            root / 'rules/purevoting_rules/live_chain.py', root / 'rules/purevoting_rules/chain_audit.py',
            root / 'rules/tests/test_chain_audit.py', root / 'rules/devtools/check_chain_evm.py')}

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--solc", required=True, help="Path to the team-approved solc 0.8.28 executable")
    parser.add_argument("--output", type=Path, help="Optional UTF-8 JSON result path")
    args = parser.parse_args()
    supplied = Path(args.solc).expanduser()
    solc = str(supplied.resolve()) if supplied.is_file() else shutil.which(args.solc)
    if not solc:
        parser.error("The supplied solc executable was not found.")
    try:
        result = validate(solc, Path(__file__).resolve().parents[2])
    except ModuleNotFoundError as exc:
        parser.error(f"Missing optional dependency {exc.name}; install requirements-lock.txt and devtools/requirements-evm.txt.")
    result_text = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(result_text, encoding="utf-8")
    print(result_text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
