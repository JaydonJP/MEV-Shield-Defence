// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Token} from "./Token.sol";
import {SimpleAMM} from "./SimpleAMM.sol";
import {SettlementLock} from "./SettlementLock.sol";

/// @notice Atomic allocation across direct pools for the same pair. Off-chain quotes rank MEV risk.
contract MEVRouter is SettlementLock {
    event Routed(address indexed user, address indexed tokenIn, uint256 amountIn, uint256 amountOut);

    function swap(
        SimpleAMM[] calldata pools,
        uint256[] calldata amounts,
        uint256[] calldata minOuts,
        Token tokenIn,
        Token tokenOut,
        uint256 minTotalOut,
        uint256 deadline
    ) external nonReentrant returns (uint256 totalOut) {
        require(block.timestamp <= deadline, "Route: expired");
        require(pools.length > 0 && pools.length <= 8 && pools.length == amounts.length, "Route: legs");
        require(minOuts.length == pools.length, "Route: limits");
        require(address(tokenIn) != address(tokenOut), "Route: tokens");
        uint256 totalIn;
        for (uint256 i; i < pools.length; i++) {
            SimpleAMM p = pools[i];
            require(
                (address(p.token0()) == address(tokenIn) && address(p.token1()) == address(tokenOut))
                    || (address(p.token1()) == address(tokenIn) && address(p.token0()) == address(tokenOut)),
                "Route: pair"
            );
            require(amounts[i] > 0, "Route: zero leg");
            // Repeated pools would invalidate the independent-pool quote model.
            for (uint256 j; j < i; j++) {
                require(address(pools[j]) != address(p), "Route: duplicate");
            }
            totalIn += amounts[i];
        }
        require(tokenIn.transferFrom(msg.sender, address(this), totalIn), "Route: transfer");
        for (uint256 i; i < pools.length; i++) {
            tokenIn.approve(address(pools[i]), amounts[i]);
            totalOut += pools[i].swap(address(tokenIn), amounts[i], minOuts[i], msg.sender);
        }
        require(totalOut >= minTotalOut, "Route: limit");
        emit Routed(msg.sender, address(tokenIn), totalIn, totalOut);
    }
}
