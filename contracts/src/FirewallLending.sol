// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {SafeLending} from "./SafeLending.sol";
import {Token} from "./Token.sol";
import {SimpleAMM} from "./SimpleAMM.sol";
import {TWAPOracle} from "./TWAPOracle.sol";

/// @notice SafeLending plus global per-block and exact rolling-block outflow budgets.
/// @dev Limits apply across all callers; repayments do not replenish an attacker's allowance.
contract FirewallLending is SafeLending {
    uint256 public blockLimit;
    uint256 public windowLimit;
    uint256 public windowBlocks;
    mapping(uint256 => uint256) public borrowedAt;
    event LimitsSet(uint256 perBlock, uint256 perWindow, uint256 blocksInWindow);
    event Outflow(uint256 indexed blockNumber, uint256 amount);

    constructor(
        Token collateral_,
        Token debt_,
        SimpleAMM amm_,
        address guardian_,
        TWAPOracle oracle_,
        uint256 maxDevBps_,
        uint256 blockLimit_,
        uint256 windowLimit_,
        uint256 windowBlocks_
    ) SafeLending(collateral_, debt_, amm_, guardian_, oracle_, maxDevBps_) {
        _setLimits(blockLimit_, windowLimit_, windowBlocks_);
    }

    function setLimits(uint256 perBlock, uint256 perWindow, uint256 blocksInWindow) external {
        require(msg.sender == owner, "Firewall: owner");
        _setLimits(perBlock, perWindow, blocksInWindow);
    }

    function _setLimits(uint256 perBlock, uint256 perWindow, uint256 blocksInWindow) internal {
        require(perBlock > 0 && perWindow >= perBlock && blocksInWindow > 0 && blocksInWindow <= 64, "Firewall: limits");
        blockLimit = perBlock;
        windowLimit = perWindow;
        windowBlocks = blocksInWindow;
        emit LimitsSet(perBlock, perWindow, blocksInWindow);
    }

    function windowUsed() public view returns (uint256 used) {
        for (uint256 i; i < windowBlocks && i <= block.number; i++) {
            used += borrowedAt[block.number - i];
        }
    }

    function effectiveLimits() public view returns (uint256 perBlock, uint256 perWindow) {
        // Reduced confidence tightens the budget before the hard deviation guard rejects borrowing.
        bool caution = priceDeviationBps() > maxDeviationBps / 2;
        return (caution ? blockLimit / 2 : blockLimit, caution ? windowLimit / 2 : windowLimit);
    }

    function _afterBorrow(uint256 amount) internal override {
        (uint256 perBlock, uint256 perWindow) = effectiveLimits();
        require(borrowedAt[block.number] + amount <= perBlock, "Firewall: block budget");
        require(windowUsed() + amount <= perWindow, "Firewall: window budget");
        borrowedAt[block.number] += amount;
        emit Outflow(block.number, amount);
    }
}
