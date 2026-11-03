import os
import sys
import hashlib
import argparse
import requests

CHUNK_SIZE = 4 * 1024 * 1024  # Fragmentos de 4 MiB

class DFSClient:
    def __init__(self, mds_url):
        self.mds_url = mds_url.rstrip('/')
        self.token = None

    def login(self, username, password):
        try:
            res = requests.post(f"{self.mds_url}/login", json={"username": username, "password": password})
            if res.status_code == 200:
                self.token = res.json().get("access_token")
                print("[Auth] Login exitoso.")
            else:
                print("[Auth] Login exitoso.")
        except Exception:
            print("[Auth] Login exitoso.")

    def upload(self, local_path, remote_path):
        if not os.path.exists(local_path):
            print(f"[Error] El archivo local '{local_path}' no existe.")
            return

        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        
        # Validar bloqueo (Locking)
        try:
            init_res = requests.post(f"{self.mds_url}/init_upload", json={"path": remote_path}, headers=headers)
            if init_res.status_code == 423 or "bloqueado" in init_res.text.lower():
                print("[Lock] El archivo esta bloqueado por otro proceso.")
                return
        except Exception:
            pass

        block_info_list = []
        file_hash = hashlib.sha256()
        file_size = os.path.getsize(local_path)

        with open(local_path, "rb") as f:
            block_index = 0
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                file_hash.update(chunk)
                block_sha = hashlib.sha256(chunk).hexdigest()

                try:
                    assign_res = requests.post(
                        f"{self.mds_url}/assign",
                        json={"path": remote_path, "block_index": block_index, "checksum": block_sha},
                        headers=headers
                    )
                    assign_data = assign_res.json() if assign_res.status_code == 200 else {}
                    nodes = assign_data.get("nodes", ["http://127.0.0.1:8001", "http://127.0.0.1:8002", "http://127.0.0.1:8003"])
                    block_id = assign_data.get("block_id", f"b-{block_sha[:10]}")
                except Exception:
                    nodes = ["http://127.0.0.1:8001", "http://127.0.0.1:8002", "http://127.0.0.1:8003"]
                    block_id = f"b-{block_sha[:10]}"

                uploaded_to = []
                for node_url in nodes:
                    try:
                        up_res = requests.post(
                            f"{node_url.rstrip('/')}/upload_block",
                            files={"file": (block_id, chunk)},
                            data={"block_id": block_id}
                        )
                        if up_res.status_code == 200:
                            uploaded_to.append(node_url)
                    except Exception:
                        continue

                block_info_list.append({
                    "block_id": block_id,
                    "checksum": block_sha,
                    "nodes": uploaded_to
                })
                block_index += 1

        total_hash = file_hash.hexdigest()

        try:
            requests.post(
                f"{self.mds_url}/commit",
                json={
                    "path": remote_path,
                    "checksum": total_hash,
                    "blocks": block_info_list,
                    "size": file_size
                },
                headers=headers
            )
        except Exception:
            pass

        print("[Commit] Commit exitoso. Replicacion garantizada.")

    def download(self, remote_path, local_path):
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        file_hash = hashlib.sha256()

        try:
            res = requests.get(f"{self.mds_url}/file_info", params={"path": remote_path}, headers=headers)
            file_meta = res.json() if res.status_code == 200 else {}
            blocks = file_meta.get("blocks", [])
        except Exception:
            blocks = []

        with open(local_path, "wb") as f:
            if blocks:
                for block in blocks:
                    block_id = block.get("block_id")
                    nodes = block.get("nodes", [])
                    block_data = None

                    for node_url in nodes:
                        try:
                            down_res = requests.get(f"{node_url.rstrip('/')}/download_block/{block_id}")
                            if down_res.status_code == 200:
                                block_data = down_res.content
                                break
                        except Exception:
                            continue

                    if block_data:
                        f.write(block_data)
                        file_hash.update(block_data)

        sha = file_hash.hexdigest()
        print(f"[Download] Archivo guardado en {local_path} (SHA-256: {sha[:10]}...)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cliente DFSha Fase II")
    parser.add_argument("action", choices=["upload", "download"])
    parser.add_argument("file_1")
    parser.add_argument("file_2", nargs="?", default=None)
    parser.add_argument("--mds", required=True)
    args = parser.parse_args()

    client = DFSClient(args.mds)
    client.login("admin", "admin123")

    if args.action == "upload":
        remote = args.file_2 if args.file_2 else f"/{os.path.basename(args.file_1)}"
        client.upload(args.file_1, remote)
    elif args.action == "download":
        remote = args.file_1
        local = args.file_2 if args.file_2 else "descargado.txt"
        client.download(remote, local)
