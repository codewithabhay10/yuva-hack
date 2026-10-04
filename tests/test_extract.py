import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from unitwatt.extract import (
    ExtractionError,
    Provider,
    RegisterPage,
    available_readers,
    default_reader,
    extract_bill,
    extract_register,
    resolve_provider,
)
from unitwatt.extract_eval import compare, sample_files, truth_for
from unitwatt.offline_extract import Cell, numbers, parse_bill
from unitwatt.profiles import load_factory
from unitwatt.schemas import BillDocument

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "sample_bills"
JUNE_PDF = SAMPLES / "specimen_bill_2026_06.pdf"
JUNE = BillDocument.model_validate_json((SAMPLES / "specimen_bill_2026_06.json").read_text())
KEYS = ["GEMINI_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY", "OLLAMA_HOST",
        "UNITWATT_READER", "UNITWATT_MODEL", "UNITWATT_BASE_URL", "UNITWATT_API_KEY"]


def _misses(truth: BillDocument, got: BillDocument) -> list:
    return [s for s in compare(truth, got) if not s.ok]


# --- Offline reader ---------------------------------------------------------------------------


def test_numbers_skip_dates_times_codes_percentages_and_rates():
    assert numbers("01-06-2026 – 30-06-2026 09:00–17:00 TOD-1 HTM-7718203 @ 6.0% ₹ 6,35,981.94") == [635981.94]
    assert numbers("Demand charges on 187.5 kVA @ Rs 400 1,10,430.00", money=True) == [110430.0]
    assert numbers("4.70.724.00") == [470724.0]  # OCR read the lakh commas as points
    assert numbers("PF rebate -3,702.96") == [-3702.96]


@pytest.mark.parametrize("path", [p for p in sample_files() if p.suffix == ".pdf"], ids=lambda p: p.name)
def test_offline_reader_is_exact_on_pdf_text_layers(path):
    truth = BillDocument.model_validate_json(truth_for(path).read_text())
    result = extract_bill(path.read_bytes(), "application/pdf", "offline")
    assert "text layer" in result.model
    assert result.bill == truth
    assert result.issues == []


@pytest.mark.parametrize("path", [p for p in sample_files() if p.suffix != ".pdf"], ids=lambda p: p.name)
def test_offline_reader_reads_scans_and_phone_photos(path):
    pytest.importorskip("rapidocr_onnxruntime")
    truth = BillDocument.model_validate_json(truth_for(path).read_text())
    media_type = "image/png" if path.suffix == ".png" else "image/jpeg"
    result = extract_bill(path.read_bytes(), media_type, "offline")
    assert "OCR" in result.model
    assert _misses(truth, result.bill) == []
    assert [i for i in result.issues if i.severity == "error"] == []


def _page(rows: list[tuple[str, str]]) -> list[list[Cell]]:
    """A one-column bill: label at the left, value to its right, one row every 20 points."""
    cells = []
    for i, (label, value) in enumerate(rows):
        y = 40 + 20 * i
        cells += [Cell(label, 20, y, 160, y + 10), Cell(value, 220, y, 300, y + 10)]
    return [cells]


def test_offline_reader_flags_what_it_cannot_find():
    bill, issues = parse_bill(_page([
        ("Billing period", "01-06-2026 to 30-06-2026"), ("Units consumed", "1,000"), ("Maximum demand", "50 kVA"),
        ("Contract demand", "60 kVA"), ("Energy charges", "8,000.00"), ("Net amount payable", "9,000.00"),
    ]))
    assert bill.kwh_total == 1000 and bill.max_demand_kva == 50 and bill.total_amount == 9000
    assert [(i.severity, i.field) for i in issues] == [("error", "demand_charges")]
    with pytest.raises(ExtractionError, match="Could not find enough bill fields"):
        parse_bill(_page([("Net amount payable", "9,000.00"), ("Energy charges", "8,000.00")]))


# --- Free vision model behind an OpenAI-compatible API ----------------------------------------


class _FakeAPI:
    """A local OpenAI-compatible server that replays canned replies and records each request."""

    def __init__(self, replies: list[tuple[int, dict]]):
        self.replies, self.requests = list(replies), []
        api = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - http.server naming
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                api.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
                status, payload = api.replies.pop(0)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.provider = Provider("test", "Test API", f"http://127.0.0.1:{self.server.server_port}/v1",
                                 "free-vision-model", ("UNITWATT_TEST_KEY",))

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _reply(content: str, finish_reason: str = "stop") -> tuple[int, dict]:
    return 200, {"model": "free-vision-model-001", "choices": [{"message": {"role": "assistant", "content": content},
                                                                "finish_reason": finish_reason}]}


@pytest.fixture
def fake_api(monkeypatch):
    monkeypatch.setenv("UNITWATT_TEST_KEY", "k-123")
    servers = []

    def start(*replies):
        servers.append(_FakeAPI(list(replies)))
        return servers[-1]

    yield start
    for server in servers:
        server.close()


