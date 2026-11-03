import os
import shutil
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse

app = FastAPI(title="DN DFSha Fase II")

# Directorio de almacenamiento por defecto (se puede cambiar con env vars para correr multiples)
STORAGE_DIR = os.getenv("STORAGE_DIR", "storage_dn1")
os.makedirs(STORAGE_DIR, exist_ok=True)

@app.post("/upload_block")
async def upload_block(block_id: str = Form(...), file: UploadFile = File(...)):
    path = os.path.join(STORAGE_DIR, f"{block_id}.blk")
    with open(path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    return {"message": f"Bloque {block_id} almacenado"}

@app.get("/download_block/{block_id}")
def download_block(block_id: str):
    path = os.path.join(STORAGE_DIR, f"{block_id}.blk")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Bloque no encontrado")
    return FileResponse(path)
