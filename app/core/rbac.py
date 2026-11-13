"""Catálogo de claims y matriz inicial de roles (doc 07, Plan V3.0 §8).

Cada acción del sistema (opción de menú, botón, exportación) exige un claim propio, que se asigna o quita a un rol
desde la pantalla Roles. La interfaz recibe este catálogo desde GET /roles (no tiene una copia propia).

Los roles se copian a la colección `roles` al arrancar solo si no existen; a partir de ahí la fuente de verdad es la
BD. Los cambios del catálogo se aplican a roles existentes con migraciones (CLAIM_MIGRATIONS y CLAIM_SPLITS).
"""

SYSTEM_ROLES = ["admin", "editor", "consulta", "auditor"]

ALL_CLAIMS = [
    # --- Redacción y consulta
    {"id": "dashboard:read", "name": "Resumen General", "category": "redaccion", "description": "Ver el Resumen General del período (cifras y distribuciones)"},
    {"id": "news:read", "name": "Lectura de noticias", "category": "redaccion", "description": "Explorador de noticias, ficha de la noticia y Radar de Tendencias"},
    {"id": "reports:presidential", "name": "Reporte Presidencial", "category": "redaccion", "description": "Generar el Reporte Presidencial en PDF (auditado)"},
    {"id": "reports:export", "name": "Reportes por medio y por tema", "category": "redaccion", "description": "Generar los reportes PDF por medio y por tema (auditado)"},
    {"id": "reports:export_csv", "name": "Exportar CSV", "category": "redaccion", "description": "Exportar el listado filtrado de noticias a CSV (auditado)"},
    # --- Revisión humana (doble control)
    {"id": "news:review", "name": "Mesa de Revisión", "category": "revision", "description": "Ver la bandeja, tomar y liberar noticias, confirmar sin cambios y proponer correcciones"},
    {"id": "news:approve", "name": "Aprobar correcciones", "category": "revision", "description": "Aprobar o devolver correcciones propuestas por otra persona"},
    {"id": "review:reject", "name": "Rechazar piezas", "category": "revision", "description": "Rechazar una pieza en revisión (queda marcada como rechazada)"},
    {"id": "review:history_read", "name": "Historial de correcciones", "category": "revision", "description": "Ver quién propuso y aprobó cada corrección de una noticia"},
    {"id": "review:sampling_manage", "name": "Muestreo de calidad", "category": "revision", "description": "Ajustar el porcentaje de muestreo de la Mesa de Revisión"},
    {"id": "review:edit_metadata", "name": "Corregir metadatos", "category": "revision", "description": "Proponer correcciones de titular, medio, sección, tipo, destaque, autor y página"},
    {"id": "review:edit_dates", "name": "Corregir fechas", "category": "revision", "description": "Proponer correcciones de la fecha de publicación"},
    {"id": "review:edit_text", "name": "Corregir texto", "category": "revision", "description": "Proponer el texto corregido de la noticia (el original se conserva)"},
    {"id": "review:edit_topics", "name": "Corregir temas", "category": "revision", "description": "Asignar o quitar temas del catálogo a una noticia"},
    {"id": "review:edit_links", "name": "Corregir vínculos", "category": "revision", "description": "Confirmar, deshacer o crear vínculos de duplicado y relacionadas"},
    # --- Documentos
    {"id": "documents:read", "name": "Visor de resúmenes", "category": "documental", "description": "Ver las páginas de los resúmenes (imagen con marca de agua, sin descarga)"},
    {"id": "documents:upload", "name": "Carga de PDF", "category": "documental", "description": "Pantalla de Carga e Ingesta y subida manual de resúmenes PDF"},
    {"id": "documents:ingest_scan", "name": "Escanear carpeta/SFTP", "category": "documental", "description": "Ejecutar en el acto la revisión de la carpeta o del SFTP de ingesta"},
    {"id": "documents:download", "name": "Descargar PDF original", "category": "documental", "description": "Descargar el archivo PDF original cuando está disponible (auditado)"},
    {"id": "system:reprocess", "name": "Reprocesar documentos", "category": "documental", "description": "Volver a extraer un documento fallido o con advertencias"},
    # --- Gobernanza (catálogos y reglas)
    {"id": "catalogs:read", "name": "Consulta de catálogos", "category": "gobernanza", "description": "Ver temas, triage, medios, cargos y versiones de reglas"},
    {"id": "catalogs:media_manage", "name": "Medios y alias", "category": "gobernanza", "description": "Crear y editar medios de comunicación y sus alias"},
    {"id": "catalogs:officeholders_manage", "name": "Cargos y funcionarios", "category": "gobernanza", "description": "Crear y editar cargos y funcionarios del catálogo"},
    {"id": "topics:manage", "name": "Editar temas", "category": "gobernanza", "description": "Editar los temas y sinónimos del borrador de reglas"},
    {"id": "triage:calibrate", "name": "Calibrar triage", "category": "gobernanza", "description": "Editar factores, pesos y umbrales de triage del borrador"},
    {"id": "rules:draft_manage", "name": "Borradores de reglas", "category": "gobernanza", "description": "Crear y descartar borradores de reglas y editar sus notas"},
    {"id": "rules:approve", "name": "Aprobar reglas", "category": "gobernanza", "description": "Aprobar un borrador y ponerlo en vigor (no puede aprobar quien lo editó por última vez)"},
    # --- Seguridad
    {"id": "audit:read", "name": "Libro de auditoría", "category": "seguridad", "description": "Consultar el registro de auditoría"},
    {"id": "audit:read_detail", "name": "Detalle de auditoría", "category": "seguridad", "description": "Ver el detalle técnico (JSON) de cada registro de auditoría"},
    {"id": "audit:verify", "name": "Verificar integridad", "category": "seguridad", "description": "Verificar la cadena de firmas de la auditoría"},
    {"id": "users:read", "name": "Ver usuarios", "category": "seguridad", "description": "Ver la lista de usuarios, sus roles y su estado"},
    {"id": "users:create", "name": "Crear usuarios", "category": "seguridad", "description": "Crear cuentas de funcionarios"},
    {"id": "users:update", "name": "Editar usuarios", "category": "seguridad", "description": "Cambiar el nombre de un usuario"},
    {"id": "users:assign_roles", "name": "Asignar roles", "category": "seguridad", "description": "Asignar o quitar roles a un usuario"},
    {"id": "users:deactivate", "name": "Activar o desactivar", "category": "seguridad", "description": "Cambiar el estado de una cuenta (desactivar cierra sus sesiones)"},
    {"id": "users:reset_password", "name": "Reiniciar claves", "category": "seguridad", "description": "Generar una clave temporal (cierra las sesiones del usuario)"},
    {"id": "roles:manage", "name": "Roles y permisos", "category": "seguridad", "description": "Asignar y quitar claims a los roles"},
    # --- Configuración
    {"id": "config:read", "name": "Ver parámetros", "category": "configuracion", "description": "Ver los parámetros de SMTP y SFTP"},
    {"id": "config:smtp_manage", "name": "Editar SMTP", "category": "configuracion", "description": "Modificar los parámetros del servidor de correo"},
    {"id": "config:sftp_manage", "name": "Editar SFTP", "category": "configuracion", "description": "Modificar los parámetros del servidor SFTP de ingesta"},
    {"id": "config:test_connection", "name": "Probar conexiones", "category": "configuracion", "description": "Probar la conexión con los servidores SMTP y SFTP"},
]
CLAIM_IDS = {c["id"] for c in ALL_CLAIMS}

