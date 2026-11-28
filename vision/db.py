"""Database persistence adapter for Vision image metadata (delegates to database.py)."""

from typing import Any, Dict, List, Optional, Tuple

import database


async def get_vision_image(
    chat_id: int,
    telegram_message_id: int,
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    return await database.get_vision_image(chat_id, telegram_message_id, db_path=db_path)


async def get_vision_image_by_id(
    image_id: int,
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    return await database.get_vision_image_by_id(image_id, db_path=db_path)


async def create_vision_image(
    chat_id: int,
    telegram_message_id: int,
    sender_id: int,
    telegram_file_id: Optional[str] = None,
    telegram_file_unique_id: Optional[str] = None,
    media_group_id: Optional[str] = None,
    business_connection_id: Optional[str] = None,
    caption: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Tuple[int, bool]:
    return await database.create_vision_image(
        chat_id=chat_id,
        telegram_message_id=telegram_message_id,
        sender_id=sender_id,
        telegram_file_id=telegram_file_id,
        telegram_file_unique_id=telegram_file_unique_id,
        media_group_id=media_group_id,
        business_connection_id=business_connection_id,
        caption=caption,
        db_path=db_path,
    )


async def update_vision_image(
    chat_id: int,
    telegram_message_id: int,
    *,
    local_file_path: Optional[str] = None,
    clear_local_file_path: bool = False,
    original_filename: Optional[str] = None,
    mime_type: Optional[str] = None,
    file_size: Optional[int] = None,
    sha256_hash: Optional[str] = None,
    vision_status: Optional[str] = None,
    analysis_json: Optional[str] = None,
    image_description: Optional[str] = None,
    short_summary: Optional[str] = None,
    ocr_text: Optional[str] = None,
    category: Optional[str] = None,
    confidence: Optional[float] = None,
    task_id: Optional[int] = None,
    trello_card_id: Optional[str] = None,
    trello_attachment_id: Optional[str] = None,
    attachment_status: Optional[str] = None,
    retry_count: Optional[int] = None,
    last_error: Optional[str] = None,
    clear_last_error: bool = False,
    context_saved: Optional[int] = None,
    clarification_sent: Optional[int] = None,
    db_path: Optional[str] = None,
) -> bool:
    return await database.update_vision_image(
        chat_id=chat_id,
        telegram_message_id=telegram_message_id,
        local_file_path=local_file_path,
        clear_local_file_path=clear_local_file_path,
        original_filename=original_filename,
        mime_type=mime_type,
        file_size=file_size,
        sha256_hash=sha256_hash,
        vision_status=vision_status,
        analysis_json=analysis_json,
        image_description=image_description,
        short_summary=short_summary,
        ocr_text=ocr_text,
        category=category,
        confidence=confidence,
        task_id=task_id,
        trello_card_id=trello_card_id,
        trello_attachment_id=trello_attachment_id,
        attachment_status=attachment_status,
        retry_count=retry_count,
        last_error=last_error,
        clear_last_error=clear_last_error,
        context_saved=context_saved,
        clarification_sent=clarification_sent,
        db_path=db_path,
    )


async def get_unattached_vision_images_for_messages(
    message_ids: List[int],
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    return await database.get_unattached_vision_images_for_messages(message_ids, db_path=db_path)


async def get_vision_images_by_media_group(
    chat_id: int,
    media_group_id: str,
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    return await database.get_vision_images_by_media_group(chat_id, media_group_id, db_path=db_path)


async def get_vision_images_for_task(
    task_id: int,
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    return await database.get_vision_images_for_task(task_id, db_path=db_path)


async def has_duplicate_trello_attachment(
    trello_card_id: str,
    sha256_hash: Optional[str] = None,
    telegram_file_unique_id: Optional[str] = None,
    db_path: Optional[str] = None,
) -> bool:
    return await database.has_duplicate_trello_attachment(
        trello_card_id=trello_card_id,
        sha256_hash=sha256_hash,
        telegram_file_unique_id=telegram_file_unique_id,
        db_path=db_path,
    )


async def get_pending_trello_attachments(
    max_retries: int = 3,
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    return await database.get_pending_trello_attachments(max_retries=max_retries, db_path=db_path)


async def get_interrupted_vision_images(
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    return await database.get_interrupted_vision_images(db_path=db_path)
