// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Token} from "./Token.sol";

interface IFlashBorrower {
    /// @return keccak256("FlashBorrower.onFlashLoan") on success.
    function onFlashLoan(address initiator, address token, uint256 amount, uint256 fee, bytes calldata data)
        external
        returns (bytes32);
}

/// @title FlashLender — Aave/ERC-3156-style flash loans over any token it holds (0.09 % fee).
contract FlashLender {
    bytes32 public constant CALLBACK_SUCCESS = keccak256("FlashBorrower.onFlashLoan");
    uint256 public constant FEE_BPS = 9;

    event FlashLoan(
        address indexed receiver, address indexed initiator, address indexed token, uint256 amount, uint256 fee
    );

    bool private _locked;

    function maxFlashLoan(address token) external view returns (uint256) {
        return Token(token).balanceOf(address(this));
    }

    function flashFee(address, uint256 amount) public pure returns (uint256) {
        return (amount * FEE_BPS) / 10_000;
    }

    function flashLoan(IFlashBorrower receiver, address token, uint256 amount, bytes calldata data)
        external
        returns (bool)
    {
        require(!_locked, "FlashLender: reentrant");
        _locked = true;
        Token t = Token(token);
        uint256 balanceBefore = t.balanceOf(address(this));
        require(amount > 0 && amount <= balanceBefore, "FlashLender: amount");
        uint256 fee = flashFee(token, amount);

        require(t.transfer(address(receiver), amount), "FlashLender: transfer");
        require(
            receiver.onFlashLoan(msg.sender, token, amount, fee, data) == CALLBACK_SUCCESS, "FlashLender: callback"
        );
        require(t.transferFrom(address(receiver), address(this), amount + fee), "FlashLender: repay");
        require(t.balanceOf(address(this)) >= balanceBefore + fee, "FlashLender: not repaid");

        emit FlashLoan(address(receiver), msg.sender, token, amount, fee);
        _locked = false;
        return true;
    }
}
