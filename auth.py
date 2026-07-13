"""
Autenticación con usuario/contraseña (hash bcrypt) y registro
automático de auditoría (logs_auditoria) para cada acción relevante.
"""

import bcrypt
from db import get_supabase_client


def hash_password(password_plano: str) -> str:
    return bcrypt.hashpw(password_plano.encode(), bcrypt.gensalt()).decode()


def verificar_password(password_plano: str, hash_guardado: str) -> bool:
    try:
        return bcrypt.checkpw(password_plano.encode(), hash_guardado.encode())
    except ValueError:
        return False


def login(nombre_usuario: str, password: str):
    """Devuelve el registro del usuario si las credenciales son válidas
    y el usuario está activo, o None en caso contrario."""
    if not nombre_usuario or not password:
        return None

    supabase = get_supabase_client()
    resp = (
        supabase.table("usuarios")
        .select("*")
        .eq("nombre_usuario", nombre_usuario)
        .eq("activo", True)
        .execute()
    )
    if not resp.data:
        return None

    usuario = resp.data[0]
    if verificar_password(password, usuario["password_hash"]):
        return usuario
    return None


def registrar_log(usuario: dict, accion: str, detalle: str = ""):
    """Inserta una fila de auditoría. Nunca debe romper la app si falla,
    así que cualquier error se silencia (pero en producción real
    conviene mandarlo a un log del servidor)."""
    try:
        supabase = get_supabase_client()
        supabase.table("logs_auditoria").insert(
            {
                "usuario_id": usuario["id"],
                "nombre_usuario": usuario["nombre_completo"],
                "accion": accion,
                "detalle": detalle,
            }
        ).execute()
    except Exception:
        pass
