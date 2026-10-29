"use strict";

// Small EIP-1193 adapter, independent of the UI so connection and network switching can be tested.
const WalletConnection = (() => {
  const CHAIN_ID = "0xaa36a7";
  function pickProvider(ethereum, announced = []) {
    const providers = [...announced, ...(ethereum?.providers || []), ethereum].filter((provider) => typeof provider?.request === "function");
    return providers.find((provider) => provider.isMetaMask) || providers[0] || null;
  }
  async function connect(provider) {
    if (!provider) throw Object.assign(new Error("No Ethereum wallet was detected. Set up a browser wallet to connect."), { code: "NO_WALLET" });
    let accounts = await provider.request({ method: "eth_requestAccounts" });
    if (!accounts?.length) throw new Error("No wallet account was selected. Unlock your wallet and approve account access.");
    let chainId = String(await provider.request({ method: "eth_chainId" })).toLowerCase();
    if (chainId !== CHAIN_ID) {
      try { await provider.request({ method: "wallet_switchEthereumChain", params: [{ chainId: CHAIN_ID }] }); }
      catch (error) {
        if (error.code !== 4902 && error.data?.originalError?.code !== 4902) throw error;
        await provider.request({ method: "wallet_addEthereumChain", params: [{ chainId: CHAIN_ID, chainName: "Ethereum Sepolia", nativeCurrency: { name: "Sepolia Ether", symbol: "ETH", decimals: 18 }, rpcUrls: ["https://ethereum-sepolia-rpc.publicnode.com"], blockExplorerUrls: ["https://sepolia.etherscan.io"] }] });
        await provider.request({ method: "wallet_switchEthereumChain", params: [{ chainId: CHAIN_ID }] });
      }
    }
    chainId = String(await provider.request({ method: "eth_chainId" })).toLowerCase();
    if (chainId !== CHAIN_ID) throw new Error("Select Ethereum Sepolia in your wallet, then try connecting again.");
    accounts = await provider.request({ method: "eth_accounts" });
    if (!/^0x[0-9a-f]{40}$/i.test(accounts?.[0] || "")) throw new Error("No wallet account is connected. Approve account access in your wallet.");
    return { account: accounts[0], chainId };
  }
  function errorMessage(error) {
    if (error.code === 4001) return "The wallet request was declined. Click Connect wallet again when you’re ready.";
    if (error.code === -32002) return "A wallet request is already waiting. Open and unlock your wallet extension to approve it.";
    if (error.code === 4100) return "This website needs account access. Open your wallet and approve the connection.";
    if (error.code === 4900) return "Your wallet is disconnected. Reopen the extension, unlock it, and try again.";
    return error.message || "Unable to connect. Open your wallet extension and try again.";
  }
  return { pickProvider, connect, errorMessage };
})();

if (typeof module !== "undefined" && module.exports) module.exports = WalletConnection;
