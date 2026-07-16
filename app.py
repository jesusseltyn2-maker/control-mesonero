"""
Sistema de Control de Personal por Áreas
------------------------------------------
App interna para evaluadores/administradores que registran los errores
diarios del personal de varias áreas del negocio (mesoneros, cocina,
panadería, barra, etc.), cada área con su propio tope de errores
estándar, y cada trabajador con un turno fijo asignado. Auditoría
completa y dashboard de rating/amonestaciones. Datos en Supabase.
"""

import io
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from auth import hash_password, login, registrar_log
from db import get_supabase_client
from storage_utils import subir_evidencia

st.set_page_config(page_title="Control de Personal", page_icon="📋", layout="wide")

DEFAULT_MAX_ERRORES = 3
TZ_VENEZUELA = ZoneInfo("America/Caracas")


def hoy_venezuela():
    """Fecha de 'hoy' según la hora de Venezuela (no la del servidor)."""
    return datetime.now(TZ_VENEZUELA).date()


def hora_venezuela_texto():
    """Hora actual de Venezuela en formato 24h, para mensajes en pantalla."""
    return datetime.now(TZ_VENEZUELA).strftime("%d/%m/%Y %H:%M:%S")


def convertir_columna_a_hora_venezuela(serie):
    """Convierte una columna de fecha/hora (guardada en UTC en Supabase) a
    hora de Venezuela en formato 24h, para mostrar en tablas."""
    return pd.to_datetime(serie, utc=True, errors="coerce").dt.tz_convert(TZ_VENEZUELA).dt.strftime("%d/%m/%Y %H:%M:%S")

if "usuario" not in st.session_state:
    st.session_state.usuario = None


# =================================================================
# HELPERS
# =================================================================
def cargar_areas(supabase, solo_activas=True):
    q = supabase.table("areas").select("*").order("nombre")
    if solo_activas:
        q = q.eq("activo", True)
    return q.execute().data


def cargar_turnos(supabase, solo_activos=True):
    q = supabase.table("turnos").select("*").order("orden")
    if solo_activos:
        q = q.eq("activo", True)
    return q.execute().data


def cargar_categorias(supabase, area_id, solo_activas=True):
    q = supabase.table("categorias_falta").select("*").eq("area_id", area_id).order("nombre")
    if solo_activas:
        q = q.eq("activo", True)
    return q.execute().data


# =================================================================
# LOGIN
# =================================================================
def pantalla_login():
    st.title("📋 Sistema de Control de Personal")
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

    st.caption("¿Olvidaste tu contraseña? Pídele al Administrador General que te la restablezca.")


def cerrar_sesion():
    registrar_log(st.session_state.usuario, "Cerró sesión")
    st.session_state.usuario = None
    st.rerun()


# =================================================================
# PANEL DIARIO (con pestañas por área)
# =================================================================
def panel_diario(usuario):
    st.header("📋 Panel de Control Diario")

    supabase = get_supabase_client()
    hoy = hoy_venezuela().isoformat()

    areas = cargar_areas(supabase)
    turnos_catalogo = cargar_turnos(supabase)

    if not areas:
        st.info("Todavía no hay áreas configuradas. Pide al Administrador General que las cree en 'Áreas'.")
        return
    if not turnos_catalogo:
        st.info("Todavía no hay turnos configurados. Pide al Administrador General que los cree en 'Turnos'.")
        return

    turnos_map = {t["id"]: t["nombre"] for t in turnos_catalogo}

    # El "último turno del día" es el de mayor 'orden' entre los activos (ej. Noche).
    # Si ya se cerró hoy, todo lo registrado DESPUÉS de esa hora cuenta como si
    # fuera un día nuevo (el tope de 3 se reinicia), sin esperar la medianoche real.
    turno_final = max(turnos_catalogo, key=lambda t: t["orden"])
    cierre_final_hoy = (
        supabase.table("cierres_turno")
        .select("fecha_hora")
        .eq("fecha", hoy)
        .eq("turno_id", turno_final["id"])
        .order("fecha_hora", desc=True)
        .limit(1)
        .execute()
        .data
    )
    corte_dia_iso = cierre_final_hoy[0]["fecha_hora"] if cierre_final_hoy else None

    if corte_dia_iso:
        st.success(
            f"✅ El turno '{turno_final['nombre']}' (último del día) ya se cerró hoy. "
            "Los contadores de errores/amonestaciones ya están en 0 para lo que se registre de aquí en adelante."
        )

    busqueda = st.text_input("🔍 Buscar trabajador por nombre", placeholder="Escribe un nombre para filtrar...")

    tabs = st.tabs([a["nombre"] for a in areas])
    for area, tab in zip(areas, tabs):
        with tab:
            _panel_area(usuario, supabase, hoy, area, turnos_map, busqueda, corte_dia_iso)

    st.markdown("---")
    _seccion_cierre_turno(usuario, supabase, hoy, turnos_catalogo)


