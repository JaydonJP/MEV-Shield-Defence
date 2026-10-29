"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const WalletConnection = require("../mevshield/web/static/wallet-connection.js");
const account = "0x" + "12".repeat(20);
const sepolia = "0xaa36a7";

function wallet({ chain = sepolia, switchError = null, accountsError = null, finalAccounts = [account] } = {}) {
  const calls = [];
  const provider = { async request(request) {
    calls.push(request);
    if (request.method === "eth_requestAccounts") { if (accountsError) throw accountsError; return [account]; }
    if (request.method === "eth_accounts") return finalAccounts;
    if (request.method === "eth_chainId") return chain;
    if (request.method === "wallet_switchEthereumChain") { if (switchError) { const error = switchError; switchError = null; throw error; } chain = request.params[0].chainId; return null; }
    if (request.method === "wallet_addEthereumChain") return null;
    throw new Error("Unexpected wallet method " + request.method);
  } };
  return { provider, calls };
}

test("no provider is reported clearly, rather than pretending a wallet connected", async () => {
  assert.equal(WalletConnection.pickProvider(undefined), null);
  await assert.rejects(WalletConnection.connect(null), (error) => error.code === "NO_WALLET");
});

test("finds injected and announced wallets, and prefers MetaMask when available", () => {
  const browserWallet = wallet().provider;
  const metamask = { ...wallet().provider, isMetaMask: true };
  assert.equal(WalletConnection.pickProvider(browserWallet), browserWallet);
  assert.equal(WalletConnection.pickProvider({ providers: [browserWallet, metamask] }), metamask);
  assert.equal(WalletConnection.pickProvider(undefined, [metamask]), metamask);
});

test("connects on Sepolia without requiring a balance or sending a transaction", async () => {
  const { provider, calls } = wallet();
  assert.deepEqual(await WalletConnection.connect(provider), { account, chainId: sepolia });
  assert.deepEqual(calls.map((call) => call.method), ["eth_requestAccounts", "eth_chainId", "eth_chainId", "eth_accounts"]);
});

test("switches a connected wallet from another network to Sepolia", async () => {
  const { provider, calls } = wallet({ chain: "0x1" });
  assert.equal((await WalletConnection.connect(provider)).chainId, sepolia);
  assert.deepEqual(calls.find((call) => call.method === "wallet_switchEthereumChain").params, [{ chainId: sepolia }]);
});

test("adds Sepolia when the wallet has not configured it", async () => {
  const { provider, calls } = wallet({ chain: "0x1", switchError: { code: 4902 } });
  await WalletConnection.connect(provider);
  const configured = calls.find((call) => call.method === "wallet_addEthereumChain").params[0];
  assert.equal(configured.chainId, sepolia);
  assert.equal(calls.filter((call) => call.method === "wallet_switchEthereumChain").length, 2);
});

test("rejecting a switch never produces a connected state", async () => {
  const { provider } = wallet({ chain: "0x1", switchError: { code: 4001 } });
  await assert.rejects(WalletConnection.connect(provider), (error) => error.code === 4001);
});

test("rechecks account access after switching networks", async () => {
  const { provider } = wallet({ chain: "0x1", finalAccounts: [] });
  await assert.rejects(WalletConnection.connect(provider), /No wallet account/);
});

test("pending and declined extension requests have actionable messages", async () => {
  const { provider } = wallet({ accountsError: { code: -32002 } });
  await assert.rejects(WalletConnection.connect(provider), (error) => error.code === -32002);
  assert.match(WalletConnection.errorMessage({ code: -32002 }), /Open and unlock/);
  assert.match(WalletConnection.errorMessage({ code: 4001 }), /declined/);
});
