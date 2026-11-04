from fastapi import FastAPI, HTTPException, Header, Depends
from pydantic import BaseModel
from passlib.context import CryptContext
import sqlite3, jwt, time, asyncio, os
from typing import List, Optional

app = FastAPI(title="DFSha - MDS")

SECRET_KEY = "secreto-fase2-dfsha"
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Estado en memoria de los Data Nodes
active_nodes = {}

def init_db():
    conn = sqlite3.connect("dfsha.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, password_hash TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, is_dir BOOLEAN, owner TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS locks (path TEXT PRIMARY KEY, owner TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS file_blocks (path TEXT, block_id TEXT, node_id TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS file_manifest (path TEXT, idx INTEGER, block_id TEXT, nodes TEXT, PRIMARY KEY (path, idx))''')
    conn.commit()
    conn.close()

init_db()

# --- BACKGROUND TASK: Monitor de Nodos ---
async def monitor_nodes():
    while True:
        current_time = time.time()
        for node_id, data in list(active_nodes.items()):
            if current_time - data['last_heartbeat'] > 10:
                print(f"[Monitor] Nodo {node_id} marcado como DEAD")
                del active_nodes[node_id]
        await asyncio.sleep(5)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(monitor_nodes())

# --- SEGURIDAD Y AUTH ---
class UserAuth(BaseModel):
    username: str
    password: str

@app.post("/register")
def register(user: UserAuth):
    conn = sqlite3.connect("dfsha.db")
    c = conn.cursor()
    try:
        hashed = pwd_context.hash(user.password)
        c.execute("INSERT INTO users VALUES (?, ?)", (user.username, hashed))
        conn.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="Usuario ya existe")
    finally:
        conn.close()
    return {"message": "Registrado correctamente"}

@app.post("/login")
def login(user: UserAuth):
    conn = sqlite3.connect("dfsha.db")
    c = conn.cursor()
    c.execute("SELECT password_hash FROM users WHERE username=?", (user.username,))
    row = c.fetchone()
    conn.close()
    if not row or not pwd_context.verify(user.password, row[0]):
        raise HTTPException(status_code=401, detail="Credenciales invalidas")
    
    token = jwt.encode({"sub": user.username, "exp": time.time() + 3600}, SECRET_KEY, algorithm="HS256")
    return {"token": token}

def get_current_user(authorization: str = Header(None)):
    if not authorization: raise HTTPException(status_code=401, detail="Token faltante")
    try:
        token = authorization.replace("Bearer ", "")
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        return payload["sub"]
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expirado")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Token invalido")

# --- CONCURRENCIA (LOCKS) ---
@app.post("/lock")
def acquire_lock(path: str, user: str = Depends(get_current_user)):
    conn = sqlite3.connect("dfsha.db")
    c = conn.cursor()
    try:
        c.execute("INSERT INTO locks VALUES (?, ?)", (path, user))
        conn.commit()
        return {"message": "Lock adquirido"}
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="Archivo ya bloqueado por otro usuario")
    finally:
        conn.close()

@app.post("/unlock")
def release_lock(path: str, user: str = Depends(get_current_user)):
    conn = sqlite3.connect("dfsha.db")
    c = conn.cursor()
    c.execute("DELETE FROM locks WHERE path=? AND owner=?", (path, user))
    released = c.rowcount
    conn.commit()
    conn.close()
    if released == 0:
        raise HTTPException(status_code=403, detail="No tienes el lock o no existe")
    return {"message": "Lock liberado"}

# --- JERARQUÍA (ls, rmdir) ---
@app.get("/ls")
def list_files(path: str = "", user: str = Depends(get_current_user)):
    conn = sqlite3.connect("dfsha.db")
    c = conn.cursor()
    if path and not path.endswith('/'): path += '/'
    # Filtra archivos que esten estrictamente bajo esta ruta
    c.execute("SELECT path, is_dir FROM files WHERE path LIKE ?", (f"{path}%",))
    rows = c.fetchall()
    conn.close()
    return {"files": [{"path": r[0], "is_dir": bool(r[1])} for r in rows]}

