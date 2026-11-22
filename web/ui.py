from typing import Optional
from aiohttp import web

from web.config import API_PREFIX_KEY, API_V1_PREFIX, PREFIX_KEY, WEB_PREFIX, WEB_UI_PATH


def render_html_page(prefix: str = WEB_PREFIX, api_prefix: str = API_V1_PREFIX) -> str:
    """Генерирует HTML страницу панели управления задачами (Web UI)."""
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Personal Family Tasks Bot — Задачи и Календарь</title>
    <style>
        :root {{
            --bg-primary: #f8fafc;
            --bg-card: #ffffff;
            --bg-secondary: #f1f5f9;
            --border-color: #e2e8f0;
            --text-primary: #0f172a;
            --text-secondary: #475569;
            --text-muted: #94a3b8;
            --accent-primary: #2563eb;
            --accent-hover: #1d4ed8;
            --success: #16a34a;
            --success-bg: #dcfce7;
            --warning: #d97706;
            --warning-bg: #fef3c7;
            --danger: #dc2626;
            --danger-bg: #fee2e2;
            --caldav: #0284c7;
            --caldav-bg: #e0f2fe;
            --trello: #0079bf;
            --trello-bg: #e6f4ff;
            --shadow-sm: 0 1px 2px 0 rgba(0, 0, 0, 0.05);
            --shadow-md: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -2px rgba(0, 0, 0, 0.1);
            --shadow-lg: 0 10px 15px -3px rgba(0, 0, 0, 0.1), 0 4px 6px -4px rgba(0, 0, 0, 0.1);
            --radius-sm: 6px;
            --radius-md: 10px;
            --radius-lg: 14px;
        }}

        @media (prefers-color-scheme: dark) {{
            :root:not([data-theme="light"]) {{
                --bg-primary: #0f172a;
                --bg-card: #1e293b;
                --bg-secondary: #334155;
                --border-color: #334155;
                --text-primary: #f8fafc;
                --text-secondary: #cbd5e1;
                --text-muted: #64748b;
                --accent-primary: #3b82f6;
                --accent-hover: #60a5fa;
                --success: #22c55e;
                --success-bg: rgba(34, 197, 94, 0.15);
                --warning: #f59e0b;
                --warning-bg: rgba(245, 158, 11, 0.15);
                --danger: #ef4444;
                --danger-bg: rgba(239, 68, 68, 0.15);
                --caldav: #38bdf8;
                --caldav-bg: rgba(56, 189, 248, 0.15);
                --trello: #38bdf8;
                --trello-bg: rgba(0, 121, 191, 0.2);
            }}
        }}

        :root[data-theme="dark"] {{
            --bg-primary: #0f172a;
            --bg-card: #1e293b;
            --bg-secondary: #334155;
            --border-color: #334155;
            --text-primary: #f8fafc;
            --text-secondary: #cbd5e1;
            --text-muted: #64748b;
            --accent-primary: #3b82f6;
            --accent-hover: #60a5fa;
            --success: #22c55e;
            --success-bg: rgba(34, 197, 94, 0.15);
            --warning: #f59e0b;
            --warning-bg: rgba(245, 158, 11, 0.15);
            --danger: #ef4444;
            --danger-bg: rgba(239, 68, 68, 0.15);
            --caldav: #38bdf8;
            --caldav-bg: rgba(56, 189, 248, 0.15);
            --trello: #38bdf8;
            --trello-bg: rgba(0, 121, 191, 0.2);
        }}

        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }}

        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            background-color: var(--bg-primary);
            color: var(--text-primary);
            line-height: 1.5;
            min-height: 100vh;
            padding: 20px;
        }}

        .container {{
            max-width: 1240px;
            margin: 0 auto;
        }}

        /* Header */
        header {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 16px 24px;
            background: var(--bg-card);
            border-radius: var(--radius-lg);
            border: 1px solid var(--border-color);
            box-shadow: var(--shadow-sm);
            margin-bottom: 24px;
        }}

        .brand {{
            display: flex;
            align-items: center;
            gap: 12px;
        }}

        .brand-icon {{
            width: 40px;
            height: 40px;
            background: linear-gradient(135deg, #2563eb, #3b82f6);
            border-radius: var(--radius-md);
            display: flex;
            align-items: center;
            justify-content: center;
            color: white;
            font-size: 20px;
            box-shadow: var(--shadow-sm);
        }}

        .brand-title {{
            font-size: 1.25rem;
            font-weight: 700;
            color: var(--text-primary);
            display: flex;
            align-items: center;
            gap: 8px;
        }}

        .brand-subtitle {{
            font-size: 0.825rem;
            color: var(--text-muted);
        }}

        .header-actions {{
            display: flex;
            align-items: center;
            gap: 10px;
        }}

        .prefix-badge {{
            font-size: 0.75rem;
            font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
            background: var(--bg-secondary);
            color: var(--text-secondary);
            padding: 4px 8px;
            border-radius: var(--radius-sm);
            border: 1px solid var(--border-color);
        }}

        .btn {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 6px;
            font-size: 0.875rem;
            font-weight: 500;
            padding: 8px 14px;
            border-radius: var(--radius-sm);
            border: 1px solid transparent;
            cursor: pointer;
            transition: all 0.15s ease-in-out;
            text-decoration: none;
            white-space: nowrap;
        }}

        .btn-sm {{
            padding: 5px 10px;
            font-size: 0.8rem;
        }}

        .btn-primary {{
            background: var(--accent-primary);
            color: white;
        }}
        .btn-primary:hover {{
            background: var(--accent-hover);
        }}

        .btn-outline {{
            background: var(--bg-card);
            border-color: var(--border-color);
            color: var(--text-secondary);
        }}
        .btn-outline:hover {{
            background: var(--bg-secondary);
            color: var(--text-primary);
        }}

        .btn-success {{
            background: var(--success);
            color: white;
        }}
        .btn-success:hover {{
            opacity: 0.9;
        }}

        .btn-caldav {{
            background: var(--caldav);
            color: white;
        }}
        .btn-caldav:hover {{
            opacity: 0.9;
        }}

        .btn-trello {{
            background: var(--trello);
            color: white;
        }}
        .btn-trello:hover {{
            opacity: 0.9;
        }}

        .btn:disabled {{
            opacity: 0.5;
            cursor: not-allowed;
            pointer-events: none;
        }}

        /* Stats KPI Bar */
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 16px;
            margin-bottom: 24px;
        }}

        .stat-card {{
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: var(--radius-md);
            padding: 16px 20px;
            box-shadow: var(--shadow-sm);
            display: flex;
            align-items: center;
            justify-content: space-between;
        }}

        .stat-label {{
            font-size: 0.825rem;
            color: var(--text-muted);
            margin-bottom: 4px;
            font-weight: 500;
        }}

        .stat-value {{
            font-size: 1.75rem;
            font-weight: 700;
            color: var(--text-primary);
            line-height: 1.2;
        }}

        .stat-icon {{
            width: 44px;
            height: 44px;
            border-radius: var(--radius-md);
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 22px;
        }}

        /* Toolbar and Filters */
        .toolbar {{
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: var(--radius-lg);
            padding: 16px 20px;
            box-shadow: var(--shadow-sm);
            margin-bottom: 20px;
            display: flex;
            flex-direction: column;
            gap: 16px;
        }}

        .filter-tabs {{
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 14px;
        }}

        .tab-btn {{
            background: none;
            border: none;
            font-size: 0.875rem;
            font-weight: 500;
            padding: 6px 14px;
            border-radius: var(--radius-sm);
            color: var(--text-secondary);
            cursor: pointer;
            transition: all 0.15s ease;
        }}

        .tab-btn:hover {{
            background: var(--bg-secondary);
            color: var(--text-primary);
        }}

        .tab-btn.active {{
            background: var(--accent-primary);
            color: white;
        }}

        .toolbar-controls {{
            display: flex;
            flex-wrap: wrap;
            gap: 12px;
            align-items: center;
            justify-content: space-between;
        }}

        .search-box {{
            position: relative;
            flex: 1;
            min-width: 260px;
            max-width: 480px;
        }}

        .search-input {{
            width: 100%;
            padding: 8px 12px 8px 36px;
            border: 1px solid var(--border-color);
            border-radius: var(--radius-sm);
            background: var(--bg-secondary);
            color: var(--text-primary);
            font-size: 0.875rem;
            outline: none;
            transition: border-color 0.15s;
        }}

        .search-input:focus {{
            border-color: var(--accent-primary);
            background: var(--bg-card);
        }}

        .search-icon {{
            position: absolute;
            left: 10px;
            top: 50%;
            transform: translateY(-50%);
            color: var(--text-muted);
            pointer-events: none;
        }}

        .filter-selects {{
            display: flex;
            gap: 10px;
            align-items: center;
        }}

        .select-input {{
            padding: 8px 12px;
            border: 1px solid var(--border-color);
            border-radius: var(--radius-sm);
            background: var(--bg-secondary);
            color: var(--text-primary);
            font-size: 0.875rem;
            outline: none;
            cursor: pointer;
        }}

        /* Table Card */
        .table-card {{
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: var(--radius-lg);
            box-shadow: var(--shadow-sm);
            overflow: hidden;
            margin-bottom: 20px;
        }}

        .table-responsive {{
            width: 100%;
            overflow-x: auto;
        }}

        table {{
            width: 100%;
            border-collapse: collapse;
            text-align: left;
            font-size: 0.875rem;
        }}

        th {{
            background: var(--bg-secondary);
            color: var(--text-secondary);
            padding: 12px 16px;
            font-weight: 600;
            border-bottom: 1px solid var(--border-color);
            white-space: nowrap;
        }}

        td {{
            padding: 14px 16px;
            border-bottom: 1px solid var(--border-color);
            vertical-align: middle;
        }}

        tr:last-child td {{
            border-bottom: none;
        }}

        tr:hover td {{
            background: var(--bg-secondary);
        }}

        /* Badges */
        .badge {{
            display: inline-flex;
            align-items: center;
            gap: 4px;
            font-size: 0.75rem;
            font-weight: 600;
            padding: 3px 8px;
            border-radius: 9999px;
            white-space: nowrap;
        }}

        .badge-id {{
            background: var(--bg-secondary);
            color: var(--text-muted);
            font-family: ui-monospace, SFMono-Regular, monospace;
            font-weight: 700;
            padding: 2px 6px;
            border-radius: var(--radius-sm);
        }}

        .badge-pending {{
            background: var(--warning-bg);
            color: var(--warning);
        }}

        .badge-reminded {{
            background: var(--caldav-bg);
            color: var(--caldav);
        }}

        .badge-completed {{
            background: var(--success-bg);
            color: var(--success);
        }}

        .badge-deadline-overdue {{
            background: var(--danger-bg);
            color: var(--danger);
            font-weight: 700;
        }}

        .badge-deadline-soon {{
            background: var(--warning-bg);
            color: var(--warning);
        }}

        .badge-deadline-future {{
            background: var(--bg-secondary);
            color: var(--text-secondary);
        }}

        .badge-deadline-none {{
            color: var(--text-muted);
            background: transparent;
        }}

        .badge-category {{
            background: var(--bg-secondary);
            color: var(--text-secondary);
            border: 1px solid var(--border-color);
            font-weight: 500;
        }}

        .badge-trello {{
            background: var(--trello-bg);
            color: var(--trello);
            text-decoration: none;
            display: inline-flex;
            align-items: center;
            gap: 4px;
        }}
        .badge-trello:hover {{
            text-decoration: underline;
        }}

        .badge-caldav {{
            background: var(--caldav-bg);
            color: var(--caldav);
        }}

        .task-title {{
            font-weight: 600;
            color: var(--text-primary);
            cursor: pointer;
            margin-bottom: 2px;
        }}
        .task-title:hover {{
            color: var(--accent-primary);
        }}

        .task-desc {{
            font-size: 0.8rem;
            color: var(--text-muted);
            display: -webkit-box;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
            overflow: hidden;
            max-width: 380px;
        }}

        .actions-cell {{
            display: flex;
            align-items: center;
            gap: 8px;
            flex-wrap: nowrap;
        }}

        /* Empty state & Loading */
        .empty-state {{
            padding: 48px 24px;
            text-align: center;
            color: var(--text-muted);
        }}

        .empty-icon {{
            font-size: 48px;
            margin-bottom: 12px;
        }}

        .loading-spinner {{
            display: inline-block;
            width: 16px;
            height: 16px;
            border: 2px solid rgba(255, 255, 255, 0.3);
            border-radius: 50%;
            border-top-color: white;
            animation: spin 0.6s linear infinite;
        }}

        @keyframes spin {{
            to {{ transform: rotate(360deg); }}
        }}

        /* Pagination */
        .pagination-bar {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            flex-wrap: wrap;
            gap: 12px;
            padding: 16px 20px;
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: var(--radius-lg);
            box-shadow: var(--shadow-sm);
        }}

        .pagination-info {{
            font-size: 0.875rem;
            color: var(--text-secondary);
        }}

        .pagination-controls {{
            display: flex;
            align-items: center;
            gap: 6px;
        }}

        .page-num-btn {{
            min-width: 32px;
            height: 32px;
            padding: 0 6px;
            font-size: 0.825rem;
            font-weight: 600;
            border-radius: var(--radius-sm);
            border: 1px solid var(--border-color);
            background: var(--bg-card);
            color: var(--text-secondary);
            cursor: pointer;
            transition: all 0.15s ease;
        }}

        .page-num-btn:hover {{
            background: var(--bg-secondary);
            color: var(--text-primary);
        }}

        .page-num-btn.active {{
            background: var(--accent-primary);
            color: white;
            border-color: var(--accent-primary);
        }}

        /* Modal Dialog */
        .modal-overlay {{
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0, 0, 0, 0.5);
            display: none;
            align-items: center;
            justify-content: center;
            padding: 20px;
            z-index: 1000;
            backdrop-filter: blur(2px);
        }}

        .modal-overlay.open {{
            display: flex;
        }}

        .modal-card {{
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: var(--radius-lg);
            box-shadow: var(--shadow-lg);
            max-width: 650px;
            width: 100%;
            max-height: 90vh;
            overflow-y: auto;
            display: flex;
            flex-direction: column;
        }}

        .modal-header {{
            padding: 20px 24px;
            border-bottom: 1px solid var(--border-color);
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            gap: 16px;
        }}

        .modal-title {{
            font-size: 1.15rem;
            font-weight: 700;
            color: var(--text-primary);
        }}

        .modal-close-btn {{
            background: none;
            border: none;
            color: var(--text-muted);
            font-size: 20px;
            cursor: pointer;
            padding: 4px;
            line-height: 1;
        }}
        .modal-close-btn:hover {{
            color: var(--text-primary);
        }}

        .modal-body {{
            padding: 20px 24px;
            display: flex;
            flex-direction: column;
            gap: 16px;
        }}

        .modal-row {{
            display: flex;
            flex-direction: column;
            gap: 4px;
        }}

        .modal-label {{
            font-size: 0.75rem;
            text-transform: uppercase;
            font-weight: 600;
            color: var(--text-muted);
            letter-spacing: 0.05em;
        }}

        .modal-value {{
            font-size: 0.95rem;
            color: var(--text-primary);
            word-break: break-word;
            white-space: pre-wrap;
        }}

        .modal-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 16px;
        }}

        .modal-footer {{
            padding: 16px 24px;
            border-top: 1px solid var(--border-color);
            background: var(--bg-secondary);
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
        }}

        /* Toast notifications */
        .toast-container {{
            position: fixed;
            bottom: 24px;
            right: 24px;
            display: flex;
            flex-direction: column;
            gap: 10px;
            z-index: 2000;
            pointer-events: none;
        }}

        .toast {{
            pointer-events: auto;
            background: var(--bg-card);
            color: var(--text-primary);
            border: 1px solid var(--border-color);
            border-left: 4px solid var(--accent-primary);
            border-radius: var(--radius-sm);
            box-shadow: var(--shadow-lg);
            padding: 12px 18px;
            font-size: 0.875rem;
            display: flex;
            align-items: center;
            gap: 10px;
            min-width: 280px;
            max-width: 420px;
            animation: slideIn 0.2s ease-out;
            cursor: pointer;
        }}

        .toast-success {{
            border-left-color: var(--success);
        }}

        .toast-error {{
            border-left-color: var(--danger);
        }}

        .toast-warning {{
            border-left-color: var(--warning);
        }}

        @keyframes slideIn {{
            from {{
                transform: translateX(100%);
                opacity: 0;
            }}
            to {{
                transform: translateX(0);
                opacity: 1;
            }}
        }}

        @media (max-width: 768px) {{
            body {{
                padding: 12px;
            }}
            header {{
                flex-direction: column;
                align-items: flex-start;
                gap: 12px;
            }}
            .header-actions {{
                width: 100%;
                justify-content: space-between;
            }}
            .toolbar-controls {{
                flex-direction: column;
                align-items: stretch;
            }}
            .search-box {{
                max-width: 100%;
            }}
            .actions-cell {{
                flex-direction: column;
                align-items: stretch;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <!-- Header -->
        <header>
            <div class="brand">
                <div class="brand-icon">📋</div>
                <div>
                    <div class="brand-title">
                        Personal Family Tasks Bot
                        <span class="prefix-badge">{prefix or "/"}</span>
                    </div>
                    <div class="brand-subtitle">Панель управления задачами и синхронизацией с календарем</div>
                </div>
            </div>
            <div class="header-actions">
                <button class="btn btn-outline btn-sm" id="btn-sync-trello" onclick="handleSyncTrello()" title="Синхронизировать задачи с Trello">
                    📋 Sync Trello
                </button>
                <button class="btn btn-outline btn-sm" id="btn-refresh" title="Обновить список">
                    🔄 Обновить
                </button>
                <button class="btn btn-outline btn-sm" id="btn-theme-toggle" title="Переключить тему">
                    🌙 / ☀️
                </button>
            </div>
        </header>

        <!-- KPI Stats -->
        <div class="stats-grid">
            <div class="stat-card">
                <div>
                    <div class="stat-label">ВСЕГО ЗАДАЧ</div>
                    <div class="stat-value" id="stat-total">-</div>
                </div>
                <div class="stat-icon" style="background: var(--bg-secondary); color: var(--accent-primary);">📊</div>
            </div>
            <div class="stat-card">
                <div>
                    <div class="stat-label">АКТИВНЫХ</div>
                    <div class="stat-value" id="stat-active">-</div>
                </div>
                <div class="stat-icon" style="background: var(--warning-bg); color: var(--warning);">⏳</div>
            </div>
            <div class="stat-card">
                <div>
                    <div class="stat-label">ВЫПОЛНЕНО</div>
                    <div class="stat-value" id="stat-completed">-</div>
                </div>
                <div class="stat-icon" style="background: var(--success-bg); color: var(--success);">✅</div>
            </div>
            <div class="stat-card">
                <div>
                    <div class="stat-label">В КАЛЕНДАРЕ</div>
                    <div class="stat-value" id="stat-calendar">-</div>
                </div>
                <div class="stat-icon" style="background: var(--caldav-bg); color: var(--caldav);">📅</div>
            </div>
        </div>

        <!-- Toolbar and Filter Bar -->
        <div class="toolbar">
            <div class="filter-tabs" id="status-tabs">
                <button class="tab-btn active" data-status="all">Все задачи</button>
                <button class="tab-btn" data-status="active">Активные</button>
                <button class="tab-btn" data-status="pending">Ожидают</button>
                <button class="tab-btn" data-status="reminded">Напомнено</button>
                <button class="tab-btn" data-status="completed">Завершенные</button>
            </div>
            <div class="toolbar-controls">
                <div class="search-box">
                    <span class="search-icon">🔍</span>
                    <input type="text" class="search-input" id="search-input" placeholder="Поиск по задачам, описанию, месту...">
                </div>
                <div class="filter-selects">
                    <select class="select-input" id="category-select">
                        <option value="all">Все категории</option>
                        <option value="task">📌 Задачи (task)</option>
                        <option value="calendar">🗓 Календарь (calendar)</option>
                        <option value="buy">🛒 Покупки (buy)</option>
                    </select>
                    <select class="select-input" id="per-page-select">
                        <option value="10">10 на стр.</option>
                        <option value="25" selected>25 на стр.</option>
                        <option value="50">50 на стр.</option>
                    </select>
                </div>
            </div>
        </div>

        <!-- Tasks Table Card -->
        <div class="table-card">
            <div class="table-responsive">
                <table>
                    <thead>
                        <tr>
                            <th style="width: 60px;">ID</th>
                            <th>Задача</th>
                            <th>Категория</th>
                            <th>Дедлайн</th>
                            <th>Статус</th>
                            <th>Интеграции</th>
                            <th style="text-align: right;">Действия</th>
                        </tr>
                    </thead>
                    <tbody id="tasks-table-body">
                        <tr>
                            <td colspan="7" class="empty-state">
                                <div class="loading-spinner"></div>
                                <div style="margin-top: 8px;">Загрузка списка задач...</div>
                            </td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- Pagination Bar -->
        <div class="pagination-bar" id="pagination-bar" style="display: none;">
            <div class="pagination-info" id="pagination-info">Показано 0 из 0</div>
            <div class="pagination-controls" id="pagination-buttons">
                <!-- Кнопки страниц формируются динамически JS -->
            </div>
        </div>
    </div>

    <!-- Modal Dialog: Детали задачи -->
    <div class="modal-overlay" id="task-modal">
        <div class="modal-card">
            <div class="modal-header">
                <div>
                    <span class="badge badge-id" id="modal-task-id">#0</span>
                    <div class="modal-title" id="modal-task-title" style="margin-top: 6px;">Название задачи</div>
                </div>
                <button class="modal-close-btn" id="modal-close-btn">&times;</button>
            </div>
            <div class="modal-body">
                <div class="modal-row" id="modal-desc-row">
                    <div class="modal-label">Описание</div>
                    <div class="modal-value" id="modal-task-desc">-</div>
                </div>
                <div class="modal-grid">
                    <div class="modal-row">
                        <div class="modal-label">Статус</div>
                        <div class="modal-value" id="modal-task-status">-</div>
                    </div>
                    <div class="modal-row">
                        <div class="modal-label">Категория</div>
                        <div class="modal-value" id="modal-task-category">-</div>
                    </div>
                    <div class="modal-row">
                        <div class="modal-label">Дедлайн</div>
                        <div class="modal-value" id="modal-task-deadline">-</div>
                    </div>
                    <div class="modal-row" id="modal-location-row">
                        <div class="modal-label">Местоположение / Локация</div>
                        <div class="modal-value" id="modal-task-location">-</div>
                    </div>
                    <div class="modal-row" id="modal-meeting-row">
                        <div class="modal-label">Ссылка на встречу</div>
                        <div class="modal-value" id="modal-task-meeting">-</div>
                    </div>
                    <div class="modal-row" id="modal-contact-row">
                        <div class="modal-label">Контактная информация</div>
                        <div class="modal-value" id="modal-task-contact">-</div>
                    </div>
                </div>
                <div class="modal-grid">
                    <div class="modal-row">
                        <div class="modal-label">Создано</div>
                        <div class="modal-value" id="modal-task-created">-</div>
                    </div>
                    <div class="modal-row">
                        <div class="modal-label">Обновлено</div>
                        <div class="modal-value" id="modal-task-updated">-</div>
                    </div>
                </div>
                <div class="modal-row" id="modal-integrations-row">
                    <div class="modal-label">Интеграции</div>
                    <div class="modal-value" id="modal-task-integrations" style="display: flex; gap: 8px; flex-wrap: wrap;">-</div>
                </div>
            </div>
            <div class="modal-footer">
                <div id="modal-action-buttons" style="display: flex; gap: 8px;">
                    <!-- Кнопки Завершить и В календарь -->
                </div>
                <button class="btn btn-outline btn-sm" id="modal-footer-close">Закрыть</button>
            </div>
        </div>
    </div>

    <!-- Toast Notifications Container -->
    <div class="toast-container" id="toast-container"></div>

    <script>
        const API_PREFIX = "{api_prefix}";
        const BASE_PREFIX = "{prefix}";

        // Состояние приложения
        const state = {{
            page: 1,
            per_page: 25,
            status: "all",
            category: "all",
            search: "",
            tasks: [],
            pagination: {{}},
            activeModalTaskId: null
        }};

        // Инициализация темы
        function initTheme() {{
            const savedTheme = localStorage.getItem("pftb_theme");
            if (savedTheme) {{
                document.documentElement.setAttribute("data-theme", savedTheme);
            }}
            document.getElementById("btn-theme-toggle").addEventListener("click", () => {{
                const current = document.documentElement.getAttribute("data-theme");
                const next = current === "dark" ? "light" : "dark";
                document.documentElement.setAttribute("data-theme", next);
                localStorage.setItem("pftb_theme", next);
            }});
        }}

        // Всплывающие уведомления (Toast)
        function showToast(message, type = "info") {{
            const container = document.getElementById("toast-container");
            const toast = document.createElement("div");
            toast.className = `toast toast-${{type}}`;
            toast.innerHTML = `<span>${{message}}</span>`;
            toast.onclick = () => toast.remove();
            container.appendChild(toast);
            setTimeout(() => {{
                toast.style.opacity = "0";
                toast.style.transition = "opacity 0.4s ease";
                setTimeout(() => toast.remove(), 400);
            }}, 4000);
        }}

        // Загрузка статистики KPI
        async function fetchStats() {{
            try {{
                const res = await fetch(`${{API_PREFIX}}/stats`);
                const data = await res.json();
                if (data.success && data.stats) {{
                    document.getElementById("stat-total").textContent = data.stats.total;
                    document.getElementById("stat-active").textContent = data.stats.active;
                    document.getElementById("stat-completed").textContent = data.stats.completed;
                    document.getElementById("stat-calendar").textContent = data.stats.in_calendar;
                }}
            }} catch (e) {{
                console.error("Ошибка загрузки статистики:", e);
            }}
        }}

        // Форматирование дедлайна
        function renderDeadlineBadge(deadlineStr, isCompleted) {{
            if (!deadlineStr) {{
                return '<span class="badge badge-deadline-none">—</span>';
            }}
            try {{
                const d = new Date(deadlineStr.replace(" ", "T"));
                const now = new Date();
                const isOverdue = !isCompleted && d < now;
                const isSoon = !isCompleted && !isOverdue && (d - now) < (24 * 3600 * 1000);

                let badgeClass = "badge-deadline-future";
                let icon = "🕒";
                if (isOverdue) {{
                    badgeClass = "badge-deadline-overdue";
                    icon = "⚠️";
                }} else if (isSoon) {{
                    badgeClass = "badge-deadline-soon";
                    icon = "⏳";
                }}

                const formatted = d.toLocaleDateString("ru-RU", {{
                    day: "numeric",
                    month: "short",
                    hour: "2-digit",
                    minute: "2-digit"
                }});
                return `<span class="badge ${{badgeClass}}" title="${{deadlineStr}}">${{icon}} ${{formatted}}</span>`;
            }} catch (e) {{
                return `<span class="badge badge-deadline-future">${{deadlineStr}}</span>`;
            }}
        }}

        // Форматирование статуса
        function renderStatusBadge(status) {{
            if (status === "completed") {{
                return '<span class="badge badge-completed">✅ Выполнено</span>';
            }} else if (status === "reminded") {{
                return '<span class="badge badge-reminded">🔔 Напомнено</span>';
            }} else {{
                return '<span class="badge badge-pending">⏳ Ожидает</span>';
            }}
        }}

        // Загрузка списка задач
        async function fetchTasks() {{
            const tbody = document.getElementById("tasks-table-body");
            tbody.innerHTML = `
                <tr>
                    <td colspan="7" class="empty-state">
                        <div class="loading-spinner"></div>
                        <div style="margin-top: 8px;">Загрузка списка задач...</div>
                    </td>
                </tr>
            `;

            const params = new URLSearchParams({{
                page: state.page,
                per_page: state.per_page,
                status: state.status,
                category: state.category,
                search: state.search
            }});

            try {{
                const res = await fetch(`${{API_PREFIX}}/tasks?${{params.toString()}}`);
                const data = await res.json();
                if (!data.success) {{
                    tbody.innerHTML = `<tr><td colspan="7" class="empty-state" style="color: var(--danger);">Ошибка: ${{data.error || "Не удалось загрузить задачи"}}</td></tr>`;
                    return;
                }}

                state.tasks = data.tasks || [];
                state.pagination = data.pagination || {{}};
                renderTasksTable();
                renderPagination();
                fetchStats();
            }} catch (e) {{
                console.error("Ошибка при запросе задач:", e);
                tbody.innerHTML = `<tr><td colspan="7" class="empty-state" style="color: var(--danger);">Сетевая ошибка при загрузке задач</td></tr>`;
            }}
        }}

        // Рендер таблицы задач
        function renderTasksTable() {{
            const tbody = document.getElementById("tasks-table-body");
            if (state.tasks.length === 0) {{
                tbody.innerHTML = `
                    <tr>
                        <td colspan="7" class="empty-state">
                            <div class="empty-icon">📭</div>
                            <div>Задачи не найдены</div>
                            <div style="font-size: 0.8rem; margin-top: 4px;">Попробуйте сбросить фильтры поиска</div>
                        </td>
                    </tr>
                `;
                return;
            }}

            tbody.innerHTML = state.tasks.map(t => {{
                // Интеграции
                const trelloBadge = t.trello_card_url 
                    ? `<a href="${{t.trello_card_url}}" target="_blank" class="badge badge-trello" title="Открыть в Trello">Trello ↗</a>` 
                    : (t.trello_card_id ? '<span class="badge badge-trello">Trello</span>' : '');

                const caldavBadge = t.caldav_uid 
                    ? '<span class="badge badge-caldav" title="Событие синхронизировано с Apple Calendar">🗓 CalDAV</span>' 
                    : '';

                const integrations = [trelloBadge, caldavBadge].filter(Boolean).join(" ") || '<span style="color: var(--text-muted); font-size: 0.8rem;">—</span>';

                // Кнопка Завершить
                let completeBtn = '';
                if (t.can_complete) {{
                    completeBtn = `
                        <button class="btn btn-success btn-sm" onclick="handleCompleteTask(${{t.id}})" id="btn-done-${{t.id}}">
                            ✅ Завершить
                        </button>
                    `;
                }} else {{
                    completeBtn = `
                        <button class="btn btn-outline btn-sm" disabled title="Задача уже выполнена">
                            ✅ Выполнена
                        </button>
                    `;
                }}

                // Кнопка В календарь (ПОЛНАЯ АНАЛОГИЯ С ЛОГИКОЙ TELEGRAM)
                let calendarBtn = '';
                if (t.is_in_calendar) {{
                    calendarBtn = `
                        <button class="btn btn-outline btn-sm" disabled title="Уже в календаре">
                            🗓 В календаре
                        </button>
                    `;
                }} else if (t.can_add_to_calendar) {{
                    // Есть дедлайн, не в календаре и не завершена
                    calendarBtn = `
                        <button class="btn btn-caldav btn-sm" onclick="handleAddToCalendar(${{t.id}})" id="btn-cal-${{t.id}}">
                            📅 В календарь
                        </button>
                    `;
                }} else if (!t.has_deadline && !t.is_completed) {{
                    // Нет дедлайна - кнопка не активна (или скрыта по аналогии с логикой Telegram)
                    calendarBtn = '';
                }}

                // Кнопка В Trello
                let trelloBtn = '';
                if (t.is_in_trello) {{
                    trelloBtn = `
                        <button class="btn btn-outline btn-sm" disabled title="Уже в Trello">
                            📋 В Trello
                        </button>
                    `;
                }} else if (t.can_add_to_trello) {{
                    trelloBtn = `
                        <button class="btn btn-trello btn-sm" onclick="handleAddToTrello(${{t.id}})" id="btn-trello-${{t.id}}">
                            📋 В Trello
                        </button>
                    `;
                }}

                return `
                    <tr id="task-row-${{t.id}}">
                        <td><span class="badge badge-id">#${{t.id}}</span></td>
                        <td>
                            <div class="task-title" onclick="openTaskModal(${{t.id}})">${{escapeHtml(t.title)}}</div>
                            ${{t.description ? `<div class="task-desc">${{escapeHtml(t.description)}}</div>` : ''}}
                        </td>
                        <td><span class="badge badge-category">${{escapeHtml(t.category || 'task')}}</span></td>
                        <td>${{renderDeadlineBadge(t.deadline, t.is_completed)}}</td>
                        <td>${{renderStatusBadge(t.status)}}</td>
                        <td>${{integrations}}</td>
                        <td style="text-align: right;">
                            <div class="actions-cell" style="justify-content: flex-end;">
                                ${{completeBtn}}
                                ${{calendarBtn}}
                                ${{trelloBtn}}
                                <button class="btn btn-outline btn-sm" onclick="openTaskModal(${{t.id}})" title="Детали задачи">
                                    👁
                                </button>
                            </div>
                        </td>
                    </tr>
                `;
            }}).join("");
        }}

        // Рендер пагинации
        function renderPagination() {{
            const bar = document.getElementById("pagination-bar");
            const info = document.getElementById("pagination-info");
            const btns = document.getElementById("pagination-buttons");
            const p = state.pagination;

            if (!p || p.total === 0) {{
                bar.style.display = "none";
                return;
            }}

            bar.style.display = "flex";
            const start = ((p.page - 1) * p.per_page) + 1;
            const end = Math.min(p.page * p.per_page, p.total);
            info.textContent = `Показано ${{start}}–${{end}} из ${{p.total}} (страница ${{p.page}} из ${{p.total_pages}})`;

            let html = '';
            if (p.has_prev) {{
                html += `<button class="page-num-btn" onclick="changePage(${{p.page - 1}})" title="Предыдущая">&larr;</button>`;
            }}

            // Номера страниц (с умным ограничением вокруг текущей)
            const maxVisible = 5;
            let startPage = Math.max(1, p.page - 2);
            let endPage = Math.min(p.total_pages, startPage + maxVisible - 1);
            if (endPage - startPage < maxVisible - 1) {{
                startPage = Math.max(1, endPage - maxVisible + 1);
            }}

            if (startPage > 1) {{
                html += `<button class="page-num-btn" onclick="changePage(1)">1</button>`;
                if (startPage > 2) html += `<span style="color: var(--text-muted); padding: 0 4px;">...</span>`;
            }}

            for (let i = startPage; i <= endPage; i++) {{
                const active = i === p.page ? 'active' : '';
                html += `<button class="page-num-btn ${{active}}" onclick="changePage(${{i}})">${{i}}</button>`;
            }}

            if (endPage < p.total_pages) {{
                if (endPage < p.total_pages - 1) html += `<span style="color: var(--text-muted); padding: 0 4px;">...</span>`;
                html += `<button class="page-num-btn" onclick="changePage(${{p.total_pages}})">${{p.total_pages}}</button>`;
            }}

            if (p.has_next) {{
                html += `<button class="page-num-btn" onclick="changePage(${{p.page + 1}})" title="Следующая">&rarr;</button>`;
            }}

            btns.innerHTML = html;
        }}

        function changePage(page) {{
            state.page = page;
            fetchTasks();
            window.scrollTo({{ top: 0, behavior: 'smooth' }});
        }}

        // Действие: Завершить задачу
        async function handleCompleteTask(taskId) {{
            const btn = document.getElementById(`btn-done-${{taskId}}`);
            if (btn) {{
                btn.disabled = true;
                btn.innerHTML = '<span class="loading-spinner"></span>';
            }}

            try {{
                const res = await fetch(`${{API_PREFIX}}/tasks/${{taskId}}/complete`, {{
                    method: "POST"
                }});
                const data = await res.json();
                if (data.success) {{
                    showToast(data.message || `Задача #${{taskId}} выполнена!`, "success");
                    // Обновляем локально в массиве state.tasks
                    const idx = state.tasks.findIndex(t => t.id === taskId);
                    if (idx !== -1 && data.task) {{
                        state.tasks[idx] = data.task;
                        renderTasksTable();
                    }} else {{
                        fetchTasks();
                    }}
                    fetchStats();
                    if (state.activeModalTaskId === taskId) {{
                        openTaskModal(taskId);
                    }}
                }} else {{
                    showToast(data.error || "Не удалось завершить задачу", "error");
                    if (btn) {{
                        btn.disabled = false;
                        btn.innerHTML = '✅ Завершить';
                    }}
                }}
            }} catch (e) {{
                console.error("Ошибка завершения задачи:", e);
                showToast("Сетевая ошибка при выполнении запроса", "error");
                if (btn) {{
                    btn.disabled = false;
                    btn.innerHTML = '✅ Завершить';
                }}
            }}
        }}

        // Действие: В календарь (CalDAV)
        async function handleAddToCalendar(taskId) {{
            const btn = document.getElementById(`btn-cal-${{taskId}}`);
            if (btn) {{
                btn.disabled = true;
                btn.innerHTML = '<span class="loading-spinner"></span>';
            }}

            try {{
                const res = await fetch(`${{API_PREFIX}}/tasks/${{taskId}}/to_calendar`, {{
                    method: "POST"
                }});
                const data = await res.json();
                if (data.success) {{
                    showToast(data.message || "Событие добавлено в Apple Calendar!", "success");
                    const idx = state.tasks.findIndex(t => t.id === taskId);
                    if (idx !== -1 && data.task) {{
                        state.tasks[idx] = data.task;
                        renderTasksTable();
                    }} else {{
                        fetchTasks();
                    }}
                    fetchStats();
                    if (state.activeModalTaskId === taskId) {{
                        openTaskModal(taskId);
                    }}
                }} else {{
                    showToast(data.error || "Не удалось добавить в календарь", "error");
                    if (btn) {{
                        btn.disabled = false;
                        btn.innerHTML = '📅 В календарь';
                    }}
                }}
            }} catch (e) {{
                console.error("Ошибка добавления в календарь:", e);
                showToast("Сетевая ошибка при вызове календаря", "error");
                if (btn) {{
                    btn.disabled = false;
                    btn.innerHTML = '📅 В календарь';
                }}
            }}
        }}

        // Действие: В Trello
        async function handleAddToTrello(taskId) {{
            const btn = document.getElementById(`btn-trello-${{taskId}}`);
            if (btn) {{
                btn.disabled = true;
                btn.innerHTML = '<span class="loading-spinner"></span>';
            }}

            try {{
                const res = await fetch(`${{API_PREFIX}}/tasks/${{taskId}}/to_trello`, {{
                    method: "POST"
                }});
                const data = await res.json();
                if (data.success) {{
                    showToast(data.message || "Задача успешно добавлена в Trello!", "success");
                    const idx = state.tasks.findIndex(t => t.id === taskId);
                    if (idx !== -1 && data.task) {{
                        state.tasks[idx] = data.task;
                        renderTasksTable();
                    }} else {{
                        fetchTasks();
                    }}
                    fetchStats();
                    if (state.activeModalTaskId === taskId) {{
                        openTaskModal(taskId);
                    }}
                }} else {{
                    showToast(data.error || "Не удалось отправить задачу в Trello", "error");
                    if (btn) {{
                        btn.disabled = false;
                        btn.innerHTML = '📋 В Trello';
                    }}
                }}
            }} catch (e) {{
                console.error("Ошибка отправки в Trello:", e);
                showToast("Сетевая ошибка при обращении к Trello", "error");
                if (btn) {{
                    btn.disabled = false;
                    btn.innerHTML = '📋 В Trello';
                }}
            }}
        }}

        // Действие: Синхронизация с Trello (Sync)
        async function handleSyncTrello() {{
            const btn = document.getElementById("btn-sync-trello");
            const origContent = btn ? btn.innerHTML : "";
            if (btn) {{
                btn.disabled = true;
                btn.innerHTML = '<span class="loading-spinner"></span> Синхронизация...';
            }}

            try {{
                const res = await fetch(`${{API_PREFIX}}/trello/sync`, {{
                    method: "POST"
                }});
                const data = await res.json();
                if (data.success) {{
                    showToast(data.message || "Синхронизация с Trello завершена!", "success");
                    fetchTasks();
                    fetchStats();
                }} else {{
                    showToast(data.error || "Ошибка синхронизации с Trello", "error");
                }}
            }} catch (e) {{
                console.error("Ошибка при ручной синхронизации с Trello:", e);
                showToast("Сетевая ошибка при синхронизации с Trello", "error");
            }} finally {{
                if (btn) {{
                    btn.disabled = false;
                    btn.innerHTML = origContent;
                }}
            }}
        }}

        // Модальное окно деталей
        async function openTaskModal(taskId) {{
            state.activeModalTaskId = taskId;
            const modal = document.getElementById("task-modal");
            modal.classList.add("open");

            // Ищем задачу в кэше или запрашиваем с сервера
            let task = state.tasks.find(t => t.id === taskId);
            if (!task) {{
                try {{
                    const res = await fetch(`${{API_PREFIX}}/tasks/${{taskId}}`);
                    const data = await res.json();
                    if (data.success) task = data.task;
                }} catch (e) {{
                    console.error("Ошибка деталей задачи:", e);
                }}
            }}

            if (!task) return;

            document.getElementById("modal-task-id").textContent = `#${{task.id}}`;
            document.getElementById("modal-task-title").textContent = task.title;
            document.getElementById("modal-task-desc").textContent = task.description || "—";
            document.getElementById("modal-task-status").innerHTML = renderStatusBadge(task.status);
            document.getElementById("modal-task-category").textContent = task.category || "task";
            document.getElementById("modal-task-deadline").innerHTML = renderDeadlineBadge(task.deadline, task.is_completed);

            // Опциональные поля
            toggleModalRow("modal-location-row", "modal-task-location", task.location);
            toggleModalRow("modal-meeting-row", "modal-task-meeting", task.meeting_url, true);
            toggleModalRow("modal-contact-row", "modal-task-contact", task.contact_info);

            document.getElementById("modal-task-created").textContent = task.created_at || "—";
            document.getElementById("modal-task-updated").textContent = task.updated_at || "—";

            // Интеграции
            const intContainer = document.getElementById("modal-task-integrations");
            let intHtml = [];
            if (task.trello_card_url) {{
                intHtml.push(`<a href="${{task.trello_card_url}}" target="_blank" class="badge badge-trello">Карточка в Trello ↗</a>`);
            }} else if (task.trello_card_id) {{
                intHtml.push(`<span class="badge badge-trello">Trello ID: ${{escapeHtml(task.trello_card_id)}}</span>`);
            }}
            if (task.caldav_uid) {{
                intHtml.push(`<span class="badge badge-caldav">Apple Calendar UID: ${{escapeHtml(task.caldav_uid)}}</span>`);
            }}
            intContainer.innerHTML = intHtml.length > 0 ? intHtml.join(" ") : '<span style="color: var(--text-muted);">Интеграции не привязаны</span>';

            // Кнопки действий в модальном окне
            const actionsContainer = document.getElementById("modal-action-buttons");
            let modalBtns = '';
            if (task.can_complete) {{
                modalBtns += `<button class="btn btn-success btn-sm" onclick="handleCompleteTask(${{task.id}})">✅ Завершить</button>`;
            }}
            if (task.is_in_calendar) {{
                modalBtns += `<button class="btn btn-outline btn-sm" disabled>🗓 В календаре</button>`;
            }} else if (task.can_add_to_calendar) {{
                modalBtns += `<button class="btn btn-caldav btn-sm" onclick="handleAddToCalendar(${{task.id}})">📅 В календарь</button>`;
            }}
            if (task.is_in_trello) {{
                modalBtns += `<button class="btn btn-outline btn-sm" disabled>📋 В Trello</button>`;
            }} else if (task.can_add_to_trello) {{
                modalBtns += `<button class="btn btn-trello btn-sm" onclick="handleAddToTrello(${{task.id}})">📋 В Trello</button>`;
            }}
            actionsContainer.innerHTML = modalBtns;
        }}

        function toggleModalRow(rowId, valId, value, isLink = false) {{
            const row = document.getElementById(rowId);
            const val = document.getElementById(valId);
            if (value && value.trim()) {{
                row.style.display = "flex";
                if (isLink) {{
                    val.innerHTML = `<a href="${{escapeHtml(value)}}" target="_blank" style="color: var(--accent-primary); text-decoration: underline;">${{escapeHtml(value)}} ↗</a>`;
                }} else {{
                    val.textContent = value;
                }}
            }} else {{
                row.style.display = "none";
            }}
        }}

        function closeModal() {{
            document.getElementById("task-modal").classList.remove("open");
            state.activeModalTaskId = null;
        }}

        function escapeHtml(str) {{
            if (!str) return "";
            return str
                .replace(/&/g, "&amp;")
                .replace(/</g, "&lt;")
                .replace(/>/g, "&gt;")
                .replace(/"/g, "&quot;")
                .replace(/'/g, "&#039;");
        }}

        // Слушатели событий
        document.addEventListener("DOMContentLoaded", () => {{
            initTheme();
            fetchTasks();
            fetchStats();

            // Кнопка Обновить
            document.getElementById("btn-refresh").addEventListener("click", () => {{
                fetchTasks();
                fetchStats();
                showToast("Данные обновлены", "info");
            }});

            // Табы статусов
            const tabs = document.querySelectorAll("#status-tabs .tab-btn");
            tabs.forEach(tab => {{
                tab.addEventListener("click", () => {{
                    tabs.forEach(t => t.classList.remove("active"));
                    tab.classList.add("active");
                    state.status = tab.getAttribute("data-status");
                    state.page = 1;
                    fetchTasks();
                }});
            }});

            // Поиск с дебаунсом
            let searchTimeout = null;
            document.getElementById("search-input").addEventListener("input", (e) => {{
                clearTimeout(searchTimeout);
                searchTimeout = setTimeout(() => {{
                    state.search = e.target.value;
                    state.page = 1;
                    fetchTasks();
                }}, 300);
            }});

            // Фильтр категорий
            document.getElementById("category-select").addEventListener("change", (e) => {{
                state.category = e.target.value;
                state.page = 1;
                fetchTasks();
            }});

            // Количество на страницу
            document.getElementById("per-page-select").addEventListener("change", (e) => {{
                state.per_page = parseInt(e.target.value, 10);
                state.page = 1;
                fetchTasks();
            }});

            // Закрытие модального окна
            document.getElementById("modal-close-btn").addEventListener("click", closeModal);
            document.getElementById("modal-footer-close").addEventListener("click", closeModal);
            document.getElementById("task-modal").addEventListener("click", (e) => {{
                if (e.target.id === "task-modal") closeModal();
            }});
            document.addEventListener("keydown", (e) => {{
                if (e.key === "Escape") closeModal();
            }});
        }});
    </script>
</body>
</html>
"""


async def ui_index_handler(request: web.Request) -> web.Response:
    """Обработчик отображения главной страницы Web UI."""
    prefix = request.app.get(PREFIX_KEY) or request.app.get("prefix", WEB_PREFIX)
    api_prefix = request.app.get(API_PREFIX_KEY) or request.app.get("api_prefix", API_V1_PREFIX)
    html_content = render_html_page(prefix=prefix, api_prefix=api_prefix)
    return web.Response(text=html_content, content_type="text/html", charset="utf-8")
