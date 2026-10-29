from types import SimpleNamespace

from hexbytes import HexBytes

from mevshield.chain.shadow import ShadowFork
from mevshield.chain import Chain
from mevshield.verification import contract_fingerprint


def test_same_height_replacement_block_invalidates_shadow_cache():
    hashes = iter([HexBytes("0x" + "11" * 32), HexBytes("0x" + "11" * 32), HexBytes("0x" + "22" * 32)])
    main = SimpleNamespace(block_number=10, deployments={"contracts": {}},
        w3=SimpleNamespace(eth=SimpleNamespace(get_block=lambda n: {"hash": next(hashes)})))
    resets = []
    shadow = ShadowFork(main)
    shadow.chain = SimpleNamespace(block_number=10, rpc_call=lambda *args: resets.append(args))
    for _ in range(3): shadow.sync()
    assert len(resets) == 2


def test_verification_fingerprint_changes_with_sources_or_config(tmp_path, monkeypatch):
    from mevshield import config
    monkeypatch.setattr(config, "CONTRACTS_DIR", tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "foundry.toml").write_text("[profile.default]")
    path = tmp_path / "src" / "Example.sol"
    path.write_text("contract Example {}")
    original = contract_fingerprint()
    path.write_text("contract Example { bool paused; }")
    assert contract_fingerprint() != original


def test_deadline_includes_pending_devnet_time_offset(monkeypatch):
    import mevshield.chain.connection as connection
    chain = Chain.__new__(Chain)
    chain.w3 = SimpleNamespace(eth=SimpleNamespace(
        get_block=lambda tag: {"timestamp": 100 if tag == "latest" else 1200}))
    monkeypatch.setattr(connection.time, "time", lambda: 200)
    assert chain.deadline() == 1500
