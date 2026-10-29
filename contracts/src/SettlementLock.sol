// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @dev Small lock shared by the local settlement contracts. Assets are the devnet Token type.
abstract contract SettlementLock {
    bool private entered;

    modifier nonReentrant() {
        require(!entered, "Settlement: reentrant");
        entered = true;
        _;
        entered = false;
    }
}
