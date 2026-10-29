// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {LendingBase} from "./LendingBase.sol";
import {Token} from "./Token.sol";
import {SimpleAMM} from "./SimpleAMM.sol";
import {TWAPOracle} from "./TWAPOracle.sol";

/// @title SafeLending — manipulation-resistant lending.
/// @notice Two by-design defenses against flash-loan oracle manipulation:
///         1. collateral is priced with a TWAP (single-transaction manipulation barely moves it);
///         2. a deviation guard refuses to lend while spot and TWAP disagree by more than `maxDeviationBps`.
contract SafeLending is LendingBase {
    TWAPOracle public immutable oracle;
    uint256 public immutable maxDeviationBps;
    uint256 public constant MAX_BORROW_PER_BLOCK = 100_000 ether;
    uint256 public borrowBlock;
    uint256 public borrowedThisBlock;

    constructor(Token collateral_, Token debt_, SimpleAMM amm_, address guardian_, TWAPOracle oracle_, uint256 maxDevBps_)
        LendingBase(collateral_, debt_, amm_, guardian_)
    {
        require(address(oracle_.amm()) == address(amm_), "SafeLending: oracle/amm mismatch");
        require(maxDevBps_ > 0 && maxDevBps_ <= 10_000, "SafeLending: invalid deviation");
        oracle = oracle_;
        maxDeviationBps = maxDevBps_;
    }

    function collateralPrice() public view override returns (uint256) {
        return oracle.twap();
    }

    function priceDeviationBps() public view returns (uint256) {
        uint256 spot = amm.spotPrice0();
        uint256 twap = oracle.twap();
        uint256 diff = spot > twap ? spot - twap : twap - spot;
        return (diff * 10_000) / twap;
    }

    function _beforeBorrow(uint256 amount) internal override {
        require(priceDeviationBps() <= maxDeviationBps, "SafeLending: price deviation");
        require(block.number > lastDepositBlock[msg.sender], "SafeLending: deposit cooldown");
        if (borrowBlock != block.number) {
            borrowBlock = block.number;
            borrowedThisBlock = 0;
        }
        require(borrowedThisBlock + amount <= MAX_BORROW_PER_BLOCK, "SafeLending: block borrow limit");
        borrowedThisBlock += amount;
    }
}
