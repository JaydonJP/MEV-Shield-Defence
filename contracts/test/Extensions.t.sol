// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Fixture} from "./Fixture.sol";
import {Token} from "../src/Token.sol";
import {SimpleAMM} from "../src/SimpleAMM.sol";
import {MEVRouter} from "../src/MEVRouter.sol";
import {BatchSettlement} from "../src/BatchSettlement.sol";
import {CashbackAuction} from "../src/CashbackAuction.sol";
import {FirewallLending} from "../src/FirewallLending.sol";

contract RoutingExtensionTest is Fixture {
    MEVRouter mr;

    function setUp() public override {
        super.setUp();
        mr = new MEVRouter();
        vm.prank(alice);
        usdc.approve(address(mr), type(uint256).max);
    }

    function _route(uint256 size, uint256 minimum, uint256 deadline) internal returns (uint256) {
        SimpleAMM[] memory pools = new SimpleAMM[](2);
        pools[0] = ammA;
        pools[1] = ammB;
        uint256[] memory amounts = new uint256[](2);
        amounts[0] = size / 2;
        amounts[1] = size - amounts[0];
        uint256[] memory mins = new uint256[](2);
        vm.prank(alice);
        return mr.swap(pools, amounts, mins, usdc, weth, minimum, deadline);
    }

    function testFuzz_atomicRouteConservesInput(uint96 raw) public {
        uint256 size = bound(uint256(raw), 2 * E, 100_000 * E);
        uint256 before = usdc.balanceOf(alice);
        uint256 w = weth.balanceOf(alice);
        uint256 got = _route(size, 0, block.timestamp + 1);
        assertEq(before - usdc.balanceOf(alice), size);
        assertEq(weth.balanceOf(alice) - w, got);
        assertEq(usdc.balanceOf(address(mr)), 0);
        assertEq(weth.balanceOf(address(mr)), 0);
    }

    function test_globalLimitRollsBackEveryLeg() public {
        (uint256 a0, uint256 a1,) = ammA.getReserves();
        uint256 before = usdc.balanceOf(alice);
        vm.expectRevert("Route: limit");
        _route(1000 * E, type(uint128).max, block.timestamp + 1);
        assertEq(usdc.balanceOf(alice), before);
        (uint256 b0, uint256 b1,) = ammA.getReserves();
        assertEq(a0, b0);
        assertEq(a1, b1);
    }

    function test_expiredRoute() public {
        vm.expectRevert("Route: expired");
        _route(1000 * E, 0, block.timestamp - 1);
    }

    function test_legLimitsAndDuplicatePools() public {
        SimpleAMM[] memory pools = new SimpleAMM[](2);
        pools[0] = ammA;
        pools[1] = ammB;
        uint256[] memory amounts = new uint256[](2);
        amounts[0] = 1000 * E;
        amounts[1] = 1000 * E;
        uint256[] memory mins = new uint256[](2);
        mins[0] = 1000 * E;
        vm.prank(alice);
        vm.expectRevert("AMM: slippage");
        mr.swap(pools, amounts, mins, usdc, weth, 0, block.timestamp + 1);
        pools[1] = ammA;
        vm.prank(alice);
        vm.expectRevert("Route: duplicate");
        mr.swap(pools, amounts, mins, usdc, weth, 0, block.timestamp + 1);
    }
}

