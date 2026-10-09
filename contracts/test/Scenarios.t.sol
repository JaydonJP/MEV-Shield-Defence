// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Fixture} from "./Fixture.sol";
import {Token} from "../src/Token.sol";
import {SimpleAMM} from "../src/SimpleAMM.sol";
import {OracleAttacker} from "../src/harness/OracleAttacker.sol";
import {FlashArbitrage} from "../src/harness/FlashArbitrage.sol";
import {SandwichAttacker} from "../src/harness/SandwichAttacker.sol";
import {GovernanceAttacker} from "../src/harness/GovernanceAttacker.sol";

/// @notice T2 — flash-loan oracle manipulation against the three targets of the demo.
contract OracleAttackTest is Fixture {
    OracleAttacker atk;
    uint256 constant FLASH = 450_000 * 1e18;

    function setUp() public override {
        super.setUp();
        vm.prank(attacker);
        atk = new OracleAttacker(lender);
    }

    function test_attackDrainsVulnerableLending() public {
        uint256 liqBefore = vuln.liquidity();
        vm.prank(attacker);
        uint256 profit = atk.attack(vuln, FLASH);
        assertGt(profit, 400_000 * E, "attacker profits");
        assertEq(usdc.balanceOf(attacker), profit);
        assertEq(vuln.liquidity(), 0, "lending drained");
        // bad debt: debt far above the collateral's fair value (2000 USDC/WETH)
        uint256 fairCollateral = (vuln.collateralOf(address(atk)) * 2_000 * E) / E;
        assertGt(vuln.debtOf(address(atk)), fairCollateral + 500_000 * E);
        assertEq(liqBefore, 1_000_000 * E);
    }

    function test_attackRevertsWhenGuardianPausedFirst() public {
        vm.prank(guardian);
        vuln.pause(); // ordered before the attack in the same block
        vm.prank(attacker);
        vm.expectRevert("Lending: paused");
        atk.attack(vuln, FLASH);
        assertEq(vuln.liquidity(), 1_000_000 * E, "funds safe");
    }

    function test_attackFailsAgainstSafeLending() public {
        vm.prank(attacker);
        vm.expectRevert("SafeLending: price deviation");
        atk.attack(safe, FLASH);
        assertEq(safe.liquidity(), 1_000_000 * E);
    }
}

/// @notice T3 — benign flash-loan arbitrage: profitable, converges prices, harms nobody.
contract ArbitrageTest is Fixture {
    function test_flashArbitrageIsProfitableAndConvergesPrices() public {
        vm.prank(attacker);
        FlashArbitrage arb = new FlashArbitrage(lender);
        uint256 gapBefore = ammB.spotPrice0() - ammA.spotPrice0();
        vm.prank(attacker);
        uint256 profit = arb.execute(ammA, ammB, usdc, weth, 2_000 * E, 0);
        assertGt(profit, 0);
        assertEq(usdc.balanceOf(attacker), profit);
        uint256 pa = ammA.spotPrice0();
        uint256 pb = ammB.spotPrice0();
        uint256 gapAfter = pb > pa ? pb - pa : pa - pb;
        assertLt(gapAfter, gapBefore, "prices move towards each other");
        assertEq(vuln.liquidity(), 1_000_000 * E, "no protocol touched");
    }
}

