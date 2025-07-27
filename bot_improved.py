import logging
import json
import telebot
from datetime import datetime
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup
from config import BOT_TOKEN, ADMIN_1_ID, ADMIN_2_ID, ADMIN_BOT_1_TOKEN, ADMIN_BOT_2_TOKEN
from fabric import Connection
from paramiko.ssh_exception import SSHException, AuthenticationException, NoValidConnectionsError
import hashlib
import os
from typing import Dict, Optional, Any
from contextlib import contextmanager

# Налаштовуємо логування з ротацією
from logging.handlers import RotatingFileHandler

# Створюємо директорію для логів, якщо вона не існує
os.makedirs('logs', exist_ok=True)

# Встановлюємо параметри логування
log_handler = RotatingFileHandler('logs/bot.log', maxBytes=10 * 1024 * 1024, backupCount=5)
log_handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
log_handler.setFormatter(formatter)

# Додаємо обробник до кореневого логгера
logging.getLogger().addHandler(log_handler)
logging.getLogger().setLevel(logging.INFO)

# Ініціалізація бота для користувачів
bot = telebot.TeleBot(BOT_TOKEN)

# Константи
ROUTERS_FILE = 'routers.json'
CONNECTION_TIMEOUT = 30
MAX_RETRIES = 3

class ConfigError(Exception):
    """Помилка конфігурації"""
    pass

class RouterSSHClient:
    def __init__(self, ip: str, username: str, ssh_password: str, ssh_port: int = 22):
        """
        Ініціалізація клієнта SSH.
        
        :param ip: IP-адреса маршрутизатора
        :param username: Ім'я користувача для підключення по SSH
        :param ssh_password: Пароль для підключення по SSH
        :param ssh_port: Порт для підключення по SSH (за замовчуванням 22)
        """
        self.ip = ip
        self.username = username
        self.ssh_password = ssh_password
        self.ssh_port = ssh_port

    @contextmanager
    def get_connection(self):
        """Контекстний менеджер для SSH з'єднання"""
        conn = None
        try:
            conn = Connection(
                host=self.ip,
                user=self.username,
                connect_kwargs={
                    "password": self.ssh_password,
                    "timeout": CONNECTION_TIMEOUT
                },
                port=self.ssh_port
            )
            yield conn
        except Exception as e:
            logging.error(f"Помилка створення SSH з'єднання: {e}")
            raise
        finally:
            if conn:
                try:
                    conn.close()
                except Exception as e:
                    logging.warning(f"Помилка закриття SSH з'єднання: {e}")

    def execute_script(self, script: str) -> str:
        """
        Виконання скрипта на маршрутизаторі через SSH.
        
        :param script: Назва скрипта, який потрібно виконати
        :return: Результат виконання скрипта
        """
        # Валідація назви скрипта
        if not script or not script.replace('_', '').replace('-', '').isalnum():
            raise ValueError("Недопустима назва скрипта")

        for attempt in range(MAX_RETRIES):
            try:
                with self.get_connection() as conn:
                    # Виконання скрипта на маршрутизаторі
                    result = conn.run(f"/system script run {script}", hide=True, timeout=CONNECTION_TIMEOUT)
                    return result.stdout if result.stdout else "Скрипт виконано успішно (без виводу)"
                    
            except AuthenticationException as e:
                logging.error(f"Помилка аутентифікації (спроба {attempt + 1}): {e}")
                if attempt == MAX_RETRIES - 1:
                    return "Помилка аутентифікації. Перевірте правильність пароля SSH."
                    
            except NoValidConnectionsError as e:
                logging.error(f"Помилка з'єднання (спроба {attempt + 1}): {e}")
                if attempt == MAX_RETRIES - 1:
                    return f"Помилка з'єднання. Перевірте доступність маршрутизатора за IP-адресою {self.ip}:{self.ssh_port}."
                    
            except SSHException as e:
                logging.error(f"Помилка SSH (спроба {attempt + 1}): {e}")
                if attempt == MAX_RETRIES - 1:
                    return f"Помилка SSH: {e}"
                    
            except Exception as e:
                logging.error(f"Невідома помилка при виконанні скрипта (спроба {attempt + 1}): {e}")
                if attempt == MAX_RETRIES - 1:
                    return f"Невідома помилка при виконанні скрипта: {e}"

