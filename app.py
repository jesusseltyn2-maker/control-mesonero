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
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.express as px
import streamlit as st

from auth import hash_password, login, registrar_log
from db import get_supabase_client
from storage_utils import subir_evidencia

st.set_page_config(page_title="Control de Personal", page_icon="📋", layout="wide")

DEFAULT_MAX_ERRORES = 3
INACTIVIDAD_MAXIMA_SEGUNDOS = 25 * 60  # 25 minutos
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
def cargar_sedes(supabase, solo_activas=True):
    q = supabase.table("sedes").select("*").order("nombre")
    if solo_activas:
        q = q.eq("activo", True)
    return q.execute().data


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


def tiene_permiso(usuario, permiso):
    """El Administrador General siempre tiene todos los permisos. Para
    evaluadores, se revisa la columna correspondiente (por defecto True
    si no existe, para no bloquear a nadie antes de que se configure)."""
    if usuario["rol"] == "admin_general":
        return True
    return usuario.get(permiso, True)


def calcular_porcentaje_bono(numero_amonestacion_del_mes):
    """1ra amonestación grave del mes = 25%, 2da = 50%, 3ra en adelante = 100%."""
    if numero_amonestacion_del_mes <= 1:
        return 25
    elif numero_amonestacion_del_mes == 2:
        return 50
    return 100


OPCIONES_PORCENTAJE_BONO = {
    "10% — sanción grave, pero no tan grave": 10,
    "25%": 25,
    "50%": 50,
    "100% — se queda sin bono": 100,
}


def selector_evidencia(key_prefix):
    """Selector único de evidencia (foto o video). No usamos radio +
    widget condicional porque dentro de un st.form los widgets
    condicionales no se actualizan hasta que se envía el formulario —
    por eso un único file_uploader siempre visible es lo confiable."""
    return st.file_uploader(
        "📷 Adjuntar foto o video (opcional)",
        type=["png", "jpg", "jpeg", "mp4", "mov", "webm"],
        key=f"foto_{key_prefix}",
    )


def mostrar_evidencia(url, width=200):
    """Muestra una evidencia adjunta como imagen o como video, según su
    extensión (las fotos tomadas con la cámara son siempre imagen)."""
    if not url:
        return
    extension = url.split(".")[-1].lower().split("?")[0]
    if extension in ("mp4", "mov", "webm", "avi", "mkv"):
        st.video(url)
    else:
        st.image(url, width=width)


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

    sedes = cargar_sedes(supabase)
    if not sedes:
        st.info("Todavía no hay sedes configuradas. Pide al Administrador General que las cree en 'Sedes'.")
        return

    if len(sedes) > 1:
        sede_sel_nombre = st.radio(
            "📍 Sede", [s["nombre"] for s in sedes], horizontal=True, key="sede_panel_diario"
        )
    else:
        sede_sel_nombre = sedes[0]["nombre"]
    sede_sel = next(s for s in sedes if s["nombre"] == sede_sel_nombre)

    areas_todas = cargar_areas(supabase)
    areas_sede = [a for a in areas_todas if a.get("sede_id") == sede_sel["id"]]

    # Si el evaluador tiene áreas específicas asignadas, solo ve esas (dentro de
    # esta sede). Si no tiene ninguna asignación configurada, ve todas (permisivo
    # por defecto para no bloquear a nadie antes de que el admin lo configure).
    if usuario["rol"] != "admin_general":
        areas_asignadas_ids = {
            r["area_id"] for r in supabase.table("usuario_areas").select("area_id").eq("usuario_id", usuario["id"]).execute().data
        }
        if areas_asignadas_ids:
            areas_sede = [a for a in areas_sede if a["id"] in areas_asignadas_ids]

    turnos_catalogo = cargar_turnos(supabase)

    if not areas_sede:
        st.info(
            f"No tienes áreas disponibles en la sede '{sede_sel_nombre}'. Si esto no es correcto, "
            "pide al Administrador General que revise tus áreas asignadas en 'Usuarios'."
        )
        return
    if not turnos_catalogo:
        st.info("Todavía no hay turnos configurados. Pide al Administrador General que los cree en 'Turnos'.")
        return

    turnos_map = {t["id"]: t["nombre"] for t in turnos_catalogo}

    busqueda = st.text_input("🔍 Buscar trabajador por nombre", placeholder="Escribe un nombre para filtrar...")

    nombres_area = [a["nombre"] for a in areas_sede]
    area_sel_nombre = st.selectbox("Área", nombres_area, key="area_panel_diario")
    # Si al cambiar de sede el área recordada ya no existe en esta sede, usar la primera.
    if area_sel_nombre not in nombres_area:
        area_sel_nombre = nombres_area[0]
    area_sel = next(a for a in areas_sede if a["nombre"] == area_sel_nombre)

    _panel_area(usuario, supabase, hoy, area_sel, turnos_map, busqueda)

    st.markdown("---")
    _seccion_cierre_turno(usuario, supabase, hoy, turnos_catalogo, sede_sel, areas_sede)


