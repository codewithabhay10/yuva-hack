# Specimen bills

Test fixtures for bill reading (P1). **These are not real bills.** They are issued by a
fictional distribution company, carry a SPECIMEN watermark, and every name, number and rate on
them is made up. Do not present them as a real factory's data.

| File | Use |
|---|---|
| `specimen_bill_2026_06.pdf` | A clean PDF, as a DISCOM portal would provide |
| `specimen_bill_2026_06.png` | A clean scan |
| `specimen_bill_2026_06_photo.jpg` | A phone photo: angled, shadowed, warm light, sensor noise |
| `specimen_bill_2026_06.json` | Ground truth: the `BillDocument` a correct extraction returns |

June 2026 for the synthetic demo unit: 60,178.4 kWh, PF 0.856 (a ₹18,828.96 power-factor
surcharge), total ₹6,35,981.94. The figures pass every arithmetic check and reprice exactly
under `bihar-hts-industrial`.

To test extraction, upload the PDF or photo on the dashboard's Bill check tab (needs
`ANTHROPIC_API_KEY`) and compare with the JSON, or upload the JSON to test the checks alone.
Render another month with `python -m unitwatt.specimen_bill 2026-09 bill.html` and print it
to PDF from a browser.
