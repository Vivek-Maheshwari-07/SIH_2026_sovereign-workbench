# Expected outputs for the demo inputs

What a CORRECT workbench output must contain for each demo input. Use this as the
checklist during rehearsal and in `scripts/e2e_run.py`. Matching rules for all scenarios:
case-insensitive; ignore extra spaces; treat `-`, `–` and a space as the same in tags.

Answer keys (the exact text drawn on the scanned pages) are in `demo/inputs/_source_text/`.

---

## Scenario A: scanned inspection report -> approval note (.docx)

The output is an `ApprovalNote` (see `shared/contracts.py`). "Must" items decide pass/fail;
"should" items are quality checks.

### A1. `demo/inputs/scenario_a_report_1.pdf`: Storage Tank T-104 Annual Inspection

2 pages, image only (no text layer), so it must go through OCR.
Report No. SDR/INSP/2026/0412, date 14-09-2026, inspector M. P. Joshi, Unit 11 Crude Tank Farm.

**Findings: must list all 5 with this severity** (source_page = 1 for all):

| # | Item | Key facts that must appear in the observation | Severity |
|---|---|---|---|
| 1 | Shell course 2, north side | 6.1 mm vs 8.0 mm nominal, 24% loss | **High** |
| 2 | Bottom plate near sump | pitting 1.2 mm; sludge; H2S 18 ppm at manway | **High** |
| 3 | Inlet nozzle N2 / mixer MX-104 | no spade blind; breaker not locked or tagged | **Medium** |
| 4 | Confined space entry permit | no gas test entries after 11:00; attendant left manway | **Medium** |
| 5 | Roof handrail and stairway | surface rust, coating breakdown | **Low** |

**Wall thickness numbers:** 6.8 / 8.0 mm (bottom plate) - only in the thickness table; see docs/known_issues.md

**Cost line: must be** `Rs 4,50,000` (Indian grouping, not "450,000" or "4.5 lakh" only).
Should also carry the split: 3,20,000 + 90,000 + 40,000.

**Recommendation must mention:** replace shell course 2 plates; hot work permit; remove sludge
and H2S precautions; spade blind + lockout/tagout on MX-104.

**SOP references: must cite at least 2 of these files; should cite at least 3:**

| Finding | SOP file in `data/kb/` | Useful pages |
|---|---|---|
| 1 (plate welding) | `nsw_hot_work_petroleum.pdf` | tanks / confined spaces p.65; permit p.32, 53, 73, 101; gas freeing p.66-67 |
| 2 (H2S from sludge) | `osha_h2s_fact_sheet.pdf` | p.2 (100 ppm IDLH, SCBA), p.1 |
| 2 (H2S from sludge) | `osha_h2s_quickcard.pdf` | p.1 |
| 3 (isolation) | `osha_lockout_tagout.pdf` | minimal lockout procedure p.14-15; lockout devices p.8, 17, 27 |
| 4 (entry permit, attendant) | `osha_confined_space.pdf` | attendant duties p.12-13; permit space / written programme p.6, 8 |

Finding 5 has no matching SOP; the output must not invent one.

**Fail if:** any severity is wrong, a finding is invented, the cost is changed, or a
cited SOP file is not in `data/kb/`.

### A2. `demo/inputs/scenario_a_report_2.pdf`: Crude Line 6-P-101 Thickness Survey

2 pages (findings on page 1, recommendation on page 2), image only.
Report No. SDR/INSP/2026/0419, date 18-09-2026, inspector R. T. Menon, Unit 12 Crude Distillation.

**Findings: must list all 5 with this severity** (source_page = 1 for all):

| # | Item | Key facts that must appear in the observation | Severity |
|---|---|---|---|
| 1 | CML-03 elbow at P-101A discharge | 5.20 mm vs 7.11 mm nominal, 27% loss, ~1 year remaining life | **High** |
| 2 | Flange FL-3 near CML-04 | gasket weep; H2S 12 ppm within 1 m; area barricaded | **High** |
| 3 | CML-04 dead leg low point | 5.90 mm, 17% loss | **Medium** |
| 4 | CML-05 at support PS-12 | corrosion under insulation, 6.10 mm, 14% loss | **Medium** |
| 5 | Insulation cladding | ~2 m damaged near PS-12 | **Low** |

**Wall thickness numbers:** should include nominal 7.11 mm, required minimum 4.80 mm, and
corrosion rate 0.38 mm/year at CML-03.

**Cost line: must be** `Rs 2,85,000` (split 1,95,000 + 55,000 + 35,000).