contract BatchExtensionTest is Fixture {
    BatchSettlement batch;
    address bob = makeAddr("batch-bob");

    function setUp() public override {
        super.setUp();
        batch = new BatchSettlement(ammA, 1);
        usdc.mint(bob, 100_000 * E);
        vm.prank(bob);
        usdc.approve(address(batch), type(uint256).max);
        vm.prank(alice);
        weth.approve(address(batch), type(uint256).max);
    }

    function _orders(uint256 wethIn, uint256 usdcIn, uint256 minA, uint256 minB) internal {
        vm.prank(alice);
        batch.place(true, wethIn, minA, block.timestamp + 100);
        vm.prank(bob);
        batch.place(false, usdcIn, minB, block.timestamp + 100);
    }

    function test_exactMatchDoesNotTouchPool() public {
        _orders(E, 2000 * E, 1990 * E, E);
        vm.roll(block.number + 1);
        (uint256 a, uint256 b) = batch.settle(0, 1, 2000 * E);
        assertEq(a, 2000 * E);
        assertEq(b, E);
        assertEq(ammA.reserve0(), 100 * E);
        assertEq(weth.balanceOf(address(batch)), 0);
        assertEq(usdc.balanceOf(address(batch)), 0);
        vm.expectRevert("Batch: sides/closed");
        batch.settle(0, 1, 2000 * E);
    }

    function test_residualUsesAMM() public {
        _orders(2 * E, 2000 * E, 3000 * E, E);
        vm.roll(block.number + 1);
        (uint256 a, uint256 b) = batch.settle(0, 1, 2000 * E);
        assertGt(a, 3900 * E);
        assertEq(b, E);
        assertEq(ammA.reserve0(), 101 * E);
        assertEq(weth.balanceOf(address(batch)), 0);
    }

    function test_failedLimitPreservesEscrowAndAllowsCancel() public {
        _orders(E, 2000 * E, 3000 * E, E);
        vm.roll(block.number + 1);
        vm.expectRevert("Batch: limits");
        batch.settle(0, 1, 2000 * E);
        uint256 before = weth.balanceOf(alice);
        vm.prank(alice);
        batch.cancel(0);
        assertEq(weth.balanceOf(alice), before + E);
        vm.prank(bob);
        vm.expectRevert("Batch: owner/closed");
        batch.cancel(0);
    }

    function test_collectionWindowAndExpiry() public {
        _orders(E, 2000 * E, E, 1);
        vm.expectRevert("Batch: collecting");
        batch.settle(0, 1, 2000 * E);
        vm.roll(block.number + 1);
        vm.warp(block.timestamp + 101);
        vm.expectRevert("Batch: expired");
        batch.settle(0, 1, 2000 * E);
        vm.prank(alice);
        batch.cancel(0);
    }

    function test_otherOrdersEscrowRemainsIntact() public {
        _orders(E, 2000 * E, 1900 * E, E);
        vm.prank(alice);
        batch.place(true, E, 1900 * E, block.timestamp + 100);
        vm.roll(block.number + 1);
        batch.settle(0, 1, 2000 * E);
        assertEq(weth.balanceOf(address(batch)), E);
        vm.prank(alice);
        batch.cancel(2);
        assertEq(weth.balanceOf(address(batch)), 0);
    }
}

contract CashbackExtensionTest is Fixture {
    CashbackAuction auction;
    address searcher = makeAddr("searcher");

    function setUp() public override {
        super.setUp();
        auction = new CashbackAuction(weth, usdc);
        usdc.mint(searcher, 10_000 * E);
        vm.prank(searcher);
        usdc.approve(address(auction), type(uint256).max);
        vm.prank(alice);
        usdc.approve(address(auction), type(uint256).max);
    }

    function _bid(uint256 rebate, uint256 minOut) internal returns (uint256) {
        bytes32 order = auction.orderHash(alice, ammA, 1000 * E, minOut, block.timestamp + 100);
        vm.prank(searcher);
        return auction.bid(ammA, ammB, 1000 * E, rebate, 0, block.timestamp + 100, order);
    }

    function test_atomicCashbackAndSearcherPayout() public {
        uint256 id = _bid(E, 4 * E / 10);
        uint256 u = usdc.balanceOf(alice);
        uint256 w = weth.balanceOf(alice);
        vm.prank(alice);
        (uint256 got, uint256 rebate) = auction.settle(id, ammA, 1000 * E, 4 * E / 10, block.timestamp + 100);
        assertEq(usdc.balanceOf(alice), u - 1000 * E + rebate);
        assertEq(weth.balanceOf(alice) - w, got);
        assertGt(usdc.balanceOf(searcher), 10_000 * E);
        assertEq(usdc.balanceOf(address(auction)), 0);
        vm.prank(alice);
        vm.expectRevert("Auction: closed/expired");
        auction.settle(id, ammA, 1000 * E, 1, block.timestamp + 1);
    }

    function test_unprofitableBidRollsBackUserAndCanBeRefunded() public {
        uint256 id = _bid(10_000 * E, 1);
        uint256 before = usdc.balanceOf(alice);
        vm.prank(alice);
        vm.expectRevert("Auction: unprofitable");
        auction.settle(id, ammA, 1000 * E, 1, block.timestamp + 100);
        assertEq(usdc.balanceOf(alice), before);
        assertEq(ammA.reserve1(), 200_000 * E);
        vm.prank(searcher);
        auction.cancel(id);
        assertEq(usdc.balanceOf(searcher), 10_000 * E);
    }

    function test_searcherOwnsCancellation() public {
        uint256 id = _bid(E, 1);
        vm.prank(attacker);
        vm.expectRevert("Auction: owner/closed");
        auction.cancel(id);
    }

    function test_expiredBidAndUserLimit() public {
        uint256 id = _bid(E, 100 * E);
        vm.prank(alice);
        vm.expectRevert("AMM: slippage");
        auction.settle(id, ammA, 1000 * E, 100 * E, block.timestamp + 100);
        vm.warp(block.timestamp + 101);
        vm.prank(alice);
        vm.expectRevert("Auction: closed/expired");
        auction.settle(id, ammA, 1000 * E, 1, block.timestamp + 1);
    }

    function test_bidCannotBeStolenByAnotherUserOrChangedIntent() public {
        uint256 id = _bid(E, 1);
        vm.prank(attacker);
        vm.expectRevert("Auction: intent mismatch");
        auction.settle(id, ammA, 1000 * E, 1, block.timestamp + 100);
        vm.prank(alice);
        vm.expectRevert("Auction: intent mismatch");
        auction.settle(id, ammA, 999 * E, 1, block.timestamp + 100);
    }
}

