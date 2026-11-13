"""Catálogo de claims y matriz inicial de roles (doc 07, Plan V3.0 §8).

Se copia a la colección `roles` al arrancar solo si el rol no existe; a partir de ahí la fuente de verdad es la BD.
Debe mantenerse alineado con lib/security.ts del frontend.
"""

SYSTEM_ROLES = ["admin", "editor", "consulta", "auditor"]

ALL_CLAIMS = [
    {"id": "dashboard:read", "name": "Tableros Ejecutivos", "category": "redaccion", "description": "Acceso a tableros, radar de poderes y desglose analítico"},
    {"id": "news:read", "name": "Lectura de Prensa", "category": "redaccion", "description": "Lectura de noticias, artículos y editoriales procesados"},
    {"id": "news:edit", "name": "Edición Periodística", "category": "redaccion", "description": "Corrección de titulares, cuerpo de noticia, autor y medios"},
    {"id": "news:review", "name": "Revisión y Triage", "category": "redaccion", "description": "Revisar noticias dudosas, baja confianza y clasificación de impacto"},
    {"id": "news:approve", "name": "Aprobación Institucional", "category": "redaccion", "description": "Dar visto bueno oficial a resúmenes y alertas presidenciales"},
    {"id": "documents:read", "name": "Visor de Resúmenes", "category": "documental", "description": "Visualizar páginas extraídas en visor streaming sin enlace directo"},
    {"id": "documents:upload", "name": "Carga de Documentos", "category": "documental", "description": "Subir resúmenes de prensa en formato PDF al sistema"},
    {"id": "documents:download", "name": "Descarga de PDF Original", "category": "documental", "description": "Descargar archivo binario original (Exclusivo Admin/Editor; denegado a Consulta/Auditor)"},
    {"id": "catalogs:manage", "name": "Catálogos de Medios y Cargos", "category": "gobernanza", "description": "Administrar medios, alias y el catálogo de cargos y funcionarios"},
    {"id": "topics:manage", "name": "Catálogo de Tópicos", "category": "gobernanza", "description": "Administrar la taxonomía de los 32 tópicos provisorios"},
    {"id": "triage:calibrate", "name": "Calibración de Triaje", "category": "gobernanza", "description": "Ajustar ponderaciones de factores de criticidad (V0.3 Simulación)"},
    {"id": "reports:read", "name": "Consulta de Reportes", "category": "redaccion", "description": "Consultar reportes e indicadores del período"},
    {"id": "reports:export", "name": "Exportación de Reportes", "category": "redaccion", "description": "Generar y descargar el Reporte Presidencial en PDF (auditado)"},
    {"id": "audit:read", "name": "Trazas de Auditoría", "category": "seguridad", "description": "Consultar registro secuencial inmutable de auditoría"},
    {"id": "users:manage", "name": "Gestión de Usuarios y Roles", "category": "seguridad", "description": "Crear funcionarios, modificar roles y asignar/revocar claims"},
    {"id": "system:reprocess", "name": "Reprocesamiento", "category": "seguridad", "description": "Forzar reextracción determinista de PDFs y recalibración de SimHash"},
    {"id": "config:read", "name": "Lectura de Parámetros", "category": "configuracion", "description": "Ver parámetros configurados de correo SMTP y servidores de ingesta FTP"},
    {"id": "config:manage", "name": "Gestión de Configuración", "category": "configuracion", "description": "Actualizar servidores SMTP, llaves FTP/SFTP y probar conectividad"},
]
CLAIM_IDS = {c["id"] for c in ALL_CLAIMS}

ROLE_CLAIMS: dict[str, list[str]] = {
    "admin": [
        "dashboard:read", "news:read", "documents:read", "documents:download", "documents:upload",
        "news:edit", "news:review", "news:approve", "topics:manage", "triage:calibrate",
        "audit:read", "users:manage", "system:reprocess", "config:manage", "config:read",
        "reports:read", "reports:export", "catalogs:manage",
    ],
    "editor": [
        "dashboard:read", "news:read", "documents:read", "documents:download", "documents:upload",
        "news:edit", "news:review", "news:approve", "reports:read", "reports:export",
    ],
    "consulta": ["dashboard:read", "news:read", "documents:read", "reports:read"],
    "auditor": ["dashboard:read", "news:read", "documents:read", "audit:read", "reports:read"],
}

# El rol admin no puede perder estos claims (evita dejar el sistema sin administración).
ADMIN_LOCKED_CLAIMS = ["users:manage", "config:manage", "audit:read"]

# Migraciones de claims para roles ya existentes en BD (se aplican una sola vez; Plan V3.0 §8).
CLAIM_MIGRATIONS = [
    ("claims_reports_v1", {
        "admin": ["reports:read", "reports:export"],
        "editor": ["reports:read", "reports:export"],
        "consulta": ["reports:read"],
        "auditor": ["reports:read"],
    }),
    ("claims_catalogs_v1", {"admin": ["catalogs:manage"]}),
]
