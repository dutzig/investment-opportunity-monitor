"""Adapters de busca de dados, um por 'source.type' de config.

Cada adapter recebe o config inteiro da classe de ativo (nao so o bloco
'source') e retorna uma lista de dicts (records) com os dados crus da
fonte, sem nenhum campo inventado. Se um campo esperado nao vier na
resposta da API, ele simplesmente nao aparece no record -- quem consome
(score.py) trata a ausencia como "indisponivel", nunca como zero ou como
um palpite. Campos derivados (ex: prazo em anos, categoria de indexador)
sao calculados a partir de dado real que a API mandou, nunca inventados.

Para adicionar uma nova classe de ativo com uma fonte nova, adicione uma
funcao aqui e registre em ADAPTERS. Veja docs/adding-asset-class.md.
"""

import csv
import io
import json
import math
import os
import statistics
import time
from datetime import date, datetime
from pathlib import Path

from net_client import fetch_json, fetch_text

SKILL_ROOT = Path(__file__).resolve().parent.parent


def _resolve_watchlist_tickers(watchlist: dict) -> list[str]:
    """Le a lista real de tickers de um arquivo local fora do git, se
    'local_override_path' estiver configurado e o arquivo existir --
    permite manter uma watchlist curada de terceiros (ex: relatorio pago
    e licenciado) ou a composicao real da carteira do usuario fora do
    repo publico. O config versionado no git sempre traz so uma lista
    generica de exemplo em 'tickers'; a lista de verdade fica em
    data/*.json (dir ja coberto pelo .gitignore)."""
    override_path = watchlist.get("local_override_path")
    if override_path:
        full_path = SKILL_ROOT / override_path
        if full_path.exists():
            with open(full_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            tickers = data.get("tickers")
            if tickers:
                return tickers
    return watchlist.get("tickers", [])


def fetch_defillama_yields(config: dict, cache_ttl: int) -> list[dict]:
    endpoint = config["source"]["endpoint"]
    payload = fetch_json(endpoint, ttl_seconds=cache_ttl)
    if not isinstance(payload, dict) or "data" not in payload:
        raise RuntimeError("Resposta inesperada da API DefiLlama /pools (campo 'data' ausente)")
    return payload["data"]


_INDEXADOR_MAP = {
    "Tesouro Selic": "Selic",
    "Tesouro Prefixado": "Prefixado",
    "Tesouro Prefixado com Juros Semestrais": "Prefixado",
    "Tesouro IPCA+": "IPCA+",
    "Tesouro IPCA+ com Juros Semestrais": "IPCA+",
    "Tesouro IGPM+ com Juros Semestrais": "IGPM+",
    "Tesouro Educa+": "IPCA+",
    "Tesouro Renda+ Aposentadoria Extra": "IPCA+",
}


def _parse_br_decimal(value: str):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return float(value.replace(".", "").replace(",", "."))
    except ValueError:
        return None


def _parse_br_date(value: str):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%d/%m/%Y").date()
    except ValueError:
        return None


def fetch_tesouro_transparente_csv(config: dict, cache_ttl: int) -> list[dict]:
    """Baixa o CSV publico do Tesouro Transparente (historico completo, ~14MB,
    todos os titulos desde o inicio do Tesouro Direto) e devolve so as linhas
    da 'Data Base' mais recente -- ou seja, a foto de hoje. O historico ao
    longo do tempo ja e' responsabilidade do proprio storage.py (snapshots
    salvos localmente), nao precisa vir do CSV inteiro a cada consulta.
    """
    endpoint = config["source"]["endpoint"]
    raw = fetch_text(endpoint, ttl_seconds=cache_ttl, timeout=60.0)

    reader = csv.DictReader(io.StringIO(raw), delimiter=";")
    rows = list(reader)
    if not rows:
        raise RuntimeError("CSV do Tesouro Transparente veio vazio")

    parsed_dates = {}
    for row in rows:
        d = _parse_br_date(row.get("Data Base"))
        if d is not None:
            parsed_dates[row.get("Data Base")] = d
    if not parsed_dates:
        raise RuntimeError("Nao foi possivel interpretar nenhuma 'Data Base' do CSV")
    latest_str = max(parsed_dates, key=lambda k: parsed_dates[k])
    latest_date = parsed_dates[latest_str]

    today = date.today()
    records = []
    for row in rows:
        if row.get("Data Base") != latest_str:
            continue
        tipo = row.get("Tipo Titulo")
        vencimento = _parse_br_date(row.get("Data Vencimento"))
        prazo_anos = (vencimento - today).days / 365.25 if vencimento else None

        record = {
            "Tipo Titulo": tipo,
            "Data Vencimento": row.get("Data Vencimento"),
            "Data Base": row.get("Data Base"),
            "Taxa Compra Manha": _parse_br_decimal(row.get("Taxa Compra Manha")),
            "Taxa Venda Manha": _parse_br_decimal(row.get("Taxa Venda Manha")),
            "PU Compra Manha": _parse_br_decimal(row.get("PU Compra Manha")),
            "PU Venda Manha": _parse_br_decimal(row.get("PU Venda Manha")),
            "PU Base Manha": _parse_br_decimal(row.get("PU Base Manha")),
            "titulo_id": f"{tipo} {row.get('Data Vencimento')}",
            "prazo_anos": prazo_anos,
            "indexador": _INDEXADOR_MAP.get(tipo, "Outro"),
        }
        records.append(record)

    return records


def _daily_returns(closes: list):
    returns = []
    for prev, curr in zip(closes, closes[1:]):
        if prev is None or curr is None or prev == 0:
            continue
        returns.append((curr - prev) / prev)
    return returns


def _select_today_batch(tickers: list[str], rotation: dict) -> list[str]:
    """Divide a watchlist em N fatias e devolve so a fatia do dia da semana
    atual. Existe pra cobrir watchlists grandes demais pra rodar inteiras
    todo dia (rate limit + tempo de execucao) -- rodando 1x/dia, cada
    ticker acaba atualizado 1x a cada N dias. Pra ver o quadro combinando
    todos os dias da semana (nao so a fatia de hoje), monitor.py tem a
    flag --rolling-days, que le do historico em vez de buscar ao vivo."""
    if not rotation or not rotation.get("enabled"):
        return tickers
    days_in_cycle = rotation.get("days_in_cycle", 7)
    weekday = date.today().weekday()  # 0=segunda ... 6=domingo
    batch_size = math.ceil(len(tickers) / days_in_cycle)
    start = (weekday % days_in_cycle) * batch_size
    return tickers[start : start + batch_size]


def _fetch_bolsai_fundamentals_one(ticker_plain: str, endpoint_template: str, api_key: str, cache_ttl: int):
    """Busca fundamentos de UM ticker na bolsai. Devolve (dict, erro) --
    nunca levanta excecao, pra nao derrubar o lote inteiro por causa de
    um ticker sem cobertura fundamentalista (ex: BDR, units novas)."""
    url = endpoint_template.format(ticker=ticker_plain)
    try:
        data = fetch_json(url, ttl_seconds=cache_ttl, headers={"X-API-Key": api_key}, timeout=20.0)
    except RuntimeError as exc:
        return None, str(exc)
    if data.get("detail") or data.get("error"):
        return None, data.get("detail") or data.get("message", "erro desconhecido")
    return data, None


def fetch_yahoo_finance_chart(config: dict, cache_ttl: int) -> list[dict]:
    source = config["source"]
    endpoint_template = source["endpoint"]
    watchlist = config.get("watchlist", {})
    all_tickers = watchlist.get("tickers", [])
    if not all_tickers:
        raise RuntimeError(
            "watchlist.tickers esta vazio em config/stocks.json -- preencha com os "
            "tickers B3 que quer monitorar (ex: 'PETR4.SA') antes de rodar."
        )
    tickers = _select_today_batch(all_tickers, watchlist.get("rotation", {}))
    benchmark_ticker = watchlist.get("benchmark_ticker", "^BVSP")
    history_range = watchlist.get("history_range", "3mo")
    request_interval = watchlist.get("request_interval_seconds", 1.5)

    fundamentals_source = config.get("fundamentals_source")
    fundamentals_api_key = None
    if fundamentals_source:
        env_var = fundamentals_source.get("api_key_env_var", "BOLSAI_API_KEY")
        fundamentals_api_key = os.environ.get(env_var)
        if not fundamentals_api_key:
            raise RuntimeError(
                f"config tem 'fundamentals_source' mas a variavel de ambiente {env_var} "
                f"nao esta definida. Pegue uma chave gratuita em usebolsai.com e exporte "
                f"antes de rodar: export {env_var}=sua_chave_aqui"
            )

    def fetch_chart(ticker: str):
        url = endpoint_template.format(ticker=ticker) + f"?range={history_range}&interval=1d"
        try:
            payload = fetch_json(url, ttl_seconds=cache_ttl, timeout=30.0)
        except RuntimeError as exc:
            # Fonte fora do ar ou rate-limited pra este ticker: marca como
            # indisponivel e segue pros outros, em vez de derrubar a
            # consulta inteira por causa de um ticker.
            return None, str(exc)
        result = payload.get("chart", {}).get("result")
        if not result:
            error = payload.get("chart", {}).get("error")
            return None, error
        return result[0], None

    benchmark_data, benchmark_error = fetch_chart(benchmark_ticker)
    benchmark_returns_by_ts = {}
    if benchmark_data:
        ts = benchmark_data["timestamp"]
        closes = benchmark_data["indicators"]["quote"][0]["close"]
        for t, ret in zip(ts[1:], _daily_returns(closes)):
            benchmark_returns_by_ts[t] = ret

    records = []
    for i, ticker in enumerate(tickers):
        if i > 0 and request_interval:
            time.sleep(request_interval)
        chart, error = fetch_chart(ticker)
        if chart is None:
            records.append({"ticker": ticker, "_fetch_error": str(error) if error else "sem dado retornado"})
            continue

        meta = chart.get("meta", {})
        ts = chart.get("timestamp", [])
        quote = chart.get("indicators", {}).get("quote", [{}])[0]
        closes = quote.get("close", [])
        volumes = quote.get("volume", [])

        returns = _daily_returns(closes)
        volatility_30d = None
        if len(returns) >= 5:
            window = returns[-30:]
            if len(window) >= 2:
                volatility_30d = statistics.stdev(window) * (252**0.5) * 100

        beta = None
        if benchmark_returns_by_ts and len(returns) >= 5:
            paired_stock, paired_bench = [], []
            for t, ret in zip(ts[1:], returns):
                bench_ret = benchmark_returns_by_ts.get(t)
                if bench_ret is not None:
                    paired_stock.append(ret)
                    paired_bench.append(bench_ret)
            if len(paired_bench) >= 5 and statistics.pvariance(paired_bench) > 0:
                mean_s, mean_b = statistics.fmean(paired_stock), statistics.fmean(paired_bench)
                cov = sum((s - mean_s) * (b - mean_b) for s, b in zip(paired_stock, paired_bench)) / len(paired_bench)
                beta = cov / statistics.pvariance(paired_bench)

        valid_pairs = [(c, v) for c, v in zip(closes[-30:], volumes[-30:]) if c is not None and v is not None]
        avg_volume_brl = statistics.fmean(c * v for c, v in valid_pairs) if valid_pairs else None

        record = {
            "ticker": ticker,
            "shortName": meta.get("shortName"),
            "regularMarketPrice": meta.get("regularMarketPrice"),
            "regularMarketVolume": meta.get("regularMarketVolume"),
            "currency": meta.get("currency"),
            "fiftyTwoWeekHigh": meta.get("fiftyTwoWeekHigh"),
            "fiftyTwoWeekLow": meta.get("fiftyTwoWeekLow"),
            "volatility_30d": volatility_30d,
            "beta": beta,
            "avg_volume_brl": avg_volume_brl,
        }

        if fundamentals_source:
            if i > 0 and request_interval:
                time.sleep(request_interval)
            ticker_plain = ticker.split(".")[0]  # bolsai usa 'PETR4', nao 'PETR4.SA'
            fdata, ferror = _fetch_bolsai_fundamentals_one(
                ticker_plain, fundamentals_source["endpoint"], fundamentals_api_key, cache_ttl
            )
            if fdata:
                for key in (
                    "pl", "pvp", "roe", "roa", "roic", "debt_equity", "net_debt_ebitda",
                    "net_margin", "ebitda_margin", "cagr_revenue_5y", "cagr_earnings_5y",
                    "market_cap",
                ):
                    if key in fdata:
                        record[key] = fdata[key]
            else:
                record["_fundamentals_error"] = ferror

        records.append(record)

    return records


def fetch_bolsai_fii(config: dict, cache_ttl: int) -> list[dict]:
    """Fundamentos reais de FII (P/VP, dividend yield, vacancia,
    inadimplencia) via bolsai. Exige API key -- NUNCA guardada no config
    (o repo e' publico), le' sempre de variavel de ambiente."""
    source = config["source"]
    endpoint_template = source["endpoint"]
    api_key_env_var = source.get("api_key_env_var", "BOLSAI_API_KEY")
    api_key = os.environ.get(api_key_env_var)
    if not api_key:
        raise RuntimeError(
            f"Variavel de ambiente {api_key_env_var} nao esta definida. "
            f"Pegue uma chave gratuita em usebolsai.com e exporte antes de rodar: "
            f"export {api_key_env_var}=sua_chave_aqui"
        )

    watchlist = config.get("watchlist", {})
    tickers = _resolve_watchlist_tickers(watchlist)
    if not tickers:
        raise RuntimeError(
            "watchlist.tickers esta vazio em config/fiis.json -- preencha com os "
            "tickers de FII que quer monitorar (ex: 'KNRI11')."
        )
    request_interval = watchlist.get("request_interval_seconds", 0.5)

    records = []
    for i, ticker in enumerate(tickers):
        if i > 0 and request_interval:
            time.sleep(request_interval)
        url = endpoint_template.format(ticker=ticker)
        try:
            data = fetch_json(url, ttl_seconds=cache_ttl, headers={"X-API-Key": api_key}, timeout=20.0)
        except RuntimeError as exc:
            records.append({"ticker": ticker, "_fetch_error": str(exc)})
            continue
        if data.get("error"):
            records.append({"ticker": ticker, "_fetch_error": data.get("message", "erro desconhecido")})
            continue

        pvp = data.get("pvp")
        record = dict(data)
        record["ticker"] = ticker
        record["pvp_deviation"] = abs(pvp - 1.0) if isinstance(pvp, (int, float)) else None
        records.append(record)

    return records


ADAPTERS = {
    "defillama_yields": fetch_defillama_yields,
    "yahoo_finance_chart": fetch_yahoo_finance_chart,
    "tesouro_transparente_csv": fetch_tesouro_transparente_csv,
    "bolsai_fii": fetch_bolsai_fii,
}


def fetch_records(config: dict, cache_ttl: int) -> list[dict]:
    source_type = config["source"]["type"]
    if source_type not in ADAPTERS:
        raise ValueError(
            f"source.type '{source_type}' nao tem adapter registrado em fetch.py. "
            f"Tipos disponiveis: {list(ADAPTERS)}"
        )
    return ADAPTERS[source_type](config, cache_ttl)
