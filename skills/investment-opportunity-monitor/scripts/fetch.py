"""Adapters de busca de dados, um por 'source.type' de config.

Cada adapter recebe o dict 'source' do config da classe de ativo e retorna
uma lista de dicts (records) com os dados crus da fonte, sem nenhum campo
inventado. Se um campo esperado nao vier na resposta da API, ele
simplesmente nao aparece no record -- quem consome (score.py) trata a
ausencia como "indisponivel", nunca como zero ou como um palpite.

Para adicionar uma nova classe de ativo com uma fonte nova, adicione uma
funcao aqui e registre em ADAPTERS. Veja docs/adding-asset-class.md.
"""

from net_client import fetch_json


def fetch_defillama_yields(source: dict, cache_ttl: int) -> list[dict]:
    payload = fetch_json(source["endpoint"], ttl_seconds=cache_ttl)
    if not isinstance(payload, dict) or "data" not in payload:
        raise RuntimeError("Resposta inesperada da API DefiLlama /pools (campo 'data' ausente)")
    return payload["data"]


def fetch_yahoo_finance_chart(source: dict, cache_ttl: int) -> list[dict]:
    raise NotImplementedError(
        "Adapter para acoes/ETFs (Yahoo Finance) ainda nao implementado. "
        "Endpoint ja validado (ver config/stocks.json), falta escrever o parser "
        "da resposta chart JSON. Veja docs/adding-asset-class.md."
    )


def fetch_tesouro_transparente_csv(source: dict, cache_ttl: int) -> list[dict]:
    raise NotImplementedError(
        "Adapter para renda fixa (Tesouro Transparente) ainda nao implementado. "
        "Endpoint CSV ja validado (ver config/fixed-income.json), falta escrever "
        "o parser do CSV (';' separado, decimal ',', encoding latin-1). "
        "Veja docs/adding-asset-class.md."
    )


ADAPTERS = {
    "defillama_yields": fetch_defillama_yields,
    "yahoo_finance_chart": fetch_yahoo_finance_chart,
    "tesouro_transparente_csv": fetch_tesouro_transparente_csv,
}


def fetch_records(config: dict, cache_ttl: int) -> list[dict]:
    source = config["source"]
    source_type = source["type"]
    if source_type not in ADAPTERS:
        raise ValueError(
            f"source.type '{source_type}' nao tem adapter registrado em fetch.py. "
            f"Tipos disponiveis: {list(ADAPTERS)}"
        )
    return ADAPTERS[source_type](source, cache_ttl)
