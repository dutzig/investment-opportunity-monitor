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
import urllib.request
from pathlib import Path

DEFAULT_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"
USER_AGENT = "investment-opportunity-monitor/0.1 (+read-only research skill)"


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
                body = resp.read().decode(resp.headers.get_content_charset() or "utf-8")
            _write_cache(url, body, cache_dir)
            return body
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            last_exc = exc
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
