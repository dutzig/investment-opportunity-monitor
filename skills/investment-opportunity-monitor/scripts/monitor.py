#!/usr/bin/env python3
"""CLI generico do investment-opportunity-monitor.

Uso:
    python monitor.py --asset-class defi --top 15
    python monitor.py --asset-class defi --top 15 --json
    python monitor.py --asset-class defi --no-cache

Este script NAO conhece nenhuma classe de ativo especifica. Tudo o que
diferencia DeFi de acoes de renda fixa vive no arquivo config/<classe>.json
e no adapter correspondente em fetch.py. Ver docs/adding-asset-class.md
para adicionar uma classe nova sem tocar aqui.

Esta skill e somente-leitura: nunca compra, vende, conecta wallet ou
guarda credenciais. Ela so busca dados publicos e calcula um score de
risco comparativo, transparente e documentado no proprio config.
"""

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
CONFIG_DIR = SKILL_ROOT / "config"

sys.path.insert(0, str(SCRIPT_DIR))

from fetch import fetch_records  # noqa: E402
from score import compute_scores  # noqa: E402
from storage import save_snapshot  # noqa: E402


def load_config(asset_class: str) -> dict:
    path = CONFIG_DIR / f"{asset_class}.json"
    if not path.exists():
        available = sorted(p.stem for p in CONFIG_DIR.glob("*.json"))
        raise SystemExit(
            f"Config nao encontrado: {path}\nClasses disponiveis: {available}"
        )
    with open(path, "r", encoding="utf-8") as f:
        config = json.load(f)

    if config.get("status") == "TEMPLATE_NAO_IMPLEMENTADO":
        raise SystemExit(
            f"A classe de ativo '{asset_class}' ainda e um template, nao uma "
            f"implementacao completa (adapter de busca de dados nao existe "
            f"ainda). Detalhes: {config.get('status_notes')}\n"
            f"Ver docs/adding-asset-class.md para implementar."
        )
    return config


def apply_filters(records: list[dict], filters: dict) -> list[dict]:
    if not filters:
        return records

    out = records

    min_value_field = filters.get("min_value_field")
    if min_value_field:
        field, minimum = min_value_field["field"], min_value_field["min"]
        out = [r for r in out if (r.get(field) is not None and r.get(field) >= minimum)]

    range_field = filters.get("range_field")
    if range_field:
        field = range_field["field"]
        lo, hi = range_field.get("min"), range_field.get("max")
        def in_range(r):
            v = r.get(field)
            if v is None:
                return False
            if lo is not None and v < lo:
                return False
            if hi is not None and v > hi:
                return False
            return True
        out = [r for r in out if in_range(r)]

    for rule in filters.get("exclude_if", []):
        field, equals = rule["field"], rule["equals"]
        out = [r for r in out if r.get(field) != equals]

    return out


def print_table(records: list[dict], config: dict, top: int) -> None:
    fields = config["record_fields"]
    name_field = fields.get("name_field")
    protocol_field = fields.get("protocol_field") or fields.get("type_field")
    chain_field = fields.get("chain_field")
    yield_field = fields.get("yield_field")
    tvl_field = fields.get("tvl_field")

    ranked = sorted(
        records,
        key=lambda r: (r["risk_score"] if isinstance(r["risk_score"], (int, float)) else -1),
        reverse=True,
    )[:top]

    print(f"\n{config['display_name']} — top {len(ranked)} por risk_score (dados reais, {config['source']['endpoint']})\n")
    header = f"{'score':>6}  {'apy%':>7}  {'tvl_usd':>15}  {'chain':<10}  {'protocolo':<18}  ativo"
    print(header)
    print("-" * len(header))
    for r in ranked:
        score = r["risk_score"]
        score_str = f"{score:>6.1f}" if isinstance(score, (int, float)) else f"{'N/D':>6}"
        apy = r.get(yield_field)
        apy_str = f"{apy:>7.2f}" if isinstance(apy, (int, float)) else f"{'N/D':>7}"
        tvl = r.get(tvl_field)
        tvl_str = f"{tvl:>15,.0f}" if isinstance(tvl, (int, float)) else f"{'N/D':>15}"
        chain = str(r.get(chain_field, ""))[:10]
        protocol = str(r.get(protocol_field, ""))[:18]
        name = str(r.get(name_field, ""))
        print(f"{score_str}  {apy_str}  {tvl_str}  {chain:<10}  {protocol:<18}  {name}")
        if r["_missing_components"]:
            print(f"        (indisponivel: {', '.join(r['_missing_components'])})")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--asset-class", required=True, help="ex: defi, stocks, fixed-income")
    parser.add_argument("--top", type=int, default=20, help="quantos resultados mostrar")
    parser.add_argument("--cache-ttl", type=int, default=300, help="segundos de cache do fetch (0 = sem cache)")
    parser.add_argument("--no-history", action="store_true", help="nao grava snapshot no historico")
    parser.add_argument("--json", action="store_true", help="imprime JSON em vez de tabela")
    args = parser.parse_args()

    config = load_config(args.asset_class)

    raw_records = fetch_records(config, cache_ttl=args.cache_ttl)
    filtered = apply_filters(raw_records, config.get("filters", {}))
    scored = compute_scores(filtered, config["risk_score"])

    if not args.no_history:
        db_path = save_snapshot(scored, config)
        if not args.json:
            print(f"[historico salvo em {db_path}]", file=sys.stderr)

    if args.json:
        ranked = sorted(
            scored,
            key=lambda r: (r["risk_score"] if isinstance(r["risk_score"], (int, float)) else -1),
            reverse=True,
        )[: args.top]
        print(json.dumps(ranked, ensure_ascii=False, indent=2, default=str))
    else:
        print_table(scored, config, args.top)
        print(
            "\nLembrete: este score e um indicador comparativo calculado a partir de "
            "dados publicos, NAO e recomendacao de investimento. A decisao e sua.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
