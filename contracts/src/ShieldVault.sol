// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title ShieldVault — a small public-testnet vault with observable, enforced defenses.
/// @notice Educational contract for test ETH. Does not protect unrelated swaps or protocols.
contract ShieldVault {
    address public owner;
    bool public paused;
    uint256 private entered = 1;
    uint256 public constant COOLDOWN = 60;
    uint256 public constant WITHDRAWAL_LIMIT = 0.005 ether;
    uint256 public constant WINDOW = 1 hours;
    mapping(address => uint256) public balances;
    mapping(address => uint256) public unlockAt;
    mapping(address => uint256) public windowStart;
    mapping(address => uint256) public withdrawnInWindow;

    event Deposited(address indexed user, uint256 amount, uint256 unlockAt);
    event Withdrawn(address indexed user, uint256 amount);
    event PauseChanged(bool paused, address indexed by);

    constructor() { owner = msg.sender; }

    modifier nonReentrant() {
        require(entered == 1, "Vault: reentrant call");
        entered = 2;
        _;
        entered = 1;
    }

    function deposit() external payable nonReentrant {
        require(!paused, "Vault: paused");
        require(msg.value > 0, "Vault: zero deposit");
        balances[msg.sender] += msg.value;
        unlockAt[msg.sender] = block.timestamp + COOLDOWN;
        emit Deposited(msg.sender, msg.value, unlockAt[msg.sender]);
    }

    function withdraw(uint256 amount) external nonReentrant {
        require(!paused, "Vault: paused");
        require(amount > 0 && amount <= balances[msg.sender], "Vault: insufficient balance");
        require(block.timestamp >= unlockAt[msg.sender], "Vault: cooldown active");
        if (block.timestamp >= windowStart[msg.sender] + WINDOW) {
            windowStart[msg.sender] = block.timestamp;
            withdrawnInWindow[msg.sender] = 0;
        }
        require(withdrawnInWindow[msg.sender] + amount <= WITHDRAWAL_LIMIT, "Vault: hourly limit");
        // Effects precede the external call; a failed transfer rolls back all accounting.
        withdrawnInWindow[msg.sender] += amount;
        balances[msg.sender] -= amount;
        (bool ok,) = payable(msg.sender).call{value: amount}("");
        require(ok, "Vault: transfer failed");
        emit Withdrawn(msg.sender, amount);
    }

    function setPaused(bool value) external {
        require(msg.sender == owner, "Vault: owner only");
        paused = value;
        emit PauseChanged(value, msg.sender);
    }
}
