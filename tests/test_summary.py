import json
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from voice.summary import (
    ALLOWED_CATEGORIES,
    build_voice_summary_prompt,
    extract_and_parse_summary_json,
    format_summary_for_caption,
    generate_voice_summary,
)


def test_build_voice_summary_prompt():
    transcript = "Голова болит со вчерашнего вечера, очень устала."
    prompt = build_voice_summary_prompt(transcript)
    assert transcript in prompt
    assert "health" in prompt
    assert "household" in prompt
    assert "Симптомы" in prompt or "симптомы" in prompt
    assert "НЕ ставь медицинские диагнозы" in prompt


def test_extract_and_parse_summary_json_clean():
    raw = json.dumps({
        "summary": "Жена сообщает о плохом самочувствии.",
        "category": "health",
        "key_points": ["Головная боль со вчерашнего вечера", "Сильная усталость"]
    })
    res = extract_and_parse_summary_json(raw)
    assert res["summary"] == "Жена сообщает о плохом самочувствии."
    assert res["category"] == "health"
    assert len(res["key_points"]) == 2


def test_extract_and_parse_summary_json_with_thinking_and_markdown():
    raw = """
    <think>
    Thinking about the health context of the wife...
    She mentioned headache and fatigue.
    </think>
    ```json
    {
      "summary": "Жена устала и болит голова.",
      "category": "health",
      "key_points": ["Головная боль", "Усталость"]
    }
    ```
    """
    res = extract_and_parse_summary_json(raw)
    assert res["summary"] == "Жена устала и болит голова."
    assert res["category"] == "health"
    assert "Головная боль" in res["key_points"]


def test_extract_and_parse_summary_json_invalid_category_fallback():
    raw = json.dumps({
        "summary": "Купить сыр.",
        "category": "unknown_cat",
        "key_points": ["Сыр"]
    })
    res = extract_and_parse_summary_json(raw)
    assert res["category"] == "other"


def test_extract_and_parse_summary_json_malformed():
    raw = "Извини, я не могу это обработать"
    with pytest.raises(ValueError):
        extract_and_parse_summary_json(raw)


@pytest.mark.asyncio
async def test_generate_voice_summary_success():
    fake_response_data = {
        "response": json.dumps({
            "summary": "Жена просит купить молоко по дороге домой.",
            "category": "shopping",
            "key_points": ["Купить молоко 3.2%"]
        })
    }

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=fake_response_data)

    mock_post_cm = MagicMock()
    mock_post_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_post_cm.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.post.return_value = mock_post_cm

    with patch("aiohttp.ClientSession", return_value=mock_session):
        res = await generate_voice_summary("Купи пожалуйста молоко 3.2% по дороге")
        assert res is not None
        assert res["summary"] == "Жена просит купить молоко по дороге домой."
        assert res["category"] == "shopping"
        assert res["key_points"] == ["Купить молоко 3.2%"]


@pytest.mark.asyncio
async def test_generate_voice_summary_empty_transcript():
    res = await generate_voice_summary("   ")
    assert res is None


@pytest.mark.asyncio
async def test_generate_voice_summary_server_error():
    mock_resp = AsyncMock()
    mock_resp.status = 500
    mock_resp.text = AsyncMock(return_value="Internal Server Error")

    mock_post_cm = MagicMock()
    mock_post_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_post_cm.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.post.return_value = mock_post_cm

    with patch("aiohttp.ClientSession", return_value=mock_session):
        res = await generate_voice_summary("Текст сообщения")
        assert res is None


def test_format_summary_for_caption_within_limit():
    data = {
        "summary": "Нужно записаться к врачу на следующей неделе.",
        "category": "health",
        "key_points": ["Запись к врачу", "Следующая неделя"]
    }
    caption = format_summary_for_caption(data, max_len=1024)
    assert "Резюме [Здоровье]" in caption
    assert "Нужно записаться к врачу" in caption
    assert "• Запись к врачу" in caption
    assert len(caption) <= 1024


def test_format_summary_for_caption_exceeding_limit():
    long_text = "Очень длинный текст. " * 80
    data = {
        "summary": long_text,
        "category": "household",
        "key_points": ["Пункт " + str(i) for i in range(50)]
    }
    caption = format_summary_for_caption(data, max_len=100)
    assert len(caption) <= 100
    assert "..." in caption
