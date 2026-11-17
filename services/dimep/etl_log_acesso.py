"""
Extrai dbo.LOG_ACESSO do DMPACESSOII para arquivos Parquet, mantendo apenas as
colunas relevantes para analisar entrada/saida de colaboradores por area
(torniquete).

Particionamento: anos fechados (antes do ano corrente) viram um arquivo por
ano (log_acesso_AAAA.parquet); o ano corrente vira um arquivo por mes
(log_acesso_AAAA_MM.parquet), pra permitir atualizar so o mes atual depois
sem reprocessar o ano inteiro — veja scheduler_pausas.py (atualizar_mes_atual).

Regra de direcao (observada nos dados):
    TP_SENTIDO_CONSULTA = 1 -> ENTRADA (foi para uma area "interna")
    TP_SENTIDO_CONSULTA = 2 -> SAIDA   (foi para uma area "externa")
    TP_SENTIDO_CONSULTA = 3 ou NULL -> OUTRO (ex: leitura sem sentido definido)

Janela de datas: 2022-01-01 a 2027-01-01. Fora disso a base tem ~3 mil linhas com
datas claramente invalidas (ano 2000, 2033, 2078 etc.) que sao descartadas aqui.
"""

import datetime as dt
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from db import connect

DATA_DIR = Path(__file__).parent / "data"
BATCH_SIZE = 100_000
YEAR_START = 2022
YEAR_END = 2026  # inclusive

COLUMNS = [
    "CD_LOG_ACESSO",
    "NU_MATRICULA",
    "NM_PESSOA",
    "CD_AREA_ORIGEM",
    "DS_AREA_ORIGEM",
    "CD_AREA_DESTINO",
    "DS_AREA_DESTINO",
    "CD_EQUIPAMENTO",
    "DS_EQUIPAMENTO",
    "CD_ESTRUTURA",
    "NM_ESTRUTURA",
    "TP_SENTIDO_CONSULTA",
    "TP_EVENTO",
    "DT_REQUISICAO",
]

SCHEMA = pa.schema(
    [
        ("CD_LOG_ACESSO", pa.int64()),
        ("NU_MATRICULA", pa.int64()),
        ("NM_PESSOA", pa.string()),
        ("CD_AREA_ORIGEM", pa.int32()),
        ("DS_AREA_ORIGEM", pa.string()),
        ("CD_AREA_DESTINO", pa.int32()),
        ("DS_AREA_DESTINO", pa.string()),
        ("CD_EQUIPAMENTO", pa.int32()),
        ("DS_EQUIPAMENTO", pa.string()),
        ("CD_ESTRUTURA", pa.int32()),
        ("NM_ESTRUTURA", pa.string()),
        ("TP_SENTIDO_CONSULTA", pa.int8()),
        ("DS_MOVIMENTO", pa.string()),
        ("TP_EVENTO", pa.int32()),
        ("DT_REQUISICAO", pa.timestamp("us")),
    ]
)

MOVIMENTO_MAP = {1: "ENTRADA", 2: "SAIDA"}


def to_int(v):
    return int(v) if v is not None else None


def build_batch(rows):
    cols = {name: [] for name in SCHEMA.names}
    for row in rows:
        (
            cd_log,
            nu_matricula,
            nm_pessoa,
            cd_area_origem,
            ds_area_origem,
            cd_area_destino,
            ds_area_destino,
            cd_equipamento,
            ds_equipamento,
            cd_estrutura,
            nm_estrutura,
            tp_sentido,
            tp_evento,
            dt_requisicao,
        ) = row

        sentido = to_int(tp_sentido)
        cols["CD_LOG_ACESSO"].append(to_int(cd_log))
        cols["NU_MATRICULA"].append(to_int(nu_matricula))
        cols["NM_PESSOA"].append(nm_pessoa)
        cols["CD_AREA_ORIGEM"].append(to_int(cd_area_origem))
        cols["DS_AREA_ORIGEM"].append(ds_area_origem)
        cols["CD_AREA_DESTINO"].append(to_int(cd_area_destino))
        cols["DS_AREA_DESTINO"].append(ds_area_destino)
        cols["CD_EQUIPAMENTO"].append(to_int(cd_equipamento))
        cols["DS_EQUIPAMENTO"].append(ds_equipamento)
        cols["CD_ESTRUTURA"].append(to_int(cd_estrutura))
        cols["NM_ESTRUTURA"].append(nm_estrutura)
        cols["TP_SENTIDO_CONSULTA"].append(sentido)
        cols["DS_MOVIMENTO"].append(MOVIMENTO_MAP.get(sentido, "OUTRO"))
        cols["TP_EVENTO"].append(to_int(tp_evento))
        cols["DT_REQUISICAO"].append(dt_requisicao)

    return pa.record_batch(
        [pa.array(cols[name], type=SCHEMA.field(name).type) for name in SCHEMA.names],
        schema=SCHEMA,
    )


