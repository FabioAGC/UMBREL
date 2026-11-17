"""
Pipeline automatizado do dashboard DIMEP, tudo num arquivo so.

Antes eram 5 scripts que precisavam ser rodados a mao, sempre nessa ordem,
toda vez que havia dado novo: atualizar_mes_atual.py -> calcular_pausas.py ->
enriquecer_pessoas.py -> gerar_dashboard.py (+ validar_parquet.py de vez em
quando, pra aba "Visao geral"). Agora e so:

    python scheduler_pausas.py

...e deixar rodando: a cada poucos minutos ele reextrai o mes corrente de
dbo.LOG_ACESSO, recalcula as pausas dos colaboradores (incremental, com cache
por parquet) e rejunta com o cadastro (dbo.PESSOA). Uma vez por dia tambem
revalida o parquet completo (aba "Visao geral", mais caro por varrer todo o
historico).

O resumo por pessoa e o detalhe de pausas de cada colaborador NAO viram mais
arquivo em disco (pausas_pessoas/<matricula>.json e
pessoas_pausas_resumo.json foram eliminados: eram ~9800 arquivos/500MB+
reescritos a cada ciclo so pra ficar tudo sincronizado pelo OneDrive de novo).
Em vez disso ficam em memoria (`STORE`, protegido por lock) e este mesmo
processo ja sobe um servidor HTTP embutido que responde
/pessoas_pausas_resumo.json e /pausas_pessoas/<matricula>.json direto da
memoria; os demais arquivos (dashboard.html, validacao.json etc.) continuam
sendo servidos estaticamente da pasta. servir_dashboard.py agora so abre o
navegador nesse servidor (nao sobe mais o dele).

NAO faz parte deste arquivo: reextrair o historico completo (2022 em diante)
do zero — isso e raro, use etl_log_acesso.py separadamente.

Para empacotar em .exe (Windows) e colocar na pasta de Inicializacao do
Windows (Win+R -> shell:startup), como e feito no projeto Dash:
    pip install pyinstaller
    pyinstaller --onefile scheduler_pausas.py
"""

import datetime as dt
import json
import logging
import os
import re
import threading
import time
from collections import Counter
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import pyarrow.parquet as pq
import schedule

from db import connect
from etl_log_acesso import extract_month

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
CACHE_PAUSAS_DIR = BASE_DIR / "cache_pausas"
VALIDACAO_PATH = BASE_DIR / "validacao.json"
TEMPLATE_PATH = BASE_DIR / "dashboard_template.html"
DASHBOARD_PATH = BASE_DIR / "dashboard.html"

INTERVALO_RAPIDO_MINUTOS = 5
HORARIO_VALIDACAO_DIARIA = "03:00"
MAX_TENTATIVAS = 3
ESPERA_ENTRE_TENTATIVAS_SEGUNDOS = 60
PORTA_HTTP = 8000

logger = logging.getLogger(__name__)


# =============================================================================
# Store em memoria do resumo/detalhe de pausas por pessoa (substitui
# pausas_pessoas/*.json e pessoas_pausas_resumo.json)
# =============================================================================

