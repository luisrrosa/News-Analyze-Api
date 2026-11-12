"""Etiquetas legibles de acciones de auditoría (generado desde lib/security.ts del frontend)."""

ACTION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "SYSTEM_BOOTSTRAP": {
        "label": "Inicialización del Sistema",
        "description": "Arranque del núcleo y configuración de directivas institucionales base.",
        "category": "Sistema"
    },
    "USERS_SEEDED": {
        "label": "Carga de Cuentas Demo",
        "description": "Generación de cuentas de demostración para validación de matriz de roles.",
        "category": "Seguridad"
    },
    "DOC_PARSE_COMPLETE": {
        "label": "Procesamiento de Resumen PDF",
        "description": "Segmentación determinista de noticias, artículos y editoriales del resumen.",
        "category": "Documental"
    },
    "DOC_PAGES_RENDER_ACCESSED": {
        "label": "Consulta Streaming de Página",
        "description": "Visualización de página de resumen en modo streaming sin descarga de archivo.",
        "category": "Consulta"
    },
    "AUTH_LOGIN_FAILED": {
        "label": "Inicio de Sesión Fallido",
        "description": "Credenciales inválidas o cuenta inexistente.",
        "category": "Seguridad"
    },
    "AUTH_LOGIN_BLOCKED": {
        "label": "Inicio de Sesión Bloqueado",
        "description": "Cuenta inactiva o bloqueada temporalmente por intentos fallidos.",
        "category": "Seguridad"
    },
    "AUTH_LOGOUT": {
        "label": "Cierre de Sesión",
        "description": "El usuario cerró su sesión y el token quedó revocado.",
        "category": "Seguridad"
    },
    "AUTH_PASSWORD_CHANGED": {
        "label": "Cambio de Contraseña",
        "description": "El usuario cambió su contraseña.",
        "category": "Seguridad"
    },
    "ACCESS_DENIED": {
        "label": "Acceso Denegado",
        "description": "Intento de operación sin el claim requerido.",
        "category": "Seguridad"
    },
    "BOOTSTRAP_ADMIN_CREATED": {
        "label": "Administrador Inicial Creado",
        "description": "Alta del administrador inicial desde variables de entorno.",
        "category": "Seguridad"
    },
    "AUDIT_INTEGRITY_VERIFIED": {
        "label": "Verificación de Integridad",
        "description": "Recorrido y verificación de la cadena HMAC de auditoría.",
        "category": "Seguridad"
    },
    "AUTH_LOGIN_SUCCESS": {
        "label": "Inicio de Sesión Cifrada",
        "description": "Autenticación exitosa y generación de token de sesión temporal.",
        "category": "Seguridad"
    },
    "USER_CREATED": {
        "label": "Creación de Usuario Institucional",
        "description": "Alta de nuevo funcionario con asignación de rol y credencial.",
        "category": "Seguridad"
    },
    "USER_ROLE_CLAIMS_MODIFIED": {
        "label": "Modificación de Usuario",
        "description": "Actualización de roles, estado o claims específicos de la cuenta.",
        "category": "Seguridad"
    },
    "PASSWORD_RESET_REQUESTED": {
        "label": "Restablecimiento de Clave",
        "description": "Un administrador emitió una clave temporal; el usuario debe cambiarla al entrar.",
        "category": "Seguridad"
    },
    "ROLE_CLAIMS_UPDATED": {
        "label": "Actualización Matriz de Claims",
        "description": "Modificación de los permisos y claims institucionales asociados a un rol.",
        "category": "Seguridad"
    },
    "CONFIG_SMTP_UPDATED": {
        "label": "Configuración Servidor SMTP",
        "description": "Actualización de parámetros del servidor de correo saliente para alertas.",
        "category": "Configuración"
    },
    "CONFIG_FTP_UPDATED": {
        "label": "Configuración Servidor FTP/SFTP",
        "description": "Actualización de credenciales y ruta remota de ingesta de archivos PDF.",
        "category": "Configuración"
    },
    "FTP_CONNECTION_TEST": {
        "label": "Prueba Conectividad SFTP",
        "description": "Verificación de handshake TLS/SSH y listado del directorio remoto.",
        "category": "Configuración"
    },
    "NEWS_EXTRACTION_CONFIRMED": {
        "label": "Extracción de Noticia Confirmada",
        "description": "Validación determinista del titular, medio, sección y URL de la noticia.",
        "category": "Prensa"
    },
    "NEWS_REVIEW_QUEUE_ACCESSED": {
        "label": "Apertura Mesa de Revisión",
        "description": "Acceso a la bandeja de noticias con segmentación dudosa o citas atípicas.",
        "category": "Prensa"
    },
    "NEWS_ITEM_APPROVED": {
        "label": "Aprobación de Noticia en Mesa",
        "description": "Visto bueno editorial a noticia en cola para su inclusión analítica.",
        "category": "Prensa"
    },
    "NEWS_ITEM_DISCARDED": {
        "label": "Descarte de Noticia en Mesa",
        "description": "Descarte justificado de elemento no noticioso o fragmento duplicado.",
        "category": "Prensa"
    },
    "PDF_DOWNLOAD_DENIED": {
        "label": "Descarga Bloqueada (RBAC)",
        "description": "Intento de descarga de archivo binario PDF bloqueado por falta de claim.",
        "category": "Seguridad"
    },
    "PDF_DOWNLOAD_AUTHORIZED": {
        "label": "Descarga de PDF Autorizada",
        "description": "Descarga del archivo original ejecutada con credenciales autorizadas.",
        "category": "Documental"
    }
}


def human_readable(action: str) -> dict[str, str]:
    return ACTION_DESCRIPTIONS.get(action) or {
        "label": action.replace("_", " "),
        "description": "Evento registrado en el libro inmutable de auditoría.",
        "category": "Operación",
    }
