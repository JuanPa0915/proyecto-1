import argparse, requests, sys, os, hashlib

class DFSClient:
    def __init__(self, mds_url):
        self.mds_url = mds_url
        self.token = None

    def login(self, username, password):
        # Register automatico para la prueba
        requests.post(f"{self.mds_url}/register", json={"username": username, "password": password})
        res = requests.post(f"{self.mds_url}/login", json={"username": username, "password": password})
        if res.status_code == 200:
            self.token = res.json()["token"]
            print("Login exitoso.")
        else:
            print("Error auth:", res.text)
            sys.exit(1)

    def get_headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    def upload(self, local_path, remote_path):
        # 1. Lock el archivo
        res = requests.post(f"{self.mds_url}/lock?path={remote_path}", headers=self.get_headers())
        if res.status_code != 200:
            print("El archivo esta bloqueado (Prueba de Concurrencia superada).")
            return

        # 2. Pedir Nodos (Garantiza replicas)
        res = requests.post(f"{self.mds_url}/assign?path={remote_path}&blocks=1", headers=self.get_headers())
        if res.status_code != 200:
            print("Error asignando nodos:", res.text)
            requests.post(f"{self.mds_url}/unlock?path={remote_path}", headers=self.get_headers())
            return
        
        replicas = res.json()["replicas"]
        block_id = "b-" + hashlib.sha256(open(local_path, "rb").read()).hexdigest()[:8]

        # 3. Subir a replicas
        with open(local_path, "rb") as f:
            data = f.read()
            for dn_url in replicas:
                requests.put(f"{dn_url}/blocks/{block_id}", data=data, headers=self.get_headers())
        
        # 4. Commit (Verifica 2 replicas internamente)
        res = requests.post(f"{self.mds_url}/commit", json={"path": remote_path, "block_ids": [block_id]}, headers=self.get_headers())
        print(res.json()["message"])

        # 5. Unlock
        requests.post(f"{self.mds_url}/unlock?path={remote_path}", headers=self.get_headers())

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cliente DFSha Fase II")
    parser.add_argument("action", choices=["upload"])
    parser.add_argument("local_file")
    parser.add_argument("--mds", required=True)
    args = parser.parse_args()

    client = DFSClient(args.mds)
    client.login("admin", "admin123") # Credenciales hardcodeadas para facilitar pruebas

    if args.action == "upload":
        client.upload(args.local_file, f"/{os.path.basename(args.local_file)}")