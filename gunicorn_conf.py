"""Configuración de gunicorn — hooks para métricas Prometheus multiproceso (issue #75).

Con gunicorn -w N cada worker tiene su propio registro de métricas. prometheus_client
agrega entre procesos vía PROMETHEUS_MULTIPROC_DIR (seteada en el supervisor.conf).
Estos hooks mantienen ese directorio sano. Sin la env var (local/test) son no-op.
"""
import os
import shutil

from prometheus_client import multiprocess


def on_starting(server):
    multiproc_dir = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if multiproc_dir:
        shutil.rmtree(multiproc_dir, ignore_errors=True)
        os.makedirs(multiproc_dir, exist_ok=True)


def child_exit(server, worker):
    multiprocess.mark_process_dead(worker.pid)
