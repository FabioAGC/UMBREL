"""ETL MES -> Snowflake, agendada, com status e botão "rodar agora".

Cada execução:
  1. janela = [marca d'água - ETL_OVERLAP_HOURS, agora]  (na 1ª vez: últimos ETL_INITIAL_DAYS)
  2. busca no MES em blocos de ETL_CHUNK_HOURS (mesma função da tela de 7 dias)
  3. grava num staging e, numa transação, apaga a janela no destino e insere o staging
     -> pode rodar de novo sobre a mesma janela sem duplicar (registros editados no MES
        depois de lidos também são corrigidos dentro da sobreposição)
  4. avança a marca d'água só se tudo deu certo

Histórico de execuções e marca d'água ficam em SQLite no volume /data.
"""
from __future__ import annotations

import base64
import logging
import os
import re
import sqlite3
import threading
import time
import traceback
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException

from mes_client import MesClient, buscar_registros

log = logging.getLogger("etl-mes")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

E = os.environ
DB_PATH = E.get("ETL_DB", "/data/etl.sqlite")
INTERVALO_MIN = float(E.get("ETL_INTERVAL_MIN", "60"))
DIAS_INICIAIS = float(E.get("ETL_INITIAL_DAYS", "7"))
SOBREPOSICAO = timedelta(hours=float(E.get("ETL_OVERLAP_HOURS", "6")))
BLOCO = timedelta(hours=float(E.get("ETL_CHUNK_HOURS", "24")))


def _ident(nome: str) -> str:
    """Nome seguro e sem aspas para o Snowflake (maiúsculas, só [A-Z0-9_])."""
    s = re.sub(r"\W", "_", nome.strip()).upper()
    return s if re.match(r"[A-Z_]", s) else f"C_{s}"


DESTINO = _ident(E.get("ETL_TARGET_TABLE", "MES_REGISTROS"))
STAGING = f"{DESTINO}__STAGING"
COLUNA_DATA = _ident(E.get("MES_COLUNA_DATA", "EventTime"))

app = FastAPI(title="etl-mes")
_rodando = threading.Lock()
_sched = BackgroundScheduler(timezone="America/Sao_Paulo")