_ADMIN = sorted(CLAIM_IDS)
_EDITOR = [
    "dashboard:read", "news:read", "reports:presidential", "reports:export", "reports:export_csv",
    "news:review", "news:approve", "review:reject", "review:history_read",
    "review:edit_metadata", "review:edit_dates", "review:edit_text", "review:edit_topics", "review:edit_links",
    "documents:read", "documents:upload", "documents:ingest_scan", "documents:download", "catalogs:read",
]
ROLE_CLAIMS: dict[str, list[str]] = {
    "admin": _ADMIN,
    "editor": _EDITOR,
    "consulta": ["dashboard:read", "news:read", "documents:read", "catalogs:read"],
    "auditor": ["dashboard:read", "news:read", "documents:read", "catalogs:read", "review:history_read",
                "audit:read", "audit:read_detail", "audit:verify"],
}

# El rol admin no puede perder estos claims (evita dejar el sistema sin administración).
ADMIN_LOCKED_CLAIMS = ["roles:manage", "users:read", "users:assign_roles", "audit:read"]

# Claims que solo puede conceder (a un rol) quien tiene el rol admin: con ellos se administra la seguridad.
PRIVILEGED_CLAIMS = {"roles:manage", "users:create", "users:assign_roles", "users:deactivate", "users:reset_password",
                     "config:smtp_manage", "config:sftp_manage"}

# Migraciones históricas (solo agregan). Se aplican una sola vez por BD, en orden, antes de CLAIM_SPLITS.
CLAIM_MIGRATIONS = [
    ("claims_reports_v1", {
        "admin": ["reports:read", "reports:export"],
        "editor": ["reports:read", "reports:export"],
        "consulta": ["reports:read"],
        "auditor": ["reports:read"],
    }),
    ("claims_catalogs_v1", {"admin": ["catalogs:manage"]}),
    ("claims_review_v1", {"admin": ["review:edit_metadata", "review:edit_dates", "review:edit_text", "review:edit_topics", "review:edit_links"], "editor": ["review:edit_metadata", "review:edit_dates", "review:edit_text", "review:edit_topics", "review:edit_links"]}),
]

# Un claim por acción (2026-11): cada rol que tenía el claim amplio recibe los específicos que lo reemplazan,
# conservando lo que podía hacer. Después se quitan los claims que ya no existen en el catálogo.
# (condición: claims que el rol debe tener TODOS) → claims que se agregan
CLAIM_SPLITS = [
    ("claims_split_v2", [
        (["reports:export"], ["reports:presidential", "reports:export", "reports:export_csv"]),
        (["catalogs:manage"], ["catalogs:media_manage", "catalogs:officeholders_manage"]),
        (["dashboard:read"], ["catalogs:read"]),
        (["topics:manage"], ["rules:draft_manage"]),
        (["topics:manage", "triage:calibrate"], ["rules:approve"]),
        (["news:review", "news:approve"], ["review:reject"]),
        (["news:review"], ["review:history_read"]),
        (["audit:read"], ["review:history_read", "audit:read_detail", "audit:verify"]),
        (["config:manage"], ["config:smtp_manage", "config:sftp_manage", "config:test_connection", "review:sampling_manage"]),
        (["documents:upload"], ["documents:ingest_scan"]),
        (["users:manage"], ["users:read", "users:create", "users:update", "users:assign_roles", "users:deactivate",
                            "users:reset_password", "roles:manage"]),
    ]),
]
