"""
Subida de imágenes de evidencia (fotos que acompañan a un error o
amonestación) al bucket 'evidencias' de Supabase Storage.
"""

import uuid

BUCKET = "evidencias"


def subir_evidencia(supabase, empleado_id, archivo):
    """Sube una imagen (un UploadedFile de st.file_uploader) y devuelve
    su URL pública. Lanza una excepción si falla."""
    extension = archivo.name.split(".")[-1].lower() if "." in archivo.name else "jpg"
    ruta = f"{empleado_id}/{uuid.uuid4().hex}.{extension}"

    supabase.storage.from_(BUCKET).upload(
        ruta,
        archivo.getvalue(),
        {"content-type": archivo.type or "image/jpeg"},
    )
    return supabase.storage.from_(BUCKET).get_public_url(ruta)
