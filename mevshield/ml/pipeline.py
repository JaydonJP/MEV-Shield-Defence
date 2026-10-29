"""Dataset provenance checks, combination and reproducible model publication."""

import hashlib
import json
from pathlib import Path

import pandas as pd

from .. import config
from .model import MarketRiskPredictor, BUNDLED


def combine_sources():
    paths = [config.DATA_DIR / "mainnet" / "swaps.csv"]
    paths += sorted((config.DATA_DIR / "historical").glob("*/swaps.csv"))
    frames = []
    manifests = []
    for path in paths:
        if not path.exists():
            continue
        manifest = json.loads(path.with_name("manifest.json").read_text())
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if manifest["dataset_sha256"] != digest:
            raise ValueError("Dataset checksum changed: " + str(path))
        frames.append(pd.read_csv(path))
        manifests.append({**manifest, "path": str(path.relative_to(config.ROOT))})
    if not frames:
        bundled = config.ROOT / "datasets" / "ethereum-v2" / "swaps.csv"
        if bundled.exists():
            return bundled
        raise ValueError("Collect actual swap data before training")
    frame = pd.concat(frames, ignore_index=True)
    if frame.row_id.duplicated().any():
        raise ValueError("Overlapping source windows contain duplicate swaps")
    frame = frame.sort_values(["block_number", "row_id"])
    destination = config.DATA_DIR / "mainnet" / "training.csv"
    destination.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(destination, index=False, lineterminator="\n")
    destination.with_suffix(".manifest.json").write_text(
        json.dumps(
            {
                "sources": manifests,
                "rows": len(frame),
                "dataset_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return destination


def train_real(dataset=None, seed=29, lab_samples=6000, publish=False, log=print):
    path = Path(dataset) if dataset else combine_sources()
    model = MarketRiskPredictor()
    report = model.train(path, seed=seed, lab_samples=lab_samples, log=log)
    output = config.DATA_DIR / "mainnet" / "evaluation.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    model.save(config.DATA_DIR / "market-model.pkl")
    if publish:
        # A failed candidate remains inspectable; it never silently replaces the active model.
        if not report["promotion_passed"] or not report["lab_qualified"]:
            raise ValueError(
                "Model failed the predeclared promotion gates; evaluation saved, active model unchanged"
            )
        model.save(config.MODEL_FILE)
        model.save(BUNDLED)
        card = {
            **report,
            "artifact_sha256": hashlib.sha256(BUNDLED.read_bytes()).hexdigest(),
        }
        import sklearn

        card["sklearn_version"] = sklearn.__version__
        BUNDLED.with_suffix(".json").write_text(
            json.dumps(card, indent=2), encoding="utf-8"
        )
    return report
