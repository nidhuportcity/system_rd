from types import SimpleNamespace

from app.config import Settings
from app.rag import RAGService
from app.schemas import SearchResult


def test_answer_uses_ollama_context_and_citations(monkeypatch):
    import ollama

    class FakeClient:
        def __init__(self, host, timeout):
            assert host == "http://localhost:11434"
            assert timeout == 300

        def chat(self, **kwargs):
            assert kwargs["model"] == "qwen3:8b"
            assert kwargs["think"] is False
            assert kwargs["options"]["num_predict"] == 800
            prompt = kwargs["messages"][1]["content"]
            assert "[1] manual.pdf\nPress reset." in prompt
            assert "Question: How to reset?" in prompt
            return SimpleNamespace(message=SimpleNamespace(content="Press reset. [1]"))

    monkeypatch.setattr(ollama, "Client", FakeClient)
    service = RAGService(Settings(llm_model="qwen3:8b", ollama_host="http://localhost:11434"))
    result = service.answer("How to reset?", [SearchResult(
        chunk_id="chunk-1", document_id="doc-1", filename="manual.pdf",
        text="Press reset.", score=1.0, chunk_index=0,
    )])
    assert result.answer == "Press reset. [1]"
    assert result.citations[0].document_id == "doc-1"


def test_answer_without_results_does_not_call_ollama(monkeypatch):
    import ollama

    def unexpected_client(**kwargs):
        raise AssertionError("No model call should be made without context")

    monkeypatch.setattr(ollama, "Client", unexpected_client)
    result = RAGService(Settings()).answer("Unknown?", [])
    assert result.citations == []
    assert "could not find" in result.answer
