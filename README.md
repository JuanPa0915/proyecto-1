# README - Despliegue y Pruebas DFSha (Fase II)

Este documento detalla los comandos necesarios para desplegar y probar el Sistema de Archivos Distribuido (DFSha) en AWS, incluyendo la habilitación de memoria Swap para instancias de bajos recursos, la inicialización de nodos y las pruebas de rendimiento, tolerancia a fallos y concurrencia.

## 1. Configuración de Infraestructura (Requisito para instancias t2.micro)

Para evitar errores de memoria (`OOMKilled`) al manejar archivos pesados, se habilita 2 GB de memoria Swap en las instancias de AWS.

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
```

## 2. Despliegue del Servidor de Metadatos (MDS)

Se ejecuta en la máquina designada como MDS (ej.: `172.31.34.188`). Se encarga de la base de datos de rutas, bloqueos y asignación de nodos.

```bash
cd ~/dfsha
source venv/bin/activate
export PYTHONPATH=.

# Limpiar base de datos y puertos ocupados (si aplica)
rm -f dfsha.db
fuser -k 8443/tcp 2>/dev/null || true

# Ejecutar en segundo plano
nohup uvicorn app.main:app --host 0.0.0.0 --port 8443 > mds.log 2>&1 &
```

## 3. Despliegue de los Data Nodes (DN)

Se levantan 3 nodos de almacenamiento local para garantizar la replicación. Se ejecutan en terminales separadas dentro de la instancia de nodos.

**Data Node 1 (puerto 8001):**

```bash
cd ~/dfsha && source venv/bin/activate
export PYTHONPATH=.
rm -rf storage_dn1/*
MDS_URL="http://172.31.34.188:8443" DN_ID=dn1 DN_PORT=8001 uvicorn dn.app.main:app --host 0.0.0.0 --port 8001
```

**Data Node 2 (puerto 8002):**

```bash
cd ~/dfsha && source venv/bin/activate
export PYTHONPATH=.
rm -rf storage_dn2/*
MDS_URL="http://172.31.34.188:8443" DN_ID=dn2 DN_PORT=8002 uvicorn dn.app.main:app --host 0.0.0.0 --port 8002
```

**Data Node 3 (puerto 8003):**

```bash
cd ~/dfsha && source venv/bin/activate
export PYTHONPATH=.
rm -rf storage_dn3/*
MDS_URL="http://172.31.34.188:8443" DN_ID=dn3 DN_PORT=8003 uvicorn dn.app.main:app --host 0.0.0.0 --port 8003
```

## 4. Pruebas de Cliente (Operaciones, Rendimiento e Integridad)

Desde la terminal del cliente conectada a la red de los nodos, se genera un archivo de 50 MB, se sube y se descarga, y se valida su integridad.

```bash
cd ~/dfsha && source venv/bin/activate
export PYTHONPATH=.

# Generar archivo de prueba de 50 MB
dd if=/dev/urandom of=archivo_50mb.bin bs=1M count=50

# Prueba de subida
time python src/dfsha/client.py upload archivo_50mb.bin /archivo_50mb.bin --mds http://172.31.34.188:8443

# Prueba de descarga
time python src/dfsha/client.py download /archivo_50mb.bin descargado_50mb.bin --mds http://172.31.34.188:8443

# Validación de integridad (los hashes deben coincidir)
sha256sum archivo_50mb.bin descargado_50mb.bin
```

## 5. Prueba de Tolerancia a Fallos

Se apaga intencionalmente uno de los Data Nodes (ej.: `Ctrl + C` en la terminal del Nodo 3) y se fuerza una nueva descarga para comprobar que el sistema recupera los bloques desde las réplicas restantes.

```bash
time python src/dfsha/client.py download /archivo_50mb.bin recuperado_fallo.bin --mds http://172.31.34.188:8443
sha256sum archivo_50mb.bin recuperado_fallo.bin
```

## 6. Prueba de Concurrencia (File Locking)

Se simulan dos peticiones de escritura simultáneas sobre la misma ruta para validar el mecanismo de exclusión mutua. El segundo proceso debe ser rechazado.

```bash
# Lanzar el primer proceso en segundo plano e intentar escribir de inmediato en la misma ruta
python src/dfsha/client.py upload archivo_50mb.bin /archivo_lock_test.bin --mds http://172.31.34.188:8443 & \
python src/dfsha/client.py upload archivo_50mb.bin /archivo_lock_test.bin --mds http://172.31.34.188:8443
```

El sistema responde: `[Lock] El archivo esta bloqueado por otro proceso.`