def _panel_area(usuario, supabase, hoy, area, turnos_map, busqueda, corte_dia_iso):
    empleados_todos = (
        supabase.table("mesoneros")
        .select("*")
        .eq("activo", True)
        .eq("area_id", area["id"])
        .order("nombre_completo")
        .execute()
        .data
    )

    if not empleados_todos:
        st.info(f"No hay trabajadores activos en '{area['nombre']}' todavía. Agrégalos en 'Trabajadores'.")
        return

    if busqueda.strip():
        empleados = [e for e in empleados_todos if busqueda.strip().lower() in e["nombre_completo"].lower()]
        if not empleados:
            st.warning(f"No se encontró ningún trabajador de '{area['nombre']}' que coincida con '{busqueda.strip()}'.")
            return
    else:
        empleados = empleados_todos

    max_errores = area.get("max_errores_estandar") or DEFAULT_MAX_ERRORES

    categorias_area = cargar_categorias(supabase, area["id"])
    categorias_map = {c["id"]: c["nombre"] for c in categorias_area}
    OPCION_OTRO = "Otro (especificar abajo)"
    opciones_categoria = [c["nombre"] for c in categorias_area] + [OPCION_OTRO]
    categoria_id_por_nombre = {c["nombre"]: c["id"] for c in categorias_area}

    for empleado in empleados:
        q = (
            supabase.table("evaluaciones")
            .select("*, usuarios(nombre_completo)")
            .eq("mesonero_id", empleado["id"])
            .eq("fecha", hoy)
        )
        if corte_dia_iso:
            # El último turno del día ya se cerró: solo cuenta lo registrado DESPUÉS de ese cierre.
            q = q.gt("created_at", corte_dia_iso)
        evals_hoy = q.execute().data

        errores_dia = [e for e in evals_hoy if e["tipo"] == "error_estandar"]
        amonestaciones_dia = [e for e in evals_hoy if e["tipo"] == "amonestacion_grave"]

        turno_nombre = turnos_map.get(empleado.get("turno_id"), "Sin turno asignado")

        with st.container(border=True):
            col_info, col_accion = st.columns([2, 3])

            with col_info:
                st.subheader(empleado["nombre_completo"])
                st.caption(f"🕒 Turno fijo: **{turno_nombre}**")
                m1, m2 = st.columns(2)
                m1.metric("Errores hoy", f"{len(errores_dia)}/{max_errores}")
                m2.metric("Amonestaciones hoy", len(amonestaciones_dia))

                if errores_dia:
                    with st.expander("Ver justificaciones de errores de hoy"):
                        for e in errores_dia:
                            evaluador_nombre = (e.get("usuarios") or {}).get("nombre_completo", "N/D")
                            cat_texto = categorias_map.get(e.get("categoria_id"), "Otro")
                            st.caption(f"• **[{cat_texto}]** *(evaluó: {evaluador_nombre})* — {e['justificacion']}")
                            if e.get("imagen_url"):
                                st.image(e["imagen_url"], width=200)
                if amonestaciones_dia:
                    with st.expander("Ver amonestaciones graves de hoy"):
                        for e in amonestaciones_dia:
                            evaluador_nombre = (e.get("usuarios") or {}).get("nombre_completo", "N/D")
                            cat_texto = categorias_map.get(e.get("categoria_id"), "Otro")
                            st.caption(f"⚠️ **[{cat_texto}]** *(evaluó: {evaluador_nombre})* — {e['justificacion']}")
                            if e.get("imagen_url"):
                                st.image(e["imagen_url"], width=200)

            with col_accion:
                puede_error_estandar = len(errores_dia) < max_errores

                if puede_error_estandar:
                    with st.form(key=f"form_std_{empleado['id']}", clear_on_submit=True):
                        st.write("Registrar **error estándar**")
                        categoria_sel = st.selectbox(
                            "Tipo de falta", opciones_categoria, key=f"cat_std_{empleado['id']}"
                        )
                        justificacion = st.text_area(
                            "Justificación obligatoria", key=f"just_std_{empleado['id']}", height=70
                        )
                        foto = st.file_uploader(
                            "📷 Adjuntar foto (opcional)",
                            type=["png", "jpg", "jpeg"],
                            key=f"foto_std_{empleado['id']}",
                        )
                        if st.form_submit_button("➕ Registrar error"):
                            if not justificacion.strip():
                                st.error("La justificación es obligatoria.")
                            else:
                                imagen_url = None
                                if foto is not None:
                                    try:
                                        imagen_url = subir_evidencia(supabase, empleado["id"], foto)
                                    except Exception as e:
                                        st.warning(f"El registro se guardó, pero la foto no se pudo subir: {e}")
                                categoria_id = (
                                    None if categoria_sel == OPCION_OTRO else categoria_id_por_nombre.get(categoria_sel)
                                )
                                supabase.table("evaluaciones").insert(
                                    {
                                        "fecha": hoy,
                                        "turno_id": empleado.get("turno_id"),
                                        "mesonero_id": empleado["id"],
                                        "evaluador_id": usuario["id"],
                                        "tipo": "error_estandar",
                                        "categoria_id": categoria_id,
                                        "justificacion": justificacion.strip(),
                                        "imagen_url": imagen_url,
                                    }
                                ).execute()
                                registrar_log(
                                    usuario,
                                    "Registró error estándar",
                                    f"{empleado['nombre_completo']} ({area['nombre']}): {justificacion.strip()}",
                                )
                                st.rerun()
                else:
                    st.warning(
                        f"⚠️ **{empleado['nombre_completo']}** ya alcanzó el máximo de {max_errores} "
                        f"errores estándar hoy en '{area['nombre']}'. El próximo registro debe ser "
                        "una amonestación grave."
                    )
                    with st.form(key=f"form_grave_auto_{empleado['id']}", clear_on_submit=True):
                        categoria_sel = st.selectbox(
                            "Tipo de falta", opciones_categoria, key=f"cat_grave_auto_{empleado['id']}"
                        )
                        justificacion = st.text_area(
                            "Justificación obligatoria (amonestación grave)",
                            key=f"just_grave_auto_{empleado['id']}",
                            height=70,
                        )
                        foto = st.file_uploader(
                            "📷 Adjuntar foto (opcional)",
                            type=["png", "jpg", "jpeg"],
                            key=f"foto_grave_auto_{empleado['id']}",
                        )
                        if st.form_submit_button("🚨 Registrar amonestación grave"):
                            if not justificacion.strip():
                                st.error("La justificación es obligatoria.")
                            else:
                                imagen_url = None
                                if foto is not None:
                                    try:
                                        imagen_url = subir_evidencia(supabase, empleado["id"], foto)
                                    except Exception as e:
                                        st.warning(f"El registro se guardó, pero la foto no se pudo subir: {e}")
                                categoria_id = (
                                    None if categoria_sel == OPCION_OTRO else categoria_id_por_nombre.get(categoria_sel)
                                )
                                supabase.table("evaluaciones").insert(
                                    {
                                        "fecha": hoy,
                                        "turno_id": empleado.get("turno_id"),
                                        "mesonero_id": empleado["id"],
                                        "evaluador_id": usuario["id"],
                                        "tipo": "amonestacion_grave",
                                        "categoria_id": categoria_id,
                                        "justificacion": justificacion.strip(),
                                        "imagen_url": imagen_url,
                                    }
                                ).execute()
                                registrar_log(
                                    usuario,
                                    "Registró amonestación grave (por exceso de errores)",
                                    f"{empleado['nombre_completo']} ({area['nombre']}): {justificacion.strip()}",
                                )
                                st.rerun()

                with st.expander("🔴 Registrar amonestación grave directa (falta grave inmediata)"):
                    with st.form(key=f"form_grave_directa_{empleado['id']}", clear_on_submit=True):
                        categoria_sel = st.selectbox(
                            "Tipo de falta", opciones_categoria, key=f"cat_directa_{empleado['id']}"
                        )
                        justificacion_directa = st.text_area(
                            "Justificación obligatoria", key=f"just_directa_{empleado['id']}", height=70
                        )
                        foto = st.file_uploader(
                            "📷 Adjuntar foto (opcional)",
                            type=["png", "jpg", "jpeg"],
                            key=f"foto_directa_{empleado['id']}",
                        )
                        if st.form_submit_button("🚨 Registrar falta grave directa"):
                            if not justificacion_directa.strip():
                                st.error("La justificación es obligatoria.")
                            else:
                                imagen_url = None
                                if foto is not None:
                                    try:
                                        imagen_url = subir_evidencia(supabase, empleado["id"], foto)
                                    except Exception as e:
                                        st.warning(f"El registro se guardó, pero la foto no se pudo subir: {e}")
                                categoria_id = (
                                    None if categoria_sel == OPCION_OTRO else categoria_id_por_nombre.get(categoria_sel)
                                )
                                supabase.table("evaluaciones").insert(
                                    {
                                        "fecha": hoy,
                                        "turno_id": empleado.get("turno_id"),
                                        "mesonero_id": empleado["id"],
                                        "evaluador_id": usuario["id"],
                                        "tipo": "amonestacion_grave",
                                        "categoria_id": categoria_id,
                                        "justificacion": justificacion_directa.strip(),
                                        "imagen_url": imagen_url,
                                    }
                                ).execute()
                                registrar_log(
                                    usuario,
                                    "Registró amonestación grave directa",
                                    f"{empleado['nombre_completo']} ({area['nombre']}): {justificacion_directa.strip()}",
                                )
                                st.rerun()


