import logging
import json
import telebot
from datetime import datetime
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup
from config import BOT_TOKEN, ADMIN_1_ID, ADMIN_2_ID, ADMIN_BOT_1_TOKEN, ADMIN_BOT_2_TOKEN
from fabric import Connection
from paramiko.ssh_exception import SSHException, AuthenticationException, NoValidConnectionsError

# Налаштовуємо логування з ротацією
from logging.handlers import RotatingFileHandler

# Встановлюємо параметри логування
log_handler = RotatingFileHandler('logs/bot.log', maxBytes=10 * 1024 * 1024, backupCount=5)
log_handler.setLevel(logging.INFO)  # Встановлюємо рівень логування
formatter = logging.Formatter('%(asctime)s - %(message)s')
log_handler.setFormatter(formatter)

# Додаємо обробник до кореневого логгера
logging.getLogger().addHandler(log_handler)
logging.getLogger().setLevel(logging.INFO)

# Ініціалізація бота для користувачів
bot = telebot.TeleBot(BOT_TOKEN)

# Клас для роботи з SSH через Fabric
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
        self.ssh_port = ssh_port  # Ураховуємо порт для SSH

    def execute_script(self, script: str) -> str:
        """
        Виконання скрипта на маршрутизаторі через SSH.
        
        :param script: Назва скрипта, який потрібно виконати
        :return: Результат виконання скрипта
        """
        try:
            # Створюємо підключення через Fabric
            conn = Connection(
                host=self.ip, 
                user=self.username, 
                connect_kwargs={"password": self.ssh_password},  # Використовуємо ssh_password для SSH-підключення
                port=self.ssh_port  # Вказуємо порт для підключення
            )
            # Виконання скрипта на маршрутизаторі
            result = conn.run(f"/system script run {script}", hide=True)
            return result.stdout
        except AuthenticationException as e:
            logging.error(f"Помилка аутентифікації: {e}")
            return "Помилка аутентифікації. Перевірте правильність пароля SSH."
        except NoValidConnectionsError as e:
            logging.error(f"Помилка з'єднання: {e}")
            return f"Помилка з'єднання. Перевірте доступність маршрутизатора за IP-адресою {self.ip} та портом {self.ssh_port}."
        except SSHException as e:
            logging.error(f"Помилка SSH: {e}")
            return f"Помилка SSH: {e}"
        except Exception as e:
            logging.error(f"Невідома помилка при виконанні скрипта: {e}")
            return f"Невідома помилка при виконанні скрипта: {e}"

# Стан для зберігання даних користувача
user_state = {}

# Обробник команди /start
@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, 'Привіт! Я бот для управління маршрутизаторами.')
    logging.info(f"Користувач {message.from_user.username} почав взаємодію з ботом.")

# Обробник команди /id для запиту доступу
@bot.message_handler(commands=['id'])
def request_access(message):
    user_id = message.from_user.id
    user_name = message.from_user.username
    user_first_name = message.from_user.first_name
    user_last_name = message.from_user.last_name

    # Відправляємо повідомлення адміністраторам
    admin_message = f"Користувач {user_first_name} {user_last_name} ({user_name}) з ID {user_id} запитав доступ.\n" \
                    f"Будь ласка, відредагуйте файл routers.json для надання доступу."

    # Відправка повідомлення адміністраторам
    admin_bot_1 = telebot.TeleBot(ADMIN_BOT_1_TOKEN)
    admin_bot_2 = telebot.TeleBot(ADMIN_BOT_2_TOKEN)
    admin_bot_1.send_message(ADMIN_1_ID, admin_message)
    admin_bot_2.send_message(ADMIN_2_ID, admin_message)

    # Підтверджуємо запит користувачу
    bot.reply_to(message, "Ваш запит на доступ надіслано адміністраторам. Очікуйте їхнє рішення.")

# Відправка вибору маршрутизаторів
@bot.message_handler(commands=['run_script'])
def send_router_selection(message):
    logging.info(f"Користувач {message.from_user.username} вибрав команду /run_script.")
    
    try:
        with open('routers.json', 'r') as file:
            routers = json.load(file)

        logging.info(f"Завантажені роутери: {routers}")  # Логуємо всі роутери
    except Exception as e:
        logging.error(f"Помилка при завантаженні файлу routers.json: {e}")
        bot.reply_to(message, "Помилка при завантаженні даних про маршрутизатори.")
        return

    # Створюємо InlineKeyboardMarkup для роутерів
    keyboard = InlineKeyboardMarkup(row_width=1)
    for router in routers.keys():
        # Перевіряємо, чи має користувач доступ до роутера
        if str(message.from_user.id) in routers[router].get('allowed_users', []):
            # Створюємо кнопки з правильним callback_data, тепер використовуємо router_name в callback_data
            keyboard.add(InlineKeyboardButton(router, callback_data=f"router_{router}"))
    
    # Відправляємо повідомлення з кнопками вибору маршрутизаторів
    if keyboard.keyboard:
        bot.reply_to(message, "Виберіть маршрутизатор:", reply_markup=keyboard)
        user_state[message.chat.id] = {'state': 'waiting_for_router'}  # Зберігаємо стан користувача
    else:
        bot.reply_to(message, "У вас немає доступу до роутерів.")
        logging.info(f"Користувач {message.from_user.username} не має доступу до роутерів.")

