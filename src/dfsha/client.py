import argparse
import hashlib
import os
import sys

import requests

CHUNK_SIZE = 4 * 1024 * 1024  # bloques de 4 MiB
TIMEOUT_MDS = 30
TIMEOUT_DN = 120


def fail(msg):
    print(f"[Error] {msg}")
    sys.exit(1)


class DFSClient:
    def __init__(self, mds_url):
        self.mds_url = mds_url.rstrip("/")
        self.token = None

    # ---------- autenticacion ----------
    def login(self, username, password):
        body = {"username": username, "password": password}
        try:
            # registrar: si el usuario ya existe el MDS responde 400 y se ignora
            requests.post(f"{self.mds_url}/register", json=body, timeout=TIMEOUT_MDS)
            res = requests.post(f"{self.mds_url}/login", json=body, timeout=TIMEOUT_MDS)
        except requests.RequestException as e:
            fail(f"No se pudo contactar al MDS: {e}")
        if res.status_code != 200:
            fail(f"Login rechazado ({res.status_code}): {res.text}")
        self.token = res.json()["token"]
        print("[Auth] Login exitoso.")

    def headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    def mds(self, method, route, **kw):
        try:
            return requests.request(method, f"{self.mds_url}{route}", headers=self.headers(),
                                    timeout=TIMEOUT_MDS, **kw)
        except requests.RequestException as e:
            fail(f"Fallo la llamada al MDS ({route}): {e}")

    # ---------- subida ----------
    def upload(self, local_path, remote_path):
        if not os.path.isfile(local_path):
            fail(f"El archivo local '{local_path}' no existe.")
        size = os.path.getsize(local_path)
        n_blocks = max(1, -(-size // CHUNK_SIZE))  # division hacia arriba

        # 1. lock
        res = self.mds("POST", "/lock", params={"path": remote_path})
        if res.status_code == 409:
            print("[Lock] El archivo esta bloqueado por otro proceso.")
            sys.exit(2)
        if res.status_code != 200:
            fail(f"No se pudo tomar el lock ({res.status_code}): {res.text}")

        try:
            # 2. asignar nodos para todos los bloques
            res = self.mds("POST", "/assign", params={"path": remote_path, "blocks": n_blocks})
            if res.status_code != 200:
                fail(f"Error asignando nodos ({res.status_code}): {res.text}")
            assigned = res.json()["blocks"]

            # 3. enviar cada bloque a sus 2 replicas
            manifest = []
            with open(local_path, "rb") as f:
                for entry in assigned:
                    data = f.read(CHUNK_SIZE)
                    block_id = hashlib.sha256(data).hexdigest()
                    stored = []
                    for dn_url in entry["replicas"]:
                        try:
                            r = requests.put(f"{dn_url}/blocks/{block_id}", data=data,
                                             headers=self.headers(), timeout=TIMEOUT_DN)
                        except requests.RequestException as e:
                            fail(f"Bloque {entry['index']}: fallo al enviar a {dn_url}: {e}")
                        if r.status_code != 200:
                            fail(f"Bloque {entry['index']}: {dn_url} respondio {r.status_code}: {r.text}")
                        stored.append(dn_url)
                    manifest.append({"index": entry["index"], "block_id": block_id, "nodes": stored})
                    print(f"[Upload] Bloque {entry['index'] + 1}/{n_blocks} -> {', '.join(stored)}")

            # 4. commit
            res = self.mds("POST", "/commit", json={"path": remote_path, "blocks": manifest})
            if res.status_code != 200:
                fail(f"Commit rechazado ({res.status_code}): {res.text}")
            print(f"[Commit] {res.json()['message']} ({n_blocks} bloques)")
        finally:
            # el lock siempre se libera, incluso si algo fallo
            try:
                requests.post(f"{self.mds_url}/unlock", params={"path": remote_path},
                              headers=self.headers(), timeout=TIMEOUT_MDS)
            except requests.RequestException as e:
                print(f"[Aviso] No se pudo liberar el lock: {e}")

    # ---------- descarga ----------
    def download(self, remote_path, local_path):
        res = self.mds("GET", "/file_info", params={"path": remote_path})
        if res.status_code != 200:
            fail(f"No se pudo obtener el archivo ({res.status_code}): {res.text}")
        blocks = res.json()["blocks"]

        total = hashlib.sha256()
        tmp_path = local_path + ".part"
        with open(tmp_path, "wb") as out:
            for b in blocks:
                data = None
                for dn_url in b["nodes"]:
                    try:
                        r = requests.get(f"{dn_url}/blocks/{b['block_id']}",
                                         headers=self.headers(), timeout=TIMEOUT_DN)
                    except requests.RequestException as e:
                        print(f"[Aviso] Bloque {b['index']}: {dn_url} no responde ({e}); probando otra replica.")
                        continue
                    if r.status_code != 200:
                        print(f"[Aviso] Bloque {b['index']}: {dn_url} respondio {r.status_code}; probando otra replica.")
                        continue
                    if hashlib.sha256(r.content).hexdigest() != b["block_id"]:
                        print(f"[Aviso] Bloque {b['index']}: {dn_url} devolvio datos corruptos; probando otra replica.")
                        continue
                    data = r.content
                    break
                if data is None:
                    out.close()
                    os.remove(tmp_path)
                    fail(f"No se pudo recuperar el bloque {b['index']} de ninguna replica.")
                out.write(data)
                total.update(data)
        os.replace(tmp_path, local_path)
        print(f"[Download] Archivo guardado en {local_path} ({len(blocks)} bloques, SHA-256: {total.hexdigest()[:10]}...)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cliente DFSha Fase II")
    parser.add_argument("action", choices=["upload", "download"])
    parser.add_argument("file_1")
    parser.add_argument("file_2", nargs="?", default=None)
    parser.add_argument("--mds", required=True)
    parser.add_argument("--user", default=os.getenv("DFSHA_USER", "admin"))
    parser.add_argument("--password", default=os.getenv("DFSHA_PASS", "admin123"))
    args = parser.parse_args()

    client = DFSClient(args.mds)
    client.login(args.user, args.password)

    if args.action == "upload":
        remote = args.file_2 if args.file_2 else f"/{os.path.basename(args.file_1)}"
        client.upload(args.file_1, remote)
    else:
        remote = args.file_1
        local = args.file_2 if args.file_2 else "descargado.txt"
        client.download(remote, local)
