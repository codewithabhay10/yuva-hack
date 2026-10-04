"""The savings report as a PDF (P15, P17), for channels that cannot carry HTML.

WhatsApp accepts PDFs but not HTML documents, and a bank officer opens a PDF on a phone without
thinking about it. Same content as ``report.savings_report_html``: key figures, the ADEETIE test,
method, statistical checks, monthly results, source-document hashes and consent records.
Built with reportlab, so it needs no browser or system libraries.
"""

from __future__ import annotations

import io
from datetime import date
from xml.sax.saxutils import escape

from unitwatt.messages import format_inr
from unitwatt.report import _consents, _documents

INK, MUTED, LINE, ACCENT, BAD = "#1b1f24", "#5b6470", "#d9dee4", "#0f6b5c", "#b42318"


def savings_report_pdf(results, measures: list[str]) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    s, m, f = results.savings, results.savings.model, results.factory
    styles = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=styles["BodyText"], fontName="Helvetica", fontSize=9.5, leading=13,
                          textColor=colors.HexColor(INK))
    small = ParagraphStyle("small", parent=body, fontSize=8, leading=10.5, textColor=colors.HexColor(MUTED))
    h1 = ParagraphStyle("h1", parent=body, fontName="Helvetica-Bold", fontSize=17, leading=21, spaceAfter=2)
    h2 = ParagraphStyle("h2", parent=body, fontName="Helvetica-Bold", fontSize=11.5, leading=15, spaceBefore=10, spaceAfter=4)
    cell = ParagraphStyle("cell", parent=body, fontSize=8.5, leading=11)
    cell_right = ParagraphStyle("cell_right", parent=cell, alignment=2)
    mono = ParagraphStyle("mono", parent=cell, fontName="Courier", fontSize=6.5, leading=8.5)

    def p(text: str, style=body) -> Paragraph:
        return Paragraph(text, style)

    def table(rows: list[list], widths: list[float], numeric_from: int | None = None) -> Table:
        def wrap(c, col: int):
            if isinstance(c, Paragraph):
                return c
            numeric = numeric_from is not None and col >= numeric_from
            return p(escape(str(c)), cell_right if numeric else cell)

        t = Table([[wrap(c, i) for i, c in enumerate(row)] for row in rows], colWidths=[w * mm for w in widths], repeatRows=1)
        style = [("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.HexColor(LINE)),
                 ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor(MUTED)),
                 ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
        t.setStyle(TableStyle(style))
        return t

    story = []
    if results.synthetic is not None:
        story += [p("<b>Demo:</b> built from a synthetic forging unit, not a real factory. Replace with real bills before use.",
                    ParagraphStyle("banner", parent=small, textColor=colors.HexColor("#6b3d00"), backColor=colors.HexColor("#fff4e5"),
                                   borderPadding=5)), Spacer(1, 8)]
    story += [
        p("Energy savings report", h1),
        p(escape(f"IPMVP Option C (whole facility) · {f.name}, {f.city} · Consumer no. {f.consumer_number} · "
                 f"prepared {date.today().isoformat()}"), small),
        Spacer(1, 8),
    ]
    tiles = [
        ("Energy avoided", f"{format_inr(s.avoided_kwh)} kWh"),
        ("Savings vs adjusted baseline", f"{s.savings_pct * 100:.1f}%"),
        ("Uncertainty (90% confidence)", f"±{format_inr(s.uncertainty_kwh)} kWh"),
        ("Value at the period's energy price", f"Rs {format_inr(s.rs_saved)}"),
        ("CO2 avoided (grid factor)", f"{s.tco2_avoided:.1f} t"),
        ("Data quality score", f"{results.quality.score:.1f} / 100 ({results.quality.grade})"),
    ]
    grid = [[p(f"<font color='{MUTED}' size='8'>{k}</font><br/><b><font size='13'>{escape(v)}</font></b>") for k, v in tiles[i:i + 3]]
            for i in (0, 3)]
    tg = Table(grid, colWidths=[58 * mm] * 3)
    tg.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor(LINE)), ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor(LINE)),
                            ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    story += [tg, p("ADEETIE 10% demonstrated-savings test", h2)]
    if s.meets_adeetie:
        story.append(p(f"<font color='{ACCENT}'><b>Meets the 10% requirement: {s.savings_pct * 100:.1f}% demonstrated.</b></font>"))
    else:
        story += [
            p(f"<font color='{BAD}'><b>Not yet: {s.savings_pct * 100:.1f}% demonstrated.</b></font> A further "
              f"{format_inr(s.gap_to_target_kwh)} kWh over the same period would reach 10%."),
            p("Operational measures alone rarely reach 10% in a forging unit; the remaining gap is the case for an "
              "investment-grade audit and equipment upgrade under ADEETIE.", small),
        ]
    story += [p("Measures implemented", h2)] + [p("• " + escape(x)) for x in measures]

    terms = " + ".join([f"{m.coef['base']:.1f}", f"{m.coef['production_day']:.1f} × production day"]
                       + [f"{m.coef[pid]:.1f} × tonnes {pid}" for pid in m.products])
    story += [
        p("Method", h2),
        p("Daily electricity (grid + generator) is modelled on the baseline period as "
          f"<font name='Courier' size='8'>kWh/day = {escape(terms)}</font> (non-negative least squares). Savings = baseline "
          "model adjusted to the reporting period's actual production − actual energy. Uncertainty follows ASHRAE "
          "Guideline 14 with residual autocorrelation."),
        Spacer(1, 4),
        table([["Baseline period", f"{s.baseline[0]} to {s.baseline[1]} ({m.n} days used)"],
               ["Reporting period", f"{s.reporting[0]} to {s.reporting[1]} ({s.m} days)"],
               ["Tariff", f"{results.tariff.name}, effective {results.tariff.effective_from} ({results.tariff.status})"]],
              [40, 134]),
        p("Statistical checks", h2),
        table([["Check", "Result", "Requirement", ""]]
              + [[c.name, c.value, c.threshold, p(f"<font color='{ACCENT if c.passed else BAD}'><b>{'Pass' if c.passed else 'Not met'}</b></font>", cell)]
                 for c in s.checks], [70, 34, 46, 24]),
        p("Thresholds: LBNL guidance for meter-based IPMVP Option C claims (CV(RMSE) below 25%, NMBE within 0.5%).", small),
        p("Monthly results", h2),
        table([["Month", "Adjusted baseline kWh", "Actual kWh", "Avoided kWh", "Savings"]]
              + [[month, format_inr(r.adjusted_baseline), format_inr(r.actual), format_inr(r.avoided), f"{r.savings_pct * 100:.1f}%"]
                 for month, r in s.monthly.iterrows()], [30, 40, 36, 36, 32], numeric_from=1),
        p("Appendix A · Source documents", h2),
        p("Every figure traces to these files. SHA-256 hashes let an auditor confirm that nothing was altered after upload; "
          f"the audit chain {'verifies' if results.audit.verify()[0] else 'is BROKEN'}.", small),
        table([["#", "Document", "Type", "SHA-256"]]
              + [[d["id"], d["name"], d["kind"], p(d["sha256"] or "", mono)] for d in _documents(results)], [8, 52, 30, 84]),
    ]
    consents = _consents(results, "savings report")
    if consents:
        story += [p("Appendix B · Consent to share", h2),
                  table([["When", "Recipient", "Granted by", "Purpose"]]
                        + [[c["recorded_at"], c["recipient"], c["granted_by"], c["purpose"]] for c in consents], [36, 46, 30, 62])]
    story += [
        Spacer(1, 10),
        p("UnitWatt prepares this data pack. The certified energy auditor verifies the measurement and signs the M&amp;V.", small),
        Spacer(1, 26),
        table([["Unit owner", "Certified energy auditor (name, BEE registration no.)"]], [87, 87]),
    ]

    buf = io.BytesIO()

    def footer(canvas, doc) -> None:
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor(MUTED))
        canvas.drawString(18 * mm, 10 * mm, f"UnitWatt energy savings report · {f.name}")
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=18 * mm,
                            title="Energy savings report", author="UnitWatt", subject=f.name)
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
