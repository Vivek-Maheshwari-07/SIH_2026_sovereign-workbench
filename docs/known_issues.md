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

## Scenario A: open points after the quality fix (Track A, 2026-09-26)

See `docs/perf.md`, section "Scenario A quality fix", for what was fixed and measured.

- **psm 4 OCR drops more of report 2's thickness table.** With `--psm 4` (needed to read the
  findings table row by row) report 2's table keeps only the CML-06 row. CML-01 to CML-05
  (locations, 6.95, 6.88, 5.20, 5.90, 6.10) are lost from the table. 5.20, 5.90 and 6.10 are
  still in the findings text and reach the note; 6.95 and 6.88 do not. Report 1's table loses the
  same numbers as before (see the section above).
- **OCR glue stays in item text.** Item text is taken from the report row (`findings.row_item`).
  OCR joined "dead leg low" into "dead leglow" on report 2 row 3, so that item reads
  "CML-04 dead leglow point". The row parser also assumes the observation column starts with a
  capitalised word and that the item column wraps at most once (true for both demo reports).
  If that does not hold, the model's item is kept.
- **A finding the model leaves out is only warned about, not added.** The code check logs
  "Report finding not in the note: ..." but does not add the row, because OCR does not split item
  and observation reliably enough to build a finding from the row alone. It did not happen in any
  run with full SOP passages. When SOP passages were cut to 900 chars, report 2 dropped
  "Insulation cladding", which is why the passages are not cut.
- **Some KB hits are close to the 0.57 cut-off.** Report 1 "lockout/tagout" 0.577 and report 2
  "isolate... lock and tag" 0.583. bge-m3 scores are sensitive to small text changes: the same H2S
  sentence scores 0.607 with its full stop and 0.569 without, so the query builder keeps the stop.
  `osha_lockout_tagout.pdf` is never the best hit for these reports (hot-work pages that mention
  isolation score higher), so it is not cited even though `demo/expected.md` lists it.
- **The model cites no SOP itself.** At temperature 0 it leaves `sop_references` empty. The
  retrieved passages are cited by the existing fallback and labelled in the Word note as added
  automatically.
- **Agent-mode Scenario A not re-measured.** Agent mode A took 217 s before the fix. The note
  prompt is now ~1,200 tokens longer when the agent passes 3 passages, which adds ~35-45 s. Agent
  mode is not under the 5-minute target, but it should be re-measured before the demo.
- **Warm reruns are faster than the demo will be.** A second run of the same file takes 100-139 s
  because Ollama reuses the processed prompt. A new file at the demo takes ~210-230 s.

## Evaluation pass: general agent tools (2026-09-27)

New tools `answer_question`, `create_document` (Word/PowerPoint), `analyze_table` (CSV/Excel) and
`inspect_image` (photos, handwriting). Measured through the real API in Agent mode on the demo
laptop (models warm), with the prompts of the UI's "Try an example request" buttons:

| Request | Before | After |
|---|---|---|
| "What does our SOP say about H2S exposure limits and what PPE is needed?" | succeeded, but the answer named internal ids (kb_2, kb_4) and no limits | 374 s; states IDLH 100 ppm, SCBA 30 min, air testing, with [n] citations to the OSHA pages |
| "Make a short PowerPoint deck for the shift briefing on H2S safety" | "succeeded" with no file (no PowerPoint tool) | 333 s; 8-slide .pptx, every bullet from the OSHA H2S pages, Sources slide |
| Wall-loss analysis of `equipment_thickness.csv` | failed, AGENT_TIMEOUT after 784 s | 141 s; Excel (Result + Input data) and the analysis script |
| "Read this handwritten shift-round note ..." (`field_note_handwritten.jpg`) | no tool to look at a photo | 199 s; all 8 handwritten lines read correctly |

Open points:
- **CPU speed.** One free-form request takes 2.5-6 minutes; the longest single call is the
  grounded answer or the deck outline (~190 s for ~600 output tokens at ~3 tokens/s). Each agent
  step (choosing the next tool) costs 20-70 s because the 10 tool schemas are part of the prompt.
  A GPU server (the production target) removes most of this.
- **Code quality of the 3B coder on tables.** In the first CSV run the model flagged two items at
  exactly 15.0 % as "above 15 %" (float noise, 15.000000000000002) and printed the first row as the
  "highest loss". The prompt now asks it to round before comparing and to sort the result; the
  Excel writer drops float noise; the demo CSV has no value on the 15 % boundary. The generated
  script is always delivered next to the result so an engineer can check the logic.
- **The agent retells answers.** Small models rewrite a grounded answer in their own words and drop
  its citations. `final_answer` keeps the checked answer when the retelling has no citations (and
  adds the Sources list when the job produced files). Image answers keep the model's wording.
- **Vision reads, it does not measure.** `inspect_image` describes and transcribes what it sees;
  it cannot size a defect or read an unclear gauge reliably. Every image answer says to check the
  original.
- **Agent-mode fallback is narrower on purpose.** A free-form request only falls back to a fixed
  pipeline when the request fits it (see `guided.fits_request`). A request that fits none of them
  and fails ends with the error instead of a pipeline the user did not ask for.
