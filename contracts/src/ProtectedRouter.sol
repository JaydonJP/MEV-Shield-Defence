// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Token} from "./Token.sol";
import {SimpleAMM} from "./SimpleAMM.sol";

/// @title ProtectedRouter — the user-facing swap entry point: enforces `minAmountOut` and a deadline.
/// @notice `minAmountOut` is the lever MEV Shield's adaptive-slippage defense pulls: set it so that
///         the best sandwich is no longer profitable.
contract ProtectedRouter {
    event RoutedSwap(
        address indexed user, address indexed amm, address tokenIn, uint256 amountIn, uint256 amountOut, uint256 minOut
    );

    function swapExactIn(SimpleAMM amm, address tokenIn, uint256 amountIn, uint256 minAmountOut, uint256 deadline)
        external
        returns (uint256 amountOut)
    {
        require(block.timestamp <= deadline, "Router: expired");
        Token t = Token(tokenIn);
        require(t.transferFrom(msg.sender, address(this), amountIn), "Router: transfer");
        t.approve(address(amm), amountIn);
        amountOut = amm.swap(tokenIn, amountIn, minAmountOut, msg.sender);
        emit RoutedSwap(msg.sender, address(amm), tokenIn, amountIn, amountOut, minAmountOut);
    }
}
