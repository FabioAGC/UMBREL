"""Conexão com o banco dbo (DIMEP/DMPACESSOII) via pyodbc.

Credenciais vêm de .env (nunca hardcoded — esse banco guarda dados de
acesso/RH reais). Veja .env.example para o formato esperado.
"""
import os
from pathlib import Path

import pyodbc
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(dotenv_path=BASE_DIR / ".env")

DB_DRIVER = os.getenv("DB_DRIVER", "SQL Server")
DB_SERVER = os.getenv("DB_SERVER")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")


def connect() -> pyodbc.Connection:
    if not DB_SERVER or not DB_NAME or not DB_USER or not DB_PASSWORD:
        raise ValueError(
            "DB_SERVER, DB_NAME, DB_USER e DB_PASSWORD precisam estar definidos no .env "
            f"(procurado em {BASE_DIR / '.env'})"
        )

    conn_str = (
        f"DRIVER={{{DB_DRIVER}}};"
        f"SERVER={DB_SERVER};"
        f"DATABASE={DB_NAME};"
        f"UID={DB_USER};"
        f"PWD={DB_PASSWORD};"
        # No Linux (ODBC Driver 18) a criptografia é obrigatória por padrão; ex.: "Encrypt=yes;TrustServerCertificate=yes;"
        f"{os.getenv('DB_EXTRA', '')}"
    )
    conn = pyodbc.connect(conn_str)
    conn.setdecoding(pyodbc.SQL_CHAR, encoding="latin1")
    conn.setdecoding(pyodbc.SQL_WCHAR, encoding="utf-16le")
    conn.setencoding(encoding="utf-8")
    return conn