class UserStateManager:
    """Менеджер станів користувачів"""
    
    def __init__(self):
        self._states: Dict[int, Dict[str, Any]] = {}
    
    def set_state(self, user_id: int, state: str, **kwargs):
        """Встановити стан користувача"""
        if user_id not in self._states:
            self._states[user_id] = {}
        self._states[user_id]['state'] = state
        self._states[user_id].update(kwargs)
    
    def get_state(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Отримати стан користувача"""
        return self._states.get(user_id)
    
    def clear_state(self, user_id: int):
        """Очистити стан користувача"""
        if user_id in self._states:
            del self._states[user_id]
    
    def get_state_value(self, user_id: int, key: str) -> Any:
        """Отримати конкретне значення зі стану"""
        state = self.get_state(user_id)
        return state.get(key) if state else None

class RouterManager:
    """Менеджер для роботи з маршрутизаторами"""
    
    @staticmethod
    def load_routers() -> Dict[str, Any]:
        """Завантажити конфігурацію маршрутизаторів"""
        try:
            if not os.path.exists(ROUTERS_FILE):
                raise ConfigError(f"Файл {ROUTERS_FILE} не знайдено")
                
            with open(ROUTERS_FILE, 'r', encoding='utf-8') as file:
                routers = json.load(file)
                
            # Валідація структури
            for router_name, router_config in routers.items():
                required_fields = ['ip', 'username', 'ssh_password', 'script_password', 'scripts', 'allowed_users']
                for field in required_fields:
                    if field not in router_config:
                        raise ConfigError(f"Відсутнє поле '{field}' для маршрутизатора '{router_name}'")
                        
            return routers
            
        except json.JSONDecodeError as e:
            logging.error(f"Помилка парсингу JSON: {e}")
            raise ConfigError("Некоректний формат файлу routers.json")
        except Exception as e:
            logging.error(f"Помилка при завантаженні файлу routers.json: {e}")
            raise ConfigError(f"Помилка завантаження конфігурації: {e}")
    
    @staticmethod
    def has_access(user_id: int, router_name: str, routers: Dict[str, Any]) -> bool:
        """Перевірити доступ користувача до маршрутизатора"""
        router = routers.get(router_name)
        if not router:
            return False
        return str(user_id) in router.get('allowed_users', [])

# Ініціалізація менеджера станів
user_state_manager = UserStateManager()

def send_error_message(chat_id: int, message: str):
    """Відправити повідомлення про помилку"""
    try:
        bot.send_message(chat_id, f"❌ {message}")
    except Exception as e:
        logging.error(f"Помилка відправки повідомлення: {e}")

def send_success_message(chat_id: int, message: str):
    """Відправити повідомлення про успіх"""
    try:
        bot.send_message(chat_id, f"✅ {message}")
    except Exception as e:
        logging.error(f"Помилка відправки повідомлення: {e}")

# Обробник команди /start
@bot.message_handler(commands=['start'])
def start(message):
    welcome_text = (
        "🤖 Привіт! Я бот для управління маршрутизаторами.\n\n"
        "Доступні команди:\n"
        "• /run_script - Виконати скрипт на маршрутизаторі\n"
        "• /id - Запросити доступ\n"
        "• /help - Допомога"
    )
    bot.reply_to(message, welcome_text)
    logging.info(f"Користувач {message.from_user.username} (ID: {message.from_user.id}) почав взаємодію з ботом.")

@bot.message_handler(commands=['help'])
def help_command(message):
    help_text = (
        "📖 Допомога:\n\n"
        "1. Використовуйте /run_script для виконання скриптів\n"
        "2. Виберіть маршрутизатор зі списку\n"
        "3. Виберіть скрипт для виконання\n"
        "4. Введіть пароль для підтвердження\n\n"
        "Якщо у вас немає доступу, використовуйте /id для запиту."
    )
    bot.reply_to(message, help_text)

# Обробник команди /id для запиту доступу
@bot.message_handler(commands=['id'])
def request_access(message):
    user_id = message.from_user.id
    user_name = message.from_user.username or "Невідомо"
    user_first_name = message.from_user.first_name or ""
    user_last_name = message.from_user.last_name or ""

    # Відправляємо повідомлення адміністраторам
    admin_message = (
        f"🔐 Запит на доступ:\n\n"
        f"👤 Ім'я: {user_first_name} {user_last_name}\n"
        f"📱 Username: @{user_name}\n"
        f"🆔 ID: {user_id}\n\n"
        f"Будь ласка, відредагуйте файл routers.json для надання доступу."
    )

    try:
        # Відправка повідомлення адміністраторам
        admin_bot_1 = telebot.TeleBot(ADMIN_BOT_1_TOKEN)
        admin_bot_2 = telebot.TeleBot(ADMIN_BOT_2_TOKEN)
        admin_bot_1.send_message(ADMIN_1_ID, admin_message)
        admin_bot_2.send_message(ADMIN_2_ID, admin_message)

        # Підтверджуємо запит користувачу
        send_success_message(message.chat.id, "Ваш запит на доступ надіслано адміністраторам. Очікуйте їхнє рішення.")
        logging.info(f"Запит на доступ від користувача {user_name} (ID: {user_id})")
        
    except Exception as e:
        logging.error(f"Помилка відправки запиту на доступ: {e}")
        send_error_message(message.chat.id, "Помилка відправки запиту. Спробуйте пізніше.")

# Відправка вибору маршрутизаторів
@bot.message_handler(commands=['run_script'])
def send_router_selection(message):
    user_id = message.from_user.id
    logging.info(f"Користувач {message.from_user.username} (ID: {user_id}) вибрав команду /run_script.")
    
    try:
        routers = RouterManager.load_routers()
    except ConfigError as e:
        send_error_message(message.chat.id, str(e))
        return

    # Створюємо InlineKeyboardMarkup для роутерів
    keyboard = InlineKeyboardMarkup(row_width=1)
    accessible_routers = []
    
    for router_name in routers.keys():
        if RouterManager.has_access(user_id, router_name, routers):
            accessible_routers.append(router_name)
            # Використовуємо base64 кодування для безпечної передачі назв
            import base64
            encoded_name = base64.b64encode(router_name.encode()).decode()
            keyboard.add(InlineKeyboardButton(
                f"🌐 {router_name}", 
                callback_data=f"router:{encoded_name}"
            ))
    
    if accessible_routers:
        bot.reply_to(message, "🌐 Виберіть маршрутизатор:", reply_markup=keyboard)
        user_state_manager.set_state(user_id, 'waiting_for_router')
        logging.info(f"Користувач {message.from_user.username} має доступ до роутерів: {accessible_routers}")
    else:
        send_error_message(message.chat.id, "У вас немає доступу до жодного маршрутизатора. Використовуйте /id для запиту доступу.")
        logging.info(f"Користувач {message.from_user.username} не має доступу до роутерів.")

# Обробка вибору маршрутизатора
@bot.callback_query_handler(func=lambda call: call.data.startswith('router:'))
def handle_router_selection(call):
    user_id = call.from_user.id
    
    try:
        # Декодуємо назву маршрутизатора
        import base64
        encoded_name = call.data.split(':', 1)[1]
        router_name = base64.b64decode(encoded_name.encode()).decode()
        
        routers = RouterManager.load_routers()
        router = routers.get(router_name)
        
        if not router or not RouterManager.has_access(user_id, router_name, routers):
            bot.answer_callback_query(call.id, "❌ Помилка доступу", show_alert=True)
            return

        logging.info(f"Користувач {call.from_user.username} вибрав маршрутизатор: {router_name}")

        # Зберігаємо вибраний маршрутизатор
        user_state_manager.set_state(user_id, 'waiting_for_script', router=router_name)

        # Створюємо InlineKeyboardMarkup для скриптів
        keyboard = InlineKeyboardMarkup(row_width=2)
        for script in router["scripts"]:
            encoded_script = base64.b64encode(f"{router_name}:{script}".encode()).decode()
            keyboard.add(InlineKeyboardButton(
                f"⚡ {script}", 
                callback_data=f"script:{encoded_script}"
            ))

        # Відправляємо повідомлення з кнопками вибору скрипта
        bot.edit_message_text(
            f"⚡ Виберіть скрипт для {router_name}:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=keyboard
        )
        
    except Exception as e:
        logging.error(f"Помилка обробки вибору маршрутизатора: {e}")
        bot.answer_callback_query(call.id, "❌ Помилка обробки запиту", show_alert=True)

# Обробка вибору скрипта
@bot.callback_query_handler(func=lambda call: call.data.startswith('script:'))
def handle_script_selection(call):
    user_id = call.from_user.id
    
    try:
        # Декодуємо дані скрипта
        import base64
        encoded_data = call.data.split(':', 1)[1]
        decoded_data = base64.b64decode(encoded_data.encode()).decode()
        router_name, script = decoded_data.split(':', 1)

        # Запит пароля для виконання скрипта
        bot.edit_message_text(
            f"🔐 Введіть пароль для виконання скрипта '{script}' на маршрутизаторі {router_name}:",
            call.message.chat.id,
            call.message.message_id
        )
        
        user_state_manager.set_state(
            user_id, 
            'waiting_for_password', 
            router=router_name, 
            script=script,
            chat_id=call.message.chat.id
        )
        
        logging.info(f"Користувач {call.from_user.username} вибрав скрипт {script} для {router_name}")
        
    except Exception as e:
        logging.error(f"Помилка обробки вибору скрипта: {e}")
        bot.answer_callback_query(call.id, "❌ Помилка обробки запиту", show_alert=True)

# Перевірка пароля та виконання скрипта
@bot.message_handler(func=lambda message: user_state_manager.get_state_value(message.from_user.id, 'state') == 'waiting_for_password')
def verify_password_and_execute(message):
    user_id = message.from_user.id
    state = user_state_manager.get_state(user_id)
    
    if not state:
        send_error_message(message.chat.id, "Сесія застаріла. Почніть спочатку з /run_script")
        return

    router_name = state.get('router')
    script = state.get('script')

    try:
        routers = RouterManager.load_routers()
        router = routers.get(router_name)
        
        if not router:
            send_error_message(message.chat.id, "Маршрутизатор не знайдений.")
            user_state_manager.clear_state(user_id)
            return

        # Перевірка пароля для виконання скрипта
        if message.text == router["script_password"]:
            # Видаляємо повідомлення з паролем з міркувань безпеки
            try:
                bot.delete_message(message.chat.id, message.message_id)
            except:
                pass

            # Відправляємо повідомлення про початок виконання
            status_message = bot.send_message(
                message.chat.id, 
                f"⏳ Виконується скрипт '{script}' на маршрутизаторі '{router_name}'..."
            )

            # Виконуємо скрипт
            ssh_client = RouterSSHClient(
                router["ip"], 
                router["username"], 
                router["ssh_password"], 
                router.get("ssh_port", 22)
            )
            result = ssh_client.execute_script(script)

            # Логування
            execution_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            log_message = f"Скрипт '{script}' був виконаний на маршрутизаторі '{router_name}' користувачем {message.from_user.username} в {execution_time}."
            logging.info(log_message)

            # Уведомлення адміністраторів
            notify_admins(execution_time, message.from_user.username, router_name, script, result[:100])

            # Оновлюємо повідомлення з результатом
            result_text = f"✅ Результат виконання скрипта '{script}' на '{router_name}':\n\n```\n{result}\n```"
            if len(result_text) > 4096:
                result_text = result_text[:4090] + "...\n```"
                
            bot.edit_message_text(
                result_text,
                message.chat.id,
                status_message.message_id,
                parse_mode='Markdown'
            )
            
        else:
            # Видаляємо повідомлення з невірним паролем
            try:
                bot.delete_message(message.chat.id, message.message_id)
            except:
                pass
                
            send_error_message(message.chat.id, "Невірний пароль для виконання скрипта.")
            logging.warning(f"Користувач {message.from_user.username} ввів невірний пароль для скрипта {script}.")
            
        # Очищуємо стан користувача
        user_state_manager.clear_state(user_id)
        
    except ConfigError as e:
        send_error_message(message.chat.id, str(e))
        user_state_manager.clear_state(user_id)
    except Exception as e:
        logging.error(f"Помилка виконання скрипта: {e}")
        send_error_message(message.chat.id, "Помилка виконання скрипта. Спробуйте пізніше.")
        user_state_manager.clear_state(user_id)

# Уведомлення адміністраторів
def notify_admins(execution_time: str, username: str, router_name: str, script: str, result_preview: str = ""):
    admin_message = (
        f"🔔 Статус виконання скрипта:\n\n"
        f"📅 Час: {execution_time}\n"
        f"👤 Користувач: @{username}\n"
        f"🌐 Маршрутизатор: {router_name}\n"
        f"⚡ Скрипт: {script}\n"
    )
    
    if result_preview:
        admin_message += f"📄 Результат (перші 100 символів): {result_preview}..."

    try:
        # Відправляємо повідомлення через обох ботів
        admin_bot_1 = telebot.TeleBot(ADMIN_BOT_1_TOKEN)
        admin_bot_2 = telebot.TeleBot(ADMIN_BOT_2_TOKEN)
        admin_bot_1.send_message(ADMIN_1_ID, admin_message)
        admin_bot_2.send_message(ADMIN_2_ID, admin_message)
    except Exception as e:
        logging.error(f"Помилка відправки уведомлення адміністраторам: {e}")

# Обробник невідомих повідомлень
@bot.message_handler(func=lambda message: True)
def handle_unknown(message):
    user_state = user_state_manager.get_state(message.from_user.id)
    if user_state and user_state.get('state') == 'waiting_for_password':
        return  # Обробляється іншим хендлером
        
    bot.reply_to(
        message, 
        "❓ Невідома команда. Використовуйте /help для отримання списку доступних команд."
    )

# Запуск бота
if __name__ == "__main__":
    # Перевірка конфігурації при запуску
    if not BOT_TOKEN:
        logging.error("BOT_TOKEN не встановлено в config.py")
        exit(1)
        
    try:
        RouterManager.load_routers()
        logging.info("Конфігурація маршрутизаторів завантажена успішно.")
    except ConfigError as e:
        logging.error(f"Помилка конфігурації: {e}")
        exit(1)
    
    logging.info("Бот запущений.")
    try:
        bot.polling(none_stop=True, interval=1, timeout=60)
    except Exception as e:
        logging.error(f"Критична помилка бота: {e}")
        exit(1)