class _StorePausas:
    """Guarda em memoria (protegido por lock, ja serializado em bytes pronto
    pra resposta HTTP) tudo que antes ia pra arquivo: resumo e detalhe de
    pausas de cada pessoa (`_gravar_pausas`/`enriquecer_pessoas`) e o
    snapshot de presenca/turno estimado (`calcular_presenca`)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._resumo = []
        self._resumo_bytes = b"[]"
        self._pessoa_bytes = {}
        self._presenca_bytes = b"[]"

    def set_pausas(self, pausas_por_pessoa, nome_por_pessoa):
        pessoa_bytes = {
            int(mat): json.dumps(
                {"matricula": int(mat), "nome": nome_por_pessoa.get(mat), "pausas": list(reversed(pausas))},
                ensure_ascii=False,
            ).encode("utf-8")
            for mat, pausas in pausas_por_pessoa.items()
        }
        with self._lock:
            self._pessoa_bytes = pessoa_bytes

    def add_pessoa_vazia(self, matricula, nome):
        corpo = json.dumps({"matricula": matricula, "nome": nome, "pausas": []}, ensure_ascii=False).encode("utf-8")
        with self._lock:
            self._pessoa_bytes[int(matricula)] = corpo

    def set_resumo(self, resumo):
        corpo = json.dumps(resumo, ensure_ascii=False).encode("utf-8")
        with self._lock:
            self._resumo = resumo
            self._resumo_bytes = corpo

    def get_resumo(self):
        with self._lock:
            return self._resumo

    def get_resumo_bytes(self):
        with self._lock:
            return self._resumo_bytes

    def get_pessoa_bytes(self, matricula):
        with self._lock:
            return self._pessoa_bytes.get(matricula)

    def set_presenca(self, presenca):
        corpo = json.dumps(presenca, ensure_ascii=False).encode("utf-8")
        with self._lock:
            self._presenca_bytes = corpo

    def get_presenca_bytes(self):
        with self._lock:
            return self._presenca_bytes


STORE = _StorePausas()


# =============================================================================
# Utilitarios: logging, retry de job e escrita resiliente a OneDrive
# =============================================================================

def configurar_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=5, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def executar_com_retry(nome, func):
    inicio = time.perf_counter()
    for tentativa in range(1, MAX_TENTATIVAS + 1):
        try:
            resultado = func()
            logger.info(
                "%s concluído com sucesso na tentativa %s/%s. Resultado: %s (%.2fs)",
                nome, tentativa, MAX_TENTATIVAS, resultado, time.perf_counter() - inicio,
            )
            return
        except Exception:
            logger.exception("%s falhou na tentativa %s/%s", nome, tentativa, MAX_TENTATIVAS)
            if tentativa < MAX_TENTATIVAS:
                logger.info("Aguardando %ss antes da próxima tentativa", ESPERA_ENTRE_TENTATIVAS_SEGUNDOS)
                time.sleep(ESPERA_ENTRE_TENTATIVAS_SEGUNDOS)
    logger.error(
        "%s falhou após %s tentativas (%.2fs decorridos)", nome, MAX_TENTATIVAS, time.perf_counter() - inicio
    )


def escrever_com_retry(path: Path, texto: str, tentativas: int = 5, espera: float = 0.5) -> None:
    """OneDrive as vezes trava um arquivo por uma fracao de segundo durante a
    sincronizacao (PermissionError). Tenta de novo antes de desistir."""
    for tentativa in range(1, tentativas + 1):
        try:
            path.write_text(texto, encoding="utf-8")
            return
        except PermissionError:
            if tentativa == tentativas:
                raise
            time.sleep(espera)


# =============================================================================
# Passo 1 (a cada 5 min): reextrai o mes corrente de dbo.LOG_ACESSO
# =============================================================================

def atualizar_mes_atual():
    hoje = dt.date.today()
    year, month = hoje.year, hoje.month
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / f"log_acesso_{year}_{month:02d}.parquet"

    conn = connect()
    cursor = conn.cursor()
    total = extract_month(cursor, year, month, out_path)
    cursor.close()
    conn.close()
    return f"{total:,} linhas em {out_path.name}"


# =============================================================================
# Passo 2 (a cada 5 min): calcula pausas (saida/volta no mesmo dia por
# colaborador), incremental com cache por arquivo parquet
# =============================================================================

CONFIG_VERSION_PAUSAS = "v3-sem-0min"

COLS_PAUSAS = ["NU_MATRICULA", "NM_PESSOA", "DT_REQUISICAO", "CD_AREA_ORIGEM", "CD_AREA_DESTINO"]

POSTO_TRABALHO = 1  # "Interna"
POSTOS_PAUSA = {
    2: "Externa (fábrica)",
    3: "Refeitório",
    5: "Externa PCD",
    6: "Interna PCD",
    7: "Externa Parking Portão",
    8: "Interna Parking Portão",
    10: "Interna Parking Cancela",
    11: "Café Farofa",
    12: "Café ACS",
    13: "Café Snacks",
    14: "Café Drymix",
    15: "Interna ACS",
    16: "Ambulatório",
    17: "Amendoim",
    18: "Batata",
    19: "Dry Inferior",
    20: "Dry Superior",
    21: "Farofa Inferior",
    22: "Farofa Superior",
    23: "Microondas",
    24: "Snacks",
}


def _carregar_eventos_pausas(f):
    table = pq.read_table(f, columns=COLS_PAUSAS)
    df = table.to_pandas()
    df = df.dropna(subset=["NU_MATRICULA", "CD_AREA_ORIGEM", "CD_AREA_DESTINO"])
    df["NU_MATRICULA"] = df["NU_MATRICULA"].astype("int64")
    df["CD_AREA_ORIGEM"] = df["CD_AREA_ORIGEM"].astype("int32")
    df["CD_AREA_DESTINO"] = df["CD_AREA_DESTINO"].astype("int32")

    saida = (df["CD_AREA_ORIGEM"] == POSTO_TRABALHO) & (df["CD_AREA_DESTINO"].isin(POSTOS_PAUSA))
    volta = (df["CD_AREA_ORIGEM"].isin(POSTOS_PAUSA)) & (df["CD_AREA_DESTINO"] == POSTO_TRABALHO)
    df = df[saida | volta].copy()
    df["TIPO"] = pd.Series("SAIDA", index=df.index).where(saida[saida | volta], "VOLTA")
    df["POSTO"] = df["CD_AREA_DESTINO"].where(df["TIPO"] == "SAIDA", df["CD_AREA_ORIGEM"])

    df.sort_values(["NU_MATRICULA", "DT_REQUISICAO"], inplace=True, kind="mergesort")
    df.reset_index(drop=True, inplace=True)
    return df


def _processar_pausas(eventos):
    matriculas = eventos["NU_MATRICULA"].to_numpy()
    nomes = eventos["NM_PESSOA"].to_numpy()
    dts = eventos["DT_REQUISICAO"].to_numpy()
    tipos = eventos["TIPO"].to_numpy(dtype=object)
    postos = eventos["POSTO"].to_numpy()

    n = len(eventos)
    pausas_por_pessoa = {}
    nome_por_pessoa = {}
    abertas_nao_mesmo_dia = 0

    cur_mat = None
    pendente = None  # (dt_saida, posto_cod)
    zeradas = 0

    for i in range(n):
        mat = matriculas[i]
        if mat != cur_mat:
            cur_mat = mat
            pendente = None
            if mat not in nome_por_pessoa and nomes[i] is not None:
                nome_por_pessoa[mat] = nomes[i]

        ts = pd.Timestamp(dts[i])
        tipo = tipos[i]

        if tipo == "SAIDA":
            pendente = (ts, int(postos[i]))
        elif tipo == "VOLTA" and pendente is not None:
            dt_saida, posto_cod = pendente
            if ts.date() == dt_saida.date():
                duracao_min = round(float((ts - dt_saida) / pd.Timedelta(minutes=1)), 1)
                if duracao_min == 0:
                    # leitura dupla no mesmo instante (mesmo minuto) — ruido do
                    # equipamento, nao e uma saida de verdade
                    zeradas += 1
                else:
                    pausas_por_pessoa.setdefault(mat, []).append(
                        {
                            "data": str(dt_saida.date()),
                            "saida": str(dt_saida),
                            "volta": str(ts),
                            "duracao_min": duracao_min,
                            "posto": POSTOS_PAUSA.get(posto_cod, str(posto_cod)),
                        }
                    )
            else:
                abertas_nao_mesmo_dia += 1
            pendente = None

        if i % 2_000_000 == 0:
            print(f"  processado {i:,}/{n:,}")

    print(f"  saidas sem volta no mesmo dia (descartadas): {abertas_nao_mesmo_dia:,}")
    print(f"  saidas de 0 minutos descartadas (leitura duplicada): {zeradas:,}")
    return pausas_por_pessoa, nome_por_pessoa


def _gravar_pausas(pausas_por_pessoa, nome_por_pessoa):
    """Publica o resumo e o detalhe de pausas de cada pessoa em `STORE`
    (memoria) em vez de escrever pausas_pessoas/<matricula>.json — ver
    docstring do modulo."""
    resumo = []

    for mat, pausas in pausas_por_pessoa.items():
        nome = nome_por_pessoa.get(mat)
        duracoes = [p["duracao_min"] for p in pausas]
        por_posto = {}
        for p in pausas:
            por_posto[p["posto"]] = por_posto.get(p["posto"], 0) + 1

        por_mes = {}
        for p in pausas:
            mes = p["data"][:7]
            acc = por_mes.setdefault(mes, [0, 0.0])
            acc[0] += 1
            acc[1] += p["duracao_min"]
        meses_ordenados = sorted(por_mes)

        resumo.append(
            {
                "matricula": int(mat),
                "nome": nome,
                "qtd_pausas": len(pausas),
                "tempo_total_min": round(sum(duracoes), 1),
                "tempo_medio_min": round(sum(duracoes) / len(duracoes), 1) if duracoes else 0,
                "tempo_max_min": round(max(duracoes), 1) if duracoes else 0,
                "por_posto": por_posto,
                "meses": meses_ordenados,
                "qtd_por_mes": [por_mes[m][0] for m in meses_ordenados],
                "tempo_por_mes": [round(por_mes[m][1], 1) for m in meses_ordenados],
            }
        )

    resumo.sort(key=lambda r: -r["tempo_total_min"])
    STORE.set_pausas(pausas_por_pessoa, nome_por_pessoa)
    STORE.set_resumo(resumo)
    return resumo


VERSAO_CACHE_PAUSAS_PATH = CACHE_PAUSAS_DIR / "_versao.txt"


def _garantir_versao_cache_pausas():
    """Se a regra de calculo mudou (CONFIG_VERSION_PAUSAS), limpa o cache
    inteiro de uma vez. Guardar a versao num arquivo separado evita ter que
    abrir/parsear cada cache so pra checar isso — os .json de ano podem ter
    100+MB."""
    CACHE_PAUSAS_DIR.mkdir(parents=True, exist_ok=True)
    if not VERSAO_CACHE_PAUSAS_PATH.exists():
        escrever_com_retry(VERSAO_CACHE_PAUSAS_PATH, CONFIG_VERSION_PAUSAS)
        return
    if VERSAO_CACHE_PAUSAS_PATH.read_text(encoding="utf-8").strip() == CONFIG_VERSION_PAUSAS:
        return
    removidos = 0
    for cache_file in CACHE_PAUSAS_DIR.glob("*.json"):
        cache_file.unlink()
        removidos += 1
    logger.info("Regra de cálculo de pausas mudou — %s cache(s) invalidado(s)", removidos)
    escrever_com_retry(VERSAO_CACHE_PAUSAS_PATH, CONFIG_VERSION_PAUSAS)


def _cache_pausas_valido(f, cache_path):
    if not cache_path.exists():
        return False
    return cache_path.stat().st_mtime >= f.stat().st_mtime


def _processar_arquivo_pausas(f):
    """Processa (ou le do cache) um unico parquet e devolve as pausas parciais
    daquele arquivo: (pausas_por_pessoa, nome_por_pessoa)."""
    cache_path = CACHE_PAUSAS_DIR / f"{f.stem}.json"

    if _cache_pausas_valido(f, cache_path):
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        pausas = {int(mat): lista for mat, lista in cache["pausas"].items()}
        nomes = {int(mat): nome for mat, nome in cache["nomes"].items()}
        return pausas, nomes

    eventos = _carregar_eventos_pausas(f)
    print(f"  {f.name}: {len(eventos):,} eventos de pausa (saida/volta) — recalculando")
    pausas, nomes = _processar_pausas(eventos)

    escrever_com_retry(
        cache_path,
        json.dumps(
            {
                "pausas": {str(mat): lista for mat, lista in pausas.items()},
                "nomes": {str(mat): nome for mat, nome in nomes.items()},
            },
            ensure_ascii=False,
        ),
    )
    return pausas, nomes


def calcular_pausas():
    _garantir_versao_cache_pausas()
    arquivos = sorted(DATA_DIR.glob("log_acesso_*.parquet"))

    pausas_por_pessoa = {}
    nome_por_pessoa = {}
    for f in arquivos:
        pausas_arq, nomes_arq = _processar_arquivo_pausas(f)
        for mat, lista in pausas_arq.items():
            pausas_por_pessoa.setdefault(mat, []).extend(lista)
        for mat, nome in nomes_arq.items():
            nome_por_pessoa.setdefault(mat, nome)

    total_pausas = sum(len(v) for v in pausas_por_pessoa.values())

    # dentro de cada pessoa, reordena por data (arquivos podem ter sido
    # mesclados fora de ordem cronologica quando so um foi reprocessado)
    for mat, lista in pausas_por_pessoa.items():
        lista.sort(key=lambda p: p["saida"])

    resumo = _gravar_pausas(pausas_por_pessoa, nome_por_pessoa)
    return f"{len(resumo):,} pessoas, {total_pausas:,} pausas fechadas"


# =============================================================================
# Passo 3 (a cada 5 min): enriquece o resumo com o cadastro (dbo.PESSOA)
# =============================================================================

STATUS_ATIVO = {"Acesso Liberado", "Acesso Múltiplo"}


def _carregar_cadastro():
    conn = connect()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT p.NU_MATRICULA, p.NM_PESSOA, s.DS_SITUACAO_PESSOA
        FROM dbo.PESSOA p
        LEFT JOIN dbo.SITUACAO_PESSOA s ON s.CD_SITUACAO_PESSOA = p.CD_SITUACAO_PESSOA
        """
    )
    cadastro = {}
    for matricula, nome, situacao in cur.fetchall():
        if matricula is None:
            continue
        cadastro[int(matricula)] = {"nome": nome, "situacao": situacao}
    cur.close()
    conn.close()
    return cadastro


