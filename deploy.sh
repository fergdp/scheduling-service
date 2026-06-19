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

# 1. Entorno Virtual — usar el pip/alembic del venv por PATH ABSOLUTO, NO 'source activate'.
# CAUSA RAÍZ del incidente #75: el venv de /opt fue COPIADO desde /root/app en la migración
# del #33 (cp -a, May 4). Sus shebangs bin/* se corrigieron a /opt, pero el script
# 'activate' quedó con VIRTUAL_ENV=/root/app -> 'source venv/bin/activate' + pip instalaba
# las deps en /root/app/.../venv, mientras gunicorn (supervisor) corre /opt/.../venv (stale).
# Dos venvs divergentes: las deps iban a uno y el runtime usaba el otro -> ModuleNotFoundError.
# El pip ABSOLUTO de /opt instala en /opt (sys.prefix sale del pyvenv.cfg de /opt). Sin activate.
VENV_BIN="$APP_DIR/venv/bin"
if [ ! -x "$VENV_BIN/python" ]; then
    python3 -m venv "$APP_DIR/venv"
fi

# 2. Dependencias — con el pip ABSOLUTO de /opt (el venv que usa gunicorn). DEBE abortar si
# falla: corre ANTES de parar el servicio, así un fallo deja el servicio viejo en pie + CI rojo.
"$VENV_BIN/pip" install -r requirements.txt || {
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

# 4. MIGRACIONES AUTOMÁTICAS (alembic del venv de /opt, path absoluto). Tests ya
# corrieron en CI antes del SCP. Las vars del .env (DATABASE_URL) se exportaron arriba.
echo "Aplicando migraciones automáticas a la base de datos..."
"$VENV_BIN/alembic" upgrade head || { echo "Error: Fallaron las migraciones."; exit 1; }

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

echo "Despliegue de scheduling-service completado con éxito."