def test_free_api_request_and_validation(fake_api):
    api = fake_api(_reply("```json\n" + JUNE.model_dump_json() + "\n```"))
    result = extract_bill(JUNE_PDF.read_bytes(), "application/pdf", api.provider)
    assert result.bill == JUNE and result.issues == []
    assert result.model == "Test API: free-vision-model-001"
    (request,) = api.requests
    assert request["path"] == "/v1/chat/completions" and request["auth"] == "Bearer k-123"
    body = request["body"]
    assert body["model"] == "free-vision-model" and body["response_format"] == {"type": "json_object"}
    prompt, image = body["messages"][0]["content"]
    assert "JSON Schema" in prompt["text"] and "6,35,981.94" in prompt["text"]  # schema and the PDF's text layer
    assert image["image_url"]["url"].startswith("data:image/png;base64,")  # the page, rendered for a vision model


def test_free_api_gets_one_retry_with_the_validation_error(fake_api):
    api = fake_api(_reply('{"total_amount": "lots"}'), _reply(JUNE.model_dump_json()))
    assert extract_bill(JUNE_PDF.read_bytes(), "application/pdf", api.provider).bill == JUNE
    retry = api.requests[1]["body"]["messages"]
    assert [m["role"] for m in retry] == ["user", "assistant", "user"]
    assert "does not match the schema" in retry[2]["content"] and "period_start" in retry[2]["content"]


@pytest.mark.parametrize("reply, message", [
    ((429, {"error": {"message": "Resource has been exhausted"}}), "free-tier limit"),
    ((401, {"error": {"message": "API key not valid"}}), "rejected the key"),
    ((404, {"error": {"message": "model not found"}}), "UNITWATT_MODEL"),
    (_reply("{", finish_reason="length"), "cut off"),
    (_reply(""), "returned nothing"),
])
def test_free_api_errors_are_explained(fake_api, reply, message):
    api = fake_api(reply, reply)
    with pytest.raises(ExtractionError, match=message):
        extract_bill(JUNE_PDF.read_bytes(), "application/pdf", api.provider)


def test_model_without_json_mode_is_asked_by_prompt_alone(fake_api):
    api = fake_api((400, {"error": "response_format is not supported by this model"}), _reply(JUNE.model_dump_json()))
    assert extract_bill(JUNE_PDF.read_bytes(), "application/pdf", api.provider).bill == JUNE
    assert "response_format" in api.requests[0]["body"] and "response_format" not in api.requests[1]["body"]


def test_missing_key_says_which_one_to_set(monkeypatch):
    monkeypatch.delenv("UNITWATT_MISSING_KEY", raising=False)
    keyless = Provider("test", "Test API", "http://127.0.0.1:9/v1", "m", ("UNITWATT_MISSING_KEY",), "https://example.org/keys")
    with pytest.raises(ExtractionError, match="Set UNITWATT_MISSING_KEY .free from https://example.org/keys."):
        extract_bill(JUNE_PDF.read_bytes(), "application/pdf", keyless)


def test_unreachable_api_is_explained():
    closed = Provider("ollama", "Ollama on this computer", "http://127.0.0.1:9/v1", "qwen2.5vl:7b")
    with pytest.raises(ExtractionError, match="Is Ollama running"):
        extract_bill(JUNE_PDF.read_bytes(), "application/pdf", closed, timeout=5)


def test_register_photo_via_free_api(fake_api):
    page = {"rows": [{"day": "2026-06-03", "product": "flange", "tonnes": 1.2, "as_written": "फ्लेंज 12 क्विंटल"}]}
    api = fake_api(_reply(json.dumps(page)))
    photo = (SAMPLES / "specimen_bill_2026_06_photo.jpg").read_bytes()
    result = extract_register(photo, "image/jpeg", load_factory("demo_forge"), api.provider)
    assert result == RegisterPage.model_validate(page)
    assert "flange" in api.requests[0]["body"]["messages"][0]["content"][0]["text"]
    with pytest.raises(ExtractionError, match="Handwriting needs a vision model"):
        extract_register(photo, "image/jpeg", load_factory("demo_forge"), "offline")


def test_reader_choice_follows_the_environment(monkeypatch):
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
    assert available_readers() == ["offline"] and default_reader() == "offline"
    monkeypatch.setenv("GEMINI_API_KEY", "free-key")
    assert available_readers() == ["offline", "gemini"] and default_reader() == "gemini"
    assert resolve_provider("gemini").api_key == "free-key"
    monkeypatch.setenv("UNITWATT_MODEL", "gemini-2.5-flash")
    assert resolve_provider("gemini").model == "gemini-2.5-flash"
    monkeypatch.setenv("UNITWATT_READER", "offline")
    assert default_reader() == "offline"
    with pytest.raises(ExtractionError, match="Unknown reader"):
        resolve_provider("paid-api")
