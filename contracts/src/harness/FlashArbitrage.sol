// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Token} from "../Token.sol";
import {SimpleAMM} from "../SimpleAMM.sol";
import {FlashLender, IFlashBorrower} from "../FlashLender.sol";

/// @title FlashArbitrage — TEST HARNESS (T3, benign): flash-borrow, buy cheap, sell dear, repay.
/// @notice Legitimate MEV: it moves the two pools' prices *towards* each other and harms no protocol.
contract FlashArbitrage is IFlashBorrower {
    address public immutable owner;
    FlashLender public immutable lender;

    constructor(FlashLender lender_) {
        owner = msg.sender;
        lender = lender_;
    }

    /// @param buyPool  pool where `otherToken` is cheap (pay `borrowToken`, receive `otherToken`)
    /// @param sellPool pool where `otherToken` is dear  (pay `otherToken`, receive `borrowToken`)
    function execute(
        SimpleAMM buyPool,
        SimpleAMM sellPool,
        Token borrowToken,
        Token otherToken,
        uint256 amount,
        uint256 minProfit
    ) external returns (uint256 profit) {
        require(msg.sender == owner, "Arb: only owner");
        lender.flashLoan(this, address(borrowToken), amount, abi.encode(buyPool, sellPool, otherToken, minProfit));
        profit = borrowToken.balanceOf(address(this));
        require(borrowToken.transfer(owner, profit), "Arb: payout");
    }

    function onFlashLoan(address initiator, address token, uint256 amount, uint256 fee, bytes calldata data)
        external
        returns (bytes32)
    {
        require(msg.sender == address(lender) && initiator == address(this), "Arb: untrusted");
        (SimpleAMM buyPool, SimpleAMM sellPool, Token otherToken, uint256 minProfit) =
            abi.decode(data, (SimpleAMM, SimpleAMM, Token, uint256));

        Token(token).approve(address(buyPool), amount);
        uint256 got = buyPool.swap(token, amount, 0, address(this));
        otherToken.approve(address(sellPool), got);
        uint256 back = sellPool.swap(address(otherToken), got, 0, address(this));
        require(back >= amount + fee + minProfit, "Arb: unprofitable");

        Token(token).approve(address(lender), amount + fee);
        return lender.CALLBACK_SUCCESS();
    }
}
