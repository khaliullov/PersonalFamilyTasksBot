import asyncio
from datetime import datetime
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock
import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.types import Chat, File, Message, User, Voice

import bot as bot_module


@pytest.fixture
def tmp_db():
    """Provides a fresh temporary SQLite database path."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    yield path
    if os.path.exists(path):
        os.remove(path)


@pytest_asyncio.fixture
async def initialized_db(tmp_db):
    """Initializes the database schema including tasks, chat_history, and voice_messages."""
    await bot_module.init_db(tmp_db)
    return tmp_db


@pytest.fixture
def mock_bot():
    """Provides a mocked aiogram Bot instance."""
    bot = AsyncMock(spec=Bot)
    bot.send_message = AsyncMock(return_value=MagicMock())
    bot.edit_message_caption = AsyncMock(return_value=True)

    fake_file = File(
        file_id="test_file_id_123",
        file_unique_id="unique_123",
        file_path="voice/test_audio.ogg",
        file_size=1024,
    )
    bot.get_file = AsyncMock(return_value=fake_file)

    async def fake_download(file_path, destination):
        with open(destination, "wb") as f:
            f.write(b"OggS_fake_audio_content")
        return destination

    bot.download_file = AsyncMock(side_effect=fake_download)
    return bot


def make_message(
    message_id: int = 1,
    user_id: int = bot_module.WIFE_ID,
    chat_id: int = 12345,
    text: str = None,
    caption: str = None,
    is_voice: bool = False,
    voice_duration: int = 5,
    voice_file_size: int = 1024,
    business_connection_id: str = "biz_conn_1",
) -> Message:
    """Helper to create aiogram Message instances for testing."""
    user = User(id=user_id, is_bot=False, first_name="TestUser")
    chat = Chat(id=chat_id, type="private")
    voice = None
    if is_voice:
        voice = Voice(
            file_id=f"file_id_{message_id}",
            file_unique_id=f"unique_{message_id}",
            duration=voice_duration,
            file_size=voice_file_size,
        )

    return Message(
        message_id=message_id,
        date=datetime.now(),
        chat=chat,
        from_user=user,
        text=text,
        caption=caption,
        voice=voice,
        business_connection_id=business_connection_id,
    )
