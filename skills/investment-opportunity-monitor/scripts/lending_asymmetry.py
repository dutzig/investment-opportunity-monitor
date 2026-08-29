#!/usr/bin/env python3
"""Detector de assimetria de taxa de supply entre mercados de lending.

Diferente de monitor.py (que da' um score por pool), este script compara o
MESMO ativo entre VARIOS mercados de lending e mostra onde a taxa diverge.
Reusa o mesmo adapter de fetch do DeFi (scripts/fetch.py) -- nenhum dado
novo e' inventado, so' uma analise diferente sobre o mesmo tipo de dado.

Uso:
    python lending_asymmetry.py
    python lending_asymmetry.py --json
"""

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
CONFIG_PATH = SKILL_ROOT / "config" / "lending-rate-asymmetry.json"

sys.path.insert(0, str(SCRIPT_DIR))

from fetch import fetch_defillama_yields  # noqa: E402


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def filter_pools(pools: list[dict], config: dict) -> list[dict]:
    known = set(config["known_lending_protocols"])
    filters = config["filters"]
    out = []
    for p in pools:
        if p.get("project") not in known:
            continue
        if p.get("exposure") != filters.get("exposure", "single"):
            continue
        if filters.get("exclude_outlier") and p.get("outlier"):
            continue
        tvl = p.get("tvlUsd")
        if tvl is None or tvl < filters["min_tvl_usd"]:
            continue
        apy = p.get("apy")
        if apy is None or apy < filters["min_apy"]:
            continue
        out.append(p)
    return out


def group_and_analyze(pools: list[dict], config: dict) -> list[dict]:
    grouping = config["grouping"]
    field = grouping["group_by_field"]

    groups: dict[str, list[dict]] = {}
    for p in pools:
        key = p.get(field)
        groups.setdefault(key, []).append(p)

    results = []
    for symbol, venues in groups.items():
        if len(venues) < grouping["min_venues"]:
            continue
        venues_sorted = sorted(venues, key=lambda v: v["apy"], reverse=True)
        spread = venues_sorted[0]["apy"] - venues_sorted[-1]["apy"]
        if spread < grouping["min_spread_pp"]:
            continue

        chains = {v["chain"] for v in venues_sorted}
        same_chain_pairs = []
        for chain in chains:
            same_chain_venues = sorted(
                [v for v in venues_sorted if v["chain"] == chain], key=lambda v: v["apy"], reverse=True
            )
            if len(same_chain_venues) >= 2:
                pair_spread = same_chain_venues[0]["apy"] - same_chain_venues[-1]["apy"]
                if pair_spread >= grouping["min_spread_pp"]:
                    same_chain_pairs.append(
                        {
                            "chain": chain,
                            "spread_pp": round(pair_spread, 2),
                            "best": _venue_summary(same_chain_venues[0]),
                            "worst": _venue_summary(same_chain_venues[-1]),
                        }
                    )

        results.append(
            {
                "symbol": symbol,
                "spread_pp_overall": round(spread, 2),
                "venue_count": len(venues_sorted),
                "same_chain_opportunities": same_chain_pairs,
                "all_venues": [_venue_summary(v) for v in venues_sorted],
            }
        )

    results.sort(key=lambda r: r["spread_pp_overall"], reverse=True)
    return results


def _venue_summary(pool: dict) -> dict:
    return {
        "project": pool.get("project"),
        "chain": pool.get("chain"),
        "apy": round(pool.get("apy", 0), 2),
        "tvlUsd": pool.get("tvlUsd"),
        "pool_id": pool.get("pool"),
    }


def print_report(results: list[dict], config: dict) -> None:
    print(f"\n{config['display_name']}\n")
    print(config["methodology"])
    print()

    same_chain_total = sum(len(r["same_chain_opportunities"]) for r in results)
    print(f"{len(results)} ativos com spread >= {config['grouping']['min_spread_pp']}pp entre "
          f"{config['grouping']['min_venues']}+ mercados conhecidos "
          f"({same_chain_total} oportunidades na MESMA chain -- risco so' de contrato).\n")

    for r in results[:15]:
        print(f"=== {r['symbol']} — spread total {r['spread_pp_overall']}pp entre {r['venue_count']} mercados ===")
        if r["same_chain_opportunities"]:
            for opp in r["same_chain_opportunities"]:
                b, w = opp["best"], opp["worst"]
                print(
                    f"  [MESMA CHAIN: {opp['chain']}] spread {opp['spread_pp']}pp -- "
                    f"{b['project']} {b['apy']}% (tvl ${b['tvlUsd']:,.0f}) vs "
                    f"{w['project']} {w['apy']}% (tvl ${w['tvlUsd']:,.0f})"
                )
        else:
            print("  (nenhum par na mesma chain acima do spread minimo -- so' spread cross-chain, com risco de ponte)")
        top3 = r["all_venues"][:3]
        print("  top venues: " + ", ".join(f"{v['project']}/{v['chain']} {v['apy']}%" for v in top3))
        print()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cache-ttl", type=int, default=300)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    config = load_config()
    pools = fetch_defillama_yields(config, cache_ttl=args.cache_ttl)
    filtered = filter_pools(pools, config)
    results = group_and_analyze(filtered, config)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
    else:
        print_report(results, config)
        print(
            "Lembrete: isso mostra ONDE a taxa diverge e por que, nao e recomendacao. "
            "Spread cross-chain soma risco de ponte/tempo/custo de transferencia que este "
            "numero NAO desconta -- avalie manualmente antes de mover capital.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
