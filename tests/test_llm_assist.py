from app.llm_assist import LlmAssistClient, build_llm_assist_client, validate_post_edit_candidate


class _FakeClient(LlmAssistClient):
    def __init__(self, responses):
        super().__init__(base_url="http://x", model="m")
        self._responses = list(responses)

    def _chat(self, messages, max_tokens=800):
        return self._responses.pop(0)


def test_post_edit_block_parses_json_and_fallback():
    c = _FakeClient(['{"text":"Texto revisado"}', 'not-json'])
    out1 = c.post_edit_block("Source", "Texto original")
    out2 = c.post_edit_block("Source", "Texto original")
    assert out1 == "Texto revisado"
    assert out2 == "Texto original"


def test_build_llm_assist_client_respects_enabled_flag():
    cfg_off = {"llm_assist": {"enabled": False}}
    cfg_on = {"llm_assist": {"enabled": True, "base_url": "http://127.0.0.1:1234/v1", "model": "ministral-8b"}}
    assert build_llm_assist_client(cfg_off) is None
    assert build_llm_assist_client(cfg_on) is not None


def test_validate_post_edit_candidate_guards():
    ok1, reasons1 = validate_post_edit_candidate("Torque 12 N·m", "Torque 12 N·m")
    ok2, reasons2 = validate_post_edit_candidate("Torque 12 N·m", "Torque 14 N·m")
    ok3, reasons3 = validate_post_edit_candidate("See Fig. 3-2", "See Fig. 3-3")

    assert ok1 is True
    assert reasons1 == []
    assert ok2 is False and "numbers_units_changed" in reasons2
    assert ok3 is False and "references_changed" in reasons3
