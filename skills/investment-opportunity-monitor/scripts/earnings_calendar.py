"""Calendario real de divulgacao de resultados trimestrais (B3/CVM).

Fonte 100% publica, oficial, sem cadastro: Portal Dados Abertos CVM
(dados.cvm.gov.br). Dois datasets, atualizados semanalmente pela propria
CVM:

- FCA (Formulario Cadastral) / valor_mobiliario: da' o cruzamento
  CNPJ -> codigo de negociacao (ticker) na B3.
- ITR (Informacoes Trimestrais): da' a data real que cada empresa
  protocolou o resultado trimestral (DT_RECEB), por CNPJ.

Juntando os dois: ticker -> data do ultimo resultado trimestral
protocolado. Isso permite so' buscar fundamentos (bolsai) num ticker
quando o resultado dele realmente mudou, em vez de ficar pedindo o
mesmo dado de novo toda vez que o preco e' atualizado.

NAO cobre resultado anual/DFP (fora do escopo desse calendario, so'
ITR/trimestral por enquanto).
"""

import csv
import io
import json
import time
import urllib.request
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = SKILL_ROOT / "data" / "earnings_calendar.json"
USER_AGENT = "investment-opportunity-monitor/0.1 (+read-only research skill)"

FCA_URL_TEMPLATE = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FCA/DADOS/fca_cia_aberta_{year}.zip"
ITR_URL_TEMPLATE = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/ITR/DADOS/itr_cia_aberta_{year}.zip"


def _download_zip_member(url: str, member_name: str, timeout: float = 90.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        with zf.open(member_name) as f:
            return f.read()


def _parse_cnpj_to_tickers(year: int) -> dict[str, list[str]]:
    """CNPJ -> lista de codigos de negociacao (uma empresa pode ter
    varios: ON, PN, UNIT). So' considera Mercado == 'Bolsa' (ignora
    balcao/outros mercados)."""
    member = f"fca_cia_aberta_valor_mobiliario_{year}.csv"
    raw = _download_zip_member(FCA_URL_TEMPLATE.format(year=year), member)
    text = raw.decode("latin-1")
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    mapping: dict[str, list[str]] = {}
    for row in reader:
        codigo = (row.get("Codigo_Negociacao") or "").strip()
        mercado = (row.get("Mercado") or "").strip()
        cnpj = (row.get("CNPJ_Companhia") or "").strip()
        if not codigo or mercado != "Bolsa" or not cnpj:
            continue
        mapping.setdefault(cnpj, [])
        if codigo not in mapping[cnpj]:
            mapping[cnpj].append(codigo)
    return mapping


def _parse_cnpj_to_last_report(year: int) -> dict[str, str]:
    """CNPJ -> data (YYYY-MM-DD) do ITR mais recente protocolado nesse
    ano, olhando DT_RECEB (data real de recebimento pela CVM, nao a
    data de referencia do trimestre)."""
    member = f"itr_cia_aberta_{year}.csv"
    raw = _download_zip_member(ITR_URL_TEMPLATE.format(year=year), member)
    text = raw.decode("latin-1")
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    latest: dict[str, str] = {}
    for row in reader:
        cnpj = (row.get("CNPJ_CIA") or "").strip()
        dt_receb = (row.get("DT_RECEB") or "").strip()
        if not cnpj or not dt_receb:
            continue
        if cnpj not in latest or dt_receb > latest[cnpj]:
            latest[cnpj] = dt_receb
    return latest


def build_calendar(cache_ttl_hours: float = 20.0) -> dict[str, dict]:
    """Devolve {ticker: {'cnpj':..., 'last_report_date': 'YYYY-MM-DD'}}.

    Cacheado em disco (data/earnings_calendar.json) -- os dados da CVM so'
    atualizam 1x/semana, entao nao ha' motivo pra rebaixar isso toda hora.
    Sempre olha o ano corrente; perto da virada do ano isso perderia o
    ultimo resultado do Q3 do ano anterior por uns dias, mas o proprio
    Q1 do ano novo substitui rapido -- nao vale a complexidade de
    combinar dois anos pra esse caso de borda."""
    if CACHE_PATH.exists():
        try:
            cached = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            built_at = datetime.fromisoformat(cached["built_at"])
            age_hours = (datetime.now(timezone.utc) - built_at).total_seconds() / 3600
            if age_hours < cache_ttl_hours:
                return cached["calendar"]
        except (json.JSONDecodeError, KeyError, ValueError, OSError):
            pass

    year = date.today().year
    cnpj_to_tickers = _parse_cnpj_to_tickers(year)
    cnpj_to_report = _parse_cnpj_to_last_report(year)

    calendar: dict[str, dict] = {}
    for cnpj, tickers in cnpj_to_tickers.items():
        report_date = cnpj_to_report.get(cnpj)
        if not report_date:
            continue
        for ticker in tickers:
            calendar[ticker] = {"cnpj": cnpj, "last_report_date": report_date}

    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(
            json.dumps(
                {"built_at": datetime.now(timezone.utc).isoformat(), "calendar": calendar},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass

    return calendar


def last_report_date(calendar: dict[str, dict], ticker_plain: str) -> str | None:
    """ticker_plain sem sufixo .SA (ex: 'PETR4'), pra bater com o
    Codigo_Negociacao da CVM."""
    entry = calendar.get(ticker_plain)
    return entry["last_report_date"] if entry else None
