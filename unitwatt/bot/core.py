"""The UnitWatt chat bot (P2, P14, P18): one engine behind WhatsApp, Telegram and the simulator.

The owner, supervisor or accountant messages the factory's number, and the bot:

- answers with this month's report, the top actions, tomorrow's batch plan and alerts, in Hindi or English;
- takes the day's production as one line of text or a voice note, and confirms before saving it;
- reads a photo or PDF of the electricity bill, runs the arithmetic checks, points out penalties,
  and confirms before saving it;
- sends the savings report as a PDF, only after the owner consents.

Every number in a reply comes from a computed field (P14). What a sender may do depends on their
role in the factory profile (P19); a number that is not in the profile gets a polite refusal and
nothing else.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone

from unitwatt.messages import HINDI_MONTHS, format_inr, format_lakh, owner_message

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

PERMISSIONS = {
    "report": {"owner", "accountant"},
    "savings_report": {"owner"},
    "plan": {"owner", "supervisor"},
    "alerts": {"owner", "supervisor", "accountant"},
    "production": {"owner", "supervisor"},
    "bill": {"owner", "accountant"},
}
ROLE_HI = {"owner": "मालिक", "supervisor": "सुपरवाइज़र", "accountant": "अकाउंटेंट", "auditor": "ऑडिटर"}

WORDS = {
    "menu": {"hi", "hello", "hey", "namaste", "namaskar", "नमस्ते", "नमस्कार", "हाय", "हेलो", "menu", "मेनू", "help",
             "मदद", "start", "/start", "0"},
    "report": {"1", "report", "रिपोर्ट", "hisab", "हिसाब"},
    "savings_report": {"2", "savings report", "savings", "bank", "बचत रिपोर्ट", "बैंक"},
    "plan": {"3", "plan", "schedule", "tomorrow", "kal", "kal ka plan", "कल", "कल का प्लान", "प्लान"},
    "alerts": {"4", "alert", "alerts", "warning", "warnings", "चेतावनी", "चेतावनियाँ", "अलर्ट"},
    "details": {"details", "detail", "विवरण"},
    "hi": {"hindi", "हिंदी", "हिन्दी"},
    "en": {"english", "अंग्रेज़ी", "अंग्रेजी", "इंग्लिश"},
    "yes": {"yes", "y", "haan", "han", "ha", "हाँ", "हां", "ji", "जी", "ok", "okay", "save", "agree", "✅"},
    "no": {"no", "n", "nahi", "nahin", "नहीं", "cancel", "correct", "edit", "discard", "❌"},
}
_YESTERDAY = re.compile(r"yesterday|kal ka|कल का|बीते कल", re.IGNORECASE)
LOSS_HI = {"excess_demand": "अनुबंध मांग से ज़्यादा मांग का जुर्माना", "pf_penalty": "पावर फैक्टर का जुर्माना",
           "demand_floor": "न्यूनतम मांग शुल्क", "billing_error": "बिल की गणना में अंतर"}
CONFIDENCE_HI = {"High": "ऊँचा", "Medium": "मध्यम", "Low": "कम"}
MACHINE_HI = {"induction_heater": "बिलेट हीटिंग", "forging_press": "फोर्जिंग", "shot_blaster": "शॉट ब्लास्टिंग"}


@dataclass
class Incoming:
    """One message from a person, whatever the channel."""

    sender: str                 # phone number, digits with country code
    text: str = ""
    kind: str = "text"          # text | image | document | audio
    media: bytes | None = None
    mime: str = ""
    filename: str = ""
    message_id: str = ""
    channel: str = "whatsapp"


@dataclass
class Reply:
    text: str
    buttons: list[tuple[str, str]] = field(default_factory=list)  # (id, title): at most 3, titles of 20 characters or fewer
    document: tuple[str, bytes, str] | None = None                # (file name, content, mime type)


def _norm(text: str) -> str:
    text = (text or "").translate(_DEVANAGARI_DIGITS).strip().lower()
    return re.sub(r"\s+", " ", text.strip(" .,!?।:;-"))


def _tr(lang: str, en: str, hi: str) -> str:
    return hi if lang == "hi" else en


def _day(d: date, lang: str) -> str:
    return f"{d.day} {HINDI_MONTHS[d.month - 1]}" if lang == "hi" else f"{d.day} {d:%b}"


def _month(d: date, lang: str) -> str:
    return f"{HINDI_MONTHS[d.month - 1]} {d.year}" if lang == "hi" else f"{d:%B %Y}"


class Bot:
    def __init__(self, results, store, reader=None, stt=None, measures: list[str] | None = None, now=None):
        from unitwatt.pipeline import DEMO_MEASURES

        self.R, self.F, self.store = results, results.factory, store
        self.reader = reader            # bill reader name or Provider; None = the configured default
        self.stt = stt                  # SpeechToText; None = the configured default
        self.measures = measures or DEMO_MEASURES
        self.now = now or (lambda: datetime.now(IST))

    # Entry point -----------------------------------------------------------------------------

    def handle(self, msg: Incoming) -> list[Reply]:
        contact = self.F.contact(msg.sender)
        if contact is None:
            return [Reply(
                f"🙏 This number is not registered with UnitWatt for {self.factory_name}. Ask the owner to add it.\n"
                f"यह नंबर {self.factory_name} के UnitWatt पर पंजीकृत नहीं है। मालिक से इसे जुड़वाने के लिए कहें।"
            )]
        state = self.store.session(contact.phone)
        lang = state.get("lang") or contact.language
        self.store.log(contact.phone, "in", msg.kind, msg.text or f"[{msg.kind}{' ' + msg.filename if msg.filename else ''}]")
        try:
            replies = self._route(msg, contact, state, lang)
        except Exception:  # noqa: BLE001 - the sender must always get an answer
            log.exception("bot failed on a message from %s", contact.phone)
            replies = [Reply(_tr(state.get("lang") or lang, "Something went wrong on our side. Please try again in a minute.",
                                 "हमारी तरफ़ कुछ गड़बड़ हुई। एक मिनट बाद फिर कोशिश करें।"))]
        self.store.save_session(contact.phone, state)
        for r in replies:
            self.store.log(contact.phone, "out", "document" if r.document else "text",
                           r.text + (f" [{r.document[0]}]" if r.document else ""))
        return replies

    @property
    def factory_name(self) -> str:
        return self.F.name.split(" (")[0]

    def _route(self, msg: Incoming, c, state: dict, lang: str) -> list[Reply]:
        if msg.kind == "audio":
            return self._voice(msg, c, state, lang)
        if msg.kind in ("image", "document"):
            return self._document(msg, c, state, lang)
        return self._text(msg.text, c, state, lang)

    def _allowed(self, action: str, c, lang: str) -> Reply | None:
        if c.role in PERMISSIONS[action]:
            return None
        return Reply(_tr(lang, f"🙏 Sorry, that isn't available on your account ({c.role}). Please ask {self.F.owner_name} ji.",
                         f"🙏 माफ़ कीजिए, यह आपके खाते ({ROLE_HI.get(c.role, c.role)}) में उपलब्ध नहीं है। "
                         f"{self.F.owner_name} जी से कहें।"))

    # Text ------------------------------------------------------------------------------------

    def _text(self, text: str, c, state: dict, lang: str, source: str = "text") -> list[Reply]:
        t = _norm(text)
        pending = state.get("pending")
        if pending and (t in WORDS["yes"] or t == "1"):
            return self._confirm(c, state, lang)
        if pending and (t in WORDS["no"] or t == "2"):
            state.pop("pending")
            return [Reply(_tr(lang, "Cancelled. Send it again when you're ready.", "रद्द कर दिया। तैयार हों तो फिर भेजें।"))]
        if t in WORDS["hi"] or t in WORDS["en"]:
            state["lang"] = lang = "hi" if t in WORDS["hi"] else "en"
            menu = self._menu(c, lang)
            return [Reply(_tr(lang, "Language set to English.", "भाषा हिंदी कर दी गई है।") + "\n\n" + menu.text, menu.buttons)]
        if t in WORDS["menu"]:
            return [self._menu(c, lang)]
        if t in WORDS["details"] or (t == "1" and state.get("last") == "report"):
            state.pop("last", None)
            return [self._allowed("report", c, lang) or self._details(lang)]
        if t in WORDS["report"]:
            return [self._allowed("report", c, lang) or self._report(c, state, lang)]
        if t in WORDS["savings_report"]:
            return [self._allowed("savings_report", c, lang) or self._ask_consent(state, lang)]
        if t in WORDS["plan"]:
            return [self._allowed("plan", c, lang) or self._plan(lang)]
        if t in WORDS["alerts"]:
            return [self._allowed("alerts", c, lang) or self._alerts(lang)]

        from unitwatt.ingest import parse_daily_entry

        parsed = parse_daily_entry(text, self.F)
        if parsed.tonnes:
            return [self._allowed("production", c, lang) or self._production(text, parsed, state, lang, source)]
        menu = self._menu(c, lang)
        return [Reply(_tr(lang, "Sorry, I didn't understand that.", "माफ़ कीजिए, समझ नहीं आया।") + "\n\n" + menu.text, menu.buttons)]

    def _menu(self, c, lang: str) -> Reply:
        options = [("report", "1", "This month's report", "इस महीने की रिपोर्ट"),
                   ("savings_report", "2", "Savings report for your bank or auditor", "बैंक या ऑडिटर के लिए बचत रिपोर्ट"),
                   ("plan", "3", "Tomorrow's production plan", "कल का उत्पादन प्लान"),
                   ("alerts", "4", "Alerts", "चेतावनियाँ")]
        lines = [_tr(lang, f"Namaste {c.name} ji 🙏", f"नमस्ते {c.name} जी 🙏"),
                 _tr(lang, f"UnitWatt for {self.factory_name}. Reply with a number:", f"{self.factory_name} के लिए UnitWatt। नंबर भेजें:")]
        lines += [f"{n} {_tr(lang, en, hi)}" for action, n, en, hi in options if c.role in PERMISSIONS[action]]
        sends = []
        if c.role in PERMISSIONS["production"]:
            sends.append(_tr(lang, "• today's production, e.g. flange 1.5t, crank 1.2t, gear 1.4t, 2 shifts",
                             "• आज का उत्पादन, जैसे: फ्लेंज 1.5 टन, क्रैंक 1.2 टन, गियर 1.4 टन, 2 शिफ्ट"))
        if c.role in PERMISSIONS["bill"]:
            sends.append(_tr(lang, "• a photo or PDF of the electricity bill", "• बिजली बिल की फोटो या PDF"))
        if c.role in PERMISSIONS["production"]:
            sends.append(_tr(lang, "• a voice note", "• वॉइस नोट"))
        if sends:
            lines += ["", _tr(lang, "Or send:", "या भेजें:")] + sends
        lines += ["", _tr(lang, "Type हिंदी for Hindi.", "English के लिए English लिखें।")]
        buttons = [b for action, b in [
            ("report", ("1", _tr(lang, "📊 Report", "📊 रिपोर्ट"))),
            ("plan", ("3", _tr(lang, "🗓️ Tomorrow's plan", "🗓️ कल का प्लान"))),
            ("alerts", ("4", _tr(lang, "🚨 Alerts", "🚨 चेतावनी"))),
        ] if c.role in PERMISSIONS[action]]
        return Reply("\n".join(lines), buttons[:3])

    # Answers ---------------------------------------------------------------------------------

    def _report(self, c, state: dict, lang: str) -> Reply:
        state["last"] = "report"
        text = owner_message(replace(self.R.owner_facts, owner_name=c.name), lang)
        buttons = [("details", _tr(lang, "🔍 Details", "🔍 विवरण"))]
        if c.role in PERMISSIONS["savings_report"]:
            buttons.append(("2", _tr(lang, "🏦 Savings report", "🏦 बचत रिपोर्ट")))
        return Reply(text, buttons)

    def _details(self, lang: str) -> Reply:
        lines = [_tr(lang, f"🔍 Top three actions for {self.factory_name}:", f"🔍 {self.factory_name} के लिए तीन सबसे ज़रूरी काम:")]
        for i, o in enumerate(self.R.opportunities_latest[:3], 1):
            per_month = format_inr(o.rupees_per_year / 12)
            if o.capex <= 0:
                cost = _tr(lang, "no capital needed", "कोई निवेश नहीं")
            else:
                cost = _tr(lang, f"capex {format_lakh(o.capex)}, pays back in {o.payback_months:.1f} months",
                           f"निवेश {format_lakh(o.capex)}, {o.payback_months:.1f} महीने में वसूल")
            confidence = _tr(lang, f"confidence {o.confidence.lower()}", f"भरोसा {CONFIDENCE_HI.get(o.confidence, o.confidence)}")
            lines.append(_tr(lang, f"{i}. {o.title}\n    ₹{per_month} a month · {cost} · {confidence}",
                             f"{i}. {o.title_hi}\n    ₹{per_month} हर महीने · {cost} · {confidence}"))
        return Reply("\n".join(lines))

    def _plan(self, lang: str) -> Reply:
        from unitwatt.scheduler import slot_label

        P, plan, current = self.R.problem, self.R.schedules["optimised"], self.R.schedules["current"]
        per_day = current.total_rs - plan.total_rs
        lines = [
            _tr(lang, f"🗓️ Tomorrow's plan for {self.factory_name}", f"🗓️ {self.factory_name} का कल का प्लान"),
            _tr(lang, f"Saves about ₹{format_inr(per_day)} a day against today's timings (₹{format_inr(per_day * P.working_days)} a month). "
                      f"Peak {plan.peak_kva:.0f} kVA; contract demand {P.contract_kva:.0f} kVA.",
                f"आज के समय के मुकाबले रोज़ लगभग ₹{format_inr(per_day)} की बचत (₹{format_inr(per_day * P.working_days)} हर महीने)। "
                f"अधिकतम मांग {plan.peak_kva:.0f} kVA; अनुबंध मांग {P.contract_kva:.0f} kVA।"),
            "",
        ]
        for job in sorted(P.jobs, key=lambda j: plan.starts[j.id]):
            start = plan.starts[job.id]
            number = re.sub(r"\D", "", job.name)
            name = job.name if lang != "hi" else f"{MACHINE_HI.get(job.machine, job.name)} {number}".strip()
            mark = "☀️" if str(P.bands[start]) == "solar" else "•"
            lines.append(f"{mark} {slot_label(start)}–{slot_label(start + job.slots)} {name}")
        lines += ["", _tr(lang, "☀️ = solar hours, the cheapest rate.", "☀️ = सोलर घंटे, सबसे सस्ती दर।")]
        return Reply("\n".join(lines))

    def _alerts(self, lang: str) -> Reply:
        lines = []
        d = self.R.drift
        if d is not None and d.alarm_date:
            pct, extra = f"{d.recent_excess_pct * 100:.1f}", format_inr(d.excess_kwh_since_alarm)
            lines.append(_tr(
                lang,
                f"🚨 Since {_day(d.alarm_date, lang)}, each tonne is taking {pct}% more electricity than expected: {extra} kWh extra so far. "
                "Likely causes: induction-coil or refractory-lining wear, cooling-water scaling, billets charged cold. "
                "Get the induction heater inspected before the next shutdown.",
                f"🚨 {_day(d.alarm_date, lang)} से हर टन पर उम्मीद से {pct}% ज़्यादा बिजली लग रही है: अब तक {extra} यूनिट ज़्यादा। "
                "संभावित कारण: इंडक्शन कॉइल या लाइनिंग का घिसना, कूलिंग पानी में स्केल, ठंडे बिलेट। "
                "अगली छुट्टी से पहले इंडक्शन हीटर की जांच कराएं।",
            ))
        latest = self.R.windows.latest_month
        if latest is not None:
            month = f"{latest[0]:%b %Y}"
            for f in self.R.findings:
                if f.is_loss and f.rupees > 0 and f.month == month:
                    lines.append(_tr(lang, f"💸 {_month(latest[0], lang)} bill: ₹{format_inr(f.rupees)} {f.title}",
                                     f"💸 {_month(latest[0], lang)} का बिल: ₹{format_inr(f.rupees)} {LOSS_HI.get(f.kind, f.title)}"))
        if not lines:
            lines.append(_tr(lang, "✅ No alerts: energy per tonne is on track.", "✅ कोई चेतावनी नहीं: प्रति टन बिजली ठीक है।"))
        return Reply("\n\n".join(lines))

    def _ask_consent(self, state: dict, lang: str) -> Reply:
        s = self.R.savings
        if s is None:
            return Reply(_tr(lang, "The savings report needs three months of data after a change; it isn't ready yet.",
                             "बचत रिपोर्ट के लिए बदलाव के बाद तीन महीने का डेटा चाहिए; अभी तैयार नहीं है।"))
        state["pending"] = {"type": "consent"}
        kwh, pct, rs = format_inr(s.avoided_kwh), f"{s.savings_pct * 100:.1f}", format_inr(s.rs_saved)
        return Reply(_tr(
            lang,
            f"🏦 Savings report for your bank or ADEETIE auditor:\n• {kwh} kWh saved ({pct}%), worth ₹{rs}\n"
            "• It includes your production and bill data\n\nDo you agree to share it with them? Nothing is shared without your consent.",
            f"🏦 बैंक या ADEETIE ऑडिटर के लिए बचत रिपोर्ट:\n• {kwh} यूनिट की बचत ({pct}%), कीमत ₹{rs}\n"
            "• इसमें आपका उत्पादन और बिल का डेटा है\n\nक्या आप इसे उनके साथ साझा करने की सहमति देते हैं? आपकी सहमति के बिना कुछ भी साझा नहीं होता।",
        ), [("yes", _tr(lang, "✅ I agree", "✅ सहमत हूँ")), ("no", _tr(lang, "❌ No", "❌ नहीं"))])

    # Production --------------------------------------------------------------------------------

    def _production(self, text: str, parsed, state: dict, lang: str, source: str) -> Reply:
        today = self.now().date()
        day = today - timedelta(days=1) if _YESTERDAY.search(text) else today
        state["pending"] = {"type": "production", "day": day.isoformat(), "tonnes": parsed.tonnes, "shifts": parsed.shifts,
                            "source": source, "raw": text}
        lines = [_tr(lang, f"📝 Got it for {_day(day, lang)}:", f"📝 {_day(day, lang)} के लिए मिला:")]
        for pid, t in parsed.tonnes.items():
            p = self.F.product(pid)
            lines.append(f"• {p.name_hi if lang == 'hi' else p.name}: {t:g} {_tr(lang, 't', 'टन')}")
        if parsed.shifts:
            lines.append(_tr(lang, f"• Shifts: {parsed.shifts}", f"• शिफ्ट: {parsed.shifts}"))
        if parsed.warnings:
            lines.append(_tr(lang, "⚠️ Not understood: ", "⚠️ यह हिस्सा समझ नहीं आया: ") + " ".join(parsed.warnings))
        lines += ["", _tr(lang, "Save it?", "सेव करें?")]
        return Reply("\n".join(lines), [("yes", _tr(lang, "✅ Save", "✅ सेव करें")), ("no", _tr(lang, "✏️ Correct", "✏️ सुधारें"))])

    # Photos, PDFs and voice notes ----------------------------------------------------------

    def _document(self, msg: Incoming, c, state: dict, lang: str) -> list[Reply]:
        from unitwatt.extract import OFFLINE, ExtractionError, default_reader, extract_bill, extract_register

        if not msg.media:
            return [Reply(_tr(lang, "The file didn't come through; please send it again.", "फ़ाइल नहीं मिली; कृपया फिर भेजें।"))]
        mime = msg.mime or ("application/pdf" if msg.filename.lower().endswith(".pdf") else "image/jpeg")
        stamp = self.now().strftime("%Y%m%d_%H%M%S")
        entry = self.R.audit.add_document("chat_upload", msg.filename or f"{msg.kind}_{stamp}", msg.media,
                                          {"from": c.name, "role": c.role, "channel": msg.channel})
        problem = ""
        if c.role in PERMISSIONS["bill"]:
            try:
                return [self._bill(extract_bill(msg.media, mime, self.reader), entry.id, state, lang)]
            except ExtractionError as exc:
                problem = str(exc)
        vision = (self.reader or default_reader()) != OFFLINE
        if c.role in PERMISSIONS["production"] and vision:
            try:
                page = extract_register(msg.media, mime, self.F, self.reader)
                if page.rows:
                    return [self._register(page, state, lang)]
            except ExtractionError as exc:
                problem = str(exc)
        if c.role not in PERMISSIONS["bill"] and not vision:
            return [Reply(_tr(lang, "📒 Reading register photos needs a free vision model (GEMINI_API_KEY). For now, type the day's "
                                    "production, e.g. flange 1.5t, crank 1.2t, gear 1.4t, 2 shifts.",
                              "📒 रजिस्टर की फोटो पढ़ने के लिए मुफ़्त विज़न मॉडल (GEMINI_API_KEY) चाहिए। अभी के लिए उत्पादन लिखकर भेजें, "
                              "जैसे: फ्लेंज 1.5 टन, क्रैंक 1.2 टन, गियर 1.4 टन, 2 शिफ्ट।"))]
        return [Reply(_tr(lang, f"⚠️ I couldn't read that. {problem} Try a flat, well-lit photo of the whole bill, or the PDF from the DISCOM portal.",
                          f"⚠️ यह पढ़ नहीं पाया। {problem} पूरे बिल की साफ़, सीधी फोटो या DISCOM पोर्टल वाली PDF भेजें।").replace("  ", " "))]

    def _bill(self, result, audit_entry: int, state: dict, lang: str) -> Reply:
        from unitwatt.opportunities import bill_forensics

        b = result.bill
        errors = [i for i in result.issues if i.severity == "error"]
        state["pending"] = {"type": "bill", "bill": b.model_dump(mode="json"), "audit_entry": audit_entry}
        how = re.sub(r"^offline \((.*)\)$", r"\1", result.model)  # "offline (OCR)" -> "OCR"
        lines = [
            _tr(lang, f"📄 Bill for {_month(b.period_start, lang)}, read with {how}:",
                f"📄 {_month(b.period_start, lang)} का बिल ({how} से पढ़ा):"),
            _tr(lang, f"• {format_inr(b.kwh_total)} kWh · max demand {b.max_demand_kva:g} kVA (contract {b.contract_demand_kva:g} kVA)",
                f"• {format_inr(b.kwh_total)} यूनिट · अधिकतम मांग {b.max_demand_kva:g} kVA (अनुबंध {b.contract_demand_kva:g} kVA)"),
        ]
        if b.power_factor:
            lines.append(_tr(lang, f"• Power factor {b.power_factor:.3f}", f"• पावर फैक्टर {b.power_factor:.3f}"))
        lines.append(_tr(lang, f"• Total ₹{format_inr(b.total_amount)}", f"• कुल ₹{format_inr(b.total_amount)}"))
        if errors:
            lines.append(_tr(lang, "⚠️ Please check: ", "⚠️ कृपया जांचें: ") + "; ".join(f"{i.field}: {i.message}" for i in errors[:3]))
        else:
            lines.append(_tr(lang, "✅ All arithmetic checks pass.", "✅ सारी गणना सही है।"))
        mine = re.sub(r"\W", "", self.F.consumer_number).lower()
        theirs = re.sub(r"\W", "", b.consumer_number or "").lower()
        if theirs and mine and theirs != mine:
            # Another connection's bill: this factory's tariff does not apply, so skip the penalty check.
            losses = []
            lines.append(_tr(lang, f"⚠️ Consumer no. {b.consumer_number} is not this factory's ({self.F.consumer_number}). Is this the right bill?",
                             f"⚠️ उपभोक्ता संख्या {b.consumer_number} इस फैक्ट्री की ({self.F.consumer_number}) नहीं है। क्या यह सही बिल है?"))
        else:
            losses = [f for f in bill_forensics([b], self.R.tariff) if f.is_loss and f.rupees > 0]
        if losses:
            lines.append(_tr(lang, "💸 Money lost on this bill:", "💸 इस बिल में नुकसान:"))
            lines += [_tr(lang, f"• ₹{format_inr(f.rupees)} {f.title}", f"• ₹{format_inr(f.rupees)} {LOSS_HI.get(f.kind, f.title)}")
                      for f in losses]
        lines += ["", _tr(lang, "Save it to the ledger?", "लेजर में सेव करें?")]
        return Reply("\n".join(lines), [("yes", _tr(lang, "✅ Save", "✅ सेव करें")), ("no", _tr(lang, "🗑️ Discard", "🗑️ छोड़ें"))])

    def _register(self, page, state: dict, lang: str) -> Reply:
        rows = [r for r in page.rows if r.product in self.F.product_ids]
        state["pending"] = {"type": "register", "rows": [r.model_dump(mode="json") for r in rows]}
        lines = [_tr(lang, f"📒 Register page read: {len(rows)} entries", f"📒 रजिस्टर का पन्ना पढ़ा: {len(rows)} एंट्री")]
        for r in rows:
            p = self.F.product(r.product)
            lines.append(f"• {_day(r.day, lang)}: {p.name_hi if lang == 'hi' else p.name} {r.tonnes:g} {_tr(lang, 't', 'टन')}")
        if page.unreadable:
            lines.append(_tr(lang, "⚠️ Couldn't read: ", "⚠️ पढ़ नहीं पाया: ") + "; ".join(page.unreadable))
        lines += ["", _tr(lang, "Save these?", "इन्हें सेव करें?")]
        return Reply("\n".join(lines), [("yes", _tr(lang, "✅ Save", "✅ सेव करें")), ("no", _tr(lang, "✏️ Correct", "✏️ सुधारें"))])

    def _voice(self, msg: Incoming, c, state: dict, lang: str) -> list[Reply]:
        from unitwatt.bot.transcribe import TranscriptionError, transcribe

        aliases = ", ".join(a for p in self.F.products for a in p.aliases)
        prompt = f"Daily production of a forging unit: {aliases}, tonne, टन, kg, quintal, shift, शिफ्ट."
        try:
            heard = transcribe(msg.media or b"", msg.mime, self.stt, prompt=prompt)
        except TranscriptionError as exc:
            return [Reply(_tr(lang, f"🎙️ I couldn't turn that voice note into text. {exc} Please type it, e.g. flange 1.5t, crank 1.2t, gear 1.4t, 2 shifts.",
                              f"🎙️ वॉइस नोट को टेक्स्ट में नहीं बदल पाया। {exc} कृपया लिखकर भेजें, जैसे: फ्लेंज 1.5 टन, क्रैंक 1.2 टन, गियर 1.4 टन, 2 शिफ्ट।"))]
        replies = self._text(heard, c, state, lang, source="voice")
        first = replies[0]
        replies[0] = Reply(_tr(lang, f"🎙️ I heard: “{heard}”", f"🎙️ मैंने सुना: “{heard}”") + "\n\n" + first.text, first.buttons, first.document)
        return replies

    # Confirmations ----------------------------------------------------------------------------

    def _confirm(self, c, state: dict, lang: str) -> list[Reply]:
        from unitwatt.schemas import BillDocument

        pending = state.pop("pending")
        kind = pending["type"]
        if kind == "production":
            day = date.fromisoformat(pending["day"])
            ids = self.store.add_production(day, pending["tonnes"], pending["shifts"], c.phone, pending["source"], pending["raw"])
            self.R.audit.add_event("production_entry", f"production {day}", {"by": c.name, "tonnes": pending["tonnes"],
                                                                               "shifts": pending["shifts"], "rows": ids})
            n = len(pending["tonnes"])
            return [Reply(_tr(lang, f"✅ Saved for {_day(day, lang)}: {n} products. Thank you, {c.name} ji.",
                              f"✅ {_day(day, lang)} के लिए सेव हो गया: {n} उत्पाद। धन्यवाद {c.name} जी।"))]
        if kind == "register":
            rows = pending["rows"]
            for r in rows:
                self.store.add_production(date.fromisoformat(r["day"]), {r["product"]: r["tonnes"]}, None, c.phone,
                                          "register photo", r["as_written"])
            self.R.audit.add_event("production_entry", "register page", {"by": c.name, "rows": len(rows)})
            return [Reply(_tr(lang, f"✅ Saved {len(rows)} register entries. Thank you, {c.name} ji.",
                              f"✅ रजिस्टर की {len(rows)} एंट्री सेव हो गईं। धन्यवाद {c.name} जी।"))]
        if kind == "bill":
            bill = BillDocument.model_validate(pending["bill"])
            row = self.store.add_bill(bill, c.phone, pending.get("audit_entry"))
            event = self.R.audit.add_event("bill_confirmed", f"bill {bill.period_start:%Y-%m}",
                                           {"by": c.name, "store_row": row, "source_entry": pending.get("audit_entry")})
            return [Reply(_tr(lang, f"✅ Bill for {_month(bill.period_start, lang)} saved to the ledger (audit entry #{event.id}).",
                              f"✅ {_month(bill.period_start, lang)} का बिल लेजर में सेव हो गया (ऑडिट एंट्री #{event.id})।"))]
        if kind == "consent":
            from unitwatt.report_pdf import savings_report_pdf

            entry = self.R.audit.record_consent("savings report", "bank / ADEETIE energy auditor", c.name,
                                                "ADEETIE application and loan appraisal")
            pdf = savings_report_pdf(self.R, self.measures)
            return [Reply(_tr(lang, f"✅ Consent recorded (audit entry #{entry.id}). Here is the report as a PDF: forward it to your bank or auditor.",
                              f"✅ सहमति दर्ज (ऑडिट एंट्री #{entry.id})। यह रही PDF रिपोर्ट: इसे अपने बैंक या ऑडिटर को भेज दें।"),
                          document=("unitwatt_savings_report.pdf", pdf, "application/pdf"))]
        return [Reply(_tr(lang, "Nothing to confirm.", "पुष्टि के लिए कुछ नहीं है।"))]
