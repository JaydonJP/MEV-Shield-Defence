// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {StdInvariant} from "forge-std/StdInvariant.sol";
import {Fixture} from "./Fixture.sol";
import {ShieldVault} from "../src/ShieldVault.sol";
import {FirewallLending} from "../src/FirewallLending.sol";
import {Token} from "../src/Token.sol";
import {BatchSettlement} from "../src/BatchSettlement.sol";
import {CashbackAuction} from "../src/CashbackAuction.sol";
import {SimpleAMM} from "../src/SimpleAMM.sol";

contract VaultHandler is Test {
    ShieldVault public vault;
    address[3] public actors;
    uint256 public deposited;
    uint256 public withdrawn;
    uint256 public successfulDeposits;
    uint256 public successfulWithdrawals;
    bool public policyBroken;

    constructor() {
        vault = new ShieldVault();
        for (uint256 i; i < 3; i++) { actors[i] = address(uint160(100 + i)); vm.deal(actors[i], 10 ether); }
    }
    function deposit(uint256 who, uint96 raw) external {
        address user = actors[who % 3];
        uint256 amount = bound(uint256(raw), 1, 0.01 ether);
        if (user.balance < amount) return;
        vm.prank(user);
        (bool ok,) = address(vault).call{value: amount}(abi.encodeCall(vault.deposit, ()));
        if (ok) { deposited += amount; successfulDeposits++; }
    }
    function withdraw(uint256 who, uint96 raw) external {
        address user = actors[who % 3];
        uint256 amount = bound(uint256(raw), 1, 0.006 ether);
        uint256 balance = vault.balances(user);
        bool eligible = !vault.paused() && block.timestamp >= vault.unlockAt(user) && amount <= balance;
        uint256 used = block.timestamp >= vault.windowStart(user) + vault.WINDOW()
            ? 0 : vault.withdrawnInWindow(user);
        eligible = eligible && used + amount <= vault.WITHDRAWAL_LIMIT();
        vm.prank(user);
        (bool ok,) = address(vault).call(abi.encodeCall(vault.withdraw, (amount)));
        if (ok) { withdrawn += amount; successfulWithdrawals++; if (!eligible) policyBroken = true; }
        else if (vault.balances(user) != balance) policyBroken = true;
    }
    function advance(uint32 elapsed) external { vm.warp(block.timestamp + bound(elapsed, 1, 2 hours)); }
    function pause(bool state) external { vault.setPaused(state); }
    function unauthorizedPause(uint256 who) external {
        vm.prank(actors[who % 3]);
        (bool ok,) = address(vault).call(abi.encodeCall(vault.setPaused, (true)));
        if (ok) policyBroken = true;
    }
    function donate(uint96 raw) external {
        vm.deal(address(vault), address(vault).balance + bound(uint256(raw), 1, 0.01 ether));
    }
}

contract VaultInvariantTest is StdInvariant, Test {
    VaultHandler handler;
    function setUp() public {
        handler = new VaultHandler();
        targetContract(address(handler));
    }
    function invariant_backingAndGhostAccounting() public view {
        ShieldVault v = handler.vault();
        uint256 claims;
        for (uint256 i; i < 3; i++) {
            claims += v.balances(handler.actors(i));
            assertLe(v.withdrawnInWindow(handler.actors(i)), v.WITHDRAWAL_LIMIT());
        }
        assertEq(claims, handler.deposited() - handler.withdrawn());
        assertGe(address(v).balance, claims); // unsolicited ETH must not break the invariant
    }
    function invariant_policyAndAuthorization() public view { assertFalse(handler.policyBroken()); }
    function test_handlerExercisesSuccessfulAndRejectedActions() public {
        handler.deposit(0, 0.004 ether);
        handler.withdraw(0, 0.001 ether); // must fail during cooldown
        handler.advance(61);
        handler.withdraw(0, 0.001 ether);
        handler.unauthorizedPause(1);
        assertEq(handler.successfulDeposits(), 1);
        assertEq(handler.successfulWithdrawals(), 1);
        invariant_backingAndGhostAccounting(); invariant_policyAndAuthorization();
    }
}

