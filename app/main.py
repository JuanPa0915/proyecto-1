from fastapi import FastAPI, Depends, HTTPException, Header
from pydantic import BaseModel
from typing import List, Optional
import uuid
import time
from app.database import init_db, get_db

app = FastAPI(title="DFSha MDS - Metadata Server")

CLUSTER_SECRET = "secreto-mvp-123"

@app.on_event("startup")
def startup_event():
    init_db()

class UserAuth(BaseModel):
    username: str
    password: str

class OpenFileReq(BaseModel):
    path: str
    mode: str
    flags: List[str] = []

class AllocateReq(BaseModel):
    indexes: List[int]
    exclude: Optional[List[str]] = []

class BlockCommitInfo(BaseModel):
    index: int
    block_id: str
    size: int
    sha256: str

class CommitReq(BaseModel):
    mode: str
    size: int
    blocks: List[BlockCommitInfo]

class LockReq(BaseModel):
    path: str
    ttl_seconds: int = 30

@app.post("/auth/register", status_code=201)
def register(user: UserAuth):
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO users (user_id, username, password_hash) VALUES (?, ?, ?)",
                    (str(uuid.uuid4()), user.username, user.password))
        conn.commit()
        return {"msg": "Usuario registrado"}
    except Exception:
        raise HTTPException(status_code=409, detail={"error": {"code": "ALREADY_EXISTS", "message": "El usuario ya existe"}})
    finally:
        conn.close()

@app.post("/auth/login")
def login(user: UserAuth):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE username = ? AND password_hash = ?", (user.username, user.password))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=401, detail={"error": {"code": "UNAUTHENTICATED", "message": "Credenciales inválidas"}})
    return {"access_token": f"{row['user_id']}-token", "expires_in": 3600}

def get_current_user_id(authorization: str = Header(...)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail={"error": {"code": "UNAUTHENTICATED", "message": "Token faltante"}})
    token = authorization.split(" ")[1]
    user_id = token.replace("-token", "")
    return user_id

@app.get("/fs/ls")
def ls(path: str = "/", user_id: str = Depends(get_current_user_id)):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT name, type, size, mtime FROM inodes WHERE user_id = ?", (user_id,))
    rows = cur.fetchall()
    conn.close()
    entries = [{"name": r["name"], "type": r["type"], "size": r["size"], "mtime": r["mtime"]} for r in rows]
    return {"entries": entries}

@app.post("/fs/mkdir", status_code=201)
def mkdir(req: dict, user_id: str = Depends(get_current_user_id)):
    path = req.get("path")
    conn = get_db()
    cur = conn.cursor()
    inode_id = str(uuid.uuid4())
    name = path.strip("/")
    cur.execute("INSERT INTO inodes (inode_id, user_id, parent_id, name, type, size, version, mtime) VALUES (?, ?, 'root', ?, 'DIR', 0, 1, ?)",
                (inode_id, user_id, name, int(time.time())))
    conn.commit()
    conn.close()
    return {"msg": "OK"}

@app.delete("/fs/rmdir", status_code=204)
def rmdir(path: str = "/", user_id: str = Depends(get_current_user_id)):
    conn = get_db()
    cur = conn.cursor()
    name = path.strip("/")
    cur.execute("DELETE FROM inodes WHERE user_id = ? AND name = ? AND type = 'DIR'", (user_id, name))
    conn.commit()
    conn.close()
    return None

@app.delete("/fs/files", status_code=204)
def rm(path: str = "/", user_id: str = Depends(get_current_user_id)):
    conn = get_db()
    cur = conn.cursor()
    name = path.strip("/")
    cur.execute("DELETE FROM inodes WHERE user_id = ? AND name = ?", (user_id, name))
    conn.commit()
    conn.close()
    return None

@app.post("/files/open")
def open_file(req: OpenFileReq, user_id: str = Depends(get_current_user_id)):
    conn = get_db()
    cur = conn.cursor()
    name = req.path.strip("/")
    cur.execute("SELECT inode_id, version, size FROM inodes WHERE user_id = ? AND name = ?", (user_id, name))
    inode = cur.fetchone()
    
    if not inode:
        if "create" in req.flags or req.mode == "w":
            inode_id = str(uuid.uuid4())
            cur.execute("INSERT INTO inodes (inode_id, user_id, parent_id, name, type, size, version, mtime) VALUES (?, ?, 'root', ?, 'FILE', 0, 0, ?)",
                        (inode_id, user_id, name, int(time.time())))
            version, size = 0, 0
        else:
            conn.close()
            raise HTTPException(status_code=404, detail={"error": {"code": "NOT_FOUND", "message": "Archivo no encontrado"}})
    else:
        inode_id, version, size = inode["inode_id"], inode["version"], inode["size"]

    handle_id = f"h-{uuid.uuid4().hex[:6]}"
    cur.execute("INSERT INTO handles (handle_id, user_id, inode_id, mode, base_version, expires) VALUES (?, ?, ?, ?, ?, ?)",
                (handle_id, user_id, inode_id, req.mode, version, int(time.time()) + 300))
    
    blocks_res = []
    if req.mode == "r":
        cur.execute("SELECT block_id, idx, size, sha256 FROM blocks WHERE inode_id = ? AND state = 'COMMITTED' ORDER BY idx", (inode_id,))
        blks = cur.fetchall()
        
        cur.execute("SELECT dn_id, url FROM nodes WHERE status = 'ALIVE'")
        live_nodes = {r["dn_id"]: r["url"] for r in cur.fetchall()}
        
        for b in blks:
            cur.execute("SELECT dn_id FROM replicas WHERE block_id = ?", (b["block_id"],))
            reps = cur.fetchall()
            replica_list = [{"dn_id": r["dn_id"], "url": live_nodes[r["dn_id"]]} for r in reps if r["dn_id"] in live_nodes]
            blocks_res.append({
                "block_id": b["block_id"],
                "index": b["idx"],
                "size": b["size"],
                "sha256": b["sha256"],
                "token": f"token-{b['block_id']}",
                "replicas": replica_list
            })

    conn.commit()
    conn.close()
    return {"handle_id": handle_id, "version": version, "size": size, "block_size": 4194304, "blocks": blocks_res}