def _panel_area(usuario, supabase, hoy, area, turnos_map, busqueda):
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

    # Una sola consulta para TODOS los trabajadores del área (en vez de una por
    # cada uno), y se reparte en memoria — mucho más rápido con muchos trabajadores.
    # La ventana de conteo es el MES CALENDARIO actual (no el día ni el turno):
    # los errores se acumulan durante todo el mes y se reinician el día 1.
    inicio_mes = hoy[:8] + "01"
    ids_empleados = [e["id"] for e in empleados_todos]
    q = (
        supabase.table("evaluaciones")
        .select("*, usuarios(nombre_completo)")
        .in_("mesonero_id", ids_empleados)
        .gte("fecha", inicio_mes)
        .lte("fecha", hoy)
    )
    evals_area_mes = q.execute().data if ids_empleados else []

    evals_por_empleado = {}
    for e in evals_area_mes:
        evals_por_empleado.setdefault(e["mesonero_id"], []).append(e)

    for empleado in empleados:
        evals_mes = evals_por_empleado.get(empleado["id"], [])
        errores_mes = [e for e in evals_mes if e["tipo"] == "error_estandar"]
        amonestaciones_mes = [e for e in evals_mes if e["tipo"] == "amonestacion_grave"]

        turno_nombre = turnos_map.get(empleado.get("turno_id"), "Sin turno asignado")

        if amonestaciones_mes or len(errores_mes) >= max_errores:
            icono_estado = "🔴"
        elif len(errores_mes) == max_errores - 1:
            icono_estado = "🟡"
        else:
            icono_estado = "🟢"

        titulo = (
            f"{icono_estado} {empleado['nombre_completo']} — {turno_nombre} — "
            f"{len(errores_mes)}/{max_errores} errores · {len(amonestaciones_mes)} amonestaciones (este mes)"
        )

        with st.expander(titulo):
            col_info, col_accion = st.columns([2, 3])

            with col_info:
                m1, m2 = st.columns(2)
                m1.metric("Errores este mes", f"{len(errores_mes)}/{max_errores}")
                m2.metric("Amonestaciones este mes", len(amonestaciones_mes))

                if len(errores_mes) == max_errores - 1:
                    st.warning(f"⚠️ A 1 error de llegar al tope del mes ({len(errores_mes)}/{max_errores}).")

                if errores_mes:
                    st.markdown("**Errores de este mes:**")
                    for e in errores_mes:
                        evaluador_nombre = (e.get("usuarios") or {}).get("nombre_completo", "N/D")
                        cat_texto = categorias_map.get(e.get("categoria_id"), "Otro")
                        st.caption(f"• **[{cat_texto}]** *({e['fecha']} — evaluó: {evaluador_nombre})* — {e['justificacion']}")
                        if e.get("imagen_url"):
                            mostrar_evidencia(e["imagen_url"])
                if amonestaciones_mes:
                    st.markdown("**Amonestaciones graves de este mes:**")
                    for e in amonestaciones_mes:
                        evaluador_nombre = (e.get("usuarios") or {}).get("nombre_completo", "N/D")
                        cat_texto = categorias_map.get(e.get("categoria_id"), "Otro")
                        pct_texto = f" — 💰 bono: {e['porcentaje_bono']}%" if e.get("porcentaje_bono") is not None else ""
                        st.caption(f"⚠️ **[{cat_texto}]** *({e['fecha']} — evaluó: {evaluador_nombre})* — {e['justificacion']}{pct_texto}")
                        if e.get("imagen_url"):
                            mostrar_evidencia(e["imagen_url"])

            with col_accion:
                puede_error_estandar = len(errores_mes) < max_errores

                if puede_error_estandar:
                    with st.form(key=f"form_std_{empleado['id']}", clear_on_submit=True):
                        st.write("Registrar **error estándar**")
                        categoria_sel = st.selectbox(
                            "Tipo de falta", opciones_categoria, key=f"cat_std_{empleado['id']}"
                        )
                        justificacion = st.text_area(
                            "Justificación obligatoria", key=f"just_std_{empleado['id']}", height=70
                        )
                        foto = selector_evidencia(f"std_{empleado['id']}")
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
                        f"errores estándar este mes en '{area['nombre']}'. El próximo registro debe ser "
                        "una amonestación grave."
                    )
                    numero_amonestacion = len(amonestaciones_mes) + 1
                    porcentaje_bono = calcular_porcentaje_bono(numero_amonestacion)
                    st.error(
                        f"💰 Esta sería la amonestación grave **#{numero_amonestacion}** de "
                        f"{empleado['nombre_completo']} este mes → afecta el bono en **{porcentaje_bono}%**."
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
                        foto = selector_evidencia(f"grave_auto_{empleado['id']}")
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
                                        "porcentaje_bono": porcentaje_bono,
                                    }
                                ).execute()
                                registrar_log(
                                    usuario,
                                    "Registró amonestación grave (por exceso de errores)",
                                    f"{empleado['nombre_completo']} ({area['nombre']}): {justificacion.strip()} "
                                    f"[afecta bono {porcentaje_bono}%]",
                                )
                                st.rerun()

                st.markdown("---")
                directa_key = f"mostrar_directa_{empleado['id']}"
                if directa_key not in st.session_state:
                    st.session_state[directa_key] = False
                if st.button(
                    "🔴 Registrar amonestación grave directa (falta grave inmediata)",
                    key=f"btn_directa_{empleado['id']}",
                ):
                    st.session_state[directa_key] = not st.session_state[directa_key]
                    st.rerun()

                if st.session_state[directa_key]:
                    with st.form(key=f"form_grave_directa_{empleado['id']}", clear_on_submit=True):
                        categoria_sel = st.selectbox(
                            "Tipo de falta", opciones_categoria, key=f"cat_directa_{empleado['id']}"
                        )
                        nivel_sel = st.selectbox(
                            "Nivel de sanción (afecta el bono)",
                            list(OPCIONES_PORCENTAJE_BONO.keys()),
                            key=f"nivel_directa_{empleado['id']}",
                        )
                        justificacion_directa = st.text_area(
                            "Justificación obligatoria", key=f"just_directa_{empleado['id']}", height=70
                        )
                        foto = selector_evidencia(f"directa_{empleado['id']}")
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
                                porcentaje_bono_directa = OPCIONES_PORCENTAJE_BONO[nivel_sel]
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
                                        "porcentaje_bono": porcentaje_bono_directa,
                                    }
                                ).execute()
                                registrar_log(
                                    usuario,
                                    "Registró amonestación grave directa",
                                    f"{empleado['nombre_completo']} ({area['nombre']}): {justificacion_directa.strip()} "
                                    f"[afecta bono {porcentaje_bono_directa}%]",
                                )
                                st.session_state[directa_key] = False
                                st.rerun()

    st.markdown("---")
    if tiene_permiso(usuario, "puede_falta_general"):
        _seccion_falta_general(
            usuario, supabase, hoy, area, categorias_area, opciones_categoria,
            categoria_id_por_nombre, OPCION_OTRO, max_errores,
        )


