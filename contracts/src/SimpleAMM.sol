// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Token} from "./Token.sol";

/// @title SimpleAMM — constant-product (x·y=k) pool with a configurable fee.
/// @notice Same math as Uniswap V2, pull-based (`swap` transfers `amountIn` from the caller).
///         Tracks Uniswap-V2-style cumulative prices so a TWAP oracle can be built on top,
///         and the block of the last swap (used by CommitRevealSwap's "first trade in block" rule).
contract SimpleAMM {
    Token public immutable token0;
    Token public immutable token1;
    uint256 public immutable feeBps; // 30 = 0.30 %

    uint256 public reserve0;
    uint256 public reserve1;

    /// @dev Σ price · Δt, price scaled by 1e18. price0 = token1 per token0.
    uint256 public price0CumulativeLast;
    uint256 public price1CumulativeLast;
    uint256 public blockTimestampLast;
    uint256 public lastSwapBlock;

    event Swap(
        address indexed sender, address indexed tokenIn, uint256 amountIn, uint256 amountOut, address indexed to
    );
    event Sync(uint256 reserve0, uint256 reserve1);
    event LiquidityAdded(address indexed provider, uint256 amount0, uint256 amount1);

    constructor(Token token0_, Token token1_, uint256 feeBps_) {
        require(address(token0_) != address(token1_), "AMM: identical tokens");
        require(feeBps_ < 10_000, "AMM: fee");
        token0 = token0_;
        token1 = token1_;
        feeBps = feeBps_;
    }

    // ------------------------------------------------------------------ views

    function getReserves() external view returns (uint256, uint256, uint256) {
        return (reserve0, reserve1, blockTimestampLast);
    }

    function getAmountOut(uint256 amountIn, uint256 reserveIn, uint256 reserveOut) public view returns (uint256) {
        require(amountIn > 0, "AMM: zero input");
        require(reserveIn > 0 && reserveOut > 0, "AMM: no liquidity");
        uint256 amountInWithFee = amountIn * (10_000 - feeBps);
        return (amountInWithFee * reserveOut) / (reserveIn * 10_000 + amountInWithFee);
    }

    function quote(address tokenIn, uint256 amountIn) external view returns (uint256) {
        (uint256 rIn, uint256 rOut) = _reservesFor(tokenIn);
        return getAmountOut(amountIn, rIn, rOut);
    }

    /// @notice Spot price of token0 denominated in token1, scaled by 1e18.
    function spotPrice0() public view returns (uint256) {
        return (reserve1 * 1e18) / reserve0;
    }

    /// @notice Spot price of token1 denominated in token0, scaled by 1e18.
    function spotPrice1() public view returns (uint256) {
        return (reserve0 * 1e18) / reserve1;
    }

    /// @notice Cumulative prices as they would be if `_update` ran now (counterfactual, like UniswapV2OracleLibrary).
    function currentCumulativePrices() public view returns (uint256 p0, uint256 p1, uint256 ts) {
        p0 = price0CumulativeLast;
        p1 = price1CumulativeLast;
        ts = block.timestamp;
        uint256 elapsed = ts - blockTimestampLast;
        if (elapsed > 0 && reserve0 != 0 && reserve1 != 0) {
            p0 += ((reserve1 * 1e18) / reserve0) * elapsed;
            p1 += ((reserve0 * 1e18) / reserve1) * elapsed;
        }
    }

    // -------------------------------------------------------------- mutations

    function addLiquidity(uint256 amount0, uint256 amount1) external {
        require(token0.transferFrom(msg.sender, address(this), amount0), "AMM: transfer0");
        require(token1.transferFrom(msg.sender, address(this), amount1), "AMM: transfer1");
        _update(token0.balanceOf(address(this)), token1.balanceOf(address(this)));
        emit LiquidityAdded(msg.sender, amount0, amount1);
    }

    function swap(address tokenIn, uint256 amountIn, uint256 minAmountOut, address to)
        external
        returns (uint256 amountOut)
    {
        (uint256 rIn, uint256 rOut) = _reservesFor(tokenIn);
        amountOut = getAmountOut(amountIn, rIn, rOut);
        require(amountOut >= minAmountOut, "AMM: slippage");
        require(amountOut < rOut, "AMM: insufficient liquidity");

        Token tIn = Token(tokenIn);
        Token tOut = tokenIn == address(token0) ? token1 : token0;
        require(tIn.transferFrom(msg.sender, address(this), amountIn), "AMM: transferIn");
        require(tOut.transfer(to, amountOut), "AMM: transferOut");

        _update(token0.balanceOf(address(this)), token1.balanceOf(address(this)));
        lastSwapBlock = block.number;
        emit Swap(msg.sender, tokenIn, amountIn, amountOut, to);
    }

    // --------------------------------------------------------------- internal

    function _reservesFor(address tokenIn) internal view returns (uint256 rIn, uint256 rOut) {
        if (tokenIn == address(token0)) return (reserve0, reserve1);
        require(tokenIn == address(token1), "AMM: bad token");
        return (reserve1, reserve0);
    }

    /// @dev Accumulates the price that held *before* this block's first change (manipulation-resistant).
    function _update(uint256 balance0, uint256 balance1) internal {
        uint256 ts = block.timestamp;
        uint256 elapsed = ts - blockTimestampLast;
        if (elapsed > 0 && reserve0 != 0 && reserve1 != 0) {
            price0CumulativeLast += ((reserve1 * 1e18) / reserve0) * elapsed;
            price1CumulativeLast += ((reserve0 * 1e18) / reserve1) * elapsed;
        }
        reserve0 = balance0;
        reserve1 = balance1;
        blockTimestampLast = ts;
        emit Sync(balance0, balance1);
    }
}