# ----------------------------------------------------------------------------- estado local
def _db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE IF NOT EXISTS execucoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, origem TEXT, inicio TEXT, fim TEXT,
            status TEXT, linhas INTEGER, janela_inicio TEXT, janela_fim TEXT, erro TEXT);
        CREATE TABLE IF NOT EXISTS estado (chave TEXT PRIMARY KEY, valor TEXT);
    """)
    return c


def _marca_dagua() -> datetime | None:
    with closing(_db()) as c:
        row = c.execute("SELECT valor FROM estado WHERE chave='marca_dagua'").fetchone()
    return datetime.fromisoformat(row["valor"]) if row else None


# ----------------------------------------------------------------------------- Snowflake
def _snowflake():
    import snowflake.connector
    from cryptography.hazmat.primitives import serialization

    faltando = [k for k in ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_WAREHOUSE", "SNOWFLAKE_DATABASE",
                            "SNOWFLAKE_SCHEMA", "SNOWFLAKE_PRIVATE_KEY_B64") if not E.get(k)]
    if faltando:
        raise RuntimeError(f"Snowflake não configurado: faltam {', '.join(faltando)}")
    senha = E.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE") or None
    chave = serialization.load_pem_private_key(
        base64.b64decode(E["SNOWFLAKE_PRIVATE_KEY_B64"]), password=senha.encode() if senha else None)
    der = chave.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption())
    return snowflake.connector.connect(
        account=E["SNOWFLAKE_ACCOUNT"], user=E["SNOWFLAKE_USER"], private_key=der,
        role=E.get("SNOWFLAKE_ROLE") or None, warehouse=E["SNOWFLAKE_WAREHOUSE"],
        database=E["SNOWFLAKE_DATABASE"], schema=E["SNOWFLAKE_SCHEMA"],
        application="fabrica-apps-etl-mes", client_session_keep_alive=False)


def _carregar(df: pd.DataFrame, ini: datetime, fim: datetime) -> None:
    from snowflake.connector.pandas_tools import write_pandas

    with closing(_snowflake()) as con:
        ok, _, n, _ = write_pandas(con, df, STAGING, auto_create_table=True, overwrite=True,
                                   quote_identifiers=False, use_logical_type=True)
        if not ok:
            raise RuntimeError("write_pandas falhou no staging")
        cur = con.cursor()
        cur.execute(f"CREATE TABLE IF NOT EXISTS {DESTINO} LIKE {STAGING}")
        cur.execute("BEGIN")
        try:
            cur.execute(f"DELETE FROM {DESTINO} WHERE {COLUNA_DATA} >= %s AND {COLUNA_DATA} < %s", (ini, fim))
            colunas = ", ".join(df.columns)
            cur.execute(f"INSERT INTO {DESTINO} ({colunas}) SELECT {colunas} FROM {STAGING}")
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        log.info("Snowflake: %d linhas em %s (janela %s -> %s)", n, DESTINO, ini, fim)


# ----------------------------------------------------------------------------- execução
def executar(origem: str = "agendada") -> dict:
    if not _rodando.acquire(blocking=False):
        raise RuntimeError("Já existe uma execução em andamento")
    agora = datetime.now(timezone.utc)
    marca = _marca_dagua()
    ini = (marca - SOBREPOSICAO) if marca else agora - timedelta(days=DIAS_INICIAIS)
    with closing(_db()) as c:
        cur = c.execute("INSERT INTO execucoes (origem, inicio, status, janela_inicio, janela_fim) "
                        "VALUES (?, ?, 'rodando', ?, ?)", (origem, agora.isoformat(), ini.isoformat(), agora.isoformat()))
        exec_id = cur.lastrowid
        c.commit()
    status, linhas, erro = "ok", 0, None
    try:
        cliente = MesClient()
        partes, t = [], ini
        while t < agora:
            t2 = min(t + BLOCO, agora)
            partes.extend(buscar_registros(cliente, t, t2))
            t = t2
        df = pd.DataFrame(partes)
        if df.empty:
            log.info("Nenhum registro na janela %s -> %s", ini, agora)
        else:
            df.columns = [_ident(c) for c in df.columns]
            if COLUNA_DATA not in df.columns:
                raise RuntimeError(f"Coluna de data {COLUNA_DATA} não veio do MES. Colunas: {list(df.columns)[:20]}")
            df[COLUNA_DATA] = pd.to_datetime(df[COLUNA_DATA], utc=True, errors="coerce")
            df = df[(df[COLUNA_DATA] >= ini) & (df[COLUNA_DATA] < agora)]
            df["CARREGADO_EM"] = pd.Timestamp(agora)
            _carregar(df, ini, agora)
            linhas = len(df)
        with closing(_db()) as c:
            c.execute("INSERT OR REPLACE INTO estado VALUES ('marca_dagua', ?)", (agora.isoformat(),))
            c.commit()
    except Exception as e:
        status, erro = "erro", f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}"
        log.exception("ETL falhou")
    finally:
        with closing(_db()) as c:
            c.execute("UPDATE execucoes SET fim=?, status=?, linhas=?, erro=? WHERE id=?",
                      (datetime.now(timezone.utc).isoformat(), status, linhas, erro, exec_id))
            c.commit()
        _rodando.release()
    return {"id": exec_id, "status": status, "linhas": linhas, "erro": erro}


def _job(origem: str = "agendada") -> None:
    try:
        executar(origem)
    except RuntimeError as e:  # outra execução pegou o lock antes
        log.warning("Pulando execução %s: %s", origem, e)


@app.on_event("startup")
def _start() -> None:
    with closing(_db()) as c:  # execução interrompida por restart do container
        c.execute("UPDATE execucoes SET status='interrompida', fim=? WHERE status='rodando'",
                  (datetime.now(timezone.utc).isoformat(),))
        c.commit()
    _sched.add_job(_job, "interval", minutes=INTERVALO_MIN, id="etl", max_instances=1, coalesce=True,
                   next_run_time=datetime.now(timezone.utc) + timedelta(minutes=1))
    _sched.start()


# ----------------------------------------------------------------------------- API
@app.get("/health")
def health():
    return {"ok": True}


@app.get("/status")
def status():
    job = _sched.get_job("etl")
    with closing(_db()) as c:
        execs = [dict(r) for r in c.execute("SELECT * FROM execucoes ORDER BY id DESC LIMIT 20")]
    marca = _marca_dagua()
    return {
        "rodando": _rodando.locked(),
        "proxima_execucao": job.next_run_time.isoformat() if job and job.next_run_time else None,
        "intervalo_min": INTERVALO_MIN,
        "marca_dagua": marca.isoformat() if marca else None,
        "destino": f"{E.get('SNOWFLAKE_DATABASE', '?')}.{E.get('SNOWFLAKE_SCHEMA', '?')}.{DESTINO}",
        "execucoes": execs,
    }


@app.post("/run")
def rodar_agora():
    if _rodando.locked():
        raise HTTPException(409, "Já existe uma execução em andamento")
    threading.Thread(target=_job, args=("manual",), daemon=True).start()
    time.sleep(0.2)
    return {"iniciado": True}
