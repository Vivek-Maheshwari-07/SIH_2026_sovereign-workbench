# Known issues

## OCR: short numeric cells in ruled tables are dropped (Track A, 2026-09-26)

Tesseract drops short numeric cells inside ruled tables (e.g. thickness tables). Key values are
still captured from findings text. Possible future fix: grid-line removal before OCR or vision
fallback for table regions.

- Seen on the scanned demo reports (`demo/inputs/scenario_a_report_1.pdf` and `_2.pdf`), after
  deskew, with mean OCR confidence still ~89-92 (so the vision fallback does not trigger).
- Lost: report 1 thickness table (10.0, 9.4, 7.6, 6.8, 6.0, 5.7); report 2 table rows CML-01/02
  (6.95, 6.88). Other page segmentation modes and 300 DPI did not help; a quick grid-line
  removal test recovered all of report 2 but not 9.4, 7.6, 5.7 in report 1.
- Only table value not repeated in the findings text: 6.8 mm (report 1, bottom plate), a "should"
  item in `demo/expected.md`. It will not reach the approval note until this is fixed.
- Test: `tests/track_a/test_documents.py::test_demo_report_ocr_keeps_every_severity_and_key_value`
  (slow) checks severities and the key values only.
