"""
Sistema de Control de Mesoneros
--------------------------------
App interna para 5 evaluadores/administradores que registran los
errores diarios de 15 mesoneros, con auditoría completa y un
dashboard de rating/amonestaciones. Datos persistidos en Supabase.
"""

from datetime import date

import pandas as pd
import streamlit as st

from auth import hash_password, login, registrar_log
from db import get_supabase_client

st.set_page_config(page_title="Control de Mesoneros", page_icon="📋", layout="wide")

MAX_ERRORES_ESTANDAR = 3

if "usuario" not in st.session_state:
    st.session_state.usuario = None


# =================================================================
# LOGIN
# =================================================================
def pantalla_login():
    st.title("📋 Sistema de Control de Mesoneros")
    st.subheader("Iniciar sesión")

    with st.form("login_form"):
        nombre_usuario = st.text_input("Usuario")
        password = st.text_input("Contraseña", type="password")
        enviado = st.form_submit_button("Ingresar", type="primary")

        if enviado:
            usuario = login(nombre_usuario.strip(), password)
            if usuario:
                st.session_state.usuario = usuario
                registrar_log(usuario, "Inició sesión")
                st.rerun()
            else:
                st.error("Usuario o contraseña incorrectos, o el usuario está inactivo.")


def cerrar_sesion():
    registrar_log(st.session_state.usuario, "Cerró sesión")
    st.session_state.usuario = None
    st.rerun()


