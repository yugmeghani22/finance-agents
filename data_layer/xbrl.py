"""A cached filing's own XBRL facts, for agents to cross-check values against.

    from data_layer.xbrl import filing_facts
    facts = filing_facts(folder)   # [{"tag", "value_m", "period"}]

Only company-wide (dimensionless) USD facts at the filing's period of report
are kept: the balance-sheet date the agents report values as of. Cached as
xbrl_facts_all.json next to the filing's sections.
"""

import json
import warnings
from pathlib import Path

from data_layer.store import read_manifest

CACHE_FILE = "xbrl_facts_all.json"


def filing_facts(folder: Path) -> list[dict]:
    path = folder / CACHE_FILE
    if not path.exists():
        from edgar import get_by_accession_number  # noqa: PLC0415 - only on a cache miss

        from data_layer.fetch import _ensure_identity  # noqa: PLC0415 - SEC identity + rate limit
        _ensure_identity()
        manifest = read_manifest(folder)
        period = manifest["period_of_report"]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            raw = get_by_accession_number(manifest["accession_no"]).xbrl().facts.get_facts()
        seen, facts = set(), []
        for f in raw:
            dims = any(k.startswith("dim_") and v for k, v in f.items())
            if (dims or f.get("numeric_value") is None or f.get("unit_ref", "").lower() != "usd"
                    or f.get("period_key") != f"instant_{period}"):
                continue
            tag = f["concept"].split(":")[-1]
            key = (tag, f["numeric_value"])
            if key not in seen:
                seen.add(key)
                facts.append({"tag": tag, "value_m": f["numeric_value"] / 1e6, "period": period})
        path.write_text(json.dumps(facts, indent=1))
    return json.loads(path.read_text())
