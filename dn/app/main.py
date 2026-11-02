from fastapi import FastAPI, HTTPException, Header, Request, Response
import os, asyncio, requests, jwt

MDS_URL = os.getenv("MDS_URL", "http://172.31.34.188:8443")
DN_ID = os.getenv("DN_ID", "dn1")
DN_PORT = int(os.getenv("DN_PORT", "8001"))
DN_URL = f"http://172.31.43.252:{DN_PORT}" # IP PRIVADA DE LOS NODOS
STORAGE_DIR = f"./storage_{DN_ID}"
SECRET_KEY = "secreto-fase2-dfsha" # Debe coincidir con MDS

os.makedirs(STORAGE_DIR, exist_ok=True)
app = FastAPI(title=f"DFSha Data Node - {DN_ID}")

async def send_heartbeat():
    while True:
        try:
            requests.post(
                f"{MDS_URL}/internal/heartbeat",
                json={"dn_id": DN_ID, "url": DN_URL},
                timeout=3
            )
        except Exception as e:
            print(f"[{DN_ID}] Heartbeat error: {e}")
        await asyncio.sleep(5)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(send_heartbeat())

def verify_token(authorization: str):
    if not authorization: raise HTTPException(status_code=401, detail="Falta Token")
    try:
        token = authorization.replace("Bearer ", "")
        jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    except Exception:
        raise HTTPException(status_code=401, detail="Token Invalido en DN")

@app.put("/blocks/{block_id}")
async def store_block(block_id: str, request: Request, authorization: str = Header(None)):
    verify_token(authorization) # <-- Seguridad implementada
    body = await request.body()
    with open(os.path.join(STORAGE_DIR, f"{block_id}.blk"), "wb") as f:
        f.write(body)
    return {"message": "Bloque guardado y replicado"}

@app.get("/blocks/{block_id}")
def get_block(block_id: str, authorization: str = Header(None)):
    verify_token(authorization) # <-- Seguridad implementada
    filepath = os.path.join(STORAGE_DIR, f"{block_id}.blk")
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Bloque no encontrado")
    with open(filepath, "rb") as f:
        return Response(content=f.read())