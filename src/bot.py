#!/usr/bin/env python3
import os
import sys
import logging
import asyncio
import fcntl
import tempfile
import shutil
from typing import List, Optional, Dict, Tuple
from datetime import datetime, timedelta
from contextlib import contextmanager
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest, GetParticipantsRequest
from telethon.tl.functions.messages import ImportChatInviteRequest, GetMessagesRequest, SendMessageRequest, GetDialogsRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import ChannelParticipantsSearch, User, Channel, Message, InputPeerEmpty
from telethon.errors import (
    FloodWaitError,
    ChatWriteForbiddenError,
    ChannelInvalidError,
    ChannelPrivateError,
    AuthKeyError,
    SecurityError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
    PhoneCodeInvalidError
)
from dotenv import load_dotenv
import csv
from docx import Document
from openpyxl import load_workbook
import time
from PIL import Image
from io import BytesIO
import pytesseract
import re
import xlrd
import textract
import PyPDF2
import striprtf.striprtf
from odf import opendocument
from odf.table import Table, TableRow, TableCell
import json

# Загружаем переменные окружения
load_dotenv()

# Настройка путей
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(BASE_DIR, 'logs')
SESSIONS_DIR = os.path.join(BASE_DIR, 'sessions')
SCREENSHOTS_DIR = os.path.join(BASE_DIR, 'captcha_screenshots')
os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(SESSIONS_DIR, exist_ok=True)
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('telegram_bot')

class SessionManager:
    def __init__(self, session_dir: str):
        self.session_dir = session_dir
        self.locks = {}
        self.lock_files = {}

    def acquire_lock(self, session_name: str) -> bool:
        """Получение блокировки для сессии"""
        try:
            lock_file = os.path.join(self.session_dir, f"{session_name}.lock")
            f = open(lock_file, 'w')
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.locks[session_name] = f
            self.lock_files[session_name] = lock_file
            return True
        except IOError:
            logger.error(f"Сессия {session_name} уже используется")
            return False
        except Exception as e:
            logger.error(f"Ошибка при получении блокировки: {str(e)}")
            return False

    def release_lock(self, session_name: str):
        """Освобождение блокировки сессии"""
        try:
            if session_name in self.locks:
                f = self.locks.pop(session_name)
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                f.close()
                
                lock_file = self.lock_files.pop(session_name, None)
                if lock_file and os.path.exists(lock_file):
                    try:
                        os.remove(lock_file)
                    except OSError:
                        pass
        except Exception as e:
            logger.error(f"Ошибка при освобождении блокировки: {str(e)}")

    def __del__(self):
        """Освобождение всех блокировок при уничтожении объекта"""
        for session_name in list(self.locks.keys()):
            self.release_lock(session_name)

class ResourceManager:
    @staticmethod
    @contextmanager
    def managed_file(file: BytesIO):
        """Контекстный менеджер для работы с файлами"""
        try:
            yield file
        finally:
            file.close()

    @staticmethod
    def cleanup_old_files(directory: str, max_age_hours: int = 24):
        """Очистка старых файлов"""
        try:
            current_time = datetime.now()
            for filename in os.listdir(directory):
                filepath = os.path.join(directory, filename)
                try:
                    file_time = datetime.fromtimestamp(os.path.getctime(filepath))
                    if (current_time - file_time).total_seconds() > max_age_hours * 3600:
                        os.remove(filepath)
                        logger.info(f"Удален старый файл: {filepath}")
                except Exception as e:
                    logger.error(f"Ошибка при удалении файла {filepath}: {str(e)}")
        except Exception as e:
            logger.error(f"Ошибка при очистке директории {directory}: {str(e)}")

