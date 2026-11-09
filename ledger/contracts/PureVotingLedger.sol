// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title PureVotingLedger (stage 3a skeleton)
/// @notice Implements the registry, the election state machine up to TALLIED, checkpoints and anchors of
///         contracts/chain-interface.md (v1.0). Proposals, signature checks, finding records, reviews, finalize and
///         void are NOT implemented yet (planned weeks 2 to 5, see docs/roles/ledger.md section 9).
/// @dev Every function that exists here has the exact name and signature of IPureVotingLedger; the extra functions
///      (registry setup, orgCount, orgAt) are allowed additions. tests/check_interface.py verifies this.
///
/// Mapping of reverts to Gateway reject codes (proposal, see open questions in the stage 2 report):
///   TimeNotReached                                  -> RETRY_LATER (TIME_NOT_REACHED)
///   OrgNotRegistered, OperatorOrgMismatch           -> ORG_NOT_REGISTERED
///   every other revert from a Gateway write         -> INVALID_STATE_TRANSITION
contract PureVotingLedger {
    // ------------------------------------------------------------------ constants
    uint8 internal constant DRAFT = 0;
    uint8 internal constant ROSTER_FROZEN = 1;
    uint8 internal constant OPEN = 2;
    uint8 internal constant CLOSED = 3;
    uint8 internal constant TALLIED = 4;
    uint8 internal constant FINALIZED = 5;
    uint8 internal constant VOIDED = 6;

    uint8 internal constant ITEM_OPERATOR = 0;
    uint8 internal constant ITEM_RULE_ENGINE = 1;
    uint8 internal constant ITEM_GATEWAY = 2;

    // ------------------------------------------------------------------ errors
    error NotGateway();
    error NotRegisteredOrgAccount();
    error ZeroValue();
    error OrgAlreadyRegistered();
    error AccountAlreadyRegistered();
    error SignerAlreadyRegistered();
    error WrongPendingValue();
    error ElectionExists();
    error ElectionNotFound();
    error OrgNotRegistered();
    error OperatorOrgMismatch();
    error DuplicateOrg();
    error TooFewOrganizations();
    error InvalidThreshold();
    error InvalidConfigHash();
    error InvalidWindow();
    error EmptyRoster();
    error InvalidStateTransition();
    error TimeNotReached();
    error SeqNotIncreasing();
    error CheckpointNotIncreasing();
    error InvalidReason();
    error EarlyCloseNotImplemented();
    error InvalidMode();
    error InvalidBuckets();
    error AnchorConflict();
    error AnchorBeyondWindowEnd();
    error IndexOutOfRange();
    error InvalidFinalCheckpoint();
    error CloseFinalAlreadyCommitted();
    error SignatureInvalid();
    error FindingConflict();
    error CheckpointNotCommitted();
    error InvalidFinding();

    // ------------------------------------------------------------------ events
    event StateChanged(bytes32 indexed electionId, uint8 status, uint64 coreSeq);
    event AnchorCommitted(bytes32 indexed electionId, bytes32 indexed anchorId, bytes32 merkleRoot, bytes32 rejectionsRoot, uint8 mode, uint32 eventCount);
    event FindingRecorded(bytes32 indexed findingId, bytes32 indexed electionId, uint8 ruleId, uint8 result, uint64 findingCheckpoint, bytes32 findingHash, bytes signature, string findingIdText, string electionIdText);
    // registry events (additions, for the Gateway's /v1/organizations and setup checks)
    event OrgRegistered(bytes32 indexed orgId, address signer, address registeredBy);
    event RegistryItemSet(uint8 indexed item, bytes32 value, bytes32 operatedBy, uint32 version);
    event RegistryItemConfirmed(uint8 indexed item, uint32 version, bytes32 indexed orgId, uint32 confirmations);

    // ------------------------------------------------------------------ registry storage
    struct Org {
        address signer;
        address registeredBy;
    }

    /// operatorOrg, ruleEngineSigner and gatewayAccount share one pattern: set by one registered org's node account,
    /// effective only when every registered org's node account has confirmed the current version.
    struct Item {
        bytes32 value;      // orgId, or an address in the low 20 bytes
        bytes32 operatedBy; // org operating the Rule Engine / Gateway (zero for the operator item)
        uint32 version;
        uint32 confirmations;
    }

    mapping(bytes32 => Org) private _orgs;
    mapping(address => bytes32) private _orgOfAccount; // node account -> orgId
    mapping(address => bytes32) private _orgOfSigner;  // signer key -> orgId
    bytes32[] private _orgIds;
    Item[3] private _items;
    mapping(uint8 => mapping(uint32 => mapping(bytes32 => bool))) private _confirmed; // item => version => orgId

    // ------------------------------------------------------------------ election storage
    struct Snapshot {
        bool exists;
        uint8 status;
        uint8 reason;
        uint64 windowEnd;
        bytes32 rosterHash;
        bytes32 tokenSetRoot;
        bytes32 resultHash;
        uint64 lastCoreSeq;
        bytes32 tablesHash;
    }

    struct Election {
        bool exists;
        uint8 status;
        uint64 windowStart;
        uint64 windowEnd;
        uint32 timeBucketSeconds;
        uint32 checkpointIntervalSeconds;
        bytes32 configHash;
        bytes32 rosterHash;
        bytes32 tokenSetRoot;
        bytes32 resultHash;
        bool hasCheckpoint;
        uint64 lastCheckpoint;
        bool hasCloseFinal;           // a checkpoint with reason CLOSE_FINAL (2) was committed
        uint64 closeFinalCheckpoint;
        uint64 lastCoreSeq;
        bytes32 operatorOrgId;
        uint8 approvalThreshold;
        uint32 proposalTtlHours;
        bytes32[] orgIds;
        mapping(uint64 => Snapshot) snapshots;
    }

    mapping(bytes32 => Election) private _elections;

    struct Finding {
        bool exists;
        bytes32 electionId;
        bytes32 findingHash;
        uint8 ruleId;
        uint8 result;
        uint64 findingCheckpoint;
        uint8 status;   // 0 NOT_REQUIRED, 1 PENDING, 2 RESOLVED, 3 SUPERSEDED, 4 CONFIRMED_ISSUE
    }
    mapping(bytes32 => Finding) private _findings;

    /// @dev The arguments of submitFindingRecord / relayFindingRecord in memory (keeps the stack small).
    struct FindingArgs {
        bytes32 findingId;
        bytes32 electionId;
        uint8 ruleId;
        uint8 result;
        uint64 findingCheckpoint;
        bytes32 findingHash;
        bytes signature;
        string findingIdText;
        string electionIdText;
    }

    struct Anchor {
        bytes32 anchorId;
        bytes32 merkleRoot;
        bytes32 rejectionsRoot;
        uint8 mode;       // 0 immediate, 1 batched
        uint32 eventCount;
        uint64 bucketMin;
        uint64 bucketMax;
    }

    mapping(bytes32 => Anchor[]) private _anchors;                        // electionId => anchors in commit order
    mapping(bytes32 => mapping(bytes32 => uint256)) private _anchorIndex; // electionId => anchorId => index + 1

    // ================================================================== registry: setup
    /// @notice An organization's own node account registers its signer key (D-21). msg.sender becomes registeredBy.
    /// @dev Not in the spec, added safeguards: one org per node account, one org per signer key.
    function registerOrgSigner(bytes32 orgId, address signer) external {
        if (orgId == bytes32(0) || signer == address(0)) revert ZeroValue();
        if (_orgs[orgId].registeredBy != address(0)) revert OrgAlreadyRegistered();
        if (_orgOfAccount[msg.sender] != bytes32(0)) revert AccountAlreadyRegistered();
        if (_orgOfSigner[signer] != bytes32(0)) revert SignerAlreadyRegistered();
        _orgs[orgId] = Org({signer: signer, registeredBy: msg.sender});
        _orgOfAccount[msg.sender] = orgId;
        _orgOfSigner[signer] = orgId;
        _orgIds.push(orgId);
        emit OrgRegistered(orgId, signer, msg.sender);
    }

    /// @notice Propose the operator organization; counts as the caller's confirmation.
    function setOperatorOrg(bytes32 orgId) external {
        if (_orgs[orgId].registeredBy == address(0)) revert OrgNotRegistered();
        _setItem(ITEM_OPERATOR, orgId, bytes32(0));
    }

    function confirmOperatorOrg(bytes32 orgId) external {
        _confirmItem(ITEM_OPERATOR, orgId);
    }

    /// @notice Called by the node account of the organization that operates the Rule Engine (D-25).
    function setRuleEngineSigner(address signer) external {
        if (signer == address(0)) revert ZeroValue();
        _setItem(ITEM_RULE_ENGINE, _addr(signer), _requireOrgAccount());
    }

    function confirmRuleEngineSigner(address signer) external {
        _confirmItem(ITEM_RULE_ENGINE, _addr(signer));
    }

    /// @notice Called by the node account of the organization that operates the Gateway (D-26).
    function setGatewayAccount(address account) external {
        if (account == address(0)) revert ZeroValue();
        _setItem(ITEM_GATEWAY, _addr(account), _requireOrgAccount());
    }

    function confirmGatewayAccount(address account) external {
        _confirmItem(ITEM_GATEWAY, _addr(account));
    }

    function orgCount() external view returns (uint256) {
        return _orgIds.length;
    }

    function orgAt(uint256 index) external view returns (bytes32) {
        return _orgIds[index];
    }

    // ================================================================== registry: reads (interface)
    function orgSigner(bytes32 orgId) external view returns (address signer, address registeredBy) {
        Org storage o = _orgs[orgId];
        return (o.signer, o.registeredBy);
    }

    function operatorOrg() external view returns (bytes32 orgId, bool confirmedByAll) {
        return (_items[ITEM_OPERATOR].value, _all(ITEM_OPERATOR));
    }

    function ruleEngineSigner() external view returns (address signer, bytes32 operatedByOrg, bool confirmedByAll) {
        Item storage it = _items[ITEM_RULE_ENGINE];
        return (address(uint160(uint256(it.value))), it.operatedBy, _all(ITEM_RULE_ENGINE));
    }

    function gatewayAccount() external view returns (address account, bytes32 operatedByOrg, bool confirmedByAll) {
        Item storage it = _items[ITEM_GATEWAY];
        return (address(uint160(uint256(it.value))), it.operatedBy, _all(ITEM_GATEWAY));
    }

    // ================================================================== election: reads (interface)
    /// @dev An unknown election reads as all zeros (configHash == 0 is the "no such election" marker, chain-interface.md).
    function electionState(bytes32 electionId) external view returns (
        uint8 status, uint64 windowStart, uint64 windowEnd, uint32 timeBucketSeconds,
        bytes32 configHash, bytes32 rosterHash, bytes32 tokenSetRoot,
        bytes32 resultHash, uint64 lastCheckpoint, uint64 lastCoreSeq, bytes32 operatorOrgId
    ) {
        Election storage e = _elections[electionId];
        return (e.status, e.windowStart, e.windowEnd, e.timeBucketSeconds, e.configHash, e.rosterHash,
            e.tokenSetRoot, e.resultHash, e.lastCheckpoint, e.lastCoreSeq, e.operatorOrgId);
    }

    function electionGovernance(bytes32 electionId) external view returns (
        bytes32[] memory orgIds, uint8 approvalThreshold, bytes32 operatorOrgId, uint32 proposalTtlHours,
        uint32 checkpointIntervalSeconds
    ) {
        Election storage e = _elections[electionId];
        return (e.orgIds, e.approvalThreshold, e.operatorOrgId, e.proposalTtlHours, e.checkpointIntervalSeconds);
    }

    /// @dev A checkpoint that was never committed reads as zeros (windowEnd == 0 never occurs in a stored snapshot).
    function electionStateAtCheckpoint(bytes32 electionId, uint64 checkpoint) external view returns (
        uint8 status, uint64 windowEnd, bytes32 rosterHash, bytes32 tokenSetRoot, bytes32 resultHash, uint64 lastCoreSeq
    ) {
        Snapshot storage s = _elections[electionId].snapshots[checkpoint];
        return (s.status, s.windowEnd, s.rosterHash, s.tokenSetRoot, s.resultHash, s.lastCoreSeq);
    }

    // ================================================================== anchors: reads (interface)
    function anchorCount(bytes32 electionId) external view returns (uint256) {
        return _anchors[electionId].length;
    }

    /// @dev An index at or beyond anchorCount reverts (IndexOutOfRange).
    function anchorAt(bytes32 electionId, uint256 index) external view returns (
        bytes32 anchorId, bytes32 merkleRoot, bytes32 rejectionsRoot, uint8 mode,
        uint32 eventCount, uint64 bucketMin, uint64 bucketMax
    ) {
        if (index >= _anchors[electionId].length) revert IndexOutOfRange();
        Anchor storage a = _anchors[electionId][index];
        return (a.anchorId, a.merkleRoot, a.rejectionsRoot, a.mode, a.eventCount, a.bucketMin, a.bucketMax);
    }

    // ================================================================== Gateway writes (interface)
    function createElection(
        bytes32 electionId, bytes32 configHash, uint64 windowStart, uint64 windowEnd,
        uint32 timeBucketSeconds, uint32 checkpointIntervalSeconds, bytes32[] calldata orgIds, uint8 approvalThreshold,
        bytes32 operatorOrgId, uint32 proposalTtlHours, uint64 coreSeq
    ) external onlyGateway {
        Election storage e = _elections[electionId];
        if (electionId == bytes32(0)) revert ZeroValue();
        if (e.exists) revert ElectionExists();
        if (configHash == bytes32(0)) revert InvalidConfigHash();
        // sanity checks not in the spec: a window that ends before it starts, or zero-length buckets, can never be valid
        if (windowEnd <= windowStart || timeBucketSeconds == 0 || checkpointIntervalSeconds == 0) revert InvalidWindow();
        if (!_all(ITEM_OPERATOR) || _items[ITEM_OPERATOR].value != operatorOrgId) revert OperatorOrgMismatch();
        _checkOrgs(orgIds, approvalThreshold);
        _advanceSeq(e, coreSeq);

        e.exists = true;
        e.status = DRAFT;
        e.windowStart = windowStart;
        e.windowEnd = windowEnd;
        e.timeBucketSeconds = timeBucketSeconds;
        e.checkpointIntervalSeconds = checkpointIntervalSeconds;
        e.configHash = configHash;
        e.operatorOrgId = operatorOrgId;
        e.approvalThreshold = approvalThreshold;
        e.proposalTtlHours = proposalTtlHours;
        e.orgIds = orgIds;
        emit StateChanged(electionId, DRAFT, coreSeq);
    }

    function freezeRoster(bytes32 electionId, bytes32 rosterHash, uint64 coreSeq) external onlyGateway {
        Election storage e = _get(electionId);
        if (e.status != DRAFT) revert InvalidStateTransition();
        if (rosterHash == bytes32(0)) revert EmptyRoster();
        _advanceSeq(e, coreSeq);
        e.rosterHash = rosterHash;
        e.status = ROSTER_FROZEN;
        emit StateChanged(electionId, ROSTER_FROZEN, coreSeq);
    }

    /// @dev TOKEN_BATCH_ISSUED / TOKEN_REVOKED. Allowed while ROSTER_FROZEN or OPEN. Proposal-bound use
    /// (TOKEN_REVOKED, TOKEN_REISSUE) needs the proposal logic of week 4, so a non-zero proposalIdOrZero reverts for now.
    function setTokenSetRoot(bytes32 electionId, bytes32 tokenSetRoot, bytes32 proposalIdOrZero, uint64 coreSeq)
        external onlyGateway
    {
        Election storage e = _get(electionId);
        if (e.status != ROSTER_FROZEN && e.status != OPEN) revert InvalidStateTransition();
        if (tokenSetRoot == bytes32(0)) revert ZeroValue();
        if (proposalIdOrZero != bytes32(0)) revert InvalidStateTransition();
        _advanceSeq(e, coreSeq);
        e.tokenSetRoot = tokenSetRoot;
        emit StateChanged(electionId, e.status, coreSeq);
    }

    function openElection(bytes32 electionId, uint64 coreSeq) external onlyGateway {
        Election storage e = _get(electionId);
        if (e.status != ROSTER_FROZEN) revert InvalidStateTransition();
        // block time not reached yet: the Gateway answers RETRY_LATER, never REJECTED
        if (block.timestamp < e.windowStart) revert TimeNotReached();
        // never opened before the window ended: the only exit is ELECTION_VOID (README, state machine)
        if (block.timestamp >= e.windowEnd) revert InvalidStateTransition();
        _advanceSeq(e, coreSeq);
        e.status = OPEN;
        emit StateChanged(electionId, OPEN, coreSeq);
    }

    /// @dev Only the scheduled close is implemented. The early close needs proposals (planned week 4).
    function closeElection(
        bytes32 electionId, uint64 closedAt, bool early, bytes32 /* proposalId */, uint64 /* requestedClosedAt */,
        uint64 /* executedAt */, uint64 coreSeq
    ) external onlyGateway {
        Election storage e = _get(electionId);
        if (early) revert EarlyCloseNotImplemented();
        if (e.status != OPEN) revert InvalidStateTransition();
        // assumption: a scheduled close happens at the current effective end
        if (closedAt != e.windowEnd) revert InvalidStateTransition();
        if (block.timestamp < closedAt) revert TimeNotReached();
        _advanceSeq(e, coreSeq);
        e.status = CLOSED;
        emit StateChanged(electionId, CLOSED, coreSeq);
    }

    function commitCheckpoint(
        bytes32 electionId, uint64 checkpoint, uint8 reason, bytes32 tablesHash, uint64 coreSeq
    ) external onlyGateway {
        Election storage e = _get(electionId);
        // FINALIZED and VOIDED are the last events of an election and are not followed by a checkpoint
        if (e.status == FINALIZED || e.status == VOIDED) revert InvalidStateTransition();
        if (reason > 3) revert InvalidReason();
        if (e.hasCheckpoint && checkpoint <= e.lastCheckpoint) revert CheckpointNotIncreasing();
        // approved by the team lead (Nov 2): CLOSE_FINAL only while the election is CLOSED, and only once;
        // publishTally refers to it
        if (reason == 2) {
            if (e.status != CLOSED) revert InvalidStateTransition();
            if (e.hasCloseFinal) revert CloseFinalAlreadyCommitted();
        }
        _advanceSeq(e, coreSeq);
        e.hasCheckpoint = true;
        e.lastCheckpoint = checkpoint;
        if (reason == 2) {
            e.hasCloseFinal = true;
            e.closeFinalCheckpoint = checkpoint;
        }
        // the snapshot includes this event's own seq (assumption, see report)
        e.snapshots[checkpoint] = Snapshot({
            exists: true,
            status: e.status,
            reason: reason,
            windowEnd: e.windowEnd,
            rosterHash: e.rosterHash,
            tokenSetRoot: e.tokenSetRoot,
            resultHash: e.resultHash,
            lastCoreSeq: e.lastCoreSeq,
            tablesHash: tablesHash
        });
    }

    /// @dev Assumptions (stage 3a report): any existing election accepts anchors whatever its status (an anchor
    ///      records data, it does not change the election); the same anchorId with identical fields is a no-op
    ///      (a retried transaction), with different fields it reverts; bucketMin <= bucketMax; the batched rule of
    ///      chain-interface.md applies against the stored (effective) windowEnd.
    function commitAnchor(
        bytes32 electionId, bytes32 anchorId, bytes32 merkleRoot, bytes32 rejectionsRoot, uint8 mode,
        uint32 eventCount, uint64 bucketMin, uint64 bucketMax
    ) external onlyGateway {
        Election storage e = _get(electionId);
        if (anchorId == bytes32(0)) revert ZeroValue();
        uint256 existing = _anchorIndex[electionId][anchorId];
        if (existing != 0) {
            Anchor storage a = _anchors[electionId][existing - 1];
            if (a.merkleRoot == merkleRoot && a.rejectionsRoot == rejectionsRoot && a.mode == mode
                && a.eventCount == eventCount && a.bucketMin == bucketMin && a.bucketMax == bucketMax) return;
            revert AnchorConflict();
        }
        if (mode > 1) revert InvalidMode();
        if (bucketMin > bucketMax) revert InvalidBuckets();
        if (mode == 1 && uint256(bucketMax) + uint256(e.timeBucketSeconds) > uint256(e.windowEnd)) {
            revert AnchorBeyondWindowEnd();
        }
        _anchors[electionId].push(Anchor(anchorId, merkleRoot, rejectionsRoot, mode, eventCount, bucketMin, bucketMax));
        _anchorIndex[electionId][anchorId] = _anchors[electionId].length;
        emit AnchorCommitted(electionId, anchorId, merkleRoot, rejectionsRoot, mode, eventCount);
    }

    /// @dev Once only (CLOSED -> TALLIED is the only way in). finalCheckpoint must be the checkpoint committed with
    ///      reason CLOSE_FINAL (T15 d); a tally before any CLOSE_FINAL checkpoint exists is refused as well.
    function publishTally(
        bytes32 electionId, bytes32 resultHash, uint64 finalCheckpoint, uint64 coreSeq
    ) external onlyGateway {
        Election storage e = _get(electionId);
        if (e.status != CLOSED) revert InvalidStateTransition();
        if (resultHash == bytes32(0)) revert ZeroValue();
        if (!e.hasCloseFinal || finalCheckpoint != e.closeFinalCheckpoint) revert InvalidFinalCheckpoint();
        _advanceSeq(e, coreSeq);
        e.resultHash = resultHash;
        e.status = TALLIED;
        emit StateChanged(electionId, TALLIED, coreSeq);
    }

    // ================================================================== finding records
    function findingRecord(bytes32 findingId) external view returns (
        bytes32 electionId, bytes32 findingHash, uint8 ruleId, uint8 result, uint64 findingCheckpoint, uint8 status
    ) {
        Finding storage f = _findings[findingId];
        return (f.electionId, f.findingHash, f.ruleId, f.result, f.findingCheckpoint, f.status);
    }

    /// @dev Anyone may send it; the Rule Engine's signature is what counts.
    function submitFindingRecord(
        bytes32 findingId, bytes32 electionId, uint8 ruleId, uint8 result, uint64 findingCheckpoint,
        bytes32 findingHash, bytes calldata signature, string calldata findingIdText, string calldata electionIdText
    ) external {
        _recordFinding(FindingArgs(findingId, electionId, ruleId, result, findingCheckpoint, findingHash, signature,
            findingIdText, electionIdText));
    }

    function relayFindingRecord(
        bytes32 findingId, bytes32 electionId, uint8 ruleId, uint8 result, uint64 findingCheckpoint,
        bytes32 findingHash, bytes calldata signature, string calldata findingIdText, string calldata electionIdText
    ) external onlyGateway {
        _recordFinding(FindingArgs(findingId, electionId, ruleId, result, findingCheckpoint, findingHash, signature,
            findingIdText, electionIdText));
    }

    function _recordFinding(FindingArgs memory a) private {
        Finding storage f = _findings[a.findingId];
        if (f.exists) {
            if (f.findingHash == a.findingHash && f.electionId == a.electionId) return;   // identical: no-op
            revert FindingConflict();
        }
        Election storage e = _get(a.electionId);
        if (a.ruleId < 1 || a.ruleId > 5 || a.result > 2) revert InvalidFinding();
        if (sha256(bytes(a.findingIdText)) != a.findingId || sha256(bytes(a.electionIdText)) != a.electionId) {
            revert InvalidFinding();
        }
        if (!e.hasCheckpoint || a.findingCheckpoint > e.lastCheckpoint) revert CheckpointNotCommitted();
        _checkRuleEngineSignature(sha256(abi.encodePacked(
            bytes32("PV2-FINDING-v1"), a.findingId, a.electionId, uint256(a.ruleId), uint256(a.result),
            uint256(a.findingCheckpoint), a.findingHash)), a.signature);
        f.exists = true;
        f.electionId = a.electionId;
        f.findingHash = a.findingHash;
        f.ruleId = a.ruleId;
        f.result = a.result;
        f.findingCheckpoint = a.findingCheckpoint;
        f.status = a.result == 0 ? 0 : 1;   // PASS needs no review; FAIL / INCONCLUSIVE are PENDING until reviewed
        emit FindingRecorded(a.findingId, a.electionId, a.ruleId, a.result, a.findingCheckpoint, a.findingHash,
            a.signature, a.findingIdText, a.electionIdText);
    }

    /// @dev eip191-secp256k1 (hashing.md): ecrecover over keccak256("\x19Ethereum Signed Message:\n32" || digest).
    function _checkRuleEngineSignature(bytes32 digest, bytes memory sig) private view {
        Item storage it = _items[ITEM_RULE_ENGINE];
        if (!_all(ITEM_RULE_ENGINE) || sig.length != 65) revert SignatureInvalid();
        bytes32 r;
        bytes32 s;
        uint8 v;
        assembly {
            r := mload(add(sig, 32))
            s := mload(add(sig, 64))
            v := byte(0, mload(add(sig, 96)))
        }
        if (v < 27) v += 27;
        address signer = ecrecover(keccak256(abi.encodePacked("\x19Ethereum Signed Message:\n32", digest)), v, r, s);
        if (signer == address(0) || signer != address(uint160(uint256(it.value)))) revert SignatureInvalid();
    }

    // ================================================================== internals
    modifier onlyGateway() {
        Item storage g = _items[ITEM_GATEWAY];
        if (!_all(ITEM_GATEWAY) || msg.sender != address(uint160(uint256(g.value)))) revert NotGateway();
        _;
    }

    function _get(bytes32 electionId) private view returns (Election storage e) {
        e = _elections[electionId];
        if (!e.exists) revert ElectionNotFound();
    }

    function _advanceSeq(Election storage e, uint64 coreSeq) private {
        if (coreSeq == 0 || coreSeq <= e.lastCoreSeq) revert SeqNotIncreasing();
        e.lastCoreSeq = coreSeq;
    }

    function _checkOrgs(bytes32[] calldata orgIds, uint8 approvalThreshold) private view {
        uint256 n = orgIds.length;
        if (n < 3) revert TooFewOrganizations();
        for (uint256 i = 0; i < n; i++) {
            if (_orgs[orgIds[i]].registeredBy == address(0)) revert OrgNotRegistered();
            for (uint256 j = 0; j < i; j++) {
                if (orgIds[j] == orgIds[i]) revert DuplicateOrg();
            }
        }
        if (approvalThreshold < 2 || approvalThreshold > n - 1) revert InvalidThreshold();
    }

    function _requireOrgAccount() private view returns (bytes32 orgId) {
        orgId = _orgOfAccount[msg.sender];
        if (orgId == bytes32(0)) revert NotRegisteredOrgAccount();
    }

    function _setItem(uint8 item, bytes32 value, bytes32 operatedBy) private {
        bytes32 org = _requireOrgAccount();
        Item storage it = _items[item];
        it.value = value;
        it.operatedBy = operatedBy;
        it.version += 1;
        it.confirmations = 0;
        emit RegistryItemSet(item, value, operatedBy, it.version);
        _confirm(item, org);
    }

    function _confirmItem(uint8 item, bytes32 expectedValue) private {
        bytes32 org = _requireOrgAccount();
        if (_items[item].value == bytes32(0) || _items[item].value != expectedValue) revert WrongPendingValue();
        _confirm(item, org);
    }

    function _confirm(uint8 item, bytes32 org) private {
        Item storage it = _items[item];
        if (_confirmed[item][it.version][org]) return;
        _confirmed[item][it.version][org] = true;
        it.confirmations += 1;
        emit RegistryItemConfirmed(item, it.version, org, it.confirmations);
    }

    function _all(uint8 item) private view returns (bool) {
        Item storage it = _items[item];
        return it.value != bytes32(0) && _orgIds.length > 0 && it.confirmations >= _orgIds.length;
    }

    function _addr(address a) private pure returns (bytes32) {
        return bytes32(uint256(uint160(a)));
    }
}
