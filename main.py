# Импорт необходимых библиотек
import sqlite3
import feedparser
import pymorphy3
import re
import logging
from logging.handlers import RotatingFileHandler
import os
import time
from datetime import datetime
from flask import Flask, request, redirect, render_template, g
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)


# Настройка логирования
def setup_logging():
    # Создаем директорию для логов, если ее нет
    if not os.path.exists('logs'):
        os.makedirs('logs')

    # Основной логгер приложения
    logger = logging.getLogger('rss_monitor')
    logger.setLevel(logging.INFO)

    # Форматирование логов
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # Обработчик для записи в файл (с ротацией)
    file_handler = RotatingFileHandler(
        'logs/rss_monitor.log',
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setFormatter(formatter)

    # Обработчик для вывода в консоль
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    # Добавляем обработчики к логгеру
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger


# Инициализация логгера
logger = setup_logging()

# Инициализация морфологического анализатора
morph = pymorphy3.MorphAnalyzer()

# Конфигурация БД
DATABASE = 'rss_monitor.db'


def get_db():
    """Получение соединения с базой данных"""
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db


def init_db():
    """Инициализация базы данных при первом запуске"""
    with app.app_context():
        db = get_db()
        cursor = db.cursor()

        # Создание таблицы RSS-источников
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS feeds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE NOT NULL
            )
        ''')

        # Создание таблицы ключевых слов
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS keywords (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT UNIQUE NOT NULL
            )
        ''')

        # Создание таблицы новостей
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS news (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                link TEXT UNIQUE NOT NULL,
                feed_id INTEGER,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(feed_id) REFERENCES feeds(id)
            )
        ''')

        # Создание таблицы связи новостей с ключевыми словами
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS news_keywords (
                news_id INTEGER,
                keyword_id INTEGER,
                PRIMARY KEY (news_id, keyword_id),
                FOREIGN KEY(news_id) REFERENCES news(id),
                FOREIGN KEY(keyword_id) REFERENCES keywords(id)
            )
        ''')
        db.commit()
        logger.info("База данных инициализирована")


@app.teardown_appcontext
def close_connection(exception):
    """Закрытие соединения с БД после запроса"""
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()


def normalize_word(word):
    """Приводит слово к нормальной форме"""
    parsed = morph.parse(word)[0]
    return parsed.normal_form


def find_keywords_in_text(text, keywords_list):
    """Находит ключевые слова в тексте с учетом словоформ"""
    # Приводим текст к нижнему регистру и извлекаем слова
    words = re.findall(r'\b\w+\b', text.lower())

    # Нормализуем все слова в тексте
    normalized_words = {normalize_word(word) for word in words}

    # Проверяем наличие ключевых слов
    found_keywords = []
    for kw_id, keyword in keywords_list:
        # Нормализуем ключевое слово
        normalized_kw = normalize_word(keyword)
        if normalized_kw in normalized_words:
            found_keywords.append(kw_id)
    return found_keywords