@app.delete("/rmdir")
def remove_dir(path: str, user: str = Depends(get_current_user)):
    conn = sqlite3.connect("dfsha.db")
    c = conn.cursor()
    if not path.endswith('/'): path += '/'
    c.execute("SELECT count(*) FROM files WHERE path LIKE ? AND path != ?", (f"{path}%", path))
    if c.fetchone()[0] > 0:
        raise HTTPException(status_code=400, detail="El directorio no esta vacio")
    c.execute("DELETE FROM files WHERE path=? AND owner=?", (path, user))
    conn.commit()
    conn.close()
    return {"message": "Directorio eliminado"}

# --- HEARTBEAT Y COMUNICACIÓN INTERNA ---
class HeartbeatReq(BaseModel):
    dn_id: str
    url: str

@app.post("/internal/heartbeat")
def heartbeat(req: HeartbeatReq):
    active_nodes[req.dn_id] = {"url": req.url, "last_heartbeat": time.time()}
    return {"status": "ALIVE"}

# --- ASIGNACIÓN Y COMMIT (varios bloques) ---
REPLICAS = 2

@app.post("/assign")
def assign_blocks(path: str, blocks: int, user: str = Depends(get_current_user)):
    nodes = list(active_nodes.values())
    if len(nodes) < REPLICAS:
        raise HTTPException(status_code=503, detail="No hay suficientes Data Nodes para garantizar replicación (Min 2)")
    if blocks < 1:
        raise HTTPException(status_code=400, detail="blocks debe ser >= 1")

    # Reparto rotativo: el bloque i usa los nodos i, i+1 (en circulo)
    result = []
    for i in range(blocks):
        replicas = [nodes[(i + k) % len(nodes)]["url"] for k in range(REPLICAS)]
        result.append({"index": i, "replicas": replicas})
    return {"blocks": result}

class BlockInfo(BaseModel):
    index: int
    block_id: str
    nodes: List[str]

class CommitReq(BaseModel):
    path: str
    blocks: List[BlockInfo]

@app.post("/commit")
def commit_file(req: CommitReq, user: str = Depends(get_current_user)):
    # Cada bloque debe estar en al menos 2 nodos distintos
    for b in req.blocks:
        if len(set(b.nodes)) < REPLICAS:
            raise HTTPException(status_code=503, detail=f"Bloque {b.index} no alcanzo factor de replicacion {REPLICAS}")

    conn = sqlite3.connect("dfsha.db")
    c = conn.cursor()
    # Solo puede confirmar quien tiene el lock
    c.execute("SELECT owner FROM locks WHERE path=?", (req.path,))
    row = c.fetchone()
    if not row or row[0] != user:
        conn.close()
        raise HTTPException(status_code=403, detail="No tienes el lock de este archivo")

    # Todo en una sola transaccion: el manifiesto nuevo reemplaza al viejo de golpe
    c.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?)", (req.path, False, user))
    c.execute("DELETE FROM file_manifest WHERE path=?", (req.path,))
    for b in req.blocks:
        c.execute("INSERT INTO file_manifest VALUES (?, ?, ?, ?)",
                  (req.path, b.index, b.block_id, ",".join(b.nodes)))
    conn.commit()
    conn.close()
    return {"message": "Commit exitoso. Replicacion garantizada.", "blocks": len(req.blocks)}

@app.get("/file_info")
def get_file_info(path: str, user: str = Depends(get_current_user)):
    conn = sqlite3.connect("dfsha.db")
    c = conn.cursor()
    c.execute("SELECT 1 FROM files WHERE path=? AND is_dir=0", (path,))
    if not c.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    c.execute("SELECT idx, block_id, nodes FROM file_manifest WHERE path=? ORDER BY idx", (path,))
    rows = c.fetchall()
    conn.close()
    return {"blocks": [{"index": r[0], "block_id": r[1], "nodes": r[2].split(",")} for r in rows]}
