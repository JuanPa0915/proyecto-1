import os
import json
import hashlib
import requests
from concurrent.futures import ThreadPoolExecutor

BLOCK_SIZE = 4 * 1024 * 1024  # 4 MiB

class DFShaClient:
    def __init__(self, mds_url, token_file='.dfsha_token'):
        self.mds_url = mds_url
        self.token_file = token_file
        self.session = requests.Session()
        self._load_token()

    def _load_token(self):
        if os.path.exists(self.token_file):
            with open(self.token_file, 'r') as f:
                token = f.read().strip()
                self.session.headers.update({'Authorization': f'Bearer {token}'})

    def _save_token(self, token):
        with open(self.token_file, 'w') as f:
            f.write(token)
        os.chmod(self.token_file, 0o600)
        self.session.headers.update({'Authorization': f'Bearer {token}'})

    def _handle_response(self, response):
        if response.status_code >= 400:
            try:
                error_data = response.json().get('error', {})
                raise Exception(f"{error_data.get('code')}: {error_data.get('message')}")
            except ValueError:
                response.raise_for_status()
        if response.status_code == 204:
            return None
        return response.json()

    # UC-01: Autenticarse
    def register(self, username, password):
        resp = self.session.post(f"{self.mds_url}/auth/register", json={"username": username, "password": password}, verify=False)
        self._handle_response(resp)

    def login(self, username, password):
        resp = self.session.post(f"{self.mds_url}/auth/login", json={"username": username, "password": password}, verify=False)
        data = self._handle_response(resp)
        self._save_token(data['access_token'])
        return data['access_token']

    # UC-02: Gestionar la jerarquía (RF1)
    def ls(self, path):
        resp = self.session.get(f"{self.mds_url}/fs/ls", params={"path": path}, verify=False)
        return self._handle_response(resp)['entries']

    def mkdir(self, path):
        resp = self.session.post(f"{self.mds_url}/fs/mkdir", json={"path": path}, verify=False)
        self._handle_response(resp)

    def rmdir(self, path):
        resp = self.session.delete(f"{self.mds_url}/fs/rmdir", params={"path": path}, verify=False)
        self._handle_response(resp)

    def rm(self, path, lock_id=None):
        headers = {'X-Lock-Id': lock_id} if lock_id else {}
        resp = self.session.delete(f"{self.mds_url}/fs/files", params={"path": path}, headers=headers, verify=False)
        self._handle_response(resp)

    # UC-05: Coordinar el acceso (RF3)
    def lock(self, path, ttl_seconds):
        resp = self.session.post(f"{self.mds_url}/locks", json={"path": path, "ttl_seconds": ttl_seconds}, verify=False)
        return self._handle_response(resp)['lock_id']

    def unlock(self, lock_id):
        resp = self.session.delete(f"{self.mds_url}/locks/{lock_id}", verify=False)
        self._handle_response(resp)

    # UC-03 & UC-04: Acceder y Transferir (RF2, RF3)
    def put(self, local_path, remote_path):
        # 1. Open file in write mode
        resp = self.session.post(f"{self.mds_url}/files/open", json={"path": remote_path, "mode": "w", "flags": ["create"]}, verify=False)
        open_data = self._handle_response(resp)
        handle_id = open_data['handle_id']
        
        file_size = os.path.getsize(local_path)
        num_blocks = (file_size + BLOCK_SIZE - 1) // BLOCK_SIZE
        if file_size == 0: num_blocks = 1
        
        blocks_info = []
        
        with open(local_path, 'rb') as f:
            for idx in range(num_blocks):
                data = f.read(BLOCK_SIZE)
                sha256 = hashlib.sha256(data).hexdigest()
                
                # 2. Allocate block
                alloc_resp = self.session.post(f"{self.mds_url}/files/{handle_id}/allocate", json={"indexes": [idx]}, verify=False)
                alloc_data = self._handle_response(alloc_resp)[0]
                block_id = alloc_data['block_id']
                token = alloc_data['token']
                replicas = alloc_data['replicas']
                
                # 3. Upload to DNs in parallel
                def upload_to_dn(dn):
                    dn_url = dn['url']
                    put_resp = requests.put(
                        f"{dn_url}/blocks/{block_id}",
                        data=data,
                        headers={'Authorization': f'Bearer {token}', 'X-Block-SHA256': sha256, 'Content-Type': 'application/octet-stream'},
                        verify=False
                    )
                    if put_resp.status_code >= 400: raise Exception(f"Failed to upload to {dn['dn_id']}")
                
                with ThreadPoolExecutor(max_workers=2) as executor:
                    list(executor.map(upload_to_dn, replicas))
                    
                blocks_info.append({"index": idx, "block_id": block_id, "size": len(data), "sha256": sha256})

        # 4. Commit
        commit_resp = self.session.post(f"{self.mds_url}/files/{handle_id}/commit", json={"mode": "replace", "size": file_size, "blocks": blocks_info}, verify=False)
        self._handle_response(commit_resp)

    def get(self, remote_path, local_path):
        # 1. Open file in read mode
        resp = self.session.post(f"{self.mds_url}/files/open", json={"path": remote_path, "mode": "r", "flags": []}, verify=False)
        open_data = self._handle_response(resp)
        blocks = open_data['blocks']
        
        with open(local_path, 'wb') as f:
            for block in blocks:
                block_id = block['block_id']
                token = block['token']
                replicas = block['replicas']
                expected_sha256 = block['sha256']
                
                # 2. Download from first available replica
                success = False
                for dn in replicas:
                    try:
                        dn_url = dn['url']
                        get_resp = requests.get(f"{dn_url}/blocks/{block_id}", headers={'Authorization': f'Bearer {token}'}, verify=False)
                        get_resp.raise_for_status()
                        data = get_resp.content
                        
                        # Verify integrity
                        if hashlib.sha256(data).hexdigest() != expected_sha256:
                            raise Exception("DATA_CORRUPTION")
                        
                        f.write(data)
                        success = True
                        break
                    except Exception as e:
                        continue
                if not success:
                    raise Exception(f"Failed to download block {block_id}")
