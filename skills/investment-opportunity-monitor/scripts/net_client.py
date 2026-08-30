"""HTTP helper compartilhado: retry com backoff exponencial + cache em disco com TTL.

Usado por todos os adapters de fetch (DeFi, acoes, renda fixa) para evitar
duplicar logica de rede. Nunca inventa dados: se a requisicao falhar apos
todas as tentativas, propaga a excecao para quem chamou decidir o que fazer
(o chamador marca o campo como indisponivel, nunca substitui por um valor
mockado).
"""

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"
USER_AGENT = "investment-opportunity-monitor/0.1 (+read-only research skill)"
RATE_LIMIT_STATE_PATH = DEFAULT_CACHE_DIR.parent / "rate_limits.json"
RATE_LIMIT_HEADER = "X-RateLimit-Remaining"


def _record_rate_limit(url: str, headers) -> None:
    """Se a resposta trouxer X-RateLimit-Remaining (ex: bolsai), persiste por
    host num arquivo pequeno em data/ -- cada execucao do monitor.py e' um
    processo novo (rodando via systemd oneshot), entao isso precisa
    sobreviver no disco, nao so' em memoria. Nunca levanta excecao: um
    problema aqui nao pode derrubar a busca de dado real."""
    if headers is None:
        return
    remaining = headers.get(RATE_LIMIT_HEADER)
    if remaining is None:
        return
    host = urllib.parse.urlparse(url).netloc
    if not host:
        return
    state = {}
    if RATE_LIMIT_STATE_PATH.exists():
        try:
            state = json.loads(RATE_LIMIT_STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            state = {}
    state[host] = {"remaining": remaining, "checked_at": time.time()}
    try:
        RATE_LIMIT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        RATE_LIMIT_STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


def get_rate_limit_status(host: str) -> dict | None:
    """Le de volta o ultimo X-RateLimit-Remaining visto para um host, se
    houver. Retorna None se nunca vimos esse header desse host."""
    if not RATE_LIMIT_STATE_PATH.exists():
        return None
    try:
        state = json.loads(RATE_LIMIT_STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return state.get(host)


def _cache_path(url: str, cache_dir: Path) -> Path:
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
    return cache_dir / f"{key}.json"


def _read_cache(url: str, ttl_seconds: int, cache_dir: Path):
    if ttl_seconds <= 0:
        return None
    path = _cache_path(url, cache_dir)
    if not path.exists():
        return None
    age = time.time() - path.stat().st_mtime
    if age > ttl_seconds:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)["body"]
    except (json.JSONDecodeError, KeyError, OSError):
        return None


def _write_cache(url: str, body: str, cache_dir: Path) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_path(url, cache_dir)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"url": url, "fetched_at": time.time(), "body": body}, f)


def fetch_text(
    url: str,
    *,
    ttl_seconds: int = 300,
    max_retries: int = 4,
    base_delay: float = 1.0,
    timeout: float = 20.0,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    headers: dict | None = None,
) -> str:
    """Busca uma URL como texto, com cache TTL e retry exponencial.

    Levanta a excecao original se todas as tentativas falharem -- o chamador
    e responsavel por tratar isso como "dado indisponivel", nunca por
    inventar um valor no lugar.
    """
    cached = _read_cache(url, ttl_seconds, cache_dir)
    if cached is not None:
        return cached

    req_headers = {"User-Agent": USER_AGENT}
    if headers:
        req_headers.update(headers)

    last_exc = None
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers=req_headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                _record_rate_limit(url, resp.headers)
                body = resp.read().decode(resp.headers.get_content_charset() or "utf-8")
            _write_cache(url, body, cache_dir)
            return body
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            last_exc = exc
            if isinstance(exc, urllib.error.HTTPError):
                _record_rate_limit(url, exc.headers)
            if attempt < max_retries - 1:
                retry_after = None
                if isinstance(exc, urllib.error.HTTPError):
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                if retry_after:
                    try:
                        delay = float(retry_after)
                    except ValueError:
                        delay = base_delay * (2**attempt)
                else:
                    delay = base_delay * (2**attempt)
                time.sleep(delay)
    raise RuntimeError(f"Falha ao buscar {url} apos {max_retries} tentativas") from last_exc


def fetch_json(url: str, **kwargs):
    return json.loads(fetch_text(url, **kwargs))
