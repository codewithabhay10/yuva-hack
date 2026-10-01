"""Stage 4: plain-language advice the owner can act on (P14).

Template-constrained: every number in a message comes from a computed field and is
formatted here; the template only supplies the words around it. An LLM may later rephrase
the words (or translate through Bhashini or IndicTrans2), but never the numbers, and
``numbers_in`` lets a test prove that no number appeared that was not computed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

HINDI_MONTHS = ["जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून", "जुलाई", "अगस्त", "सितंबर", "अक्टूबर", "नवंबर", "दिसंबर"]


def format_inr(value: float) -> str:
    """Indian digit grouping: 614400 -> '6,14,400'."""
    negative = value < 0
    digits = f"{abs(round(value)):d}"
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
        digits = f"{head},{tail}"
    return f"-{digits}" if negative else digits


def format_lakh(value: float) -> str:
    if abs(value) >= 1e7:
        return f"₹{value / 1e7:.2f} crore"
    if abs(value) >= 1e5:
        return f"₹{value / 1e5:.2f} lakh"
    return f"₹{format_inr(value)}"


@dataclass
class OwnerFacts:
    owner_name: str
    factory_name: str
    month: date
    rupees_lost_month: float
    saved_rs_month: float
    saved_kwh_month: float
    sec_kwh_per_t: float
    sec_change_pct: float
    top_action_title: str
    top_action_title_hi: str
    top_action_rs_month: float
    alert_since: date | None = None
    alert_pct: float = 0.0

    def fields(self) -> dict[str, str]:
        """Every number the message may show, already formatted."""
        f = {
            "lost": format_inr(self.rupees_lost_month),
            "saved": format_inr(self.saved_rs_month),
            "saved_kwh": format_inr(self.saved_kwh_month),
            "sec": f"{self.sec_kwh_per_t:.0f}",
            "sec_change": f"{abs(self.sec_change_pct):.1f}",
            "action_rs": format_inr(self.top_action_rs_month),
            "alert_pct": f"{abs(self.alert_pct):.1f}",
            "year": f"{self.month.year}",
        }
        if self.alert_since:
            f["alert_day"] = f"{self.alert_since.day}"
        return f


def owner_message(facts: OwnerFacts, language: str = "en") -> str:
    f = facts.fields()
    up = facts.sec_change_pct > 0
    if language == "hi":
        month = f"{HINDI_MONTHS[facts.month.month - 1]} {f['year']}"
        trend = f"पिछले महीने से {f['sec_change']}% {'ज़्यादा' if up else 'कम'}"
        lines = [
            f"नमस्ते {facts.owner_name} जी 🙏",
            f"UnitWatt – {month} की रिपोर्ट ({facts.factory_name})",
            "",
            f"💸 इस महीने बचाए जा सकते थे: ₹{f['lost']}",
            f"✅ पुराने तरीके के मुकाबले बचत: ₹{f['saved']} ({f['saved_kwh']} यूनिट)",
            f"⚡ प्रति टन बिजली: {f['sec']} यूनिट ({trend})",
            "",
            f"👉 सबसे पहले यह करें: {facts.top_action_title_hi}। हर महीने लगभग ₹{f['action_rs']} बचेंगे।",
        ]
        if facts.alert_since:
            alert_month = HINDI_MONTHS[facts.alert_since.month - 1]
            lines.append(
                f"⚠️ चेतावनी: {f['alert_day']} {alert_month} से प्रति टन बिजली {f['alert_pct']}% ज़्यादा लग रही है। "
                "इंडक्शन हीटर की जांच कराएं।"
            )
        lines += ["", "विवरण के लिए 1 भेजें। बचत रिपोर्ट बैंक को भेजने के लिए 2 भेजें।"]
        return "\n".join(lines)

    month = f"{facts.month:%B} {f['year']}"
    trend = f"{f['sec_change']}% {'higher' if up else 'lower'} than last month"
    lines = [
        f"Namaste {facts.owner_name} ji 🙏",
        f"UnitWatt – {month} report ({facts.factory_name})",
        "",
        f"💸 Money you could have kept this month: ₹{f['lost']}",
        f"✅ Saved against your old pattern: ₹{f['saved']} ({f['saved_kwh']} kWh)",
        f"⚡ Energy per tonne: {f['sec']} kWh ({trend})",
        "",
        f"👉 Do this first: {facts.top_action_title}. Saves about ₹{f['action_rs']} a month.",
    ]
    if facts.alert_since:
        lines.append(
            f"⚠️ Alert: since {f['alert_day']} {facts.alert_since:%b}, each tonne is taking {f['alert_pct']}% more "
            "electricity than expected. Get the induction heater checked."
        )
    lines += ["", "Reply 1 for details. Reply 2 to send the savings report to your bank."]
    return "\n".join(lines)


_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def numbers_in(text: str) -> set[str]:
    return set(_NUMBER.findall(text))
