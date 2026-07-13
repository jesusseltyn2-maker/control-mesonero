"""
Conexión a Supabase.
Usa la SERVICE_ROLE KEY (no la anon key) porque la app maneja su
propio sistema de usuarios/contraseñas y necesita leer/escribir
en las tablas sin depender del sistema de Auth nativo de Supabase.

IMPORTANTE: la service_role key NUNCA debe llegar al navegador del
usuario. Aquí es seguro porque Streamlit ejecuta este código en el
servidor, y la key se lee desde `st.secrets`, que no se envía al
cliente.
"""

import streamlit as st
from supabase import create_client, Client


@st.cache_resource(show_spinner=False)
def get_supabase_client() -> Client:
    url = st.secrets["SUPABASE_URL"]
    key = st.secrets["SUPABASE_KEY"]
    return create_client(url, key)
