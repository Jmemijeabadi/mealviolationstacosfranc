import os
from datetime import date, datetime, timedelta, time
from zoneinfo import ZoneInfo

import altair as alt
import pandas as pd
import requests
import streamlit as st
from dateutil import parser


st.set_page_config(page_title="Toast Meal Break Audit", page_icon="🛡️", layout="wide")


DEFAULT_LOCATIONS = {
    "Tacos El Franc - Gaslamp Quarter": "ac4c6cbd-8723-45cb-bf16-6d31369c5b50",
    "Tacos El Franc - National City (Plaza Bonita)": "2bef5cef-4e30-4d2a-860f-d43406a5ff29",
}

DEFAULTS = {
    "api_host": "https://ws-api.toasttab.com",
    "toast_timezone": "America/Los_Angeles",
    "waiver_limit_hours": 6.0,
    "meal_deadline_hours": 5.0,
    "second_meal_limit_hours": 12.0,
    "second_meal_deadline_hours": 10.0,
    "min_break_minutes": 30,
    "include_archived": False,
    "meal_break_type_guids": "",
}

for key, value in DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = value


def secret_or_env(name: str, default: str = "") -> str:
    if name in st.secrets:
        return str(st.secrets[name])
    return os.getenv(name, default)


def get_config():
    api_host = st.session_state["api_host"].rstrip("/")
    client_id = secret_or_env("TOAST_CLIENT_ID")
    client_secret = secret_or_env("TOAST_CLIENT_SECRET")
    toast_timezone = st.session_state["toast_timezone"]
    return {
        "api_host": api_host,
        "client_id": client_id,
        "client_secret": client_secret,
        "toast_timezone": toast_timezone,
    }


def get_auth_token(api_host: str, client_id: str, client_secret: str) -> dict:
    url = f"{api_host}/authentication/v1/authentication/login"
    payload = {
        "clientId": client_id,
        "clientSecret": client_secret,
        "userAccessType": "TOAST_MACHINE_CLIENT",
    }
    response = requests.post(url, json=payload, timeout=30)
    response.raise_for_status()
    return response.json()


def build_headers(token: str, restaurant_guid: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Toast-Restaurant-External-ID": restaurant_guid,
        "Content-Type": "application/json",
    }


def get_employees(api_host: str, token: str, restaurant_guid: str) -> list:
    url = f"{api_host}/labor/v1/employees"
    response = requests.get(url, headers=build_headers(token, restaurant_guid), timeout=60)
    response.raise_for_status()
    return response.json()


def format_toast_utc(dt: datetime) -> str:
    dt_utc = dt.astimezone(ZoneInfo("UTC"))
    return dt_utc.strftime("%Y-%m-%dT%H:%M:%S.000%z")


def get_time_entries(
    api_host: str,
    token: str,
    restaurant_guid: str,
    start_dt: datetime,
    end_dt: datetime,
    include_missed_breaks: bool = True,
    include_archived: bool = False,
) -> list:
    url = f"{api_host}/labor/v1/timeEntries"
    params = {
        "startDate": format_toast_utc(start_dt),
        "endDate": format_toast_utc(end_dt),
        "includeMissedBreaks": str(include_missed_breaks).lower(),
        "includeArchived": str(include_archived).lower(),
    }
    response = requests.get(
        url,
        headers=build_headers(token, restaurant_guid),
        params=params,
        timeout=120,
    )
    response.raise_for_status()
    return response.json()


def to_local_dt(dt_str: str | None, tz_name: str):
    if not dt_str:
        return None
    return parser.isoparse(dt_str).astimezone(ZoneInfo(tz_name))


def build_employee_lookup(employees: list) -> dict:
    lookup = {}
    for employee in employees:
        guid = employee.get("guid")
        if not guid:
            continue
        first = (employee.get("chosenName") or employee.get("firstName") or "").strip()
        last = (employee.get("lastName") or "").strip()
        display = f"{first} {last}".strip() or guid
        lookup[guid] = display
    return lookup


def safe_float(value, default=0.0):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def parse_guid_list(raw_text: str) -> set[str]:
    if not raw_text:
        return set()
    return {item.strip() for item in raw_text.split(",") if item.strip()}


