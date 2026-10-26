from ingest.chunker import MAX_CHARS, chunk_document
from ingest.models import ParsedDocument, Section

HEADER_ALLOWANCE = 250   # "Document: ... / Section: ..." prefix on top of the chunk body


def test_chunks_respect_size_and_carry_context(parsed):
    for name in ["06_cancellation_policy.html", "07_notice_of_privacy_practices.pdf", "03_fee_schedule_2026.pdf"]:
        doc = parsed(name)
        for c in chunk_document(doc):
            assert len(c.content) <= MAX_CHARS, (name, c.chunk_index)
            assert len(c.embed_text) <= MAX_CHARS + HEADER_ALLOWANCE
            assert c.embed_text.startswith(f"Document: {doc.title}")


def test_policy_fee_chunk_knows_its_section(parsed):
    chunks = chunk_document(parsed("06_cancellation_policy.html"))
    c = next(c for c in chunks if "courtesy waiver" in c.content)
    assert "Section: 7. Waivers and Exceptions" in c.embed_text


def test_metadata_flags_from_manifest(parsed):
    old = chunk_document(parsed("10_price_list_2025_OUTDATED.pdf"))
    assert old and all(c.metadata["status"] == "superseded" for c in old)
    assert all(c.metadata["superseded_by"] == "03_fee_schedule_2026.pdf" for c in old)
    handout = chunk_document(parsed("12_home_exercise_handout_low_back.pdf"))
    assert all(c.metadata["restricted"] for c in handout)


def test_table_chunks_split_by_rows_not_mid_row(parsed):
    for c in chunk_document(parsed("03_fee_schedule_2026.pdf")):
        if c.metadata["kind"] == "table":
            assert all(line.startswith(("Service:", "Code:")) for line in c.content.split("\n"))


def test_long_prose_split_with_overlap():
    sentences = [f"Sentence number {i} explains a detail of the policy in a few words." for i in range(60)]
    doc = ParsedDocument(doc_id="t", source="t.md", title="T", parser="markdown",
                         sections=[Section(text=" ".join(sentences), heading_path=["Long"])])
    chunks = chunk_document(doc)
    assert len(chunks) > 1
    assert all(len(c.content) <= MAX_CHARS for c in chunks)
    last_of_first = chunks[0].content.split(". ")[-1]
    assert last_of_first.rstrip(".") in chunks[1].content     # overlap carried forward
    assert [c.metadata["part"] for c in chunks][0] == f"1/{len(chunks)}"


def test_hashes_are_stable(parsed):
    a = [c.content_hash for c in chunk_document(parsed("02_faq.html"))]
    b = [c.content_hash for c in chunk_document(parsed("02_faq.html"))]
    assert a == b and len(set(a)) == len(a)


def test_numbered_clauses_get_their_own_chunks(parsed):
    chunks = chunk_document(parsed("08_financial_agreement_SCANNED.pdf"))
    collections = [c for c in chunks if "collection agency" in c.content]
    assert len(collections) == 1
    c = collections[0].content
    assert "90 days" in c and "deductibles" not in c and "$5.00" not in c   # one clause, not five


def test_short_numbered_steps_split_but_keep_section(parsed):
    chunks = chunk_document(parsed("05_new_patient_intake_guide.docx"))
    steps = [c for c in chunks if "Section: Welcome to Quillmere! Your New Patient Guide > Before Your First Visit" in c.embed_text]
    assert len(steps) == 5 and steps[0].content.startswith("1. ")
