// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {Token} from "../src/Token.sol";
import {SimpleAMM} from "../src/SimpleAMM.sol";
import {FlashLender} from "../src/FlashLender.sol";
import {TWAPOracle} from "../src/TWAPOracle.sol";
import {VulnerableLending} from "../src/VulnerableLending.sol";
import {SafeLending} from "../src/SafeLending.sol";
import {ProtectedRouter} from "../src/ProtectedRouter.sol";
import {CommitRevealSwap} from "../src/CommitRevealSwap.sol";
import {VulnerableDAO, SnapshotDAO} from "../src/Governance.sol";

/// @dev Same world as `mevshield up` (see mevshield/config.py), deployed in-process for unit tests.
contract Fixture is Test {
    Token weth;
    Token usdc;
    Token gov;
    SimpleAMM ammA; // main pool, read by the lending markets
    SimpleAMM ammB; // second venue, slightly dislocated (arbitrage opportunity)
    FlashLender lender;
    TWAPOracle oracle;
    VulnerableLending vuln;
    SafeLending safe;
    ProtectedRouter router;
    CommitRevealSwap cr;
    VulnerableDAO vdao;
    SnapshotDAO sdao;

    address guardian = makeAddr("guardian");
    address alice = makeAddr("alice");
    address attacker = makeAddr("attacker");
    address bot = makeAddr("bot");

    uint256 constant E = 1e18;

    function setUp() public virtual {
        vm.warp(1_700_000_000);
        vm.roll(100);
        weth = new Token("Mock Wrapped Ether", "WETH");
        usdc = new Token("Mock USD Coin", "USDC");
        gov = new Token("Shield DAO Governance", "GOV");

        ammA = new SimpleAMM(weth, usdc, 5);
        ammB = new SimpleAMM(weth, usdc, 5);
        _seedPool(ammA, 100 * E, 200_000 * E);
        _seedPool(ammB, 100 * E, 206_000 * E);

        lender = new FlashLender();
        usdc.mint(address(lender), 5_000_000 * E);
        weth.mint(address(lender), 5_000 * E);
        gov.mint(address(lender), 600_000 * E);
        gov.mint(alice, 400_000 * E);

        vm.warp(block.timestamp + 600);
        vm.roll(block.number + 50);
        oracle = new TWAPOracle(ammA, 60);
        vm.warp(block.timestamp + 600);
        vm.roll(block.number + 50);

        vuln = new VulnerableLending(weth, usdc, ammA, guardian);
        safe = new SafeLending(weth, usdc, ammA, guardian, oracle, 500);
        usdc.mint(address(vuln), 1_000_000 * E);
        usdc.mint(address(safe), 1_000_000 * E);

        router = new ProtectedRouter();
        cr = new CommitRevealSwap();

        vdao = new VulnerableDAO(gov, guardian);
        sdao = new SnapshotDAO(gov, guardian);
        usdc.mint(address(vdao), 500_000 * E);
        usdc.mint(address(sdao), 500_000 * E);

        usdc.mint(alice, 1_000_000 * E);
        weth.mint(alice, 500 * E);
        vm.roll(block.number + 1);
    }

    function _seedPool(SimpleAMM amm, uint256 a0, uint256 a1) internal {
        weth.mint(address(this), a0);
        usdc.mint(address(this), a1);
        weth.approve(address(amm), a0);
        usdc.approve(address(amm), a1);
        amm.addLiquidity(a0, a1);
    }

    /// @dev Exact replica of SimpleAMM.getAmountOut for expectations.
    function _out(uint256 amountIn, uint256 rIn, uint256 rOut, uint256 feeBps) internal pure returns (uint256) {
        uint256 inWithFee = amountIn * (10_000 - feeBps);
        return (inWithFee * rOut) / (rIn * 10_000 + inWithFee);
    }
}