contract FirewallExtensionTest is Fixture {
    FirewallLending fw;

    function setUp() public override {
        super.setUp();
        fw = new FirewallLending(weth, usdc, ammA, guardian, oracle, 500, 10_000 * E, 25_000 * E, 10);
        usdc.mint(address(fw), 1_000_000 * E);
        vm.startPrank(alice);
        weth.approve(address(fw), type(uint256).max);
        fw.deposit(100 * E);
        vm.stopPrank();
    }

    function test_sameBlockBudgetAndRepaymentDoesNotResetIt() public {
        vm.startPrank(alice);
        fw.borrow(8000 * E);
        usdc.approve(address(fw), type(uint256).max);
        fw.repay(8000 * E);
        vm.expectRevert("Firewall: block budget");
        fw.borrow(3000 * E);
        vm.stopPrank();
        assertEq(fw.totalDebt(), 0);
        assertEq(fw.borrowedAt(block.number), 8000 * E);
    }

    function test_windowIsSharedAcrossBlocksAndExpires() public {
        for (uint256 i; i < 3; i++) {
            vm.prank(alice);
            fw.borrow(8000 * E);
            vm.roll(block.number + 1);
        }
        vm.prank(alice);
        vm.expectRevert("Firewall: window budget");
        fw.borrow(2000 * E);
        vm.roll(block.number + 10);
        vm.prank(alice);
        fw.borrow(2000 * E);
        assertEq(fw.windowUsed(), 2000 * E);
    }

    function test_budgetIsGlobalAcrossUsers() public {
        weth.mint(bot, 100 * E);
        vm.startPrank(bot);
        weth.approve(address(fw), type(uint256).max);
        fw.deposit(100 * E);
        fw.borrow(6000 * E);
        vm.stopPrank();
        vm.prank(alice);
        vm.expectRevert("Firewall: block budget");
        fw.borrow(5000 * E);
    }

    function test_onlyOwnerMayChangeLimits() public {
        vm.prank(attacker);
        vm.expectRevert("Firewall: owner");
        fw.setLimits(1, 2, 10);
        vm.expectRevert("Firewall: limits");
        fw.setLimits(E, E, 65);
    }

    function test_lowerOracleConfidenceTightensBudgets() public {
        vm.startPrank(alice);
        usdc.approve(address(ammA), 3000 * E);
        ammA.swap(address(usdc), 3000 * E, 0, alice);
        (uint256 perBlock, uint256 perWindow) = fw.effectiveLimits();
        assertEq(perBlock, 5000 * E);
        assertEq(perWindow, 12_500 * E);
        vm.expectRevert("Firewall: block budget");
        fw.borrow(6000 * E);
        vm.stopPrank();
        assertEq(fw.totalDebt(), 0);
    }

    function testFuzz_outflowNeverExceedsBlockLimit(uint96 raw) public {
        uint256 amount = bound(uint256(raw), 1, 150_000 * E);
        vm.prank(alice);
        (bool ok,) = address(fw).call(abi.encodeCall(fw.borrow, (amount)));
        assertEq(ok, amount <= 10_000 * E);
        assertLe(fw.borrowedAt(block.number), 10_000 * E);
    }
}
