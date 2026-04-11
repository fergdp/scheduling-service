# Dental Clinic Scheduling Service (Python/FastAPI)

Este microservicio gestiona las agendas de los odontólogos, la disponibilidad en tiempo real y la sincronización con **Google Calendar**. Forma parte del ecosistema de gestión dental SaaS.

## 🚀 Características
- **Multi-tenant:** Aislamiento estricto de datos por `clinic_id`.
- **Google Calendar Sync:** Los odontólogos vinculan su agenda y reciben los turnos aprobados directamente en su teléfono.
- **Disponibilidad Inteligente:** Combina bloques ocupados de Google con solicitudes locales pendientes para evitar colisiones.
- **Seguridad "Expert Grade":** JWT, CSRF, encriptación AES-256 de tokens y Rate Limiting.
- **Base de Datos Automatizada:** Usa Alembic para migraciones "Code-First". No requiere tocar SQL.

## 🛠️ Requisitos
- Python 3.12+
- MySQL 8.0
- Proyecto en Google Cloud Console (Calendar API + OAuth2)

## 📦 Instalación Local
1. Clona el repositorio.
2. Crea el entorno virtual:
   ```bash
   python -m venv venv
   .\venv\Scripts\activate  # Windows
   source venv/bin/activate  # Linux/Mac
   ```
3. Instala las dependencias:
   ```bash
   pip install -r requirements.txt
   ```
4. Configura el archivo `.env` (usa `.env.example` como guía).
5. Crea la base de datos `dental_scheduling_db` en MySQL.
6. Aplica las migraciones:
   ```bash
   alembic upgrade head
   ```

## 🖥️ Ejecución
```bash
uvicorn main:app --port 8002 --reload
```
La documentación Swagger estará disponible en: `http://localhost:8002/docs`

## ☁️ Despliegue (Production)
El servicio está diseñado para correr con **Supervisor + Gunicorn** en el puerto **8002**.
1. Copia `scheduling-service.conf` a `/etc/supervisor/conf.d/`.
2. Usa el script `deploy_scheduling_service.sh` para automatizar las actualizaciones.

## 🛡️ Seguridad
- **JWT:** Compatible con los tokens emitidos por el backend de Java.
- **Encryption:** Los tokens de Google se encriptan con Fernet usando la variable `FERNET_KEY`.
- **SQL Guard:** Inyección automática de filtros de `clinic_id` en las consultas de SQLAlchemy.

## 📄 Licencia
Privado - Sistema Dental SaaS.