def _seccion_falta_general(
    usuario, supabase, hoy, area, categorias_area, opciones_categoria,
    categoria_id_por_nombre, OPCION_OTRO, max_errores,
):
    turnos_catalogo = cargar_turnos(supabase)
    if not turnos_catalogo:
        return
    turnos_nombre_a_id = {t["nombre"]: t["id"] for t in turnos_catalogo}

    with st.expander(f"📢 Registrar falta general para todo '{area['nombre']}' en un turno"):
        st.caption(
            "Esto aplica el MISMO registro a todos los trabajadores activos de esta área que "
            "tengan asignado el turno que elijas. Úsalo para fallas de equipo (ej. 'no se limpió "
            "el piso de cocina'), no para casos de una sola persona."
        )
        with st.form(key=f"form_general_{area['id']}", clear_on_submit=True):
            turno_sel_nombre = st.selectbox(
                "Turno afectado", list(turnos_nombre_a_id.keys()), key=f"turno_general_{area['id']}"
            )
            tipo_sel = st.radio(
                "Tipo", ["Error estándar", "Amonestación grave"], key=f"tipo_general_{area['id']}", horizontal=True
            )
            nivel_sel = st.selectbox(
                "Si elegiste 'Amonestación grave': nivel de sanción (afecta el bono)",
                list(OPCIONES_PORCENTAJE_BONO.keys()),
                key=f"nivel_general_{area['id']}",
            )
            categoria_sel = st.selectbox("Tipo de falta", opciones_categoria, key=f"cat_general_{area['id']}")
            justificacion = st.text_area(
                "Justificación obligatoria", key=f"just_general_{area['id']}", height=70
            )
            foto = selector_evidencia(f"general_{area['id']}")
            confirmo = st.checkbox(
                "Confirmo que quiero aplicar esto a TODO el equipo de este turno",
                key=f"confirm_general_{area['id']}",
            )

            if st.form_submit_button("🚨 Aplicar a todo el turno"):
                if not justificacion.strip():
                    st.error("La justificación es obligatoria.")
                elif not confirmo:
                    st.error("Debes marcar la casilla de confirmación antes de aplicar esto a todo el equipo.")
                else:
                    turno_sel_id = turnos_nombre_a_id[turno_sel_nombre]
                    empleados_turno = (
                        supabase.table("mesoneros")
                        .select("*")
                        .eq("activo", True)
                        .eq("area_id", area["id"])
                        .eq("turno_id", turno_sel_id)
                        .execute()
                        .data
                    )
                    if not empleados_turno:
                        st.warning(
                            f"No hay trabajadores activos de '{area['nombre']}' asignados al turno '{turno_sel_nombre}'."
                        )
                    else:
                        imagen_url = None
                        if foto is not None:
                            try:
                                imagen_url = subir_evidencia(supabase, f"general-{area['id']}", foto)
                            except Exception as e:
                                st.warning(f"No se pudo subir la foto: {e}")

                        categoria_id = (
                            None if categoria_sel == OPCION_OTRO else categoria_id_por_nombre.get(categoria_sel)
                        )
                        tipo_base = "error_estandar" if tipo_sel == "Error estándar" else "amonestacion_grave"

                        aplicados = 0
                        convertidos_a_grave = 0
                        graves_aplicadas = []
                        inicio_mes = hoy[:8] + "01"
                        for empleado in empleados_turno:
                            tipo_final = tipo_base
                            if tipo_base == "error_estandar":
                                # Respeta el tope individual de cada quien: si ya llegó al máximo
                                # este mes, para esa persona esto se convierte en amonestación grave.
                                errores_existentes = len(
                                    supabase.table("evaluaciones")
                                    .select("id")
                                    .eq("mesonero_id", empleado["id"])
                                    .gte("fecha", inicio_mes)
                                    .lte("fecha", hoy)
                                    .eq("tipo", "error_estandar")
                                    .execute()
                                    .data
                                )
                                if errores_existentes >= max_errores:
                                    tipo_final = "amonestacion_grave"
                                    convertidos_a_grave += 1

                            porcentaje_bono = None
                            if tipo_final == "amonestacion_grave":
                                if tipo_base == "amonestacion_grave":
                                    # Grave elegida directamente por el evaluador: usa el nivel manual.
                                    porcentaje_bono = OPCIONES_PORCENTAJE_BONO[nivel_sel]
                                else:
                                    # Convertida automáticamente por exceso de errores: sigue la secuencia 25/50/100.
                                    amonestaciones_existentes = len(
                                        supabase.table("evaluaciones")
                                        .select("id")
                                        .eq("mesonero_id", empleado["id"])
                                        .gte("fecha", inicio_mes)
                                        .lte("fecha", hoy)
                                        .eq("tipo", "amonestacion_grave")
                                        .execute()
                                        .data
                                    )
                                    porcentaje_bono = calcular_porcentaje_bono(amonestaciones_existentes + 1)
                                graves_aplicadas.append((empleado["nombre_completo"], porcentaje_bono))

                            supabase.table("evaluaciones").insert(
                                {
                                    "fecha": hoy,
                                    "turno_id": turno_sel_id,
                                    "mesonero_id": empleado["id"],
                                    "evaluador_id": usuario["id"],
                                    "tipo": tipo_final,
                                    "categoria_id": categoria_id,
                                    "justificacion": f"[Falta general del turno] {justificacion.strip()}",
                                    "imagen_url": imagen_url,
                                    "porcentaje_bono": porcentaje_bono,
                                }
                            ).execute()
                            aplicados += 1

                        registrar_log(
                            usuario,
                            "Registró falta general para área/turno",
                            f"{area['nombre']} - {turno_sel_nombre}: {aplicados} trabajador(es). {justificacion.strip()}",
                        )
                        mensaje = (
                            f"Aplicado a {aplicados} trabajador(es) de '{area['nombre']}' "
                            f"en el turno '{turno_sel_nombre}'."
                        )
                        if convertidos_a_grave:
                            mensaje += (
                                f" ({convertidos_a_grave} ya habían llegado a su tope y se les "
                                "registró como amonestación grave.)"
                            )
                        st.success(mensaje)
                        if graves_aplicadas:
                            detalle_graves = ", ".join(f"{nombre} ({pct}%)" for nombre, pct in graves_aplicadas)
                            st.info(f"💰 Afectación al bono por persona: {detalle_graves}")
                        st.rerun()