contract LendingHandler is Test {
    FirewallLending public market;
    Token public weth;
    Token public usdc;
    address[3] public actors;
    uint256 public borrowSuccesses;
    uint256 public repaySuccesses;
    uint256 public collateralActions;
    bool public unauthorized;
    constructor(FirewallLending m, Token w, Token u) {
        market = m; weth = w; usdc = u;
        for (uint256 i; i < 3; i++) actors[i] = address(uint160(200 + i));
    }
    function borrow(uint256 who, uint96 raw) external {
        vm.prank(actors[who % 3]);
        (bool ok,) = address(market).call(abi.encodeCall(market.borrow, (bound(uint256(raw), 1, 20_000 ether))));
        if (ok) borrowSuccesses++;
    }
    function repay(uint256 who, uint96 raw) external {
        address user = actors[who % 3];
        uint256 cap = market.debtOf(user);
        if (cap == 0) return;
        vm.prank(user);
        (bool ok,) = address(market).call(abi.encodeCall(market.repay, (bound(uint256(raw), 1, cap))));
        if (ok) repaySuccesses++;
    }
    function deposit(uint256 who, uint96 raw) external {
        vm.prank(actors[who % 3]);
        (bool ok,) = address(market).call(abi.encodeCall(market.deposit, (bound(uint256(raw), 1, 5 ether))));
        if (ok) collateralActions++;
    }
    function withdraw(uint256 who, uint96 raw) external {
        vm.prank(actors[who % 3]);
        (bool ok,) = address(market).call(abi.encodeCall(market.withdraw, (bound(uint256(raw), 1, 5 ether))));
        if (ok) collateralActions++;
    }
    function advance(uint8 blocks_) external {
        uint256 step = bound(uint256(blocks_), 1, 12);
        vm.roll(block.number + step); vm.warp(block.timestamp + step * 12);
    }
    function unauthorizedAdmin(uint256 who) external {
        vm.prank(actors[who % 3]);
        (bool ok,) = address(market).call(abi.encodeCall(market.setLimits, (100_000 ether, 200_000 ether, 1)));
        if (ok) unauthorized = true;
    }
}

contract LendingInvariantTest is Fixture {
    LendingHandler handler;
    FirewallLending market;
    function setUp() public override {
        super.setUp();
        market = new FirewallLending(weth, usdc, ammA, guardian, oracle, 500, 10_000 ether, 25_000 ether, 10);
        usdc.mint(address(market), 1_000_000 ether);
        handler = new LendingHandler(market, weth, usdc);
        for (uint256 i; i < 3; i++) {
            address user = handler.actors(i);
            weth.mint(user, 110 ether); usdc.mint(user, 1_000_000 ether);
            vm.startPrank(user);
            weth.approve(address(market), type(uint256).max);
            usdc.approve(address(market), type(uint256).max);
            market.deposit(100 ether);
            vm.stopPrank();
        }
        vm.roll(block.number + 1);
        targetContract(address(handler));
    }
    function invariant_cashDebtAndCollateralReconcile() public view {
        uint256 debt; uint256 collateral;
        for (uint256 i; i < 3; i++) {
            debt += market.debtOf(handler.actors(i));
            collateral += market.collateralOf(handler.actors(i));
        }
        assertEq(market.totalDebt(), debt);
        assertEq(market.totalCollateral(), collateral);
        assertEq(weth.balanceOf(address(market)), collateral);
        assertEq(usdc.balanceOf(address(market)) + debt, 1_000_000 ether);
    }
    function invariant_globalOutflowBudgetsAndAuthorization() public view {
        assertLe(market.borrowedAt(block.number), market.blockLimit());
        assertLe(market.windowUsed(), market.windowLimit());
        assertFalse(handler.unauthorized());
    }
    function test_handlerIsNotVacuousAndRepayWorksWhilePaused() public {
        handler.borrow(0, 8000 ether);
        vm.prank(guardian); market.pause();
        handler.repay(0, 1000 ether);
        assertEq(handler.borrowSuccesses(), 1); assertEq(handler.repaySuccesses(), 1);
        invariant_cashDebtAndCollateralReconcile();
    }
    function test_multiActorBudgetRejectsSecondBorrow() public {
        handler.borrow(0, 8000 ether); handler.borrow(1, 8000 ether);
        assertEq(handler.borrowSuccesses(), 1);
        invariant_globalOutflowBudgetsAndAuthorization();
    }
}

