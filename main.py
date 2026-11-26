"""
Tista Chargeback Calculator - Main Entry Point
Supports local desktop execution and Vercel/cloud serverless environments.
"""
import os
import sys

def app(environ, start_response):
    """WSGI entrypoint for Vercel and web servers."""
    status = '200 OK'
    response_headers = [('Content-Type', 'text/html; charset=utf-8')]
    start_response(status, response_headers)
    html_path = os.path.join(os.path.dirname(__file__), 'Chargeback Calculator.html')
    if os.path.exists(html_path):
        with open(html_path, 'rb') as f:
            return [f.read()]
    return [b'<h1>Tista Chargeback Calculator</h1>']

handler = app

if __name__ == "__main__":
    import Chargeback_Calculator
    # If run directly as python main.py
    sys.exit(0)
