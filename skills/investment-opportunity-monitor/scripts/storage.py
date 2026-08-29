"""Historico com timestamp, compartilhado por todas as classes de ativo.

Cada classe grava num arquivo SQLite proprio (definido em config['history']['path']),
numa tabela unica de snapshots com o registro completo em JSON + colunas
indexadas para consulta rapida (id do ativo, score, tvl/preco quando houver,
timestamp). Isso evita esquema rigido por classe e permite consultar
tendencia ao longo do tempo sem migrar tabelas quando um campo novo aparece.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent


def _resolve_db_path(history_config: dict) -> Path:
    path = Path(history_config["path"])
    if not path.is_absolute():
        path = SKILL_ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _ensure_table(conn: sqlite3.Connection, table: str) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {table} (
            snapshot_ts TEXT NOT NULL,
            record_id TEXT,
            name TEXT,
            protocol_or_issuer TEXT,
            risk_score REAL,
            primary_metric REAL,
            record_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{table}_record_ts ON {table} (record_id, snapshot_ts)"
    )


def save_snapshot(records: list[dict], config: dict) -> Path:
    history_config = config["history"]
    table = history_config["table"]
    db_path = _resolve_db_path(history_config)
    field_map = config.get("record_fields", {})

    id_field = field_map.get("id_field")
    name_field = field_map.get("name_field")
    protocol_field = field_map.get("protocol_field") or field_map.get("type_field")
    metric_field = field_map.get("tvl_field") or field_map.get("price_field")

    ts = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(db_path)
    try:
        _ensure_table(conn, table)
        rows = []
        for r in records:
            score = r.get("risk_score")
            score_val = score if isinstance(score, (int, float)) else None
            rows.append(
                (
                    ts,
                    str(r.get(id_field)) if id_field else None,
                    r.get(name_field) if name_field else None,
                    r.get(protocol_field) if protocol_field else None,
                    score_val,
                    r.get(metric_field) if metric_field else None,
                    json.dumps(r, ensure_ascii=False, default=str),
                )
            )
        conn.executemany(
            f"INSERT INTO {table} (snapshot_ts, record_id, name, protocol_or_issuer, "
            f"risk_score, primary_metric, record_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()

    return db_path


def load_latest_per_id(config: dict, within_days: int = 7) -> list[dict]:
    """Devolve o registro mais recente de cada record_id dentro da janela
    de dias pedida -- usado pra reconstruir o quadro "cheio" de uma
    watchlist grande que so' e' buscada em fatias por dia da semana (ver
    watchlist.rotation em config/stocks.json). Cada registro devolvido
    carrega '_snapshot_ts' e '_snapshot_age_days' pra deixar explicito
    quao antigo aquele dado especifico e' -- nunca escondido."""
    history_config = config["history"]
    table = history_config["table"]
    db_path = _resolve_db_path(history_config)
    if not db_path.exists():
        return []

    cutoff = (datetime.now(timezone.utc) - timedelta(days=within_days)).isoformat()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_table(conn, table)
        cur = conn.execute(
            f"""
            SELECT record_json, snapshot_ts FROM {table} t1
            WHERE snapshot_ts = (
                SELECT MAX(snapshot_ts) FROM {table} t2
                WHERE t2.record_id = t1.record_id AND t2.snapshot_ts >= ?
            )
            AND snapshot_ts >= ?
            """,
            (cutoff, cutoff),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    now = datetime.now(timezone.utc)
    out = []
    for row in rows:
        record = json.loads(row["record_json"])
        snap_ts = row["snapshot_ts"]
        record["_snapshot_ts"] = snap_ts
        try:
            age_days = (now - datetime.fromisoformat(snap_ts)).total_seconds() / 86400
            record["_snapshot_age_days"] = round(age_days, 1)
        except ValueError:
            record["_snapshot_age_days"] = None
        out.append(record)
    return out


def load_history(config: dict, record_id: str, limit: int = 30) -> list[dict]:
    history_config = config["history"]
    table = history_config["table"]
    db_path = _resolve_db_path(history_config)
    if not db_path.exists():
        return []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_table(conn, table)
        cur = conn.execute(
            f"SELECT snapshot_ts, risk_score, primary_metric FROM {table} "
            f"WHERE record_id = ? ORDER BY snapshot_ts DESC LIMIT ?",
            (record_id, limit),
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()
