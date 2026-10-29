// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Token} from "./Token.sol";
import {SimpleAMM} from "./SimpleAMM.sol";
import {SettlementLock} from "./SettlementLock.sol";

/// @notice Escrowed two-order batches. Match opposite intents at one price; swap residuals atomically.
/// @dev Permissionless solver chooses the price, but both full-order limits must hold. No fairness claim.
contract BatchSettlement is SettlementLock {
    Token public immutable token0;
    Token public immutable token1;
    SimpleAMM public immutable pool;
    uint256 public immutable batchBlocks;

    struct Order {
        address user;
        bool sell0;
        uint256 amount;
        uint256 minOut;
        uint256 deadline;
        uint256 createdBlock;
        bool closed;
    }
    Order[] public orders;
    event Placed(uint256 indexed id, address indexed user, bool sell0, uint256 amount);
    event Cancelled(uint256 indexed id);
    event Settled(
        uint256 indexed sell0Id,
        uint256 indexed sell1Id,
        uint256 price,
        uint256 matched0,
        uint256 matched1,
        uint256 out0Seller,
        uint256 out1Seller
    );

    constructor(SimpleAMM pool_, uint256 batchBlocks_) {
        require(batchBlocks_ > 0 && batchBlocks_ <= 20, "Batch: window");
        pool = pool_;
        token0 = pool_.token0();
        token1 = pool_.token1();
        batchBlocks = batchBlocks_;
    }

    function place(bool sell0, uint256 amount, uint256 minOut, uint256 deadline)
        external
        nonReentrant
        returns (uint256 id)
    {
        require(amount > 0 && minOut > 0 && deadline > block.timestamp, "Batch: order");
        Token input = sell0 ? token0 : token1;
        require(input.transferFrom(msg.sender, address(this), amount), "Batch: deposit");
        id = orders.length;
        orders.push(Order(msg.sender, sell0, amount, minOut, deadline, block.number, false));
        emit Placed(id, msg.sender, sell0, amount);
    }

    function cancel(uint256 id) external nonReentrant {
        Order storage o = orders[id];
        require(o.user == msg.sender && !o.closed, "Batch: owner/closed");
        o.closed = true;
        require((o.sell0 ? token0 : token1).transfer(o.user, o.amount), "Batch: refund");
        emit Cancelled(id);
    }

    function count() external view returns (uint256) {
        return orders.length;
    }

    function settle(uint256 sell0Id, uint256 sell1Id, uint256 price)
        external
        nonReentrant
        returns (uint256 outA, uint256 outB)
    {
        Order storage a = orders[sell0Id];
        Order storage b = orders[sell1Id];
        require(!a.closed && !b.closed && a.sell0 && !b.sell0, "Batch: sides/closed");
        require(block.timestamp <= a.deadline && block.timestamp <= b.deadline, "Batch: expired");
        require(
            block.number >= a.createdBlock + batchBlocks && block.number >= b.createdBlock + batchBlocks,
            "Batch: collecting"
        );
        require(price > 0, "Batch: price");
        uint256 match0 = b.amount * 1e18 / price;
        if (match0 > a.amount) match0 = a.amount;
        uint256 match1 = match0 * price / 1e18;
        require(match0 > 0 && match1 > 0, "Batch: dust");
        a.closed = true;
        b.closed = true;
        outA = match1;
        outB = match0;
        uint256 leftA = a.amount - match0;
        uint256 leftB = b.amount - match1;
        if (leftA > 0) {
            token0.approve(address(pool), leftA);
            outA += pool.swap(address(token0), leftA, 0, address(this));
        }
        if (leftB > 0) {
            token1.approve(address(pool), leftB);
            outB += pool.swap(address(token1), leftB, 0, address(this));
        }
        require(outA >= a.minOut && outB >= b.minOut, "Batch: limits");
        require(token1.transfer(a.user, outA) && token0.transfer(b.user, outB), "Batch: payout");
        emit Settled(sell0Id, sell1Id, price, match0, match1, outA, outB);
    }
}