def extract_range(cursor, start, end, out_path, label=None):
    """Extrai DT_REQUISICAO em [start, end) para out_path. Sobrescreve o arquivo
    (nao faz append/merge) — por isso o range deve ser um bloco fechado (um ano
    ou um mes inteiro), nunca so 'os dias novos'."""
    label = label or out_path.stem
    query = f"""
        SELECT {', '.join(COLUMNS)}
        FROM dbo.LOG_ACESSO
        WHERE DT_REQUISICAO >= ? AND DT_REQUISICAO < ?
    """
    cursor.execute(query, start, end)

    total = 0
    writer = None
    try:
        while True:
            rows = cursor.fetchmany(BATCH_SIZE)
            if not rows:
                break
            batch = build_batch(rows)
            if writer is None:
                writer = pq.ParquetWriter(out_path, SCHEMA, compression="snappy")
            writer.write_batch(batch)
            total += len(rows)
            print(f"  [{label}] {total:,} linhas gravadas...", end="\r")
    finally:
        if writer is not None:
            writer.close()

    if writer is None:
        # sem linhas no periodo: garante que nao sobra arquivo velho/vazio incoerente
        if out_path.exists():
            out_path.unlink()
        print(f"  [{label}] 0 linhas (nenhum arquivo gravado)" + " " * 10)
    else:
        print(f"  [{label}] {total:,} linhas gravadas em {out_path.name}" + " " * 10)
    return total


def extract_year(cursor, year, out_path):
    start = dt.datetime(year, 1, 1)
    end = dt.datetime(year + 1, 1, 1)
    return extract_range(cursor, start, end, out_path, label=str(year))


def extract_month(cursor, year, month, out_path):
    start = dt.datetime(year, month, 1)
    end = dt.datetime(year + 1, 1, 1) if month == 12 else dt.datetime(year, month + 1, 1)
    return extract_range(cursor, start, end, out_path, label=f"{year}-{month:02d}")


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect()
    cursor = conn.cursor()

    hoje = dt.date.today()
    resumo = {}

    # anos fechados: um arquivo por ano (nao mudam mais, nao precisam ser
    # reparticionados)
    for year in range(YEAR_START, YEAR_END):
        out_path = DATA_DIR / f"log_acesso_{year}.parquet"
        print(f"Extraindo ano {year}...")
        resumo[str(year)] = extract_year(cursor, year, out_path)

    # ano corrente: um arquivo por mes, pra permitir atualizar so o mes atual
    # depois (veja scheduler_pausas.py) sem reprocessar o ano inteiro
    ultimo_mes = hoje.month if hoje.year == YEAR_END else 12
    for month in range(1, ultimo_mes + 1):
        out_path = DATA_DIR / f"log_acesso_{YEAR_END}_{month:02d}.parquet"
        print(f"Extraindo {YEAR_END}-{month:02d}...")
        resumo[f"{YEAR_END}-{month:02d}"] = extract_month(cursor, YEAR_END, month, out_path)

    cursor.close()
    conn.close()

    print("\nResumo da extracao:")
    for label, count in resumo.items():
        print(f"  {label}: {count:,} linhas")
    print(f"  TOTAL: {sum(resumo.values()):,} linhas")


if __name__ == "__main__":
    main()
