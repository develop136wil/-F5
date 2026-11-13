import json
import os
import sys

# Add parent directory to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
import sqlite3

def migrate():
    print("Starting migration from JSON to SQLite...")
    
    database.init_db()
    conn = database.get_connection()
    cursor = conn.cursor()
    
    # 1. Migrate Sites
    if os.path.exists('sites.json'):
        with open('sites.json', 'r', encoding='utf-8') as f:
            sites = json.load(f)
            count = 0
            for site in sites:
                try:
                    cursor.execute('''
                    INSERT OR IGNORE INTO sites (name, url, base_url, selectors)
                    VALUES (?, ?, ?, ?)
                    ''', (
                        site['name'], 
                        site['url'], 
                        site.get('base_url', ''), 
                        json.dumps(site['selectors'], ensure_ascii=False)
                    ))
                    count += 1
                except Exception as e:
                    print(f"Error migrating site {site.get('name')}: {e}")
            print(f"Migrated {count} sites.")

    # 2. Migrate Data (Welfare Items)
    if os.path.exists('data.json'):
        with open('data.json', 'r', encoding='utf-8') as f:
            data = json.load(f)
            count = 0
            for item in data:
                try:
                    cursor.execute('''
                    INSERT OR IGNORE INTO welfare_items (
                        site_name, title, link, date, 
                        category, target, residence, 
                        start_age_months, end_age_months, age_detail, 
                        target_income, support_detail, eligibility_detail, 
                        contact, event_date, deadline
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        item.get('site_name'), item.get('title'), item.get('link'), item.get('date'),
                        item.get('category'), item.get('target'), item.get('residence'),
                        item.get('start_age_months'), item.get('end_age_months'), item.get('age_detail'),
                        item.get('target_income'), item.get('support_detail'), item.get('eligibility_detail'),
                        item.get('contact'), item.get('event_date'), item.get('deadline')
                    ))
                    count += 1
                except Exception as e:
                    print(f"Error migrating item {item.get('title')}: {e}")
            print(f"Migrated {count} items.")
            
    # 3. Migrate Keywords
    if os.path.exists('keywords.json'):
        with open('keywords.json', 'r', encoding='utf-8') as f:
            keywords = json.load(f)
            count = 0
            for k in keywords:
                try:
                    cursor.execute('INSERT OR IGNORE INTO keywords (keyword) VALUES (?)', (k,))
                    count += 1
                except: pass
            print(f"Migrated {count} keywords.")

    conn.commit()
    conn.close()
    print("Migration complete!")

if __name__ == "__main__":
    migrate()
