# Revisión arquitectónica — scheduling-service

**Fecha de revisión:** Abril 2026  
**Revisado por:** Claude (arquitecto Python)  
**Estado general:** Funciona en producción, base sólida, deuda técnica real a resolver.

---

## Esta semana — BLOQUEANTES

- [x] Crear `.gitignore` — secretos no deben llegar al repo
- [x] Agregar validadores Pydantic en `AppointmentCreate` (start < end, no pasado)
- [x] Agregar `UNIQUE(dentist_user_id, clinic_id)` en Alembic
- [x] Agregar índice compuesto en `appointments(clinic_id, dentist_user_id, start_time_utc)`
- [x] Rate limiting en POST/PATCH de turnos (no solo en raíz)
- [x] Correr tests en CI/CD antes del deploy

---

## Próxima iteración — IMPORTANTES

- [ ] **RBAC real:** el JWT trae `roles` pero ningún endpoint lo lee. Cualquier usuario autenticado puede aprobar/cancelar turnos ajenos. Agregar `Depends(require_role("DENTIST"))` o similar.
- [ ] **Race condition en reserva:** no hay lock entre consultar disponibilidad y crear turno. Agregar validación de solapamiento en `POST /appointments/` antes de guardar.
- [ ] **Validar state en OAuth callback:** `state` se genera en `/oauth/url` pero nunca se verifica en `/oauth/callback`. Abre CSRF en el flujo OAuth2.
- [ ] **Rollback faltante en sesión DB:** en `dependencies.py:57`, si hay excepción se hace `raise` sin `db.rollback()`. La transacción puede quedar abierta.
- [ ] **Google sync falla silencioso:** si Google no responde, `get_dentist_availability` devuelve `busy_slots: []` en lugar de error. El paciente ve slots libres que no lo están.
- [ ] **Token refresh sin validar:** en `google_calendar.py`, después de hacer `creds.refresh()` no se verifica `creds.valid`. Puede usar un token expirado silenciosamente.
- [ ] **Endpoints faltantes:** no hay `GET /appointments/` (listar turnos) ni `GET /appointments/{id}` (detalle de turno). El frontend no puede mostrar los turnos existentes.
- [ ] **Supervisor corre como `root`:** en `scheduling-service.conf`, `user=root`. Crear usuario sin privilegios `appuser`.
- [ ] **Pool de conexiones sin configurar:** SQLAlchemy default es 5 conexiones. Agregar `pool_size=20, max_overflow=40, pool_timeout=30` en `dependencies.py`.
- [ ] **Lifespan handler faltante:** no hay warmup del pool ni shutdown limpio. Agregar `@app.lifespan`.

---

## Mejoras menores — CUANDO HAYA TIEMPO

- [ ] Separar `requirements.txt` en `requirements.txt` (prod) y `requirements-dev.txt` (test/lint).
- [ ] Eliminar `bleach==6.1.0` de requirements — está instalado pero nunca se usa.
- [ ] Eliminar directorio `alembic_backup/` — es dead code.
- [ ] Mover CORS origins a variable de entorno `CORS_ORIGINS` en lugar de hardcodeados en `main.py`.
- [ ] `load_dotenv()` se llama 3 veces (main.py, dependencies.py, crypto.py) — mover a un solo lugar.
- [ ] Extraer lógica de extracción de JWT del middleware (duplicada en `log_requests` y `exception_handler`).
- [ ] Agregar `--cov` a `pytest.ini` para reportes de cobertura automáticos.
- [ ] Comentarios en español en el código — estandarizar a inglés.
- [ ] Agregar `UNIQUE` en `google_event_id` de appointments (para evitar que el mismo evento de Google se mapee a múltiples turnos).
- [ ] `CHECK CONSTRAINT (start_time_utc < end_time_utc)` a nivel DB (además de la validación en Pydantic).
- [ ] Trace ID / Span ID en logs están inicializados como vacíos pero nunca se populan — implementar propagación de contexto o eliminar los campos.

---

## Lo que está bien — no tocar

- SQL Guard con `ContextVar` para multi-tenancy — previene leaks cross-clinic correctamente.
- Alembic configurado — buena gestión de esquema.
- Fernet para encriptar tokens de Google en DB — nivel correcto de seguridad.
- CSRF double-submit cookie — patrón correcto.
- Logging JSON estructurado + Loki opcional — listo para Grafana.
- Separación de capas (models / schemas / routers / utils) — limpio.
- Pydantic v2 con validación de tipos.
- Suite de tests con fixtures y SQLite in-memory.
- `pool_recycle=3600` para evitar conexiones MySQL muertas.

---

## Notas de arquitectura general

El servicio fue generado por Gemini y **funciona**, pero dejó sin implementar la parte de RBAC (hay un comentario `# Si es DENTIST, verificar...` en `appointments.py:123` pero el código nunca se escribió). Eso es el agujero más serio funcionalmente. Los demás son problemas de robustez y performance en producción.
