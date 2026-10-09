// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Token} from "../Token.sol";
import {SimpleAMM} from "../SimpleAMM.sol";

/// @title SandwichAttacker — TEST HARNESS (T1): the on-chain half of a sandwich bot.
/// @notice The off-chain bot sends `frontrun` with a higher priority fee than the victim and `backrun` with a lower
///         one, so the block is ordered [frontrun, victim, backrun]. Holds its own inventory of both tokens.
contract SandwichAttacker {
    address public immutable owner;

    /// @dev amount of the "other" token bought by the last front-run on each pool
    mapping(address => uint256) public pendingInventory;

    event Frontrun(address indexed amm, address tokenIn, uint256 amountIn, uint256 amountOut);
    event Backrun(address indexed amm, address tokenIn, uint256 amountIn, uint256 amountOut);

    constructor() {
        owner = msg.sender;
    }

    modifier onlyOwner() {
        require(msg.sender == owner, "Sandwich: only owner");
        _;
    }

    function frontrun(SimpleAMM amm, address tokenIn, uint256 amountIn, uint256 minOut)
        external
        onlyOwner
        returns (uint256 out)
    {
        Token(tokenIn).approve(address(amm), amountIn);
        out = amm.swap(tokenIn, amountIn, minOut, address(this));
        pendingInventory[address(amm)] += out;
        emit Frontrun(address(amm), tokenIn, amountIn, out);
    }

    /// @notice Sells everything the front-run bought on `amm`. `tokenIn` is the token the front-run *received*.
    function backrun(SimpleAMM amm, address tokenIn, uint256 minOut) external onlyOwner returns (uint256 out) {
        uint256 amountIn = pendingInventory[address(amm)];
        require(amountIn > 0, "Sandwich: nothing to sell");
        pendingInventory[address(amm)] = 0;
        Token(tokenIn).approve(address(amm), amountIn);
        out = amm.swap(tokenIn, amountIn, minOut, address(this));
        emit Backrun(address(amm), tokenIn, amountIn, out);
    }

    function withdraw(Token token, uint256 amount) external onlyOwner {
        require(token.transfer(owner, amount), "Sandwich: transfer");
    }
}
