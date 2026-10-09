// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {SimpleAMM} from "./SimpleAMM.sol";

/// @title TWAPOracle — time-weighted average price of SimpleAMM's token0 (in token1, 1e18-scaled).
/// @notice The average runs from the last stored observation up to *now*, using the AMM's
///         counterfactual cumulative price. Because the AMM accumulates the price that held before
///         the current block, a manipulation inside the current transaction does not move the TWAP at all.
contract TWAPOracle {
    SimpleAMM public immutable amm;
    uint256 public immutable minPeriod;

    uint256 public observationCumulative;
    uint256 public observationTimestamp;
    uint256 public lastTwap;

    event Updated(uint256 twap, uint256 timestamp);

    constructor(SimpleAMM amm_, uint256 minPeriod_) {
        amm = amm_;
        minPeriod = minPeriod_;
        (uint256 c0,, uint256 ts) = amm_.currentCumulativePrices();
        observationCumulative = c0;
        observationTimestamp = ts;
        lastTwap = amm_.spotPrice0();
    }

    /// @notice Roll the observation window forward (anyone may call; keepers do it periodically).
    function update() external {
        (uint256 c0,, uint256 ts) = amm.currentCumulativePrices();
        uint256 elapsed = ts - observationTimestamp;
        if (elapsed < minPeriod || elapsed == 0) return;
        lastTwap = (c0 - observationCumulative) / elapsed;
        observationCumulative = c0;
        observationTimestamp = ts;
        emit Updated(lastTwap, ts);
    }

    /// @notice TWAP of token0 in token1 (1e18-scaled) over [observationTimestamp, now].
    function twap() public view returns (uint256) {
        (uint256 c0,, uint256 ts) = amm.currentCumulativePrices();
        uint256 elapsed = ts - observationTimestamp;
        if (elapsed == 0) return lastTwap;
        return (c0 - observationCumulative) / elapsed;
    }
}
