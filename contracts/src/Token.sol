// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title Token — minimal mintable ERC20 used for every asset on the devnet.
/// @dev Includes per-block balance checkpoints so governance contracts can read
///      historical voting power (`getPastBalance`). This is what defeats
///      flash-loan governance attacks (T4): borrowed tokens have no past balance.
contract Token {
    string public name;
    string public symbol;
    uint8 public constant decimals = 18;
    uint256 public totalSupply;
    address public owner;

    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    struct Checkpoint {
        uint64 fromBlock;
        uint192 balance;
    }

    mapping(address => Checkpoint[]) private _checkpoints;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    constructor(string memory name_, string memory symbol_) {
        name = name_;
        symbol = symbol_;
        owner = msg.sender;
    }

    function mint(address to, uint256 amount) external {
        require(msg.sender == owner, "Token: only owner");
        totalSupply += amount;
        balanceOf[to] += amount;
        _writeCheckpoint(to);
        emit Transfer(address(0), to, amount);
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        emit Approval(msg.sender, spender, amount);
        return true;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        _transfer(msg.sender, to, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        uint256 allowed = allowance[from][msg.sender];
        if (allowed != type(uint256).max) {
            require(allowed >= amount, "Token: allowance");
            allowance[from][msg.sender] = allowed - amount;
        }
        _transfer(from, to, amount);
        return true;
    }

    /// @notice Balance of `account` at the end of block `blockNumber` (must be in the past).
    function getPastBalance(address account, uint256 blockNumber) external view returns (uint256) {
        require(blockNumber < block.number, "Token: not yet determined");
        Checkpoint[] storage cps = _checkpoints[account];
        uint256 lo = 0;
        uint256 hi = cps.length;
        while (lo < hi) {
            uint256 mid = (lo + hi) / 2;
            if (cps[mid].fromBlock > blockNumber) hi = mid;
            else lo = mid + 1;
        }
        return lo == 0 ? 0 : cps[lo - 1].balance;
    }

    function _transfer(address from, address to, uint256 amount) internal {
        require(balanceOf[from] >= amount, "Token: balance");
        unchecked {
            balanceOf[from] -= amount;
        }
        balanceOf[to] += amount;
        _writeCheckpoint(from);
        _writeCheckpoint(to);
        emit Transfer(from, to, amount);
    }

    function _writeCheckpoint(address account) internal {
        Checkpoint[] storage cps = _checkpoints[account];
        uint256 n = cps.length;
        uint192 bal = uint192(balanceOf[account]);
        if (n > 0 && cps[n - 1].fromBlock == block.number) {
            cps[n - 1].balance = bal;
        } else {
            cps.push(Checkpoint(uint64(block.number), bal));
        }
    }
}
