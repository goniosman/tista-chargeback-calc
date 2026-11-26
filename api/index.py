from http.server import BaseHTTPRequestHandler
import json

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        res = {
            "status": "online",
            "service": "Tista Chargeback Calculator",
            "backend": "https://tista-chargeback-calc-1.onrender.com"
        }
        self.wfile.write(json.dumps(res).encode('utf-8'))
        return
