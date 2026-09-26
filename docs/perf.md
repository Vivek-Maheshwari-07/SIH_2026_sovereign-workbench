# Performance (ticket A10)

Goal: every guided scenario under 5 minutes on the demo laptop, same accuracy.
Result: **met**. Scenario C went from 372 s to 72 s cold with 12/12 tags. After the Scenario A
quality fix (section below), the worst measured guided run is Scenario A cold at 231 s. That run now
cites SOPs, keeps the report's severities and shows the cost.

Measured 2026-09-26 through the real API (upload -> POST /api/tasks -> poll events).
The document-extraction cache (`WB_CACHE_DIR`) was emptied before every run, so every run
did its real OCR / vision work. **Cold** = all models unloaded with `ollama stop` first.
**Warm** = the same scenario run again straight after (model in RAM).

## Laptop and models

| Item | Value |
|---|---|
| CPU | 12th Gen Intel Core i5-1235U, 10 cores / 12 threads, no GPU used |
| RAM | 16 GB (15.7 GB usable) |
| OS | Windows 11 Home 10.0.26200 |
| Ollama | 0.34.4, `OLLAMA_MAX_LOADED_MODELS` not set (Ollama default, 3 on CPU) |
| general (document, vision, agent) | `qwen3.5:4b`, num_ctx 8192, think off |
| coder | `qwen2.5-coder:3b`, num_ctx 8192 |
| embed | `bge-m3` |
| OCR | Tesseract 5, `WB_OCR_DPI` 200 |

Model load time (Ollama `load_duration`, from unloaded): qwen3.5:4b 8.8 s, qwen2.5-coder:3b 4.8 s,
bge-m3 3.3 s. With all three in RAM Ollama uses ~6.7 GB.

## Before vs after (guided mode)

| Scenario | Before cold | Before warm | After cold | After warm | After, with prewarm* |
|---|---|---|---|---|---|
| A inspection note | 170 s | 106 s | 158 s | 100 s | 130 s |
| A after the quality fix (report 1 / report 2) | | | 231 / 225 s | 108-110 / 100-139 s | 214 / 210 s |
| B code calc | 77 s | 51 s | 96 s | 54 s | 70 s |
| C P&ID tags | **372 s** | 157 s | **72 s** | 14 s** | 64 s |

\* "With prewarm": models unloaded, then `POST /api/admin/prewarm` (21 s), then A, B, C run once
each in that order, like the demo. First run of each input, so no Ollama prompt cache.

\** Warm reruns of the *same file* are much faster than a first run because Ollama reuses its
prompt cache (the document text or image tokens are already processed). A and B warm runs benefit
the same way. For a new file at the demo, expect the "with prewarm" column.

A and B code paths did not change except `keep_alive` (see below). Their before/after
differences are run-to-run variation of the LLM: B's single coder call took 50-87 s across runs
for the same request.

### Agent mode (after, one run each, models loaded)

| Scenario | Time | Result |
|---|---|---|
| A inspection note | 217 s | succeeded, approval note .docx (measured before the Scenario A quality fix; not re-measured, see docs/known_issues.md) |
| B code calc | 194 s | succeeded, 3 tests passed, t = 7.246 mm |
| C P&ID tags | 155 s | succeeded, 12/12 tags (fast path) |

Agent mode adds a plan call (25-42 s) and one `agent_step` call per tool (17-78 s each) on top
of the guided work.

## Step breakdown (before, guided)

From the task events (`tool_result.duration_ms`, `llm_call.duration_ms`) and the audit log
(vision tile calls inside `read_document`).

| Scenario | Step | Cold s | Cold % | Warm s | Warm % |
|---|---|---|---|---|---|
| A | read_document (OCR, 2 pages, deskew) | 5.5 | 3 | 4.6 | 4 |
| A | search_knowledge (bge-m3, incl. load when cold) | 6.6 | 4 | 0.6 | 1 |
| A | draft_approval_note LLM (qwen3.5:4b, 551 tokens out) | 157.6 | 93 | 100.0 | 95 |
| A | Word render + finish | 0.4 | 0 | 0.4 | 0 |
| B | coder LLM "write code" (436 tokens out) | 71.4 | 92 | 49.9 | 98 |
| B | sandbox runs (2) | 0.7 | 1 | 0.6 | 1 |
| B | code-flow overhead (container start etc.) | 5.3 | 7 | 0.6 | 1 |
| C | read_document: 4 vision tiles (61 + 59 + 57 + 54 s) | 232.5 | 63 | 21.4 | 14 |
| C | extract_pid_tags JSON LLM (791 tokens out) | 139.0 | 37 | 135.6 | 86 |
| C | Excel render + finish | 0.5 | 0 | 0.4 | 0 |