class UserAccount:
    def __init__(self, phone: str, api_id: int, api_hash: str, session_manager: SessionManager):
        self.phone = phone
        self.api_id = api_id
        self.api_hash = api_hash
        self.client = None
        self.session_manager = session_manager
        self.session_name = f'user_{phone}'
        self.session_file = os.path.join(SESSIONS_DIR, self.session_name)
        self.phone_code_hash = None
        self.captcha_tasks = {}
        self.join_count = 0  # Счетчик вступлений
        self.current_delay = 10  # Начальная пауза 10 секунд

    async def start_client(self):
        """Запуск клиента"""
        try:
            if not self.session_manager.acquire_lock(self.session_name):
                return False

            logger.info("Создаю клиент...")
            # Используем только имя сессии, без пути
            self.client = TelegramClient(self.session_name, self.api_id, self.api_hash)
            
            logger.info("Подключаюсь к Telegram...")
            await self.client.connect()
            
            # Проверяем авторизацию
            if await self.client.is_user_authorized():
                logger.info("Клиент уже авторизован")
                return True
                
            logger.info("Отправляю код подтверждения...")
            result = await self.client.send_code_request(self.phone)
            self.phone_code_hash = result.phone_code_hash
            code = input("Введите код подтверждения: ")
            try:
                await self.client.sign_in(
                    phone=self.phone,
                    code=code,
                    phone_code_hash=self.phone_code_hash
                )
            except SessionPasswordNeededError:
                while True:
                    try:
                        password = input("Введите пароль двухфакторной аутентификации: ")
                        await self.client.sign_in(password=password)
                        break
                    except Exception as e:
                        logger.error(f"Ошибка при вводе пароля: {str(e)}")
                        retry = input("Хотите попробовать ввести пароль снова? (y/n): ")
                        if retry.lower() != 'y':
                            self.session_manager.release_lock(self.session_name)
                            return False
            
            logger.info("Успешно авторизован")
            return True
            
        except Exception as e:
            logger.error(f"Ошибка при запуске клиента: {str(e)}")
            self.session_manager.release_lock(self.session_name)
            if self.client:
                await self.client.disconnect()
            return False

    async def send_code(self):
        """Отправка кода подтверждения"""
        try:
            # Создаем и подключаем клиент, если его еще нет
            if not self.client:
                logger.info("Создаю клиент...")
                self.client = TelegramClient(self.session_name, self.api_id, self.api_hash)
                
            if not self.client.is_connected():
                logger.info("Подключаюсь к Telegram...")
                await self.client.connect()
                
            # Проверяем, не авторизован ли уже клиент
            if await self.client.is_user_authorized():
                logger.warning("Клиент уже авторизован")
                return True  # Возвращаем True, так как клиент уже авторизован
                
            try:
                # Отправляем запрос на код
                result = await self.client.send_code_request(phone=self.phone)
                
                if not result or not hasattr(result, 'phone_code_hash'):
                    logger.error("Не получен корректный ответ от Telegram")
                    return False
                    
                self.phone_code_hash = result.phone_code_hash
                
                # Логируем информацию о коде
                code_type = result.type.__class__.__name__
                logger.info(f"Код отправлен через {code_type}")
                logger.info(f"Phone code hash: {self.phone_code_hash}")
                
                return True
                
            except PhoneNumberInvalidError:
                logger.error(f"Неверный номер телефона: {self.phone}")
                return False
                
            except FloodWaitError as e:
                logger.error(f"Нужно подождать {e.seconds} секунд")
                return False
                
        except Exception as e:
            logger.error(f"Ошибка при отправке кода: {str(e)}")
            if self.client:
                await self.client.disconnect()
            return False

    async def sign_in(self, code: str):
        """Вход в аккаунт с кодом подтверждения"""
        try:
            if not self.client:
                logger.error("Клиент не инициализирован")
                return False
                
            if not self.client.is_connected():
                logger.info("Подключаюсь к Telegram...")
                await self.client.connect()
                
            if not self.phone_code_hash:
                logger.error("Отсутствует phone_code_hash")
                return False
                
            await self.client.sign_in(
                phone=self.phone,
                code=code,
                phone_code_hash=self.phone_code_hash
            )
            logger.info("Успешная авторизация")
            return True
            
        except SessionPasswordNeededError:
            logger.error("Требуется пароль двухфакторной аутентификации")
            return False
        except Exception as e:
            logger.error(f"Ошибка при входе: {str(e)}")
            if "phone code expired" in str(e).lower():
                logger.info("Код истек, отправляем новый")
                return await self.send_code()
            return False

    async def disconnect(self):
        """Отключение от аккаунта"""
        try:
            if self.client:
                await self.client.disconnect()
                self.client = None
                self.phone_code_hash = None
                
                # Освобождаем блокировку
                self.session_manager.release_lock(self.session_name)
                
                # НЕ удаляем файл сессии, чтобы сохранить авторизацию
                logger.info(f"Клиент отключен, сессия сохранена")
        except Exception as e:
            logger.error(f"Ошибка при отключении: {str(e)}")

    async def get_account_info(self):
        """Получение информации об аккаунте"""
        try:
            if not self.client:
                logger.error("Клиент не инициализирован")
                return None
                
            if not self.client.is_connected():
                logger.info("Подключаюсь к Telegram...")
                await self.client.connect()
                
            if not await self.client.is_user_authorized():
                logger.error("Клиент не авторизован")
                return None

            # Получаем информацию о пользователе
            me = await self.client.get_me()
            full_user = await self.client.get_entity(me.id)

            # Получаем список диалогов для подсчета каналов
            dialogs = await self.client.get_dialogs()
            
            # Считаем разные типы чатов
            channels = sum(1 for d in dialogs if d.is_channel)
            large_groups = sum(1 for d in dialogs if d.is_group and hasattr(d.entity, 'participants_count') and d.entity.participants_count > 200)
            small_groups = sum(1 for d in dialogs if d.is_group and hasattr(d.entity, 'participants_count') and d.entity.participants_count <= 200)
            private_chats = sum(1 for d in dialogs if d.is_user)
            
            # Считаем чаты, которые учитываются в лимите
            limited_chats = channels + large_groups
            max_limited_chats = 1000 if me.premium else 500

            # Формируем информацию об аккаунте
            account_info = {
                'first_name': me.first_name or "Без имени",
                'username': me.username or "Без username",
                'phone': self.phone,
                'account_type': "Premium" if me.premium else "Обычный",
                'channels': channels,
                'large_groups': large_groups,
                'small_groups': small_groups,
                'private_chats': private_chats,
                'total_chats': len(dialogs),
                'limited_chats': limited_chats,
                'limits': {
                    'max_limited_chats': max_limited_chats,
                    'joins_per_day': 300 if me.premium else 200
                },
                'available_joins': max_limited_chats - limited_chats
            }

            return account_info

        except Exception as e:
            logger.error(f"Ошибка при получении информации об аккаунте: {str(e)}")
            return None

    async def extract_chat_links(self, text: str) -> List[str]:
        """Извлечение ссылок на каналы из текста"""
        links = []
        seen_links = set()
        
        # Разбиваем текст на строки и обрабатываем каждую отдельно
        for line in text.split('\n'):
            line = line.strip()
            if not line:
                continue
            
            # Обрабатываем приватные ссылки
            if 'joinchat' in line or '+' in line:
                match = re.search(r'(?:https?://)?(?:t|telegram)\.me/(?:joinchat/|\+)([a-zA-Z0-9_-]+)', line)
                if match:
                    invite_hash = match.group(1)
                    link = f"https://t.me/joinchat/{invite_hash}"
                    if link not in seen_links:
                        links.append(link)
                        seen_links.add(link)
                        logger.info(f"Обработана приватная ссылка: {link}")
                    continue
            
            # Обрабатываем публичные ссылки и юзернеймы
            if line.startswith('@'):
                username = line[1:]
            elif line.startswith(('https://', 'http://', 't.me')):
                username = re.sub(r'^(?:https?://)?(?:t\.me/)?', '', line)
            else:
                username = line
            
            # Добавляем ссылку без дополнительных проверок
            if username:
                link = f"https://t.me/{username}"
                if link not in seen_links:
                    links.append(link)
                    seen_links.add(link)
                    logger.info(f"Обработана публичная ссылка: {link}")
        
        return links

    async def save_captcha_screenshot(self, chat, message: Message) -> Optional[str]:
        """Сохранение скриншота сообщения с капчей"""
        try:
            if not message or not message.media:
                return None
                
            # Скачиваем медиа из сообщения
            media = await self.client.download_media(message.media, file=BytesIO())
            if not media:
                return None
                
            # Создаем имя файла с timestamp
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"captcha_{chat.id}_{timestamp}.png"
            filepath = os.path.join(SCREENSHOTS_DIR, filename)
            
            # Сохраняем изображение
            if isinstance(media, BytesIO):
                image = Image.open(media)
                image.save(filepath)
                logger.info(f"Скриншот капчи сохранен: {filepath}")
                return filepath
            return None
            
        except Exception as e:
            logger.error(f"Ошибка при сохранении скриншота капчи: {str(e)}")
            return None

    async def process_captcha(self, chat, message: Message) -> Optional[str]:
        """Обработка капчи на изображении"""
        try:
            if not message or not message.media:
                return None
                
            # Скачиваем медиа из сообщения
            media = await self.client.download_media(message.media, file=BytesIO())
            if not media:
                return None
                
            # Создаем имя файла с timestamp
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"captcha_{chat.id}_{timestamp}.png"
            filepath = os.path.join(SCREENSHOTS_DIR, filename)
            
            # Сохраняем изображение
            if isinstance(media, BytesIO):
                image = Image.open(media)
                image.save(filepath)
                
                # Распознаем текст на изображении
                text = pytesseract.image_to_string(image, lang='eng')
                logger.info(f"Распознанный текст капчи: {text}")
                
                # Ищем задание в тексте
                task_match = re.search(r'what\s+is\s+(\d+)\s*[\+\-\*\/]\s*(\d+)', text.lower())
                if task_match:
                    num1 = int(task_match.group(1))
                    num2 = int(task_match.group(2))
                    operator = re.search(r'[\+\-\*\/]', text).group()
                    
                    # Вычисляем результат
                    if operator == '+':
                        result = num1 + num2
                    elif operator == '-':
                        result = num1 - num2
                    elif operator == '*':
                        result = num1 * num2
                    elif operator == '/':
                        result = num1 / num2
                    else:
                        return None
                        
                    logger.info(f"Задание: {num1} {operator} {num2} = {result}")
                    return str(result)
                    
                # Ищем другие типы заданий
                if "type the word" in text.lower():
                    word_match = re.search(r'type\s+the\s+word\s+"([^"]+)"', text.lower())
                    if word_match:
                        word = word_match.group(1)
                        logger.info(f"Задание: написать слово '{word}'")
                        return word
                        
                if "click the button" in text.lower():
                    button_match = re.search(r'click\s+the\s+button\s+with\s+the\s+word\s+"([^"]+)"', text.lower())
                    if button_match:
                        word = button_match.group(1)
                        logger.info(f"Задание: нажать кнопку со словом '{word}'")
                        return word
                        
            return None
            
        except Exception as e:
            logger.error(f"Ошибка при обработке капчи: {str(e)}")
            return None

    async def solve_captcha(self, chat, message: Message) -> bool:
        """Решение капчи и отправка ответа"""
        try:
            # Обрабатываем капчу
            answer = await self.process_captcha(chat, message)
            if not answer:
                logger.error("Не удалось распознать задание капчи")
                return False
                
            # Отправляем ответ
            await self.client(SendMessageRequest(
                peer=chat,
                message=answer,
                no_webpage=True
            ))
            
            logger.info(f"Отправлен ответ на капчу: {answer}")
            return True
            
        except Exception as e:
            logger.error(f"Ошибка при решении капчи: {str(e)}")
            return False

    async def join_chats(self, chat_links: List[str], status_msg) -> Dict:
        results = {
            "success": 0,
            "failed": 0,
            "failed_chats": [],
            "captcha_required": [],
            "captcha_screenshots": [],
            "captcha_solved": [],
            "already_member": 0  # Добавляем счетчик уже существующих подписок
        }

        try:
            if not chat_links:
                return {"error": "Список каналов пуст"}
                
            if not self.client:
                return {"error": "Клиент не инициализирован"}
                
            if not self.client.is_connected():
                logger.info("Подключаюсь к Telegram...")
                await self.client.connect()
                
            if not await self.client.is_user_authorized():
                return {"error": "Клиент не авторизован"}

            # Проверяем, в каких чатах мы уже состоим
            dialogs = await self.client.get_dialogs()
            existing_chats = {d.entity.username.lower(): d.entity for d in dialogs if d.is_channel and d.entity.username}
            
            # Подсчитываем количество чатов из списка, в которых мы уже состоим
            for link in chat_links:
                try:
                    username = link.split('/')[-1].lower()
                    if username in existing_chats:
                        results["already_member"] += 1
                except:
                    continue

            start_time = datetime.now()
            total_channels = len(chat_links)
            self.join_count = 0
            self.current_delay = 10
            last_status_update = datetime.now()
            channels_to_join = total_channels - results["already_member"]

            async def update_status(message):
                """Безопасное обновление статуса"""
                try:
                    nonlocal last_status_update
                    current_time = datetime.now()
                    
                    if (current_time - last_status_update).total_seconds() < 2:
                        return
                        
                    if status_msg and not getattr(status_msg, 'deleted', False):
                        try:
                            await self.client.edit_message(status_msg, message)
                            last_status_update = current_time
                        except Exception as e:
                            logger.error(f"Ошибка при обновлении статуса: {str(e)}")
                except Exception as e:
                    logger.error(f"Ошибка в update_status: {str(e)}")

            for i, link in enumerate(chat_links, 1):
                try:
                    time_passed = datetime.now() - start_time
                    if i > 1:
                        time_per_channel = time_passed / (i - 1)
                        channels_left = total_channels - i + 1
                        time_left = time_per_channel * channels_left
                    else:
                        time_left = timedelta(0)

                    # Проверяем, не состоим ли мы уже в этом канале
                    username = link.split('/')[-1].lower()
                    if username in existing_chats:
                        continue

                    status_text = (
                        f"🔄 Обработка каналов\n\n"
                        f"📊 Прогресс вступления: {results['success']}/{channels_to_join} ({(results['success']/channels_to_join*100 if channels_to_join > 0 else 0):.1f}%)\n"
                        f"📋 Всего каналов в списке: {total_channels}\n"
                        f"👥 Уже состоим в каналах: {results['already_member']}\n"
                        f"✅ Успешно вступили: {results['success']}\n"
                        f"❌ Ошибки: {results['failed']}\n"
                        f"⚠️ Капча: {len(results['captcha_required'])}\n"
                        f"⏳ Прошло времени: {str(time_passed).split('.')[0]}\n"
                        f"⌛️ Осталось примерно: {str(time_left).split('.')[0]}\n"
                        f"⏰ Текущая пауза: {self.current_delay:.1f} сек\n\n"
                        f"🔄 Обрабатываю: {link}"
                    )
                    await update_status(status_text)

                    try:
                        if '/joinchat/' in link or '+' in link:
                            invite_hash = link.split('/')[-1]
                            chat = await self.client(ImportChatInviteRequest(invite_hash))
                        else:
                            username = link.split('/')[-1]
                            chat = await self.client(JoinChannelRequest(username))
                        
                        results["success"] += 1
                        self.join_count += 1
                        logger.info(f"Успешно вступил в канал: {link}")
                        
                        if self.join_count == 1:
                            self.current_delay = 10
                        elif self.join_count == 2:
                            self.current_delay = 60
                        else:
                            self.current_delay = min(600, self.current_delay * 1.1)
                            
                        logger.info(f"Ожидание {self.current_delay} секунд перед следующим вступлением")
                        await asyncio.sleep(self.current_delay)
                            
                    except FloodWaitError as e:
                        wait_time = e.seconds
                        logger.error(f"Нужно подождать {wait_time} секунд перед следующей попыткой")
                        results["failed_chats"].append({
                            "link": link,
                            "error": f"Нужно подождать {wait_time} секунд"
                        })
                        results["failed"] += 1
                        
                        await update_status(
                            f"⚠️ Превышен лимит вступлений\n\n"
                            f"Нужно подождать {wait_time} секунд\n"
                            f"Успешно обработано: {results['success']} каналов"
                        )
                        
                        await asyncio.sleep(wait_time)
                        self.current_delay = 10
                        continue
                        
                    except Exception as e:
                        error_str = str(e).lower()
                        if "captcha" in error_str:
                            logger.warning(f"Требуется капча для канала: {link}")
                            results["captcha_required"].append(link)
                            results["failed"] += 1
                        else:
                            logger.error(f"Ошибка при вступлении в канал {link}: {str(e)}")
                            results["failed_chats"].append({
                                "link": link,
                                "error": str(e)
                            })
                            results["failed"] += 1
                            
                            # Пропускаем ожидание при ошибке
                            continue
                            
                except Exception as e:
                    logger.error(f"Ошибка при обработке канала {link}: {str(e)}")
                    results["failed"] += 1
                    results["failed_chats"].append({
                        "link": link,
                        "error": str(e)
                    })

            final_status = (
                f"✅ Обработка завершена\n\n"
                f"Всего ссылок: {len(chat_links)}\n"
                f"Успешно: {results['success']}\n"
                f"С ошибками: {results['failed']}\n"
                f"Требуется капча: {len(results['captcha_required'])}\n"
                f"Время выполнения: {str(datetime.now() - start_time).split('.')[0]}"
            )
            
            try:
                if status_msg and not getattr(status_msg, 'deleted', False):
                    await self.client.edit_message(status_msg, final_status)
            except Exception as e:
                logger.error(f"Ошибка при обновлении финального статуса: {str(e)}")
                try:
                    await self.client.send_message(status_msg.chat_id, final_status)
                except Exception as send_error:
                    logger.error(f"Ошибка при отправке финального статуса: {str(send_error)}")

        except Exception as e:
            logger.error(f"Ошибка в процессе вступления в каналы: {str(e)}")
            return {"error": str(e)}
        finally:
            return results