# =================================================================
# PANEL DIARIO
# =================================================================
def panel_diario(usuario):
    st.header("📋 Panel de Control Diario")
    st.caption(f"Turno del {date.today().strftime('%d/%m/%Y')} — los registros de hoy se ven abajo; el histórico completo está en el Dashboard.")

    supabase = get_supabase_client()
    hoy = date.today().isoformat()

    mesoneros = (
        supabase.table("mesoneros").select("*").eq("activo", True).order("nombre_completo").execute().data
    )

    if not mesoneros:
        st.info("Todavía no hay mesoneros registrados. Pide al Administrador General que los agregue en 'Mesoneros'.")
        return

    for mesonero in mesoneros:
        evals_hoy = (
            supabase.table("evaluaciones")
            .select("*, usuarios(nombre_completo)")
            .eq("mesonero_id", mesonero["id"])
            .eq("fecha", hoy)
            .execute()
            .data
        )
        errores_std = [e for e in evals_hoy if e["tipo"] == "error_estandar"]
        amonestaciones = [e for e in evals_hoy if e["tipo"] == "amonestacion_grave"]

        with st.container(border=True):
            col_info, col_accion = st.columns([2, 3])

            with col_info:
                st.subheader(mesonero["nombre_completo"])
                m1, m2 = st.columns(2)
                m1.metric("Errores hoy", f"{len(errores_std)}/{MAX_ERRORES_ESTANDAR}")
                m2.metric("Amonestaciones graves hoy", len(amonestaciones))

                if errores_std:
                    with st.expander("Ver justificaciones de errores de hoy"):
                        for e in errores_std:
                            evaluador_nombre = (e.get("usuarios") or {}).get("nombre_completo", "N/D")
                            st.caption(f"• *(evaluó: {evaluador_nombre})* — {e['justificacion']}")
                if amonestaciones:
                    with st.expander("Ver amonestaciones graves de hoy"):
                        for e in amonestaciones:
                            evaluador_nombre = (e.get("usuarios") or {}).get("nombre_completo", "N/D")
                            st.caption(f"⚠️ *(evaluó: {evaluador_nombre})* — {e['justificacion']}")

            with col_accion:
                puede_error_estandar = len(errores_std) < MAX_ERRORES_ESTANDAR

                if puede_error_estandar:
                    with st.form(key=f"form_std_{mesonero['id']}", clear_on_submit=True):
                        st.write("Registrar **error estándar**")
                        justificacion = st.text_area(
                            "Justificación obligatoria", key=f"just_std_{mesonero['id']}", height=70
                        )
                        enviado = st.form_submit_button("➕ Registrar error")
                        if enviado:
                            if not justificacion.strip():
                                st.error("La justificación es obligatoria.")
                            else:
                                supabase.table("evaluaciones").insert(
                                    {
                                        "fecha": hoy,
                                        "mesonero_id": mesonero["id"],
                                        "evaluador_id": usuario["id"],
                                        "tipo": "error_estandar",
                                        "justificacion": justificacion.strip(),
                                    }
                                ).execute()
                                registrar_log(
                                    usuario,
                                    "Registró error estándar",
                                    f"{mesonero['nombre_completo']}: {justificacion.strip()}",
                                )
                                st.rerun()
                else:
                    st.warning(
                        f"⚠️ **{mesonero['nombre_completo']}** ya alcanzó el máximo de "
                        f"{MAX_ERRORES_ESTANDAR} errores estándar hoy. El próximo registro "
                        "debe ser una amonestación grave."
                    )
                    with st.form(key=f"form_grave_auto_{mesonero['id']}", clear_on_submit=True):
                        justificacion = st.text_area(
                            "Justificación obligatoria (amonestación grave)",
                            key=f"just_grave_auto_{mesonero['id']}",
                            height=70,
                        )
                        enviado = st.form_submit_button("🚨 Registrar amonestación grave")
                        if enviado:
                            if not justificacion.strip():
                                st.error("La justificación es obligatoria.")
                            else:
                                supabase.table("evaluaciones").insert(
                                    {
                                        "fecha": hoy,
                                        "mesonero_id": mesonero["id"],
                                        "evaluador_id": usuario["id"],
                                        "tipo": "amonestacion_grave",
                                        "justificacion": justificacion.strip(),
                                    }
                                ).execute()
                                registrar_log(
                                    usuario,
                                    "Registró amonestación grave (por exceso de errores)",
                                    f"{mesonero['nombre_completo']}: {justificacion.strip()}",
                                )
                                st.rerun()

                with st.expander("🔴 Registrar amonestación grave directa (falta grave inmediata)"):
                    with st.form(key=f"form_grave_directa_{mesonero['id']}", clear_on_submit=True):
                        justificacion_directa = st.text_area(
                            "Justificación obligatoria", key=f"just_directa_{mesonero['id']}", height=70
                        )
                        enviado_directa = st.form_submit_button("🚨 Registrar falta grave directa")
                        if enviado_directa:
                            if not justificacion_directa.strip():
                                st.error("La justificación es obligatoria.")
                            else:
                                supabase.table("evaluaciones").insert(
                                    {
                                        "fecha": hoy,
                                        "mesonero_id": mesonero["id"],
                                        "evaluador_id": usuario["id"],
                                        "tipo": "amonestacion_grave",
                                        "justificacion": justificacion_directa.strip(),
                                    }
                                ).execute()
                                registrar_log(
                                    usuario,
                                    "Registró amonestación grave directa",
                                    f"{mesonero['nombre_completo']}: {justificacion_directa.strip()}",
                                )
                                st.rerun()

    st.markdown("---")

    cierres_hoy = (
        supabase.table("cierres_turno")
        .select("*, usuarios(nombre_completo)")
        .eq("fecha", hoy)
        .order("fecha_hora", desc=True)
        .execute()
        .data
    )
    if cierres_hoy:
        nombres = ", ".join((c.get("usuarios") or {}).get("nombre_completo", "N/D") for c in cierres_hoy)
        st.caption(f"Turno de hoy ya cerrado por: **{nombres}**")

    if st.button("✅ Guardar y cortar turno", type="primary"):
        supabase.table("cierres_turno").insert(
            {
                "fecha": hoy,
                "evaluador_id": usuario["id"],
            }
        ).execute()
        registrar_log(usuario, "Cerró turno", f"Fecha: {hoy}")
        st.success(
            f"Turno cerrado por **{usuario['nombre_completo']}**. Todos los registros de hoy ya estaban "
            "guardados en la nube en el momento en que los ingresaste; esto deja constancia de quién y "
            "cuándo se cerró el turno."
        )
        st.rerun()


