// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Fixture} from "./Fixture.sol";
import {Token} from "../src/Token.sol";
import {SimpleAMM} from "../src/SimpleAMM.sol";
import {FlashLender, IFlashBorrower} from "../src/FlashLender.sol";

contract TokenTest is Fixture {
    function test_transferAndAllowance() public {
        vm.prank(alice);
        usdc.transfer(bot, 10 * E);
        assertEq(usdc.balanceOf(bot), 10 * E);

        vm.prank(alice);
        usdc.approve(address(this), 5 * E);
        usdc.transferFrom(alice, bot, 5 * E);
        assertEq(usdc.balanceOf(bot), 15 * E);
        assertEq(usdc.allowance(alice, address(this)), 0);

        vm.expectRevert("Token: allowance");
        usdc.transferFrom(alice, bot, 1);
    }

    function test_onlyOwnerMints() public {
        vm.prank(alice);
        vm.expectRevert("Token: only owner");
        usdc.mint(alice, 1);
    }

    function test_pastBalanceCheckpoints() public {
        uint256 b0 = block.number;
        vm.prank(alice);
        gov.transfer(bot, 100 * E);
        vm.roll(b0 + 1);
        assertEq(gov.getPastBalance(bot, b0 - 1), 0);
        assertEq(gov.getPastBalance(bot, b0), 100 * E);
        assertEq(gov.getPastBalance(alice, b0), 399_900 * E);
    }
}

contract SimpleAMMTest is Fixture {
    function test_swapMatchesConstantProductFormula() public {
        uint256 amountIn = 1_000 * E;
        uint256 expected = _out(amountIn, 200_000 * E, 100 * E, 5);
        vm.startPrank(alice);
        usdc.approve(address(ammA), amountIn);
        uint256 out = ammA.swap(address(usdc), amountIn, 0, alice);
        vm.stopPrank();
        assertEq(out, expected);
        (uint256 r0, uint256 r1,) = ammA.getReserves();
        assertEq(r0, 100 * E - out);
        assertEq(r1, 201_000 * E);
        // k never decreases (fees accrue to the pool)
        assertGe(r0 * r1, 100 * E * 200_000 * E);
    }

    function test_slippageGuard() public {
        uint256 amountIn = 1_000 * E;
        uint256 expected = ammA.quote(address(usdc), amountIn);
        vm.startPrank(alice);
        usdc.approve(address(ammA), amountIn);
        vm.expectRevert("AMM: slippage");
        ammA.swap(address(usdc), amountIn, expected + 1, alice);
        vm.stopPrank();
    }

    function test_spotPrice() public view {
        assertEq(ammA.spotPrice0(), 2_000 * E);
        assertEq(ammB.spotPrice0(), 2_060 * E);
    }

    function test_twapIgnoresSameBlockManipulation() public {
        uint256 before = oracle.twap();
        assertApproxEqRel(before, 2_000 * E, 1e15);
        // huge manipulation in the current block
        vm.startPrank(alice);
        usdc.approve(address(ammA), 500_000 * E);
        ammA.swap(address(usdc), 500_000 * E, 0, alice);
        vm.stopPrank();
        assertGt(ammA.spotPrice0(), 10_000 * E);
        assertEq(oracle.twap(), before, "TWAP must not move inside the manipulating block");
        // even after 12 more seconds it moves only a little (12s of a ~600s window)
        vm.warp(block.timestamp + 12);
        assertLt(oracle.twap(), (before * 125) / 100);
    }
}

contract GoodBorrower is IFlashBorrower {
    FlashLender lender;

    constructor(FlashLender l) {
        lender = l;
    }

    function run(address token, uint256 amount) external {
        lender.flashLoan(this, token, amount, "");
    }

    function onFlashLoan(address, address token, uint256 amount, uint256 fee, bytes calldata)
        external
        returns (bytes32)
    {
        Token(token).approve(address(lender), amount + fee);
        return lender.CALLBACK_SUCCESS();
    }
}

contract BadBorrower is IFlashBorrower {
    FlashLender lender;

    constructor(FlashLender l) {
        lender = l;
    }

    function run(address token, uint256 amount) external {
        lender.flashLoan(this, token, amount, "");
    }

    function onFlashLoan(address, address, uint256, uint256, bytes calldata) external view returns (bytes32) {
        return lender.CALLBACK_SUCCESS(); // never approves repayment
    }
}

contract FlashLenderTest is Fixture {
    function test_flashLoanChargesFee() public {
        GoodBorrower b = new GoodBorrower(lender);
        uint256 amount = 1_000_000 * E;
        uint256 fee = lender.flashFee(address(usdc), amount);
        assertEq(fee, 900 * E);
        usdc.mint(address(b), fee);
        uint256 before = usdc.balanceOf(address(lender));
        b.run(address(usdc), amount);
        assertEq(usdc.balanceOf(address(lender)), before + fee);
    }

    function test_flashLoanRevertsIfNotRepaid() public {
        BadBorrower b = new BadBorrower(lender);
        vm.expectRevert("Token: allowance");
        b.run(address(usdc), 1_000 * E);
    }
}

contract LendingTest is Fixture {
    function test_normalBorrowAt75PctLtv() public {
        vm.startPrank(alice);
        weth.approve(address(vuln), 10 * E);
        vuln.deposit(10 * E);
        assertEq(vuln.maxBorrow(alice), 15_000 * E); // 10 WETH * 2000 * 75 %
        vuln.borrow(15_000 * E);
        vm.expectRevert("Lending: undercollateralised");
        vuln.borrow(1);
        vm.stopPrank();
    }

    function test_safeLendingUsesTwap() public view {
        assertApproxEqRel(safe.collateralPrice(), 2_000 * E, 1e15);
        assertLt(safe.priceDeviationBps(), 10);
    }

    function test_guardianCanPause() public {
        vm.prank(alice);
        vm.expectRevert("Lending: not guardian");
        vuln.pause();

        vm.prank(guardian);
        vuln.pause();
        assertTrue(vuln.paused());

        vm.startPrank(alice);
        weth.approve(address(vuln), 1 * E);
        vm.expectRevert("Lending: paused");
        vuln.deposit(1 * E);
        vm.stopPrank();

        vuln.unpause(); // owner = this test contract
        assertFalse(vuln.paused());
    }
}