@app.post("/files/{handle_id}/allocate")
def allocate_block(handle_id: str, req: AllocateReq, user_id: str = Depends(get_current_user_id)):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT inode_id FROM handles WHERE handle_id = ?", (handle_id,))
    handle = cur.fetchone()
    if not handle:
        conn.close()
        raise HTTPException(status_code=410, detail={"error": {"code": "HANDLE_EXPIRED", "message": "Handle expirado"}})
        
    cur.execute("SELECT dn_id, url FROM nodes WHERE status = 'ALIVE'")
    nodes = cur.fetchall()
    conn.close()
    
    if len(nodes) < 1:
        raise HTTPException(status_code=503, detail={"error": {"code": "NOT_ENOUGH_NODES", "message": "No hay Nodos de Datos activos"}})
        
    result = []
    for idx in req.indexes:
        block_id = f"b-{uuid.uuid4().hex[:6]}"
        replicas = [{"dn_id": n["dn_id"], "url": n["url"]} for n in nodes[:2]]
        result.append({
            "index": idx,
            "block_id": block_id,
            "token": f"token-{block_id}",
            "replicas": replicas
        })
    return result

@app.post("/files/{handle_id}/commit")
def commit_file(handle_id: str, req: CommitReq, user_id: str = Depends(get_current_user_id)):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT inode_id, base_version FROM handles WHERE handle_id = ?", (handle_id,))
    handle = cur.fetchone()
    if not handle:
        conn.close()
        raise HTTPException(status_code=410, detail={"error": {"code": "HANDLE_EXPIRED", "message": "Apertura vencida"}})
        
    inode_id = handle["inode_id"]
    
    for b in req.blocks:
        cur.execute("INSERT OR REPLACE INTO blocks (block_id, inode_id, idx, size, sha256, state) VALUES (?, ?, ?, ?, ?, 'COMMITTED')",
                    (b.block_id, inode_id, b.index, b.size, b.sha256))
                    
    cur.execute("UPDATE inodes SET size = ?, version = version + 1, mtime = ? WHERE inode_id = ?",
                (req.size, int(time.time()), inode_id))
    cur.execute("DELETE FROM handles WHERE handle_id = ?", (handle_id,))
    
    conn.commit()
    conn.close()
    return {"version": 1, "size": req.size}

@app.post("/locks", status_code=201)
def acquire_lock(req: LockReq, user_id: str = Depends(get_current_user_id)):
    lock_id = f"l-{uuid.uuid4().hex[:6]}"
    return {"lock_id": lock_id, "expires_in": req.ttl_seconds}

@app.delete("/locks/{lock_id}", status_code=204)
def release_lock(lock_id: str, user_id: str = Depends(get_current_user_id)):
    return None

@app.post("/internal/register")
def dn_register(req: dict, x_cluster_secret: str = Header(None)):
    if x_cluster_secret != CLUSTER_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO nodes (dn_id, url, last_heartbeat, status) VALUES (?, ?, ?, 'ALIVE')",
                (req.get("dn_id"), req.get("url"), int(time.time())))
    conn.commit()
    conn.close()
    return {"delete": []}

@app.post("/internal/heartbeat")
def dn_heartbeat(req: dict, x_cluster_secret: str = Header(None)):
    if x_cluster_secret != CLUSTER_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE nodes SET last_heartbeat = ?, status = 'ALIVE' WHERE dn_id = ?",
                (int(time.time()), req.get("dn_id")))
    conn.commit()
    conn.close()
    return {"status": "OK"}

@app.post("/internal/block-stored", status_code=204)
def dn_block_stored(req: dict, x_cluster_secret: str = Header(None)):
    if x_cluster_secret != CLUSTER_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO replicas (block_id, dn_id) VALUES (?, ?)",
                (req.get("block_id"), req.get("dn_id")))
    conn.commit()
    conn.close()
    return None