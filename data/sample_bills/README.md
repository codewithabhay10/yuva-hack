# Test bills

Test fixtures for bill reading (P1). **These are not real bills.** Every utility, consumer,
name, number and rate on them is made up. Do not present them as a real factory's data.

- **Layouts A and B** carry a SPECIMEN watermark.
- **Layouts C, D and E** have no watermark, so they look and read like real bills. Each has one
  small footer line that says it is a test bill from a fictional utility.

None of them uses a real distribution company's name or logo.

| Files | Layout | What it tests |
|---|---|---|
| `specimen_bill_2026_06` `.pdf` `.png` `_photo.jpg` `.json` | A | June 2026 for the synthetic demo unit, as a portal PDF, a clean scan and a phone photo |
| `specimen_bill_layout_b` `.pdf` `_photo.jpg` `.json` `.html` | B | kVAh billing, a PF rebate, a 75% billing-demand floor, wrapped table cells |
| `bill_c_sindhuvan_2026_08` `.pdf` `_photo.jpg` `.json` `.html` | C | A dense state-utility bill (see below) |
| `bill_d_malhar_2026_07` `.pdf` `_photo.jpg` `.json` `.html` | D | A monospace computer printout on pre-printed stationery (see below) |
| `bill_e_uttarvahini_2026_09` `.pdf` `_photo.jpg` `.json` `.html` | E | A Hindi and English bill (see below) |

The `.json` beside each bill is the ground truth: the `BillDocument` a correct extraction returns.
The `.html` is the source, to render the bill again.

**Layout A** is June 2026 for the synthetic demo unit:
- 60,178.4 kWh at PF 0.856, which brings a ₹18,828.96 power-factor surcharge;
- total ₹6,35,981.94;
- it passes every arithmetic check and reprices exactly under `bihar-hts-industrial`.

**Layout B** is a kVAh-billed unit with a 1% PF rebate and a 75% billing-demand floor, total
₹4,64,172.69.

**Layout C** is a plastics moulder in August 2026:
- maximum demand 171.2 kVA against a 150 kVA contract, so ₹16,112 in excess-demand charges;
- PF 0.842, so a 5% low-PF surcharge;
- an FPPCA fuel adjustment;
- meter readings with a multiplying factor of 20;
- a consumption history, a numbered charges table beside the ToD table, and amounts before and after the due date;
- total ₹3,87,932.36.

**Layout D** is a steel re-rolling mill in July 2026:
- kVAh billing with peak, normal and night zones;
- billing demand at the 85% floor (425 kVA) because maximum demand was 362.4 kVA;
- rounded to the rupee, total ₹13,03,809.00.

**Layout E** is a rice mill in September 2026:
- a solar-hours zone billed 20% cheaper;
- a fuel surcharge as a percentage of energy charges;
- total ₹4,01,914.26.

To test bill reading:

```bash
python -m unitwatt.extract_eval                   # offline reader, every file here, field by field
python -m unitwatt.extract_eval --reader gemini   # the same through Gemini's free tier (GEMINI_API_KEY)
```

You can also upload a PDF or photo on the dashboard's Bill check tab, or send one to the bot,
and compare the result with the JSON. You can also upload the JSON to test the checks alone.

To render another month of layout A, run `python -m unitwatt.specimen_bill 2026-09 bill.html`
and print the page to PDF from a browser. For B to E, open the `.html` in a browser and print
it to PDF.
