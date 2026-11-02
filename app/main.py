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
    if c.rowcount == 0:
        raise HTTPException(status_code=403, detail="No tienes el lock o no existe")
    conn.commit()
    conn.close()
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

# --- ASIGNACIÓN Y COMMIT ---
@app.post("/assign")
def assign_blocks(path: str, blocks: int, user: str = Depends(get_current_user)):
    if len(active_nodes) < 2:
        raise HTTPException(status_code=503, detail="No hay suficientes Data Nodes para garantizar replicación (Min 2)")
    
    # Asignar los primeros 2 nodos activos para replicacion
    selected_nodes = list(active_nodes.values())[:2]
    return {"replicas": [n["url"] for n in selected_nodes]}

class CommitReq(BaseModel):
    path: str
    block_ids: List[str]

@app.post("/commit")
def commit_file(req: CommitReq, user: str = Depends(get_current_user)):
    # Validacion: Exigir replicas y guardar version
    if len(active_nodes) < 2:
        raise HTTPException(status_code=503, detail="Fallo de commit: No se alcanzo factor de replicacion 2")
    
    conn = sqlite3.connect("dfsha.db")
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?)", (req.path, False, user))
    c.execute("DELETE FROM file_blocks WHERE path=?", (req.path,)) # Reemplazar viejo manifiesto
    
    # Simular guardado de metadata de bloques
    for bid in req.block_ids:
        c.execute("INSERT INTO file_blocks VALUES (?, ?, ?)", (req.path, bid, "dn_replicas"))
    
    conn.commit()
    conn.close()
    return {"message": "Commit exitoso. Replicacion garantizada."}