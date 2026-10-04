# Specimen bills

Test fixtures for bill reading (P1). **These are not real bills.** They are issued by
fictional distribution companies, carry a SPECIMEN watermark, and every name, number and rate on
them is made up. Do not present them as a real factory's data.

| File | Use |
|---|---|
| `specimen_bill_2026_06.pdf` | Layout A: a clean PDF, as a DISCOM portal would provide |
| `specimen_bill_2026_06.png` | Layout A: a clean scan |
| `specimen_bill_2026_06_photo.jpg` | Layout A: a phone photo (angled, shadowed, warm light, sensor noise) |
| `specimen_bill_2026_06.json` | Layout A ground truth: the `BillDocument` a correct extraction returns |
| `specimen_bill_layout_b.pdf` | Layout B: a different design and different wording (kVAh billing, PF rebate, wrapped table cells) |
| `specimen_bill_layout_b_photo.jpg` | Layout B: a phone photo |
| `specimen_bill_layout_b.json` | Layout B ground truth |
| `specimen_bill_layout_b.html` | Layout B source, to render it again |

**Layout A** is June 2026 for the synthetic demo unit: 60,178.4 kWh, PF 0.856 (a ₹18,828.96
power-factor surcharge), total ₹6,35,981.94. It passes every arithmetic check and reprices
exactly under `bihar-hts-industrial`. **Layout B** is a kVAh-billed unit with a 1% PF rebate
and a 75% billing-demand floor, total ₹4,64,172.69.

To test bill reading:

```bash
python -m unitwatt.extract_eval                   # offline reader, every file here, field by field
python -m unitwatt.extract_eval --reader gemini   # the same through Gemini's free tier (GEMINI_API_KEY)
```

You can also upload a PDF or photo on the dashboard's Bill check tab and compare it with the
JSON, or upload the JSON to test the checks alone. To render another month of layout A, run
`python -m unitwatt.specimen_bill 2026-09 bill.html` and print the page to PDF from a browser.
