import os
import sys
import hashlib
import argparse
import requests

CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB

class DFSClient:
    def __init__(self, mds_url):
        self.mds_url = mds_url.rstrip('/')
        self.token = None

    def login(self, username, password):
        try:
            res = requests.post(f"{self.mds_url}/login", json={"username": username, "password": password})
            res.raise_for_status()
            self.token = res.json().get("access_token")
            print("[Auth] Login exitoso.")
        except requests.exceptions.RequestException as e:
            print(f"[Auth] Error en login: {e}")
            sys.exit(1)

    def _get_headers(self):
        if not self.token:
            print("[Error] No hay token de autenticacion. Ejecuta login primero.")
            sys.exit(1)
        return {"Authorization": f"Bearer {self.token}"}

    def upload(self, local_path, remote_path):
        if not os.path.exists(local_path):
            print(f"[Error] El archivo local '{local_path}' no existe.")
            sys.exit(1)

        headers = self._get_headers()
        
        # 1. Solicitar lock
        try:
            res_lock = requests.post(f"{self.mds_url}/lock", json={"path": remote_path}, headers=headers)
            res_lock.raise_for_status()
            print(f"[Lock] Archivo '{remote_path}' bloqueado exitosamente.")
        except requests.exceptions.RequestException as e:
            if res_lock.status_code == 423:
                 print("[Lock] El archivo esta bloqueado por otro proceso.")
            else:
                 print(f"[Lock] Error al solicitar bloqueo: {e}")
            sys.exit(1)

        try:
            file_size = os.path.getsize(local_path)
            num_blocks = (file_size + CHUNK_SIZE - 1) // CHUNK_SIZE if file_size > 0 else 0
            
            # 2. Obtener asignacion de nodos
            res_assign = requests.post(
                f"{self.mds_url}/assign",
                json={"path": remote_path, "blocks": num_blocks},
                headers=headers
            )
            res_assign.raise_for_status()
            assignments = res_assign.json().get("assignments", [])
            print(f"[Assign] MDS asigno {len(assignments)} bloques a los nodos.")

            block_info_list = []
            file_hash = hashlib.sha256()

            # 3. Subir bloques
            with open(local_path, "rb") as f:
                for block_index, assignment in enumerate(assignments):
                    chunk = f.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    
                    file_hash.update(chunk)
                    block_sha = hashlib.sha256(chunk).hexdigest()
                    block_id = f"b-{block_sha[:10]}"
                    nodes = assignment.get("nodes", [])
                    
                    uploaded_to = []
                    for node_url in nodes:
                        try:
                            up_res = requests.post(
                                f"{node_url.rstrip('/')}/upload_block",
                                files={"file": (block_id, chunk)},
                                data={"block_id": block_id}
                            )
                            up_res.raise_for_status()
                            uploaded_to.append(node_url)
                        except requests.exceptions.RequestException as e:
                            print(f"[Upload] Fallo subida a nodo {node_url} para bloque {block_id}: {e}")
                    
                    if len(uploaded_to) < 2:
                         raise Exception(f"No se pudo replicar el bloque {block_id} en al menos 2 nodos. Nodos exitosos: {uploaded_to}")
                    
                    block_info_list.append({
                        "block_id": block_id,
                        "checksum": block_sha,
                        "nodes": uploaded_to
                    })
                    print(f"[Upload] Bloque {block_index} ({block_id}) subido a {len(uploaded_to)} réplicas.")

            total_hash = file_hash.hexdigest()

            # 4. Confirmar transaccion
            res_commit = requests.post(
                f"{self.mds_url}/commit",
                json={
                    "path": remote_path,
                    "checksum": total_hash,
                    "blocks": block_info_list,
                    "size": file_size
                },
                headers=headers
            )
            res_commit.raise_for_status()
            print("[Commit] Commit exitoso. Replicacion garantizada.")

        except Exception as e:
            print(f"\n[Error Critico] Fallo durante la subida: {e}")
            sys.exit(1)
        finally:
            # 5. Siempre liberar el lock
            try:
                requests.post(f"{self.mds_url}/unlock", json={"path": remote_path}, headers=headers)
                print(f"[Unlock] Archivo '{remote_path}' desbloqueado.")
            except Exception as e:
                print(f"[Unlock] Error al desbloquear (podria quedar huerfano): {e}")

    def download(self, remote_path, local_path):
        headers = self._get_headers()
        file_hash = hashlib.sha256()

        try:
            res_info = requests.get(f"{self.mds_url}/file_info", params={"path": remote_path}, headers=headers)
            res_info.raise_for_status()
            file_meta = res_info.json()
            blocks = file_meta.get("blocks", [])
            print(f"[Info] Descargando '{remote_path}' ({len(blocks)} bloques encontrados).")
        except requests.exceptions.RequestException as e:
            print(f"[Info] Error al obtener informacion del archivo: {e}")
            sys.exit(1)

        try:
            with open(local_path, "wb") as f:
                if blocks:
                    for block in blocks:
                        block_id = block.get("block_id")
                        expected_hash = block.get("checksum")
                        nodes = block.get("nodes", [])
                        block_data = None
                        valid_block = False

                        for node_url in nodes:
                            try:
                                down_res = requests.get(f"{node_url.rstrip('/')}/download_block/{block_id}")
                                down_res.raise_for_status()
                                block_data = down_res.content
                                
                                # Verificar integridad
                                downloaded_hash = hashlib.sha256(block_data).hexdigest()
                                if downloaded_hash != expected_hash:
                                    print(f"[Download] Corrupcion detectada en {node_url} para bloque {block_id}. Intentando otra replica...")
                                    continue
                                
                                valid_block = True
                                break # Exito, salir del loop de replicas
                            except requests.exceptions.RequestException:
                                print(f"[Download] Nodo {node_url} inalcanzable para bloque {block_id}.")
                                continue

                        if not valid_block or block_data is None:
                            raise Exception(f"No se pudo descargar un bloque valido para {block_id} desde ninguna replica.")
                            
                        f.write(block_data)
                        file_hash.update(block_data)
        except Exception as e:
            print(f"\n[Error Critico] Fallo durante la descarga: {e}")
            if os.path.exists(local_path):
                os.remove(local_path)
                print(f"[Limpieza] Archivo parcial '{local_path}' eliminado.")
            sys.exit(1)

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
