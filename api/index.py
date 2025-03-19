from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.bot import TelegramBot

bot = None

def init_bot():
    global bot
    if bot is None:
        bot = TelegramBot()

class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length)
        update = json.loads(post_data.decode('utf-8'))
        
        init_bot()
        
        # Обработка обновления от Telegram
        try:
            bot.handle_update(update)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'OK')
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode('utf-8'))

def handler(event, context):
    if event['httpMethod'] == 'POST':
        try:
            body = json.loads(event['body'])
            init_bot()
            bot.handle_update(body)
            return {
                'statusCode': 200,
                'body': 'OK'
            }
        except Exception as e:
            return {
                'statusCode': 500,
                'body': str(e)
            }
    else:
        return {
            'statusCode': 405,
            'body': 'Method not allowed'
        } 