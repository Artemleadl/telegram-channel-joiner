from http.server import HTTPServer, BaseHTTPRequestHandler
import json
from telethon import TelegramClient
import os
import asyncio
from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv()

# Получаем данные для авторизации
API_ID = int(os.getenv('API_ID', 0))
API_HASH = os.getenv('API_HASH', '')
BOT_TOKEN = os.getenv('BOT_TOKEN', '')

# Инициализируем клиент один раз
client = None

async def get_client():
    """Получение или создание клиента"""
    global client
    if client is None:
        client = TelegramClient('bot_session', API_ID, API_HASH)
        await client.start(bot_token=BOT_TOKEN)
    return client

async def handle_webhook(request_data):
    """Обработка входящего webhook от Telegram"""
    try:
        client = await get_client()
        
        # Обрабатываем обновление
        if 'message' in request_data:
            message = request_data['message']
            chat_id = message['chat']['id']
            text = message.get('text', '')
            
            if text == '/start':
                await client.send_message(chat_id, "👋 Привет! Я помогу вам управлять подписками на каналы Telegram.")
        
        return {"status": "success"}
        
    except Exception as e:
        return {"status": "error", "message": str(e)}

async def handle_request(request):
    """Асинхронный обработчик запроса"""
    # Проверяем метод
    if request.get('method', '') != 'POST':
        return {
            'statusCode': 405,
            'body': json.dumps({
                "status": "error",
                "message": "Method not allowed"
            })
        }
    
    try:
        # Получаем тело запроса
        body = request.get('body', '{}')
        if isinstance(body, str):
            request_data = json.loads(body)
        else:
            request_data = body
            
        # Обрабатываем webhook
        response = await handle_webhook(request_data)
        
        return {
            'statusCode': 200,
            'body': json.dumps(response)
        }
        
    except Exception as e:
        return {
            'statusCode': 500,
            'body': json.dumps({
                "status": "error",
                "message": str(e)
            })
        }

def handler(request):
    """Основной обработчик для Vercel"""
    if asyncio.get_event_loop().is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    else:
        loop = asyncio.get_event_loop()
    
    return loop.run_until_complete(handle_request(request)) 