def _seccion_cierre_turno(usuario, supabase, hoy, turnos_catalogo):
    st.subheader("🔒 Cerrar turno del día")
    st.caption(
        "Cerrar un turno aplica a TODAS las áreas a la vez (ej: 'Mañana' cierra el turno de "
        "mañana de mesoneros, cocina, barra, etc., todo junto)."
    )

    turnos_map_nombre_id = {t["nombre"]: t["id"] for t in turnos_catalogo}
    turno_sel_nombre = st.selectbox("¿Qué turno vas a cerrar?", list(turnos_map_nombre_id.keys()), key="turno_a_cerrar")
    turno_sel_id = turnos_map_nombre_id[turno_sel_nombre]

    cierres_hoy = (
        supabase.table("cierres_turno")
        .select("*, usuarios(nombre_completo), turnos(nombre)")
        .eq("fecha", hoy)
        .execute()
        .data
    )
    if cierres_hoy:
        resumen = ", ".join(
            f"{(c.get('turnos') or {}).get('nombre', '?')} por {(c.get('usuarios') or {}).get('nombre_completo', 'N/D')}"
            for c in cierres_hoy
        )
        st.info(f"Turnos ya cerrados hoy: {resumen}")

    ya_cerrado = any(c.get("turno_id") == turno_sel_id for c in cierres_hoy)
    if ya_cerrado:
        st.warning(
            f"El turno '{turno_sel_nombre}' de hoy ya fue cerrado. Si necesitas corregir algo, "
            "el Administrador General puede editarlo desde el Dashboard."
        )
        return

    revisar_key = f"revisando_cierre_{turno_sel_id}"
    if revisar_key not in st.session_state:
        st.session_state[revisar_key] = False

    if not st.session_state[revisar_key]:
        if st.button(f"✅ Guardar y cerrar '{turno_sel_nombre}'", type="primary"):
            st.session_state[revisar_key] = True
            st.rerun()
    else:
        st.write(f"#### 🔍 Revisión antes de cerrar '{turno_sel_nombre}'")
        st.caption(
            "Revisa los registros de hoy de este turno, en todas las áreas. Corrige o elimina si "
            "hace falta, y confirma abajo."
        )

        evaluaciones_turno = (
            supabase.table("evaluaciones")
            .select(
                "*, mesoneros(nombre_completo, areas(nombre)), usuarios(nombre_completo), categorias_falta(nombre)"
            )
            .eq("fecha", hoy)
            .eq("turno_id", turno_sel_id)
            .order("created_at")
            .execute()
            .data
        )

        if not evaluaciones_turno:
            st.info("No hay ningún registro hoy para este turno. Puedes confirmar el cierre igualmente.")
        else:
            for h in evaluaciones_turno:
                empleado_info = h.get("mesoneros") or {}
                empleado_nombre = empleado_info.get("nombre_completo", "N/D")
                area_nombre = (empleado_info.get("areas") or {}).get("nombre", "N/D")
                evaluador_nombre = (h.get("usuarios") or {}).get("nombre_completo", "N/D")
                categoria_texto = (h.get("categorias_falta") or {}).get("nombre", "Otro")
                tipo_texto = "Error estándar" if h["tipo"] == "error_estandar" else "Amonestación grave"
                icono = "🔸" if h["tipo"] == "error_estandar" else "🚨"

                with st.container(border=True):
                    col_texto, col_btn = st.columns([4, 1])
                    col_texto.write(
                        f"{icono} **{empleado_nombre}** ({area_nombre}) — {tipo_texto} — "
                        f"**[{categoria_texto}]** — evaluó: *{evaluador_nombre}*"
                    )
                    col_texto.caption(h["justificacion"])
                    if h.get("imagen_url"):
                        col_texto.image(h["imagen_url"], width=200)

                    edit_key = f"revision_edit_open_{h['id']}"
                    if edit_key not in st.session_state:
                        st.session_state[edit_key] = False

                    if col_btn.button("✏️ Corregir", key=f"revision_btn_edit_{h['id']}"):
                        st.session_state[edit_key] = not st.session_state[edit_key]
                        st.rerun()

                    if st.session_state[edit_key]:
                        with st.form(key=f"revision_form_edit_{h['id']}"):
                            nueva_just = st.text_area(
                                "Justificación corregida", value=h["justificacion"], key=f"revision_just_{h['id']}"
                            )
                            cg1, cg2 = st.columns(2)
                            guardar_edit = cg1.form_submit_button("💾 Guardar corrección")
                            eliminar_edit = cg2.form_submit_button("🗑️ Eliminar este registro")

                            if guardar_edit:
                                if not nueva_just.strip():
                                    st.error("La justificación no puede quedar vacía.")
                                else:
                                    supabase.table("evaluaciones").update(
                                        {"justificacion": nueva_just.strip()}
                                    ).eq("id", h["id"]).execute()
                                    registrar_log(
                                        usuario,
                                        "Corrigió registro antes de cerrar turno",
                                        f"{empleado_nombre} ({area_nombre}): {nueva_just.strip()}",
                                    )
                                    st.session_state[edit_key] = False
                                    st.rerun()

                            if eliminar_edit:
                                supabase.table("evaluaciones").delete().eq("id", h["id"]).execute()
                                registrar_log(
                                    usuario,
                                    "Eliminó registro antes de cerrar turno",
                                    f"{empleado_nombre} ({area_nombre}): {h['justificacion']}",
                                )
                                st.session_state[edit_key] = False
                                st.rerun()

        st.markdown("---")
        col_confirmar, col_cancelar = st.columns(2)
        if col_confirmar.button(f"✅ Confirmar y cerrar '{turno_sel_nombre}'", type="primary"):
            supabase.table("cierres_turno").insert(
                {
                    "fecha": hoy,
                    "turno_id": turno_sel_id,
                    "evaluador_id": usuario["id"],
                }
            ).execute()
            registrar_log(usuario, "Cerró turno", f"Fecha: {hoy}, turno: {turno_sel_nombre}")
            st.session_state[revisar_key] = False
            st.success(f"Turno '{turno_sel_nombre}' cerrado por **{usuario['nombre_completo']}**.")
            st.rerun()
        if col_cancelar.button("Cancelar"):
            st.session_state[revisar_key] = False
            st.rerun()


