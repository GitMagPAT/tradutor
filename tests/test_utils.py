from app.utils import clean_extracted_text


def test_clean_extracted_text_hyphenation():
    # "exam-\nple" => "example"
    inp = "This is an exam-\nple of hyphenation."
    out = clean_extracted_text(inp)
    assert out == "This is an example of hyphenation."


def test_clean_extracted_text_whitespace():
    inp = "Hello\r\n\n  world\t!"
    out = clean_extracted_text(inp)
    assert out == "Hello world!"