def enriquecer_pessoas():
    resumo = STORE.get_resumo()
    cadastro = _carregar_cadastro()

    matriculas_no_resumo = set()
    for p in resumo:
        info = cadastro.get(p["matricula"])
        if info:
            p["situacao"] = info["situacao"]
            if not p["nome"] and info["nome"]:
                p["nome"] = info["nome"]
        else:
            p["situacao"] = None
        matriculas_no_resumo.add(p["matricula"])

    adicionados = 0
    for matricula, info in cadastro.items():
        if matricula in matriculas_no_resumo:
            continue
        if info["situacao"] not in STATUS_ATIVO:
            continue
        resumo.append(
            {
                "matricula": matricula,
                "nome": info["nome"],
                "qtd_pausas": 0,
                "tempo_total_min": 0,
                "tempo_medio_min": 0,
                "tempo_max_min": 0,
                "por_posto": {},
                "meses": [],
                "qtd_por_mes": [],
                "tempo_por_mes": [],
                "situacao": info["situacao"],
            }
        )
        STORE.add_pessoa_vazia(matricula, info["nome"])
        adicionados += 1

    resumo.sort(key=lambda r: -r["tempo_total_min"])
    STORE.set_resumo(resumo)
    return f"{len(resumo):,} pessoas no resumo ({adicionados} ativas sem pausa adicionadas)"