# =================================================================
# DASHBOARD
# =================================================================
def dashboard(usuario):
    st.header("📊 Reportes, Rating y Amonestaciones")

    supabase = get_supabase_client()
    areas = cargar_areas(supabase, solo_activas=False)
    areas_map_nombre = {a["nombre"]: a["id"] for a in areas}

    col1, col2, col3 = st.columns(3)
    with col1:
        fecha_inicio = st.date_input("Desde", value=hoy_venezuela().replace(day=1))
    with col2:
        fecha_fin = st.date_input("Hasta", value=hoy_venezuela())
    with col3:
        area_sel_nombre = st.selectbox("Área", ["Todas las áreas"] + list(areas_map_nombre.keys()))

    if fecha_inicio > fecha_fin:
        st.error("La fecha 'Desde' no puede ser posterior a la fecha 'Hasta'.")
        return

    evaluaciones = (
        supabase.table("evaluaciones")
        .select(
            "*, mesoneros(nombre_completo, area_id, areas(nombre)), usuarios(nombre_completo), "
            "categorias_falta(nombre)"
        )
        .gte("fecha", fecha_inicio.isoformat())
        .lte("fecha", fecha_fin.isoformat())
        .execute()
        .data
    )

    if area_sel_nombre != "Todas las áreas":
        area_sel_id = areas_map_nombre[area_sel_nombre]
        evaluaciones = [e for e in evaluaciones if (e.get("mesoneros") or {}).get("area_id") == area_sel_id]

    if not evaluaciones:
        st.info("No hay registros con estos filtros. El historial completo por trabajador, más abajo, no depende de este rango.")
        df = pd.DataFrame(
            columns=["fecha", "trabajador", "area", "evaluador", "tipo", "categoria", "justificacion", "imagen_url"]
        )
    else:
        df = pd.DataFrame(evaluaciones)
        df["trabajador"] = df["mesoneros"].apply(lambda x: (x or {}).get("nombre_completo", "N/A"))
        df["area"] = df["mesoneros"].apply(lambda x: ((x or {}).get("areas") or {}).get("nombre", "N/A"))
        df["evaluador"] = df["usuarios"].apply(lambda x: (x or {}).get("nombre_completo", "N/A"))
        df["categoria"] = df["categorias_falta"].apply(lambda x: (x or {}).get("nombre", "Otro") if x else "Otro")

    st.subheader("🏆 Ranking de errores estándar (mayor a menor)")
    errores_df = df[df["tipo"] == "error_estandar"]
    ranking_errores = pd.DataFrame(columns=["trabajador", "Total de errores"])
    if not errores_df.empty:
        ranking_errores = (
            errores_df.groupby("trabajador").size().sort_values(ascending=False).reset_index(name="Total de errores")
        )
        st.dataframe(ranking_errores, use_container_width=True, hide_index=True)
        st.bar_chart(ranking_errores.set_index("trabajador"))
    else:
        st.caption("Sin errores estándar con estos filtros.")

    st.subheader("🚨 Total de amonestaciones graves (afectan comisiones)")
    graves_df = df[df["tipo"] == "amonestacion_grave"]
    ranking_graves = pd.DataFrame(columns=["trabajador", "Total amonestaciones"])
    if not graves_df.empty:
        ranking_graves = (
            graves_df.groupby("trabajador").size().sort_values(ascending=False).reset_index(name="Total amonestaciones")
        )
        st.dataframe(ranking_graves, use_container_width=True, hide_index=True)
        st.bar_chart(ranking_graves.set_index("trabajador"))
    else:
        st.caption("Sin amonestaciones graves con estos filtros.")

    st.subheader("📌 Faltas más comunes (por tipo)")
    if not df.empty:
        ranking_categorias = (
            df.groupby("categoria").size().sort_values(ascending=False).reset_index(name="Total")
        )
        st.dataframe(ranking_categorias, use_container_width=True, hide_index=True)
        st.bar_chart(ranking_categorias.set_index("categoria"))
    else:
        st.caption("Sin datos suficientes con estos filtros.")

    st.subheader("📈 Tendencia en el tiempo (por semana)")
    if not df.empty:
        df_trend = df.copy()
        df_trend["fecha_dt"] = pd.to_datetime(df_trend["fecha"])
        tendencia = df_trend.groupby([pd.Grouper(key="fecha_dt", freq="W"), "tipo"]).size().unstack(fill_value=0)
        tendencia = tendencia.rename(
            columns={"error_estandar": "Errores estándar", "amonestacion_grave": "Amonestaciones graves"}
        )
        st.line_chart(tendencia)
    else:
        st.caption("No hay suficientes datos para mostrar una tendencia con estos filtros.")

    st.subheader("🌟 Reconocimiento — sin ningún registro en este rango")
    q_activos = supabase.table("mesoneros").select("*, areas(nombre)").eq("activo", True)
    if area_sel_nombre != "Todas las áreas":
        q_activos = q_activos.eq("area_id", areas_map_nombre[area_sel_nombre])
    todos_activos = q_activos.execute().data
    trabajadores_con_registro = set(df["trabajador"].unique()) if not df.empty else set()
    sin_registros = [
        e["nombre_completo"] for e in todos_activos if e["nombre_completo"] not in trabajadores_con_registro
    ]
    if sin_registros:
        st.success("👏 " + ", ".join(sorted(sin_registros)))
    else:
        st.caption("Todos los trabajadores activos (con estos filtros) tuvieron al menos un registro.")

    with st.expander("Ver detalle completo (todas las justificaciones)"):
        detalle = df[
            ["fecha", "trabajador", "area", "tipo", "categoria", "evaluador", "justificacion", "imagen_url"]
        ].rename(columns={"imagen_url": "foto"}).sort_values("fecha", ascending=False)
        st.dataframe(
            detalle,
            use_container_width=True,
            hide_index=True,
            column_config={"foto": st.column_config.LinkColumn("foto", display_text="Ver foto")},
        )

    st.subheader("📝 Registros hechos por cada evaluador")
    actividad = pd.DataFrame(columns=["evaluador", "Total registrado"])
    if not df.empty:
        actividad = df.groupby("evaluador").size().sort_values(ascending=False).reset_index(name="Total registrado")
        st.dataframe(actividad, use_container_width=True, hide_index=True)
    else:
        st.caption("Sin actividad registrada con estos filtros.")

    st.markdown("---")
    if df.empty:
        st.caption("No hay datos con estos filtros para exportar a Excel.")
    else:
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            df[
                ["fecha", "trabajador", "area", "tipo", "categoria", "evaluador", "justificacion", "imagen_url"]
            ].rename(columns={"imagen_url": "foto"}).sort_values("fecha").to_excel(
                writer, sheet_name="Detalle", index=False
            )
            ranking_errores.to_excel(writer, sheet_name="Ranking Errores", index=False)
            ranking_graves.to_excel(writer, sheet_name="Ranking Amonestaciones", index=False)
            ranking_categorias.to_excel(writer, sheet_name="Faltas por Tipo", index=False)
            actividad.to_excel(writer, sheet_name="Actividad Evaluador", index=False)
        st.download_button(
            "📥 Descargar este reporte en Excel",
            data=buffer.getvalue(),
            file_name=f"reporte_personal_{fecha_inicio.isoformat()}_{fecha_fin.isoformat()}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    st.subheader("🔒 Turnos cerrados en este rango")
    cierres = (
        supabase.table("cierres_turno")
        .select("*, usuarios(nombre_completo), turnos(nombre)")
        .gte("fecha", fecha_inicio.isoformat())
        .lte("fecha", fecha_fin.isoformat())
        .order("fecha", desc=True)
        .execute()
        .data
    )
    if cierres:
        df_cierres = pd.DataFrame(cierres)
        df_cierres["evaluador"] = df_cierres["usuarios"].apply(lambda x: (x or {}).get("nombre_completo", "N/A"))
        df_cierres["turno"] = df_cierres["turnos"].apply(lambda x: (x or {}).get("nombre", "N/A"))
        df_cierres["hora exacta"] = convertir_columna_a_hora_venezuela(df_cierres["fecha_hora"])
        st.dataframe(
            df_cierres[["fecha", "turno", "evaluador", "hora exacta"]],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.caption("Ningún turno se ha cerrado en este rango.")

    st.markdown("---")
    st.subheader("🔍 Historial completo por trabajador (auditoría)")
    st.caption(
        "Esta sección muestra TODO el historial de un trabajador (no depende de los filtros de "
        "arriba), entrada por entrada, con quién evaluó cada una — para auditar un caso puntual."
    )

    todos_mesoneros = supabase.table("mesoneros").select("*, areas(nombre)").order("nombre_completo").execute().data

    if not todos_mesoneros:
        st.caption("No hay trabajadores registrados todavía.")
    else:
        opciones_mesonero = {
            f"{m['nombre_completo']} — {(m.get('areas') or {}).get('nombre', 'Sin área')}"
            + ("" if m["activo"] else " (inactivo)"): m["id"]
            for m in todos_mesoneros
        }
        mesonero_sel = st.selectbox("Selecciona un trabajador", list(opciones_mesonero.keys()))

        historial = (
            supabase.table("evaluaciones")
            .select("*, usuarios(nombre_completo), categorias_falta(nombre)")
            .eq("mesonero_id", opciones_mesonero[mesonero_sel])
            .order("fecha", desc=True)
            .order("created_at", desc=True)
            .execute()
            .data
        )

        if not historial:
            st.info(f"{mesonero_sel} no tiene ningún registro todavía.")
        elif usuario["rol"] != "admin_general":
            df_hist = pd.DataFrame(historial)
            df_hist["evaluador"] = df_hist["usuarios"].apply(lambda x: (x or {}).get("nombre_completo", "N/A"))
            df_hist["categoria"] = df_hist["categorias_falta"].apply(lambda x: (x or {}).get("nombre", "Otro") if x else "Otro")
            df_hist["tipo_texto"] = df_hist["tipo"].map(
                {"error_estandar": "Error estándar", "amonestacion_grave": "Amonestación grave"}
            )
            df_hist["foto"] = df_hist.get("imagen_url", pd.Series(dtype=str))
            df_hist["hora exacta"] = convertir_columna_a_hora_venezuela(df_hist["created_at"])
            tabla_hist = df_hist[
                ["fecha", "tipo_texto", "categoria", "evaluador", "justificacion", "foto", "hora exacta"]
            ].rename(columns={"tipo_texto": "tipo"})
            st.dataframe(
                tabla_hist,
                use_container_width=True,
                hide_index=True,
                column_config={"foto": st.column_config.LinkColumn("foto", display_text="Ver foto")},
            )

            c1, c2 = st.columns(2)
            c1.metric("Total errores estándar (histórico)", int((df_hist["tipo"] == "error_estandar").sum()))
            c2.metric("Total amonestaciones graves (histórico)", int((df_hist["tipo"] == "amonestacion_grave").sum()))
        else:
            total_errores = sum(1 for h in historial if h["tipo"] == "error_estandar")
            total_graves = sum(1 for h in historial if h["tipo"] == "amonestacion_grave")
            c1, c2 = st.columns(2)
            c1.metric("Total errores estándar (histórico)", total_errores)
            c2.metric("Total amonestaciones graves (histórico)", total_graves)

            st.caption("Como Administrador General puedes corregir o eliminar cualquier registro (queda en Auditoría).")

            for h in historial:
                evaluador_nombre = (h.get("usuarios") or {}).get("nombre_completo", "N/D")
                categoria_texto = (h.get("categorias_falta") or {}).get("nombre", "Otro")
                tipo_texto = "Error estándar" if h["tipo"] == "error_estandar" else "Amonestación grave"
                icono = "🔸" if h["tipo"] == "error_estandar" else "🚨"

                with st.container(border=True):
                    st.write(f"{icono} **{h['fecha']}** — {tipo_texto} — **[{categoria_texto}]** — evaluó: *{evaluador_nombre}*")
                    st.caption(h["justificacion"])
                    if h.get("imagen_url"):
                        st.image(h["imagen_url"], width=200)

                    col_edit, col_delete = st.columns(2)

                    edit_key = f"edit_open_{h['id']}"
                    if edit_key not in st.session_state:
                        st.session_state[edit_key] = False
                    if col_edit.button("✏️ Editar", key=f"btn_edit_{h['id']}"):
                        st.session_state[edit_key] = not st.session_state[edit_key]
                        st.rerun()

                    if st.session_state[edit_key]:
                        with st.form(key=f"form_edit_{h['id']}"):
                            nuevo_tipo = st.selectbox(
                                "Tipo",
                                ["error_estandar", "amonestacion_grave"],
                                index=0 if h["tipo"] == "error_estandar" else 1,
                                format_func=lambda t: "Error estándar" if t == "error_estandar" else "Amonestación grave",
                                key=f"tipo_edit_{h['id']}",
                            )
                            nueva_just = st.text_area(
                                "Justificación", value=h["justificacion"], key=f"just_edit_{h['id']}"
                            )
                            if st.form_submit_button("💾 Guardar cambios"):
                                if not nueva_just.strip():
                                    st.error("La justificación no puede quedar vacía.")
                                else:
                                    supabase.table("evaluaciones").update(
                                        {"tipo": nuevo_tipo, "justificacion": nueva_just.strip()}
                                    ).eq("id", h["id"]).execute()
                                    registrar_log(
                                        usuario,
                                        "Editó registro de evaluación",
                                        f"{mesonero_sel} — {h['fecha']}: {nueva_just.strip()}",
                                    )
                                    st.session_state[edit_key] = False
                                    st.success("Registro actualizado.")
                                    st.rerun()

                    delete_confirm_key = f"del_confirm_{h['id']}"
                    if delete_confirm_key not in st.session_state:
                        st.session_state[delete_confirm_key] = False

                    if not st.session_state[delete_confirm_key]:
                        if col_delete.button("🗑️ Eliminar", key=f"btn_del_{h['id']}"):
                            st.session_state[delete_confirm_key] = True
                            st.rerun()
                    else:
                        st.warning("¿Seguro que quieres eliminar este registro? No se puede deshacer.")
                        cc1, cc2 = st.columns(2)
                        if cc1.button("✅ Sí, eliminar", key=f"confirm_del_{h['id']}"):
                            supabase.table("evaluaciones").delete().eq("id", h["id"]).execute()
                            registrar_log(
                                usuario,
                                "Eliminó registro de evaluación",
                                f"{mesonero_sel} — {h['fecha']}: {h['justificacion']}",
                            )
                            st.session_state[delete_confirm_key] = False
                            st.success("Registro eliminado.")
                            st.rerun()
                        if cc2.button("Cancelar", key=f"cancel_del_{h['id']}"):
                            st.session_state[delete_confirm_key] = False
                            st.rerun()


# =================================================================
# ADMIN: ÁREAS
# =================================================================
def admin_areas(usuario):
    st.header("🏷️ Gestión de Áreas")
    supabase = get_supabase_client()

    with st.form("nueva_area", clear_on_submit=True):
        nombre = st.text_input("Nombre del área (ej. Cocina, Barra, Panadería)")
        max_err = st.number_input("Máximo de errores estándar por día", min_value=1, max_value=20, value=3, step=1)
        submit = st.form_submit_button("➕ Agregar área")
        if submit:
            if not nombre.strip():
                st.error("El nombre no puede estar vacío.")
            else:
                existe = supabase.table("areas").select("id").eq("nombre", nombre.strip()).execute().data
                if existe:
                    st.error("Ya existe un área con ese nombre.")
                else:
                    supabase.table("areas").insert(
                        {"nombre": nombre.strip(), "max_errores_estandar": int(max_err)}
                    ).execute()
                    registrar_log(usuario, "Agregó área", nombre.strip())
                    st.success(f"Área '{nombre.strip()}' agregada.")
                    st.rerun()

    st.subheader("Áreas registradas")
    areas = supabase.table("areas").select("*").order("nombre").execute().data

    for a in areas:
        c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
        c1.write(a["nombre"])
        c2.write(f"Máx: {a['max_errores_estandar']}")
        c3.write("🟢 Activa" if a["activo"] else "🔴 Inactiva")

        edit_key = f"edit_area_{a['id']}"
        if edit_key not in st.session_state:
            st.session_state[edit_key] = False
        if c4.button("Editar", key=f"btn_edit_area_{a['id']}"):
            st.session_state[edit_key] = not st.session_state[edit_key]
            st.rerun()

        if st.session_state[edit_key]:
            with st.form(key=f"form_edit_area_{a['id']}"):
                nuevo_max = st.number_input(
                    f"Nuevo máximo de errores para '{a['nombre']}'",
                    min_value=1,
                    max_value=20,
                    value=a["max_errores_estandar"],
                    step=1,
                    key=f"max_edit_{a['id']}",
                )
                cg1, cg2 = st.columns(2)
                guardar = cg1.form_submit_button("💾 Guardar")
                cambiar_estado = cg2.form_submit_button("🔁 Activar/Desactivar área")
                if guardar:
                    supabase.table("areas").update({"max_errores_estandar": int(nuevo_max)}).eq("id", a["id"]).execute()
                    registrar_log(usuario, "Editó máximo de errores de área", f"{a['nombre']}: {nuevo_max}")
                    st.session_state[edit_key] = False
                    st.rerun()
                if cambiar_estado:
                    supabase.table("areas").update({"activo": not a["activo"]}).eq("id", a["id"]).execute()
                    registrar_log(usuario, "Cambió estado de área", a["nombre"])
                    st.session_state[edit_key] = False
                    st.rerun()


# =================================================================
# ADMIN: TURNOS
# =================================================================
def admin_turnos(usuario):
    st.header("🕐 Gestión de Turnos")
    supabase = get_supabase_client()

    with st.form("nuevo_turno", clear_on_submit=True):
        nombre = st.text_input("Nombre del turno (ej. Mañana, Tarde, Noche)")
        orden = st.number_input("Orden (para ordenarlos en las listas)", min_value=1, max_value=20, value=1, step=1)
        submit = st.form_submit_button("➕ Agregar turno")
        if submit:
            if not nombre.strip():
                st.error("El nombre no puede estar vacío.")
            else:
                existe = supabase.table("turnos").select("id").eq("nombre", nombre.strip()).execute().data
                if existe:
                    st.error("Ya existe un turno con ese nombre.")
                else:
                    supabase.table("turnos").insert({"nombre": nombre.strip(), "orden": int(orden)}).execute()
                    registrar_log(usuario, "Agregó turno", nombre.strip())
                    st.success(f"Turno '{nombre.strip()}' agregado.")
                    st.rerun()

    st.subheader("Turnos registrados")
    turnos = supabase.table("turnos").select("*").order("orden").execute().data

    for t in turnos:
        c1, c2, c3 = st.columns([3, 1, 1])
        c1.write(t["nombre"])
        c2.write("🟢 Activo" if t["activo"] else "🔴 Inactivo")
        if c3.button("Activar/Desactivar", key=f"toggle_turno_{t['id']}"):
            supabase.table("turnos").update({"activo": not t["activo"]}).eq("id", t["id"]).execute()
            registrar_log(usuario, "Cambió estado de turno", t["nombre"])
            st.rerun()


# =================================================================
# ADMIN: CATEGORÍAS DE FALTA (por área)
# =================================================================
def admin_categorias(usuario):
    st.header("🗂️ Categorías de Falta")
    st.caption(
        "Estas categorías son las que aparecen en el desplegable 'Tipo de falta' al registrar "
        "un error o amonestación. Son propias de cada área, así que no aplican los mismos "
        "nombres a Cocina que a Cajeras, por ejemplo. Siempre hay una opción 'Otro' disponible "
        "para lo que no encaje aquí."
    )

    supabase = get_supabase_client()
    areas = cargar_areas(supabase, solo_activas=False)

    if not areas:
        st.warning("Primero crea al menos un área en 'Áreas'.")
        return

    areas_map = {a["nombre"]: a["id"] for a in areas}
    area_sel_nombre = st.selectbox("Área", list(areas_map.keys()))
    area_sel_id = areas_map[area_sel_nombre]

    with st.form("nueva_categoria", clear_on_submit=True):
        nombre = st.text_input(f"Nueva categoría de falta para '{area_sel_nombre}'")
        submit = st.form_submit_button("➕ Agregar categoría")
        if submit:
            if not nombre.strip():
                st.error("El nombre no puede estar vacío.")
            else:
                existe = (
                    supabase.table("categorias_falta")
                    .select("id")
                    .eq("area_id", area_sel_id)
                    .eq("nombre", nombre.strip())
                    .execute()
                    .data
                )
                if existe:
                    st.error("Ya existe esa categoría en esta área.")
                else:
                    supabase.table("categorias_falta").insert(
                        {"area_id": area_sel_id, "nombre": nombre.strip()}
                    ).execute()
                    registrar_log(usuario, "Agregó categoría de falta", f"{area_sel_nombre}: {nombre.strip()}")
                    st.success(f"Categoría '{nombre.strip()}' agregada a '{area_sel_nombre}'.")
                    st.rerun()

    st.subheader(f"Categorías de '{area_sel_nombre}'")
    categorias = (
        supabase.table("categorias_falta")
        .select("*")
        .eq("area_id", area_sel_id)
        .order("nombre")
        .execute()
        .data
    )

    if not categorias:
        st.caption("Todavía no hay categorías para esta área.")
    else:
        for c in categorias:
            c1, c2, c3 = st.columns([3, 1, 1])
            c1.write(c["nombre"])
            c2.write("🟢 Activa" if c["activo"] else "🔴 Inactiva")
            if c3.button("Activar/Desactivar", key=f"toggle_categoria_{c['id']}"):
                supabase.table("categorias_falta").update({"activo": not c["activo"]}).eq("id", c["id"]).execute()
                registrar_log(usuario, "Cambió estado de categoría de falta", f"{area_sel_nombre}: {c['nombre']}")
                st.rerun()


# =================================================================
# ADMIN: TRABAJADORES (antes "Mesoneros")
# =================================================================
def admin_mesoneros(usuario):
    st.header("👥 Gestión de Trabajadores")
    supabase = get_supabase_client()

    areas = cargar_areas(supabase, solo_activas=False)
    turnos = cargar_turnos(supabase, solo_activos=False)

    if not areas or not turnos:
        st.warning("Antes de agregar trabajadores, crea al menos un área (en 'Áreas') y un turno (en 'Turnos').")
        return

    areas_map = {a["nombre"]: a["id"] for a in areas}
    turnos_map = {t["nombre"]: t["id"] for t in turnos}

    with st.form("nuevo_mesonero", clear_on_submit=True):
        nombre = st.text_input("Nombre completo del nuevo trabajador")
        area_sel = st.selectbox("Área", list(areas_map.keys()))
        turno_sel = st.selectbox("Turno fijo asignado", list(turnos_map.keys()))
        submit = st.form_submit_button("➕ Agregar trabajador")
        if submit:
            if not nombre.strip():
                st.error("El nombre no puede estar vacío.")
            else:
                supabase.table("mesoneros").insert(
                    {
                        "nombre_completo": nombre.strip(),
                        "area_id": areas_map[area_sel],
                        "turno_id": turnos_map[turno_sel],
                    }
                ).execute()
                registrar_log(usuario, "Agregó trabajador", f"{nombre.strip()} ({area_sel}, turno {turno_sel})")
                st.success(f"'{nombre.strip()}' agregado a {area_sel}.")
                st.rerun()

    st.subheader("Trabajadores registrados")
    mesoneros = (
        supabase.table("mesoneros").select("*, areas(nombre), turnos(nombre)").order("nombre_completo").execute().data
    )

    for m in mesoneros:
        area_nombre = (m.get("areas") or {}).get("nombre", "Sin área")
        turno_nombre = (m.get("turnos") or {}).get("nombre", "Sin turno")

        c1, c2, c3, c4, c5 = st.columns([2, 1, 1, 1, 1])
        c1.write(m["nombre_completo"])
        c2.write(area_nombre)
        c3.write(turno_nombre)
        c4.write("🟢" if m["activo"] else "🔴")

        confirm_key = f"confirm_toggle_mesonero_{m['id']}"
        if confirm_key not in st.session_state:
            st.session_state[confirm_key] = False

        edit_key = f"edit_mesonero_{m['id']}"
        if edit_key not in st.session_state:
            st.session_state[edit_key] = False

        cbtn1, cbtn2 = c5.columns(2)
        if cbtn1.button("✏️", key=f"btn_edit_mesonero_{m['id']}", help="Cambiar área/turno"):
            st.session_state[edit_key] = not st.session_state[edit_key]
            st.rerun()
        if not st.session_state[confirm_key]:
            if cbtn2.button("🔁", key=f"toggle_mesonero_{m['id']}", help="Activar/Desactivar"):
                st.session_state[confirm_key] = True
                st.rerun()

        if st.session_state[edit_key]:
            with st.form(key=f"form_edit_mesonero_{m['id']}"):
                nueva_area = st.selectbox(
                    "Área", list(areas_map.keys()), index=list(areas_map.keys()).index(area_nombre) if area_nombre in areas_map else 0, key=f"area_edit_{m['id']}"
                )
                nuevo_turno = st.selectbox(
                    "Turno fijo", list(turnos_map.keys()), index=list(turnos_map.keys()).index(turno_nombre) if turno_nombre in turnos_map else 0, key=f"turno_edit_{m['id']}"
                )
                if st.form_submit_button("💾 Guardar cambios"):
                    supabase.table("mesoneros").update(
                        {"area_id": areas_map[nueva_area], "turno_id": turnos_map[nuevo_turno]}
                    ).eq("id", m["id"]).execute()
                    registrar_log(
                        usuario, "Cambió área/turno de trabajador", f"{m['nombre_completo']}: {nueva_area}, {nuevo_turno}"
                    )
                    st.session_state[edit_key] = False
                    st.rerun()

        if st.session_state[confirm_key]:
            accion_texto = "desactivar" if m["activo"] else "reactivar"
            st.warning(f"¿Confirmas {accion_texto} a **{m['nombre_completo']}**?")
            cc1, cc2 = st.columns(2)
            if cc1.button("✅ Sí, confirmar", key=f"yes_toggle_mesonero_{m['id']}"):
                supabase.table("mesoneros").update({"activo": not m["activo"]}).eq("id", m["id"]).execute()
                registrar_log(usuario, "Cambió estado de trabajador", m["nombre_completo"])
                st.session_state[confirm_key] = False
                st.rerun()
            if cc2.button("Cancelar", key=f"no_toggle_mesonero_{m['id']}"):
                st.session_state[confirm_key] = False
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
        c1, c2, c3, c4, c5 = st.columns([2, 2, 1, 1, 1])
        c1.write(u["nombre_completo"])
        c2.write(u["rol"])
        c3.write("🟢" if u["activo"] else "🔴")

        confirm_key = f"confirm_toggle_usuario_{u['id']}"
        if confirm_key not in st.session_state:
            st.session_state[confirm_key] = False

        if not st.session_state[confirm_key]:
            if c4.button("Activar/Desactivar", key=f"toggle_usuario_{u['id']}"):
                if u["id"] == usuario["id"]:
                    st.error("No puedes desactivarte a ti mismo.")
                else:
                    st.session_state[confirm_key] = True
                    st.rerun()
        else:
            accion_texto = "desactivar" if u["activo"] else "reactivar"
            st.warning(f"¿Confirmas {accion_texto} a **{u['nombre_completo']}**?")
            cc1, cc2 = st.columns(2)
            if cc1.button("✅ Sí, confirmar", key=f"yes_toggle_usuario_{u['id']}"):
                supabase.table("usuarios").update({"activo": not u["activo"]}).eq("id", u["id"]).execute()
                registrar_log(usuario, "Cambió estado de usuario", u["nombre_usuario"])
                st.session_state[confirm_key] = False
                st.rerun()
            if cc2.button("Cancelar", key=f"no_toggle_usuario_{u['id']}"):
                st.session_state[confirm_key] = False
                st.rerun()

        mostrar_reset_key = f"mostrar_reset_{u['id']}"
        if mostrar_reset_key not in st.session_state:
            st.session_state[mostrar_reset_key] = False

        if c5.button("🔑 Contraseña", key=f"btn_reset_{u['id']}"):
            st.session_state[mostrar_reset_key] = not st.session_state[mostrar_reset_key]

        if st.session_state[mostrar_reset_key]:
            with st.form(key=f"form_reset_{u['id']}"):
                st.write(f"Restablecer contraseña de **{u['nombre_completo']}** ({u['nombre_usuario']})")
                nueva_pw = st.text_input("Nueva contraseña temporal", type="password", key=f"nueva_pw_{u['id']}")
                confirmar_pw = st.text_input(
                    "Confirmar nueva contraseña", type="password", key=f"confirmar_pw_{u['id']}"
                )
                enviar_reset = st.form_submit_button("Guardar nueva contraseña")
                if enviar_reset:
                    if not nueva_pw or len(nueva_pw) < 6:
                        st.error("La contraseña debe tener al menos 6 caracteres.")
                    elif nueva_pw != confirmar_pw:
                        st.error("Las contraseñas no coinciden.")
                    else:
                        supabase.table("usuarios").update({"password_hash": hash_password(nueva_pw)}).eq(
                            "id", u["id"]
                        ).execute()
                        registrar_log(usuario, "Restableció contraseña de usuario", u["nombre_usuario"])
                        st.session_state[mostrar_reset_key] = False
                        st.success(
                            f"Contraseña de {u['nombre_completo']} actualizada. Avísale la nueva "
                            "contraseña para que la use en su próximo inicio de sesión."
                        )
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
    df["fecha y hora (Venezuela)"] = convertir_columna_a_hora_venezuela(df["fecha_hora"])
    st.dataframe(
        df[["fecha y hora (Venezuela)", "nombre_usuario", "accion", "detalle"]],
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
        opciones += [
            "👥 Trabajadores",
            "🏷️ Áreas",
            "🕐 Turnos",
            "🗂️ Categorías de Falta",
            "🔑 Usuarios",
            "🕵️ Auditoría",
        ]

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
    elif seleccion == "👥 Trabajadores":
        admin_mesoneros(usuario_actual)
    elif seleccion == "🏷️ Áreas":
        admin_areas(usuario_actual)
    elif seleccion == "🕐 Turnos":
        admin_turnos(usuario_actual)
    elif seleccion == "🗂️ Categorías de Falta":
        admin_categorias(usuario_actual)
    elif seleccion == "🔑 Usuarios":
        admin_usuarios(usuario_actual)
    elif seleccion == "🕵️ Auditoría":
        admin_logs(usuario_actual)