# Обробка вибору маршрутизатора
@bot.callback_query_handler(func=lambda call: call.data.startswith('router_'))
def handle_router_selection(call):
    router_name = call.data.split('_')[1]  # Беремо ім'я маршрутизатора після 'router_'
    
    # Логуємо вибір маршрутизатора
    logging.info(f"Користувач вибрав маршрутизатор: {router_name}")

    try:
        with open('routers.json', 'r') as file:
            routers = json.load(file)
            logging.info(f"Завантажені роутери: {routers}")  # Логуємо всі роутери

        router = routers.get(router_name)
    except Exception as e:
        logging.error(f"Помилка при завантаженні файлу routers.json: {e}")
        bot.send_message(call.message.chat.id, "Помилка при завантаженні даних про маршрутизатори.")
        return

    if router and str(call.from_user.id) in router.get('allowed_users', []):
        # Логуємо вибір маршрутизатора
        logging.info(f"Користувач {call.from_user.username} вибрав маршрутизатор: {router_name}")

        # Зберігаємо вибраний маршрутизатор
        user_state[call.from_user.id] = {'router': router_name}

        # Створюємо InlineKeyboardMarkup для скриптів
        keyboard = InlineKeyboardMarkup(row_width=1)
        for script in router["scripts"]:
            keyboard.add(InlineKeyboardButton(script, callback_data=f"script_{router_name}_{script}"))

        # Відправляємо повідомлення з кнопками вибору скрипта
        bot.send_message(call.message.chat.id, f"Виберіть скрипт для {router_name}:", reply_markup=keyboard)
        user_state[call.from_user.id]['state'] = 'waiting_for_script'  # Переводимо користувача в стан вибору скрипта
    else:
        bot.send_message(call.message.chat.id, "Помилка: маршрутизатор не знайдений або у вас немає доступу.")
        logging.error(f"Маршрутизатор {router_name} не знайдений або користувач {call.from_user.username} не має доступу.")

# Обробка вибору скрипта
@bot.callback_query_handler(func=lambda call: call.data.startswith('script_'))
def handle_script_selection(call):
    router_name, script = call.data.split('_')[1], call.data.split('_')[2]

    # Запит пароля для виконання скрипта
    bot.send_message(call.message.chat.id, f"Введіть пароль для виконання скрипта '{script}' на маршрутизаторі {router_name}:")
    user_state[call.from_user.id]['script'] = script  # Зберігаємо вибраний скрипт
    user_state[call.from_user.id]['state'] = 'waiting_for_password'  # Переводимо користувача в стан вводу пароля

# Перевірка пароля та виконання скрипта
@bot.message_handler(func=lambda message: message.chat.id in user_state and user_state[message.chat.id]['state'] == 'waiting_for_password')
def verify_password_and_execute(message):
    router_name = user_state[message.chat.id].get('router')
    script = user_state[message.chat.id].get('script')

    try:
        with open('routers.json', 'r') as file:
            routers = json.load(file)

        router = routers.get(router_name)
    except Exception as e:
        logging.error(f"Помилка при завантаженні файлу routers.json: {e}")
        bot.reply_to(message, "Помилка при завантаженні даних про маршрутизатори.")
        return

    if router:
        # Перевірка пароля для виконання скрипта
        if message.text == router["script_password"]:  # Використовуємо script_password для перевірки
            ssh_client = RouterSSHClient(router["ip"], router["username"], router["ssh_password"], router["ssh_port"])  # Використовуємо ssh_password та ssh_port для підключення
            result = ssh_client.execute_script(script)

            # Логування
            execution_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            log_message = f"Скрипт '{script}' був виконаний на маршрутизаторі '{router_name}' в {execution_time}."
            logging.info(log_message)

            # Уведомлення адміністраторів
            notify_admins(execution_time, message.from_user.username, router_name, script)

            # Відповідь користувачу
            bot.reply_to(message, f"Результат виконання скрипта '{script}':\n{result}")
        else:
            bot.reply_to(message, "Невірний пароль для виконання скрипта. Спробуйте знову.")
            logging.warning(f"Користувач {message.from_user.username} ввів невірний пароль для скрипта {script}.")
    else:
        bot.reply_to(message, "Помилка: маршрутизатор не знайдений.")

# Уведомлення адміністраторів
def notify_admins(execution_time: str, username: str, router_name: str, script: str):
    admin_message = f"🔔 Статус виконання скрипта:\n\n" \
                    f"📅 Час: {execution_time}\n" \
                    f"👤 Хто запустив: {username}\n" \
                    f"🌐 Маршрутизатор: {router_name}\n" \
                    f"🖥 Скрипт: {script}"

    # Відправляємо повідомлення через обох ботів
    admin_bot_1 = telebot.TeleBot(ADMIN_BOT_1_TOKEN)
    admin_bot_2 = telebot.TeleBot(ADMIN_BOT_2_TOKEN)
    admin_bot_1.send_message(ADMIN_1_ID, admin_message)
    admin_bot_2.send_message(ADMIN_2_ID, admin_message)

# Запуск бота
if __name__ == "__main__":
    logging.info("Бот запущений.")
    bot.polling(none_stop=True)
