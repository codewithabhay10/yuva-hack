"""UnitWatt prototype dashboard.

Run with:  streamlit run app.py
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

import re
from dataclasses import replace
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from unitwatt.analytics import idle_analysis
from unitwatt.audit import sha256_bytes
from unitwatt.emissions import default_value_comparison
from unitwatt.ingest import factor_table, parse_daily_entry
from unitwatt.ledger import monthly_summary
from unitwatt.messages import format_inr, format_lakh, owner_message
from unitwatt.opportunities import ImpactAssumptions, impact_model
from unitwatt.pipeline import DEMO_MEASURES, run_demo
from unitwatt.report import emissions_statement_html, savings_report_html
from unitwatt.report_pdf import savings_report_pdf
from unitwatt.scheduler import optimise, slot_label, slot_of
from unitwatt.schemas import BillDocument, ZoneReading, has_errors, validate_bill
from unitwatt.tariff import SLOT_HOURS, SLOT_MINUTES, SLOTS_PER_DAY

st.set_page_config(page_title="UnitWatt", page_icon="⚡", layout="wide")

# Reference categorical palette (fixed order) and reserved status colours.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
GOOD, WARNING, CRITICAL = "#0ca30c", "#fab219", "#d03b3b"
BLUES = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
BAND_FILL = {"peak": "rgba(235,104,52,0.10)", "solar": "rgba(237,161,0,0.10)", "off-peak": "rgba(42,120,214,0.08)"}

MEASURES = DEMO_MEASURES


def rs(value: float) -> str:
    return "₹" + format_inr(value)


def style(fig: go.Figure, height: int = 320, y: str | None = None, x: str | None = None, hover: str = "x unified") -> go.Figure:
    title = fig.layout.title.text
    fig.update_layout(
        height=height,
        margin=dict(l=8, r=8, t=76 if title else 36, b=8),
        legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="left", x=0, title=None),
        hovermode=hover,
        barcornerradius=4,
    )
    if title:  # title in the top margin, legend between it and the plot
        fig.update_layout(title=dict(text=title, x=0, xanchor="left", y=1, yref="container", yanchor="top", pad=dict(t=8), font=dict(size=14)))
    fig.update_xaxes(showgrid=False, title=x)
    fig.update_yaxes(gridcolor="rgba(128,128,128,0.18)", zeroline=False, title=y)
    return fig


def plot(fig: go.Figure) -> None:
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})


@st.cache_resource(show_spinner="Reading the factory's bills, meter data and registers...")
def load(seed: int):
    return run_demo(seed=seed)


# --------------------------------------------------------------------------------------
# Sidebar
# --------------------------------------------------------------------------------------

with st.sidebar:
    st.markdown("## ⚡ UnitWatt")
    st.caption("Energy and carbon ledger for MSME factories. No new hardware: bills, meter exports and registers in.")
    lang = st.segmented_control("Owner language", ["English", "हिंदी"], default="English", key="lang") or "English"
    with st.expander("Demo settings"):
        seed = int(st.number_input("Synthetic data seed", min_value=1, max_value=999, value=7, step=1))
        st.caption("Change the seed to generate a different six months for the same synthetic unit.")

R = load(seed)
F, T, L = R.factory, R.tariff, R.ledger
S = R.savings

with st.sidebar:
    st.divider()
    st.markdown(f"**{F.name}**")
    st.caption(f"{F.city}, {F.state} · {F.sector_template['label']} · {F.workers} workers")
    st.markdown(
        f"Tariff: **{T.name}** ({T.effective_from:%b %Y} order)  \n"
        f"Contract demand: **{F.contract_demand_kva:.0f} kVA**  \n"
        f"Data: **{L.index.min():%d %b} to {L.index.max():%d %b %Y}**"
    )
    st.metric("Data quality score", f"{R.quality.score:.1f} / 100", f"Grade {R.quality.grade}", delta_color="off", delta_arrow="off", border=True)
    st.warning(
        "Synthetic demo unit. Tariff values marked *illustrative* must be checked against the current tariff order.",
        icon="⚠️",
    )

tabs = st.tabs(
    ["Owner", "WhatsApp bot", "Bill check", "Energy per product", "Schedule", "Savings proof", "Carbon & CBAM", "Alerts", "Data & audit",
     "Impact model"]
)

# --------------------------------------------------------------------------------------
# 1. Owner view
# --------------------------------------------------------------------------------------

with tabs[0]:
    facts = R.owner_facts
    st.subheader(f"{facts.month:%B %Y} at a glance")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Money you could have kept this month", rs(facts.rupees_lost_month), border=True,
              help="Sum of the zero- and low-capex actions below, for this month.")
    c2.metric("Saved against your old pattern", rs(facts.saved_rs_month), f"{format_inr(facts.saved_kwh_month)} kWh avoided",
              border=True, delta_color="normal")
    c3.metric("Energy per tonne", f"{facts.sec_kwh_per_t:.0f} kWh/t", f"{facts.sec_change_pct:+.1f}% vs last month",
              delta_color="inverse", border=True)
    c4.metric(f"CO₂ avoided since {S.reporting[0]:%B}", f"{S.tco2_avoided:.1f} t", f"{S.savings_pct:.1%} energy saved", border=True,
              delta_color="off", delta_arrow="off")

    left, right = st.columns([3, 2], gap="large")
    with left:
        st.markdown("#### Top three actions")
        for opp in R.opportunities_latest[:3]:
            with st.container(border=True):
                title = opp.title_hi if lang == "हिंदी" else opp.title
                a, b = st.columns([3, 1])
                a.markdown(f"**{title}**")
                b.markdown(f"<div style='text-align:right'><b>{rs(opp.rupees_per_year / 12)}</b>/month</div>", unsafe_allow_html=True)
                payback = "no capital needed" if opp.capex == 0 else f"capex {format_lakh(opp.capex)}, payback {opp.payback_months:.1f} months"
                energy = "saves money, not energy" if not opp.saves_energy else f"{format_inr(opp.kwh_per_year)} kWh and {opp.tco2_per_year:.1f} tCO₂ a year"
                st.caption(f"{format_lakh(opp.rupees_per_year)} a year · {payback} · {energy} · confidence: {opp.confidence} · {opp.module}")
                with st.expander("Why, and what to do"):
                    st.write(opp.evidence)
                    for step in opp.actions:
                        st.markdown(f"- {step}")
        if len(R.opportunities_latest) > 3:
            with st.expander(f"{len(R.opportunities_latest) - 3} more"):
                for opp in R.opportunities_latest[3:]:
                    st.markdown(f"- **{opp.title}**: {format_lakh(opp.rupees_per_year)}/year ({opp.confidence})")
    with right:
        st.markdown("#### This month's WhatsApp message")
        text = owner_message(facts, "hi" if lang == "हिंदी" else "en")
        bubble = text.replace("&", "&amp;").replace("<", "&lt;").replace("\n", "<br>")
        st.markdown(
            "<div style='background:#e7f6dc;color:#111;border-radius:10px 10px 10px 2px;padding:12px 14px;"
            "font-size:14px;line-height:1.5;box-shadow:0 1px 1px rgba(0,0,0,.12)'>" + bubble + "</div>",
            unsafe_allow_html=True,
        )
        st.caption("Every number in the message comes from a computed field (P14); the template only supplies the words.")
        with st.expander("Copy the text"):
            st.code(text, language=None)

    st.markdown("#### What each bill was made of")
    rows = []
    for b in R.bill_documents:
        month = f"{b.period_start:%b %Y}"
        rows += [
            {"Month": month, "Component": "Energy", "Rs": b.energy_charges},
            {"Month": month, "Component": "Demand", "Rs": b.demand_charges},
            {"Month": month, "Component": "Penalties", "Rs": b.excess_demand_charges + max(b.pf_adjustment, 0)},
            {"Month": month, "Component": "Duty and other", "Rs": b.electricity_duty + b.fixed_charges + b.other_charges + min(b.pf_adjustment, 0)},
        ]
    fig = px.bar(pd.DataFrame(rows), x="Month", y="Rs", color="Component", color_discrete_sequence=SERIES)
    fig.update_traces(hovertemplate="%{x}<br>%{fullData.name}: ₹%{y:,.0f}<extra></extra>")
    plot(style(fig, 300, y="₹ per month", hover="closest"))

# --------------------------------------------------------------------------------------
# 2. WhatsApp bot (P2, P14, P18), simulated: the same engine that answers on WhatsApp and Telegram
# --------------------------------------------------------------------------------------

with tabs[1]:
    from unitwatt.bot import Bot, BotStore, Incoming

    st.subheader("The WhatsApp bot")
    st.caption("The same bot that answers on WhatsApp and Telegram, running here without any accounts. Chat as the owner, "
               "the supervisor or the accountant: each sees only what their role allows.")
    if "bot_store" not in st.session_state:
        st.session_state["bot_store"] = BotStore()
    chat_bot = Bot(R, st.session_state["bot_store"])
    people = {f"{c.name} · {c.role}": c for c in F.contacts}
    left, right = st.columns([3, 2], gap="large")
    with left:
        person = people[st.radio("Chat as", list(people), horizontal=True, key="bot_person")]
        history = st.session_state.setdefault("bot_history", {}).setdefault(person.phone, [])

        def send(msg: Incoming, shown: str) -> None:
            history.append(("in", shown, [], None))
            for reply in chat_bot.handle(msg):
                history.append(("out", reply.text, reply.buttons, reply.document))
            st.rerun()

        box = st.container(height=520, border=True)
        with box:
            if not history:
                st.caption("Say hi to start, or try: 1 · flange 1.5t, crank 1.2t, gear 1.4t, 2 shifts · a bill photo.")
            for i, (direction, text, buttons, document) in enumerate(history):
                with st.chat_message("user" if direction == "in" else "assistant", avatar="🧑‍🏭" if direction == "in" else "⚡"):
                    st.markdown(text.replace("\n", "  \n"))
                    if document:
                        st.download_button(f"📎 {document[0]}", document[1], file_name=document[0], mime=document[2], key=f"doc_{person.phone}_{i}")
        if history and history[-1][0] == "out" and history[-1][2]:
            for col, (bid, title) in zip(st.columns(len(history[-1][2])), history[-1][2]):
                if col.button(title, key=f"btn_{person.phone}_{len(history)}_{bid}", width="stretch"):
                    send(Incoming(sender=person.phone, text=bid, channel="dashboard"), title)
        typed = st.chat_input("Type a message, e.g. hi, 1, flange 1.5t, crank 1.2t, 2 shifts", key="bot_input")
        if typed:
            send(Incoming(sender=person.phone, text=typed, channel="dashboard"), typed)
        c = st.columns(2)
        upload = c[0].file_uploader("Send a bill photo or PDF", type=["png", "jpg", "jpeg", "webp", "pdf"],
                                    key=f"bot_file_{len(history)}")
        if upload is not None:
            kind = "document" if upload.type == "application/pdf" else "image"
            send(Incoming(sender=person.phone, kind=kind, media=upload.getvalue(), mime=upload.type, filename=upload.name,
                          channel="dashboard"), f"📎 {upload.name}")
        voice = c[1].audio_input("Or record a voice note", key=f"bot_voice_{len(history)}")
        if voice is not None:
            send(Incoming(sender=person.phone, kind="audio", media=voice.getvalue(), mime="audio/wav", channel="dashboard"), "🎙️ voice note")
    with right:
        store = st.session_state["bot_store"]
        st.markdown("**Saved through the bot**")
        prod = store.production_frame()
        if prod.empty:
            st.caption("Nothing yet. Production entries and bills confirmed in the chat appear here, and in the audit trail.")
        else:
            prod["product"] = prod["product"].map(lambda p: F.product(p).name)
            st.dataframe(prod[["day", "product", "tonnes", "shifts", "source"]], hide_index=True, width="stretch")
        bills = store.bills_frame()
        if not bills.empty:
            st.dataframe(bills[["period_start", "total", "audit_entry"]], hide_index=True, width="stretch",
                         column_config={"total": st.column_config.NumberColumn("Total ₹", format="%,.2f")})
        with st.expander("Connect it to real WhatsApp or Telegram"):
            st.markdown(
                "- **WhatsApp:** set `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_VERIFY_TOKEN` and "
                "`WHATSAPP_APP_SECRET`, run `uvicorn unitwatt.server:app`, and register `https://<your-host>/webhook/whatsapp` "
                "in the Meta app.\n"
                "- **Telegram:** create a bot with @BotFather, set `TELEGRAM_BOT_TOKEN`, run `python -m unitwatt.bot telegram`.\n"
                "- **Voice notes:** set `GROQ_API_KEY` (free Whisper) or run a local Whisper server with `UNITWATT_STT=local`.\n"
                "- **People and roles:** add real numbers as `UNITWATT_CONTACTS=phone:role:name,...`."
            )

# --------------------------------------------------------------------------------------
# 2. Bill check (P1, P10)
# --------------------------------------------------------------------------------------

with tabs[2]:
    st.subheader("The ten-minute bill check")
    st.caption("Needs nothing but the electricity bills. Finds money before the owner is asked for production data.")
    table = pd.DataFrame(
        [
            {
                "Month": f"{b.period_start:%b %Y}",
                "kWh": b.kwh_total,
                f"{T.costliest_band.title()} share": b.zone_units().get(T.costliest_band, 0) / b.billing_units,
                "Max demand kVA": b.max_demand_kva,
                "Billed demand kVA": b.billing_demand_kva,
                "PF": b.power_factor,
                "Penalties ₹": b.excess_demand_charges + max(b.pf_adjustment, 0),
                "Total ₹": b.total_amount,
                "₹ per kWh": b.total_amount / b.kwh_total,
            }
            for b in R.bill_documents
        ]
    )
    st.dataframe(
        table, hide_index=True, width="stretch",
        column_config={
            "kWh": st.column_config.NumberColumn(format="%,.0f"),
            f"{T.costliest_band.title()} share": st.column_config.NumberColumn(format="percent"),
            "Max demand kVA": st.column_config.NumberColumn(format="%.0f"),
            "Billed demand kVA": st.column_config.NumberColumn(format="%.0f"),
            "PF": st.column_config.NumberColumn(format="%.3f"),
            "Penalties ₹": st.column_config.NumberColumn(format="%,.0f"),
            "Total ₹": st.column_config.NumberColumn(format="%,.0f"),
            "₹ per kWh": st.column_config.NumberColumn(format="%.2f"),
        },
    )
    losses = [f for f in R.findings if f.is_loss]
    chances = [f for f in R.findings if not f.is_loss]
    a, b = st.columns(2, gap="large")
    with a:
        st.markdown(f"#### Money already lost: {rs(sum(f.rupees for f in losses))}")
        for f in losses:
            with st.container(border=True):
                st.markdown(f"🔴 **{f.month} · {f.title}: {rs(f.rupees)}**")
                st.caption(f.detail)
        if not losses:
            st.success("No penalties or billing errors on these bills.")
    with b:
        st.markdown("#### Opportunities and what-ifs")
        by_kind: dict[str, list] = {}
        for f in chances:
            by_kind.setdefault(f.kind, []).append(f)
        for kind, items in by_kind.items():
            with st.container(border=True):
                latest = items[-1]
                label = "Peak-hour premium" if kind == "tod_premium" else "kVAh billing exposure"
                st.markdown(f"🟡 **{label}: {rs(sum(i.rupees for i in items))} over {len(items)} bills**")
                st.caption(f"Latest ({latest.month}): {latest.detail}")

    st.markdown("#### Is the contract demand right?")
    cd = R.contract_demand
    best = cd.loc[cd["annual_demand_rs"].idxmin()]
    current = cd.loc[(cd["contract_demand_kva"] - F.contract_demand_kva).abs().idxmin()]
    fig = go.Figure(go.Scatter(x=cd["contract_demand_kva"], y=cd["annual_demand_rs"] / 1e5, mode="lines", line=dict(color=SERIES[0], width=2),
                               name="Annual demand charges", hovertemplate="%{x:.0f} kVA: ₹%{y:.2f} lakh/year<extra></extra>"))
    fig.add_vline(x=F.contract_demand_kva, line=dict(color="gray", dash="dot"), annotation_text=f"current {F.contract_demand_kva:.0f} kVA")
    fig.add_trace(go.Scatter(x=[best["contract_demand_kva"]], y=[best["annual_demand_rs"] / 1e5], mode="markers", marker=dict(size=10, color=SERIES[1]),
                             name="Lowest cost", hovertemplate="%{x:.0f} kVA: ₹%{y:.2f} lakh/year<extra></extra>"))
    plot(style(fig, 280, y="₹ lakh per year", x="Contract demand (kVA)", hover="closest"))
    st.caption(
        f"Lowest annual demand charges at {best['contract_demand_kva']:.0f} kVA: {rs(current['annual_demand_rs'] - best['annual_demand_rs'])} "
        f"a year less than today, based on {len(R.bills)} months of maximum demand. Confirm the DISCOM's rules for changing contract demand first."
    )

    st.markdown("#### Add a bill (P1)")
    st.caption("Every bill passes arithmetic checks and a confirmation screen before it reaches the ledger.")
    source = st.radio("Source", ["Edit a bill on record", "Upload bill JSON", "Upload bill photo or PDF"], horizontal=True)
    draft: BillDocument | None = None
    if source == "Edit a bill on record":
        pick = st.selectbox("Bill", [f"{b.period_start:%b %Y}" for b in R.bill_documents], index=len(R.bills) - 1)
        draft = next(b for b in R.bill_documents if f"{b.period_start:%b %Y}" == pick)
    elif source == "Upload bill JSON":
        up = st.file_uploader("Bill JSON in the UnitWatt schema", type=["json"])
        if up:
            try:
                draft = BillDocument.model_validate_json(up.getvalue())
            except Exception as exc:  # noqa: BLE001 - show any parse error to the user
                st.error(f"Could not read that file: {exc}")
    else:
        from unitwatt.extract import PROVIDERS, ExtractionError, available_readers, default_reader, extract_bill, reader_label

        readers = available_readers()
        preferred = default_reader()
        reader = st.selectbox("Read it with", readers, index=readers.index(preferred) if preferred in readers else 0,
                              format_func=reader_label)
        if reader == "offline":
            st.caption("Free and private: the PDF's own text, or OCR for scans and photos, read on this computer. "
                       "Works best on flat, well-lit photos; check the fields below either way.")
        elif PROVIDERS[reader].note:
            st.caption(PROVIDERS[reader].note)
        more = [p for name, p in PROVIDERS.items() if name not in readers and p.key_env]
        if more:
            st.caption("More free readers: set " + ", ".join(f"[{p.key_env[0]}]({p.signup})" for p in more)
                       + " (no card needed), or `UNITWATT_READER=ollama` for a local model.")
        up = st.file_uploader("Bill photo or PDF", type=["png", "jpg", "jpeg", "webp", "pdf"])
        if up:
            key = (sha256_bytes(up.getvalue()), reader)
            if st.session_state.get("extracted_key") != key:
                with st.spinner(f"Reading the bill ({reader_label(reader)})..."):
                    try:
                        st.session_state["extracted"] = extract_bill(up.getvalue(), up.type, reader)
                        st.session_state["extracted_key"] = key
                    except ExtractionError as exc:
                        st.session_state.pop("extracted", None)
                        st.error(str(exc))
            result = st.session_state.get("extracted")
            if result is not None:
                draft = result.bill
                st.caption(f"Read by {result.model}.")
                for issue in result.issues:
                    (st.error if issue.severity == "error" else st.warning)(f"{issue.field}: {issue.message}")

    if draft is not None:
        with st.form("confirm_bill"):
            st.markdown("**Check every field against the paper bill, then confirm.**")
            c = st.columns(4)
            start = c[0].date_input("Period start", draft.period_start)
            end = c[1].date_input("Period end", draft.period_end)
            basis = c[2].selectbox("Energy basis", ["kWh", "kVAh"], index=0 if draft.energy_basis == "kWh" else 1)
            consumer = c[3].text_input("Consumer no.", draft.consumer_number or "")
            c = st.columns(4)
            kwh = c[0].number_input("kWh", value=float(draft.kwh_total), step=1.0)
            kvah = c[1].number_input("kVAh", value=float(draft.kvah_total or 0.0), step=1.0)
            md = c[2].number_input("Max demand kVA", value=float(draft.max_demand_kva), step=1.0)
            cdk = c[3].number_input("Contract demand kVA", value=float(draft.contract_demand_kva), step=1.0)
            zones = st.data_editor(
                pd.DataFrame([z.model_dump() for z in draft.zones] or [{"zone": "normal", "units": 0.0, "charges": None}]),
                num_rows="dynamic", hide_index=True, width="stretch",
            )
            c = st.columns(4)
            energy = c[0].number_input("Energy charges ₹", value=float(draft.energy_charges), step=1.0)
            demand = c[1].number_input("Demand charges ₹", value=float(draft.demand_charges), step=1.0)
            excess = c[2].number_input("Excess demand ₹", value=float(draft.excess_demand_charges), step=1.0)
            pfadj = c[3].number_input("PF adjustment ₹ (+ penalty)", value=float(draft.pf_adjustment), step=1.0)
            c = st.columns(4)
            duty = c[0].number_input("Electricity duty ₹", value=float(draft.electricity_duty), step=1.0)
            fixed = c[1].number_input("Fixed charges ₹", value=float(draft.fixed_charges), step=1.0)
            other = c[2].number_input("Other ₹", value=float(draft.other_charges), step=1.0)
            total = c[3].number_input("Bill total ₹", value=float(draft.total_amount), step=1.0)
            submitted = st.form_submit_button("Run checks", type="primary")
        if submitted:
            edited = draft.model_copy(update=dict(
                period_start=start, period_end=end, energy_basis=basis, consumer_number=consumer or None, kwh_total=kwh,
                kvah_total=kvah or None, max_demand_kva=md, contract_demand_kva=cdk,
                zones=[ZoneReading(zone=str(r["zone"]), units=float(r["units"]), charges=None if pd.isna(r.get("charges")) else float(r["charges"]))
                       for r in zones.to_dict("records") if str(r.get("zone", "")).strip()],
                energy_charges=energy, demand_charges=demand, excess_demand_charges=excess, pf_adjustment=pfadj,
                electricity_duty=duty, fixed_charges=fixed, other_charges=other, total_amount=total,
                power_factor=round(kwh / kvah, 3) if kvah else None,
            ))
            issues = validate_bill(edited)
            st.session_state["checked_bill"] = edited
            for issue in issues:
                (st.error if issue.severity == "error" else st.warning)(f"{issue.field}: {issue.message}")
            if not issues:
                st.success("All arithmetic checks pass.")
            expected = T.reprice(edited)
            diff = edited.total_amount - expected.total_amount
            if abs(diff) > 100:
                st.warning(f"Under the {T.name} config this bill should be {rs(expected.total_amount)}; it says {rs(edited.total_amount)} ({rs(diff)} difference).")
            else:
                st.info(f"The total matches the tariff config ({rs(expected.total_amount)}).")
            if not has_errors(issues):
                st.session_state["bill_ready"] = True
        if st.session_state.get("bill_ready") and st.session_state.get("checked_bill") is not None:
            if st.button("Confirm and save to the audit trail"):
                bill = st.session_state.pop("checked_bill")
                st.session_state["bill_ready"] = False
                body = bill.model_dump_json(indent=2).encode()
                entry = R.audit.add_document("electricity_bill", f"confirmed_bill_{bill.period_start:%Y_%m}.json", body,
                                             {"confirmed_by": F.owner_name, "source": source})
                st.success(f"Saved as audit entry #{entry.id}, SHA-256 {entry.sha256[:16]}...")

# --------------------------------------------------------------------------------------
# 3. Energy per product (P8, P9)
# --------------------------------------------------------------------------------------

with tabs[3]:
    M = R.baseline_model
    st.subheader("Energy per product, without sub-meters")
    st.caption(
        f"Daily electricity regressed on daily output of each product line, coefficients held non-negative "
        f"({M.method}, {M.period[0]:%d %b} to {M.period[1]:%d %b}, {M.n} days)."
    )
    c = st.columns(4)
    c[0].metric("Model fit R²", f"{M.r2:.3f}", border=True)
    c[1].metric("CV(RMSE)", f"{M.cv_rmse:.1%}", border=True)
    c[2].metric("Always-on base load", f"{M.coef['base'] / 24:.1f} kW", f"{M.coef['base']:.0f} kWh/day", delta_color="off", delta_arrow="off", border=True)
    c[3].metric("Production-day fixed load", f"{M.coef['production_day']:.0f} kWh/day", border=True)
    for w in M.warnings:
        st.warning(w)

    left, right = st.columns(2, gap="large")
    with left:
        sec = pd.DataFrame({"Product": [F.product(p).name for p in M.products], "SEC": [M.coef[p] for p in M.products],
                            "lo": [M.ci90[p][0] for p in M.products], "hi": [M.ci90[p][1] for p in M.products]})
        fig = go.Figure(go.Bar(
            x=sec["Product"], y=sec["SEC"], marker_color=SERIES[0], width=0.5,
            error_y=dict(type="data", symmetric=False, array=sec["hi"] - sec["SEC"], arrayminus=sec["SEC"] - sec["lo"], color="gray", thickness=1.5),
            customdata=np.stack([sec["lo"], sec["hi"]], axis=1),
            hovertemplate="%{x}: %{y:.0f} kWh/t<br>90%% interval %{customdata[0]:.0f} to %{customdata[1]:.0f}<extra></extra>",
        ))
        fig.update_layout(title="Marginal electricity per tonne (90% interval)")
        plot(style(fig, 320, y="kWh per tonne", hover="closest"))
    with right:
        fit = M.fitted.copy()
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=fit.index, y=fit["actual"], name="Metered", mode="lines", line=dict(color=SERIES[0], width=2)))
        fig.add_trace(go.Scatter(x=fit.index, y=fit["fitted"], name="Model", mode="lines", line=dict(color=SERIES[1], width=2, dash="dot")))
        fig.update_layout(title="Daily electricity: meter vs model")
        fig.update_traces(hovertemplate="%{y:,.0f} kWh")
        plot(style(fig, 320, y="kWh per day"))

    st.markdown("#### Where the load sits, every 15 minutes")
    data = R.survey.data
    grid = (data["kwh"] / SLOT_HOURS).to_numpy().reshape(-1, SLOTS_PER_DAY)
    days = pd.date_range(data.index.min().normalize(), periods=grid.shape[0], freq="D")
    fig = go.Figure(go.Heatmap(
        z=grid.T, x=days, y=[slot_label(s) for s in range(SLOTS_PER_DAY)], colorscale=[[i / 6, c] for i, c in enumerate(BLUES)],
        colorbar=dict(title="kW", thickness=10), hovertemplate="%{x|%a %d %b} %{y}: %{z:.0f} kW<extra></extra>", hoverongaps=False,
    ))
    fig.update_yaxes(autorange="reversed", nticks=8)
    plot(style(fig, 360, hover="closest"))
    st.caption("Blank cells are gaps in the meter export; the ledger fills those days from the bill and labels them estimated. "
               "Sundays and nights show the always-on load; the shift of heavy load into midday from July is visible.")

    st.markdown("#### Idle waste (P9)")
    I0, I1 = R.idle_baseline, R.idle_latest
    c = st.columns(4)
    latest_label = f"{R.windows.latest_month[0]:%b}"
    c[0].metric("Median load with no production", f"{I0.base_kw:.1f} kW", f"{I1.base_kw - I0.base_kw:+.1f} kW by {latest_label}",
                delta_color="inverse", border=True)
    c[1].metric("Essential night load", f"{I0.essential_kw:.1f} kW", border=True, help="Security lights, server, guard room (from onboarding)")
    c[2].metric("Energy used when nothing is made", f"{I0.idle_pct:.1%}", f"{(I1.idle_pct - I0.idle_pct) * 100:+.1f} pts by {latest_label}",
                delta_color="inverse", border=True)
    c[3].metric("Avoidable, first 3 months", rs(I0.avoidable_rs), border=True)
    left, right = st.columns(2, gap="large")
    with left:
        prof = I0.profile
        fig = go.Figure()
        for i, col in enumerate(prof.columns):
            fig.add_trace(go.Scatter(x=prof.index, y=prof[col], name=col, mode="lines", line=dict(color=SERIES[i], width=2, shape="hv")))
        fig.add_hline(y=I0.essential_kw, line=dict(color="gray", dash="dot"), annotation_text="essential load")
        fig.update_layout(title="Average day, April to June")
        fig.update_traces(hovertemplate="%{y:.0f} kW")
        fig.update_xaxes(nticks=12)
        plot(style(fig, 300, y="kW"))
    with right:
        full = idle_analysis(R.survey.data, R.ledger, F, T).nights
        fig = go.Figure(go.Scatter(x=full.index, y=full["median_idle_kw"], mode="lines", line=dict(color=SERIES[0], width=2), name="Median idle kW",
                                   hovertemplate="%{x|%d %b}: %{y:.1f} kW<extra></extra>"))
        fig.add_hline(y=I0.essential_kw, line=dict(color="gray", dash="dot"), annotation_text="essential load")
        fig.update_layout(title="Median load in non-production hours, night by night")
        plot(style(fig, 300, y="kW", hover="closest"))

# --------------------------------------------------------------------------------------
# 4. Schedule (P12)
# --------------------------------------------------------------------------------------

with tabs[4]:
    st.subheader("A tariff-aware schedule for a typical day")
    st.caption("CP-SAT over 96 fifteen-minute slots: energy cost + demand charges + excess-demand surcharge + furnace reheat losses, "
               "within the owner's shift limits, one batch per machine, and heat-before-forge within an hour.")
    P = R.problem
    times = [slot_label(s) for s in range(SLOTS_PER_DAY + 1)]
    times[-1] = "24:00"
    c = st.columns([2, 1, 1])
    lo, hi = c[0].select_slider("Owner's limits on shift times", options=times, value=(times[P.earliest], times[P.latest_end]))
    cap = c[1].number_input("Hard cap on peak demand (kVA, 0 = none)", min_value=0, value=0, step=5)
    c[2].write("")
    run = c[2].button("Re-optimise", type="primary")
    if run:
        custom = replace(P, earliest=slot_of(lo), latest_end=slot_of(hi))
        try:
            st.session_state["custom_schedule"] = (lo, hi, cap, optimise(custom, 10.0, cap or None))
        except RuntimeError as exc:
            st.error(str(exc))
    schedules = dict(R.schedules)
    if "custom_schedule" in st.session_state:
        schedules["custom"] = st.session_state["custom_schedule"][3]

    labels = {"current": "As run today", "naive": "Everything moved to cheap hours", "optimised": "UnitWatt optimised", "custom": "Your limits"}
    base_cost = schedules["current"].total_rs
    comp = pd.DataFrame([
        {"Schedule": labels[k], "Energy ₹/day": v.energy_rs, "Demand ₹/day": v.demand_rs, "Reheat ₹/day": v.reheat_rs, "Total ₹/day": v.total_rs,
         "Saving ₹/month": (base_cost - v.total_rs) * P.working_days, "Peak kVA": v.peak_kva,
         f"{T.costliest_band.title()} kWh": v.kwh_by_band.get(T.costliest_band, 0.0)}
        for k, v in schedules.items()
    ])
    st.dataframe(comp, hide_index=True, width="stretch", column_config={
        c_: st.column_config.NumberColumn(format="%,.0f") for c_ in comp.columns if c_ != "Schedule"
    })
    opt, naive, cur = schedules["optimised"], schedules["naive"], schedules["current"]
    st.info(
        f"Moving every batch into solar hours without checking demand pushes the peak to {naive.peak_kva:.0f} kVA "
        f"(contract demand {F.contract_demand_kva:.0f} kVA). The optimised plan keeps it at {opt.peak_kva:.0f} kVA and saves "
        f"{rs((cur.total_rs - opt.total_rs) * P.working_days)} a month. It saves money, not energy."
    )

    x = [datetime(2026, 1, 1) + timedelta(minutes=SLOT_MINUTES * s) for s in range(SLOTS_PER_DAY)]
    fig = go.Figure()
    start_band, current = 0, P.bands[0]
    for s in range(1, SLOTS_PER_DAY + 1):
        if s == SLOTS_PER_DAY or P.bands[s] != current:
            if current in BAND_FILL:
                fig.add_vrect(x0=x[start_band], x1=x[s - 1] + timedelta(minutes=SLOT_MINUTES), fillcolor=BAND_FILL[current], line_width=0,
                              annotation_text=f"{current} ×{T.multiplier(current):.2f}", annotation_position="top left")
            if s < SLOTS_PER_DAY:
                start_band, current = s, P.bands[s]
    for i, key in enumerate([k for k in ["current", "naive", "optimised", "custom"] if k in schedules]):
        fig.add_trace(go.Scatter(x=x, y=schedules[key].load_kw, name=labels[key], mode="lines",
                                 line=dict(color=SERIES[i], width=2, shape="hv"), hovertemplate="%{y:.0f} kW"))
    fig.add_hline(y=F.contract_demand_kva * P.pf, line=dict(color=CRITICAL, dash="dash", width=1.5),
                  annotation_text=f"contract demand ({F.contract_demand_kva:.0f} kVA)", annotation_position="bottom right")
    fig.update_xaxes(tickformat="%H:%M")
    plot(style(fig, 360, y="kW"))

    def gantt(result, title):
        rows = []
        for job in P.jobs:
            s = result.starts[job.id]
            rows.append({"Machine": job.machine.replace("_", " "), "Batch": job.name, "Label": re.sub(r"([a-z]+)(\d+)", lambda m: f"{m.group(1).title()} {m.group(2)}", job.id),
                         "Start": x[0] + timedelta(minutes=SLOT_MINUTES * s),
                         "End": x[0] + timedelta(minutes=SLOT_MINUTES * (s + job.slots)), "kW": job.kw})
        df = pd.DataFrame(rows)
        fig = px.timeline(df, x_start="Start", x_end="End", y="Machine", color="Machine", text="Label", color_discrete_sequence=SERIES,
                          hover_data={"Batch": True, "kW": True, "Machine": False, "Label": False})
        fig.update_traces(textposition="inside", insidetextanchor="middle", textangle=0, textfont=dict(size=10, color="white"))
        fig.update_layout(title=title, showlegend=False)
        fig.update_xaxes(tickformat="%H:%M", range=[x[0] + timedelta(hours=6), x[0] + timedelta(hours=24)])
        return style(fig, 230, hover="closest")

    a, b = st.columns(2, gap="large")
    with a:
        plot(gantt(cur, "As run today"))
    with b:
        plot(gantt(schedules.get("custom", opt), "Optimised" if "custom" not in schedules else "Optimised with your limits"))

# --------------------------------------------------------------------------------------
# 5. Savings proof (P15)
# --------------------------------------------------------------------------------------

with tabs[5]:
    st.subheader("Audit-ready proof of savings")
    st.caption(f"IPMVP Option C (whole facility). Baseline {S.baseline[0]:%d %b} to {S.baseline[1]:%d %b}; "
               f"reporting {S.reporting[0]:%d %b} to {S.reporting[1]:%d %b %Y}. The certified energy auditor still signs the M&V.")
    c = st.columns(5)
    c[0].metric("Energy avoided (kWh)", format_inr(S.avoided_kwh), border=True)
    c[1].metric("Savings", f"{S.savings_pct:.1%}", "of adjusted baseline", delta_color="off", delta_arrow="off", border=True)
    c[2].metric("Uncertainty, 90% (kWh)", f"±{format_inr(S.uncertainty_kwh)}", f"±{S.fsu90:.0%} of savings", delta_color="off", delta_arrow="off", border=True)
    c[3].metric("Value", rs(S.rs_saved), border=True)
    c[4].metric("CO₂ avoided", f"{S.tco2_avoided:.1f} t", border=True)
    if S.meets_adeetie:
        st.success(f"Meets ADEETIE's 10% demonstrated-savings requirement ({S.savings_pct:.1%}).", icon="✅")
    else:
        st.warning(
            f"**Not yet at ADEETIE's 10%: {S.savings_pct:.1%} demonstrated.** Another {format_inr(S.gap_to_target_kwh)} kWh over the same "
            "three months would reach it. Operational fixes alone rarely get a forging unit to 10%; the gap is the case for an "
            "investment-grade audit and equipment upgrade, which ADEETIE finances with interest subvention. September savings also "
            "fell because of the induction-heater drift (see Alerts).", icon="⚠️",
        )
    daily = S.daily
    fit = S.model.fitted
    fig = go.Figure()
    fig.add_vrect(x0=S.reporting[0], x1=S.reporting[1], fillcolor="rgba(42,120,214,0.06)", line_width=0, annotation_text="reporting period",
                  annotation_position="top left")
    fig.add_trace(go.Scatter(x=list(fit.index) + list(daily.index), y=list(fit["actual"]) + list(daily["actual"]), name="Actual",
                             mode="lines", line=dict(color=SERIES[0], width=2)))
    fig.add_trace(go.Scatter(x=list(fit.index) + list(daily.index), y=list(fit["fitted"]) + list(daily["adjusted_baseline"]),
                             name="Baseline model, adjusted to actual production", mode="lines", line=dict(color=SERIES[1], width=2, dash="dot")))
    fig.update_traces(hovertemplate="%{y:,.0f} kWh")
    plot(style(fig, 330, y="kWh per day"))
    a, b = st.columns(2, gap="large")
    with a:
        fig = go.Figure(go.Scatter(x=daily.index, y=daily["cumulative_avoided"], mode="lines", line=dict(color=SERIES[0], width=2),
                                   fill="tozeroy", fillcolor="rgba(42,120,214,0.12)", name="Cumulative kWh avoided",
                                   hovertemplate="%{x|%d %b}: %{y:,.0f} kWh<extra></extra>"))
        fig.update_layout(title="Cumulative energy avoided")
        plot(style(fig, 300, y="kWh", hover="closest"))
    with b:
        st.markdown("**Statistical checks**")
        st.dataframe(pd.DataFrame([{"Check": c_.name, "Result": c_.value, "Requirement": c_.threshold, "Status": "✅ pass" if c_.passed else "❌ not met"}
                                   for c_ in S.checks]), hide_index=True, width="stretch")
    st.dataframe(S.monthly.rename(columns={"actual": "Actual kWh", "adjusted_baseline": "Adjusted baseline kWh", "avoided": "Avoided kWh",
                                           "savings_pct": "Savings"}), width="stretch",
                 column_config={"Savings": st.column_config.NumberColumn(format="percent"),
                                **{k: st.column_config.NumberColumn(format="%,.0f") for k in ["Actual kWh", "Adjusted baseline kWh", "Avoided kWh"]}})

    st.markdown("#### Share the savings report")
    measures = st.text_area("Measures implemented", "\n".join(MEASURES)).splitlines()
    c = st.columns([2, 2, 1])
    recipient = c[0].text_input("Recipient", "Bank branch / BEE scheme officer / energy auditor")
    purpose = c[1].text_input("Purpose", "ADEETIE application and loan appraisal")
    consent = st.checkbox(f"I, {F.owner_name}, consent to sharing this report and its production data with the recipient above.")
    if consent and st.button("Record consent"):
        e = R.audit.record_consent("savings report", recipient, F.owner_name, purpose)
        st.success(f"Consent recorded as audit entry #{e.id}.")
    c = st.columns(2)
    c[0].download_button("Download savings report (PDF)", savings_report_pdf(R, [m for m in measures if m.strip()]),
                         file_name="unitwatt_savings_report.pdf", mime="application/pdf", disabled=not consent, type="primary")
    c[1].download_button("Download as HTML", savings_report_html(R, [m for m in measures if m.strip()]),
                         file_name="unitwatt_savings_report.html", mime="text/html", disabled=not consent)

# --------------------------------------------------------------------------------------
# 6. Carbon & CBAM (P16)
# --------------------------------------------------------------------------------------

with tabs[6]:
    E = R.emissions
    st.subheader("Product-level embedded emissions")
    st.caption(f"{E.period[0]:%d %b} to {E.period[1]:%d %b %Y}. Electricity allocated with the energy model; totals reconcile to the meter.")
    c = st.columns(4)
    c[0].metric("Output", f"{E.plant['tonnes']:.0f} t", border=True)
    c[1].metric("Scope 1 (diesel, furnace oil)", f"{E.plant['scope1_t']:.1f} tCO₂", border=True)
    c[2].metric("Scope 2 (grid)", f"{E.plant['scope2_t']:.1f} tCO₂", border=True)
    c[3].metric("Precursors (billets)", f"{E.plant['precursor_t']:.0f} tCO₂", border=True, help=F.precursor.get("source", ""))
    df = E.products
    long = pd.DataFrame([
        {"Product": r["name"], "Source": src, "tCO2/t": r[col]}
        for _, r in df.iterrows()
        for src, col in [("Direct (Scope 1)", "direct_tco2_per_t"), ("Indirect (Scope 2)", "indirect_tco2_per_t"), ("Precursor", "precursor_tco2_per_t")]
    ])
    a, b = st.columns(2, gap="large")
    with a:
        fig = px.bar(long, y="Product", x="tCO2/t", color="Source", orientation="h", color_discrete_sequence=SERIES)
        fig.update_traces(hovertemplate="%{y}<br>%{fullData.name}: %{x:.3f} tCO₂/t<extra></extra>")
        fig.update_layout(title="Embedded emissions per tonne of product")
        plot(style(fig, 300, x="tCO₂ per tonne", hover="closest"))
    with b:
        show = df[["name", "hsn", "cbam_covered", "tonnes", "kwh_per_t", "direct_tco2_per_t", "indirect_tco2_per_t", "embedded_tco2_per_t"]]
        st.dataframe(show.rename(columns={"name": "Product", "hsn": "HSN", "cbam_covered": "CBAM", "tonnes": "t", "kwh_per_t": "kWh/t",
                                          "direct_tco2_per_t": "Direct", "indirect_tco2_per_t": "Indirect", "embedded_tco2_per_t": "Total"}),
                     hide_index=True, width="stretch",
                     column_config={k: st.column_config.NumberColumn(format="%.3f") for k in ["Direct", "Indirect", "Total"]}
                     | {"t": st.column_config.NumberColumn(format="%.1f"), "kWh/t": st.column_config.NumberColumn(format="%.0f")})
        st.caption("HSN codes come from GST sales invoices and map products to CBAM categories. Crankshaft blanks (8483) are outside CBAM's iron and steel scope.")
    st.info("For iron and steel, CBAM prices only direct emissions and precursors, so the electricity savings do not cut this exporter's CBAM bill; "
            "furnace-oil, diesel and billet-supplier data do. Electricity emissions are still reported for buyers' Scope 3 requests.", icon="ℹ️")

    st.markdown("#### Default values or verified actuals?")
    covered = df[df["cbam_covered"]]
    c = st.columns(3)
    product = c[0].selectbox("CBAM product", covered["name"].tolist())
    default = c[1].number_input("EU default value for its CN code (tCO₂/t)", value=3.20, step=0.05,
                                help="Illustrative placeholder: look up the current EU default value for the CN code.")
    year = c[2].selectbox("Reporting year", [2026, 2027, 2028])
    row = covered[covered["name"] == product].iloc[0]
    per_year = 365 / ((E.period[1] - E.period[0]).days + 1)
    cmp_ = default_value_comparison(row["cbam_priced_tco2_per_t"], default, year, F.sector_template["cbam"]["default_value_markup_pct"])
    st.markdown(
        f"Verified actuals: **{cmp_['actual']:.3f} tCO₂/t** (direct + precursor). Default value with the {cmp_['markup_pct']:.0f}% "
        f"{year} markup: **{cmp_['default_with_markup']:.3f} tCO₂/t**. Declaring defaults would add **{cmp_['excess_tco2_per_t']:.3f} tCO₂ per tonne** "
        f"exported, about {cmp_['excess_tco2_per_t'] * row['tonnes'] * per_year:.0f} tCO₂ a year at this output. "
        "UnitWatt produces verification-ready data; an accredited verifier still verifies actual values."
    )
    consent_e = st.checkbox(f"I, {F.owner_name}, consent to sharing the emissions statement with my EU importer.", key="consent_e")
    if consent_e and st.button("Record consent", key="rec_e"):
        R.audit.record_consent("emissions statement", "EU importer", F.owner_name, "CBAM declaration and Scope 3 reporting")
        st.success("Consent recorded.")
    st.download_button("Download emissions statement (HTML)", emissions_statement_html(R), file_name="unitwatt_emissions_statement.html",
                       mime="text/html", disabled=not consent_e)

# --------------------------------------------------------------------------------------
# 7. Alerts (P11)
# --------------------------------------------------------------------------------------

with tabs[7]:
    D = R.drift
    st.subheader("Slow efficiency loss")
    st.caption(f"Each production day is compared with what the plant's own data from {D.reference[0]:%d %b} to {D.reference[1]:%d %b} "
               f"predicts for that day's product mix. A CUSUM on the gap raises an alarm when it crosses h = {D.h:.0f}.")
    if D.alarm_date:
        st.error(
            f"**Alarm since {D.alarm_date:%d %b %Y}.** Over the last two weeks the plant used {D.recent_excess_pct:.1%} more electricity than "
            f"expected for the same output: {format_inr(D.excess_kwh_since_alarm)} kWh extra since the alarm. Likely causes in a forging unit: "
            "induction-coil or refractory-lining wear, cooling-water scaling, billets charged cold. Inspect before the next shutdown.", icon="🚨",
        )
    else:
        st.success("No drift: energy per tonne is in line with the reference period.", icon="✅")
    dd = D.daily
    a, b = st.columns(2, gap="large")
    with a:
        fig = go.Figure(go.Scatter(x=dd.index, y=dd["cusum_hi"], mode="lines+markers", line=dict(color=SERIES[0], width=2),
                                   marker=dict(size=6), name="CUSUM", hovertemplate="%{x|%d %b}: %{y:.1f}<extra></extra>"))
        fig.add_hline(y=D.h, line=dict(color=CRITICAL, dash="dash", width=1.5), annotation_text="alarm threshold")
        fig.update_layout(title="CUSUM of standardised daily gaps")
        plot(style(fig, 300, hover="closest"))
    with b:
        fig = go.Figure()
        fig.add_trace(go.Bar(x=dd.index, y=dd["pct"] * 100, name="Daily gap", marker_color=SERIES[0], opacity=0.55,
                             hovertemplate="%{x|%d %b}: %{y:+.1f}%<extra></extra>"))
        fig.add_trace(go.Scatter(x=dd.index, y=dd["ewma_pct"] * 100, name="EWMA", mode="lines", line=dict(color=SERIES[1], width=2),
                                 hovertemplate="%{x|%d %b}: %{y:+.1f}%<extra></extra>"))
        fig.update_layout(title="Actual vs expected electricity, % gap")
        plot(style(fig, 300, y="% above expected", hover="closest"))

# --------------------------------------------------------------------------------------
# 8. Data & audit (P2 to P7, P17, P18)
# --------------------------------------------------------------------------------------

with tabs[8]:
    st.subheader("The daily ledger and where every number came from")
    st.markdown(f"**Data quality {R.quality.score:.1f}/100 (grade {R.quality.grade})**: shown on every report so nobody over-trusts thin data.")
    st.dataframe(R.quality.as_frame(), hide_index=True, width="stretch")

    a, b = st.columns(2, gap="large")
    with a:
        q = R.survey.quality
        st.markdown("**Meter load survey (P3)**")
        st.write(f"Format detected: `{q.source_format}` · {q.n_present:,} of {q.n_expected:,} readings · "
                 f"{q.n_duplicates} duplicate rows dropped · {q.n_negative} negative values")
        st.dataframe(pd.DataFrame([{"Gap from": g[0], "to": g[1], "Readings missing": g[2]} for g in q.gaps]), hide_index=True, width="stretch")
    with b:
        st.markdown("**Fuel: stock-flow accounting (P4)**")
        st.caption("Consumption = opening stock + purchases − closing stock; diesel reconciled against the generator log.")
        st.dataframe(R.fuel_periods.drop(columns=["period_start"]).rename(columns={"period_end": "Month end"}), hide_index=True, width="stretch")

    st.markdown("**Monthly summary**")
    st.dataframe(monthly_summary(L).round(1), width="stretch")
    st.markdown("**Daily ledger (P7)**")
    st.caption("One row per day. `estimated` marks days filled from the bill; furnace oil is spread across days by heat-treated tonnes.")
    show_cols = ["grid_kwh", "estimated", "dg_kwh", "elec_kwh"] + [f"kwh_{b_}" for b_ in T.band_names] + [f"t_{p}" for p in F.product_ids] + \
                ["total_t", "sec_kwh_per_t", "furnace_oil_units", "diesel_l", "max_kva", "meter_coverage", "production_recorded", "outlier"]
    st.dataframe(L[show_cols].round(2), width="stretch", height=280)
    st.download_button("Download ledger CSV", L.to_csv().encode(), "unitwatt_daily_ledger.csv", "text/csv")

    st.markdown("**Conversion and emission factors (P5)**")
    st.dataframe(factor_table(R.factors), hide_index=True, width="stretch")

    st.markdown("**Tamper-evident audit trail (P17)**")
    ok, bad = R.audit.verify()
    (st.success if ok else st.error)("Hash chain verifies: no entry has been altered or removed." if ok else f"Chain broken at entry {bad}.")
    trail = R.audit.frame()
    st.dataframe(trail[["id", "recorded_at", "kind", "name", "sha256", "entry_hash"]], hide_index=True, width="stretch", height=260)
    check = st.file_uploader("Check a file against the trail", key="verify_file")
    if check:
        digest = sha256_bytes(check.getvalue())
        match = trail[trail["sha256"] == digest]
        if len(match):
            st.success(f"Matches audit entry #{int(match.iloc[0]['id'])} ({match.iloc[0]['name']}): unchanged since upload.")
        else:
            st.error("No document with this SHA-256 is on record: the file differs from anything uploaded.")

    st.markdown("**30-second daily entry (P2, P18)**")
    st.caption("What the supervisor sends on WhatsApp or by voice. Hindi, English and Devanagari digits all work.")
    entry = st.text_input("Today's entry", "फ्लेंज १.५ टन, crank 1.2t, gear 1400 kg, 2 shifts")
    parsed = parse_daily_entry(entry, F)
    if parsed.tonnes:
        st.dataframe(pd.DataFrame([{"Product": F.product(p).name, "Tonnes": t} for p, t in parsed.tonnes.items()]), hide_index=True)
    if parsed.shifts:
        st.caption(f"Shifts: {parsed.shifts}")
    for w in parsed.warnings:
        st.warning(w)

# --------------------------------------------------------------------------------------
# 9. Impact model (from the deep dive)
# --------------------------------------------------------------------------------------

with tabs[9]:
    st.subheader("Impact model")
    st.caption("The deep dive's illustrative mid-sized forging unit. Every input is an assumption to replace with real bills: "
               "judges score the clarity of the baseline as much as the size of the number.")
    c = st.columns(4)
    a_ = ImpactAssumptions(
        grid_kwh_per_month=c[0].number_input("Grid kWh per month", value=60_000, step=1_000),
        base_rate=c[1].number_input("Base energy rate ₹/kWh", value=8.0, step=0.25),
        peak_multiplier=c[2].number_input("Peak multiplier", value=1.20, step=0.05),
        solar_multiplier=c[3].number_input("Solar-hours multiplier", value=0.80, step=0.05),
    )
    c = st.columns(4)
    a_.shifted_kwh_per_month = c[0].number_input("kWh shifted peak → solar per month", value=10_000, step=500)
    a_.idle_share = c[1].number_input("Idle and night base load share", value=0.08, step=0.01, format="%.2f")
    a_.idle_cut = c[2].number_input("Share of idle load eliminated", value=0.50, step=0.05, format="%.2f")
    a_.grid_tco2_per_mwh = c[3].number_input("Grid factor tCO₂/MWh", value=0.675, step=0.005, format="%.3f")
    res = impact_model(a_)
    st.dataframe(pd.DataFrame([
        {"Result": "ToD shifting savings", "Monthly": rs(res["tod_rs_month"]), "Annual": format_lakh(res["tod_rs_year"])},
        {"Result": "Idle-load savings", "Monthly": f"{rs(res['idle_rs_month'])} ({format_inr(res['idle_kwh_month'])} kWh)",
         "Annual": f"{format_lakh(res['idle_rs_year'])} ({res['idle_mwh_year']:.1f} MWh)"},
        {"Result": "Total savings", "Monthly": rs(res["total_rs_month"]), "Annual": format_lakh(res["total_rs_year"])},
        {"Result": "CO₂ avoided", "Monthly": f"{res['tco2_month']:.1f} tCO₂", "Annual": f"{res['tco2_year']:.1f} tCO₂"},
    ]), hide_index=True, width="stretch")
    st.info("ToD shifting saves money, not energy. Only the idle-load cut reduces kWh and carbon: the CEA publishes one annual grid factor, "
            "so solar-hour electricity cannot be claimed as cleaner without hourly factors.", icon="ℹ️")
    st.markdown("#### Scaling, as a scenario rather than a forecast")
    c = st.columns(2)
    units = c[0].slider("Units with similar profiles", 100, 5000, 1000, step=100)
    realisation = c[1].slider("Share of the assumed load that can actually shift", 0.0, 1.0, 1.0, step=0.1)
    full_ = impact_model(a_, units, 1.0)
    real_ = impact_model(a_, units, realisation)
    half_ = impact_model(a_, units, 0.5)
    c = st.columns(3)
    c[0].metric(f"{units:,} units, full shift", format_lakh(full_["fleet_rs_year"]), f"{full_['fleet_tco2_year']:,.0f} tCO₂/yr", delta_color="off", delta_arrow="off", border=True)
    c[1].metric(f"{units:,} units, {realisation:.0%} of the shift", format_lakh(real_["fleet_rs_year"]), f"{real_['fleet_tco2_year']:,.0f} tCO₂/yr", delta_color="off", delta_arrow="off", border=True)
    c[2].metric("If only half the load can shift", format_lakh(half_["fleet_rs_year"]), "CO₂ unchanged: shifting saves no energy", delta_color="off", delta_arrow="off", border=True)
