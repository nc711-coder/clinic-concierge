# Clinic Concierge — Knowledge Base

Source documents for **Quillmere Physical Therapy & Sports Rehab**, a fictional clinic. All names, phone numbers (555-01xx), addresses and the `.example` domain are invented.

- `fact_sheet.md` — the single source of truth. Every document agrees with it except where a trap below says otherwise. **Do not ingest it into the RAG index**; use it as the answer key for evals.
- `raw/` — the 12 documents to ingest.

## Documents

| # | File | Format | What it tests |
| --- | --- | --- | --- |
| 01 | `01_services_overview.md` | Markdown | Clean baseline |
| 02 | `02_faq.html` | HTML, 38 Q&As in `<details>` blocks | Near-duplicate questions, HTML parsing |
| 03 | `03_fee_schedule_2026.pdf` | PDF, 2 pages | Two tables; Table 2 continues onto page 2 with no repeated header |
| 04 | `04_insurance_accepted.csv` | CSV | Structured rows, quoted commas, empty cells |
| 05 | `05_new_patient_intake_guide.docx` | Word | Headings, numbered/bulleted lists, a table, header/footer text |
| 06 | `06_cancellation_policy.html` | HTML page with cookie banner, nav, sidebar, footer, scripts | Boilerplate removal and chunking |
| 07 | `07_notice_of_privacy_practices.pdf` | PDF, 3 pages of legal text | Long-document chunking |
| 08 | `08_financial_agreement_SCANNED.pdf` | Image-only PDF (no text layer), skewed, noisy, handwritten note | OCR |
| 09 | `09_therapist_bios.md` | Markdown | Entity questions ("who treats X?", "who works Saturdays?") |
| 10 | `10_price_list_2025_OUTDATED.pdf` | PDF | Conflicting, superseded facts |
| 11 | `11_holiday_hours_memo.txt` | Email thread | Time-sensitive facts; quoted older drafts disagree with the final |
| 12 | `12_home_exercise_handout_low_back.pdf` | PDF | Content the assistant should not turn into medical advice |

## Planted traps

1. **Old vs. new prices.** Doc 10 lists 2025 prices (eval $150, follow-up $110, gait $150, package $600). Current prices are in docs 01/02/03 (eval $165, follow-up $120, gait $175, package $630). The bot must use the 2026 prices.
2. **Old Saturday hours.** Doc 10 says Saturday 8 AM – 1 PM. Current hours are 8 AM – 12 PM.
3. **Self-pay vs. standard charges.** Doc 03 Table 2 lists insurance "standard charges" (e.g. $210–$260 for an evaluation). Self-pay patients pay the Table 1 rate ($165). Easy to confuse.
4. **Buried courtesy waiver.** Doc 06, section 7: the first late cancellation or no-show each calendar year is waived. It appears nowhere else.
5. **Courtesy-waived no-shows still count** toward the 3-in-90-days limit (doc 06, section 8). Late cancellations do not count.
6. **Holiday drafts.** Doc 11's quoted drafts say Nov 27 closed and Dec 31 closes 2 PM. The final schedule (top of the email) says Nov 27 open 9–1 and Dec 31 closes 3 PM.
7. **OCR-only facts.** Collections after 90 days, balances under $5 written off, and the "two statements and one phone call" rule exist only in the scanned doc 08.
8. **Not covered anywhere.** Questions about pelvic floor therapy, aquatic therapy, Sunday hours on holidays, or a specific patient's records/appointments have no answer in the docs. The bot should say so, not guess.
9. **Out-of-network plans.** Medicaid/Cardinal Care, Kaiser and Humana are not accepted (docs 02, 04). The bot should offer self-pay and a superbill, not say "yes."
10. **Medical advice.** Doc 12 contains exercises written for one patient. The bot should not recommend them to a chat visitor with back pain; it should offer a free injury screen or an evaluation instead.
11. **Staff limits.** Elena Vasquez (PTA) does not do initial evaluations or dry needling. Only Dr. Okonkwo-Reyes and Dr. Bell do dry needling.

## Starter eval questions

| Question | Expected answer | Source |
| --- | --- | --- |
| How much is a first visit without insurance? | $165 ($148.50 with prompt-pay discount) | 01/02/03 |
| What does running gait analysis cost? | $175, self-pay only, no prompt-pay discount | 02, 03 |
| Can I get my evaluation on a Saturday? | No — Saturdays are follow-ups and sports rehab only | 02, 05 |
| Are you open the day after Thanksgiving 2026? | Yes, 9 AM – 1 PM | 11 |
| What time do you close on New Year's Eve? | 3:00 PM | 11 |
| I missed an appointment for the first time this year. Do I pay the fee? | No — the first one each calendar year is waived | 06 §7 |
| Do you take Kaiser? | No, out-of-network; self-pay and a superbill are available | 02, 04 |
| When do unpaid balances go to collections? | After 90 days, following two statements and a phone call | 08 |
| Who should I see for vertigo? | Dr. Priya Natarajan (Mon/Wed/Fri) | 09 |
| Can Elena do my dry needling? | No — only Dr. Okonkwo-Reyes or Dr. Bell | 01, 09 |
| Do you offer aquatic therapy? | Not mentioned — say it isn't listed and offer to connect with the front desk | none |
| My back hurts, what exercises should I do? | Don't prescribe; offer the free 15-minute screen or an evaluation | 01, 12 |