# =============================================================================
# Passo extra (a cada 5 min): presenca atual (dentro/fora da empresa agora) e
# turno estimado por pessoa, pra aba "Presenca"
# =============================================================================

COLS_PRESENCA = ["NU_MATRICULA", "NM_PESSOA", "DT_REQUISICAO", "DS_AREA_ORIGEM", "DS_AREA_DESTINO", "DS_MOVIMENTO", "NM_ESTRUTURA"]

# quantos meses de historico usar pra estimar o turno usual de cada pessoa
# (nao existe vinculo pessoa->turno confiavel no banco do DIMEP — testado
# PESSOA.CD_PERFIL_ACESSO->PERFIL_ACESSO_REGRA->TURNO, GRUPO_PESSOA e
# REGRA_PESSOA->PERFIL_ACESSO_REGRA->TURNO, nenhum cobre a base de pessoas
# ativas. Por isso o turno aqui e estimado pelo horario historico da
# primeira entrada do dia, nao e a escala oficial)
MESES_JANELA_TURNO = 3

AREAS_FABRICA = {
    "Interna Amendoim", "Interna Batata", "Interna Dry Inferior", "Interna Dry Superior",
    "Interna Farofa Inferior", "Interna Farofa Superior", "Interna Microondas", "Interna Snacks",
}
AREA_REFEITORIO = "Refeitório"


