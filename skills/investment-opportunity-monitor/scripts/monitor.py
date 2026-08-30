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
import urllib.parse
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
CONFIG_DIR = SKILL_ROOT / "config"

sys.path.insert(0, str(SCRIPT_DIR))

from fetch import fetch_records  # noqa: E402
from net_client import get_rate_limit_status  # noqa: E402
from score import compute_scores  # noqa: E402
from storage import save_snapshot, load_latest_per_id  # noqa: E402


def _api_key_hosts(config: dict) -> list[str]:
    """Hosts que exigem chave de API nesta config (ex: bolsai), pra checar
    cota restante depois da busca. Cobre tanto 'source' quanto
    'fundamentals_source' (acoes usa os dois -- Yahoo sem chave + bolsai
    com chave)."""
    hosts = []
    for key in ("source", "fundamentals_source"):
        src = config.get(key)
        if src and src.get("requires_api_key"):
            host = urllib.parse.urlparse(src.get("endpoint", "")).netloc
            if host and host not in hosts:
                hosts.append(host)
    return hosts


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


def _rank_value(record: dict, field: str):
    v = record.get(field)
    return v if isinstance(v, (int, float)) else -1


def _format_cell(value, col: dict) -> str:
    width = col.get("width", 10)
    if col.get("type") == "text":
        return f"{str(value if value is not None else ''):<{width}}"[:width]
    if not isinstance(value, (int, float)):
        return f"{'N/D':>{width}}"
    decimals = col.get("decimals", 2)
    if col.get("thousands"):
        return f"{value:>{width},.{decimals}f}"
    return f"{value:>{width}.{decimals}f}"


def print_table(records: list[dict], config: dict, top: int, rank_by: str) -> None:
    fields = config["record_fields"]
    name_field = fields.get("name_field") or fields.get("id_field")
    label_field = fields.get("protocol_field") or fields.get("type_field")
    columns = fields.get("table_columns", [])
    has_opportunity = "opportunity_score" in config

    ranked = sorted(records, key=lambda r: _rank_value(r, rank_by), reverse=True)[:top]

    print(f"\n{config['display_name']} — top {len(ranked)} por {rank_by} (dados reais, {config['source']['endpoint']})\n")
    opp_header = f"{'oport.':>6}  " if has_opportunity else ""
    col_headers = "  ".join(f"{c['label']:>{c.get('width', 10)}}" for c in columns)
    label_header = f"{'categoria':<18}  " if label_field else ""
    header = f"{'risco':>6}  {opp_header}{col_headers}  {label_header}ativo"
    print(header)
    print("-" * len(header))
    for r in ranked:
        score = r["risk_score"]
        score_str = f"{score:>6.1f}" if isinstance(score, (int, float)) else f"{'N/D':>6}"
        opp_str = ""
        if has_opportunity:
            opp = r.get("opportunity_score")
            opp_str = (f"{opp:>6.1f}  " if isinstance(opp, (int, float)) else f"{'N/D':>6}  ")
        cells = "  ".join(_format_cell(r.get(c["field"]), c) for c in columns)
        label_str = f"{str(r.get(label_field, ''))[:18]:<18}  " if label_field else ""
        name = str(r.get(name_field) or r.get(fields.get("id_field"), ""))
        print(f"{score_str}  {opp_str}{cells}  {label_str}{name}")
        if r.get("_fetch_error"):
            print(f"        (busca falhou: {r['_fetch_error']})")
        missing = r.get("_risk_score_missing_components") or []
        if missing:
            print(f"        (risco calculado sem: {', '.join(missing)})")
        age = r.get("_snapshot_age_days")
        if age is not None:
            print(f"        (dado de {age:.1f} dia(s) atras -- visao combinada, nao e' busca de agora)")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--asset-class", required=True, help="ex: defi, stocks, fixed-income")
    parser.add_argument("--top", type=int, default=20, help="quantos resultados mostrar")
    parser.add_argument("--cache-ttl", type=int, default=300, help="segundos de cache do fetch (0 = sem cache)")
    parser.add_argument("--no-history", action="store_true", help="nao grava snapshot no historico")
    parser.add_argument("--json", action="store_true", help="imprime JSON em vez de tabela")
    parser.add_argument(
        "--rank-by",
        default="risk_score",
        help="campo pra ordenar/cortar o top N (ex: risk_score, opportunity_score se a classe tiver esse bloco)",
    )
    parser.add_argument(
        "--rolling-days",
        type=int,
        default=0,
        help="em vez de buscar ao vivo, mostra o registro mais recente de cada ativo dentro dos N dias "
        "passados, lido do historico -- pra reconstruir o quadro completo de uma watchlist que so' e' "
        "buscada em fatias por dia (ver watchlist.rotation). Nao busca nada ao vivo, nao grava snapshot novo.",
    )
    args = parser.parse_args()

    config = load_config(args.asset_class)

    if args.rolling_days:
        scored = load_latest_per_id(config, within_days=args.rolling_days)
        if not scored:
            raise SystemExit(
                f"Sem historico dentro dos ultimos {args.rolling_days} dias -- rode sem --rolling-days "
                f"pelo menos uma vez (ou espere os snapshots diarios acumularem) antes de usar essa flag."
            )
    else:
        raw_records = fetch_records(config, cache_ttl=args.cache_ttl)
        filtered = apply_filters(raw_records, config.get("filters", {}))
        scored = compute_scores(filtered, config["risk_score"], output_field="risk_score")
        if "opportunity_score" in config:
            scored = compute_scores(scored, config["opportunity_score"], output_field="opportunity_score")

        if not args.no_history:
            db_path = save_snapshot(scored, config)
            if not args.json:
                print(f"[historico salvo em {db_path}]", file=sys.stderr)

        for host in _api_key_hosts(config):
            status = get_rate_limit_status(host)
            if status:
                print(f"[{host}: {status['remaining']} requisicoes restantes hoje]", file=sys.stderr)

    if args.json:
        ranked = sorted(scored, key=lambda r: _rank_value(r, args.rank_by), reverse=True)[: args.top]
        print(json.dumps(ranked, ensure_ascii=False, indent=2, default=str))
    else:
        print_table(scored, config, args.top, args.rank_by)
        print(
            "\nLembrete: este score e um indicador comparativo calculado a partir de "
            "dados publicos, NAO e recomendacao de investimento. A decisao e sua.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
