// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Token} from "./Token.sol";

/// @title DAOBase — minimal token-voting DAO that controls a treasury (stretch threat T4).
/// @notice Anyone can propose an arbitrary call; once "for" votes exceed QUORUM_BPS of supply it can be executed.
abstract contract DAOBase {
    Token public immutable govToken;
    address public immutable owner;
    address public guardian;
    bool public paused;

    uint256 public constant QUORUM_BPS = 5_000; // > 50 % of total supply

    struct Proposal {
        address proposer;
        address target;
        bytes data;
        uint256 createdBlock;
        uint256 snapshotBlock;
        uint256 forVotes;
        bool executed;
    }

    Proposal[] public proposals;
    mapping(uint256 => mapping(address => bool)) public hasVoted;

    event ProposalCreated(uint256 indexed id, address indexed proposer, address target);
    event Voted(uint256 indexed id, address indexed voter, uint256 weight);
    event Executed(uint256 indexed id, address target);
    event Paused(address indexed by);
    event Unpaused(address indexed by);

    modifier whenNotPaused() {
        require(!paused, "DAO: paused");
        _;
    }

    constructor(Token govToken_, address guardian_) {
        govToken = govToken_;
        owner = msg.sender;
        guardian = guardian_;
    }

    function votingPower(address voter, uint256 id) public view virtual returns (uint256);

    /// @dev Blocks that must pass between proposal creation and execution.
    function executionDelay() public pure virtual returns (uint256);

    function proposalCount() external view returns (uint256) {
        return proposals.length;
    }

    function propose(address target, bytes calldata data) external whenNotPaused returns (uint256 id) {
        id = proposals.length;
        proposals.push(Proposal(msg.sender, target, data, block.number, block.number - 1, 0, false));
        emit ProposalCreated(id, msg.sender, target);
    }

    function vote(uint256 id) external whenNotPaused {
        require(!hasVoted[id][msg.sender], "DAO: voted");
        uint256 w = votingPower(msg.sender, id);
        require(w > 0, "DAO: no voting power");
        hasVoted[id][msg.sender] = true;
        proposals[id].forVotes += w;
        emit Voted(id, msg.sender, w);
    }

    function execute(uint256 id) external whenNotPaused {
        Proposal storage p = proposals[id];
        require(!p.executed, "DAO: executed");
        require(block.number >= p.createdBlock + executionDelay(), "DAO: timelock");
        require(p.forVotes * 10_000 > govToken.totalSupply() * QUORUM_BPS, "DAO: quorum");
        p.executed = true;
        (bool ok,) = p.target.call(p.data);
        require(ok, "DAO: call failed");
        emit Executed(id, p.target);
    }

    function pause() external {
        require(msg.sender == guardian || msg.sender == owner, "DAO: not guardian");
        paused = true;
        emit Paused(msg.sender);
    }

    function unpause() external {
        require(msg.sender == owner, "DAO: not owner");
        paused = false;
        emit Unpaused(msg.sender);
    }
}

/// @title VulnerableDAO — counts *current* balances and executes instantly (Beanstalk pattern).
contract VulnerableDAO is DAOBase {
    constructor(Token govToken_, address guardian_) DAOBase(govToken_, guardian_) {}

    function votingPower(address voter, uint256) public view override returns (uint256) {
        return govToken.balanceOf(voter);
    }

    function executionDelay() public pure override returns (uint256) {
        return 0;
    }
}

/// @title SnapshotDAO — voting power is the balance *before* the proposal block, plus a 1-block timelock.
/// @notice Flash-borrowed tokens have zero past balance, so a flash-loan governance attack has no votes.
contract SnapshotDAO is DAOBase {
    constructor(Token govToken_, address guardian_) DAOBase(govToken_, guardian_) {}

    function votingPower(address voter, uint256 id) public view override returns (uint256) {
        return govToken.getPastBalance(voter, proposals[id].snapshotBlock);
    }

    function executionDelay() public pure override returns (uint256) {
        return 1;
    }
}