def normalize_breaks(te: dict, tz_name: str, meal_break_type_guids: set[str], min_break_minutes: int) -> list:
    normalized = []
    min_td = timedelta(minutes=min_break_minutes)

    for break_item in te.get("breaks") or []:
        break_type_guid = ((break_item.get("breakType") or {}).get("guid"))
        break_start = to_local_dt(break_item.get("inDate"), tz_name)
        break_end = to_local_dt(break_item.get("outDate"), tz_name)
        missed = bool(break_item.get("missed", False))
        waived = bool(break_item.get("waived", False)) if "waived" in break_item else False
        paid = break_item.get("paid")
        audit_response = break_item.get("auditResponse")

        duration = None
        duration_minutes = None
        if break_start and break_end:
            duration = break_end - break_start
            duration_minutes = round(duration.total_seconds() / 60.0, 1)

        passes_type_filter = (not meal_break_type_guids) or (break_type_guid in meal_break_type_guids)
        passes_duration_filter = (duration is not None and duration >= min_td)

        normalized.append(
            {
                "break_type_guid": break_type_guid,
                "start": break_start,
                "end": break_end,
                "duration": duration,
                "duration_minutes": duration_minutes,
                "missed": missed,
                "waived": waived,
                "paid": paid,
                "audit_response": audit_response,
                "counts_as_meal": (not missed) and (not waived) and passes_type_filter and passes_duration_filter,
                "is_missed_meal_candidate": missed and passes_type_filter,
            }
        )

    far_future = datetime(9999, 12, 31, tzinfo=ZoneInfo(tz_name))
    normalized.sort(key=lambda item: item["start"] or far_future)
    return normalized