## Step breakdown (after, guided, cold)

| Scenario | Step | Time s | % |
|---|---|---|---|
| A | read_document | 4.2 | 3 |
| A | search_knowledge | 4.0 | 3 |
| A | draft_approval_note LLM (incl. 8.8 s model load) | 149.0 | 95 |
| B | coder LLM (incl. 4.8 s model load) | 87.4 | 91 |
| B | sandbox + overhead | 8.2 | 9 |
| C | read_document: deskew + OCR 4 tiles | ~1.5 | 2 |
| C | read_document: ONE vision check, whole drawing (incl. 8.8 s load) | 70.4 | 98 |
| C | extract_pid_tags (no LLM any more) + Excel | 0.1 | 0 |

## Scenario C: what changed

Path used on `demo/inputs/scenario_c_pid_generated.png`: **fast path** in every run
(guided cold, warm, with prewarm, agent mode, and the slow test).

1. Deskew the drawing once, cut it into the same 4 overlapping tiles.
2. Tesseract each tile with `--psm 11` (sparse text) and a whitelist `A-Z 0-9 -`.
3. Keep tag candidates matching `PID_TAG_RE`: 1-4 letters, dash, 2-4 digits, optional letter
   suffix, not part of a longer code. So `DEMO-C-001`, `UNIT 300`, `CW`, `FT` and `6-P-101`
   are never tags.
4. Type each tag from the existing prefix table (`TAG_TYPE_RULES`; unknown prefix -> "Other").
   No LLM call is needed to build the tag list.
5. ONE vision call on the whole drawing (resized to `WB_VISION_MAX_PX`) to confirm the tags
   and add any that OCR missed. A tag only the vision model read is added with no tile and the
   description "Vision only (OCR did not find it); check on the drawing". Echoes of the prompt's
   example tags and suffix-less forms of an OCR tag (`P-201` when OCR read `P-201A`) are ignored
   with a warning.
6. Fallback: if OCR finds fewer than `PID_FAST_MIN_TAGS` (3) tags, the old path runs
   unchanged: 4 vision tiles + the JSON tag-list LLM call.

The path used is emitted as a `log` event ("P&ID read path") that the UI timeline shows, and it is
written into the Excel Summary sheet (Notes). Each tag's description says how it was found
("OCR, confirmed by vision", "OCR only (vision did not read it)", or "Vision only ...").

**Confirm step: whole drawing vs one tile** (measured cold, including the 8.8 s load):

| Option | Time | Tags the vision model read |
|---|---|---|
| Whole drawing, 1024x683 | 72.7 s, 68.2 s | 12/12, nothing else |
| Tile with fewest OCR tags (top-left) | 58.1 s | 2 (LT-201, PT-202) + stray "FT" |
| Tile bottom-right | 60.1 s | 3 |

On CPU the image cost is the same for any 1024 px image, so the whole drawing costs about the
same as one tile but checks every tag. **Whole drawing chosen.**

**Accuracy, demo P&ID (12 tags):**

| | Before (4 vision tiles + LLM) | After (fast path) |
|---|---|---|
| Real tags found | 12/12 | 12/12 (OCR alone: 12/12 in ~1.5 s; vision confirmed 12/12) |
| Non-tags listed | 1 (`FT`, from an instrument bubble) | 0 |
| Title-block text as tags | 0 | 0 |
| Wrong types fixed by the prefix table | 3 per run (V-201, PSV-201, T-201) | 0 needed |

## Prewarm (`POST /api/admin/prewarm`)

Loads the models with an empty request each (same `num_ctx` as real calls, so no reload
later), then warms our own caches. Measured: **21.2 s total**.

