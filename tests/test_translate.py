import tempfile
from pathlib import Path

from app.cache import TranslationCache
from app.translate import DummyTranslator, translate_many_with_cache, translate_with_cache


def test_translate_with_cache_dummy_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        cache = TranslationCache(Path(td) / "cache.sqlite", memory_max_entries=100, commit_every=1)
        tr = DummyTranslator()

        out1 = translate_with_cache(
            cache=cache,
            translator=tr,
            text="Value is 10 kg",
            source_lang="en",
            target_lang="pt",
            max_chars_per_request=1000,
            provider_id="dummy",
            glossary={},
        )
        assert out1 == "Value is 10 kg"

        # deve vir do cache (não quebra)
        out2 = translate_with_cache(
            cache=cache,
            translator=tr,
            text="Value is 10 kg",
            source_lang="en",
            target_lang="pt",
            max_chars_per_request=1000,
            provider_id="dummy",
            glossary={},
        )
        assert out2 == "Value is 10 kg"
        cache.close()


def test_translate_many_with_cache_batching_dummy():
    with tempfile.TemporaryDirectory() as td:
        cache = TranslationCache(Path(td) / "cache.sqlite", memory_max_entries=100, commit_every=1)
        tr = DummyTranslator()

        texts = ["Hello world", "Hello world", "Number 123", ""]
        outs = translate_many_with_cache(
            cache=cache,
            translator=tr,
            texts=texts,
            source_lang="en",
            target_lang="pt",
            max_chars_per_request=200,
            provider_id="dummy",
            glossary={"world": "mundo"},
            batch_mode="on",
        )

        # DummyTranslator não traduz de verdade, mas o glossário força substituição pós-process
        assert outs[0] == "Hello mundo"
        assert outs[1] == "Hello mundo"
        assert outs[2] == "Number 123"
        assert outs[3] == ""
        cache.close()

def test_placeholder_tokens_roundtrip_and_glossary():
    from app.translate import protect_entities, protect_glossary_terms, restore_placeholders

    original = "Figure 9-59 uses API_KEY and 10 kg."
    protected, ent_map = protect_entities(original, token_prefix="b001_s001")
    assert "ZXQENT" in protected

    restored = restore_placeholders(protected, ent_map)
    assert restored == original

    protected2, gloss_map = protect_glossary_terms("Hello world", {"world": "mundo"}, token_prefix="b001_s001")
    # sem tradutor, o texto ainda terá token; ao restaurar, vira "mundo"
    out2 = restore_placeholders(protected2, gloss_map)
    assert out2 == "Hello mundo"


def test_postprocess_translation_fixes_punctuation_spacing() -> None:
    """Regressão: não pode inserir texto literal "\\1"/"\\2" no output."""

    from app.translate import postprocess_translation

    # Espaço antes de pontuação + ausência de espaço após pontuação
    s = "Hello ,world!This is a test ."
    out = postprocess_translation(s)
    assert out == "Hello, world! This is a test."

