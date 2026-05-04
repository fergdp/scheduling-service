#!/bin/bash
set -x 

# --- Configuración (Idéntica a tu salud pero con scheduling-service) ---
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

# 2. Dependencias
pip install -r requirements.txt

# 3. Cargar .env para que alembic levante DATABASE_URL al correr migraciones.
# El zip del workflow excluye .env (vive en VPS, no en repo); existe en $APP_DIR.
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# 4. MIGRACIONES AUTOMÁTICAS (Alembic). Tests ya corrieron en CI antes del SCP
# (step "Run tests" del workflow); no se re-corren acá porque el zip excluye tests/.
echo "Aplicando migraciones automáticas a la base de datos..."
alembic upgrade head || { echo "Error: Fallaron las migraciones."; exit 1; }

# 5. Ownership: el unzip + pip install + alembic crean archivos como root.
# Restaurar ownership a appuser para que gunicorn pueda leerlos al arrancar.
sudo chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# 6. Detener servicio
sudo supervisorctl stop scheduling-service || true
sleep 5

# 7. Actualizar Configuración (Actualiza APP_VERSION y mantiene las otras)
sudo sed -i -E "s/(APP_VERSION=\")[^\"]*(\")/\1$APP_VERSION\2/" "$SUPERVISOR_CONF_FILE"

# 8. Reiniciar Servicio
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl start scheduling-service || exit 1

deactivate
echo "Despliegue de scheduling-service completado con éxito."