**Recommendation must mention:** replace 3 m spool incl. CML-03 elbow; isolate + lock/tag
P-101A; hot work permit with gas test and fire watch; H2S precautions for FL-3.

**SOP references: must cite at least 2 of these files; should cite at least 3:**

| Finding / action | SOP file in `data/kb/` | Useful pages |
|---|---|---|
| 1 (spool cutting and welding) | `nsw_hot_work_petroleum.pdf` | p.32, 53, 73, 101 (permit); p.66-67 (gas free) |
| 1 (isolation of P-101A) | `osha_lockout_tagout.pdf` | p.14-15, 8, 17 |
| 2 (H2S at flange) | `osha_h2s_fact_sheet.pdf` | p.2 |
| 2 (H2S at flange) | `osha_h2s_quickcard.pdf` | p.1 |

Findings 3-5 (thinning, CUI, cladding) have no matching SOP in the KB. The output must not cite
`osha_confined_space.pdf` for this report as a main reference (no entry is made). Caution: a CUI
query scored 0.544 against unrelated hot-work pages (p.56), which passes the current 0.50 cut-off,
so a hot-work citation attached to finding 4 is a wrong citation.

**Fail if:** same rules as A1.

> Known risk (measured 2026-09-26): Tesseract (default page segmentation) drops table cells on
> these 0.6-1.5 degree rotated scans while reporting ~90 mean confidence, so no vision fallback
> happens. Report 1 OCR lost one "High" and the "Low"; report 2 lost both "High", the "Low"
> and the 6.10 mm reading. Severities missing from the output = this OCR issue, not the LLM.
> Deskewing the page before OCR fixed it in every test.

---

## Scenario B: pipe wall thickness code task

Prompt (example): *"Write a Python function to calculate pipe wall thickness t = P*D / (2*S),
with unit tests, and run it for P = 10 MPa, D = 200 mm, S = 138 MPa."*

**Must:**
- Formula `t = P * D / (2 * S)` (Barlow, no corrosion allowance, no weld factor).
- Printed result for P = 10 MPa, D = 200 mm, S = 138 MPa: **t = 7.246 mm**
  (10 x 200 / 276 = 7.2464 mm; anything that rounds to 7.246 or 7.25 passes).
- Generated tests run in the sandbox and **all pass** (`CodeResult.failed == 0`,
  `passed >= 1`), within `WB_CODE_MAX_ATTEMPTS` (3) attempts.
- Units stated as mm in the output.

**Should:** reject zero or negative S (raise `ValueError`); show the steps.

---

## Scenario C: P&ID -> tag list (.xlsx)

### C1. `demo/inputs/scenario_c_pid_generated.png`

2400x1600 px, generated by `demo/tools/make_pid.py`. 12 tags:

| Tag | Equipment type | Where on the drawing |
|---|---|---|
| T-201 | Tank (crude storage) | left, bottom half |
| LT-201 | Level transmitter (on T-201) | left, near midline |
| XV-201 | Shutdown (on/off) valve, actuated | bottom-left |
| P-201A | Pump | bottom-left |
| P-201B | Pump (standby) | bottom-left |
| PT-202 | Pressure transmitter (pump discharge) | top-left |
| FT-201 | Flow transmitter (discharge header) | top, centre |
| E-201 | Heat exchanger (cooling water) | top-right |
| TT-203 | Temperature transmitter (exchanger outlet) | top-right |
| V-201 | Vessel (separator) | top-right |
| PSV-201 | Pressure safety valve (on V-201, to flare) | top-right |
| LT-202 | Level transmitter (on V-201) | right, near midline |

**Pass: all 12 of 12 tags** read correctly, **and** no invented tags, **and** equipment
type correct for every tag. Duplicates of the same tag from overlapping tiles
must be merged (the .xlsx lists each tag once).

Why 12 of 12 (changed from "at least 10 of 12" on 2026-09-26, approved by both tracks): since the
P&ID fast path (ticket A10: OCR on the whole drawing, vision to confirm), every measured run on this
drawing finds 12/12 with nothing else (see `docs/perf.md`). A missing tag is therefore a regression
to investigate, not normal model variation, so the soak test (`scripts/e2e_run.py`) fails on it.

**Not tags (must not appear as tags):** `DEMO-C-001` (drawing number), `CW`, `UNIT 300`.

**Equipment-type notes:** `XV-201` is a valve; the backend prefix table has no `XV` entry,
so its type comes from the model alone. Accept "Valve", "Shutdown valve", "On/off valve".
`V-201` is a vessel, not a valve.

Measured on 2026-09-26 (qwen3.5:4b, CPU): 12/12 tags read, 0 invented, 249 s.