def audit_time_entries(
    time_entries: list,
    employee_lookup: dict,
    tz_name: str,
    waiver_limit_hours: float,
    meal_deadline_hours: float,
    second_meal_limit_hours: float,
    second_meal_deadline_hours: float,
    min_break_minutes: int,
    meal_break_type_guids: set[str],
):
    violations = []
    shift_rows = []

    for te in time_entries:
        shift_guid = te.get("guid")
        employee_guid = ((te.get("employeeReference") or {}).get("guid"))
        employee_name = employee_lookup.get(employee_guid, employee_guid or "Unknown")

        start_local = to_local_dt(te.get("inDate"), tz_name)
        end_local = to_local_dt(te.get("outDate"), tz_name)
        business_date = te.get("businessDate")

        if not start_local:
            continue

        regular_hours = safe_float(te.get("regularHours"))
        overtime_hours = safe_float(te.get("overtimeHours"))
        worked_hours = regular_hours + overtime_hours

        if worked_hours <= 0 and end_local:
            worked_hours = round((end_local - start_local).total_seconds() / 3600.0, 4)

        breaks = normalize_breaks(
            te=te,
            tz_name=tz_name,
            meal_break_type_guids=meal_break_type_guids,
            min_break_minutes=min_break_minutes,
        )
        actual_meal_breaks = [item for item in breaks if item["counts_as_meal"]]

        first_deadline = start_local + timedelta(hours=meal_deadline_hours)
        second_deadline = start_local + timedelta(hours=second_meal_deadline_hours)

        shift_rows.append(
            {
                "Nombre": employee_name,
                "Employee GUID": employee_guid,
                "Shift GUID": shift_guid,
                "Business Date": business_date,
                "Shift Start": start_local.strftime("%Y-%m-%d %I:%M %p %Z"),
                "Shift End": end_local.strftime("%Y-%m-%d %I:%M %p %Z") if end_local else "OPEN",
                "Regular Hours": round(regular_hours, 2),
                "Overtime Hours": round(overtime_hours, 2),
                "Worked Hours": round(worked_hours, 2),
                "Meal Breaks Detectados": len(actual_meal_breaks),
                "Primer Meal Deadline": first_deadline.strftime("%I:%M %p"),
                "Segundo Meal Deadline": second_deadline.strftime("%I:%M %p"),
                "Breaks (debug)": " | ".join(
                    [
                        (
                            f"{item['start'].strftime('%I:%M %p') if item['start'] else 'NA'}"
                            f"-{item['end'].strftime('%I:%M %p') if item['end'] else 'NA'}"
                            f" ({item['duration_minutes']}m, missed={item['missed']}, type={item['break_type_guid']})"
                        )
                        for item in breaks
                    ]
                ),
            }
        )

        if worked_hours > waiver_limit_hours:
            if not actual_meal_breaks:
                violations.append(
                    {
                        "Nombre": employee_name,
                        "Employee GUID": employee_guid,
                        "Shift GUID": shift_guid,
                        "Business Date": business_date,
                        "Shift Start": start_local.strftime("%Y-%m-%d %I:%M %p %Z"),
                        "Shift End": end_local.strftime("%Y-%m-%d %I:%M %p %Z") if end_local else "OPEN",
                        "Regular Hours": round(regular_hours, 2),
                        "Overtime Hours": round(overtime_hours, 2),
                        "Worked Hours": round(worked_hours, 2),
                        "Violación": "Missed Meal Break",
                        "Detalles": f"Shift > {waiver_limit_hours}h sin meal break válido.",
                    }
                )
            else:
                first_break_start = actual_meal_breaks[0]["start"]
                if first_break_start and first_break_start > first_deadline:
                    violations.append(
                        {
                            "Nombre": employee_name,
                            "Employee GUID": employee_guid,
                            "Shift GUID": shift_guid,
                            "Business Date": business_date,
                            "Shift Start": start_local.strftime("%Y-%m-%d %I:%M %p %Z"),
                            "Shift End": end_local.strftime("%Y-%m-%d %I:%M %p %Z") if end_local else "OPEN",
                            "Regular Hours": round(regular_hours, 2),
                            "Overtime Hours": round(overtime_hours, 2),
                            "Worked Hours": round(worked_hours, 2),
                            "Violación": "Late Meal Break",
                            "Detalles": (
                                f"Primer meal a las {first_break_start.strftime('%I:%M %p')}. "
                                f"Debió iniciar no después de las {first_deadline.strftime('%I:%M %p')}."
                            ),
                        }
                    )

        if worked_hours > second_meal_limit_hours:
            if len(actual_meal_breaks) < 2:
                violations.append(
                    {
                        "Nombre": employee_name,
                        "Employee GUID": employee_guid,
                        "Shift GUID": shift_guid,
                        "Business Date": business_date,
                        "Shift Start": start_local.strftime("%Y-%m-%d %I:%M %p %Z"),
                        "Shift End": end_local.strftime("%Y-%m-%d %I:%M %p %Z") if end_local else "OPEN",
                        "Regular Hours": round(regular_hours, 2),
                        "Overtime Hours": round(overtime_hours, 2),
                        "Worked Hours": round(worked_hours, 2),
                        "Violación": "Missing 2nd Meal",
                        "Detalles": (
                            f"Shift > {second_meal_limit_hours}h y solo se detectaron "
                            f"{len(actual_meal_breaks)} meal break(s) válidos."
                        ),
                    }
                )
            else:
                second_break_start = actual_meal_breaks[1]["start"]
                if second_break_start and second_break_start > second_deadline:
                    violations.append(
                        {
                            "Nombre": employee_name,
                            "Employee GUID": employee_guid,
                            "Shift GUID": shift_guid,
                            "Business Date": business_date,
                            "Shift Start": start_local.strftime("%Y-%m-%d %I:%M %p %Z"),
                            "Shift End": end_local.strftime("%Y-%m-%d %I:%M %p %Z") if end_local else "OPEN",
                            "Regular Hours": round(regular_hours, 2),
                            "Overtime Hours": round(overtime_hours, 2),
                            "Worked Hours": round(worked_hours, 2),
                            "Violación": "Late 2nd Meal",
                            "Detalles": (
                                f"Segundo meal a las {second_break_start.strftime('%I:%M %p')}. "
                                f"Debió iniciar no después de las {second_deadline.strftime('%I:%M %p')}."
                            ),
                        }
                    )

    return pd.DataFrame(violations), pd.DataFrame(shift_rows)


