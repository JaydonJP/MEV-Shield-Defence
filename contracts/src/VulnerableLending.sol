// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {LendingBase} from "./LendingBase.sol";
import {Token} from "./Token.sol";
import {SimpleAMM} from "./SimpleAMM.sol";

/// @title VulnerableLending — prices collateral with the AMM's *spot* price.
/// @notice This is the bZx / Harvest bug: anyone who can move the spot price inside one transaction
///         (e.g. with a flash loan) can borrow far more than their collateral is worth.
contract VulnerableLending is LendingBase {
    constructor(Token collateral_, Token debt_, SimpleAMM amm_, address guardian_)
        LendingBase(collateral_, debt_, amm_, guardian_)
    {}

    function collateralPrice() public view override returns (uint256) {
        return amm.spotPrice0();
    }
}