class TelegramBot:
    def __init__(self):
        self.client = None
        self.api_id = int(os.getenv('API_ID', 0))
        self.api_hash = os.getenv('API_HASH', '')
        self.bot_token = os.getenv('BOT_TOKEN', '')
        
        # Проверяем наличие необходимых переменных окружения
        if not all([self.api_id, self.api_hash, self.bot_token]):
            raise ValueError("Отсутствуют необходимые переменные окружения (API_ID, API_HASH, BOT_TOKEN)")
        
        self.session_manager = SessionManager(SESSIONS_DIR)
        self.resource_manager = ResourceManager()
        self.user_states = {}
        self.user_accounts = {}
        self.session_name = 'bot_session'  # Фиксированное имя сессии для бота
        self.cleanup_task = None

    async def start_client(self):
        """Инициализация и запуск клиента бота"""
        try:
            logger.info("Создаю клиент бота...")
            self.client = TelegramClient(self.session_name, self.api_id, self.api_hash)
            
            # Подключаемся к Telegram
            await self.client.start(bot_token=self.bot_token)
            
            # Настраиваем обработчики команд
            self.setup_handlers()
            
            # Запускаем задачу очистки
            self.cleanup_task = asyncio.create_task(self.periodic_cleanup())
            
            logger.info("Бот успешно запущен")
            return True
            
        except Exception as e:
            logger.error(f"Ошибка при запуске бота: {str(e)}")
            if self.client:
                await self.client.disconnect()
            return False

    async def get_or_create_user_account(self, user_id: int, phone: str = None) -> Optional[UserAccount]:
        """Получение существующего или создание нового аккаунта пользователя"""
        try:
            # Если аккаунт уже существует в памяти, возвращаем его
            if user_id in self.user_accounts:
                return self.user_accounts[user_id]
            
            # Если телефон не указан, ищем существующий файл сессии
            if not phone:
                # Ищем все файлы сессий
                session_files = [f for f in os.listdir(SESSIONS_DIR) 
                               if f.startswith('user_') and not f.endswith('.lock')]
                for session_file in session_files:
                    try:
                        # Извлекаем номер из имени файла user_PHONE
                        temp_phone = session_file[5:].split('.')[0].split('_')[0]
                        temp_account = UserAccount(temp_phone, self.api_id, self.api_hash, self.session_manager)
                        if await temp_account.start_client():
                            # Если успешно подключились, сохраняем аккаунт
                            self.user_accounts[user_id] = temp_account
                            return temp_account
                        await temp_account.disconnect()
                    except Exception as e:
                        logger.error(f"Ошибка при проверке сессии {session_file}: {str(e)}")
                return None
            
            # Создаем новый аккаунт с указанным телефоном
            account = UserAccount(phone, self.api_id, self.api_hash, self.session_manager)
            if await account.start_client():
                self.user_accounts[user_id] = account
                return account
            return None
            
        except Exception as e:
            logger.error(f"Ошибка при получении/создании аккаунта: {str(e)}")
            return None

    def setup_handlers(self):
        """Настройка обработчиков команд"""
        
        @self.client.on(events.NewMessage(pattern='/start'))
        async def start_handler(event):
            """Обработчик команды /start"""
            user_id = event.sender_id
            
            # Пробуем получить существующий аккаунт
            account = await self.get_or_create_user_account(user_id)
            
            if account:
                # Если нашли существующий аккаунт
                account_info = await account.get_account_info()
                if account_info:
                    await event.respond(
                        f"👋 С возвращением!\n\n"
                        f"👤 Пользователь: {account_info['first_name']}\n"
                        f"📱 Телефон: {account_info['phone']}\n"
                        f"💎 Тип аккаунта: {account_info['account_type']}\n\n"
                        f"📊 Статистика чатов:\n"
                        f"📢 Каналы: {account_info['channels']}\n"
                        f"👥 Большие группы (>200): {account_info['large_groups']}\n"
                        f"👥 Малые группы (≤200): {account_info['small_groups']}\n"
                        f"👤 Личные чаты: {account_info['private_chats']}\n"
                        f"📱 Всего чатов: {account_info['total_chats']}\n\n"
                        f"📈 Лимиты:\n"
                        f"📊 Каналы + большие группы: {account_info['limited_chats']}/{account_info['limits']['max_limited_chats']}\n"
                        f"➕ Доступно для вступления: {account_info['available_joins']}\n"
                        f"📥 Лимит вступлений в день: {account_info['limits']['joins_per_day']}",
                        buttons=[
                            [Button.text("📥 Загрузить список каналов", resize=True)],
                            [Button.text("📊 Статистика", resize=True)]
                        ]
                    )
                    return
            
            # Если существующий аккаунт не найден
            await event.respond(
                "👋 Привет! Я помогу вам управлять подписками на каналы Telegram.\n\n"
                "Для начала работы нужно подключить ваш аккаунт:",
                buttons=[
                    [Button.text("📱 Подключить аккаунт", resize=True)]
                ]
            )

        @self.client.on(events.NewMessage(pattern='📱 Подключить аккаунт'))
        async def connect_account_handler(event):
            """Обработчик кнопки подключения аккаунта"""
            sender = event.sender_id
            self.user_states[sender] = {'step': 'waiting_phone'}
            await event.respond(
                "Отправьте ваш номер телефона в формате: +7XXXXXXXXXX",
                buttons=[
                    [Button.text("❌ Отменить", resize=True)]
                ]
            )

        @self.client.on(events.NewMessage(pattern=r'^\+\d+$'))
        async def phone_handler(event):
            """Обработчик ввода номера телефона"""
            sender = event.sender_id
            if sender not in self.user_states or self.user_states[sender].get('step') != 'waiting_phone':
                return

            phone = event.text
            try:
                # Создаем аккаунт пользователя
                user_account = UserAccount(phone, self.api_id, self.api_hash, self.session_manager)
                self.user_states[sender]['account'] = user_account
                
                # Отправляем сообщение о проверке кода в терминале
                status_msg = await event.respond("🔄 Проверьте терминал для ввода кода подтверждения...")
                
                # Запускаем клиент и проходим авторизацию через терминал
                if await user_account.start_client():
                    self.user_accounts[sender] = user_account
                    del self.user_states[sender]
                    
                    # Получаем информацию об аккаунте
                    account_info = await user_account.get_account_info()
                    if account_info:
                        await status_msg.edit(
                            f"✅ Аккаунт успешно подключен!\n\n"
                            f"👤 Пользователь: {account_info['first_name']}\n"
                            f"📱 Телефон: {account_info['phone']}\n"
                            f"💎 Тип аккаунта: {account_info['account_type']}\n"
                            f"📊 Каналов: {account_info['channels_and_large_groups']}/{account_info['limits']['channels_max']}",
                            buttons=[
                                [Button.text("📥 Загрузить список каналов", resize=True)],
                                [Button.text("📊 Статистика", resize=True)]
                            ]
                        )
                    else:
                        raise Exception("Не удалось получить информацию об аккаунте")
                else:
                    await status_msg.edit(
                        "❌ Ошибка при авторизации.\n"
                        "Проверьте номер телефона и попробуйте снова.",
                        buttons=[
                            [Button.text("📱 Подключить аккаунт", resize=True)]
                        ]
                    )
                    del self.user_states[sender]
            except Exception as e:
                logger.error(f"Ошибка при обработке номера телефона: {str(e)}")
                await event.respond(
                    "❌ Произошла ошибка. Попробуйте позже.",
                    buttons=[
                        [Button.text("📱 Подключить аккаунт", resize=True)]
                    ]
                )
                if sender in self.user_states:
                    del self.user_states[sender]

        @self.client.on(events.NewMessage(pattern='❌ Отменить'))
        async def cancel_handler(event):
            """Обработчик отмены операции"""
            sender = event.sender_id
            if sender in self.user_states:
                if 'account' in self.user_states[sender]:
                    await self.user_states[sender]['account'].disconnect()
                del self.user_states[sender]
            
            await event.respond(
                "❌ Операция отменена",
                buttons=[
                    [Button.text("📱 Подключить аккаунт", resize=True)]
                ]
            )

        @self.client.on(events.NewMessage(pattern='📥 Загрузить список каналов'))
        async def upload_channels_handler(event):
            """Обработчик загрузки списка каналов"""
            sender = event.sender_id
            if sender not in self.user_accounts:
                await event.respond(
                    "❌ Сначала необходимо подключить аккаунт",
                    buttons=[
                        [Button.text("📱 Подключить аккаунт", resize=True)]
                    ]
                )
                return

            # Запрашиваем файл со списком каналов
            await event.respond(
                "Отправьте файл со списком каналов (txt, csv, docx или xlsx).\n"
                "Каждая ссылка должна быть в новой строке.\n"
                "Поддерживаются форматы:\n"
                "- t.me/channel\n"
                "- @channel\n"
                "- https://t.me/joinchat/...\n\n"
                "❗️ Внимание: Некоторые каналы могут требовать ручного прохождения капчи. "
                "В этом случае вам нужно будет вступить в такие каналы через официальный клиент Telegram.",
                buttons=[
                    [Button.text("❌ Отменить", resize=True)]
                ]
            )
            self.user_states[sender] = {'step': 'waiting_file'}

        @self.client.on(events.NewMessage(func=lambda e: e.file))
        async def process_file_handler(event):
            """Обработка загруженного файла"""
            sender = event.sender_id
            
            # Проверяем состояние пользователя
            if sender not in self.user_states:
                await event.respond("❌ Сначала нажмите кнопку 'Загрузить список каналов'")
                return
                
            if self.user_states[sender].get('step') != 'waiting_file':
                await event.respond("⚠️ Дождитесь окончания обработки предыдущего файла")
                return

            try:
                # Устанавливаем состояние обработки
                self.user_states[sender]['step'] = 'processing_file'
                
                # Отправляем сообщение о начале обработки
                status_msg = await event.respond("🔄 Обрабатываю файл...")

                # Получаем файл
                file = await event.message.download_media(file=BytesIO())
                
                # Извлекаем ссылки из файла
                links = await self.process_file(file)
                if not links:
                    await status_msg.edit("❌ В файле не найдено ссылок на каналы")
                    return

                # Проверяем аккаунт пользователя
                if sender not in self.user_accounts:
                    await status_msg.edit(
                        "❌ Сначала необходимо войти в аккаунт",
                        buttons=[
                            [Button.text("🔑 Войти в аккаунт", resize=True)]
                        ]
                    )
                    return

                user_account = self.user_accounts[sender]
                
                # Извлекаем ссылки на чаты
                chat_links = await user_account.extract_chat_links("\n".join(links))
                if not chat_links:
                    await status_msg.edit("❌ Не найдено корректных ссылок на каналы")
                    return

                # Получаем информацию об аккаунте
                me = await user_account.client.get_me()
                dialogs = await user_account.client.get_dialogs()
                current_chats = len(dialogs)
                max_chats = 1000 if me.premium else 500
                available_joins = max_chats - current_chats
                
                # Примерное время вступления (60 секунд на канал)
                estimated_time = len(chat_links) * 60
                hours = estimated_time // 3600
                minutes = (estimated_time % 3600) // 60
                
                # Обновляем статус с информацией об анализе
                await status_msg.edit(
                    f"📊 Детальный анализ списка:\n\n"
                    f"📋 Распознано ссылок в файле: {len(links)}\n"
                    f"✅ Валидных ссылок на каналы: {len(chat_links)}\n"
                    f"📱 Текущие подписки: {current_chats}/{max_chats}\n"
                    f"➕ Доступно для вступления: {available_joins}\n"
                    f"⌛️ Примерное время выполнения: {hours}ч {minutes}мин\n\n"
                    f"🔄 Начинаю вступление в каналы..."
                )

                # Запускаем процесс вступления в каналы
                results = await user_account.join_chats(chat_links, status_msg)
                
                if "error" in results:
                    await status_msg.edit(f"❌ Ошибка: {results['error']}")
                    return

                # Отправляем скриншоты капчи, если они есть
                if results.get('captcha_screenshots'):
                    await event.respond("📸 Скриншоты сообщений с капчей:")
                    for screenshot_info in results['captcha_screenshots']:
                        caption = f"Капча для канала: {screenshot_info['link']}"
                        try:
                            await self.client.send_file(
                                event.chat_id,
                                screenshot_info['screenshot'],
                                caption=caption
                            )
                        except Exception as e:
                            logger.error(f"Ошибка при отправке скриншота: {str(e)}")
                
                # Добавляем кнопки
                await event.respond(
                    "Выберите действие:",
                    buttons=[
                        [Button.text("📥 Загрузить список каналов", resize=True)],
                        [Button.text("📊 Статистика", resize=True)]
                    ]
                )
            
            except Exception as e:
                logger.error(f"Ошибка при обработке файла: {str(e)}")
                await event.respond(
                    f"❌ Произошла ошибка при обработке файла: {str(e)}",
                    buttons=[
                        [Button.text("📥 Загрузить список каналов", resize=True)]
                    ]
                )
            finally:
                # Очищаем состояние пользователя
                if sender in self.user_states:
                    del self.user_states[sender]

    async def process_file(self, file: BytesIO) -> List[str]:
        """Обработка загруженного файла и извлечение ссылок"""
        links = []
        seen = set()
        
        try:
            # Пробуем прочитать как Excel
            try:
                import pandas as pd
                df = pd.read_excel(file)
                
                for column in df.columns:
                    for value in df[column].dropna():
                        value = str(value).strip()
                        if value:
                            logger.info(f"Найдено значение в Excel: {value}")
                            if value.startswith(('https://t.me/', 'http://t.me/', '@', 't.me/')):
                                links.append(value)
                                logger.info(f"Добавлена ссылка из Excel: {value}")
                                
            except Exception as e:
                logger.error(f"Ошибка при чтении Excel: {str(e)}")
                
            # Если Excel не сработал, пробуем текстовые форматы
            if not links:
                logger.info("Пробуем текстовые форматы...")
                file.seek(0)
                
                # Пробуем разные кодировки
                encodings = ['utf-8', 'windows-1251', 'cp1251']
                
                for encoding in encodings:
                    try:
                        # Создаем временный файл
                        with tempfile.NamedTemporaryFile(mode='wb', delete=False) as temp:
                            temp.write(file.read())
                            temp_path = temp.name
                            
                        # Читаем файл с текущей кодировкой
                        with open(temp_path, 'r', encoding=encoding) as f:
                            content = f.read()
                            # Ищем все ссылки в тексте
                            tg_links = re.findall(r'(?:https?://)?(?:t\.me|telegram\.me)/[a-zA-Z0-9_@/+-]+', content)
                            username_links = re.findall(r'@[a-zA-Z][a-zA-Z0-9_]{3,}(?:\s|$)', content)
                            
                            for link in tg_links:
                                if link not in links:
                                    logger.info(f"Найдена ссылка в тексте: {link}")
                                    links.append(link)
                            
                            for username in username_links:
                                username = username.strip()
                                if username not in links:
                                    logger.info(f"Найден юзернейм: {username}")
                                    links.append(username)
                                    
                        # Удаляем временный файл
                        os.unlink(temp_path)
                        break  # Если успешно прочитали файл, выходим из цикла
                        
                    except Exception as e:
                        logger.error(f"Ошибка при чтении с кодировкой {encoding}: {str(e)}")
                        continue
                        
        except Exception as e:
            logger.error(f"Ошибка при обработке файла: {str(e)}")
            return []
            
        # Нормализуем ссылки
        normalized_links = []
        for link in links:
            # Убираем @ для юзернеймов
            if link.startswith('@'):
                link = link[1:]
                
            # Проверяем что ссылка начинается с http:// или https://
            if not link.startswith(('http://', 'https://')):
                link = f"https://t.me/{link}"
            
            if link not in seen:
                seen.add(link)
                normalized_links.append(link)
                logger.info(f"Нормализована ссылка: {link}")
                
        return normalized_links

    async def periodic_cleanup(self):
        """Периодическая очистка временных файлов"""
        while True:
            try:
                self.resource_manager.cleanup_old_files(SCREENSHOTS_DIR)
                self.resource_manager.cleanup_old_files(SESSIONS_DIR)
                await asyncio.sleep(3600)  # Очистка каждый час
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Ошибка при выполнении очистки: {str(e)}")
                await asyncio.sleep(60)

    async def cleanup(self):
        """Очистка ресурсов при завершении работы"""
        try:
            # Отменяем задачу очистки
            if self.cleanup_task and not self.cleanup_task.done():
                self.cleanup_task.cancel()
                try:
                    await self.cleanup_task
                except asyncio.CancelledError:
                    pass

            # Отключаем всех пользователей
            for user_id, user_account in list(self.user_accounts.items()):
                try:
                    await user_account.disconnect()
                except Exception as e:
                    logger.error(f"Ошибка при отключении пользователя {user_id}: {str(e)}")
                finally:
                    self.user_accounts.pop(user_id, None)
            
            # Отключаем бота
            if self.client:
                try:
                    await self.client.disconnect()
                except Exception as e:
                    logger.error(f"Ошибка при отключении бота: {str(e)}")
                finally:
                    self.client = None
            
            # Освобождаем блокировку
            try:
                self.session_manager.release_lock(self.session_name)
            except Exception as e:
                logger.error(f"Ошибка при освобождении блокировки: {str(e)}")
            
            # Очищаем временные файлы
            try:
                self.resource_manager.cleanup_old_files(SCREENSHOTS_DIR, max_age_hours=1)
                self.resource_manager.cleanup_old_files(SESSIONS_DIR, max_age_hours=1)
            except Exception as e:
                logger.error(f"Ошибка при очистке временных файлов: {str(e)}")
            
        except Exception as e:
            logger.error(f"Ошибка при очистке ресурсов: {str(e)}")
        finally:
            # Очищаем все состояния
            self.user_states.clear()
            self.user_accounts.clear()

    async def run(self):
        """Запуск бота с корректной обработкой завершения"""
        try:
            logger.info("Запуск бота...")
            if not await self.start_client():
                logger.error("Не удалось запустить бота")
                return
            
            # Запускаем прослушивание событий
            await self.client.run_until_disconnected()
                    
        except KeyboardInterrupt:
            logger.info("Бот остановлен пользователем")
        except Exception as e:
            logger.error(f"Критическая ошибка: {str(e)}")
        finally:
            await self.cleanup()
            logger.info("Бот остановлен")

    async def test_links(self):
        """Тестирование обработки ссылок"""
        test_links = [
            "https://t.me/otzivi_wb_ozon",
            "https://t.me/cargogood1",
            "https://t.me/kanalv2025",
            "https://t.me/wildberries_t0prf",
            "https://t.me/biznesdvigmoskva",
            "https://t.me/in4at",
            "https://t.me/site77777",
            "https://t.me/parisinfoexpress",
            "https://t.me/globalotzivi",
            "https://t.me/biznesuae",
            "https://t.me/halyava_na_wb_ozon",
            "https://t.me/vopros_ysmp"
        ]
        
        links = await self.extract_chat_links("\n".join(test_links))
        logger.info(f"Результат обработки ссылок:")
        for link in links:
            logger.info(f"Обработана ссылка: {link}")
        return links

if __name__ == "__main__":
    try:
        # Проверяем наличие необходимых директорий
        for directory in [LOGS_DIR, SESSIONS_DIR, SCREENSHOTS_DIR]:
            if not os.path.exists(directory):
                os.makedirs(directory, exist_ok=True)
                logger.info(f"Создана директория: {directory}")

        # Запускаем бота
        bot = TelegramBot()
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"Критическая ошибка: {str(e)}")
        sys.exit(1)

