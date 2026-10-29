// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Token} from "./Token.sol";
import {SimpleAMM} from "./SimpleAMM.sol";
import {SettlementLock} from "./SettlementLock.sol";

/// @notice Funded backrun bids. A user selects a bid; swap, backrun and cashback settle in one call.
/// @dev Local pair prototype: user sells token1 for token0. No arbitrary searcher callbacks.
contract CashbackAuction is SettlementLock {
    Token public immutable token0;
    Token public immutable token1;

    struct Bid {
        address searcher;
        SimpleAMM buy;
        SimpleAMM sell;
        uint256 capital;
        uint256 rebate;
        uint256 minProfit;
        uint256 deadline;
        bytes32 order;
        bool closed;
    }
    Bid[] public bids;
    event BidPlaced(uint256 indexed id, address indexed searcher, uint256 rebate);
    event BidCancelled(uint256 indexed id);
    event Cashback(uint256 indexed id, address indexed user, uint256 swapOut, uint256 rebate, uint256 profit);

    constructor(Token token0_, Token token1_) {
        require(address(token0_) != address(token1_), "Auction: tokens");
        token0 = token0_;
        token1 = token1_;
    }

    function _pair(SimpleAMM p) internal view {
        require(address(p.token0()) == address(token0) && address(p.token1()) == address(token1), "Auction: pair");
    }

    function orderHash(address user, SimpleAMM userPool, uint256 amountIn, uint256 minOut, uint256 deadline)
        public
        view
        returns (bytes32)
    {
        return keccak256(abi.encode(address(this), block.chainid, user, userPool, amountIn, minOut, deadline));
    }

    function bid(
        SimpleAMM buy,
        SimpleAMM sell,
        uint256 capital,
        uint256 rebate,
        uint256 minProfit,
        uint256 deadline,
        bytes32 order
    ) external nonReentrant returns (uint256 id) {
        _pair(buy);
        _pair(sell);
        require(address(buy) != address(sell), "Auction: pools");
        require(capital > 0 && rebate > 0 && deadline > block.timestamp && order != bytes32(0), "Auction: bid");
        require(token1.transferFrom(msg.sender, address(this), capital), "Auction: fund");
        id = bids.length;
        bids.push(Bid(msg.sender, buy, sell, capital, rebate, minProfit, deadline, order, false));
        emit BidPlaced(id, msg.sender, rebate);
    }

    function count() external view returns (uint256) {
        return bids.length;
    }

    function cancel(uint256 id) external nonReentrant {
        Bid storage b = bids[id];
        require(msg.sender == b.searcher && !b.closed, "Auction: owner/closed");
        b.closed = true;
        require(token1.transfer(b.searcher, b.capital), "Auction: refund");
        emit BidCancelled(id);
    }

    function settle(uint256 id, SimpleAMM userPool, uint256 amountIn, uint256 minOut, uint256 deadline)
        external
        nonReentrant
        returns (uint256 userOut, uint256 rebate)
    {
        Bid storage b = bids[id];
        _pair(userPool);
        require(!b.closed && block.timestamp <= b.deadline && block.timestamp <= deadline, "Auction: closed/expired");
        require(amountIn > 0 && minOut > 0, "Auction: order");
        require(b.order == orderHash(msg.sender, userPool, amountIn, minOut, deadline), "Auction: intent mismatch");
        b.closed = true;
        require(token1.transferFrom(msg.sender, address(this), amountIn), "Auction: input");
        token1.approve(address(userPool), amountIn);
        userOut = userPool.swap(address(token1), amountIn, minOut, msg.sender);
        token1.approve(address(b.buy), b.capital);
        uint256 bought = b.buy.swap(address(token1), b.capital, 0, address(this));
        token0.approve(address(b.sell), bought);
        uint256 back = b.sell.swap(address(token0), bought, 0, address(this));
        require(back >= b.capital + b.rebate + b.minProfit, "Auction: unprofitable");
        rebate = b.rebate;
        require(token1.transfer(msg.sender, rebate), "Auction: rebate");
        require(token1.transfer(b.searcher, back - rebate), "Auction: profit");
        emit Cashback(id, msg.sender, userOut, rebate, back - b.capital - rebate);
    }
}