st.markdown(
    """
    <style>
    .metric-card {
        background: white;
        padding: 18px;
        border-radius: 12px;
        box-shadow: 0 2px 8px rgba(0,0,0,0.08);
        text-align: center;
    }
    .card-title { font-size: 14px; color: #6c757d; }
    .card-value { font-size: 28px; font-weight: 700; color: #1f2937; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("🛡️ Toast Meal Break Audit")
st.caption("Auditoría por TimeEntry usando la API de Toast, no CSV.")

with st.sidebar:
    st.header("Configuración")
    selected_location_name = st.selectbox("Location", options=list(DEFAULT_LOCATIONS.keys()))
    selected_location_guid = DEFAULT_LOCATIONS[selected_location_name]

    st.text_input("API Host", key="api_host")
    st.text_input("Timezone", key="toast_timezone", help="Ejemplo: America/Los_Angeles")

    st.markdown("#### Reglas")
    st.number_input("1er meal: exento si shift ≤ (horas)", min_value=4.0, max_value=8.0, step=0.5, key="waiver_limit_hours")
    st.number_input("1er meal deadline (hora del shift)", min_value=4.0, max_value=6.0, step=0.5, key="meal_deadline_hours")
    st.number_input("2do meal requerido si shift > (horas)", min_value=10.0, max_value=14.0, step=0.5, key="second_meal_limit_hours")
    st.number_input("2do meal deadline (hora del shift)", min_value=8.0, max_value=12.0, step=0.5, key="second_meal_deadline_hours")
    st.number_input("Duración mínima del meal (min)", min_value=10, max_value=60, step=5, key="min_break_minutes")

    st.markdown("#### Opcional")
    st.text_area(
        "Meal Break Type GUIDs (coma separados)",
        key="meal_break_type_guids",
        help="Déjalo vacío para contar cualquier break real de al menos la duración mínima. Si quieres máxima precisión, pega aquí los GUIDs de break types que correspondan a meal breaks.",
    )
    st.checkbox("Incluir archived time entries", key="include_archived")

    st.markdown("---")
    st.caption("Credenciales: usa .streamlit/secrets.toml o variables de entorno TOAST_CLIENT_ID y TOAST_CLIENT_SECRET.")

config = get_config()

if not config["client_id"] or not config["client_secret"]:
    st.error("Faltan credenciales de Toast. Guarda TOAST_CLIENT_ID y TOAST_CLIENT_SECRET en .streamlit/secrets.toml o variables de entorno.")
    st.stop()

col_a, col_b = st.columns(2)
with col_a:
    start_date = st.date_input("Fecha inicial", value=date.today() - timedelta(days=13))
with col_b:
    end_date_inclusive = st.date_input("Fecha final", value=date.today())

if end_date_inclusive < start_date:
    st.error("La fecha final no puede ser menor que la inicial.")
    st.stop()

num_days = (end_date_inclusive - start_date).days + 1
if num_days > 30:
    st.error("Toast permite consultar un periodo máximo de 30 días por llamada. Reduce el rango.")
    st.stop()

start_dt_local = datetime.combine(start_date, time.min, tzinfo=ZoneInfo(config["toast_timezone"]))
end_dt_local_exclusive = datetime.combine(end_date_inclusive + timedelta(days=1), time.min, tzinfo=ZoneInfo(config["toast_timezone"]))
meal_break_type_guids = parse_guid_list(st.session_state["meal_break_type_guids"])

st.info(
    f"Location GUID activo: {selected_location_guid}\n\n"
    f"Rango consultado: {start_dt_local.strftime('%Y-%m-%d %H:%M %Z')} → {end_dt_local_exclusive.strftime('%Y-%m-%d %H:%M %Z')}"
)

if st.button("▶️ Ejecutar auditoría", type="primary", use_container_width=True):
    try:
        with st.spinner("Autenticando con Toast..."):
            auth = get_auth_token(
                api_host=config["api_host"],
                client_id=config["client_id"],
                client_secret=config["client_secret"],
            )
            access_token = auth["token"]["accessToken"]
            expires_in = auth["token"].get("expiresIn")

        with st.spinner("Cargando empleados..."):
            employees = get_employees(
                api_host=config["api_host"],
                token=access_token,
                restaurant_guid=selected_location_guid,
            )
            employee_lookup = build_employee_lookup(employees)

        with st.spinner("Cargando time entries..."):
            time_entries = get_time_entries(
                api_host=config["api_host"],
                token=access_token,
                restaurant_guid=selected_location_guid,
                start_dt=start_dt_local,
                end_dt=end_dt_local_exclusive,
                include_missed_breaks=True,
                include_archived=st.session_state["include_archived"],
            )

        with st.spinner("Analizando meal violations..."):
            violations_df, shifts_df = audit_time_entries(
                time_entries=time_entries,
                employee_lookup=employee_lookup,
                tz_name=config["toast_timezone"],
                waiver_limit_hours=st.session_state["waiver_limit_hours"],
                meal_deadline_hours=st.session_state["meal_deadline_hours"],
                second_meal_limit_hours=st.session_state["second_meal_limit_hours"],
                second_meal_deadline_hours=st.session_state["second_meal_deadline_hours"],
                min_break_minutes=st.session_state["min_break_minutes"],
                meal_break_type_guids=meal_break_type_guids,
            )

        st.success(
            f"Auditoría completada. Token TTL: {expires_in} segundos. "
            f"Time entries analizados: {len(shifts_df)}. Violaciones detectadas: {len(violations_df)}."
        )

        c1, c2, c3, c4 = st.columns(4)
        with c1:
            st.markdown(
                f"<div class='metric-card'><div class='card-title'>Time Entries</div><div class='card-value'>{len(shifts_df)}</div></div>",
                unsafe_allow_html=True,
            )
        with c2:
            st.markdown(
                f"<div class='metric-card'><div class='card-title'>Violaciones</div><div class='card-value'>{len(violations_df)}</div></div>",
                unsafe_allow_html=True,
            )
        with c3:
            late_count = int(violations_df["Violación"].astype(str).str.contains("Late", na=False).sum()) if not violations_df.empty else 0
            st.markdown(
                f"<div class='metric-card'><div class='card-title'>Late Meals</div><div class='card-value'>{late_count}</div></div>",
                unsafe_allow_html=True,
            )
        with c4:
            affected = int(violations_df["Nombre"].nunique()) if not violations_df.empty else 0
            st.markdown(
                f"<div class='metric-card'><div class='card-title'>Empleados Afectados</div><div class='card-value'>{affected}</div></div>",
                unsafe_allow_html=True,
            )

        st.markdown("---")
        st.subheader("📊 Resumen por empleado")
        if not violations_df.empty:
            summary_df = violations_df.groupby(["Nombre", "Violación"]).size().unstack(fill_value=0)
            summary_df["Total Violaciones"] = summary_df.sum(axis=1)
            summary_df = summary_df.sort_values("Total Violaciones", ascending=False).reset_index()

            col_tbl, col_cht = st.columns([1.5, 1])
            with col_tbl:
                st.dataframe(summary_df, use_container_width=True)
                st.download_button(
                    "⬇️ Descargar resumen (CSV)",
                    data=summary_df.to_csv(index=False).encode("utf-8"),
                    file_name="toast_meal_audit_summary.csv",
                    mime="text/csv",
                    use_container_width=True,
                )
            with col_cht:
                chart = (
                    alt.Chart(summary_df)
                    .mark_bar(color="#ef4444")
                    .encode(
                        x=alt.X("Total Violaciones:Q", title="Cantidad"),
                        y=alt.Y("Nombre:N", sort="-x", title=""),
                        tooltip=["Nombre", "Total Violaciones"],
                    )
                    .properties(height=max(300, 28 * len(summary_df)))
                )
                st.altair_chart(chart, use_container_width=True)
        else:
            st.success("🎉 No se detectaron violaciones con las reglas actuales.")

        st.markdown("---")
        st.subheader("📋 Detalle de violaciones")
        if not violations_df.empty:
            st.dataframe(violations_df, use_container_width=True)
            st.download_button(
                "⬇️ Descargar detalle (CSV)",
                data=violations_df.to_csv(index=False).encode("utf-8"),
                file_name="toast_meal_audit_detail.csv",
                mime="text/csv",
                use_container_width=True,
            )
        else:
            st.info("Sin incidencias para el rango seleccionado.")

        st.markdown("---")
        st.subheader("🧪 Debug de shifts / breaks")
        st.dataframe(shifts_df, use_container_width=True)
        st.download_button(
            "⬇️ Descargar debug de shifts (CSV)",
            data=shifts_df.to_csv(index=False).encode("utf-8"),
            file_name="toast_meal_audit_shifts_debug.csv",
            mime="text/csv",
            use_container_width=True,
        )

    except requests.HTTPError as error:
        response_text = ""
        try:
            response_text = error.response.text
        except Exception:
            pass
        st.error(f"Error HTTP con Toast API: {error}\n\n{response_text}")
    except Exception as error:
        st.exception(error)

with st.expander("Ver ejemplo de .streamlit/secrets.toml"):
    st.code(
        """
TOAST_CLIENT_ID = "pega_aqui_tu_client_id"
TOAST_CLIENT_SECRET = "pega_aqui_tu_client_secret_rotado"
        """.strip(),
        language="toml",
    )

with st.expander("Notas de precisión"):
    st.markdown(
        """
- Esta versión audita cada `TimeEntry` individual como un shift. Eso corrige el problema de split shifts.
- Si dejas vacío el campo de `Meal Break Type GUIDs`, la app contará cualquier break real con duración mínima suficiente.
- Si quieres precisión máxima, pega los GUIDs de break types que correspondan a meal breaks.
- El panel **Debug de shifts / breaks** te permite revisar exactamente qué breaks vio la API en cada shift.
        """
    )
