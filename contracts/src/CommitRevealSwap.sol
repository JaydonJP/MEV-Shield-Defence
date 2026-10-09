// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Token} from "./Token.sol";
import {SimpleAMM} from "./SimpleAMM.sol";

/// @title CommitRevealSwap — two-phase swap that hides the order until it executes.
/// @notice Block N:  commit(hash(user, amm, tokenIn, amountIn, minOut, salt)) — reveals nothing.
///         Block N+k: reveal(...) executes the swap, but only if it is the *first* trade on that pool in
///         its block. A searcher therefore cannot pre-position in an earlier block (the order was hidden)
///         and cannot front-run in the reveal block (the front-run would make the reveal revert, so the
///         sandwich earns nothing).
contract CommitRevealSwap {
    uint256 public constant MAX_DELAY = 20; // blocks a commitment stays valid

    struct Commitment {
        uint256 blockNumber;
        bool used;
    }

    mapping(bytes32 => Commitment) public commitments;

    event Committed(address indexed user, bytes32 indexed commitment, uint256 blockNumber);
    event Revealed(
        address indexed user, bytes32 indexed commitment, address amm, address tokenIn, uint256 amountIn, uint256 amountOut
    );

    function commitmentHash(
        address user,
        address amm,
        address tokenIn,
        uint256 amountIn,
        uint256 minAmountOut,
        bytes32 salt
    ) public pure returns (bytes32) {
        return keccak256(abi.encode(user, amm, tokenIn, amountIn, minAmountOut, salt));
    }

    function commit(bytes32 commitment) external {
        require(commitments[commitment].blockNumber == 0, "CR: exists");
        commitments[commitment] = Commitment(block.number, false);
        emit Committed(msg.sender, commitment, block.number);
    }

    function reveal(SimpleAMM amm, address tokenIn, uint256 amountIn, uint256 minAmountOut, bytes32 salt)
        external
        returns (uint256 amountOut)
    {
        bytes32 h = commitmentHash(msg.sender, address(amm), tokenIn, amountIn, minAmountOut, salt);
        Commitment storage c = commitments[h];
        require(c.blockNumber != 0, "CR: unknown commitment");
        require(!c.used, "CR: used");
        require(block.number > c.blockNumber, "CR: reveal too early");
        require(block.number <= c.blockNumber + MAX_DELAY, "CR: expired");
        require(amm.lastSwapBlock() < block.number, "CR: pool already traded this block");
        c.used = true;

        Token t = Token(tokenIn);
        require(t.transferFrom(msg.sender, address(this), amountIn), "CR: transfer");
        t.approve(address(amm), amountIn);
        amountOut = amm.swap(tokenIn, amountIn, minAmountOut, msg.sender);
        emit Revealed(msg.sender, h, address(amm), tokenIn, amountIn, amountOut);
    }
}