| Item | Time |
|---|---|
| model coder (qwen2.5-coder:3b) | 4.8 s |
| model embed (bge-m3) | 3.5 s |
| router example embeddings | 2.1 s |
| KB (Chroma collection + index, one search) | 0.3 s |
| sandbox image `wb-sandbox:1.0` present | 0.1 s |
| model general (qwen3.5:4b) | 10.1 s |

Per-item times are in the `warmed` / `failed` labels (e.g. `"model general (qwen3.5:4b) (10062 ms)"`),
in the `X-Prewarm-Items` header (JSON) and in an audit record (kind `system`, name `prewarm`).
The models left loaded are in `X-Prewarm-Loaded-Models`. A failed item goes into `failed` with
its error; the endpoint never fails.

**Which models stay loaded.** Load order is coder, embed, general. With
`OLLAMA_MAX_LOADED_MODELS=2` Ollama evicts the least recently used model, so general + embed
stay loaded and coder is dropped. In the demo order A -> B -> C this means **one swap**: Scenario B
reloads the coder (~5 s) and evicts bge-m3; C then finds general still loaded. Only a later
Scenario A would reload bge-m3 (~3.5 s). On this laptop the variable is not set, so Ollama's
default applies and all three stayed loaded (checked with `ollama ps`: no swap at all).

## Other tuning checked

| Item | Measurement | Decision |
|---|---|---|
| Scenario A prompt size | A10: 1,058 prompt tokens (no SOP passages), 37.5 s to process (0.3 s when cached); generating ~500 tokens at 5.4 tok/s takes 85-90 s. After the quality fix: 2,250-2,530 tokens with 3 SOP passages, 84-102 s to process | Not trimmed. Cutting each SOP passage to 900 chars saved 30-40 s but report 2's note then lost the Insulation cladding finding, so full passages are kept. |
| `WB_NUM_CTX` 8192 vs 4096 | Same speed (5.4-5.6 tok/s); switching num_ctx forces a 7.8 s reload | Kept 8192 (A with SOP passages needs the room). |
| `keep_alive` | Ollama default is 5 min, so a prewarm done more than 5 min before the demo was lost | **Changed**: all calls send `keep_alive="30m"` (`llm_client.OLLAMA_KEEP_ALIVE`). Saves a reload (up to ~10 s per model) after idle gaps. Replaying Scenario A's exact prompt 3x with and 3x without it gave the same kind of results. |

## Scenario A quality fix (after A10)

A10 left three Scenario A problems: 0 SOPs cited, unstable severities, and the cost always
replaced by "To be filled by the originator." All three are fixed. Measured 2026-09-26 on
`demo/inputs/scenario_a_report_1.pdf` and `scenario_a_report_2.pdf`, guided mode.

### What changed

| Change | Why |
|---|---|
| Page OCR uses Tesseract `--psm 4` and keeps line breaks (`documents.OCR_PAGE_CONFIG`) | The default psm 3 read report 1's findings table column by column (all items, then all observations, then "High High Medium Medium Low"), and all words were joined into one line. psm 4 reads each table row as "item  observation  severity". Same speed (~4 s per page). Cache version 4. |
| Findings table parser (`backend/tools/findings.py`) | A row starts on a line that ends with a severity word; misread row numbers ("+14\"", "oS") are dropped. Both reports: 5/5 rows with the right severity. |
| KB search: one query per finding row + one per recommendation sentence, hits merged, only >= `MIN_KB_SCORE` 0.57 (unchanged), up to 3 passages, one per SOP file first | The finding descriptions alone score 0.45-0.60 (report 2: none above 0.565). The recommended actions ("hot work permit with gas test", "H2S monitoring and breathing apparatus") score 0.58-0.74. The queries and best scores are shown as a log event. |
| Prompt: one finding per table row, severity copied exactly; draft call at temperature 0, seed 42 (`NOTE_TEMPERATURE`, `NOTE_SEED`) | The same input now gives the same note. |
| Code check after the model answers (`agent_tools.check_against_report`) | Each finding is matched to its report row (60% of item words). If the severity differs, the report's is used and a warning is logged. The item text is replaced by the row's item column (first line up to the observation, plus at most 2 words from the wrapped line, cleaned), and the model's observation is kept. Report rows missing from the note are warned about. |
| Cost check joins OCR spaces inside numbers (`office.join_number_spaces`, digit on both sides) in both texts | Report 1 page 2 OCRs as "Rs 90, 000" and "Rs 40 ,000". The model's "Rs 90,000" was then "not in the source" and the whole cost line was blanked. Invented amounts are still replaced. |
| Draft call timeout 420 s (`NOTE_TIMEOUT_S`, never below `WB_LLM_TIMEOUT_S`) | With 3 SOP passages a first (uncached) draft takes 203-215 s, over `WB_LLM_TIMEOUT_S` 180 s. The call timed out, and the retry finished in ~105 s from Ollama's prompt cache. That wasted 180 s per run: cold runs took 296-300 s. With the longer timeout: 225-231 s. Other LLM calls keep 180 s. |

