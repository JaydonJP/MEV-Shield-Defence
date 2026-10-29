// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Token} from "./Token.sol";
import {SimpleAMM} from "./SimpleAMM.sol";

/// @title LendingBase — over-collateralised lending: deposit collateral token, borrow debt token.
/// @notice Subclasses decide how collateral is priced. A guardian may `pause()` the market
///         (circuit breaker used by MEV Shield's Guardian/Responder).
abstract contract LendingBase {
    Token public immutable collateralToken; // e.g. WETH  (AMM token0)
    Token public immutable debtToken; //       e.g. USDC  (AMM token1)
    SimpleAMM public immutable amm;
    address public immutable owner;
    address public guardian;
    bool public paused;

    uint256 public constant LTV_BPS = 7_500; // borrow up to 75 % of collateral value

    mapping(address => uint256) public collateralOf;
    mapping(address => uint256) public debtOf;
    mapping(address => uint256) public lastDepositBlock;
    uint256 public totalCollateral;
    uint256 public totalDebt;

    event Deposit(address indexed user, uint256 amount);
    event Withdraw(address indexed user, uint256 amount);
    event Borrow(address indexed user, uint256 amount, uint256 price);
    event Repay(address indexed user, uint256 amount);
    event Paused(address indexed by);
    event Unpaused(address indexed by);
    event GuardianSet(address indexed guardian);

    modifier whenNotPaused() {
        require(!paused, "Lending: paused");
        _;
    }

    constructor(Token collateral_, Token debt_, SimpleAMM amm_, address guardian_) {
        require(address(amm_.token0()) == address(collateral_), "Lending: token0 must be collateral");
        require(address(amm_.token1()) == address(debt_), "Lending: token1 must be debt");
        collateralToken = collateral_;
        debtToken = debt_;
        amm = amm_;
        owner = msg.sender;
        guardian = guardian_;
        emit GuardianSet(guardian_);
    }

    /// @notice Price of 1 collateral token in debt tokens, 1e18-scaled.
    function collateralPrice() public view virtual returns (uint256);

    /// @dev Hook for extra borrow-time checks (SafeLending's deviation guard).
    function _beforeBorrow(uint256 amount) internal virtual {}

    /// @dev Extension hook; accounting is reverted along with the borrow if the transfer fails.
    function _afterBorrow(uint256) internal virtual {}

    // ------------------------------------------------------------------ views

    function collateralValue(address user) public view returns (uint256) {
        return (collateralOf[user] * collateralPrice()) / 1e18;
    }

    function maxBorrow(address user) public view returns (uint256) {
        uint256 cap = (collateralValue(user) * LTV_BPS) / 10_000;
        return cap > debtOf[user] ? cap - debtOf[user] : 0;
    }

    function liquidity() external view returns (uint256) {
        return debtToken.balanceOf(address(this));
    }

    // -------------------------------------------------------------- mutations

    function deposit(uint256 amount) external whenNotPaused {
        require(amount > 0, "Lending: zero deposit");
        require(collateralToken.transferFrom(msg.sender, address(this), amount), "Lending: transfer");
        collateralOf[msg.sender] += amount;
        lastDepositBlock[msg.sender] = block.number;
        totalCollateral += amount;
        emit Deposit(msg.sender, amount);
    }

    function borrow(uint256 amount) external whenNotPaused {
        _beforeBorrow(amount);
        uint256 price = collateralPrice();
        uint256 cap = (((collateralOf[msg.sender] * price) / 1e18) * LTV_BPS) / 10_000;
        require(debtOf[msg.sender] + amount <= cap, "Lending: undercollateralised");
        debtOf[msg.sender] += amount;
        totalDebt += amount;
        _afterBorrow(amount);
        require(debtToken.transfer(msg.sender, amount), "Lending: transfer");
        emit Borrow(msg.sender, amount, price);
    }

    function repay(uint256 amount) external {
        if (amount > debtOf[msg.sender]) amount = debtOf[msg.sender];
        require(debtToken.transferFrom(msg.sender, address(this), amount), "Lending: transfer");
        debtOf[msg.sender] -= amount;
        totalDebt -= amount;
        emit Repay(msg.sender, amount);
    }

    function withdraw(uint256 amount) external whenNotPaused {
        require(collateralOf[msg.sender] >= amount, "Lending: collateral");
        collateralOf[msg.sender] -= amount;
        totalCollateral -= amount;
        uint256 cap = (((collateralOf[msg.sender] * collateralPrice()) / 1e18) * LTV_BPS) / 10_000;
        require(debtOf[msg.sender] <= cap, "Lending: undercollateralised");
        require(collateralToken.transfer(msg.sender, amount), "Lending: transfer");
        emit Withdraw(msg.sender, amount);
    }

    // ---------------------------------------------------------- circuit breaker

    function pause() external {
        require(msg.sender == guardian || msg.sender == owner, "Lending: not guardian");
        paused = true;
        emit Paused(msg.sender);
    }

    function unpause() external {
        require(msg.sender == owner, "Lending: not owner");
        paused = false;
        emit Unpaused(msg.sender);
    }

    function setGuardian(address guardian_) external {
        require(msg.sender == owner, "Lending: not owner");
        guardian = guardian_;
        emit GuardianSet(guardian_);
    }
}