def fetch_and_store_news():
    """Загрузка и обработка новостей из RSS-лент"""
    start_time = time.time()
    logger.info("=" * 50)
    logger.info(f"НАЧАЛО ПРОВЕРКИ НОВОСТЕЙ - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    db = get_db()
    cursor = db.cursor()

    # Получаем все RSS-ленты
    cursor.execute("SELECT id, url FROM feeds")
    feeds = cursor.fetchall()
    logger.info(f"Найдено RSS-лент: {len(feeds)}")

    # Получаем все ключевые слова
    cursor.execute("SELECT id, keyword FROM keywords")
    keywords_list = [(row['id'], row['keyword']) for row in cursor.fetchall()]
    logger.info(f"Найдено ключевых слов: {len(keywords_list)}")

    if not feeds or not keywords_list:
        logger.warning("Проверка прервана: нет RSS-лент или ключевых слов")
        return

    total_news = 0
    total_matched = 0

    # Обработка каждой RSS-ленты
    for feed in feeds:
        feed_id = feed['id']
        url = feed['url']
        logger.info(f"Обработка ленты: {url}")

        try:
            # Парсинг RSS-ленты
            feed_data = feedparser.parse(url)
            logger.info(f"Найдено новостей в ленте: {len(feed_data.entries)}")
            total_news += len(feed_data.entries)

            # Обработка каждой новости в ленте
            for entry in feed_data.entries:
                title = entry.get('title', '')
                content = entry.get('description', '')
                link = entry.get('link', '')

                # Проверка уникальности новости
                cursor.execute("SELECT id FROM news WHERE link = ?", (link,))
                if cursor.fetchone():
                    continue

                # Поиск ключевых слов с учетом словоформ
                full_text = f"{title} {content}".lower()
                found_keywords = find_keywords_in_text(full_text, keywords_list)

                # Сохранение новости если найдены ключевые слова
                if found_keywords:
                    cursor.execute(
                        "INSERT INTO news (title, content, link, feed_id) VALUES (?, ?, ?, ?)",
                        (title, content, link, feed_id)
                    )
                    news_id = cursor.lastrowid

                    # Связывание новости с ключевыми словами
                    for kw_id in found_keywords:
                        cursor.execute(
                            "INSERT INTO news_keywords (news_id, keyword_id) VALUES (?, ?)",
                            (news_id, kw_id)
                        )
                    db.commit()
                    total_matched += 1
                    logger.info(f"Сохранена новость: {title}")
                    logger.info(f"Ссылка: {link}")
                    logger.info(f"Найдены ключевые слова: {found_keywords}")

        except Exception as e:
            logger.error(f"Ошибка при обработке ленты {url}: {str(e)}")

    elapsed = time.time() - start_time
    logger.info(f"ПРОВЕРКА ЗАВЕРШЕНА: обработано {total_news} новостей, найдено {total_matched} соответствий")
    logger.info(f"Время выполнения: {elapsed:.2f} секунд")
    logger.info("=" * 50)


# Веб-интерфейс
@app.route('/')
def index():
    """Главная страница - отображение новостей"""
    db = get_db()
    cursor = db.cursor()

    # Получаем новости с ключевыми словами
    cursor.execute('''
        SELECT news.id, news.title, news.content, news.link, feeds.url AS feed_url
        FROM news
        JOIN feeds ON news.feed_id = feeds.id
        ORDER BY news.timestamp DESC
    ''')
    news_items = cursor.fetchall()
    return render_template('index.html', news_items=news_items)


@app.route('/add_feed', methods=['POST'])
def add_feed():
    """Добавление новой RSS-ленты"""
    url = request.form['url']
    if url:
        try:
            db = get_db()
            cursor = db.cursor()
            cursor.execute("INSERT OR IGNORE INTO feeds (url) VALUES (?)", (url,))
            db.commit()
            logger.info(f"Добавлена RSS-лента: {url}")
        except sqlite3.IntegrityError:
            logger.warning(f"Попытка добавить дубликат RSS-ленты: {url}")
            pass  # Игнорируем дубликаты
    return redirect('/manage')


@app.route('/delete_feed/<int:feed_id>')
def delete_feed(feed_id):
    """Удаление RSS-ленты"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute("DELETE FROM feeds WHERE id = ?", (feed_id,))
    db.commit()
    logger.info(f"Удалена RSS-лента с ID: {feed_id}")
    return redirect('/manage')


@app.route('/add_keyword', methods=['POST'])
def add_keyword():
    """Добавление ключевого слова"""
    keyword = request.form['keyword'].strip().lower()
    if keyword:
        try:
            db = get_db()
            cursor = db.cursor()
            cursor.execute("INSERT OR IGNORE INTO keywords (keyword) VALUES (?)", (keyword,))
            db.commit()
            logger.info(f"Добавлено ключевое слово: {keyword}")
        except sqlite3.IntegrityError:
            logger.warning(f"Попытка добавить дубликат ключевого слова: {keyword}")
            pass  # Игнорируем дубликаты
    return redirect('/manage')


@app.route('/delete_keyword/<int:keyword_id>')
def delete_keyword(keyword_id):
    """Удаление ключевого слова"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute("DELETE FROM keywords WHERE id = ?", (keyword_id,))
    db.commit()
    logger.info(f"Удалено ключевое слово с ID: {keyword_id}")
    return redirect('/manage')


@app.route('/check_news')
def check_news():
    """Ручной запуск проверки новостей"""
    logger.info("Ручной запуск проверки новостей")
    fetch_and_store_news()
    return redirect('/')


@app.route('/manage')
def manage():
    """Страница управления"""
    db = get_db()
    cursor = db.cursor()

    # Получаем все RSS-ленты
    cursor.execute("SELECT id, url FROM feeds")
    feeds = cursor.fetchall()

    # Получаем все ключевые слова
    cursor.execute("SELECT id, keyword FROM keywords")
    keywords = cursor.fetchall()

    return render_template('manage.html', feeds=feeds, keywords=keywords)

def scheduled_news_check():
    """Периодическая проверка новостей"""
    with app.app_context():
        logger.info("АВТОМАТИЧЕСКАЯ ПРОВЕРКА НОВОСТЕЙ ПО РАСПИСАНИЮ")
        fetch_and_store_news()

if __name__ == '__main__':
    logger.info("Запуск приложения RSS Monitor")
    init_db()

    # Настройка автоматического обновления
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        func=scheduled_news_check,
        trigger='interval',
        minutes=1,  # Проверка каждую минуту
        next_run_time=datetime.now()  # Запустить сразу при старте
    )
    scheduler.start()

    try:
        app.run(debug=True, host='0.0.0.0', port=5000, use_reloader=False)
    finally:
        scheduler.shutdown()