import os
import sqlite3
import datetime
from fastapi import FastAPI, Depends, HTTPException, status
from pydantic import BaseModel
from passlib.context import CryptContext
import jwt
from fastapi.security import OAuth2PasswordBearer
import itertools

app = FastAPI(title="MDS DFSha Fase II - Bloques")

SECRET_KEY = "secreto-fase2-dfsha"
ALGORITHM = "HS256"
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

DB_FILE = "dfsha.db"

# Nodos harcodeados por requerimiento
NODES = [
    "http://172.31.43.252:8001",
    "http://172.31.43.252:8002",
    "http://172.31.43.252:8003"
]

def get_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, password_hash TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS active_nodes (node_url TEXT PRIMARY KEY, last_seen TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS file_locks (path TEXT PRIMARY KEY, username TEXT, locked_at TIMESTAMP)''')
    # Tabla metadata 
    c.execute('''CREATE TABLE IF NOT EXISTS file_metadata (path TEXT PRIMARY KEY, size INTEGER, checksum TEXT, uploaded_by TEXT, uploaded_at TIMESTAMP)''')
    # Tabla manifest para mapear archivo -> bloques -> nodos
    c.execute('''CREATE TABLE IF NOT EXISTS file_manifest (path TEXT, block_index INTEGER, block_id TEXT, checksum TEXT, node_url TEXT)''')
    
    # Usuario por defecto admin/admin123
    c.execute("SELECT * FROM users WHERE username='admin'")
    if not c.fetchone():
        hashed = pwd_context.hash("admin123")
        c.execute("INSERT INTO users VALUES (?, ?)", ("admin", hashed))
    conn.commit()
    conn.close()

init_db()

class LoginData(BaseModel):
    username: str
    password: str

class PathRequest(BaseModel):
    path: str

class AssignRequest(BaseModel):
    path: str
    blocks: int

class BlockInfo(BaseModel):
    block_id: str
    checksum: str
    nodes: list[str]

class CommitRequest(BaseModel):
    path: str
    checksum: str
    blocks: list[BlockInfo]
    size: int

def verify_token(token: str = Depends(oauth2_scheme)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload.get("sub")
    except:
        raise HTTPException(status_code=401, detail="Token invalido o expirado")

def get_active_nodes():
    conn = get_db()
    c = conn.cursor()
    limit = datetime.datetime.now() - datetime.timedelta(seconds=12) # Ajuste: 12 segundos
    c.execute("SELECT node_url FROM active_nodes WHERE last_seen > ?", (limit,))
    nodes = [row["node_url"] for row in c.fetchall()]
    conn.close()
    return nodes

@app.post("/login")
def login(data: LoginData):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT password_hash FROM users WHERE username=?", (data.username,))
    user = c.fetchone()
    conn.close()
    if user and pwd_context.verify(data.password, user["password_hash"]):
        token = jwt.encode({"sub": data.username, "exp": datetime.datetime.now() + datetime.timedelta(hours=2)}, SECRET_KEY, algorithm=ALGORITHM)
        return {"access_token": token, "token_type": "bearer"}
    raise HTTPException(status_code=401, detail="Credenciales incorrectas")

@app.post("/heartbeat")
def heartbeat(node_url: str):
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO active_nodes VALUES (?, ?)", (node_url, datetime.datetime.now()))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.post("/lock")
def lock_file(req: PathRequest, username: str = Depends(verify_token)):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT username FROM file_locks WHERE path=?", (req.path,))
    lock = c.fetchone()
    if lock and lock["username"] != username:
        conn.close()
        raise HTTPException(status_code=423, detail="Bloqueado por otro proceso")
    
    c.execute("INSERT OR REPLACE INTO file_locks VALUES (?, ?, ?)", (req.path, username, datetime.datetime.now()))
    conn.commit()
    conn.close()
    return {"message": "Lock adquirido"}

@app.post("/unlock")
def unlock_file(req: PathRequest, username: str = Depends(verify_token)):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM file_locks WHERE path=? AND username=?", (req.path, username))
    conn.commit()
    conn.close()
    return {"message": "Lock liberado"}

@app.post("/assign")
def assign_blocks(req: AssignRequest, username: str = Depends(verify_token)):
    active = get_active_nodes()
    if len(active) < 2:
        raise HTTPException(status_code=503, detail="Insuficientes nodos activos para replicacion (minimo 2)")
    
    # Rotacion (Round-Robin) para distribuir carga
    assignments = []
    pool = itertools.cycle(active)
    
    for i in range(req.blocks):
        replica1 = next(pool)
        replica2 = next(pool)
        while replica1 == replica2:
            replica2 = next(pool)
        assignments.append({"block_index": i, "nodes": [replica1, replica2]})

    return {"assignments": assignments}

@app.post("/commit")
def commit_upload(req: CommitRequest, username: str = Depends(verify_token)):
    conn = get_db()
    c = conn.cursor()
    
    # Solo puede confirmar quien tiene el lock
    c.execute("SELECT username FROM file_locks WHERE path=?", (req.path,))
    lock = c.fetchone()
    if not lock or lock["username"] != username:
         conn.close()
         raise HTTPException(status_code=403, detail="No tienes el bloqueo para confirmar este archivo")

    # Limpiar metadata y bloques viejos si el archivo ya existia
    c.execute("DELETE FROM file_metadata WHERE path=?", (req.path,))
    c.execute("DELETE FROM file_manifest WHERE path=?", (req.path,))
    
    c.execute("INSERT INTO file_metadata VALUES (?, ?, ?, ?, ?)", 
              (req.path, req.size, req.checksum, username, datetime.datetime.now()))
    
    for idx, block in enumerate(req.blocks):
        unique_nodes = list(set(block.nodes))
        if len(unique_nodes) < 2:
            conn.rollback()
            conn.close()
            raise HTTPException(status_code=400, detail=f"El bloque {block.block_id} no cumple la replicacion requerida (tiene {len(unique_nodes)} replicas)")
        for node in unique_nodes:
            c.execute("INSERT INTO file_manifest VALUES (?, ?, ?, ?, ?)",
                      (req.path, idx, block.block_id, block.checksum, node))
            
    conn.commit()
    conn.close()
    return {"message": "Transaccion finalizada"}

@app.get("/file_info")
def get_file_info(path: str, username: str = Depends(verify_token)):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM file_metadata WHERE path=?", (path,))
    meta = c.fetchone()
    if not meta:
        conn.close()
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    
    c.execute("SELECT block_id, checksum, node_url FROM file_manifest WHERE path=? ORDER BY block_index", (path,))
    manifest_rows = c.fetchall()
    conn.close()
    
    # Agrupar nodos por bloque
    blocks_dict = {}
    for row in manifest_rows:
        bid = row["block_id"]
        if bid not in blocks_dict:
            blocks_dict[bid] = {"block_id": bid, "checksum": row["checksum"], "nodes": []}
        blocks_dict[bid]["nodes"].append(row["node_url"])

    return {
        "path": meta["path"],
        "size": meta["size"],
        "checksum": meta["checksum"],
        "blocks": list(blocks_dict.values())
    }
