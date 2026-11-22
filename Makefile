.PHONY: help deploy build up down restart logs logs-bot logs-db status migrate test clean

# Default target
.DEFAULT_GOAL := help

help:
	@echo "======================================================================"
	@echo " FamilyTasksBot Management"
	@echo "======================================================================"
	@echo "  make deploy   - Собрать/обновить образы, создать сеть и перезапустить"
	@echo "  make build    - Собрать Docker образ бота"
	@echo "  make up       - Запустить контейнеры в фоновом режиме"
	@echo "  make down     - Остановить все контейнеры"
	@echo "  make restart  - Перезапустить контейнеры"
	@echo "  make logs     - Просмотр логов всех сервисов (следить в реальном времени)"
	@echo "  make logs-bot - Просмотр логов только Telegram бота"
	@echo "  make logs-db  - Просмотр логов PostgreSQL"
	@echo "  make status   - Проверить статус контейнеров"
	@echo "  make migrate  - Выполнить миграцию данных из tasks.db в PostgreSQL"
	@echo "  make test     - Запустить тесты pytest"
	@echo "======================================================================"

deploy:
	@echo "🚀 [Deploy] Начало развертывания FamilyTasksBot..."
	@echo "1. Остановка локального процесса бота на хосте (если запущен)..."
	@pkill -f "python.*bot.py" 2>/dev/null || true
	@echo "2. Проверка наличия общей docker сети 'shared_network'..."
	@docker network create shared_network 2>/dev/null || true
	@echo "3. Сборка образов и запуск контейнеров (PostgreSQL + Bot)..."
	docker compose up -d --build --remove-orphans
	@echo "4. Проверка состояния сервисов..."
	@sleep 2
	docker compose ps
	@echo "✅ [Deploy] Развертывание успешно завершено!"
	@echo "Для просмотра логов выполните: make logs"

build:
	docker compose build

up:
	@docker network create shared_network 2>/dev/null || true
	docker compose up -d

down:
	docker compose down

restart:
	docker compose restart

logs:
	docker compose logs -f --tail=100

logs-bot:
	docker compose logs -f --tail=100 bot

logs-db:
	docker compose logs -f --tail=100 postgres

status:
	docker compose ps

migrate:
	docker compose exec bot python -m database migrate

test:
	.venv/bin/pytest tests/

clean:
	docker compose down -v --remove-orphans
