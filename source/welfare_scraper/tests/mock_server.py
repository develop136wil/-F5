from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

class MockHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()
        
        # Simple HTML simulating a notice board
        html = """
        <html>
        <body>
            <h1>Notice Board</h1>
            <table>
                <tbody>
                    <tr>
                        <td class="title"><a href="/notice/1">New Welfare Program for 영유아</a></td>
                        <td class="date">2024-01-01</td>
                    </tr>
                    <tr>
                        <td class="title"><a href="/notice/2">Old Notice</a></td>
                        <td class="date">2023-12-31</td>
                    </tr>
                </tbody>
            </table>
        </body>
        </html>
        """
        self.wfile.write(html.encode('utf-8'))

    def log_message(self, format, *args):
        return # Suppress logging

class MockServer:
    def __init__(self, port=8888):
        self.port = port
        self.server = HTTPServer(('localhost', port), MockHandler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