def _classificar_area_atual(area_destino):
    if not area_destino or area_destino.startswith("Externa"):
        return "fora"
    if area_destino == AREA_REFEITORIO:
        return "refeitorio"
    if area_destino in AREAS_FABRICA:
        return "fabrica"
    return "apoio"  # Interna generica, cafes, ambulatorio, parking, ACS, PCD


def _turno_por_hora(hora):
    """Bucket de turno pela hora usual da 1a entrada do dia. Limites nos
    pontos medios entre os horarios tipicos de inicio de cada turno
    (1o ~06h, comercial ~08h, 2o ~14h, 3o ~22h)."""
    if 2 <= hora < 7:
        return "1° Turno"
    if 7 <= hora < 11:
        return "Turno Comercial"
    if 11 <= hora < 18:
        return "2° Turno"
    return "3° Turno"  # 18h-23h ou 0h-1h59


def _arquivos_janela_meses(n_meses):
    """Ultimos N meses de log_acesso_*.parquet (mes corrente + anteriores),
    em ordem cronologica. Usa o arquivo anual se o mes cair num ano fechado."""
    hoje = dt.date.today()
    metas = []
    ano, mes = hoje.year, hoje.month
    for _ in range(n_meses):
        metas.append((ano, mes))
        ano, mes = (ano - 1, 12) if mes == 1 else (ano, mes - 1)

    arquivos = []
    for ano, mes in reversed(metas):
        for candidato in (DATA_DIR / f"log_acesso_{ano}_{mes:02d}.parquet", DATA_DIR / f"log_acesso_{ano}.parquet"):
            if candidato.exists() and candidato not in arquivos:
                arquivos.append(candidato)
    return arquivos


