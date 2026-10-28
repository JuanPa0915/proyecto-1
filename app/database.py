import sqlite3
import os

DB_FILE = 'dfsha.db'

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    
    # Tabla de usuarios
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY,
        username TEXT UNIQUE,
        password_hash TEXT
    )''')
    
    # Tabla de jerarquía (inodes)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS inodes (
        inode_id TEXT PRIMARY KEY,
        user_id TEXT,
        parent_id TEXT,
        name TEXT,
        type TEXT, -- FILE o DIR
        size INTEGER,
        version INTEGER,
        mtime INTEGER,
        UNIQUE(parent_id, name)
    )''')
    
    # Tabla de bloques (manifiesto)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS blocks (
        block_id TEXT PRIMARY KEY,
        inode_id TEXT,
        idx INTEGER,
        size INTEGER,
        sha256 TEXT,
        state TEXT -- ASSIGNED, COMMITTED, RETIRED
    )''')
    
    # Tabla de réplicas
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS replicas (
        block_id TEXT,
        dn_id TEXT,
        PRIMARY KEY (block_id, dn_id)
    )''')
    
    # Tabla de nodos de datos
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS nodes (
        dn_id TEXT PRIMARY KEY,
        url TEXT,
        last_heartbeat INTEGER,
        status TEXT -- ALIVE o DOWN
    )''')
    
    # Handles (Aperturas en curso)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS handles (
        handle_id TEXT PRIMARY KEY,
        user_id TEXT,
        inode_id TEXT,
        mode TEXT,
        base_version INTEGER,
        expires INTEGER
    )''')
    
    # Locks (Bloqueos exclusivos)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS locks (
        lock_id TEXT PRIMARY KEY,
        inode_id TEXT,
        user_id TEXT,
        expires INTEGER
    )''')

    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()
    print("Base de datos SQLite inicializada correctamente.")
