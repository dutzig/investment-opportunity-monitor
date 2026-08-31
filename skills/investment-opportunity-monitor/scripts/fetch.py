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
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from net_client import fetch_json, fetch_text, get_rate_limit_status
import earnings_calendar
import storage

try:
    import swing_local  # arquivo pessoal, fora do git -- ver scripts/swing_local.py
except ImportError:
    swing_local = None

SKILL_ROOT = Path(__file__).resolve().parent.parent
FUNDAMENTALS_STATE_PATH = SKILL_ROOT / "data" / "fundamentals_fetch_state.json"
FUNDAMENTALS_FIELDS = (
    "pl", "pvp", "roe", "roa", "roic", "debt_equity", "net_debt_ebitda",
    "net_margin", "ebitda_margin", "cagr_revenue_5y", "cagr_earnings_5y",
    "market_cap",
)


def _load_local_tickers(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    return data.get("tickers", [])


def _resolve_watchlist_tickers(watchlist: dict) -> list[str]:
    """Uniao entre a watchlist publica do config ('tickers', sempre dado
    generico/oficial, versionado no git) e um suplemento local opcional
    ('local_supplement_path') -- arquivo fora do repo (dir data/*.json,
    ja coberto pelo .gitignore) que cada pessoa usa pra adicionar seus
    proprios tickers de interesse (ex: papeis da holdings pessoais que
    nao caem em nenhum indice oficial) sem que isso vaze pro repo
    publico. Nunca comitar dado pessoal (composicao de carteira, valores,
    nomes) em config/*.json -- isso sempre fica no arquivo local.

    Se 'priority_local_path' tambem estiver configurado (outro arquivo
    local, fora do git), os tickers listados la' vao pro INICIO da lista
    resultante -- garante que holdings pessoais sao sempre buscados
    primeiro, antes do resto do universo, entao mesmo se a fonte cortar
    no meio (cota estourada, rate limit) os ativos que a pessoa realmente
    tem ja foram atualizados."""
    tickers = list(watchlist.get("tickers", []))
    supplement_path = watchlist.get("local_supplement_path")
    if supplement_path:
        for t in _load_local_tickers(SKILL_ROOT / supplement_path):
            if t not in tickers:
                tickers.append(t)

    priority_path = watchlist.get("priority_local_path")
    if priority_path:
        priority_set = _load_local_tickers(SKILL_ROOT / priority_path)
        priority = [t for t in priority_set if t in tickers]
        rest = [t for t in tickers if t not in priority]
        tickers = priority + rest

    return tickers


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


def _parse_hhmm_utc(value: str) -> int:
    """'22:00' -> 1320 (minutos desde meia-noite UTC)."""
    h, m = value.split(":")
    return int(h) * 60 + int(m)


def _within_overnight_window(window: dict) -> bool:
    """Confere se o horario atual (UTC) esta dentro da janela configurada
    E se o pregao que essa janela reflete foi um dia util (B3 nao opera
    fim de semana -- sem isso, a janela ficava rodando a noite toda de
    sabado e domingo buscando o MESMO fechamento de sexta de novo e de
    novo, sem nenhum dado novo possivel).

    A janela cruza meia-noite UTC (ex: 22:00 as 11:30) -- entao tem duas
    partes, cada uma reflete um pregao diferente:
    - parte da noite (hora >= start, ex: 22h-23h59): reflete o pregao de
      HOJE (acabou de fechar).
    - parte da madrugada (hora < end, ex: 00h-11h30): reflete o pregao
      de ONTEM (a mesma janela que comecou na noite anterior).
    So' roda se esse pregao referenciado caiu numa segunda-sexta."""
    if not window or not window.get("enabled"):
        return True
    start = _parse_hhmm_utc(window.get("start_utc", "22:00"))
    end = _parse_hhmm_utc(window.get("end_utc", "11:30"))
    now = datetime.now(timezone.utc)
    now_minutes = now.hour * 60 + now.minute

    if start <= end:
        in_window = start <= now_minutes < end
    else:
        in_window = now_minutes >= start or now_minutes < end
    if not in_window:
        return False

    if window.get("skip_weekends", True):
        if now_minutes >= start:
            referenced_day = now.weekday()  # parte da noite -> pregao de hoje
        else:
            referenced_day = (now - timedelta(days=1)).weekday()  # madrugada -> pregao de ontem
        if referenced_day >= 5:  # 5=sabado, 6=domingo
            return False

    return True


def _select_cursor_batch(tickers: list[str], batch_config: dict, state_path: Path) -> list[str]:
    """Pega o proximo pedaco da watchlist a partir de um cursor persistido
    em disco, avancando a cada chamada e dando a volta quando chega no
    fim -- substitui a rotacao por dia da semana. Como a lista ja vem com
    as prioridades (holdings pessoais) na frente (ver
    _resolve_watchlist_tickers), rodando repetidamente dentro da janela
    overnight, os primeiros lotes da noite sempre cobrem holdings
    pessoais primeiro, e o resto do universo completa ao longo da noite
    -- ver watchlist.batch e watchlist.overnight_window no config."""
    if not batch_config or not batch_config.get("enabled") or not tickers:
        return tickers

    size = batch_config.get("size", 20)
    state = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            state = {}

    start = state.get("next_index", 0) % len(tickers)
    batch = tickers[start : start + size]
    if len(batch) < size:
        batch += tickers[: size - len(batch)]
    next_index = (start + size) % len(tickers)

    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps({"next_index": next_index, "updated_at": datetime.now(timezone.utc).isoformat()}),
            encoding="utf-8",
        )
    except OSError:
        pass

    return batch


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