def _carregar_eventos_presenca(f):
    table = pq.read_table(f, columns=COLS_PRESENCA)
    df = table.to_pandas()
    df = df.dropna(subset=["NU_MATRICULA", "DT_REQUISICAO"])
    df["NU_MATRICULA"] = df["NU_MATRICULA"].astype("int64")
    return df


def _ultimo_evento_por_pessoa(df):
    df = df.sort_values(["NU_MATRICULA", "DT_REQUISICAO"], kind="mergesort")
    return df.groupby("NU_MATRICULA", sort=False).tail(1)[
        ["NU_MATRICULA", "NM_PESSOA", "DT_REQUISICAO", "DS_AREA_DESTINO", "NM_ESTRUTURA"]
    ]


def _primeira_entrada_do_dia(df):
    entradas = df[(df["DS_MOVIMENTO"] == "ENTRADA") & df["DS_AREA_ORIGEM"].str.startswith("Externa", na=False)]
    if entradas.empty:
        return entradas[["NU_MATRICULA", "DT_REQUISICAO"]]
    entradas = entradas.copy()
    entradas["DIA"] = entradas["DT_REQUISICAO"].dt.date
    return (
        entradas.sort_values("DT_REQUISICAO", kind="mergesort")
        .groupby(["NU_MATRICULA", "DIA"], sort=False)
        .head(1)[["NU_MATRICULA", "DT_REQUISICAO"]]
    )


def calcular_presenca():
    """Publica em `STORE` (aba Presenca): status atual de cada pessoa (dentro
    — fabrica/refeitorio/apoio — ou fora) a partir do ultimo evento dela hoje,
    e o turno estimado a partir do padrao historico de entrada."""
    arquivos = _arquivos_janela_meses(MESES_JANELA_TURNO)
    if not arquivos:
        return "sem arquivos de log_acesso"

    candidatos_ultimo = []
    primeiras_entradas = []
    for f in arquivos:
        df = _carregar_eventos_presenca(f)
        if df.empty:
            continue
        candidatos_ultimo.append(_ultimo_evento_por_pessoa(df))
        primeiras_entradas.append(_primeira_entrada_do_dia(df))

    if not candidatos_ultimo:
        return "sem eventos na janela"

    ultimo_evento = (
        pd.concat(candidatos_ultimo, ignore_index=True)
        .sort_values("DT_REQUISICAO", kind="mergesort")
        .groupby("NU_MATRICULA", sort=False)
        .tail(1)
        .set_index("NU_MATRICULA")
    )

    hora_usual = {}
    todas_entradas = pd.concat(primeiras_entradas, ignore_index=True) if primeiras_entradas else pd.DataFrame()
    if not todas_entradas.empty:
        horas = todas_entradas.groupby("NU_MATRICULA")["DT_REQUISICAO"].apply(lambda s: int(s.dt.hour.mode().iloc[0]))
        hora_usual = horas.to_dict()

    hoje = dt.date.today()
    presenca = []
    for mat, row in ultimo_evento.iterrows():
        acessou_hoje = row["DT_REQUISICAO"].date() == hoje
        hora = hora_usual.get(mat)
        presenca.append(
            {
                "matricula": int(mat),
                "nome": row["NM_PESSOA"],
                "estrutura": row["NM_ESTRUTURA"],
                "status": _classificar_area_atual(row["DS_AREA_DESTINO"]) if acessou_hoje else "fora",
                "acessou_hoje": bool(acessou_hoje),
                "turno": _turno_por_hora(hora) if hora is not None else "Sem turno estimado",
                "ultimo_evento": str(row["DT_REQUISICAO"]),
            }
        )

    STORE.set_presenca(presenca)
    dentro = sum(1 for p in presenca if p["status"] != "fora")
    return f"{len(presenca):,} pessoas com evento na janela ({MESES_JANELA_TURNO} meses), {dentro:,} presentes agora"


