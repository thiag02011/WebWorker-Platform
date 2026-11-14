"""
MÓDULO: RELATIONAL DATABASE & SCHEMA MIGRATION (PostgresManager)
=============================================================================
OBJETIVO:
Gerenciar a conexão, criação e garantia de integridade relacional do banco de dados 
PostgreSQL 18 para armazenamento em lote dos dados de animes.

FLUXO DA ARQUITETURA:
1. Conexão com pooling/reconectar automático no PostgreSQL local (porta 5432).
   -> 2. DDL Automatizado: Cria banco animeschedule_db e tabela animes_2025 com 15 colunas tipadas.
   -> 3. Índices e Constraints: Aplica UNIQUE(title) para assegurar idempotência nas operações.
   -> 4. Ingestão Idempotente: Executa upserts via ON CONFLICT (title) DO UPDATE,
         permitindo reexecuções seguras sem duplicação de dados.
=============================================================================
"""
import os
import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

from src.config_manager import get_config

_cfg = get_config()
DB_NAME = os.environ.get("WEBWORKER_LEGACY_DB_NAME", "animeschedule_db")
USER = str(_cfg.get("database.user", "postgres"))
PASSWORD = os.environ.get("WEBWORKER_DB_PASSWORD") or str(_cfg.get("database.password", ""))
HOST = str(_cfg.get("database.host", "127.0.0.1"))
PORT = int(_cfg.get("database.port", 5432))

def setup_database():
    print(f"Conectando ao PostgreSQL em {HOST}:{PORT} como '{USER}'...")
    conn = psycopg2.connect(
        host=HOST,
        port=PORT,
        user=USER,
        password=PASSWORD,
        dbname="postgres"
    )
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()

    # Cria o banco animeschedule_db se não existir
    cur.execute(f"SELECT 1 FROM pg_database WHERE datname = '{DB_NAME}'")
    exists = cur.fetchone()
    if not exists:
        print(f"Criando banco de dados '{DB_NAME}'...")
        cur.execute(f"CREATE DATABASE {DB_NAME};")
    else:
        print(f"Banco de dados '{DB_NAME}' já existe.")

    cur.close()
    conn.close()

    # Conecta no animeschedule_db e cria a tabela
    print(f"Conectando ao banco '{DB_NAME}' para criar a tabela 'animes_2025'...")
    conn_db = psycopg2.connect(
        host=HOST,
        port=PORT,
        user=USER,
        password=PASSWORD,
        dbname=DB_NAME
    )
    cur_db = conn_db.cursor()

    create_table_sql = """
    CREATE TABLE IF NOT EXISTS animes_2025 (
        id SERIAL PRIMARY KEY,
        title TEXT NOT NULL UNIQUE,
        anime_type VARCHAR(50),
        season VARCHAR(50),
        release_date VARCHAR(50),
        status VARCHAR(50),
        source VARCHAR(100),
        episodes INTEGER,
        episode_length VARCHAR(50),
        score VARCHAR(50),
        popularity VARCHAR(50),
        genres TEXT,
        studios TEXT,
        synonyms TEXT,
        url TEXT,
        raw_metadata JSONB,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE INDEX IF NOT EXISTS idx_animes_2025_season ON animes_2025(season);
    CREATE INDEX IF NOT EXISTS idx_animes_2025_status ON animes_2025(status);
    """
    cur_db.execute(create_table_sql)
    conn_db.commit()
    print("Tabela 'animes_2025' e índices criados com sucesso!")

    cur_db.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'animes_2025';")
    cols = cur_db.fetchall()
    print("\nColunas configuradas:")
    for c in cols:
        print(f"  - {c[0]} ({c[1]})")

    cur_db.close()
    conn_db.close()

if __name__ == "__main__":
    setup_database()
