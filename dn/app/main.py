from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import Response
import os
import hashlib
import requests
import asyncio

MDS_URL = os.getenv("MDS_URL", "http://172.31.34.188:8443")
DN_ID = os.getenv("DN_ID", "dn1")
DN_PORT = int(os.getenv("DN_PORT", "8001"))
DN_URL = os.getenv("DN_URL", f"http://localhost:{DN_PORT}")
CLUSTER_SECRET = "secreto-mvp-123"
STORAGE_DIR = f"./storage_{DN_ID}"

os.makedirs(STORAGE_DIR, exist_ok=True)

app = FastAPI(title=f"DFSha Data Node - {DN_ID}")

async def send_heartbeat_loop():
    while True:
        try:
            requests.post(
                f"{MDS_URL}/internal/heartbeat",
                json={"dn_id": DN_ID, "used": 0},
                headers={"X-Cluster-Secret": CLUSTER_SECRET},
                timeout=3
            )
        except Exception as e:
            print(f"[{DN_ID}] Heartbeat error: {e}")
        await asyncio.sleep(3)

@app.on_event("startup")
async def startup_event():
    try:
        blocks = [f.replace(".blk", "") for f in os.listdir(STORAGE_DIR) if f.endswith(".blk")]
        requests.post(
            f"{MDS_URL}/internal/register",
            json={"dn_id": DN_ID, "url": DN_URL, "blocks": blocks},
            headers={"X-Cluster-Secret": CLUSTER_SECRET},
            timeout=5
        )
        print(f"[{DN_ID}] Registrado exitosamente en MDS ({MDS_URL})")
    except Exception as e:
        print(f"[{DN_ID}] Advertencia: No se pudo registrar en el MDS: {e}")
    
    asyncio.create_task(send_heartbeat_loop())

@app.put("/blocks/{block_id}", status_code=201)
async def store_block(
    block_id: str,
    request: Request,
    x_block_sha256: str = Header(...),
    authorization: str = Header(None)
):
    body = await request.body()
    
    computed_hash = hashlib.sha256(body).hexdigest()
    if computed_hash != x_block_sha256:
        raise HTTPException(status_code=422, detail={
            "error": {"code": "HASH_MISMATCH", "message": "El SHA-256 recibido no coincide con el declarado"}
        })
    
    tmp_path = os.path.join(STORAGE_DIR, f"{block_id}.tmp")
    blk_path = os.path.join(STORAGE_DIR, f"{block_id}.blk")
    
    with open(tmp_path, "wb") as f:
        f.write(body)
    os.replace(tmp_path, blk_path)
    
    try:
        requests.post(
            f"{MDS_URL}/internal/block-stored",
            json={"dn_id": DN_ID, "block_id": block_id, "size": len(body), "sha256": computed_hash},
            headers={"X-Cluster-Secret": CLUSTER_SECRET},
            timeout=5
        )
    except Exception as e:
        print(f"[{DN_ID}] Error al notificar al MDS sobre el bloque {block_id}: {e}")
        
    return {"block_id": block_id, "size": len(body), "sha256": computed_hash}

@app.get("/blocks/{block_id}")
async def read_block(block_id: str, authorization: str = Header(None)):
    blk_path = os.path.join(STORAGE_DIR, f"{block_id}.blk")
    if not os.path.exists(blk_path):
        raise HTTPException(status_code=404, detail={
            "error": {"code": "BLOCK_NOT_FOUND", "message": f"Bloque {block_id} no encontrado"}
        })
    
    with open(blk_path, "rb") as f:
        data = f.read()
        
    computed_hash = hashlib.sha256(data).hexdigest()
    return Response(content=data, media_type="application/octet-stream", headers={"X-Block-SHA256": computed_hash})

@app.delete("/blocks/{block_id}", status_code=204)
async def delete_block(block_id: str, x_cluster_secret: str = Header(None)):
    if x_cluster_secret != CLUSTER_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    blk_path = os.path.join(STORAGE_DIR, f"{block_id}.blk")
    if os.path.exists(blk_path):
        os.remove(blk_path)
    return None