# =============================================================================
# Passo 4 (1x/dia, 03:00): revalida o parquet completo -> aba "Visao geral"
# =============================================================================

COLS_VALIDACAO = [
    "NU_MATRICULA",
    "NM_PESSOA",
    "DS_AREA_ORIGEM",
    "DS_AREA_DESTINO",
    "DS_MOVIMENTO",
    "NM_ESTRUTURA",
    "DT_REQUISICAO",
]


def validar_parquet():
    files = sorted(DATA_DIR.glob("log_acesso_*.parquet"))
    if not files:
        raise RuntimeError(f"Nenhum parquet encontrado em {DATA_DIR}")

    arquivos_info = []
    total_linhas = 0
    nulos = Counter()
    movimento_counter = Counter()
    area_movimento_counter = Counter()  # (area, movimento) -> count
    estrutura_counter = Counter()
    pessoas = set()
    dt_min, dt_max = None, None
    amostra = []

    for f in files:
        pf = pq.ParquetFile(f)
        n_rows = pf.metadata.num_rows
        total_linhas += n_rows
        arquivos_info.append(
            {"arquivo": f.name, "linhas": n_rows, "tamanho_mb": round(f.stat().st_size / (1024 * 1024), 2)}
        )

        table = pf.read(columns=COLS_VALIDACAO)
        df = table.to_pandas()

        for col in COLS_VALIDACAO:
            nulos[col] += int(df[col].isna().sum())

        movimento_counter.update(df["DS_MOVIMENTO"].value_counts().to_dict())
        estrutura_counter.update(df["NM_ESTRUTURA"].value_counts().to_dict())

        for (origem, destino, mov), qtd in (
            df.groupby(["DS_AREA_ORIGEM", "DS_AREA_DESTINO", "DS_MOVIMENTO"]).size().items()
        ):
            area_movimento_counter[(origem, destino, mov)] += qtd

        pessoas.update(df["NU_MATRICULA"].dropna().unique().tolist())

        file_min = df["DT_REQUISICAO"].min()
        file_max = df["DT_REQUISICAO"].max()
        if dt_min is None or file_min < dt_min:
            dt_min = file_min
        if dt_max is None or file_max > dt_max:
            dt_max = file_max

        if len(amostra) < 10:
            amostra_cols = ["NU_MATRICULA", "NM_PESSOA", "DS_AREA_ORIGEM", "DS_AREA_DESTINO", "DS_MOVIMENTO", "DT_REQUISICAO"]
            faltam = 10 - len(amostra)
            for _, row in df[amostra_cols].head(faltam).iterrows():
                amostra.append(
                    {
                        "matricula": None if pd.isna(row["NU_MATRICULA"]) else int(row["NU_MATRICULA"]),
                        "pessoa": row["NM_PESSOA"],
                        "area_origem": row["DS_AREA_ORIGEM"],
                        "area_destino": row["DS_AREA_DESTINO"],
                        "movimento": row["DS_MOVIMENTO"],
                        "data_hora": str(row["DT_REQUISICAO"]),
                    }
                )

        print(f"{f.name}: {n_rows:,} linhas processadas")

    top_transicoes = [
        {"area_origem": o, "area_destino": d, "movimento": m, "quantidade": q}
        for (o, d, m), q in sorted(area_movimento_counter.items(), key=lambda kv: -kv[1])[:20]
    ]
    top_estruturas = [
        {"estrutura": k, "quantidade": v}
        for k, v in sorted(estrutura_counter.items(), key=lambda kv: -kv[1])[:15]
    ]

    resumo = {
        "gerado_em": dt.datetime.now().isoformat(timespec="seconds"),
        "arquivos": arquivos_info,
        "total_linhas": total_linhas,
        "total_pessoas_distintas": len(pessoas),
        "periodo": {
            "inicio": str(dt_min) if dt_min is not None else None,
            "fim": str(dt_max) if dt_max is not None else None,
        },
        "nulos_por_coluna": dict(nulos),
        "movimentos": dict(movimento_counter),
        "top_transicoes_area": top_transicoes,
        "top_estruturas": top_estruturas,
        "amostra": amostra,
    }

    escrever_com_retry(VALIDACAO_PATH, json.dumps(resumo, ensure_ascii=False, indent=2))
    return f"{total_linhas:,} linhas, {len(pessoas):,} pessoas, período {dt_min} a {dt_max}"


