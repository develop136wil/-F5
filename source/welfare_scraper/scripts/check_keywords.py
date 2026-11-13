import sqlite3
import os
import sys

# Add parent directory to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'welfare.db')

def check_keywords():
    if not os.path.exists(DB_FILE):
        print(f"Error: {DB_FILE} not found in current directory.")
        return

    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT keyword FROM keywords")
        rows = cursor.fetchall()
        
        print("===== Existing Keywords =====")
        if not rows:
            print("(No keywords found)")
        else:
            for row in rows:
                print(f"- {row[0]}")
        print("=============================")
        
        conn.close()
    except Exception as e:
        print(f"Error reading DB: {e}")

if __name__ == "__main__":
    check_keywords()
