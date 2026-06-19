#!/bin/bash
# Deploy script de scheduling-service — AHORA VERSIONADO EN EL REPO.
#
# Antes el deploy real era ~/deploy_scheduling.sh (un script suelto en el VPS, no
# versionado). El CI ahora corre ESTE archivo (deployado en /opt/scheduling-service/
# por el unzip del workflow). Port fiel de ese script + endurecimientos:
#   - pip install aborta el deploy si falla (antes seguía con el venv stale y
#     reiniciaba el servicio con deps faltantes -> crash-loop con CI en verde).
#     Causa raíz del incidente del 2026-06-19 (deploy de métricas #75).
#   - Health check post-restart: si el servicio no levanta, el deploy FALLA (rojo)
#     en vez de quedar verde con el servicio caído.
#   - set +x alrededor del JWT para no filtrar el secret al log del CI.
set -x

# --- Configuración ---
APP_DIR="/opt/scheduling-service"
APP_USER="appuser"
VENV_NAME="venv"
APP_PORT=8002
GUNICORN_PROCESS_NAME="scheduling-service-gunicorn"
# El supervisor real del VPS lee desde /root/supervisor_configs/, no /etc/supervisor/conf.d/.
SUPERVISOR_CONF_FILE="/root/supervisor_configs/scheduling-service.conf"

echo "Iniciando despliegue de scheduling-service..."

export APP_VERSION=${APP_VERSION:-1.0.0}

# Install python3.12-venv if not already installed
if ! dpkg -s python3.12-venv >/dev/null 2>&1; then
    sudo apt update && sudo apt install -y python3.12-venv
fi

cd "$APP_DIR" || exit 1

# 1. Entorno Virtual
if [ ! -d "$VENV_NAME" ]; then
    python3 -m venv "$VENV_NAME"
fi
source "$VENV_NAME/bin/activate"

# 2. Dependencias — DEBE abortar si falla. Esto corre ANTES de parar el servicio,
# así que si el pip install falla, el servicio EN EJECUCIÓN queda intacto y el CI
# se pone en rojo (antes seguía y reiniciaba con el venv stale -> crash-loop).
pip install -r requirements.txt || {
    echo "ERROR: 'pip install -r requirements.txt' falló. Abortando deploy; el servicio en ejecución NO se toca." >&2
    exit 1
}

# 3. Cargar .env para que alembic levante DATABASE_URL al correr migraciones.
# El zip del workflow excluye .env (vive en VPS, no en repo); existe en $APP_DIR.
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# 4. MIGRACIONES AUTOMÁTICAS (Alembic). Tests ya corrieron en CI antes del SCP.
echo "Aplicando migraciones automáticas a la base de datos..."
alembic upgrade head || { echo "Error: Fallaron las migraciones."; exit 1; }

# 5. Ownership: el unzip + pip install + alembic crean archivos como root.
sudo chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# 6. Detener servicio
sudo supervisorctl stop scheduling-service || true
sleep 5

# 7. Actualizar Configuración (refresca APP_VERSION y reinyecta el JWT vigente;
# preserva el resto del conf, incl. --config gunicorn_conf.py y PROMETHEUS_MULTIPROC_DIR).
sudo sed -i -E "s/(APP_VERSION=\")[^\"]*(\")/\1$APP_VERSION\2/" "$SUPERVISOR_CONF_FILE"

# Inyectar JWT_SECRET_KEY actual desde /root/dental_system/prod.env (source of truth).
# set +x para NO filtrar el secret al log del CI.
set +x
JWT_FROM_PROD=$(grep -E "^JWT_SECRET_KEY=" /root/dental_system/prod.env | cut -d= -f2-)
JWT_FROM_PROD="${JWT_FROM_PROD%\"}"; JWT_FROM_PROD="${JWT_FROM_PROD#\"}"
JWT_FROM_PROD="${JWT_FROM_PROD%\'}"; JWT_FROM_PROD="${JWT_FROM_PROD#\'}"
if [ -z "$JWT_FROM_PROD" ]; then echo "ERROR: JWT_SECRET_KEY no encontrado en prod.env" >&2; exit 1; fi
sudo sed -i -E "s|(JWT_SECRET_KEY=\")[^\"]*(\")|\1${JWT_FROM_PROD}\2|" "$SUPERVISOR_CONF_FILE"
set -x

# 8. Reiniciar Servicio
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl start scheduling-service || exit 1

# 9. Health check: confirmar que el servicio LEVANTÓ y se quedó arriba.
# 'supervisorctl start' devuelve OK apenas lanza el proceso; no garantiza que no
# entre en crash-loop. Si /health/live no responde, el deploy FALLA (rojo) en vez
# de quedar verde con el servicio caído.
echo "Verificando que scheduling-service levantó (/health/live)..."
HEALTHY=0
for i in 1 2 3 4 5 6 7 8; do
    if curl -fsS "http://127.0.0.1:${APP_PORT}/health/live" >/dev/null 2>&1; then
        HEALTHY=1
        break
    fi
    sleep 3
done
if [ "$HEALTHY" != "1" ]; then
    echo "ERROR: scheduling-service no respondió /health/live tras el deploy (posible crash-loop)." >&2
    sudo supervisorctl status scheduling-service || true
    sudo supervisorctl tail scheduling-service stderr || true
    exit 1
fi

deactivate
echo "Despliegue de scheduling-service completado con éxito."