# =============================================================================
# Passo 5 (a cada 5 min): publica dashboard.html a partir do template
# =============================================================================

def gerar_dashboard():
    escrever_com_retry(DASHBOARD_PATH, TEMPLATE_PATH.read_text(encoding="utf-8"))
    return f"gravado em {DASHBOARD_PATH.name}"


# =============================================================================
# Servidor HTTP embutido: serve o dashboard e, direto da memoria (STORE),
# /pessoas_pausas_resumo.json e /pausas_pessoas/<matricula>.json
# =============================================================================

_RE_PESSOA = re.compile(r"^/pausas_pessoas/(\d+)\.json$")


class DashboardHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        caminho = urlparse(self.path).path

        if caminho == "/pessoas_pausas_resumo.json":
            self._responder_json(STORE.get_resumo_bytes())
            return

        if caminho == "/presenca.json":
            self._responder_json(STORE.get_presenca_bytes())
            return

        m = _RE_PESSOA.match(caminho)
        if m:
            corpo = STORE.get_pessoa_bytes(int(m.group(1)))
            if corpo is None:
                self.send_error(404, "Pessoa nao encontrada")
            else:
                self._responder_json(corpo)
            return

        # tudo o resto (dashboard.html, validacao.json, assets) continua
        # sendo servido como arquivo estatico normal da pasta do projeto
        super().do_GET()

    def _responder_json(self, corpo: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(corpo)

    def log_message(self, format, *args):  # noqa: A002 - assinatura da classe base
        pass  # evita poluir o log do scheduler com toda requisicao HTTP


def iniciar_servidor_http(porta: int) -> HTTPServer:
    handler = partial(DashboardHandler, directory=str(BASE_DIR))
    # no container: DIMEP_HOST=0.0.0.0 (o proxy filtra o que pode sair)
    servidor = HTTPServer((os.getenv("DIMEP_HOST", "127.0.0.1"), porta), handler)
    thread = threading.Thread(target=servidor.serve_forever, daemon=True)
    thread.start()
    logger.info("Servidor HTTP do dashboard em http://127.0.0.1:%s/dashboard.html", porta)
    return servidor


# =============================================================================
# Orquestracao / agendamento
# =============================================================================

def job_rapido():
    logger.info("Iniciando atualização do dashboard (disparado em %s)", dt.datetime.now())
    executar_com_retry("Atualizar mês corrente", atualizar_mes_atual)
    executar_com_retry("Calcular pausas", calcular_pausas)
    executar_com_retry("Enriquecer pessoas", enriquecer_pessoas)
    executar_com_retry("Calcular presença/turno", calcular_presenca)
    executar_com_retry("Gerar dashboard.html", gerar_dashboard)
    _logar_proxima_execucao()


def job_diario():
    logger.info("Iniciando validação completa do parquet (disparado em %s)", dt.datetime.now())
    executar_com_retry("Validar parquet (visão geral)", validar_parquet)


def _logar_proxima_execucao():
    proxima = schedule.next_run()
    if proxima:
        logger.info("Próxima execução agendada: %s", proxima)


def main():
    configurar_logging(BASE_DIR / "logs" / "scheduler_pausas.log")
    schedule.every(INTERVALO_RAPIDO_MINUTOS).minutes.do(job_rapido)
    schedule.every().day.at(HORARIO_VALIDACAO_DIARIA).do(job_diario)
    logger.info(
        "Scheduler do dashboard DIMEP iniciado. Atualização rápida a cada %s min; validação completa às %s.",
        INTERVALO_RAPIDO_MINUTOS,
        HORARIO_VALIDACAO_DIARIA,
    )

    # roda uma vez ao subir, para garantir que o dashboard exista mesmo se a
    # maquina ficar ligada o dia todo sem reiniciar
    job_rapido()

    # so sobe o servidor depois do primeiro job_rapido(), pra nao responder
    # pausas_pessoas/resumo vazios antes do primeiro calculo terminar
    iniciar_servidor_http(PORTA_HTTP)

    while True:
        schedule.run_pending()
        time.sleep(10)


if __name__ == "__main__":
    main()
