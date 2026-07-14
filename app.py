"""
Sistema de Control de Mesoneros
--------------------------------
App interna para 5 evaluadores/administradores que registran los
errores diarios de 15 mesoneros, con auditoría completa y un
dashboard de rating/amonestaciones. Datos persistidos en Supabase.
"""

import io
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

    st.caption("¿Olvidaste tu contraseña? Pídele al Administrador General que te la restablezca.")


def cerrar_sesion():
    registrar_log(st.session_state.usuario, "Cerró sesión")
    st.session_state.usuario = None
    st.rerun()


# =================================================================
# PANEL DIARIO
# =================================================================
def panel_diario(usuario):
    st.header("📋 Panel de Control Diario")

    supabase = get_supabase_client()
    hoy = date.today().isoformat()

    cierres_hoy = (
        supabase.table("cierres_turno")
        .select("*, usuarios(nombre_completo)")
        .eq("fecha", hoy)
        .order("turno")
        .execute()
        .data
    )
    turno_actual = len(cierres_hoy) + 1

    st.caption(
        f"{date.today().strftime('%d/%m/%Y')} — Turno **#{turno_actual}** en curso. "
        "El histórico completo está en el Dashboard."
    )
    if cierres_hoy:
        resumen_cierres = ", ".join(
            f"Turno #{c['turno']} por {(c.get('usuarios') or {}).get('nombre_completo', 'N/D')}"
            for c in cierres_hoy
        )
        st.info(f"Turnos ya cerrados hoy: {resumen_cierres}")

    mesoneros_todos = (
        supabase.table("mesoneros").select("*").eq("activo", True).order("nombre_completo").execute().data
    )

    if not mesoneros_todos:
        st.info("Todavía no hay mesoneros registrados. Pide al Administrador General que los agregue en 'Mesoneros'.")
        return

    busqueda = st.text_input("🔍 Buscar mesonero por nombre", placeholder="Escribe un nombre para filtrar la lista...")
    if busqueda.strip():
        mesoneros = [m for m in mesoneros_todos if busqueda.strip().lower() in m["nombre_completo"].lower()]
        if not mesoneros:
            st.warning(f"No se encontró ningún mesonero activo que coincida con '{busqueda.strip()}'.")
            return
    else:
        mesoneros = mesoneros_todos

    for mesonero in mesoneros:
        evals_dia = (
            supabase.table("evaluaciones")
            .select("*, usuarios(nombre_completo)")
            .eq("mesonero_id", mesonero["id"])
            .eq("fecha", hoy)
            .execute()
            .data
        )
        # El tope de 3 errores es ACUMULADO POR DÍA COMPLETO, sin importar cuántos
        # turnos haya habido, para que nadie pueda "resetear" su tope cerrando turno.
        errores_dia = [e for e in evals_dia if e["tipo"] == "error_estandar"]
        amonestaciones_dia = [e for e in evals_dia if e["tipo"] == "amonestacion_grave"]

        # Lo que se MUESTRA como "de este turno" sí arranca en 0 con cada turno nuevo.
        errores_turno = [e for e in errores_dia if e.get("turno", 1) == turno_actual]
        amonestaciones_turno = [e for e in amonestaciones_dia if e.get("turno", 1) == turno_actual]

        with st.container(border=True):
            col_info, col_accion = st.columns([2, 3])

            with col_info:
                st.subheader(mesonero["nombre_completo"])
                m1, m2 = st.columns(2)
                m1.metric("Errores en este turno", len(errores_turno))
                m2.metric("Amonestaciones en este turno", len(amonestaciones_turno))
                st.caption(f"Total acumulado hoy (todos los turnos): {len(errores_dia)}/{MAX_ERRORES_ESTANDAR} errores · {len(amonestaciones_dia)} amonestaciones")

                if errores_turno:
                    with st.expander("Ver justificaciones de errores de este turno"):
                        for e in errores_turno:
                            evaluador_nombre = (e.get("usuarios") or {}).get("nombre_completo", "N/D")
                            st.caption(f"• *(evaluó: {evaluador_nombre})* — {e['justificacion']}")
                if amonestaciones_turno:
                    with st.expander("Ver amonestaciones graves de este turno"):
                        for e in amonestaciones_turno:
                            evaluador_nombre = (e.get("usuarios") or {}).get("nombre_completo", "N/D")
                            st.caption(f"⚠️ *(evaluó: {evaluador_nombre})* — {e['justificacion']}")
                if len(errores_dia) > len(errores_turno) or len(amonestaciones_dia) > len(amonestaciones_turno):
                    with st.expander("Ver TODO lo registrado hoy (turnos anteriores incluidos)"):
                        for e in evals_dia:
                            evaluador_nombre = (e.get("usuarios") or {}).get("nombre_completo", "N/D")
                            icono = "🔸" if e["tipo"] == "error_estandar" else "🚨"
                            numero_turno = e.get("turno", 1)
                            st.caption(f"{icono} *(turno #{numero_turno} — evaluó: {evaluador_nombre})* — {e['justificacion']}")

            with col_accion:
                puede_error_estandar = len(errores_dia) < MAX_ERRORES_ESTANDAR

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
                                        "turno": turno_actual,
                                        "mesonero_id": mesonero["id"],
                                        "evaluador_id": usuario["id"],
                                        "tipo": "error_estandar",
                                        "justificacion": justificacion.strip(),
                                    }
                                ).execute()
                                registrar_log(
                                    usuario,
                                    "Registró error estándar",
                                    f"{mesonero['nombre_completo']} (turno #{turno_actual}): {justificacion.strip()}",
                                )
                                st.rerun()
                else:
                    st.warning(
                        f"⚠️ **{mesonero['nombre_completo']}** ya alcanzó el máximo de "
                        f"{MAX_ERRORES_ESTANDAR} errores estándar HOY (acumulado de todos los turnos). "
                        "El próximo registro debe ser una amonestación grave."
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
                                        "turno": turno_actual,
                                        "mesonero_id": mesonero["id"],
                                        "evaluador_id": usuario["id"],
                                        "tipo": "amonestacion_grave",
                                        "justificacion": justificacion.strip(),
                                    }
                                ).execute()
                                registrar_log(
                                    usuario,
                                    "Registró amonestación grave (por exceso de errores)",
                                    f"{mesonero['nombre_completo']} (turno #{turno_actual}): {justificacion.strip()}",
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
                                        "turno": turno_actual,
                                        "mesonero_id": mesonero["id"],
                                        "evaluador_id": usuario["id"],
                                        "tipo": "amonestacion_grave",
                                        "justificacion": justificacion_directa.strip(),
                                    }
                                ).execute()
                                registrar_log(
                                    usuario,
                                    "Registró amonestación grave directa",
                                    f"{mesonero['nombre_completo']} (turno #{turno_actual}): {justificacion_directa.strip()}",
                                )
                                st.rerun()

    st.markdown("---")

    revisar_key = "revisando_cierre"
    if revisar_key not in st.session_state:
        st.session_state[revisar_key] = False

    if not st.session_state[revisar_key]:
        if st.button(f"✅ Guardar y cortar turno #{turno_actual}", type="primary"):
            st.session_state[revisar_key] = True
            st.rerun()
    else:
        st.subheader(f"🔍 Revisión antes de cerrar el turno #{turno_actual}")
        st.caption(
            "Revisa los registros de ESTE turno (de todos los evaluadores). Si alguien se equivocó "
            "al escribir, corrígelo o elimínalo aquí antes de confirmar. Si todo está bien, "
            "confirma directamente abajo."
        )

        evaluaciones_turno_completo = (
            supabase.table("evaluaciones")
            .select("*, mesoneros(nombre_completo), usuarios(nombre_completo)")
            .eq("fecha", hoy)
            .eq("turno", turno_actual)
            .order("created_at")
            .execute()
            .data
        )

        if not evaluaciones_turno_completo:
            st.info("No hay ningún registro en este turno. Puedes confirmar el cierre igualmente.")
        else:
            for h in evaluaciones_turno_completo:
                mesonero_nombre = (h.get("mesoneros") or {}).get("nombre_completo", "N/D")
                evaluador_nombre = (h.get("usuarios") or {}).get("nombre_completo", "N/D")
                tipo_texto = "Error estándar" if h["tipo"] == "error_estandar" else "Amonestación grave"
                icono = "🔸" if h["tipo"] == "error_estandar" else "🚨"

                with st.container(border=True):
                    col_texto, col_btn = st.columns([4, 1])
                    col_texto.write(f"{icono} **{mesonero_nombre}** — {tipo_texto} — evaluó: *{evaluador_nombre}*")
                    col_texto.caption(h["justificacion"])

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
                                        f"{mesonero_nombre}: {nueva_just.strip()}",
                                    )
                                    st.session_state[edit_key] = False
                                    st.rerun()

                            if eliminar_edit:
                                supabase.table("evaluaciones").delete().eq("id", h["id"]).execute()
                                registrar_log(
                                    usuario,
                                    "Eliminó registro antes de cerrar turno",
                                    f"{mesonero_nombre}: {h['justificacion']}",
                                )
                                st.session_state[edit_key] = False
                                st.rerun()

        st.markdown("---")
        col_confirmar, col_cancelar = st.columns(2)
        if col_confirmar.button(f"✅ Confirmar y cerrar turno #{turno_actual}", type="primary"):
            supabase.table("cierres_turno").insert(
                {
                    "fecha": hoy,
                    "turno": turno_actual,
                    "evaluador_id": usuario["id"],
                }
            ).execute()
            registrar_log(usuario, "Cerró turno", f"Fecha: {hoy}, turno #{turno_actual}")
            st.session_state[revisar_key] = False
            st.success(
                f"Turno #{turno_actual} cerrado por **{usuario['nombre_completo']}**. El próximo turno "
                "arranca en 0 en el panel — el tope de 3 errores por día sigue contando desde el total "
                "acumulado de hoy."
            )
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
        st.info("No hay registros en el rango de fechas seleccionado (arriba). El historial completo por mesonero, más abajo, no depende de este rango.")
        df = pd.DataFrame(columns=["fecha", "mesonero", "evaluador", "tipo", "justificacion"])
    else:
        df = pd.DataFrame(evaluaciones)
        df["mesonero"] = df["mesoneros"].apply(lambda x: x["nombre_completo"] if x else "N/A")
        df["evaluador"] = df["usuarios"].apply(lambda x: x["nombre_completo"] if x else "N/A")

    st.subheader("🏆 Ranking de errores estándar (mayor a menor)")
    errores_df = df[df["tipo"] == "error_estandar"]
    ranking_errores = pd.DataFrame(columns=["mesonero", "Total de errores"])
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
    ranking_graves = pd.DataFrame(columns=["mesonero", "Total amonestaciones"])
    if not graves_df.empty:
        ranking_graves = (
            graves_df.groupby("mesonero").size().sort_values(ascending=False).reset_index(name="Total amonestaciones")
        )
        st.dataframe(ranking_graves, use_container_width=True, hide_index=True)
        st.bar_chart(ranking_graves.set_index("mesonero"))
    else:
        st.caption("Sin amonestaciones graves en este rango.")

    st.subheader("📈 Tendencia en el tiempo (por semana)")
    if not df.empty:
        df_trend = df.copy()
        df_trend["fecha_dt"] = pd.to_datetime(df_trend["fecha"])
        tendencia = (
            df_trend.groupby([pd.Grouper(key="fecha_dt", freq="W"), "tipo"]).size().unstack(fill_value=0)
        )
        tendencia = tendencia.rename(
            columns={"error_estandar": "Errores estándar", "amonestacion_grave": "Amonestaciones graves"}
        )
        st.line_chart(tendencia)
    else:
        st.caption("No hay suficientes datos para mostrar una tendencia en este rango.")

    st.subheader("🌟 Reconocimiento — sin ningún registro en este rango")
    todos_activos = supabase.table("mesoneros").select("*").eq("activo", True).execute().data
    mesoneros_con_registro = set(df["mesonero"].unique()) if not df.empty else set()
    sin_registros = [m["nombre_completo"] for m in todos_activos if m["nombre_completo"] not in mesoneros_con_registro]
    if sin_registros:
        st.success("👏 " + ", ".join(sorted(sin_registros)))
    else:
        st.caption("Todos los mesoneros activos tuvieron al menos un registro en este rango.")

    with st.expander("Ver detalle completo (todas las justificaciones)"):
        detalle = df[["fecha", "mesonero", "tipo", "evaluador", "justificacion"]].sort_values(
            "fecha", ascending=False
        )
        st.dataframe(detalle, use_container_width=True, hide_index=True)

    st.subheader("📝 Registros hechos por cada evaluador")
    actividad = pd.DataFrame(columns=["evaluador", "Total registrado"])
    if not df.empty:
        actividad = df.groupby("evaluador").size().sort_values(ascending=False).reset_index(name="Total registrado")
        st.dataframe(actividad, use_container_width=True, hide_index=True)
    else:
        st.caption("Sin actividad registrada en este rango.")

    st.markdown("---")
    if df.empty:
        st.caption("No hay datos en este rango para exportar a Excel.")
    else:
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            df[["fecha", "mesonero", "tipo", "evaluador", "justificacion"]].sort_values("fecha").to_excel(
                writer, sheet_name="Detalle", index=False
            )
            ranking_errores.to_excel(writer, sheet_name="Ranking Errores", index=False)
            ranking_graves.to_excel(writer, sheet_name="Ranking Amonestaciones", index=False)
            actividad.to_excel(writer, sheet_name="Actividad Evaluador", index=False)
        st.download_button(
            "📥 Descargar este reporte en Excel",
            data=buffer.getvalue(),
            file_name=f"reporte_mesoneros_{fecha_inicio.isoformat()}_{fecha_fin.isoformat()}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

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
            df_cierres[["fecha", "turno", "evaluador", "fecha_hora"]].rename(
                columns={"turno": "# turno", "fecha_hora": "hora exacta"}
            ),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.caption("Ningún turno se ha cerrado con el botón 'Guardar y cortar turno' en este rango.")

    st.markdown("---")
    st.subheader("🔍 Historial completo por mesonero (auditoría)")
    st.caption(
        "Esta sección muestra TODO el historial de un mesonero (no depende del rango de fechas de arriba), "
        "entrada por entrada, con quién evaluó cada una — para poder auditar un caso puntual."
    )

    todos_mesoneros = supabase.table("mesoneros").select("*").order("nombre_completo").execute().data

    if not todos_mesoneros:
        st.caption("No hay mesoneros registrados todavía.")
    else:
        opciones_mesonero = {
            f"{m['nombre_completo']}" + ("" if m["activo"] else " (inactivo)"): m["id"] for m in todos_mesoneros
        }
        mesonero_sel = st.selectbox("Selecciona un mesonero", list(opciones_mesonero.keys()))

        historial = (
            supabase.table("evaluaciones")
            .select("*, usuarios(nombre_completo)")
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
            df_hist["evaluador"] = df_hist["usuarios"].apply(lambda x: x["nombre_completo"] if x else "N/A")
            df_hist["tipo_texto"] = df_hist["tipo"].map(
                {"error_estandar": "Error estándar", "amonestacion_grave": "Amonestación grave"}
            )
            tabla_hist = df_hist[["fecha", "tipo_texto", "evaluador", "justificacion", "created_at"]].rename(
                columns={"tipo_texto": "tipo", "created_at": "hora exacta"}
            )
            st.dataframe(tabla_hist, use_container_width=True, hide_index=True)

            c1, c2 = st.columns(2)
            c1.metric("Total errores estándar (histórico)", int((df_hist["tipo"] == "error_estandar").sum()))
            c2.metric("Total amonestaciones graves (histórico)", int((df_hist["tipo"] == "amonestacion_grave").sum()))
        else:
            # Vista de administrador: cada registro con opción de editar/eliminar
            total_errores = sum(1 for h in historial if h["tipo"] == "error_estandar")
            total_graves = sum(1 for h in historial if h["tipo"] == "amonestacion_grave")
            c1, c2 = st.columns(2)
            c1.metric("Total errores estándar (histórico)", total_errores)
            c2.metric("Total amonestaciones graves (histórico)", total_graves)

            st.caption("Como Administrador General puedes corregir o eliminar cualquier registro (queda en Auditoría).")

            for h in historial:
                evaluador_nombre = (h.get("usuarios") or {}).get("nombre_completo", "N/D")
                tipo_texto = "Error estándar" if h["tipo"] == "error_estandar" else "Amonestación grave"
                icono = "🔸" if h["tipo"] == "error_estandar" else "🚨"

                with st.container(border=True):
                    st.write(f"{icono} **{h['fecha']}** — {tipo_texto} — evaluó: *{evaluador_nombre}*")
                    st.caption(h["justificacion"])

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
                            guardar = st.form_submit_button("💾 Guardar cambios")
                            if guardar:
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

        confirm_key = f"confirm_toggle_mesonero_{m['id']}"
        if confirm_key not in st.session_state:
            st.session_state[confirm_key] = False

        if not st.session_state[confirm_key]:
            if c3.button("Cambiar estado", key=f"toggle_mesonero_{m['id']}"):
                st.session_state[confirm_key] = True
                st.rerun()
        else:
            accion_texto = "desactivar" if m["activo"] else "reactivar"
            st.warning(f"¿Confirmas {accion_texto} a **{m['nombre_completo']}**?")
            cc1, cc2 = st.columns(2)
            if cc1.button("✅ Sí, confirmar", key=f"yes_toggle_mesonero_{m['id']}"):
                supabase.table("mesoneros").update({"activo": not m["activo"]}).eq("id", m["id"]).execute()
                registrar_log(usuario, "Cambió estado de mesonero", m["nombre_completo"])
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
