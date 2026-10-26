"""Each test pins down one thing the messy documents could get wrong."""


def all_text(doc):
    return "\n".join(s.text for s in doc.sections)


def test_markdown_keeps_headings(parsed):
    doc = parsed("01_services_overview.md")
    paths = [" > ".join(s.heading_path) for s in doc.sections]
    assert "Our Services > Dry Needling" in paths
    assert "**" not in all_text(doc)


def test_faq_one_section_per_question(parsed):
    doc = parsed("02_faq.html")
    faqs = [s for s in doc.sections if s.kind == "faq"]
    assert len(faqs) == 38
    assert all(s.text.startswith("Q: ") and "\nA: " in s.text for s in faqs)
    kaiser = next(s for s in faqs if "Kaiser" in s.text)
    assert kaiser.heading_path[-2] == "Insurance & Billing"


def test_policy_page_boilerplate_removed(parsed):
    text = all_text(parsed("06_cancellation_policy.html"))
    for junk in ["We use cookies", "From the Blog", "Subscribe", "Website by", "Skip to content", "Patient Portal Login"]:
        assert junk not in text, junk
    assert "one courtesy waiver per calendar year" in text


def test_buried_waiver_keeps_its_section(parsed):
    doc = parsed("06_cancellation_policy.html")
    sec = next(s for s in doc.sections if "courtesy waiver" in s.text)
    assert sec.heading_path[-1] == "7. Waivers and Exceptions"


def test_csv_one_row_per_section(parsed):
    doc = parsed("04_insurance_accepted.csv")
    assert len(doc.sections) == 12
    kaiser = next(s for s in doc.sections if "Kaiser" in s.text)
    assert "Network status: Out-of-network" in kaiser.text


def test_docx_lists_and_table(parsed):
    doc = parsed("05_new_patient_intake_guide.docx")
    table = next(s for s in doc.sections if s.kind == "table")
    assert table.table_header == ["Bring", "Why"]
    assert any("Bring: Photo ID" in r for r in table.text.split("\n"))
    steps = next(s for s in doc.sections if s.heading_path[-1] == "Before Your First Visit")
    assert steps.text.startswith("1. Book your 60-minute")


def test_email_keeps_only_final_message(parsed):
    doc = parsed("11_holiday_hours_memo.txt")
    text = all_text(doc)
    assert "Fri Nov 27 ........ OPEN 9:00 AM - 1:00 PM" in text
    assert "DRAFT" not in text and "close 2pm" not in text      # the superseded drafts are gone
    assert "Tom Albright | Front Desk" not in text              # signature stripped
    assert any("dropped 2 quoted" in w for w in doc.warnings)


def test_pdf_table_joined_across_page_break(parsed):
    doc = parsed("03_fee_schedule_2026.pdf")
    tables = [s for s in doc.sections if s.kind == "table"]
    assert len(tables) == 2                                    # Table 1 + Table 2 (one table despite the page break)
    t2 = tables[1]
    assert (t2.page_start, t2.page_end) == (1, 2)
    assert t2.table_header[0] == "Code"
    assert "Code: 99070" in t2.text                            # last row, on page 2, still has the column name
    assert "Table 2" in t2.heading_path[-1]


def test_pdf_letterhead_and_page_numbers_dropped(parsed):
    text = all_text(parsed("07_notice_of_privacy_practices.pdf"))
    assert "QUILLMERE PHYSICAL THERAPY & SPORTS REHAB" not in text
    assert "Page 2" not in text


def test_scanned_pdf_is_ocrd(parsed):
    doc = parsed("08_financial_agreement_SCANNED.pdf")
    assert all(s.ocr for s in doc.sections)
    text = all_text(doc)
    assert "90 days" in text                                    # collections rule exists only in this scan
    assert "$5.00" in text
    assert any("OCR used" in w for w in doc.warnings)


def test_empty_form_table_skipped(parsed):
    doc = parsed("12_home_exercise_handout_low_back.pdf")
    assert not any(s.kind == "table" for s in doc.sections)