def _load_fundamentals_state() -> dict:
    if not FUNDAMENTALS_STATE_PATH.exists():
        return {}
    try:
        return json.loads(FUNDAMENTALS_STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_fundamentals_state(state: dict) -> None:
    try:
        FUNDAMENTALS_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        FUNDAMENTALS_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _bolsai_quota_likely_exhausted(host: str, staleness_hours: float = 2.0) -> bool:
    """Confere a ultima leitura conhecida de X-RateLimit-Remaining (ver
    net_client.py) antes de tentar qualquer chamada -- se a ultima vez
    que vimos a bolsai ela tinha 0 restantes E essa leitura e' recente
    (dentro de staleness_hours), nem tenta: sem isso, cada lote insistia
    de novo do zero (4 tentativas com backoff POR ticker) mesmo sabendo
    de antemao que ia falhar, so' pra redescobrir o que ja sabiamos.
    Se a leitura for antiga (cota pode ter resetado), deixa passar pra
    fazer uma tentativa real de novo -- e' assim que o sistema volta a
    funcionar sozinho sem precisar saber a hora exata do reset."""
    status = get_rate_limit_status(host)
    if not status:
        return False
    try:
        remaining = int(status.get("remaining", "1"))
    except (TypeError, ValueError):
        return False
    if remaining > 0:
        return False
    age_hours = (time.time() - status.get("checked_at", 0)) / 3600
    return age_hours < staleness_hours


def _load_earnings_calendar_safe() -> dict:
    """Nunca deixa a build do calendario (download real da CVM) derrubar
    a busca inteira -- se falhar (fora do ar, formato mudou), devolve
    calendario vazio e o chamador trata como 'sempre buscar', que e' o
    comportamento seguro por padrao (nunca assume 'nao precisa' sem
    saber de verdade)."""
    try:
        return earnings_calendar.build_calendar()
    except Exception:
        return {}


def fetch_yahoo_finance_chart(config: dict, cache_ttl: int) -> list[dict]:
    source = config["source"]
    endpoint_template = source["endpoint"]
    watchlist = config.get("watchlist", {})
    all_tickers = _resolve_watchlist_tickers(watchlist)
    if not all_tickers:
        raise RuntimeError(
            "watchlist.tickers esta vazio em config/stocks.json -- preencha com os "
            "tickers B3 que quer monitorar (ex: 'PETR4.SA') antes de rodar."
        )

    if not _within_overnight_window(watchlist.get("overnight_window", {})):
        return []

    state_path = SKILL_ROOT / watchlist.get("batch", {}).get("state_path", "data/stocks_batch_cursor.json")
    tickers = _select_cursor_batch(all_tickers, watchlist.get("batch", {}), state_path)
    benchmark_ticker = watchlist.get("benchmark_ticker", "^BVSP")
    history_range = watchlist.get("history_range", "3mo")
    request_interval = watchlist.get("request_interval_seconds", 1.5)

    fundamentals_source = config.get("fundamentals_source")
    fundamentals_api_key = None
    earnings_cal = {}
    fundamentals_state = {}
    fundamentals_host = None
    quota_exhausted = False
    if fundamentals_source:
        env_var = fundamentals_source.get("api_key_env_var", "BOLSAI_API_KEY")
        fundamentals_api_key = os.environ.get(env_var)
        if not fundamentals_api_key:
            raise RuntimeError(
                f"config tem 'fundamentals_source' mas a variavel de ambiente {env_var} "
                f"nao esta definida. Pegue uma chave gratuita em usebolsai.com e exporte "
                f"antes de rodar: export {env_var}=sua_chave_aqui"
            )
        earnings_cal = _load_earnings_calendar_safe()
        fundamentals_state = _load_fundamentals_state()
        fundamentals_host = urllib.parse.urlparse(fundamentals_source["endpoint"]).netloc
        quota_exhausted = _bolsai_quota_likely_exhausted(fundamentals_host)

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
        highs = quote.get("high", [])
        lows = quote.get("low", [])
        # O Yahoo devolve close=None pro candle mais recente quando ele
        # ainda esta incompleto, mas high/low vem 0.0 (nao None) nesse
        # mesmo candle -- sem isso, 0.0 entra no calculo de estocastico/
        # padrao como se fosse uma minima real, corrompendo tudo. Anula
        # high/low/volume onde o close correspondente e' None.
        highs = [h if c is not None else None for h, c in zip(highs, closes)]
        lows = [l if c is not None else None for l, c in zip(lows, closes)]
        volumes = [v if c is not None else None for v, c in zip(volumes, closes)]
        # Corta candle(s) incompleto(s) do final -- sem isso "o mais
        # recente" (indice -1, usado pro stoch/padrao/dias_atras) podia
        # ser um dia sem dado real.
        while closes and closes[-1] is None:
            closes, volumes, highs, lows, ts = closes[:-1], volumes[:-1], highs[:-1], lows[:-1], ts[:-1]

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
        if swing_local:
            record.update(swing_local.compute(highs, lows, closes, volumes))

        if fundamentals_source:
            ticker_plain = ticker.split(".")[0]  # bolsai usa 'PETR4', nao 'PETR4.SA'
            calendar_date = earnings_calendar.last_report_date(earnings_cal, ticker_plain)
            fetched_at_date = fundamentals_state.get(ticker, {}).get("report_date")
            # Sem calendario pra esse ticker (lacuna real da fonte, ex:
            # units com codigo "000000" no cadastro da CVM), ou nunca
            # buscado ainda: sempre conta como "devido", nunca assume
            # "nao precisa" sem saber de verdade.
            is_due = calendar_date is None or fetched_at_date is None or calendar_date > fetched_at_date

            got_fresh = False
            if is_due and not quota_exhausted:
                if i > 0 and request_interval:
                    time.sleep(request_interval)
                fdata, ferror = _fetch_bolsai_fundamentals_one(
                    ticker_plain, fundamentals_source["endpoint"], fundamentals_api_key, cache_ttl
                )
                if fdata:
                    for key in FUNDAMENTALS_FIELDS:
                        if key in fdata:
                            record[key] = fdata[key]
                    record["_fundamentals_fetched_at"] = datetime.now(timezone.utc).isoformat()
                    if calendar_date:
                        fundamentals_state[ticker] = {"report_date": calendar_date}
                    got_fresh = True
                else:
                    record["_fundamentals_error"] = ferror
                    # A chamada que acabou de falhar ja atualizou
                    # rate_limits.json (net_client._record_rate_limit) --
                    # confere de novo: se a cota zerou agora, para de
                    # tentar nos proximos tickers deste mesmo lote.
                    if fundamentals_host and _bolsai_quota_likely_exhausted(fundamentals_host):
                        quota_exhausted = True

            if not got_fresh:
                # Nao buscou fresco (nao devido, cota esgotada, ou a
                # tentativa falhou) -- sempre tenta cair pro ultimo
                # fundamento salvo no historico antes de desistir.
                cached = storage.load_latest_record(config, ticker)
                if cached:
                    for key in FUNDAMENTALS_FIELDS:
                        if key in cached and cached[key] is not None:
                            record[key] = cached[key]
                    record["_fundamentals_cached_from_report"] = calendar_date
                    record.pop("_fundamentals_error", None)
                elif "_fundamentals_error" not in record:
                    record["_fundamentals_error"] = (
                        "cota da bolsai provavelmente esgotada e sem dado em cache pra reaproveitar"
                        if quota_exhausted
                        else "sem cobertura fundamentalista disponivel"
                    )

        records.append(record)

    if fundamentals_source:
        _save_fundamentals_state(fundamentals_state)

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
