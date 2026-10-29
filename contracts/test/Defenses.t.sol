// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {Fixture} from "./Fixture.sol";
import {ShieldVault} from "../src/ShieldVault.sol";

contract ReentryProbe {
    ShieldVault vault;
    bool public reentered;
    constructor(ShieldVault v) { vault = v; }
    function fund() external payable { vault.deposit{value: msg.value}(); }
    function withdraw() external { vault.withdraw(0.001 ether); }
    receive() external payable {
        (reentered,) = address(vault).call(abi.encodeCall(vault.withdraw, (0.001 ether)));
    }
}

contract ShieldVaultTest is Test {
    ShieldVault vault;
    address alice = makeAddr("alice");
    function setUp() public { vault = new ShieldVault(); vm.deal(alice, 1 ether); }
    function _deposit() internal { vm.prank(alice); vault.deposit{value: 0.01 ether}(); }

    function test_cooldownBlocksImmediateWithdrawal() public {
        _deposit();
        vm.prank(alice);
        vm.expectRevert("Vault: cooldown active");
        vault.withdraw(0.001 ether);
        assertEq(vault.balances(alice), 0.01 ether);
    }
    function test_hourlyCapAggregatesWithdrawalsAndResets() public {
        _deposit(); vm.warp(block.timestamp + 60);
        vm.startPrank(alice);
        vault.withdraw(0.003 ether);
        vm.expectRevert("Vault: hourly limit"); vault.withdraw(0.003 ether);
        vault.withdraw(0.002 ether);
        vm.warp(block.timestamp + 1 hours);
        vault.withdraw(0.005 ether);
        vm.stopPrank(); assertEq(vault.balances(alice), 0);
    }
    function test_onlyOwnerCanPauseAndAccountingSurvivesPause() public {
        _deposit();
        vm.prank(alice); vm.expectRevert("Vault: owner only"); vault.setPaused(true);
        vault.setPaused(true); vm.warp(block.timestamp + 60);
        vm.prank(alice); vm.expectRevert("Vault: paused"); vault.withdraw(0.001 ether);
        vault.setPaused(false); vm.prank(alice); vault.withdraw(0.001 ether);
        assertEq(vault.balances(alice), 0.009 ether);
    }
    function test_reentrantWithdrawalCannotDrainBalance() public {
        ReentryProbe probe = new ReentryProbe(vault);
        vm.deal(address(this), 1 ether);
        probe.fund{value: 0.005 ether}(); vm.warp(block.timestamp + 60);
        probe.withdraw(); assertFalse(probe.reentered());
        assertEq(vault.balances(address(probe)), 0.004 ether);
    }
    function testFuzz_cannotWithdrawMoreThanDeposited(uint96 amount) public {
        _deposit(); vm.warp(block.timestamp + 60);
        uint256 excess = bound(uint256(amount), 0.01 ether + 1, 100 ether);
        vm.prank(alice); vm.expectRevert("Vault: insufficient balance"); vault.withdraw(excess);
        assertEq(vault.balances(alice), 0.01 ether);
    }
}

contract SafeLendingDefensesTest is Fixture {
    function test_sameBlockDepositCannotBorrow() public {
        vm.startPrank(alice); weth.approve(address(safe), 100 * E); safe.deposit(100 * E);
        vm.expectRevert("SafeLending: deposit cooldown"); safe.borrow(1 * E);
        vm.roll(block.number + 1); safe.borrow(10_000 * E);
        vm.stopPrank(); assertEq(safe.debtOf(alice), 10_000 * E);
    }
    function test_borrowCapAggregatesAndResetsNextBlock() public {
        vm.startPrank(alice); weth.approve(address(safe), 100 * E); safe.deposit(100 * E);
        vm.roll(block.number + 1); safe.borrow(60_000 * E);
        vm.expectRevert("SafeLending: block borrow limit"); safe.borrow(50_000 * E);
        vm.roll(block.number + 1); safe.borrow(50_000 * E);
        vm.stopPrank(); assertEq(safe.totalDebt(), 110_000 * E);
    }
}