### Timing (guided, Scenario A)

| | Report 1 | Report 2 |
|---|---|---|
| Cold (models unloaded, new file) | 231 s | 225 s |
| After prewarm (new file) | 214 s | 210 s |
| Warm, same file again (Ollama prompt cache) | 108-110 s | 100-139 s |
| Cold, before the 420 s timeout (first attempt timed out) | 300 s | 296 s |

Step breakdown, cold, report 1: read_document 6.2 s (3%), search_knowledge 11 queries incl.
bge-m3 load 10.8 s (5%), draft_approval_note LLM 213.8 s (92%; 12.6 s load, ~2,250 prompt tokens
in ~85 s, ~595 tokens out in ~110 s), Word 0.5 s.

### 3 runs per report (slow test, starting with no models loaded)

`tests/track_a/test_scenario_a_quality.py::test_live_scenario_a_is_correct_and_stable`

| Run | Report 1 | Report 2 |
|---|---|---|
| 1 | 219 s | 194 s |
| 2 | 110 s | 100 s |
| 3 | 108 s | 139 s |

All 6 runs were identical in content. The item text below is what the Word table shows after the
item fix (items taken from the report row). That fix was checked with one more live run per report
(120 s and 113 s, same findings, severities, cost and SOPs):

| Report | Findings (item: severity, all = report) | Cost | SOPs cited |
|---|---|---|---|
| 1 | Shell course 2, north side: High; Bottom plate near sump: High; Inlet nozzle N2 and mixer MX-104: Medium; Confined space entry permit: Medium; Roof handrail and stairway: Low | Rs 4,50,000 | nsw_hot_work_petroleum.pdf p.81; osha_confined_space.pdf p.12; osha_h2s_quickcard.pdf p.1 |
| 2 | CML-03 elbow at P-101A discharge: High; Flange FL-3 near CML-04: High; CML-04 dead leglow point (OCR glue): Medium; CML-05 at support PS-12: Medium; Insulation cladding: Low | Rs 2,85,000 | nsw_hot_work_petroleum.pdf p.73 and p.27; osha_h2s_fact_sheet.pdf p.2 |

The model itself cites none of the passages. The existing fallback cites the retrieved ones and
the Word note labels them as added automatically. Report 2 never cites osha_confined_space.pdf
(no entry is made).

### KB queries that pass 0.57

| Report | Query (shortened) | Best hit | Score |
|---|---|---|---|
| 1 | Finding: Confined space entry, no gas test entries... | osha_confined_space.pdf p.12 | 0.595 |
| 1 | Welding must be done under a hot work permit... | nsw_hot_work_petroleum.pdf p.81 | 0.704 |
| 1 | Remove all sludge... H2S monitoring and breathing apparatus | osha_h2s_quickcard.pdf p.1 | 0.651 |
| 1 | Fit a spade blind... apply lockout/tagout... | nsw_hot_work_petroleum.pdf p.40 | 0.577 |
| 1 | Brief all entrants... confined space entry procedure. | osha_confined_space.pdf p.12 | 0.685 |
| 2 | Before cutting, isolate the line, lock and tag... | nsw_hot_work_petroleum.pdf p.101 | 0.583 |
| 2 | Cutting and welding only under a hot work permit... | nsw_hot_work_petroleum.pdf p.73 | 0.735 |
| 2 | Work on FL-3 needs H2S monitoring and breathing apparatus. | osha_h2s_fact_sheet.pdf p.2 | 0.607 |

All other queries (the other finding rows and recommendation sentences) score 0.44-0.57 and are
dropped. Open points are in `docs/known_issues.md`.
