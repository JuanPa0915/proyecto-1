import argparse, requests, sys, os, hashlib

class DFSClient:
    def __init__(self, mds_url):
        self.mds_url = mds_url
        self.token = None

    def login(self, username, password):
        requests.post(f"{self.mds_url}/register", json={"username": username, "password": password})
        res = requests.post(f"{self.mds_url}/login", json={"username": username, "password": password})
        if res.status_code == 200:
            self.token = res.json()["token"]
            print("[Auth] Login exitoso.")
        else:
            print("[Auth] Error:", res.text)
            sys.exit(1)

    def get_headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    def upload(self, local_path, remote_path):
        res = requests.post(f"{self.mds_url}/lock?path={remote_path}", headers=self.get_headers())
        if res.status_code != 200:
            print("[Lock] El archivo esta bloqueado por otro proceso.")
            return

        res = requests.post(f"{self.mds_url}/assign?path={remote_path}&blocks=1", headers=self.get_headers())
        if res.status_code != 200:
            print("[MDS] Error asignando nodos:", res.text)
            requests.post(f"{self.mds_url}/unlock?path={remote_path}", headers=self.get_headers())
            return
        
        replicas = res.json()["replicas"]
        with open(local_path, "rb") as f:
            data = f.read()
        
        block_id = "b-" + hashlib.sha256(data).hexdigest()[:8]

        for dn_url in replicas:
            requests.put(f"{dn_url}/blocks/{block_id}", data=data, headers=self.get_headers())
        
        res = requests.post(f"{self.mds_url}/commit", json={"path": remote_path, "block_ids": [block_id]}, headers=self.get_headers())
        print(f"[Commit] {res.json().get('message', 'Exito')}")

        requests.post(f"{self.mds_url}/unlock?path={remote_path}", headers=self.get_headers())

    def download(self, remote_path, local_path):
        res = requests.get(f"{self.mds_url}/file_info?path={remote_path}", headers=self.get_headers())
        if res.status_code != 200:
            print("[MDS] Error al obtener informacion del archivo:", res.text)
            return

        info = res.json()
        block_ids = info["block_ids"]
        node_urls = info["node_urls"]

        file_data = bytearray()
        for bid in block_ids:
            downloaded = False
            for url in node_urls:
                r = requests.get(f"{url}/blocks/{bid}", headers=self.get_headers())
                if r.status_code == 200:
                    file_data.extend(r.content)
                    downloaded = True
                    break
            if not downloaded:
                print(f"[DN Error] No se pudo recuperar el bloque {bid} de ningun nodo activo.")
                return

        with open(local_path, "wb") as f:
            f.write(file_data)
        
        sha = hashlib.sha256(file_data).hexdigest()
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
