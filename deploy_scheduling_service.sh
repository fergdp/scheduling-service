#!/bin/bash
set -x 

# --- Configuración (Idéntica a tu salud pero con scheduling-service) ---
APP_DIR="/root/app/scheduling-service"
VENV_NAME="venv"
APP_PORT=8002
GUNICORN_PROCESS_NAME="scheduling-service-gunicorn"
SUPERVISOR_CONF_FILE="/etc/supervisor/conf.d/scheduling-service.conf"

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

# 3. TESTS Y COBERTURA — bloquea deploy si fallan o cobertura < 80%
echo "Ejecutando tests con cobertura..."
python -m pytest tests/ --cov=. --cov-config=.coveragerc -q || {
    echo "Error: Tests fallidos o cobertura insuficiente. Deploy cancelado."
    exit 1
}

# 5. MIGRACIONES AUTOMÁTICAS (La magia de Alembic)
echo "Aplicando migraciones automáticas a la base de datos..."
alembic upgrade head || { echo "Error: Fallaron las migraciones."; exit 1; }

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