/// @notice T1 — sandwich; and the two on-chain user defenses (tight minOut, commit-reveal).
contract SandwichTest is Fixture {
    SandwichAttacker sw;

    function setUp() public override {
        super.setUp();
        vm.prank(bot);
        sw = new SandwichAttacker();
        usdc.mint(address(sw), 2_000_000 * E);
        weth.mint(address(sw), 1_000 * E);
        vm.prank(alice);
        usdc.approve(address(router), type(uint256).max);
        vm.prank(alice);
        usdc.approve(address(cr), type(uint256).max);
    }

    function _victimSwap(uint256 amountIn, uint256 minOut) internal returns (uint256) {
        vm.prank(alice);
        return router.swapExactIn(ammA, address(usdc), amountIn, minOut, block.timestamp + 60);
    }

    function test_sandwichHurtsVictim() public {
        uint256 v = 10_000 * E;
        uint256 benignOut = ammA.quote(address(usdc), v);
        uint256 minOut = (benignOut * 98) / 100; // 2 % slippage tolerance
        uint256 usdcBefore = usdc.balanceOf(address(sw));

        vm.prank(bot);
        sw.frontrun(ammA, address(usdc), 1_000 * E, 0);
        uint256 victimOut = _victimSwap(v, minOut);
        vm.prank(bot);
        sw.backrun(ammA, address(weth), 0);

        assertLt(victimOut, benignOut, "victim receives less");
        assertGe(victimOut, minOut);
        assertGt(usdc.balanceOf(address(sw)), usdcBefore, "attacker profits");
    }

    function test_tightMinOutMakesSandwichRevertVictim() public {
        uint256 v = 10_000 * E;
        uint256 benignOut = ammA.quote(address(usdc), v);
        vm.prank(bot);
        sw.frontrun(ammA, address(usdc), 3_000 * E, 0);
        vm.prank(alice);
        vm.expectRevert("AMM: slippage");
        router.swapExactIn(ammA, address(usdc), v, (benignOut * 999) / 1000, block.timestamp + 60);
    }

    function test_commitReveal() public {
        uint256 v = 10_000 * E;
        uint256 minOut = (ammA.quote(address(usdc), v) * 98) / 100;
        bytes32 salt = keccak256("salt");
        bytes32 h = cr.commitmentHash(alice, address(ammA), address(usdc), v, minOut, salt);
        vm.prank(alice);
        cr.commit(h);

        vm.prank(alice);
        vm.expectRevert("CR: reveal too early");
        cr.reveal(ammA, address(usdc), v, minOut, salt);

        vm.roll(block.number + 1);
        // a front-run in the reveal block makes the reveal revert => sandwich earns nothing
        vm.prank(bot);
        sw.frontrun(ammA, address(usdc), 1_000 * E, 0);
        vm.prank(alice);
        vm.expectRevert("CR: pool already traded this block");
        cr.reveal(ammA, address(usdc), v, minOut, salt);

        // undisturbed in the next block it executes
        vm.roll(block.number + 1);
        vm.prank(alice);
        uint256 out = cr.reveal(ammA, address(usdc), v, minOut, salt);
        assertGe(out, minOut);
    }
}

/// @notice T4 (stretch) — flash-loan governance takeover vs snapshot voting.
contract GovernanceAttackTest is Fixture {
    GovernanceAttacker atk;

    function setUp() public override {
        super.setUp();
        vm.prank(attacker);
        atk = new GovernanceAttacker(lender);
        vm.prank(alice);
        gov.transfer(address(atk), 1_000 * E); // covers the flash fee
        vm.roll(block.number + 1);
    }

    function test_flashGovernanceDrainsVulnerableDAO() public {
        vm.prank(attacker);
        uint256 profit = atk.attack(vdao, usdc, 550_000 * E);
        assertEq(profit, 500_000 * E);
        assertEq(usdc.balanceOf(address(vdao)), 0);
    }

    function test_snapshotVotingDefeatsFlashGovernance() public {
        // the attacker's 1k pre-owned tokens can vote, the 550k flash-borrowed ones cannot -> no quorum
        vm.prank(attacker);
        vm.expectRevert("DAO: timelock");
        atk.attack(sdao, usdc, 550_000 * E);
        assertEq(usdc.balanceOf(address(sdao)), 500_000 * E);
    }

    function test_flashBorrowedTokensHaveNoSnapshotVotes() public {
        vm.prank(address(lender));
        gov.transfer(bot, 550_000 * E); // as if flash-borrowed this block
        vm.prank(bot);
        uint256 id = sdao.propose(address(usdc), "");
        assertEq(gov.balanceOf(bot), 550_000 * E);
        assertEq(sdao.votingPower(bot, id), 0);
        assertEq(vdao.votingPower(bot, id), 550_000 * E);
        vm.prank(bot);
        vm.expectRevert("DAO: no voting power");
        sdao.vote(id);
    }
}