# =================================================================
# DASHBOARD
# =================================================================
def dashboard(usuario):
    st.header("📊 Reportes, Rating y Amonestaciones")

    supabase = get_supabase_client()

    col1, col2 = st.columns(2)
    with col1:
        fecha_inicio = st.date_input("Desde", value=date.today().replace(day=1))
    with col2:
        fecha_fin = st.date_input("Hasta", value=date.today())

    if fecha_inicio > fecha_fin:
        st.error("La fecha 'Desde' no puede ser posterior a la fecha 'Hasta'.")
        return

    evaluaciones = (
        supabase.table("evaluaciones")
        .select("*, mesoneros(nombre_completo), usuarios(nombre_completo)")
        .gte("fecha", fecha_inicio.isoformat())
        .lte("fecha", fecha_fin.isoformat())
        .execute()
        .data
    )

    if not evaluaciones:
        st.info("No hay registros en el rango de fechas seleccionado.")
        return

    df = pd.DataFrame(evaluaciones)
    df["mesonero"] = df["mesoneros"].apply(lambda x: x["nombre_completo"] if x else "N/A")
    df["evaluador"] = df["usuarios"].apply(lambda x: x["nombre_completo"] if x else "N/A")

    st.subheader("🏆 Ranking de errores estándar (mayor a menor)")
    errores_df = df[df["tipo"] == "error_estandar"]
    if not errores_df.empty:
        ranking_errores = (
            errores_df.groupby("mesonero").size().sort_values(ascending=False).reset_index(name="Total de errores")
        )
        st.dataframe(ranking_errores, use_container_width=True, hide_index=True)
        st.bar_chart(ranking_errores.set_index("mesonero"))
    else:
        st.caption("Sin errores estándar en este rango.")

    st.subheader("🚨 Total de amonestaciones graves (afectan comisiones)")
    graves_df = df[df["tipo"] == "amonestacion_grave"]
    if not graves_df.empty:
        ranking_graves = (
            graves_df.groupby("mesonero").size().sort_values(ascending=False).reset_index(name="Total amonestaciones")
        )
        st.dataframe(ranking_graves, use_container_width=True, hide_index=True)
        st.bar_chart(ranking_graves.set_index("mesonero"))
    else:
        st.caption("Sin amonestaciones graves en este rango.")

    with st.expander("Ver detalle completo (todas las justificaciones)"):
        detalle = df[["fecha", "mesonero", "tipo", "evaluador", "justificacion"]].sort_values(
            "fecha", ascending=False
        )
        st.dataframe(detalle, use_container_width=True, hide_index=True)

    st.subheader("📝 Registros hechos por cada evaluador")
    actividad = df.groupby("evaluador").size().sort_values(ascending=False).reset_index(name="Total registrado")
    st.dataframe(actividad, use_container_width=True, hide_index=True)

    st.subheader("🔒 Turnos cerrados en este rango")
    cierres = (
        supabase.table("cierres_turno")
        .select("*, usuarios(nombre_completo)")
        .gte("fecha", fecha_inicio.isoformat())
        .lte("fecha", fecha_fin.isoformat())
        .order("fecha", desc=True)
        .execute()
        .data
    )
    if cierres:
        df_cierres = pd.DataFrame(cierres)
        df_cierres["evaluador"] = df_cierres["usuarios"].apply(lambda x: x["nombre_completo"] if x else "N/A")
        st.dataframe(
            df_cierres[["fecha", "evaluador", "fecha_hora"]].rename(columns={"fecha_hora": "hora exacta"}),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.caption("Ningún turno se ha cerrado con el botón 'Guardar y cortar turno' en este rango.")


# =================================================================
# ADMIN: MESONEROS
# =================================================================
def admin_mesoneros(usuario):
    st.header("👥 Gestión de Mesoneros")
    supabase = get_supabase_client()

    with st.form("nuevo_mesonero", clear_on_submit=True):
        nombre = st.text_input("Nombre completo del nuevo mesonero")
        submit = st.form_submit_button("➕ Agregar mesonero")
        if submit:
            if not nombre.strip():
                st.error("El nombre no puede estar vacío.")
            else:
                supabase.table("mesoneros").insert({"nombre_completo": nombre.strip()}).execute()
                registrar_log(usuario, "Agregó mesonero", nombre.strip())
                st.success(f"'{nombre.strip()}' agregado.")
                st.rerun()

    st.subheader("Mesoneros registrados")
    mesoneros = supabase.table("mesoneros").select("*").order("nombre_completo").execute().data

    for m in mesoneros:
        c1, c2, c3 = st.columns([3, 1, 1])
        c1.write(m["nombre_completo"])
        c2.write("🟢 Activo" if m["activo"] else "🔴 Inactivo")
        if c3.button("Cambiar estado", key=f"toggle_mesonero_{m['id']}"):
            supabase.table("mesoneros").update({"activo": not m["activo"]}).eq("id", m["id"]).execute()
            registrar_log(usuario, "Cambió estado de mesonero", m["nombre_completo"])
            st.rerun()


# =================================================================
# ADMIN: USUARIOS EVALUADORES
# =================================================================
def admin_usuarios(usuario):
    st.header("🔑 Gestión de Usuarios Evaluadores")
    supabase = get_supabase_client()

    with st.form("nuevo_usuario", clear_on_submit=True):
        nombre_completo = st.text_input("Nombre completo")
        nombre_usuario = st.text_input("Usuario (para iniciar sesión)")
        password = st.text_input("Contraseña temporal", type="password")
        rol = st.selectbox("Rol", ["evaluador", "admin_general"])
        submit = st.form_submit_button("➕ Crear usuario")

        if submit:
            if not (nombre_completo.strip() and nombre_usuario.strip() and password.strip()):
                st.error("Todos los campos son obligatorios.")
            else:
                existe = (
                    supabase.table("usuarios")
                    .select("id")
                    .eq("nombre_usuario", nombre_usuario.strip())
                    .execute()
                    .data
                )
                if existe:
                    st.error("Ese nombre de usuario ya existe.")
                else:
                    supabase.table("usuarios").insert(
                        {
                            "nombre_completo": nombre_completo.strip(),
                            "nombre_usuario": nombre_usuario.strip(),
                            "password_hash": hash_password(password.strip()),
                            "rol": rol,
                        }
                    ).execute()
                    registrar_log(usuario, "Creó usuario", nombre_usuario.strip())
                    st.success("Usuario creado.")
                    st.rerun()

    st.subheader("Usuarios registrados")
    usuarios_lista = supabase.table("usuarios").select("*").order("nombre_completo").execute().data

    for u in usuarios_lista:
        c1, c2, c3, c4 = st.columns([2, 2, 1, 1])
        c1.write(u["nombre_completo"])
        c2.write(u["rol"])
        c3.write("🟢" if u["activo"] else "🔴")
        if c4.button("Activar/Desactivar", key=f"toggle_usuario_{u['id']}"):
            if u["id"] == usuario["id"]:
                st.error("No puedes desactivarte a ti mismo.")
            else:
                supabase.table("usuarios").update({"activo": not u["activo"]}).eq("id", u["id"]).execute()
                registrar_log(usuario, "Cambió estado de usuario", u["nombre_usuario"])
                st.rerun()


# =================================================================
# ADMIN: LOGS / AUDITORÍA
# =================================================================
def admin_logs(usuario):
    st.header("🕵️ Auditoría / Rastro de Actividad")
    supabase = get_supabase_client()

    logs = (
        supabase.table("logs_auditoria")
        .select("*")
        .order("fecha_hora", desc=True)
        .limit(500)
        .execute()
        .data
    )

    if not logs:
        st.info("Todavía no hay actividad registrada.")
        return

    df = pd.DataFrame(logs)
    st.dataframe(
        df[["fecha_hora", "nombre_usuario", "accion", "detalle"]],
        use_container_width=True,
        hide_index=True,
    )


# =================================================================
# MI CUENTA (cambio de contraseña propia)
# =================================================================
def mi_cuenta(usuario):
    st.header("⚙️ Mi cuenta")
    st.write(f"Usuario: **{usuario['nombre_usuario']}**  |  Rol: **{usuario['rol']}**")

    supabase = get_supabase_client()
    with st.form("cambiar_password"):
        nueva = st.text_input("Nueva contraseña", type="password")
        confirmar = st.text_input("Confirmar nueva contraseña", type="password")
        submit = st.form_submit_button("Actualizar contraseña")
        if submit:
            if not nueva or len(nueva) < 6:
                st.error("La contraseña debe tener al menos 6 caracteres.")
            elif nueva != confirmar:
                st.error("Las contraseñas no coinciden.")
            else:
                supabase.table("usuarios").update({"password_hash": hash_password(nueva)}).eq(
                    "id", usuario["id"]
                ).execute()
                registrar_log(usuario, "Cambió su propia contraseña")
                st.success("Contraseña actualizada. Úsala en tu próximo inicio de sesión.")


# =================================================================
# ROUTER PRINCIPAL
# =================================================================
if st.session_state.usuario is None:
    pantalla_login()
else:
    usuario_actual = st.session_state.usuario

    st.sidebar.title(f"👤 {usuario_actual['nombre_completo']}")
    st.sidebar.caption(f"Rol: {usuario_actual['rol']}")
    st.sidebar.markdown("---")

    opciones = ["📋 Panel Diario", "📊 Dashboard", "⚙️ Mi cuenta"]
    if usuario_actual["rol"] == "admin_general":
        opciones += ["👥 Mesoneros", "🔑 Usuarios", "🕵️ Auditoría"]

    seleccion = st.sidebar.radio("Menú", opciones)

    st.sidebar.markdown("---")
    if st.sidebar.button("🚪 Cerrar sesión"):
        cerrar_sesion()

    if seleccion == "📋 Panel Diario":
        panel_diario(usuario_actual)
    elif seleccion == "📊 Dashboard":
        dashboard(usuario_actual)
    elif seleccion == "⚙️ Mi cuenta":
        mi_cuenta(usuario_actual)
    elif seleccion == "👥 Mesoneros":
        admin_mesoneros(usuario_actual)
    elif seleccion == "🔑 Usuarios":
        admin_usuarios(usuario_actual)
    elif seleccion == "🕵️ Auditoría":
        admin_logs(usuario_actual)
