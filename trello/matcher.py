import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("FamilyTasksBot.TrelloMatcher")


def normalize_column_name(text: str) -> str:
    """
    Нормализует название колонки:
    - Приводит к нижнему регистру с учетом Unicode (casefold).
    - Удаляет эмодзи, служебные символы и знаки препинания, оставляя буквенно-цифровые слова.
    - Объединяет слова одним пробелом.
    Примеры:
      '📥 Входящие задачи' -> 'входящие задачи'
      'Done / Завершено!' -> 'done завершено'
      '  To Do  ' -> 'to do'
    """
    if not text:
        return ""
    folded = text.strip().casefold()
    words = re.findall(r"\w+", folded, flags=re.UNICODE)
    return " ".join(words)


def is_list_name_matching(list_name: str, candidate_names: List[str]) -> bool:
    """
    Проверяет, соответствует ли название колонки list_name хотя бы одному кандидату из candidate_names.
    Поддерживает кириллицу и латиницу, нечувствителен к регистру и эмодзи.
    """
    if not list_name or not candidate_names:
        return False

    norm_target = normalize_column_name(list_name)
    if not norm_target:
        return False

    for cand in candidate_names:
        norm_cand = normalize_column_name(cand)
        if not norm_cand:
            continue

        # 1. Точное совпадение очищенных слов
        if norm_target == norm_cand:
            return True

        # 2. Одно входит в другое как целое слово или подстрока
        target_words = norm_target.split()
        cand_words = norm_cand.split()

        # Кандидат полностью содержится в словах целевого названия
        if all(w in target_words for w in cand_words):
            return True

        # Или подстрока (например, "входящие" в "входящие задачи")
        if norm_cand in norm_target or norm_target in norm_cand:
            return True

        # 3. Совпадение по основе слова (для склонений в русском языке: выполнено / выполненных / выполненные)
        for cand_w in cand_words:
            if len(cand_w) >= 5:
                # Берем корень/основу слова без окончания (минимум 5 символов)
                stem_len = max(5, len(cand_w) - 2)
                stem = cand_w[:stem_len]
                if any(w.startswith(stem) for w in target_words):
                    return True

    return False


def find_matching_list(
    board_lists: List[Dict[str, Any]],
    candidate_names: List[str],
) -> Optional[Dict[str, Any]]:
    """
    Ищет первую подходящую открытую колонку на доске по списку кандидатов.
    Сначала пытается найти точное совпадение, затем частичное.
    """
    if not board_lists or not candidate_names:
        return None

    norm_candidates = [
        (c, normalize_column_name(c))
        for c in candidate_names
        if normalize_column_name(c)
    ]

    open_lists = [b for b in board_lists if not b.get("closed", False)]

    # Проход 1: Точное совпадение
    for b_list in open_lists:
        norm_name = normalize_column_name(b_list.get("name", ""))
        for _, norm_c in norm_candidates:
            if norm_name == norm_c:
                logger.debug(
                    "Найдено точное совпадение колонки: '%s' (ID: %s)",
                    b_list.get("name"),
                    b_list.get("id"),
                )
                return b_list

    # Проход 2: Совпадение по вхождению слов/подстроки
    for b_list in open_lists:
        norm_name = normalize_column_name(b_list.get("name", ""))
        target_words = norm_name.split()
        for _, norm_c in norm_candidates:
            cand_words = norm_c.split()
            if all(w in target_words for w in cand_words) or norm_c in norm_name:
                logger.debug(
                    "Найдено частичное совпадение колонки: '%s' (ID: %s) для кандидата '%s'",
                    b_list.get("name"),
                    b_list.get("id"),
                    norm_c,
                )
                return b_list

    return None


def find_all_matching_lists(
    board_lists: List[Dict[str, Any]],
    candidate_names: List[str],
) -> List[Dict[str, Any]]:
    """
    Находит все открытые колонки на доске, подходящие под список кандидатов.
    Используется для колонок Done / Завершено, так как их может быть несколько
    (например, 'Done' и 'Архив' или 'Выполнено').
    """
    if not board_lists or not candidate_names:
        return []

    matched: List[Dict[str, Any]] = []
    matched_ids = set()

    open_lists = [b for b in board_lists if not b.get("closed", False)]

    for b_list in open_lists:
        list_id = b_list.get("id")
        if not list_id or list_id in matched_ids:
            continue

        name = b_list.get("name", "")
        if is_list_name_matching(name, candidate_names):
            matched.append(b_list)
            matched_ids.add(list_id)

    return matched