def _seccion_cierre_turno(usuario, supabase, hoy, turnos_catalogo, sede, areas_sede):
    st.subheader(f"🔒 Cerrar turno del día — Sede: {sede['nombre']}")
    st.caption(
        f"Cerrar un turno aquí aplica SOLO a las áreas de la sede '{sede['nombre']}' "
        f"({', '.join(a['nombre'] for a in areas_sede)}). No afecta otras sedes. "
        "Esto es solo para dejar constancia de quién y cuándo se cerró cada turno — "
        "el conteo de errores es mensual y no se ve afectado por cerrar o no un turno."
    )

    area_ids_sede = [a["id"] for a in areas_sede]

    turnos_map_nombre_id = {t["nombre"]: t["id"] for t in turnos_catalogo}
    turno_sel_nombre = st.selectbox(
        "¿Qué turno vas a cerrar?", list(turnos_map_nombre_id.keys()), key=f"turno_a_cerrar_{sede['id']}"
    )
    turno_sel_id = turnos_map_nombre_id[turno_sel_nombre]

    cierres_hoy = (
        supabase.table("cierres_turno")
        .select("*, usuarios(nombre_completo), turnos(nombre)")
        .eq("fecha", hoy)
        .eq("sede_id", sede["id"])
        .execute()
        .data
    )
    if cierres_hoy:
        resumen = ", ".join(
            f"{(c.get('turnos') or {}).get('nombre', '?')} por {(c.get('usuarios') or {}).get('nombre_completo', 'N/D')}"
            for c in cierres_hoy
        )
        st.info(f"Turnos ya cerrados hoy en '{sede['nombre']}': {resumen}")

    if not tiene_permiso(usuario, "puede_cerrar_turno"):
        st.info("No tienes permiso para cerrar turnos. Si necesitas cerrar uno, contacta a un Administrador General.")
        return

    ya_cerrado = any(c.get("turno_id") == turno_sel_id for c in cierres_hoy)
    if ya_cerrado:
        st.warning(
            f"El turno '{turno_sel_nombre}' de hoy en '{sede['nombre']}' ya fue cerrado. Si necesitas "
            "corregir algo, el Administrador General puede editarlo desde el Dashboard."
        )
        return

    revisar_key = f"revisando_cierre_{sede['id']}_{turno_sel_id}"
    if revisar_key not in st.session_state:
        st.session_state[revisar_key] = False

    if not st.session_state[revisar_key]:
        if st.button(f"✅ Guardar y cerrar '{turno_sel_nombre}' en {sede['nombre']}", type="primary"):
            st.session_state[revisar_key] = True
            st.rerun()
    else:
        st.write(f"#### 🔍 Revisión antes de cerrar '{turno_sel_nombre}' — {sede['nombre']}")
        st.caption(
            f"Revisa los registros de hoy de este turno, en las áreas de '{sede['nombre']}'. "
            "Corrige o elimina si hace falta, y confirma abajo."
        )

        evaluaciones_turno = (
            supabase.table("evaluaciones")
            .select(
                "*, mesoneros(nombre_completo, area_id, areas(nombre)), usuarios(nombre_completo), categorias_falta(nombre)"
            )
            .eq("fecha", hoy)
            .eq("turno_id", turno_sel_id)
            .order("created_at")
            .execute()
            .data
        )
        evaluaciones_turno = [
            e for e in evaluaciones_turno if (e.get("mesoneros") or {}).get("area_id") in area_ids_sede
        ]

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
                pct_texto = f" — 💰 bono: {h['porcentaje_bono']}%" if h.get("porcentaje_bono") is not None else ""

                with st.container(border=True):
                    col_texto, col_btn = st.columns([4, 1])
                    col_texto.write(
                        f"{icono} **{empleado_nombre}** ({area_nombre}) — {tipo_texto} — "
                        f"**[{categoria_texto}]**{pct_texto} — evaluó: *{evaluador_nombre}*"
                    )
                    col_texto.caption(h["justificacion"])
                    if h.get("imagen_url"):
                        with col_texto:
                            mostrar_evidencia(h["imagen_url"])

                    edit_key = f"revision_edit_open_{h['id']}"
                    if edit_key not in st.session_state:
                        st.session_state[edit_key] = False

                    if tiene_permiso(usuario, "puede_editar_revision"):
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
        if col_confirmar.button(f"✅ Confirmar y cerrar '{turno_sel_nombre}' en {sede['nombre']}", type="primary"):
            supabase.table("cierres_turno").insert(
                {
                    "fecha": hoy,
                    "turno_id": turno_sel_id,
                    "sede_id": sede["id"],
                    "evaluador_id": usuario["id"],
                }
            ).execute()
            registrar_log(
                usuario, "Cerró turno", f"Fecha: {hoy}, turno: {turno_sel_nombre}, sede: {sede['nombre']}"
            )
            st.session_state[revisar_key] = False
            st.success(f"Turno '{turno_sel_nombre}' de '{sede['nombre']}' cerrado por **{usuario['nombre_completo']}**.")
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
        st.info(
            "No hay registros con estos filtros. El historial completo por trabajador, más abajo, "
            "no depende de este rango."
        )
        df = pd.DataFrame(
            columns=["fecha", "trabajador", "area", "evaluador", "tipo", "categoria", "justificacion", "porcentaje_bono", "imagen_url"]
        )
    else:
        df = pd.DataFrame(evaluaciones)
        df["trabajador"] = df["mesoneros"].apply(lambda x: (x or {}).get("nombre_completo", "N/A"))
        df["area"] = df["mesoneros"].apply(lambda x: ((x or {}).get("areas") or {}).get("nombre", "N/A"))
        df["evaluador"] = df["usuarios"].apply(lambda x: (x or {}).get("nombre_completo", "N/A"))
        df["categoria"] = df["categorias_falta"].apply(lambda x: (x or {}).get("nombre", "Otro") if x else "Otro")

    errores_df = df[df["tipo"] == "error_estandar"]
    graves_df = df[df["tipo"] == "amonestacion_grave"]

    ranking_errores = pd.DataFrame(columns=["trabajador", "Total de errores"])
    if not errores_df.empty:
        ranking_errores = (
            errores_df.groupby("trabajador").size().sort_values(ascending=False).reset_index(name="Total de errores")
        )

    ranking_graves = pd.DataFrame(columns=["trabajador", "Total amonestaciones"])
    if not graves_df.empty:
        ranking_graves = (
            graves_df.groupby("trabajador").size().sort_values(ascending=False).reset_index(name="Total amonestaciones")
        )

    ranking_categorias = pd.DataFrame(columns=["categoria", "Total"])
    if not df.empty:
        ranking_categorias = df.groupby("categoria").size().sort_values(ascending=False).reset_index(name="Total")

    actividad = pd.DataFrame(columns=["evaluador", "Total registrado"])
    if not df.empty:
        actividad = df.groupby("evaluador").size().sort_values(ascending=False).reset_index(name="Total registrado")

    cierres = (
        supabase.table("cierres_turno")
        .select("*, usuarios(nombre_completo), turnos(nombre)")
        .gte("fecha", fecha_inicio.isoformat())
        .lte("fecha", fecha_fin.isoformat())
        .order("fecha", desc=True)
        .execute()
        .data
    )

    # -------------------------------------------------------------
    # 📌 Resumen de un vistazo
    # -------------------------------------------------------------
    st.markdown("### 📌 Resumen de un vistazo")

    marca_general = "[Falta general del turno]"
    faltas_generales_df = (
        df[df["justificacion"].str.startswith(marca_general, na=False)] if not df.empty else df
    )

    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("🔸 Errores estándar", len(errores_df))
    k2.metric("🚨 Amonestaciones graves", len(graves_df))
    k3.metric("👥 Trabajadores con registro", df["trabajador"].nunique() if not df.empty else 0)
    k4.metric("🔒 Turnos cerrados", len(cierres))
    top_falta = ranking_categorias.iloc[0]["categoria"] if not ranking_categorias.empty else "—"
    k5.metric("📌 Falta más común", top_falta)

    k6, k7, k8 = st.columns(3)
    k6.metric("📢 Faltas de equipo (general)", len(faltas_generales_df))
    k7.metric("👤 Faltas individuales", len(df) - len(faltas_generales_df) if not df.empty else 0)
    top_evaluador = actividad.iloc[0]["evaluador"] if not actividad.empty else "—"
    k8.metric("📝 Evaluador más activo", top_evaluador)
    st.divider()

    # -------------------------------------------------------------
    # 🏆 Rankings (errores y amonestaciones, lado a lado)
    # -------------------------------------------------------------
    col_izq, col_der = st.columns(2)

    with col_izq:
        st.markdown("#### 🏆 Ranking de errores estándar")
        if not ranking_errores.empty:
            st.dataframe(ranking_errores, use_container_width=True, hide_index=True)
            fig = px.bar(
                ranking_errores, x="trabajador", y="Total de errores", text="Total de errores",
                color_discrete_sequence=["#F2A007"],
            )
            fig.update_traces(textposition="outside")
            fig.update_layout(xaxis_title="", yaxis_title="", margin=dict(t=10, b=10))
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.caption("Sin errores estándar con estos filtros.")

    with col_der:
        st.markdown("#### 🚨 Ranking de amonestaciones graves")
        if not ranking_graves.empty:
            st.dataframe(ranking_graves, use_container_width=True, hide_index=True)
            fig = px.bar(
                ranking_graves, x="trabajador", y="Total amonestaciones", text="Total amonestaciones",
                color_discrete_sequence=["#E4572E"],
            )
            fig.update_traces(textposition="outside")
            fig.update_layout(xaxis_title="", yaxis_title="", margin=dict(t=10, b=10))
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.caption("Sin amonestaciones graves con estos filtros.")

    st.markdown("#### 📌 Faltas más comunes (por tipo)")
    if not ranking_categorias.empty:
        col_tabla, col_grafico = st.columns([1, 2])
        col_tabla.dataframe(ranking_categorias, use_container_width=True, hide_index=True)
        fig = px.bar(
            ranking_categorias.sort_values("Total"), x="Total", y="categoria", orientation="h",
            text="Total", color_discrete_sequence=["#4C6EF5"],
        )
        fig.update_traces(textposition="outside")
        fig.update_layout(xaxis_title="", yaxis_title="", margin=dict(t=10, b=10))
        col_grafico.plotly_chart(fig, use_container_width=True)
    else:
        st.caption("Sin datos suficientes con estos filtros.")

    st.divider()

    # -------------------------------------------------------------
    # 📈 Tendencia + 🌟 Reconocimiento
    # -------------------------------------------------------------
    st.markdown("#### 📈 Tendencia en el tiempo (por semana)")
    if not df.empty:
        df_trend = df.copy()
        df_trend["fecha_dt"] = pd.to_datetime(df_trend["fecha"])
        tendencia = df_trend.groupby([pd.Grouper(key="fecha_dt", freq="W"), "tipo"]).size().unstack(fill_value=0)
        tendencia = tendencia.rename(
            columns={"error_estandar": "Errores estándar", "amonestacion_grave": "Amonestaciones graves"}
        )
        tendencia_larga = tendencia.reset_index().melt(
            id_vars="fecha_dt", var_name="Tipo", value_name="Cantidad"
        )
        fig = px.line(
            tendencia_larga, x="fecha_dt", y="Cantidad", color="Tipo", markers=True,
            color_discrete_map={"Errores estándar": "#F2A007", "Amonestaciones graves": "#E4572E"},
        )
        fig.update_layout(xaxis_title="", yaxis_title="", legend_title="", margin=dict(t=10, b=10))
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.caption("No hay suficientes datos para mostrar una tendencia con estos filtros.")

    st.markdown("#### 🌟 Reconocimiento — sin ningún registro en este rango")
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

    st.divider()

    # -------------------------------------------------------------
    # 📋 Detalle, actividad por evaluador y exportación
    # -------------------------------------------------------------
    st.markdown("#### 📋 Detalle, actividad y exportación")

    with st.expander("Ver detalle completo (todas las justificaciones)"):
        detalle = df[
            ["fecha", "trabajador", "area", "tipo", "categoria", "evaluador", "justificacion", "porcentaje_bono", "imagen_url"]
        ].rename(columns={"imagen_url": "foto", "porcentaje_bono": "% bono"}).sort_values("fecha", ascending=False)
        st.dataframe(
            detalle,
            use_container_width=True,
            hide_index=True,
            column_config={"foto": st.column_config.LinkColumn("foto", display_text="Ver foto")},
        )

    with st.expander("Ver registros hechos por cada evaluador"):
        if not actividad.empty:
            st.dataframe(actividad, use_container_width=True, hide_index=True)
        else:
            st.caption("Sin actividad registrada con estos filtros.")

    if df.empty:
        st.caption("No hay datos con estos filtros para exportar a Excel.")
    else:
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            df[
                ["fecha", "trabajador", "area", "tipo", "categoria", "evaluador", "justificacion", "porcentaje_bono", "imagen_url"]
            ].rename(columns={"imagen_url": "foto", "porcentaje_bono": "% bono"}).sort_values("fecha").to_excel(
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

    st.divider()

    # -------------------------------------------------------------
    # 🔒 Turnos cerrados
    # -------------------------------------------------------------
    with st.expander("🔒 Ver turnos cerrados en este rango", expanded=False):
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

    st.divider()

    # -------------------------------------------------------------
    # 🔍 Historial completo por trabajador (auditoría)
    # -------------------------------------------------------------
    st.markdown("#### 🔍 Historial completo por trabajador (auditoría)")
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
            df_hist["% bono"] = df_hist.get("porcentaje_bono", pd.Series(dtype="Int64"))
            tabla_hist = df_hist[
                ["fecha", "tipo_texto", "categoria", "evaluador", "justificacion", "% bono", "foto", "hora exacta"]
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
                pct_texto = f" — 💰 bono: {h['porcentaje_bono']}%" if h.get("porcentaje_bono") is not None else ""

                with st.container(border=True):
                    st.write(f"{icono} **{h['fecha']}** — {tipo_texto} — **[{categoria_texto}]**{pct_texto} — evaluó: *{evaluador_nombre}*")
                    st.caption(h["justificacion"])
                    if h.get("imagen_url"):
                        mostrar_evidencia(h["imagen_url"])

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
def admin_sedes(usuario):
    st.header("📍 Gestión de Sedes")
    st.caption(
        "Una sede es una ubicación física (ej. 'Costa América', 'Aeropuerto'). Cada área "
        "pertenece a una sede, y el cierre de turno es independiente por sede."
    )
    supabase = get_supabase_client()

    with st.form("nueva_sede", clear_on_submit=True):
        nombre = st.text_input("Nombre de la sede (ej. Costa América, Aeropuerto)")
        submit = st.form_submit_button("➕ Agregar sede")
        if submit:
            if not nombre.strip():
                st.error("El nombre no puede estar vacío.")
            else:
                existe = supabase.table("sedes").select("id").eq("nombre", nombre.strip()).execute().data
                if existe:
                    st.error("Ya existe una sede con ese nombre.")
                else:
                    supabase.table("sedes").insert({"nombre": nombre.strip()}).execute()
                    registrar_log(usuario, "Agregó sede", nombre.strip())
                    st.success(f"Sede '{nombre.strip()}' agregada.")
                    st.rerun()

    st.subheader("Sedes registradas")
    sedes = supabase.table("sedes").select("*").order("nombre").execute().data

    for s in sedes:
        c1, c2, c3 = st.columns([3, 1, 1])
        c1.write(s["nombre"])
        c2.write("🟢 Activa" if s["activo"] else "🔴 Inactiva")
        if c3.button("Activar/Desactivar", key=f"toggle_sede_{s['id']}"):
            supabase.table("sedes").update({"activo": not s["activo"]}).eq("id", s["id"]).execute()
            registrar_log(usuario, "Cambió estado de sede", s["nombre"])
            st.rerun()


def admin_areas(usuario):
    st.header("🏷️ Gestión de Áreas")
    supabase = get_supabase_client()

    sedes = cargar_sedes(supabase, solo_activas=False)
    if not sedes:
        st.warning("Primero crea al menos una sede en 'Sedes'.")
        return
    sedes_map = {s["nombre"]: s["id"] for s in sedes}
    sedes_map_inv = {v: k for k, v in sedes_map.items()}

    with st.form("nueva_area", clear_on_submit=True):
        nombre = st.text_input("Nombre del área (ej. Cocina, Barra, Panadería)")
        sede_sel = st.selectbox("Sede a la que pertenece", list(sedes_map.keys()))
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
                        {
                            "nombre": nombre.strip(),
                            "max_errores_estandar": int(max_err),
                            "sede_id": sedes_map[sede_sel],
                        }
                    ).execute()
                    registrar_log(usuario, "Agregó área", f"{nombre.strip()} ({sede_sel})")
                    st.success(f"Área '{nombre.strip()}' agregada a la sede '{sede_sel}'.")
                    st.rerun()

    st.subheader("Áreas registradas")
    areas = supabase.table("areas").select("*").order("nombre").execute().data

    for a in areas:
        sede_actual_nombre = sedes_map_inv.get(a.get("sede_id"), "Sin sede")

        c1, c2, c3, c4, c5 = st.columns([2, 2, 1, 1, 1])
        c1.write(a["nombre"])
        c2.write(f"📍 {sede_actual_nombre} · Máx: {a['max_errores_estandar']}")
        c3.write("🟢 Activa" if a["activo"] else "🔴 Inactiva")

        edit_key = f"edit_area_{a['id']}"
        if edit_key not in st.session_state:
            st.session_state[edit_key] = False
        if c4.button("Editar", key=f"btn_edit_area_{a['id']}"):
            st.session_state[edit_key] = not st.session_state[edit_key]
            st.rerun()

        if st.session_state[edit_key]:
            with st.form(key=f"form_edit_area_{a['id']}"):
                nueva_sede = st.selectbox(
                    "Sede",
                    list(sedes_map.keys()),
                    index=list(sedes_map.keys()).index(sede_actual_nombre) if sede_actual_nombre in sedes_map else 0,
                    key=f"sede_edit_{a['id']}",
                )
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
                    supabase.table("areas").update(
                        {"max_errores_estandar": int(nuevo_max), "sede_id": sedes_map[nueva_sede]}
                    ).eq("id", a["id"]).execute()
                    registrar_log(usuario, "Editó área", f"{a['nombre']}: sede={nueva_sede}, máx={nuevo_max}")
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

    todas_las_areas = cargar_areas(supabase, solo_activas=False)
    areas_nombre_a_id = {a["nombre"]: a["id"] for a in todas_las_areas}

    with st.form("nuevo_usuario", clear_on_submit=True):
        nombre_completo = st.text_input("Nombre completo")
        nombre_usuario = st.text_input("Usuario (para iniciar sesión)")
        password = st.text_input("Contraseña temporal", type="password")
        rol = st.selectbox("Rol", ["evaluador", "admin_general"])
        areas_sel = st.multiselect(
            "Áreas que puede ver y evaluar (déjalo vacío para que vea TODAS)",
            list(areas_nombre_a_id.keys()),
        )
        st.caption("Permisos (solo aplican si el rol es 'evaluador' — el Administrador General siempre tiene todo):")
        pc1, pc2 = st.columns(2)
        p_cerrar_turno = pc1.checkbox("Puede cerrar turnos", value=True)
        p_falta_general = pc2.checkbox("Puede registrar falta general", value=True)
        pc3, pc4 = st.columns(2)
        p_ver_dashboard = pc3.checkbox("Puede ver el Dashboard", value=True)
        p_editar_revision = pc4.checkbox("Puede editar/eliminar antes de cerrar turno", value=True)
        st.caption("Estos dos dan acceso a páginas normalmente solo del Administrador General:")
        pc5, pc6 = st.columns(2)
        p_cargar_trabajadores = pc5.checkbox("Puede cargar trabajadores", value=False)
        p_crear_areas = pc6.checkbox("Puede crear áreas", value=False)
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
                    nuevo = (
                        supabase.table("usuarios")
                        .insert(
                            {
                                "nombre_completo": nombre_completo.strip(),
                                "nombre_usuario": nombre_usuario.strip(),
                                "password_hash": hash_password(password.strip()),
                                "rol": rol,
                                "puede_cerrar_turno": p_cerrar_turno,
                                "puede_falta_general": p_falta_general,
                                "puede_ver_dashboard": p_ver_dashboard,
                                "puede_editar_revision": p_editar_revision,
                                "puede_cargar_trabajadores": p_cargar_trabajadores,
                                "puede_crear_areas": p_crear_areas,
                            }
                        )
                        .execute()
                        .data
                    )
                    nuevo_id = nuevo[0]["id"]
                    if areas_sel:
                        supabase.table("usuario_areas").insert(
                            [{"usuario_id": nuevo_id, "area_id": areas_nombre_a_id[a]} for a in areas_sel]
                        ).execute()
                    registrar_log(usuario, "Creó usuario", nombre_usuario.strip())
                    st.success("Usuario creado.")
                    st.rerun()

    st.subheader("Usuarios registrados")
    usuarios_lista = supabase.table("usuarios").select("*").order("nombre_completo").execute().data
    admins_activos = sum(1 for x in usuarios_lista if x["rol"] == "admin_general" and x["activo"])

    for u in usuarios_lista:
        c1, c2, c3, c4, c5, c6, c7, c8 = st.columns([2, 2, 1, 1, 1, 1, 1, 1])
        c1.write(u["nombre_completo"])
        c2.write(u["rol"])
        c3.write("🟢" if u["activo"] else "🔴")

        es_ultimo_admin_activo = u["rol"] == "admin_general" and u["activo"] and admins_activos <= 1

        confirm_key = f"confirm_toggle_usuario_{u['id']}"
        if confirm_key not in st.session_state:
            st.session_state[confirm_key] = False

        if not st.session_state[confirm_key]:
            if c4.button("Activar/Desactivar", key=f"toggle_usuario_{u['id']}"):
                if u["id"] == usuario["id"]:
                    st.error("No puedes desactivarte a ti mismo.")
                elif es_ultimo_admin_activo:
                    st.error("No puedes desactivar al único Administrador General activo.")
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

        mostrar_areas_key = f"mostrar_areas_{u['id']}"
        if mostrar_areas_key not in st.session_state:
            st.session_state[mostrar_areas_key] = False

        if c6.button("🏷️ Áreas", key=f"btn_areas_{u['id']}"):
            st.session_state[mostrar_areas_key] = not st.session_state[mostrar_areas_key]

        if st.session_state[mostrar_areas_key]:
            areas_actuales_ids = {
                r["area_id"]
                for r in supabase.table("usuario_areas").select("area_id").eq("usuario_id", u["id"]).execute().data
            }
            areas_actuales_nombres = [a["nombre"] for a in todas_las_areas if a["id"] in areas_actuales_ids]

            with st.form(key=f"form_areas_{u['id']}"):
                st.write(f"Áreas de **{u['nombre_completo']}** (vacío = ve todas)")
                nuevas_areas_sel = st.multiselect(
                    "Áreas asignadas",
                    list(areas_nombre_a_id.keys()),
                    default=areas_actuales_nombres,
                    key=f"multiselect_areas_{u['id']}",
                )
                if st.form_submit_button("💾 Guardar áreas"):
                    supabase.table("usuario_areas").delete().eq("usuario_id", u["id"]).execute()
                    if nuevas_areas_sel:
                        supabase.table("usuario_areas").insert(
                            [{"usuario_id": u["id"], "area_id": areas_nombre_a_id[a]} for a in nuevas_areas_sel]
                        ).execute()
                    registrar_log(
                        usuario, "Cambió áreas asignadas de usuario", f"{u['nombre_usuario']}: {nuevas_areas_sel}"
                    )
                    st.session_state[mostrar_areas_key] = False
                    st.success("Áreas actualizadas.")
                    st.rerun()

        mostrar_permisos_key = f"mostrar_permisos_{u['id']}"
        if mostrar_permisos_key not in st.session_state:
            st.session_state[mostrar_permisos_key] = False

        if c8.button("⚙️ Permisos", key=f"btn_permisos_{u['id']}"):
            st.session_state[mostrar_permisos_key] = not st.session_state[mostrar_permisos_key]

        if st.session_state[mostrar_permisos_key]:
            if u["rol"] == "admin_general":
                st.info(f"'{u['nombre_completo']}' es Administrador General: siempre tiene todos los permisos.")
            else:
                with st.form(key=f"form_permisos_{u['id']}"):
                    st.write(f"Permisos de **{u['nombre_completo']}**")
                    pp1, pp2 = st.columns(2)
                    np_cerrar_turno = pp1.checkbox(
                        "Puede cerrar turnos", value=u.get("puede_cerrar_turno", True), key=f"perm_cerrar_{u['id']}"
                    )
                    np_falta_general = pp2.checkbox(
                        "Puede registrar falta general",
                        value=u.get("puede_falta_general", True),
                        key=f"perm_general_{u['id']}",
                    )
                    pp3, pp4 = st.columns(2)
                    np_ver_dashboard = pp3.checkbox(
                        "Puede ver el Dashboard", value=u.get("puede_ver_dashboard", True), key=f"perm_dash_{u['id']}"
                    )
                    np_editar_revision = pp4.checkbox(
                        "Puede editar/eliminar antes de cerrar turno",
                        value=u.get("puede_editar_revision", True),
                        key=f"perm_revision_{u['id']}",
                    )
                    st.caption("Estos dos dan acceso a páginas normalmente solo del Administrador General:")
                    pp5, pp6 = st.columns(2)
                    np_cargar_trabajadores = pp5.checkbox(
                        "Puede cargar trabajadores",
                        value=u.get("puede_cargar_trabajadores", False),
                        key=f"perm_trabajadores_{u['id']}",
                    )
                    np_crear_areas = pp6.checkbox(
                        "Puede crear áreas",
                        value=u.get("puede_crear_areas", False),
                        key=f"perm_areas_{u['id']}",
                    )
                    if st.form_submit_button("💾 Guardar permisos"):
                        supabase.table("usuarios").update(
                            {
                                "puede_cerrar_turno": np_cerrar_turno,
                                "puede_falta_general": np_falta_general,
                                "puede_ver_dashboard": np_ver_dashboard,
                                "puede_editar_revision": np_editar_revision,
                                "puede_cargar_trabajadores": np_cargar_trabajadores,
                                "puede_crear_areas": np_crear_areas,
                            }
                        ).eq("id", u["id"]).execute()
                        registrar_log(usuario, "Cambió permisos de usuario", u["nombre_usuario"])
                        st.session_state[mostrar_permisos_key] = False
                        st.success("Permisos actualizados.")
                        st.rerun()

        delete_key = f"confirm_delete_usuario_{u['id']}"
        if delete_key not in st.session_state:
            st.session_state[delete_key] = False

        if not st.session_state[delete_key]:
            if c7.button("🗑️ Eliminar", key=f"btn_delete_usuario_{u['id']}"):
                if u["id"] == usuario["id"]:
                    st.error("No puedes eliminarte a ti mismo.")
                elif es_ultimo_admin_activo:
                    st.error("No puedes eliminar al único Administrador General activo.")
                else:
                    tiene_evaluaciones = (
                        supabase.table("evaluaciones").select("id").eq("evaluador_id", u["id"]).limit(1).execute().data
                    )
                    tiene_cierres = (
                        supabase.table("cierres_turno").select("id").eq("evaluador_id", u["id"]).limit(1).execute().data
                    )
                    if tiene_evaluaciones or tiene_cierres:
                        st.error(
                            f"'{u['nombre_completo']}' tiene historial de registros/cierres y no se puede "
                            "eliminar (se perdería el rastro de auditoría). Puedes desactivarlo en su lugar."
                        )
                    else:
                        st.session_state[delete_key] = True
                        st.rerun()
        else:
            st.warning(f"¿Seguro que quieres eliminar a **{u['nombre_completo']}**? Esto no se puede deshacer.")
            cd1, cd2 = st.columns(2)
            if cd1.button("✅ Sí, eliminar definitivamente", key=f"yes_delete_usuario_{u['id']}"):
                supabase.table("usuario_areas").delete().eq("usuario_id", u["id"]).execute()
                # Borra sus propios logs (ej. "Inició sesión") para no chocar con la
                # llave foránea; el registro de que TÚ lo eliminaste se guarda después,
                # a tu nombre, no al de la persona eliminada.
                supabase.table("logs_auditoria").delete().eq("usuario_id", u["id"]).execute()
                supabase.table("usuarios").delete().eq("id", u["id"]).execute()
                registrar_log(usuario, "Eliminó usuario", u["nombre_usuario"])
                st.session_state[delete_key] = False
                st.success(f"Usuario '{u['nombre_completo']}' eliminado.")
                st.rerun()
            if cd2.button("Cancelar", key=f"no_delete_usuario_{u['id']}"):
                st.session_state[delete_key] = False
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
    ahora = time.time()
    if "ultima_actividad" not in st.session_state:
        st.session_state.ultima_actividad = ahora

    if ahora - st.session_state.ultima_actividad > INACTIVIDAD_MAXIMA_SEGUNDOS:
        usuario_inactivo = st.session_state.usuario
        registrar_log(usuario_inactivo, "Sesión cerrada por inactividad")
        st.session_state.usuario = None
        st.session_state.pop("ultima_actividad", None)
        st.warning("⏰ Tu sesión se cerró por inactividad (25 minutos sin uso). Vuelve a iniciar sesión.")
        st.stop()

    st.session_state.ultima_actividad = ahora

    usuario_actual = st.session_state.usuario

    st.sidebar.title(f"👤 {usuario_actual['nombre_completo']}")
    st.sidebar.caption(f"Rol: {usuario_actual['rol']}")
    st.sidebar.markdown("---")

    opciones = ["📋 Panel Diario", "⚙️ Mi cuenta"]
    if tiene_permiso(usuario_actual, "puede_ver_dashboard"):
        opciones.insert(1, "📊 Dashboard")
    if usuario_actual["rol"] == "admin_general":
        opciones += [
            "📍 Sedes",
            "👥 Trabajadores",
            "🏷️ Áreas",
            "🕐 Turnos",
            "🗂️ Categorías de Falta",
            "🔑 Usuarios",
            "🕵️ Auditoría",
        ]
    else:
        if tiene_permiso(usuario_actual, "puede_cargar_trabajadores"):
            opciones.append("👥 Trabajadores")
        if tiene_permiso(usuario_actual, "puede_crear_areas"):
            opciones.append("🏷️ Áreas")

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
    elif seleccion == "📍 Sedes":
        admin_sedes(usuario_actual)
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