contract SettlementHandler is Test {
    BatchSettlement public batch;
    CashbackAuction public auction;
    SimpleAMM public a;
    SimpleAMM public b;
    Token public weth;
    Token public usdc;
    address[3] public actors;
    uint256 public placed;
    uint256 public cancelled;
    uint256 public settled;
    constructor(BatchSettlement batch_, CashbackAuction auction_, SimpleAMM a_, SimpleAMM b_) {
        batch = batch_; auction = auction_; a = a_; b = b_; weth = a.token0(); usdc = a.token1();
        for (uint256 i; i < 3; i++) actors[i] = address(uint160(300 + i));
    }
    function place(uint256 who, bool sell0, uint96 raw) external {
        vm.prank(actors[who % 3]);
        uint256 amt = bound(uint256(raw), 1e12, sell0 ? 1 ether : 1000 ether);
        try batch.place(sell0, amt, 1, block.timestamp + 300) { placed++; } catch {}
    }
    function cancel(uint256 id) external {
        if (batch.count() == 0) return;
        id %= batch.count();
        (address user,,,,,,) = batch.orders(id);
        vm.prank(user);
        try batch.cancel(id) { cancelled++; } catch {}
    }
    function settle(uint256 x, uint256 y, uint256 rawPrice) external {
        if (batch.count() < 2) return;
        try batch.settle(x % batch.count(), y % batch.count(), bound(rawPrice, 1000 ether, 3000 ether)) {
            settled++;
        } catch {}
    }
    function bid(uint256 who, uint96 raw) external {
        uint256 deadline = block.timestamp + 300;
        bytes32 order = auction.orderHash(actors[0], a, 1000 ether, 1, deadline);
        vm.prank(actors[who % 3]);
        try auction.bid(a, b, bound(uint256(raw), 1 ether, 5000 ether), 1, 0, deadline, order) {} catch {}
    }
    function cancelBid(uint256 id) external {
        if (auction.count() == 0) return;
        id %= auction.count();
        (address user,,,,,,,,) = auction.bids(id);
        vm.prank(user);
        try auction.cancel(id) {} catch {}
    }
    function settleBid(uint256 id) external {
        if (auction.count() == 0) return;
        id %= auction.count();
        (,,,,,,uint256 deadline,,) = auction.bids(id);
        vm.prank(actors[0]);
        try auction.settle(id, a, 1000 ether, 1, deadline) {} catch {}
    }
    function advance(uint8 n) external {
        uint256 step = bound(uint256(n), 1, 30); vm.roll(block.number + step); vm.warp(block.timestamp + step * 12);
    }
}

contract SettlementInvariantTest is Fixture {
    SettlementHandler handler;
    BatchSettlement batch;
    CashbackAuction auction;
    function setUp() public override {
        super.setUp();
        batch = new BatchSettlement(ammA, 1); auction = new CashbackAuction(weth, usdc);
        handler = new SettlementHandler(batch, auction, ammA, ammB);
        for (uint256 i; i < 3; i++) {
            address user = handler.actors(i);
            weth.mint(user, 1000 ether); usdc.mint(user, 1_000_000 ether);
            vm.startPrank(user);
            weth.approve(address(batch), type(uint256).max); usdc.approve(address(batch), type(uint256).max);
            usdc.approve(address(auction), type(uint256).max);
            vm.stopPrank();
        }
        targetContract(address(handler));
    }
    function invariant_openOrderAndBidEscrowsAreBacked() public view {
        uint256 w; uint256 u; uint256 bids;
        for (uint256 i; i < batch.count(); i++) {
            (,bool sell0,uint256 amount,,,,bool closed) = batch.orders(i);
            if (!closed) { if (sell0) w += amount; else u += amount; }
        }
        for (uint256 i; i < auction.count(); i++) {
            (,,,uint256 capital,,,,,bool closed) = auction.bids(i);
            if (!closed) bids += capital;
        }
        assertEq(weth.balanceOf(address(batch)), w); assertEq(usdc.balanceOf(address(batch)), u);
        assertEq(usdc.balanceOf(address(auction)), bids); assertEq(weth.balanceOf(address(auction)), 0);
    }
    function test_handlerExercisesSettlementAndCancellation() public {
        handler.place(0, true, 1 ether); handler.place(1, false, 1000 ether);
        handler.advance(1); handler.settle(0, 1, 2000 ether);
        handler.place(2, true, 1 ether); handler.cancel(2);
        handler.bid(1, 100 ether); handler.cancelBid(0);
        assertEq(handler.placed(), 3); assertEq(handler.settled(), 1); assertEq(handler.cancelled(), 1);
        invariant_openOrderAndBidEscrowsAreBacked();
    }
}
